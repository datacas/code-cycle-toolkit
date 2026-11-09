"""A rerun resumes a finished publishing stage at the next stage (#464)."""
from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cycle_cli  # noqa: E402
import cycle_journal  # noqa: E402
import cycle_resume as cr  # noqa: E402

REPO = "owner/repo"
TASK = "464"
SHA = "a" * 40
OTHER = "b" * 40
DONE = {"work_units": [{"id": "WU-1", "commit_sha": SHA, "rollback": "independent"}],
        "tests": {"ran": True, "passed": True, "checks": []}}


def payload(**changes) -> dict:
    return {"status": "BLOCKED", "head_sha": SHA, "change_request_id": "463", **DONE, **changes}


class WorkDoneTests(unittest.TestCase):
    def test_a_result_that_claims_or_proves_its_work_is_done(self) -> None:
        for label, role, data in (
                ("structured evidence", "implement", payload()),
                ("a claimed completion", "implement", payload(
                    completed_status="IMPLEMENTED", work_units=None, tests=None)),
                ("a completed status", "implement", payload(status="IMPLEMENTED")),
                ("resolve outcomes", "resolve", payload(
                    finding_outcomes=[{"id": "REV-001", "status": "resolved"}], work_units=None)),
                ("a claimed resolve", "resolve", payload(completed_status="RESOLVED"))):
            with self.subTest(label):
                self.assertTrue(cr.work_done(data, role))

    def test_a_pull_request_that_exists_is_not_completion(self) -> None:
        for label, role, data in (
                ("no work units", "implement", payload(work_units=None)),
                ("tests did not run", "implement", payload(tests={"ran": False})),
                ("no head", "implement", payload(head_sha=None)),
                ("a short head", "implement", payload(head_sha="abc123")),
                ("no change request", "implement", payload(change_request_id=None)),
                ("a pending publication", "implement", payload(publication_pending=True)),
                ("an environment blocker", "implement", payload(
                    environment_blocker={"kind": "sandbox_local_access"})),
                ("an essential refusal", "implement", payload(essential_permission="x")),
                ("a failed check", "implement", payload(
                    tests={"ran": True, "checks": [{"id": "c", "result": "fail"}]})),
                ("a review role", "review", payload(status="APPROVED")),
                ("resolve without outcomes", "resolve", payload(work_units=None)),
                ("not a mapping", "implement", [])):
            with self.subTest(label):
                self.assertFalse(cr.work_done(data, role))


class JournalCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "telemetry.sqlite"
        self.journal = cycle_journal.Journal(self.database, REPO, TASK, "cycle-1")
        self.sequence = 0

    def point(self, role: Any = "implement", *, head: Any = SHA, number: Any = "463",
              status: Any = "blocked_done", next_stage: Any = None, cycle: Any = "cycle-1",
              reason: Any = "the stage said CI pending") -> None:
        self.row("stage_result", cycle, role=role, component=cr.POINT, status=status, reason=reason,
                 change_request_id=number,
                 correlation={"head_sha": head, "branch": "task-464",
                              "next_stage": next_stage or cr.NEXT_STAGE.get(role, "review")})

    def row(self, kind, cycle="cycle-1", **fields) -> None:
        cycle_journal.record_event(self.database, repo_id=REPO, task_id=TASK, cycle_id=cycle,
                                   kind=kind, **fields)

    def stop(self, cycle="cycle-1") -> None:
        self.row("stop", cycle, component="runtime", status="HUMAN_INTERVENTION", reason="blocked")

    @staticmethod
    def pull(state: str = "OPEN", head: str = SHA, branch: str = "task-464"):
        return lambda number: {"state": state, "headRefOid": head, "headRefName": branch}

    def resume(self, **pull: str):
        return cr.auto_resume(self.database, REPO, TASK, read_pull=self.pull(**pull))


class AutoResumeTests(JournalCase):
    def test_a_finished_implement_resumes_at_review_with_its_cause_and_checks(self) -> None:
        self.row("stage_end", role="implement", component="claude", status="succeeded")
        self.point()
        self.stop()
        resume = self.resume()
        self.assertEqual(("review", "463", SHA, "implement"),
                         (resume.start_from, resume.change_request_id, resume.head_sha,
                          resume.blocked_role))
        self.assertEqual("blocked_done", resume.status)
        self.assertIn("CI pending", resume.cause)
        self.assertTrue(any("open at the recorded head" in check for check in resume.checks))
        self.assertIn("review", resume.notice())

    def test_a_finished_resolve_resumes_at_rereview(self) -> None:
        self.point("resolve", next_stage="rereview", status="completed")
        self.stop()
        self.assertEqual("rereview", self.resume().start_from)

    def test_nothing_resumes_without_a_point_or_after_a_later_stage(self) -> None:
        self.assertIsNone(self.resume())
        self.row("stage_end", role="implement", component="claude", status="succeeded")
        self.stop()
        self.assertIsNone(self.resume())
        self.point()
        self.stop()
        self.assertIsNotNone(self.resume())
        self.row("stage_start", "cycle-2", role="review", component="runtime")
        self.assertIsNone(self.resume())

    def test_an_unreadable_history_starts_the_work_item_as_before(self) -> None:
        self.assertIsNone(cr.auto_resume(Path("/nonexistent/dir/db.sqlite"), REPO, TASK,
                                         read_pull=self.pull()))

    def test_a_mismatched_pull_request_head_or_state_is_refused_with_the_mismatch(self) -> None:
        self.point()
        self.stop()
        for label, pull, contains in (
                ("moved head", {"head": OTHER}, "not at the recorded"),
                ("merged", {"state": "MERGED"}, "MERGED"),
                ("closed", {"state": "CLOSED"}, "not open"),
                ("another branch", {"branch": "other"}, "recorded branch")):
            with self.subTest(label):
                with self.assertRaises(cr.ResumeRefused) as caught:
                    self.resume(**pull)
                self.assertIn(contains, str(caught.exception))
        self.assertIn("--from review --pr 463", str(self.refusal(head=OTHER)))

    def refusal(self, **pull) -> Exception:
        try:
            self.resume(**pull)
        except cr.ResumeRefused as error:
            return error
        raise AssertionError("no refusal")

    def test_a_point_whose_run_recorded_no_stop_is_refused(self) -> None:
        self.point()
        with self.assertRaises(cr.ResumeRefused) as caught:
            self.resume()
        self.assertIn("recorded no stop", str(caught.exception))

    def test_an_inconsistent_persisted_point_is_refused(self) -> None:
        for label, change in (
                ("a short head", {"head": "abc"}),
                ("the wrong next stage", {"next_stage": "rereview"}),
                ("no change request", {"number": None}),                ("a stage that publishes nothing", {"role": "review"})):
            with self.subTest(label):
                self.setUp()
                self.point(**change)
                self.stop()
                with self.assertRaises(cr.ResumeRefused) as caught:
                    self.resume()
                self.assertIn("inconsistent", str(caught.exception))

    def test_the_resume_is_idempotent_and_records_what_it_checked(self) -> None:
        self.point()
        self.stop()
        first = self.resume()
        self.assertTrue(cr.record_auto_resume(self.database, REPO, TASK, first))
        # The resumed run has not stopped yet: a second rerun must not start beside it.
        with self.assertRaises(cr.ResumeRefused):
            self.resume()
        # It stopped before any stage ran: the same point resumes again.
        self.stop("cycle-2")
        self.assertEqual("review", self.resume().start_from)
        # Once a stage of the resumed run began, nothing is left to resume.
        self.row("stage_start", "cycle-2", role="review", component="runtime")
        self.assertIsNone(self.resume())
        recorded = [row for row in cycle_journal.events(self.database, REPO, task_id=TASK)
                    if row["component"] == cr.RESUMED]
        self.assertEqual(1, len(recorded))
        self.assertEqual({"head_sha": SHA, "recovery_stage": "review",
                          "original_status": "blocked_done", "point": first.point},
                         recorded[0]["correlation"])
        self.assertIn("recorded head", recorded[0]["detail"])
        self.assertIn("CI pending", recorded[0]["reason"])


class RecordedPointTests(JournalCase):
    def test_only_proven_work_leaves_a_point(self) -> None:
        self.assertFalse(cr.record_resume_point(
            self.journal, "implement", payload(work_units=None), cause="x", status="blocked_done"))
        self.assertTrue(cr.record_resume_point(
            self.journal, "implement", payload(), cause="done", status="completed",
            branch="task-464"))
        rows = [row for row in cycle_journal.events(self.database, REPO, task_id=TASK)
                if row["component"] == cr.POINT]
        self.assertEqual(1, len(rows))
        self.assertEqual("463", rows[0]["change_request_id"])
        self.assertEqual({"head_sha": SHA, "next_stage": "review", "branch": "task-464"},
                         rows[0]["correlation"])


class CliTests(JournalCase):
    """The rerun needs no `--from` and `--pr` (#464)."""

    def args(self, **changes) -> argparse.Namespace:
        values = dict(start_from="implement", change_request=None, continue_work=False,
                      local_only=False, auto_resume=True, orca_action="launch", task=TASK,
                      database=str(self.database))
        return argparse.Namespace(**{**values, **changes})

    def api(self):
        return SimpleNamespace(Path=Path, default_database_path=lambda: self.database,
                               CycleDriverError=RuntimeError,
                               validate_start=lambda *args, **kwargs: None)

    def apply(self, args, config=None):
        return cycle_cli.auto_resume_of(self.api(), args, config or {}, REPO,
                                        read_pull=self.pull())

    def test_implement_resumes_from_review_without_from_and_pr(self) -> None:
        self.point()
        self.stop()
        args = self.args()
        resume = self.apply(args)
        self.assertEqual(("review", "463"), (args.start_from, args.change_request))
        self.assertEqual("review", resume.start_from)
        # Nothing is recorded until the run itself starts, so a rerun that fails
        # a preflight check does not look like a run in progress.
        self.assertEqual([], [row for row in cycle_journal.events(self.database, REPO, task_id=TASK)
                              if row["component"] == cr.RESUMED])
        self.assertEqual("review", self.apply(self.args()).start_from)

    def test_resolve_resumes_from_rereview_without_from_and_pr(self) -> None:
        self.point("resolve")
        self.stop()
        args = self.args()
        self.apply(args)
        self.assertEqual(("rereview", "463"), (args.start_from, args.change_request))

    def test_an_explicit_choice_is_never_overridden(self) -> None:
        self.point()
        self.stop()
        for label, changes in (("--from", {"start_from": "resolve", "change_request": "9"}),
                               ("--pr", {"change_request": "9"}),
                               ("--continue", {"continue_work": True}),
                               ("--local-only", {"local_only": True}),
                               ("--no-auto-resume", {"auto_resume": False}),
                               ("an Orca resume", {"orca_action": "resume"})):
            with self.subTest(label):
                args = self.args(**changes)
                before = (args.start_from, args.change_request)
                self.assertIsNone(self.apply(args))
                self.assertEqual(before, (args.start_from, args.change_request))

    def test_a_host_other_than_github_is_left_alone(self) -> None:
        self.point()
        self.stop()
        args = self.args()
        self.assertIsNone(self.apply(args, {"code_cycle": {"code_host": "bitbucket"}}))
        self.assertEqual("implement", args.start_from)

    def test_a_point_that_cannot_be_honoured_stops_the_run_and_names_the_mismatch(self) -> None:
        self.point()
        self.stop()
        with self.assertRaises(RuntimeError) as caught:
            cycle_cli.auto_resume_of(self.api(), self.args(), {}, REPO,
                                     read_pull=self.pull(head=OTHER))
        message = str(caught.exception)
        self.assertIn("cannot be resumed automatically", message)
        self.assertIn("not at the recorded", message)
        self.assertIn("--no-auto-resume", message)

    def test_a_work_item_without_history_starts_as_before(self) -> None:
        self.assertIsNone(self.apply(self.args(database=str(self.database) + ".none")))


if __name__ == "__main__":
    unittest.main()
