"""#228: the one deterministic wait on a change request's checks."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ci_gate as gate  # noqa: E402

SHA = "a" * 40
OTHER = "b" * 40


def run(name: str, state: str = gate.SUCCESS, *, source: str = "github-actions",
        order: int = 1, app_id: int | None = 15368) -> gate.Check:
    return gate.Check(name, source, state, order, app_id)


class Clock:
    """Time that moves only when the gate sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeCI:
    """A controlled code host: each read of the checks returns the next script entry."""

    def __init__(self, reads, *, required=(), head=SHA, state="OPEN", heads=None) -> None:
        self.reads = list(reads)
        self.required_checks = tuple(required)
        self.head = head
        self.state = state
        self.heads = list(heads or [])
        self.pulls = 0
        self.check_reads = 0
        self.failure: dict[str, str] = {}

    def pull(self, repo, change_request_id):
        self.pulls += 1
        if "pull" in self.failure:
            raise gate.CheckReadError(self.failure["pull"])
        if self.heads:
            self.head = self.heads.pop(0)
        return gate.PullState(self.state, self.head, "main")

    def required(self, repo, base_ref):
        if "required" in self.failure:
            raise gate.CheckReadError(self.failure["required"])
        return self.required_checks

    def checks(self, repo, sha):
        self.check_reads += 1
        if "checks" in self.failure:
            raise gate.CheckReadError(self.failure["checks"])
        if len(self.reads) > 1:
            return self.reads.pop(0)
        return self.reads[0]


def wait(ci: FakeCI, clock: Clock | None = None, **kw) -> gate.GateResult:
    clock = clock or Clock()
    kw.setdefault("timeout", 300)
    kw.setdefault("poll_interval", 30)
    return gate.wait_for_checks(ci, "owner/api", "4", SHA, clock=clock, sleep=clock.sleep, **kw)


REQUIRED = (gate.RequiredCheck("build", 15368), gate.RequiredCheck("lint"))


class BarrierTests(unittest.TestCase):
    def test_every_declared_check_passing_passes_after_one_read(self) -> None:
        ci = FakeCI([(run("build"), run("lint", source="status", app_id=None))],
                    required=REQUIRED)

        result = wait(ci)

        self.assertEqual(gate.CI_PASSED, result.status)
        self.assertEqual("declared", result.required_source)
        self.assertEqual(1, result.polls)
        self.assertEqual(("build (github-actions)", "lint (status)"), result.passed)
        # The final barrier reads the head once more after the pass.
        self.assertEqual(2, ci.pulls)

    def test_pending_checks_are_waited_for_and_the_wait_is_measured(self) -> None:
        clock = Clock()
        pending = (run("build", gate.PENDING), run("lint", gate.PENDING))
        ci = FakeCI([pending, pending, (run("build"), run("lint"))], required=REQUIRED)
        polls = []

        result = wait(ci, clock, on_poll=polls.append)

        self.assertEqual(gate.CI_PASSED, result.status)
        self.assertEqual(3, result.polls)
        self.assertEqual([30, 30], clock.slept)
        self.assertEqual(60, result.waited_seconds)
        self.assertEqual([gate.CI_PENDING, gate.CI_PENDING, gate.CI_PASSED],
                         [item.status for item in polls])

    def test_a_failure_ends_the_wait_even_while_others_are_pending(self) -> None:
        for conclusion in ("failure", "cancelled", "timed_out", "action_required",
                           "startup_failure", "stale"):
            with self.subTest(conclusion=conclusion):
                ci = FakeCI([(run("build", gate.FAILED), run("lint", gate.PENDING))],
                            required=REQUIRED)

                result = wait(ci)

                self.assertEqual(gate.CI_FAILED, result.status)
                self.assertEqual(("build (github-actions)",), result.failed)
                self.assertEqual(("lint (github-actions)",), result.pending)
                self.assertEqual(1, result.polls)

    def test_skipped_and_neutral_never_pass(self) -> None:
        ci = FakeCI([(run("build", gate.SKIPPED), run("lint"))], required=REQUIRED)

        result = wait(ci)

        self.assertEqual(gate.CI_SKIPPED, result.status)
        self.assertFalse(result.passed_gate)

    def test_a_missing_declared_check_is_pending_until_the_deadline(self) -> None:
        clock = Clock()
        ci = FakeCI([(run("build"),)], required=REQUIRED)

        result = wait(ci, clock, timeout=90)

        self.assertEqual(gate.CI_TIMEOUT, result.status)
        self.assertEqual(("lint",), result.missing)
        self.assertEqual(4, result.polls)
        self.assertEqual(90, result.waited_seconds)

    def test_pending_checks_at_the_deadline_time_out_and_never_pass(self) -> None:
        ci = FakeCI([(run("build", gate.PENDING), run("lint"))], required=REQUIRED)

        result = wait(ci, timeout=45)

        self.assertEqual(gate.CI_TIMEOUT, result.status)
        self.assertEqual(("build (github-actions)",), result.pending)
        self.assertFalse(result.passed_gate)

    def test_without_declared_checks_every_reported_check_is_required(self) -> None:
        ci = FakeCI([(run("build"), run("docs", gate.FAILED, source="other-app", app_id=7))])

        result = wait(ci)

        self.assertEqual(gate.CI_FAILED, result.status)
        self.assertEqual("reported", result.required_source)
        self.assertEqual(("docs (other-app)",), result.failed)

    def test_no_check_at_all_is_reported_after_the_grace_only(self) -> None:
        clock = Clock()
        ci = FakeCI([()])

        result = wait(ci, clock, no_checks_grace=60)

        self.assertEqual(gate.NO_CI_CHECKS_FOUND, result.status)
        self.assertEqual(60, result.waited_seconds)
        self.assertEqual(3, result.polls)

    def test_checks_that_appear_while_waiting_are_required_and_reported_late(self) -> None:
        ci = FakeCI([(run("build", gate.PENDING),),
                     (run("build"), run("e2e", gate.PENDING)),
                     (run("build"), run("e2e"))])

        result = wait(ci)

        self.assertEqual(gate.CI_PASSED, result.status)
        self.assertEqual(("e2e (github-actions)",), result.late)
        self.assertIn("e2e (github-actions)", result.passed)

    def test_the_check_set_is_fixed_when_the_barrier_is_evaluated(self) -> None:
        # The first read already passes: the barrier is decided on what was
        # reported then, and a check that appears afterwards is not waited for.
        ci = FakeCI([(run("build"),), (run("build"), run("e2e", gate.FAILED))])
        self.assertEqual(gate.CI_PASSED, wait(ci).status)

        # A check that appears while the barrier is still open is part of it.
        ci = FakeCI([(run("build", gate.PENDING),), (run("build"), run("e2e", gate.FAILED))])
        result = wait(ci)
        self.assertEqual(gate.CI_FAILED, result.status)
        self.assertEqual(("e2e (github-actions)",), result.late)

    def test_repeated_runs_count_once_as_their_latest(self) -> None:
        retried = (run("build", gate.FAILED, order=1), run("build", order=2), run("lint"))
        regressed = (run("build", order=1), run("build", gate.FAILED, order=2), run("lint"))

        self.assertEqual(gate.CI_PASSED, wait(FakeCI([retried], required=REQUIRED)).status)
        self.assertEqual(gate.CI_FAILED, wait(FakeCI([regressed], required=REQUIRED)).status)

    def test_a_declared_app_is_matched_by_its_id(self) -> None:
        impostor = (run("build", source="other-app", app_id=99), run("lint"))

        result = wait(FakeCI([impostor], required=REQUIRED), timeout=30)

        self.assertEqual(gate.CI_TIMEOUT, result.status)
        self.assertEqual(("build (app 15368)",), result.missing)

    def test_a_commit_status_satisfies_only_a_requirement_open_to_any_source(self) -> None:
        status = run("build", source="status", app_id=None)

        self.assertEqual(gate.CI_PASSED, wait(FakeCI(
            [(status,)], required=(gate.RequiredCheck("build"),))).status)
        self.assertEqual(gate.CI_TIMEOUT, wait(FakeCI(
            [(status,)], required=(gate.RequiredCheck("build", 15368),)), timeout=30).status)


class HeadAndReadTests(unittest.TestCase):
    def test_a_head_that_already_moved_is_reported_without_reading_checks(self) -> None:
        ci = FakeCI([(run("build"),)], head=OTHER)

        result = wait(ci)

        self.assertEqual(gate.CI_HEAD_MOVED, result.status)
        self.assertEqual(OTHER, result.pr_head_sha)
        self.assertEqual(0, ci.check_reads)

    def test_a_head_that_moves_while_waiting_ends_the_wait(self) -> None:
        ci = FakeCI([(run("build", gate.PENDING),)], heads=[SHA, OTHER])

        result = wait(ci)

        self.assertEqual(gate.CI_HEAD_MOVED, result.status)
        self.assertEqual(1, result.polls)

    def test_a_head_that_moves_at_the_final_barrier_is_not_a_pass(self) -> None:
        ci = FakeCI([(run("build"),)], heads=[SHA, OTHER])

        result = wait(ci)

        self.assertEqual(gate.CI_HEAD_MOVED, result.status)

    def test_a_closed_change_request_is_unreadable_never_passed(self) -> None:
        for state in ("CLOSED", "MERGED"):
            with self.subTest(state=state):
                result = wait(FakeCI([(run("build"),)], state=state))

                self.assertEqual(gate.CI_UNREADABLE, result.status)
                self.assertIn("not open", result.reason)

    def test_every_read_failure_is_unreadable(self) -> None:
        for where in ("pull", "required", "checks"):
            with self.subTest(where=where):
                ci = FakeCI([(run("build"),)])
                ci.failure[where] = "gh exited 1"

                result = wait(ci)

                self.assertEqual(gate.CI_UNREADABLE, result.status)
                self.assertIn("gh exited 1", result.reason)

    def test_a_single_read_never_waits(self) -> None:
        clock = Clock()
        cases = (((run("build", gate.PENDING),), gate.CI_PENDING),
                 ((), gate.CI_PENDING),
                 ((run("build", gate.FAILED),), gate.CI_FAILED),
                 ((run("build"),), gate.CI_PASSED))
        for checks, expected in cases:
            with self.subTest(expected=expected, checks=len(checks)):
                result = wait(FakeCI([checks]), clock, once=True)

                self.assertEqual(expected, result.status)
                self.assertEqual(1, result.polls)
        self.assertEqual([], clock.slept)

    def test_only_a_full_sha_is_read(self) -> None:
        for sha in ("a" * 39, "A" * 40, None, "main"):
            with self.subTest(sha=sha):
                with self.assertRaises(ValueError):
                    gate.wait_for_checks(FakeCI([()]), "owner/api", "4", sha)

    def test_the_result_serialises_its_counts_and_lists(self) -> None:
        result = wait(FakeCI([(run("build", gate.FAILED), run("lint", gate.PENDING))],
                             required=REQUIRED))

        data = result.to_json()

        self.assertEqual({"passed": 0, "failed": 1, "skipped": 0, "pending": 1,
                          "missing": 0, "late": 0}, data["counts"])
        self.assertEqual(gate.CI_FAILED, data["status"])
        self.assertEqual(SHA, data["head_sha"])


def completed(stdout) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(stdout), stderr="")


class GitHubChecksTests(unittest.TestCase):
    def responder(self, responses: dict):
        calls = []

        def respond(command, **kw):
            calls.append(command)
            path = command[-1]
            for prefix, value in responses.items():
                if path.startswith(prefix):
                    return value if isinstance(value, subprocess.CompletedProcess) \
                        else completed(value)
            raise AssertionError(f"unexpected call {command}")

        return respond, calls

    def test_the_pull_request_state_head_and_base_are_read(self) -> None:
        respond, calls = self.responder({"repos/owner/api/pulls/4": {
            "state": "open", "merged": False, "head": {"sha": SHA}, "base": {"ref": "main"}}})

        pull = gate.GitHubChecks(run=respond).pull("owner/api", "4")

        self.assertEqual(gate.PullState("OPEN", SHA, "main"), pull)
        self.assertEqual(["gh", "api", "repos/owner/api/pulls/4"], calls[0])

    def test_required_checks_merge_classic_protection_and_rulesets(self) -> None:
        respond, calls = self.responder({
            "repos/owner/api/branches/release%2F1": {"protection": {"required_status_checks": {
                "enforcement_level": "non_admins",
                "checks": [{"context": "build", "app_id": 15368},
                           {"context": "lint", "app_id": -1}],
                "contexts": ["build", "lint"]}}},
            "repos/owner/api/rules/branches/release%2F1": [[
                {"type": "deletion"},
                {"type": "required_status_checks", "parameters": {"required_status_checks": [
                    {"context": "build", "integration_id": 15368},
                    {"context": "e2e"}]}}]],
        })

        required = gate.GitHubChecks(run=respond).required("owner/api", "release/1")

        self.assertEqual((gate.RequiredCheck("build", 15368), gate.RequiredCheck("lint"),
                          gate.RequiredCheck("e2e")), required)
        self.assertEqual(["gh", "api", "--paginate", "--slurp",
                          "repos/owner/api/rules/branches/release%2F1"], calls[1])

    def test_unenforced_classic_protection_declares_nothing(self) -> None:
        respond, _ = self.responder({
            "repos/owner/api/branches/main": {"protection": {"enabled": False,
                "required_status_checks": {"enforcement_level": "off",
                                           "contexts": ["build"], "checks": []}}},
            "repos/owner/api/rules/branches/main": [[]],
        })

        self.assertEqual((), gate.GitHubChecks(run=respond).required("owner/api", "main"))

    def test_legacy_contexts_are_read_when_no_checks_are_listed(self) -> None:
        respond, _ = self.responder({
            "repos/owner/api/branches/main": {"protection": {"required_status_checks": {
                "enforcement_level": "everyone", "contexts": ["ci/legacy"]}}},
            "repos/owner/api/rules/branches/main": [[]],
        })

        self.assertEqual((gate.RequiredCheck("ci/legacy"),),
                         gate.GitHubChecks(run=respond).required("owner/api", "main"))

    def test_check_runs_and_statuses_are_normalised(self) -> None:
        respond, _ = self.responder({
            f"repos/owner/api/commits/{SHA}/check-runs": [
                {"total_count": 3, "check_runs": [
                    {"id": 5, "name": "build", "status": "completed", "conclusion": "failure",
                     "app": {"id": 15368, "slug": "github-actions"}},
                    {"id": 9, "name": "build", "status": "completed", "conclusion": "success",
                     "app": {"id": 15368, "slug": "github-actions"}}]},
                {"total_count": 3, "check_runs": [
                    {"id": 7, "name": "e2e", "status": "in_progress", "conclusion": None,
                     "app": {"id": 15368, "slug": "github-actions"}},
                    {"id": 8, "name": "docs", "status": "completed", "conclusion": "neutral",
                     "app": {"id": 3, "slug": "docs-bot"}},
                    {"id": 4, "name": "odd", "status": "something_new", "conclusion": None,
                     "app": {}}]}],
            f"repos/owner/api/commits/{SHA}/status": [
                {"state": "pending", "statuses": [
                    {"id": 1, "context": "legacy", "state": "error"},
                    {"id": 2, "context": "deploy", "state": "pending"},
                    {"id": 3, "context": "cov", "state": "success"}]}],
        })

        checks = gate.GitHubChecks(run=respond).checks("owner/api", SHA)

        states = {(check.name, check.source, check.order): check.state for check in checks}
        self.assertEqual({
            ("build", "github-actions", 5): gate.FAILED,
            ("build", "github-actions", 9): gate.SUCCESS,
            ("e2e", "github-actions", 7): gate.PENDING,
            ("docs", "docs-bot", 8): gate.SKIPPED,
            ("odd", "unknown", 4): gate.PENDING,
            ("legacy", "status", 1): gate.FAILED,
            ("deploy", "status", 2): gate.PENDING,
            ("cov", "status", 3): gate.SUCCESS,
        }, states)
        latest = {check.key: check.state for check in gate.latest_runs(checks)}
        self.assertEqual(gate.SUCCESS, latest[("build", "github-actions")])

    def test_an_empty_combined_status_is_not_a_check(self) -> None:
        respond, _ = self.responder({
            f"repos/owner/api/commits/{SHA}/check-runs": [{"total_count": 0, "check_runs": []}],
            f"repos/owner/api/commits/{SHA}/status": [
                {"state": "pending", "statuses": [], "total_count": 0}],
        })

        self.assertEqual((), gate.GitHubChecks(run=respond).checks("owner/api", SHA))

    def test_every_tooling_failure_is_a_read_error(self) -> None:
        failures = (
            subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 404"),
            subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
            completed({"unexpected": True}),
        )
        for failure in failures:
            with self.subTest(stdout=failure.stdout):
                provider = gate.GitHubChecks(run=lambda *a, **k: failure)
                with self.assertRaises(gate.CheckReadError):
                    provider.checks("owner/api", SHA)
        broken = gate.GitHubChecks(run=mock.Mock(side_effect=OSError("no gh")))
        with self.assertRaises(gate.CheckReadError):
            broken.pull("owner/api", "4")
        for bad in ({"head": {}}, {"state": "open", "head": {"sha": SHA}, "base": {"ref": ""}}):
            with self.subTest(pull=bad):
                with self.assertRaises(gate.CheckReadError):
                    gate.GitHubChecks(run=lambda *a, **k: completed(bad)).pull("owner/api", "4")


class CommandLineTests(unittest.TestCase):
    def run_main(self, argv, provider=None):
        stdout = io.StringIO()
        with mock.patch.object(gate, "GitHubChecks", return_value=provider), \
                contextlib.redirect_stdout(stdout):
            code = gate.main(argv)
        return code, stdout.getvalue()

    def test_the_result_is_printed_as_json_and_only_a_pass_exits_zero(self) -> None:
        passing = FakeCI([(run("build"),)])
        code, out = self.run_main(["--repo", "owner/api", "--pr", "4", "--sha", SHA], passing)
        self.assertEqual(0, code)
        self.assertEqual(gate.CI_PASSED, json.loads(out)["status"])

        failing = FakeCI([(run("build", gate.FAILED),)])
        code, out = self.run_main(["--repo", "owner/api", "--pr", "#4", "--sha", SHA,
                                   "--once"], failing)
        self.assertEqual(1, code)
        self.assertEqual(gate.CI_FAILED, json.loads(out)["status"])

    def test_invalid_arguments_are_refused_before_reading_anything(self) -> None:
        for argv in (["--repo", "owner", "--pr", "4", "--sha", SHA],
                     ["--repo", "owner/api", "--pr", "x", "--sha", SHA],
                     ["--repo", "owner/api", "--pr", "4", "--sha", "abc"],
                     ["--repo", "owner/api", "--pr", "4", "--sha", SHA, "--timeout", "0"],
                     ["--repo", "owner/api", "--pr", "4", "--sha", SHA,
                      "--poll-interval", "0"]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        self.run_main(argv)
                self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
