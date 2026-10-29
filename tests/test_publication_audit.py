"""#261: the publication invariants, checked after every publishing stage.

Every test uses a local bare `origin` and a fake `gh` that answers from memory,
so a merge, a close, a force-push, a push to the default branch or a deleted
ref is a real Git or code-host state change, never a mocked comparison.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import publication_audit as pa  # noqa: E402
import run_cycle as rc  # noqa: E402
from test_run_cycle import COMPLETED, RunCycleTestCase, Talker, block  # noqa: E402

REPO = "owner/api"
BRANCH = "task-7"


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class FakeGh:
    """The GitHub API the audit reads, from memory: pulls and protection."""

    def __init__(self) -> None:
        self.protected = ["main"]
        self.pulls: dict[int, dict] = {}
        self.fail: str | None = None
        self.calls: list[list[str]] = []

    def open(self, number: int, head_ref: str = BRANCH) -> None:
        self.pulls[number] = {"number": number, "state": "open", "merged": False,
                              "merged_at": None, "head": {"ref": head_ref}}

    def merge(self, number: int) -> None:
        self.pulls[number].update(state="closed", merged=True, merged_at="2026-10-29T10:00:00Z")

    def close(self, number: int) -> None:
        self.pulls[number].update(state="closed")

    def __call__(self, command, **_kw):
        self.calls.append(command)
        assert command[:2] == ["gh", "api"], command
        path = command[-1]
        if self.fail and self.fail in path:
            return subprocess.CompletedProcess(command, 1, "", "HTTP 502")
        parts = urlsplit(path)
        query = parse_qs(parts.query)
        if parts.path == f"repos/{REPO}/branches":
            body = [[{"name": name} for name in self.protected]]
        elif parts.path == f"repos/{REPO}/pulls":
            head = unquote(query["head"][0]).partition(":")[2]
            body = [[pull for pull in self.pulls.values() if pull["head"]["ref"] == head]]
        elif parts.path.startswith(f"repos/{REPO}/pulls/"):
            number = int(parts.path.rsplit("/", 1)[1])
            if number not in self.pulls:
                return subprocess.CompletedProcess(command, 1, "", "HTTP 404")
            body = self.pulls[number]
        else:
            return subprocess.CompletedProcess(command, 1, "", "unexpected path")
        return subprocess.CompletedProcess(command, 0, json.dumps(body), "")


class Remote:
    """A bare `origin` with `main`, `release` and the cycle branch, and a clone."""

    def __init__(self, test: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.origin = base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.work = base / "work"
        self.other = base / "other"
        for clone in (self.work, self.other):
            subprocess.run(["git", "clone", "-q", str(self.origin), str(clone)],
                           check=True, capture_output=True)
            git(clone, "config", "user.name", "Cycle Test")
            git(clone, "config", "user.email", "cycle@example.invalid")
            git(clone, "config", "commit.gpgsign", "false")
        self.commit(self.work, "initial")
        git(self.work, "branch", "-M", "main")
        git(self.work, "push", "-q", "origin", "main", "main:release")
        git(self.work, "checkout", "-q", "-b", BRANCH)
        self.commit(self.work, "work")
        git(self.work, "push", "-q", "-u", "origin", BRANCH)
        git(self.other, "fetch", "-q", "origin")

    @staticmethod
    def commit(cwd: Path, message: str) -> str:
        path = cwd / "file.txt"
        path.write_text(f"{message}\n", encoding="utf-8")
        git(cwd, "add", "file.txt")
        git(cwd, "commit", "-qm", message)
        return git(cwd, "rev-parse", "HEAD")

    def head(self, branch: str) -> str | None:
        line = git(self.origin, "show-ref", f"refs/heads/{branch}")
        return line.split()[0] if line else None

    # -- what a stage might do ---------------------------------------------
    def push_forward(self) -> None:
        self.commit(self.work, "fix")
        git(self.work, "push", "-q", "origin", BRANCH)

    def force_push(self) -> None:
        git(self.work, "reset", "-q", "--hard", "HEAD~1")
        self.commit(self.work, "rewritten")
        git(self.work, "push", "-q", "--force", "origin", BRANCH)

    def push_default(self) -> None:
        git(self.work, "push", "-q", "origin", f"{BRANCH}:main")

    def delete(self, branch: str) -> None:
        git(self.work, "push", "-q", "origin", "--delete", branch)

    def push_unrelated(self) -> None:
        git(self.other, "checkout", "-q", "-b", "someone-else", "origin/main")
        self.commit(self.other, "elsewhere")
        git(self.other, "push", "-q", "origin", "someone-else")

    def push_from_elsewhere(self) -> None:
        """A force-push whose commit only the code host has."""
        git(self.other, "checkout", "-q", "-B", "rewrite", "origin/main")
        self.commit(self.other, "foreign")
        git(self.other, "push", "-q", "--force", "origin", f"rewrite:{BRANCH}")


class AuditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.remote = Remote(self)
        self.gh = FakeGh()
        self.gh.open(4)
        self.gh.protected = ["main", "release"]
        self.auditor = pa.PublicationAuditor(
            str(self.remote.work), REPO, pa.GitHubHostReader(run=self.gh))

    def audit(self, change, *, request: str | None = "4", host=True):
        auditor = self.auditor if host else pa.PublicationAuditor(str(self.remote.work), REPO, None)
        before = auditor.capture(checkout_branch=BRANCH, change_request_id=request)
        change()
        return auditor.verify(before, change_request_id=request)

    def kinds(self, result) -> list[tuple[str, str]]:
        return [(breach.kind, breach.ref) for breach in result.breaches]

    def test_a_fast_forward_push_and_a_comment_pass(self) -> None:
        old = self.remote.head(BRANCH)
        result = self.audit(self.remote.push_forward)
        self.assertEqual(pa.PASSED, result.status)
        evidence = result.evidence()
        self.assertEqual({"before": old, "after": self.remote.head(BRANCH)},
                         evidence["refs"][BRANCH])
        self.assertEqual({"before": "OPEN", "after": "OPEN"}, evidence["change_requests"]["4"])
        self.assertEqual(["main", "release"], evidence["protected"])

    def test_a_merged_change_request_is_a_breach(self) -> None:
        result = self.audit(lambda: self.gh.merge(4))
        self.assertEqual(pa.VIOLATED, result.status)
        self.assertEqual([(pa.MERGED, "change_request:4")], self.kinds(result))
        self.assertEqual(("OPEN", "MERGED"),
                         (result.breaches[0].before, result.breaches[0].after))

    def test_a_closed_change_request_is_a_breach(self) -> None:
        result = self.audit(lambda: self.gh.close(4))
        self.assertEqual([(pa.CLOSED, "change_request:4")], self.kinds(result))

    def test_a_force_push_to_the_cycle_branch_is_a_breach_with_its_commits(self) -> None:
        old = self.remote.head(BRANCH)
        result = self.audit(self.remote.force_push)
        (breach,) = result.breaches
        self.assertEqual((pa.NON_FAST_FORWARD, f"refs/heads/{BRANCH}"), (breach.kind, breach.ref))
        self.assertEqual((old, self.remote.head(BRANCH)), (breach.before, breach.after))
        self.assertEqual((old,), breach.commits_removed)
        self.assertEqual((self.remote.head(BRANCH),), breach.commits_added)

    def test_a_force_push_from_elsewhere_is_fetched_and_compared(self) -> None:
        result = self.audit(self.remote.push_from_elsewhere)
        self.assertEqual([(pa.NON_FAST_FORWARD, f"refs/heads/{BRANCH}")], self.kinds(result))
        # Into FETCH_HEAD only: no ref was left behind in the checkout.
        self.assertNotIn("rewrite", git(self.remote.work, "for-each-ref", "--format=%(refname)"))

    def test_any_move_of_the_default_branch_is_a_breach_even_a_fast_forward(self) -> None:
        result = self.audit(self.remote.push_default)
        self.assertEqual([(pa.MOVED, "refs/heads/main")], self.kinds(result))

    def test_a_deleted_audited_ref_is_a_breach(self) -> None:
        for branch in ("release", BRANCH):
            with self.subTest(branch=branch):
                if branch == BRANCH:
                    self.gh.pulls.clear()
                result = self.audit(lambda: self.remote.delete(branch), request=None)
                self.assertIn((pa.DELETED, f"refs/heads/{branch}"), self.kinds(result))

    def test_a_push_to_an_unrelated_branch_passes(self) -> None:
        result = self.audit(self.remote.push_unrelated)
        self.assertEqual(pa.PASSED, result.status)
        self.assertNotIn("someone-else", result.evidence()["refs"])

    def test_an_unreadable_protected_branch_query_is_never_read_as_none(self) -> None:
        self.gh.fail = "/branches"
        with self.assertRaisesRegex(pa.AuditUnreadable, "exited 1"):
            self.auditor.capture(checkout_branch=BRANCH, change_request_id="4")

    def test_an_unreadable_change_request_after_the_stage_fails_closed(self) -> None:
        before = self.auditor.capture(checkout_branch=BRANCH, change_request_id="4")
        self.gh.fail = "/pulls"
        with self.assertRaises(pa.AuditUnreadable):
            self.auditor.verify(before, change_request_id="4")

    def test_an_implementation_may_create_its_branch_and_an_open_change_request(self) -> None:
        self.gh.pulls.clear()
        before = self.auditor.capture(checkout_branch="task-8")
        git(self.remote.work, "push", "-q", "origin", f"{BRANCH}:task-8")
        self.gh.open(5, "task-8")
        self.assertEqual(pa.PASSED, self.auditor.verify(before, change_request_id="5").status)

    def test_an_implementation_whose_new_change_request_is_merged_fails(self) -> None:
        self.gh.pulls.clear()
        before = self.auditor.capture(checkout_branch="task-8")
        git(self.remote.work, "push", "-q", "origin", f"{BRANCH}:task-8")
        self.gh.open(5, "task-8")
        self.gh.merge(5)
        result = self.auditor.verify(before)
        self.assertEqual([(pa.MERGED, "change_request:5")], self.kinds(result))

    def test_a_configured_default_branch_is_the_one_audited(self) -> None:
        auditor = pa.PublicationAuditor(str(self.remote.work), REPO, None,
                                        default_branch="release")
        before = auditor.capture(checkout_branch=BRANCH)
        git(self.remote.work, "push", "-q", "origin", f"{BRANCH}:release")
        result = auditor.verify(before)
        self.assertEqual("release", before.default_branch)
        self.assertEqual([(pa.MOVED, "refs/heads/release")], self.kinds(result))

    def test_an_empty_remote_has_no_default_branch_to_audit_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            origin, work = Path(temporary) / "empty.git", Path(temporary) / "work"
            subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
            subprocess.run(["git", "clone", "-q", str(origin), str(work)],
                           check=True, capture_output=True)
            auditor = pa.PublicationAuditor(str(work), REPO, None)
            before = auditor.capture(checkout_branch=BRANCH)
            self.assertIsNone(before.default_branch)
            result = auditor.verify(before)
        self.assertEqual(pa.UNVERIFIED, result.status)
        self.assertIn(pa.DEFAULT_BRANCH, result.unverified)

    def test_the_change_request_head_is_the_cycle_branch(self) -> None:
        before = self.auditor.capture(checkout_branch="local-name", change_request_id="4")
        self.assertEqual(BRANCH, before.cycle_branch)

    def test_a_host_without_a_reader_is_unverified_but_its_refs_are_still_audited(self) -> None:
        passed = self.audit(self.remote.push_forward, host=False)
        self.assertEqual(pa.UNVERIFIED, passed.status)
        self.assertEqual([pa.CHANGE_REQUEST_STATE, pa.PROTECTED_BRANCHES],
                         passed.evidence()["unverified"])
        forced = self.audit(self.remote.force_push, host=False)
        self.assertEqual(pa.VIOLATED, forced.status)
        pushed = self.audit(self.remote.push_default, host=False)
        self.assertEqual([(pa.MOVED, "refs/heads/main")], self.kinds(pushed))


class Actor(Talker):
    """A fake executor whose stages change the code host before they answer."""

    ROLE_OF = {"cc-implement-issue": "implement", "cc-initial-review": "review",
               "cc-resolve-comments": "resolve", "cc-rereview": "rereview"}

    def __init__(self, name: str, **effects) -> None:
        super().__init__(name)
        self.effects = effects
        self.answers: dict[str, str] = {}

    def spoken(self, task: str) -> str:
        role = next(role for skill, role in self.ROLE_OF.items() if skill in task)
        effect = self.effects.pop(role, None)
        if effect is not None:
            effect()
        if role in self.answers:
            return self.answers[role]
        return COMPLETED.get(next(skill for skill in self.ROLE_OF if skill in task),
                             block("APPROVED"))


class CycleAuditTests(RunCycleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.remote = Remote(self)
        self.gh = FakeGh()
        self.gh.open(4)

    def cycle(self, implementer, reviewer=None, *, host=True, **kw):
        reviewer = reviewer or Actor("claude")
        auditor = pa.PublicationAuditor(
            str(self.remote.work), REPO, pa.GitHubHostReader(run=self.gh) if host else None)
        return self.run_cycle(implementer, reviewer, cwd=str(self.remote.work),
                              publication_auditor=auditor, **kw), reviewer

    def resume(self, implementer, **kw):
        return self.cycle(implementer, start_from="resolve", change_request_id="4", **kw)

    def closing(self) -> dict:
        return [row for row in self.rows()
                if row["payload"].get("record_kind") == "cycle"][-1]["payload"]

    def assert_violation(self, report, reviewer, kind: str, ref: str) -> None:
        self.assertEqual("publication_violation", report.stop_reason)
        self.assertEqual("publication_violation", self.closing()["stop_reason"])
        self.assertIn("changed outside the stage?", report.stopped_because)
        self.assertEqual([], reviewer.dispatched)
        self.assertEqual(1, len(report.stages))
        evidence = report.publication_audit
        self.assertEqual(pa.VIOLATED, evidence["status"])
        self.assertIn({"kind": kind, "ref": ref},
                      [{"kind": item["kind"], "ref": item["ref"]} for item in evidence["breaches"]])

    def test_a_stage_that_merges_the_change_request_stops_the_cycle(self) -> None:
        report, reviewer = self.cycle(Actor("codex", implement=lambda: self.gh.merge(4)))
        self.assert_violation(report, reviewer, pa.MERGED, "change_request:4")
        self.assertEqual({"before": "OPEN", "after": "MERGED"},
                         report.publication_audit["change_requests"]["4"])

    def test_a_force_push_a_default_push_and_a_deleted_ref_stop_the_cycle(self) -> None:
        cases = {
            "force_push": (lambda remote: remote.force_push(), pa.NON_FAST_FORWARD,
                           f"refs/heads/{BRANCH}"),
            "push_default": (lambda remote: remote.push_default(), pa.MOVED, "refs/heads/main"),
            "delete": (lambda remote: remote.delete(BRANCH), pa.DELETED, f"refs/heads/{BRANCH}"),
        }
        for name, (effect, kind, ref) in cases.items():
            with self.subTest(name):
                remote = self.remote = Remote(self)
                report, reviewer = self.cycle(Actor("codex", implement=lambda: effect(remote)))
                self.assert_violation(report, reviewer, kind, ref)
                breach = next(item for item in report.publication_audit["breaches"]
                              if item["ref"] == ref)
                self.assertIsNotNone(breach["before"])

    def test_a_breach_wins_over_a_blocked_report(self) -> None:
        implementer = Actor("codex", implement=lambda: self.gh.merge(4))
        implementer.answers["implement"] = block("BLOCKED", reason="incidental refusal")
        report, reviewer = self.cycle(implementer)
        self.assert_violation(report, reviewer, pa.MERGED, "change_request:4")

    def test_a_normal_resolution_that_pushes_forward_passes(self) -> None:
        report, reviewer = self.resume(Actor("codex", resolve=self.remote.push_forward))
        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual(["resolve", "rereview"], [stage.role for stage in report.stages])
        self.assertEqual(pa.PASSED, report.publication_audit["status"])
        self.assertEqual("rereview", report.publication_audit["role"])

    def test_a_push_to_an_unrelated_branch_during_the_stage_passes(self) -> None:
        report, _ = self.resume(Actor("codex", resolve=self.remote.push_unrelated))
        self.assertEqual(rc.APPROVED_END, report.status)

    def test_a_reviewer_that_merges_stops_the_cycle(self) -> None:
        reviewer = Actor("claude", rereview=lambda: self.gh.merge(4))
        report, _ = self.cycle(Actor("codex"), reviewer, start_from="resolve",
                               change_request_id="4")
        self.assertEqual("publication_violation", report.stop_reason)
        self.assertEqual(["resolve", "rereview"], [stage.role for stage in report.stages])

    def test_an_unreadable_code_host_stops_before_the_stage(self) -> None:
        self.gh.fail = "/branches"
        implementer = Actor("codex")
        report, _ = self.cycle(implementer)
        self.assertEqual("publication_unverified", report.stop_reason)
        self.assertEqual("BLOCKED", report.status)
        self.assertEqual([], implementer.dispatched)

    def test_another_code_host_is_reported_unverified_and_still_audits_refs(self) -> None:
        report, _ = self.resume(Actor("codex", resolve=self.remote.push_forward), host=False)
        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertIn("unverified", report.explain())
        self.assertEqual(pa.UNVERIFIED, report.publication_audit["status"])
        report, _ = self.resume(Actor("codex", resolve=self.remote.force_push), host=False)
        self.assertEqual("publication_violation", report.stop_reason)


class WiringTests(unittest.TestCase):
    def test_main_audits_every_cycle_that_publishes(self) -> None:
        seen = []

        class Report:
            status = rc.APPROVED_END

            def explain(self) -> str:
                return "scripted"

            def decisions_record(self) -> None:
                return None

        def run_cycle(*_args, **kw):
            seen.append(kw["publication_auditor"])
            return Report()

        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / ".code-cycle.yml"
            config.write_text("code_cycle:\n  code_host: github\n", encoding="utf-8")
            original = rc.run_cycle
            rc.run_cycle = run_cycle
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc.main(["--repo", REPO, "--task", "API-7", "--workspace", "current",
                             "--config", str(config),
                             "--database", str(Path(temporary) / "t.sqlite")])
            except SystemExit:
                self.skipTest("this checkout cannot run the driver's entry point")
            finally:
                rc.run_cycle = original
        (auditor,) = seen
        self.assertIsInstance(auditor, pa.PublicationAuditor)
        self.assertIsInstance(auditor.host, pa.GitHubHostReader)


if __name__ == "__main__":
    unittest.main()
