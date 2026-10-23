"""#228: the parallel CI policy, driven against a controlled fake CI provider.

Review overlaps with the checks; approval needs a clean review *and* a CI pass
of the same SHA, decided by one deterministic wait in the runtime. `strict`
stays the original flow.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import ci_gate as gate  # noqa: E402
import cycle_history as ch  # noqa: E402
import cycle_journal as cj  # noqa: E402
import review_contract as contract  # noqa: E402
import run_cycle as rc  # noqa: E402
import telemetry as tm  # noqa: E402
from test_run_cycle import RunCycleTestCase, Talker, block  # noqa: E402
from test_executors import git, git_repo  # noqa: E402

HEAD = "a" * 40
FIX = "b" * 40
OTHER = "c" * 40
LATER = "d" * 40
AWAITING = tm.AWAITING_CI_STATUS
ATTEMPT = re.compile(r"Runtime attempt_id: ([^. ]+)\.")
ROLE_OF = {"cc-implement-issue": "implement", "cc-initial-review": "review",
           "cc-resolve-comments": "resolve", "cc-rereview": "rereview"}
FINDING = {"id": "REV-001", "severity": "high", "status": "open", "disposition": "-",
           "blocks_approval": True}


def check(name: str = "build", state: str = gate.SUCCESS, order: int = 1) -> gate.Check:
    return gate.Check(name, "github-actions", state, order, 15368)


PENDING = (check(state=gate.PENDING),)
PASSED = (check(),)
FAILED = (check(state=gate.FAILED),)


class Clock:
    """Time that only moves when the CI gate or a fake stage moves it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self.on_sleep = None

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.now)


class FakeGitHub:
    """One pull request in memory: its head, its comments and its checks.

    It is the controlled fake CI provider of #228. Each SHA's checks follow a
    script, one entry per read, the last repeating; nothing changes unless the
    test or a fake stage changes it. `log` keeps the order in which stages were
    dispatched and checks were read, with the CI state each one saw.
    """

    def __init__(self, head: str = HEAD, clock: Clock | None = None) -> None:
        self.head = head
        self.state = "OPEN"
        self.items: list[contract.ProviderComment] = []
        self.scripts: dict[str, list[tuple]] = {}
        self.finishes: dict[str, tuple[float, tuple]] = {}
        self.required_checks: tuple = ()
        self.log: list[tuple] = []
        self.unreadable: str | None = None
        self.clock = clock

    def set_checks(self, sha: str, *reads: tuple) -> None:
        self.scripts[sha] = list(reads)

    def finish_at(self, sha: str, when: float, checks: tuple = PASSED) -> None:
        """Checks of `sha` that run until `when` on the clock, then conclude."""
        self.finishes[sha] = (when, checks)

    def current(self, sha: str) -> tuple:
        if sha in self.finishes:
            when, checks = self.finishes[sha]
            return checks if self.clock() >= when else PENDING
        return (self.scripts.get(sha) or [PENDING])[0]

    def ci_state(self, sha: str) -> str:
        states = {item.state for item in self.current(sha)}
        return (gate.FAILED if gate.FAILED in states
                else gate.PENDING if gate.PENDING in states or not states else gate.SUCCESS)

    # -- the CheckProvider the gate reads ---------------------------------
    def pull(self, repo, number):
        if self.unreadable:
            raise gate.CheckReadError(self.unreadable)
        return gate.PullState(self.state, self.head, "main")

    def required(self, repo, base_ref):
        return self.required_checks

    def checks(self, repo, sha):
        if self.unreadable:
            raise gate.CheckReadError(self.unreadable)
        self.log.append(("ci_read", sha, self.ci_state(sha)))
        if sha in self.finishes:
            return self.current(sha)
        reads = self.scripts.setdefault(sha, [PENDING])
        return reads.pop(0) if len(reads) > 1 else reads[0]

    # -- the HistoryRemote the cycle publishes through --------------------
    def probe(self, repo, number):
        latest = max((item.updated_at for item in self.items if item.updated_at), default=None)
        return ch.RemoteMark(self.head, len(self.items), latest)

    def comments(self, repo, number, since=None):
        if self.unreadable:
            raise rc.CycleDriverError(self.unreadable)
        return [item for item in self.items
                if since is None or (item.updated_at and item.updated_at >= since)]

    def publish(self, body: str, author: str = "reviewer") -> None:
        number = len(self.items) + 1
        self.items.append(contract.ProviderComment(
            author=author, body=body, id=str(100 + number),
            updated_at=f"2026-10-13T10:{number // 60:02d}:{number % 60:02d}Z"))


def receipt(stage: str, attempt: str, status: str, head: str, *, number: str = "4") -> str:
    return "<!-- code-cycle-stage " + json.dumps({
        "schema": 2, "repo": "owner/api", "change_request_id": number, "stage": stage,
        "attempt_id": attempt, "head_sha": head, "status": status,
        "finding_outcomes": []}) + " -->"


def published(stage: str, status: str, head: str, attempt: str, run: int,
              findings=()) -> str:
    """A stage comment the way the skills publish one: run line, findings, receipt."""
    if stage == "resolve":
        line = (f"#### [CCT-20261013-{run:03d}] · cheap_coder · openai/luna→? · high · "
                f"triaged:{head} · schema:2")
    else:
        line = (f"#### [CCR-20261013-{run:03d}] · senior_reviewer · "
                "anthropic/sonnet-5→? · high · schema:2")
    headers = [f"#### [{item.get('id')}] · {item.get('severity', 'high')} · "
               f"{'open' if item.get('status') in {'open', 'still_open'} else item.get('status')} · "
               f"{item.get('disposition', '-')} · "
               f"blocks:{'yes' if item.get('blocks_approval') else 'no'} — Leak"
               for item in findings]
    return "\n".join(["Summary.", line, *headers, receipt(stage, attempt, status, head)])


class Stage(Talker):
    """A fake stage answering from a script and publishing like a real one.

    Each answer is `(status, fields)`. `push` moves the change request's head
    before the stage reports; `publish=False` leaves no comment; every other
    comment-publishing stage leaves its schema-2 receipt. With `wait_for_ci`,
    the stage honours the strict contract and waits for its head's checks.
    """

    def __init__(self, name: str, github: FakeGitHub, clock: Clock | None = None, *,
                 wait_for_ci: bool = False, duration: float = 0, **scripts) -> None:
        super().__init__(name)
        self.github = github
        self.clock = clock
        self.wait_for_ci = wait_for_ci
        self.duration = duration
        self.scripts = {role: list(answers) for role, answers in scripts.items()}
        self.prompts: dict[str, list[str]] = {}
        self.runs = 0

    def spoken(self, task: str) -> str:
        skill = next(name for name in ROLE_OF if name in task)
        role = ROLE_OF[skill]
        self.prompts.setdefault(role, []).append(task)
        github = self.github
        github.log.append(("dispatch", role, github.ci_state(github.head),
                           self.clock() if self.clock else None))
        default = {"implement": ("IMPLEMENTED", {}), "review": (AWAITING, {}),
                   "resolve": ("RESOLVED", {"push": FIX}), "rereview": (AWAITING, {})}[role]
        status, fields = (self.scripts[role].pop(0) if self.scripts.get(role) else default)
        fields = dict(fields)
        push = fields.pop("push", None)
        publish = fields.pop("publish", True)
        author = fields.pop("author", "reviewer")
        if push:
            github.head = push
        if self.clock is not None:
            self.clock.now += self.duration
            if self.wait_for_ci and role in {"implement", "resolve"}:
                # The strict contract: the stage itself waits for its head.
                while github.ci_state(github.head) == gate.PENDING:
                    github.checks("owner/api", github.head)
                    self.clock.now += 30
        fields.setdefault("head_sha", github.head)
        if role in {"review", "rereview"}:
            fields.setdefault("reviewed_head_sha", github.head)
            blocking = [item["id"] for key in ("findings", "verified_findings", "new_findings")
                        for item in fields.get(key, [])
                        if item.get("status") in {"open", "still_open"}
                        and item.get("blocks_approval")]
            fields.setdefault("blocking_findings", blocking)
            fields.setdefault("findings" if role == "review" else "verified_findings", [])
        match = ATTEMPT.search(task)
        if publish and match and role != "implement":
            self.runs += 1
            findings = [*fields.get("findings", []), *fields.get("verified_findings", []),
                        *fields.get("new_findings", [])]
            github.publish(published(role, status, github.head, match.group(1), self.runs,
                                     findings), author=author)
        return block(status, **fields)


class ParallelTestCase(RunCycleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = Clock()
        self.github = FakeGitHub(clock=self.clock)

    def stages(self, **scripts):
        implement = {role: answers for role, answers in scripts.items()
                     if role in {"implement", "resolve"}}
        review = {role: answers for role, answers in scripts.items()
                  if role in {"review", "rereview"}}
        self.implementer = Stage("codex", self.github, self.clock, **implement)
        self.reviewer = Stage("claude", self.github, self.clock, **review)
        return self.implementer, self.reviewer

    def parallel(self, **kw):
        implementer, reviewer = (kw.pop("implementer", None), kw.pop("reviewer", None))
        if implementer is None:
            implementer, reviewer = self.implementer, self.reviewer
        kw.setdefault("ci_policy", "parallel")
        kw.setdefault("ci_checks", self.github)
        kw.setdefault("history_remote", self.github)
        kw.setdefault("trusted_authors", ("reviewer",))
        kw.setdefault("code_host", "github")
        kw.setdefault("ci_clock", self.clock)
        kw.setdefault("ci_sleep", self.clock.sleep)
        kw.setdefault("ci_timeout", 300)
        return self.run_cycle(implementer, reviewer, **kw)

    def events(self) -> list[tuple]:
        return [entry[:3] for entry in self.github.log]

    def dispatched(self, role: str | None = None) -> list[tuple]:
        return [entry for entry in self.github.log if entry[0] == "dispatch"
                and (role is None or entry[1] == role)]

    def rows(self, kind: str | None = None) -> list[dict]:
        rows = self.store.rows("owner/api")
        return [row for row in rows if kind is None or row["payload"].get("record_kind") == kind]

    def closing(self) -> dict:
        return self.rows("cycle")[-1]["payload"]


class KeyCaseTests(ParallelTestCase):
    """The two cases #228 names, plus the dispatch ordering behind them."""

    def test_the_review_is_dispatched_while_the_implementation_checks_are_pending(self) -> None:
        self.stages()
        self.github.set_checks(HEAD, PENDING, PENDING, PASSED)

        report = self.parallel()

        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual([("dispatch", "implement", gate.PENDING),
                          ("dispatch", "review", gate.PENDING),
                          ("ci_read", HEAD, gate.PENDING), ("ci_read", HEAD, gate.PENDING),
                          ("ci_read", HEAD, gate.SUCCESS)], self.events())
        (implement,) = self.implementer.prompts["implement"]
        self.assertIn(rc.PARALLEL_IMPLEMENT, implement)
        (review,) = self.reviewer.prompts["review"]
        self.assertIn(rc.PARALLEL_REVIEW, review)

    def test_resolution_receives_the_recovered_review_boundary(self) -> None:
        self.stages(review=[("CHANGES_REQUESTED", {"findings": [FINDING]})])
        self.github.set_checks(HEAD, PENDING)
        self.github.set_checks(FIX, PASSED)
        with mock.patch.object(rc, "resolve_delta_base", return_value=HEAD) as recover:
            self.parallel()
        self.assertEqual("4", recover.call_args.args[2])
        self.assertIn(f"receipt: {HEAD}", self.implementer.prompts["resolve"][0])
        self.assertIn(rc.PARALLEL_RESOLVE, self.implementer.prompts["resolve"][0])

    def test_changes_requested_starts_resolution_before_pending_ci_completes(self) -> None:
        self.stages(review=[("CHANGES_REQUESTED", {"findings": [FINDING]})],
                    rereview=[(AWAITING, {"verified_findings": [
                        {**FINDING, "status": "resolved"}]})])
        # The implementation's checks never complete; the fix's pass at once.
        self.github.set_checks(HEAD, PENDING)
        self.github.set_checks(FIX, PASSED)

        report = self.parallel()

        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual([("dispatch", "implement", gate.PENDING),
                          ("dispatch", "review", gate.PENDING),
                          # One read of the reviewed head, never a wait.
                          ("ci_read", HEAD, gate.PENDING),
                          ("dispatch", "resolve", gate.PENDING),
                          ("dispatch", "rereview", gate.SUCCESS),
                          ("ci_read", FIX, gate.SUCCESS)], self.events())
        self.assertEqual([], self.clock.slept)
        (snapshot, wait) = [row["payload"] for row in self.rows("ci")]
        self.assertEqual(("once", gate.CI_PENDING, HEAD),
                         (snapshot["ci_mode"], snapshot["ci_status"], snapshot["ci_head_sha"]))
        self.assertEqual(("wait", gate.CI_PASSED, FIX),
                         (wait["ci_mode"], wait["ci_status"], wait["ci_head_sha"]))

    def test_a_clean_review_awaits_ci_without_approval_and_without_another_review(self) -> None:
        self.stages()
        self.github.set_checks(HEAD, PENDING, PENDING, PASSED)
        closed_while_waiting = []
        self.clock.on_sleep = lambda now: closed_while_waiting.append(bool(self.rows("cycle")))

        report = self.parallel()

        # Pending: no approval, no closing row, no second review.
        self.assertEqual([False, False], closed_while_waiting)
        self.assertEqual(1, len(self.dispatched("review")))
        self.assertEqual([], self.dispatched("rereview"))
        # Then CI passes on the same SHA, and only then is the cycle approved.
        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual("approved", report.stop_reason)
        self.assertTrue(report.approved)
        self.assertEqual(AWAITING, report.verdict)
        (review,) = [row for row in self.rows("verdict") if row["role"] == "review"]
        self.assertEqual((AWAITING, HEAD),
                         (review["status"], review["payload"]["reviewed_head_sha"]))
        (wait,) = [row["payload"] for row in self.rows("ci")]
        self.assertEqual((gate.CI_PASSED, HEAD, 3, 60000),
                         (wait["ci_status"], wait["ci_head_sha"], wait["ci_polls"],
                          wait["ci_wait_ms"]))
        closing = self.closing()
        self.assertEqual("parallel", closing["ci_policy"])
        self.assertEqual(gate.CI_PASSED, closing["final_ci_status"])
        self.assertTrue(closing["first_pass_approved"])
        self.assertTrue(closing["final_approved"])
        self.assertEqual(0, closing["redundant_reviews"])
        self.assertEqual((3, 60000), (closing["ci_polls"], closing["ci_wait_ms"]))


class NeverApprovedTests(ParallelTestCase):
    """Failed, skipped, missing, stale, absent or unreadable checks never approve."""

    def assert_not_approved(self, report, status: str, stop_reason: str) -> None:
        self.assertNotEqual(rc.APPROVED_END, report.status)
        self.assertFalse(report.approved)
        self.assertEqual(status, report.status)
        self.assertEqual(stop_reason, report.stop_reason)
        self.assertNotEqual(True, self.closing().get("final_approved"))

    def test_checks_that_stay_pending_time_out_and_keep_the_clean_review(self) -> None:
        self.stages()

        report = self.parallel(ci_timeout=90)

        self.assert_not_approved(report, "BLOCKED", "ci_timeout")
        self.assertIn("stays valid", report.stopped_because)
        self.assertIn("--from rereview --pr 4", report.stopped_because)
        self.assertEqual(1, len(self.dispatched("review")))
        self.assertEqual(gate.CI_TIMEOUT, self.closing()["final_ci_status"])
        self.assertNotIn("first_pass_approved", self.closing())

    def test_a_missing_required_check_is_never_a_pass(self) -> None:
        self.stages()
        self.github.required_checks = (gate.RequiredCheck("build", 15368),
                                       gate.RequiredCheck("e2e"))
        self.github.set_checks(HEAD, PASSED)

        report = self.parallel(ci_timeout=60)

        self.assert_not_approved(report, "BLOCKED", "ci_timeout")
        self.assertIn("1 missing (e2e)", report.stopped_because)

    def test_skipped_or_absent_checks_stop_for_a_person(self) -> None:
        for checks in ((check(state=gate.SKIPPED),), ()):
            with self.subTest(checks=checks):
                self.setUp()
                self.stages()
                self.github.set_checks(HEAD, checks)

                report = self.parallel()

                self.assert_not_approved(report, rc.UNRESOLVED_END, "ci_not_passing")
                expected = gate.CI_SKIPPED if checks else gate.NO_CI_CHECKS_FOUND
                self.assertEqual(expected, self.closing()["final_ci_status"])

    def test_unreadable_checks_block(self) -> None:
        self.stages()
        original = self.github.checks

        def broken(repo, sha):
            raise gate.CheckReadError("gh api exited 1")

        self.github.checks = broken
        self.addCleanup(setattr, self.github, "checks", original)

        report = self.parallel()

        self.assert_not_approved(report, "BLOCKED", "ci_unreadable")
        self.assertIn("gh api exited 1", report.stopped_because)

    def test_a_failure_goes_to_resolution_and_never_approves_that_sha(self) -> None:
        self.stages(resolve=[("RESOLVED", {"push": FIX})],
                    rereview=[(AWAITING, {})])
        self.github.set_checks(HEAD, FAILED)
        self.github.set_checks(FIX, PASSED)

        report = self.parallel()

        self.assertEqual(rc.APPROVED_END, report.status)
        (resolve,) = self.implementer.prompts["resolve"]
        self.assertIn("Resolve the failed checks of the reviewed head.", resolve)
        self.assertIn(f"Required checks failed on head {HEAD}", resolve)
        self.assertNotIn("build", resolve.split("Required checks failed")[1].split(".")[0])
        waits = [(row["payload"]["ci_head_sha"], row["payload"]["ci_status"])
                 for row in self.rows("ci")]
        self.assertEqual([(HEAD, gate.CI_FAILED), (FIX, gate.CI_PASSED)], waits)
        closing = self.closing()
        self.assertFalse(closing["first_pass_approved"])
        self.assertTrue(closing["final_approved"])
        self.assertEqual(1, closing["iterations"])

    def test_a_resolution_that_leaves_the_failed_head_unchanged_stops(self) -> None:
        self.stages(resolve=[("RESOLVED", {})])
        self.github.set_checks(HEAD, FAILED)

        report = self.parallel()

        self.assertEqual("no_progress", report.stop_reason)
        self.assertEqual([], self.dispatched("rereview"))

    def test_failed_checks_with_no_budget_left_end_at_the_iteration_limit(self) -> None:
        self.stages()
        self.github.set_checks(HEAD, FAILED)

        report = self.parallel(max_iterations=0)

        self.assertEqual("iteration_limit", report.stop_reason)
        self.assertIn("failed after its clean review", report.stopped_because)
        self.assertEqual([], self.dispatched("resolve"))


class HeadMovedTests(ParallelTestCase):
    def test_a_moved_head_invalidates_the_clean_review_and_is_re_reviewed_once(self) -> None:
        self.stages()
        self.github.set_checks(HEAD, PENDING)
        self.github.set_checks(OTHER, PASSED)

        def push(now):
            self.github.head = OTHER

        self.clock.on_sleep = push

        report = self.parallel()

        self.assertEqual(rc.APPROVED_END, report.status)
        (rereview,) = self.reviewer.prompts["rereview"]
        self.assertIn(f"moved from {HEAD} to {OTHER}", rereview)
        waits = [(row["payload"]["ci_head_sha"], row["payload"]["ci_status"])
                 for row in self.rows("ci")]
        self.assertEqual([(HEAD, gate.CI_HEAD_MOVED), (OTHER, gate.CI_PASSED)], waits)
        self.assertEqual(1, report.iterations)
        # The finding history of the old head stays available: nothing was reset.
        self.assertEqual(2, len(self.github.items))

    def test_the_new_head_is_synchronized_and_recorded_before_rereview(self) -> None:
        self.stages()
        self.github.set_checks(HEAD, PENDING)
        self.github.set_checks(OTHER, PASSED)

        def push(now):
            self.github.head = OTHER

        self.clock.on_sleep = push
        with mock.patch.object(rc, "probe_change_request",
                               return_value=mock.Mock(head_sha=OTHER)), mock.patch.object(
                rc, "synchronize_worktree", return_value=(HEAD, OTHER)) as sync:
            report = self.parallel(cwd=str(ROOT))

        self.assertTrue(sync.called, report.explain())
        sync.assert_called_once_with("4", str(ROOT), target_head=OTHER)
        self.assertTrue(any(HEAD in line and OTHER in line for _, line in report.notes))
        sync_events = cj.events(self.store.path, "owner/api", task_id="API-7",
                                kinds=("stage_result",))
        self.assertTrue(any(event["correlation"] == {
            "old_head_sha": HEAD, "new_head_sha": OTHER,
        } for event in sync_events))
        self.assertEqual(rc.APPROVED_END, report.status)

        self.assertTrue(any(event["role"] == "rereview" and event["stage_seq"] == 3
                            for event in sync_events))

    def test_a_second_move_stops_for_a_person(self) -> None:
        self.stages()
        heads = iter([OTHER, LATER])

        def push(now):
            self.github.head = next(heads, self.github.head)

        self.clock.on_sleep = push

        report = self.parallel()

        self.assertEqual(rc.UNRESOLVED_END, report.status)
        self.assertEqual("head_moved", report.stop_reason)
        self.assertIn("moved again", report.stopped_because)


class ConcurrentFailureTests(ParallelTestCase):
    def test_review_findings_and_a_ci_failure_both_reach_the_resolution(self) -> None:
        self.stages(review=[("CHANGES_REQUESTED", {"findings": [FINDING]})],
                    rereview=[(AWAITING, {"verified_findings": [
                        {**FINDING, "status": "resolved"}]})])
        self.github.set_checks(HEAD, FAILED)
        self.github.set_checks(FIX, PASSED)

        report = self.parallel()

        self.assertEqual(rc.APPROVED_END, report.status)
        (resolve,) = self.implementer.prompts["resolve"]
        self.assertIn("Resolve the findings from the review.", resolve)
        self.assertIn(f"Required checks failed on head {HEAD}", resolve)
        self.assertIn("neither resolves, replaces nor hides the other", resolve)
        # Separately visible: the review's verdict and the CI read, each with its SHA.
        (review,) = [row for row in self.rows("verdict") if row["role"] == "review"]
        self.assertEqual(("CHANGES_REQUESTED", 1, HEAD),
                         (review["status"], review["findings_blocking"],
                          review["payload"]["reviewed_head_sha"]))
        snapshot = self.rows("ci")[0]["payload"]
        self.assertEqual((gate.CI_FAILED, "once", HEAD, 1),
                         (snapshot["ci_status"], snapshot["ci_mode"],
                          snapshot["ci_head_sha"], snapshot["checks_failed"]))

    def test_several_resolution_rounds_end_on_one_ci_wait(self) -> None:
        second = {**FINDING, "id": "REV-002"}
        self.stages(review=[("CHANGES_REQUESTED", {"findings": [FINDING]})],
                    resolve=[("RESOLVED", {"push": FIX}), ("RESOLVED", {"push": OTHER})],
                    rereview=[("CHANGES_REQUESTED", {"verified_findings": [
                                  {**FINDING, "status": "resolved"}],
                                  "new_findings": [second]}),
                              (AWAITING, {"verified_findings": [
                                  {**FINDING, "status": "resolved"},
                                  {**second, "status": "resolved"}]})])
        self.github.set_checks(OTHER, PENDING, PASSED)

        report = self.parallel()

        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual(2, report.iterations)
        modes = [(row["payload"]["ci_mode"], row["payload"]["ci_head_sha"])
                 for row in self.rows("ci")]
        self.assertEqual([("once", HEAD), ("once", FIX), ("wait", OTHER)], modes)


class ReceiptTests(ParallelTestCase):
    """The PR comment receipt is the record of a clean review awaiting CI."""

    def test_a_clean_review_without_its_receipt_is_never_waited_on(self) -> None:
        cases = (
            ({"publish": False}, "no trusted comment carries its receipt"),
            ({"author": "someone-else"}, "no trusted comment carries its receipt"),
        )
        for fields, fragment in cases:
            with self.subTest(fields=fields):
                self.setUp()
                self.stages(review=[(AWAITING, fields)])
                self.github.set_checks(HEAD, PASSED)

                report = self.parallel()

                self.assertEqual("BLOCKED", report.status)
                self.assertIn(fragment, report.stopped_because)
                self.assertEqual([], [entry for entry in self.github.log
                                      if entry[0] == "ci_read"])

    def test_a_receipt_for_another_head_or_status_does_not_vouch(self) -> None:
        self.stages(review=[(AWAITING, {})])
        self.github.set_checks(HEAD, PASSED)
        original = self.github.publish

        def misreport(body, author="reviewer"):
            original(body.replace(f'"head_sha": "{HEAD}"', f'"head_sha": "{OTHER}"'), author)

        self.github.publish = misreport

        report = self.parallel()

        self.assertEqual("BLOCKED", report.status)
        self.assertIn("names another stage, head or status", report.stopped_because)

    def test_a_publication_during_the_wait_blocks_the_approval(self) -> None:
        self.stages()
        self.github.set_checks(HEAD, PENDING, PASSED)

        def interfere(now):
            self.github.publish(receipt("review", "attempt-elsewhere", "APPROVED", HEAD))

        self.clock.on_sleep = interfere

        report = self.parallel()

        self.assertEqual("BLOCKED", report.status)
        self.assertIn("receipt no longer holds", report.stopped_because)
        self.assertIn("another stage published after it", report.stopped_because)

    def test_an_invalid_clean_result_is_a_stage_that_did_not_complete(self) -> None:
        cases = (
            ({"reviewed_head_sha": HEAD[:12]}, "no full reviewed_head_sha"),
            ({"reviewed_head_sha": OTHER}, "is not its head_sha"),
            ({"findings": [FINDING], "blocking_findings": []}, "an open finding blocks approval"),
            ({"blocking_findings": ["REV-001"]}, "lists blocking findings"),
            ({"blocking_findings": None}, "no blocking_findings list"),
            ({"findings": [{"id": "REV-001", "status": "weird"}]}, "cannot be read"),
        )
        for fields, fragment in cases:
            with self.subTest(fields=fields):
                self.setUp()
                self.stages(review=[(AWAITING, fields)])

                report = self.parallel()

                self.assertEqual("stage_not_completed", report.stop_reason)
                self.assertIn(fragment, report.stopped_because)
                self.assertEqual([], [entry for entry in self.github.log
                                      if entry[0] == "ci_read"])


class StrictPolicyTests(ParallelTestCase):
    """`strict`, the default, is the original serial flow."""

    def test_strict_never_reads_checks_or_tells_a_stage_not_to_wait(self) -> None:
        self.stages(review=[("APPROVED", {})])

        report = self.parallel(ci_policy="strict")

        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual([], [entry for entry in self.github.log if entry[0] == "ci_read"])
        prompts = [*self.implementer.prompts["implement"], *self.reviewer.prompts["review"]]
        self.assertTrue(all("CI policy" not in prompt for prompt in prompts))
        self.assertEqual([], self.rows("ci"))
        self.assertEqual("strict", self.closing()["ci_policy"])

    def test_strict_refuses_an_awaiting_review(self) -> None:
        self.stages()

        report = self.parallel(ci_policy="strict")

        self.assertEqual("stage_not_completed", report.stop_reason)
        self.assertIn("only the parallel CI policy accepts", report.stopped_because)

    def test_only_parallel_resolution_gets_the_non_waiting_delta_contract(self) -> None:
        prompts = {}
        for policy in rc.CI_POLICIES:
            prompts[policy] = rc.compose("resolve", "owner/api", "API-7",
                                         "Resolve the findings from the review.",
                                         change_request_id="4", ci_policy=policy)
        self.assertNotIn("CI policy", prompts["strict"])
        self.assertIn(rc.PARALLEL_RESOLVE, prompts["parallel"])
        self.assertIn("review the accumulated diff", prompts["parallel"])
        self.assertNotIn(rc.PARALLEL_RESOLVE, rc.compose(
            "resolve", "owner/api", "API-7", ci_policy="parallel", local_only=True))
        self.assertNotIn("CI policy", rc.compose("implement", "owner/api", "API-7",
                                                 ci_policy="parallel", local_only=True))

    def test_parallel_needs_a_github_code_host(self) -> None:
        self.stages()
        with self.assertRaises(rc.CycleDriverError):
            self.parallel(code_host="bitbucket")
        with self.assertRaises(rc.CycleDriverError):
            rc.check_ci_policy("eager", "github")
        rc.check_ci_policy("strict", "bitbucket")


class ResolveDeltaTests(unittest.TestCase):
    def test_delta_boundary_uses_real_git_ancestry_and_rejects_rewritten_history(self):
        repo = git_repo(self)
        reviewed = git(repo, "rev-parse", "HEAD")
        git(repo, "commit", "--allow-empty", "-qm", "fix")
        comments = [self.comment(head=reviewed)]
        self.assertEqual(reviewed, rc.resolve_delta_base(
            comments, "owner/api", "4", ("reviewer",), str(repo)))
        git(repo, "checkout", "--orphan", "rewritten")
        git(repo, "commit", "-qm", "different history")
        self.assertIsNone(rc.resolve_delta_base(
            comments, "owner/api", "4", ("reviewer",), str(repo)))

    def comment(self, stage="review", head=HEAD, author="reviewer", **kw):
        return contract.ProviderComment(
            body=receipt(stage, "attempt-test", "CHANGES_REQUESTED", head, **kw),
            author=author)

    def base(self, comments):
        return rc.resolve_delta_base(comments, "owner/api", "4", ("reviewer",), None)

    def test_latest_trusted_review_ignores_resolution_and_other_scopes(self):
        comments = [self.comment(), self.comment(stage="rereview", head=FIX),
                    self.comment(stage="resolve", head=OTHER),
                    self.comment(head=OTHER, author="outsider"),
                    self.comment(head=OTHER, number="9")]
        with mock.patch.object(rc.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(FIX, self.base(comments))
            self.assertEqual(["git", "merge-base", "--is-ancestor", FIX, "HEAD"],
                             run.call_args.args[0])
        prompt = rc.compose("resolve", "owner/api", "7", ci_policy="parallel",
                            resolve_review_base=FIX)
        self.assertIn(f"receipt: {FIX}", prompt)
        self.assertIn("ancestor of the new head", prompt)

    def test_missing_malformed_and_untrusted_evidence_widens_review(self):
        for comments in ([], [self.comment(author="outsider")],
                         [contract.ProviderComment(body='<!-- code-cycle-stage {bad} -->',
                                                   author="reviewer")]):
            with self.subTest(comments=comments):
                self.assertIsNone(self.base(comments))

    def test_non_ancestor_missing_commit_and_git_failure_widen_review(self):
        for code in (1, 128):
            with self.subTest(code=code), mock.patch.object(rc.subprocess, "run") as run:
                run.return_value.returncode = code
                self.assertIsNone(self.base([self.comment()]))
        with mock.patch.object(rc.subprocess, "run", side_effect=OSError("git unavailable")):
            self.assertIsNone(self.base([self.comment()]))

    def test_blocked_latest_review_does_not_reuse_an_older_boundary(self):
        blocked = contract.ProviderComment(
            body=receipt("rereview", "attempt-blocked", "BLOCKED", FIX), author="reviewer")
        self.assertIsNone(self.base([self.comment(), blocked]))


class ResumeTests(ParallelTestCase):
    """A resume while awaiting CI reuses the clean review its receipt records."""

    def clean_history(self, head: str = HEAD, status: str = AWAITING) -> None:
        self.github.publish(published("review", status, head, "attempt-earlier", 1))

    def resume(self, **kw):
        kw.setdefault("start_from", "rereview")
        kw.setdefault("change_request_id", "4")
        return self.parallel(**kw)

    def test_the_clean_review_of_the_current_head_is_reused_without_a_review(self) -> None:
        self.stages()
        self.clean_history()
        self.github.set_checks(HEAD, PENDING, PASSED)

        report = self.resume()

        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual([], self.dispatched())
        self.assertIn("reused the clean review", report.explain())
        closing = self.closing()
        self.assertTrue(closing["review_reused"])
        self.assertEqual(gate.CI_PASSED, closing["final_ci_status"])

    def test_a_timeout_can_be_resumed_again(self) -> None:
        self.stages()
        self.clean_history()

        first = self.resume(ci_timeout=60)
        self.assertEqual("ci_timeout", first.stop_reason)
        self.github.set_checks(HEAD, PASSED)
        second = self.resume()

        self.assertEqual(rc.APPROVED_END, second.status)
        self.assertEqual([], self.dispatched())

    def test_anything_but_a_clean_review_of_the_current_head_is_reviewed(self) -> None:
        cases = {
            "head moved": lambda: (self.clean_history(), setattr(self.github, "head", FIX)),
            "later resolution": lambda: (self.clean_history(), self.github.publish(
                published("resolve", "RESOLVED", HEAD, "attempt-fix", 2))),
            "changes requested": lambda: self.clean_history(status="CHANGES_REQUESTED"),
            "malformed receipt": lambda: (self.clean_history(), self.github.publish(
                "<!-- code-cycle-stage {not json} -->")),
            "blocking finding": lambda: self.github.publish(published(
                "review", AWAITING, HEAD, "attempt-earlier", 1, findings=[FINDING])),
            "no receipt": lambda: None,
        }
        for name, prepare in cases.items():
            with self.subTest(case=name):
                self.setUp()
                self.stages(rereview=[(AWAITING, {})])
                prepare()
                self.github.set_checks(self.github.head, PASSED)

                report = self.resume()

                self.assertEqual(1, len(self.dispatched("rereview")))
                self.assertEqual(rc.APPROVED_END, report.status)
                self.assertFalse(self.closing()["review_reused"])

    def test_an_unreadable_awaiting_state_stops_with_its_reason(self) -> None:
        self.stages()
        self.clean_history()
        original = self.github.pull

        def broken(repo, number):
            raise gate.CheckReadError("gh api exited 1")

        self.github.pull = broken
        self.addCleanup(setattr, self.github, "pull", original)

        report = self.resume()

        self.assertEqual("BLOCKED", report.status)
        self.assertEqual("ci_unreadable", report.stop_reason)
        self.assertIn("gh api exited 1", report.stopped_because)
        self.assertEqual([], self.dispatched())

    def test_a_closed_change_request_is_not_resumed(self) -> None:
        self.stages()
        self.clean_history()
        self.github.state = "CLOSED"

        report = self.resume()

        self.assertEqual("BLOCKED", report.status)
        self.assertIn("is closed, not open", report.stopped_because)
        self.assertEqual([], self.dispatched())

    def test_an_explicit_initial_review_is_always_dispatched(self) -> None:
        self.stages()
        self.clean_history()
        self.github.set_checks(HEAD, PASSED)

        self.resume(start_from="review")

        self.assertEqual(1, len(self.dispatched("review")))


class PollingOwnershipTests(ParallelTestCase):
    """One deterministic wait owns the checks; no stage and no model polls."""

    def test_no_stage_runs_while_the_gate_waits_and_it_waits_once(self) -> None:
        self.stages()
        self.github.set_checks(HEAD, PENDING, PENDING, PENDING, PASSED)
        dispatches_during_wait = []
        self.clock.on_sleep = lambda now: dispatches_during_wait.append(
            len(self.dispatched()))

        self.parallel()

        self.assertEqual([2, 2, 2], dispatches_during_wait)
        self.assertEqual([30, 30, 30], self.clock.slept)
        self.assertEqual(1, len([row for row in self.rows("ci")
                                 if row["payload"]["ci_mode"] == "wait"]))
        for prompt in (*self.implementer.prompts["implement"],
                       *self.reviewer.prompts["review"]):
            self.assertIn("Do not wait for", prompt)

    def test_the_wait_is_shown_in_the_status_and_the_report(self) -> None:
        self.stages()
        self.github.set_checks(HEAD, PENDING, PASSED)
        states = []

        def observe(now):
            status = next((self.store.path.parent / "status").glob("*.json"))
            states.append(json.loads(status.read_text()).get("ci", {}).get("state"))

        self.clock.on_sleep = observe

        report = self.parallel()

        self.assertEqual(["waiting"], states)
        self.assertIn(f"ci       {gate.CI_PASSED} on {HEAD[:12]} after 2 reads", report.explain())


class BenchmarkTests(ParallelTestCase):
    """A repeatable measurement of what `parallel` removes, against `strict`.

    The fake CI takes `CI` seconds, a review takes `REVIEW`; a strict stage
    honours its contract and waits for its own head's checks. Simulated time,
    so the comparison is deterministic.
    """

    CI, REVIEW = 240, 180

    def measure(self, policy: str) -> dict:
        self.setUp()
        self.github.finish_at(HEAD, self.CI)
        implementer = Stage("codex", self.github, self.clock, wait_for_ci=policy == "strict")
        reviewer = Stage("claude", self.github, self.clock, duration=self.REVIEW,
                         review=[("APPROVED" if policy == "strict" else AWAITING, {})])
        report = self.parallel(implementer=implementer, reviewer=reviewer, ci_policy=policy)
        self.assertEqual(rc.APPROVED_END, report.status)
        dispatch = {entry[1]: entry[3] for entry in self.github.log if entry[0] == "dispatch"}
        in_stage_polls = sum(1 for entry in self.github.log if entry[0] == "ci_read") - sum(
            row["payload"]["ci_polls"] for row in self.rows("ci"))
        return {
            "implementation_to_review": dispatch["review"] - dispatch["implement"],
            "idle_ci_wait": self.closing().get("ci_wait_ms", 0) / 1000,
            "model_polls": in_stage_polls,
            "redundant_reviews": self.closing()["redundant_reviews"],
            "wall": self.clock.now,
        }

    def test_parallel_removes_the_ci_wait_before_review_and_the_model_polling(self) -> None:
        strict, parallel = self.measure("strict"), self.measure("parallel")

        self.assertEqual(self.CI, strict["implementation_to_review"])
        self.assertEqual(0, parallel["implementation_to_review"])
        self.assertEqual(self.CI // 30, strict["model_polls"])
        self.assertEqual(0, parallel["model_polls"])
        # The review already used most of the CI time; only the rest is waited.
        self.assertEqual(self.CI - self.REVIEW, parallel["idle_ci_wait"])
        self.assertEqual((0, 0), (strict["redundant_reviews"], parallel["redundant_reviews"]))
        self.assertLess(parallel["wall"], strict["wall"])


class ConfigurationTests(unittest.TestCase):
    def write(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / ".code-cycle.yml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_policy_and_timeout_are_read_and_the_command_line_wins(self) -> None:
        config = rc.load_config(self.write(
            "code_cycle:\n  ci:\n    policy: parallel\n    timeout: 120\n"))

        self.assertEqual("parallel", rc.ci_policy_of(config))
        self.assertEqual("strict", rc.ci_policy_of(config, "strict"))
        self.assertEqual(120, rc.ci_timeout_of(config))
        self.assertEqual(30, rc.ci_timeout_of(config, 30))
        self.assertEqual(("strict", gate.DEFAULT_TIMEOUT),
                         (rc.ci_policy_of({}), rc.ci_timeout_of({})))

    def test_a_ci_block_nobody_declared_is_refused_by_name(self) -> None:
        for text, fragment in (
                ("code_cycle:\n  ci:\n    polcy: parallel\n", "unknown key under code_cycle.ci"),
                ("code_cycle:\n  ci:\n    policy: eager\n", "code_cycle.ci.policy must be one of"),
                ("code_cycle:\n  ci:\n    timeout: 0\n", "code_cycle.ci.timeout"),
                ("code_cycle:\n  ci:\n    timeout: soon\n", "code_cycle.ci.timeout"),
                ("code_cycle:\n  ci: parallel\n", "code_cycle.ci must be a mapping")):
            with self.subTest(text=text):
                with self.assertRaises(rc.CycleDriverError) as raised:
                    rc.load_config(self.write(text))
                self.assertIn(fragment, str(raised.exception))

    def test_main_passes_the_effective_policy_to_the_cycle(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / ".code-cycle.yml").write_text(
            "code_cycle:\n  code_host: github\n  ci:\n    policy: parallel\n"
            "    timeout: 600\n", encoding="utf-8")
        cases = (([], ("parallel", 600)),
                 (["--ci-policy", "strict", "--ci-timeout", "60"], ("strict", 60)))
        for extra, expected in cases:
            with self.subTest(extra=extra):
                report = rc.CycleReport("owner/api", "API-7", status=rc.APPROVED_END)
                with mock.patch.object(rc, "run_cycle", return_value=report) as run, \
                        mock.patch.object(rc, "select_workspace",
                                          return_value=(str(root), None)), \
                        mock.patch.object(rc, "save_decisions"), \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = rc.main(["--task", "API-7", "--repo", "owner/api", "--cwd",
                                    str(root), "--database", str(root / "t.sqlite"),
                                    "--workspace", "current", *extra])
                self.assertEqual(0, code)
                kwargs = run.call_args.kwargs
                self.assertEqual(expected, (kwargs["ci_policy"], kwargs["ci_timeout"]))

    def test_main_refuses_parallel_on_another_code_host(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / ".code-cycle.yml").write_text("code_cycle:\n  code_host: bitbucket\n",
                                              encoding="utf-8")
        with mock.patch.object(rc, "run_cycle") as run, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                rc.main(["--task", "API-7", "--repo", "owner/api", "--cwd", str(root),
                         "--ci-policy", "parallel"])
        run.assert_not_called()
        self.assertIn("parallel CI policy reads GitHub checks", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
