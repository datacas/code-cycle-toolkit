"""#446: a review or rereview publication is confirmed by a bounded re-read.

The runtime never posts. After a `review` or `rereview` stage the change
request is read again, with backoff, before a publication that was not seen is
called absent, and a read failure is never read as an absence.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, TypeVar

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import cycle_history as ch  # noqa: E402
import cycle_journal  # noqa: E402
import run_cycle as rc  # noqa: E402
import telemetry as tm  # noqa: E402
import test_cycle_history as th  # noqa: E402
from test_run_cycle import APPROVED, RunCycleTestCase, Scripted  # noqa: E402

REPO, REQUEST = th.REPO, th.REQUEST
T = TypeVar("T")


def required(value: T | None) -> T:
    assert value is not None
    return value


def attempt_in(task: str) -> str:
    return required(re.search(r"Runtime attempt_id: ([^. ]+)\.", task)).group(1)


def publication(store: ch.CycleHistoryStore, attempt: str) -> dict[str, Any]:
    return required(store.publication(attempt))


def check_of(store: ch.CycleHistoryStore, attempt: str) -> dict[str, Any]:
    return required(store.publication_check(attempt))


class StoreTests(th.StoreTestCase):
    def expect(self, attempt: str, stage: str = "review", **kw) -> None:
        self.store.expect_publication(attempt, REPO, REQUEST, stage, **kw)

    def settle(self, attempt: str, state: str, rereads: int = 3, error=None,
               stage: str = "review") -> None:
        self.store.settle_publication(attempt, REPO, REQUEST, stage, state, rereads, error)

    def state(self, attempt: str) -> str:
        return publication(self.store, attempt)["state"]

    def test_states_distinguish_a_confirmed_absence_from_a_read_failure(self) -> None:
        self.expect("attempt-a")
        self.expect("attempt-b")

        self.settle("attempt-a", "failed")
        self.settle("attempt-b", "verification_error", error="gh exited 1")

        self.assertEqual("failed", self.state("attempt-a"))
        self.assertEqual("verification_error", self.state("attempt-b"))
        check = check_of(self.store, "attempt-b")
        self.assertEqual((3, "gh exited 1"), (check["rereads"], check["last_error"]))
        self.assertEqual([], self.store.pending_publications(REPO, REQUEST))
        self.assertEqual(["attempt-b"], [item["attempt_id"] for item in
                                         self.store.unverified_publications(REPO, REQUEST, "review")])

    def test_the_provider_error_is_redacted_and_bounded(self) -> None:
        self.expect("attempt-a")

        self.settle("attempt-a", "verification_error",
                    error="HTTP 502 Authorization: Bearer abc123secret " + "x" * 500)

        stored = check_of(self.store, "attempt-a")["last_error"]
        self.assertNotIn("abc123secret", stored)
        self.assertLessEqual(len(stored), 201)

    def test_a_failed_attempt_is_retried_once_and_the_claim_is_exclusive(self) -> None:
        self.expect("attempt-a")
        self.settle("attempt-a", "failed")

        first = self.store.claim_retries(REPO, REQUEST, "review")
        second = self.store.claim_retries(REPO, REQUEST, "review")

        self.assertEqual((["attempt-a"], []), (first, second))
        self.expect("attempt-b", retry_of=first)
        self.assertEqual("attempt-b", check_of(self.store, "attempt-a")["retried_by"])
        self.assertEqual("attempt-a", check_of(self.store, "attempt-b")["retry_of"])

    def test_a_retry_is_never_retried_again(self) -> None:
        self.expect("attempt-a")
        self.settle("attempt-a", "failed")
        self.expect("attempt-b", retry_of=self.store.claim_retries(REPO, REQUEST, "review"))

        self.settle("attempt-b", "failed")

        self.assertEqual([], self.store.claim_retries(REPO, REQUEST, "review"))

    def test_a_published_attempt_is_never_moved_back(self) -> None:
        remote = th.FakeRemote(th.CLAIMED)
        self.sync(remote)
        self.expect("attempt-a")
        remote.items.append(th.comment(30, None, extra=th._receipt("attempt-a", "review")))
        self.sync(remote)

        self.settle("attempt-a", "failed")

        self.assertEqual("published", self.state("attempt-a"))
        self.assertEqual([], self.store.claim_retries(REPO, REQUEST, "review"))

    def test_a_later_publication_supersedes_a_failed_attempt(self) -> None:
        remote = th.FakeRemote(th.CLAIMED)
        self.sync(remote)
        self.expect("attempt-a")
        self.settle("attempt-a", "failed")
        self.expect("attempt-b")
        remote.items.append(th.comment(30, None, extra=th._receipt("attempt-b", "review")))

        self.sync(remote)

        self.assertEqual("superseded", self.state("attempt-a"))
        self.assertEqual("published", self.state("attempt-b"))

    def test_an_unknown_settled_state_is_refused(self) -> None:
        self.expect("attempt-a")
        with self.assertRaises(ch.HistoryError):
            self.settle("attempt-a", "lost")


class SchemaCompatibilityTests(unittest.TestCase):
    OLD_TABLE = (
        "CREATE TABLE cycle_publications ("
        "attempt_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, change_request_id TEXT NOT NULL, "
        "stage TEXT NOT NULL CHECK(stage IN ('implement', 'review', 'resolve', 'rereview')), "
        "state TEXT NOT NULL CHECK(state IN ('pending', 'published', 'superseded')), "
        "comment_id TEXT, head_sha TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL)")

    def test_an_older_outbox_is_migrated_without_losing_a_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.sqlite"
            connection = sqlite3.connect(path)
            connection.execute(self.OLD_TABLE)
            connection.execute("CREATE INDEX cycle_publications_pending ON cycle_publications "
                               "(repo_id, change_request_id, stage, state)")
            connection.execute(
                "INSERT INTO cycle_publications VALUES ('old-1', ?, ?, 'review', 'pending', "
                "NULL, NULL, 't', 't', 1)", (REPO, REQUEST))
            connection.execute(
                "INSERT INTO cycle_publications VALUES ('old-2', ?, ?, 'resolve', 'published', "
                "'9', ?, 't', 't', 1)", (REPO, REQUEST, "b" * 40))
            connection.commit()
            connection.close()

            tm.Telemetry(path)
            tm.Telemetry(path)  # a second open leaves the migrated table alone
            store = ch.CycleHistoryStore(path)

            self.assertEqual(["old-1"], [item["attempt_id"] for item in
                                         store.pending_publications(REPO, REQUEST)])
            old = publication(store, "old-2")
            self.assertEqual(("published", "9"), (old["state"], old["comment_id"]))
            store.settle_publication("old-1", REPO, REQUEST, "review", "failed", 3)
            self.assertEqual("failed", publication(store, "old-1")["state"])
            connection = sqlite3.connect(path)
            self.addCleanup(connection.close)
            indexes = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='cycle_publications'")]
            self.assertIn("cycle_publications_pending", indexes)

    def test_an_outbox_reader_of_the_earlier_states_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.sqlite"
            tm.Telemetry(path)
            store = ch.CycleHistoryStore(path)
            store.expect_publication("attempt-a", REPO, REQUEST, "review")

            # The earlier reader: rows of ten columns, `pending` only.
            self.assertEqual(["attempt-a"], [item["attempt_id"] for item in
                                             store.pending_publications(REPO, REQUEST, "review")])
            connection = sqlite3.connect(path)
            self.addCleanup(connection.close)
            row = connection.execute("SELECT * FROM cycle_publications").fetchone()
            self.assertEqual(10, len(row))
            self.assertEqual(29, tm.SCHEMA_VERSION)


class DelayedRemote(th.FakeRemote):
    """A change request whose published comment appears after some reads."""

    def __init__(self, *args, **kw) -> None:
        super().__init__(*args, **kw)
        self.hidden: list = []
        self.reads = 0
        self.reveal_after = 0
        self.fail_after: int | None = None

    def hide(self, item, *, until: int) -> None:
        """`item` is posted now but seen only from read `until` + 1."""
        self.hidden.append(item)
        self.reads, self.reveal_after = 0, until

    def probe(self, repo_id, change_request_id):
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise rc.CycleDriverError("gh exited 1: HTTP 502")
        if self.hidden and self.reads > self.reveal_after:
            self.items.extend(self.hidden)
            self.hidden = []
        return super().probe(repo_id, change_request_id)

    def receipts(self, attempt: str) -> int:
        return sum(attempt in item.body for item in self.items) + sum(
            attempt in item.body for item in self.hidden)


class Publisher(Scripted):
    """A reviewer that publishes, the way a stage does, or does not."""

    def __init__(self, remote: DelayedRemote, stage: str, publish) -> None:
        super().__init__("claude")
        self.remote, self.stage, self.publish = remote, stage, publish
        self.prompts: list[str] = []

    def spoken(self, task: str) -> str:
        self.prompts.append(task)
        self.publish(self.remote, self.stage, attempt_in(task), task)
        return super().spoken(task)


def post(number: int, *, until: int = 0):
    def publish(remote: DelayedRemote, stage: str, attempt: str, _task: str) -> None:
        item = th.comment(number, None, extra=th._receipt(attempt, stage))
        if until:
            remote.hide(item, until=until)
        else:
            remote.items.append(item)
    return publish


def silent(_remote, _stage, _attempt, _task) -> None:
    """A stage whose publication is never seen."""


def unreadable_after_posting(remote: DelayedRemote, stage: str, attempt: str, task: str) -> None:
    """A stage that publishes, after which the host stops answering reads."""
    post(30, until=99)(remote, stage, attempt, task)
    remote.fail_after = 0


def unreadable_without_posting(remote: DelayedRemote, _stage, _attempt, _task) -> None:
    remote.fail_after = 0


class _RereadCases(RunCycleTestCase):
    """Cases shared by `review` and `rereview`; `stage` names the one under test."""

    stage = ""

    def drive(self, remote: DelayedRemote, publish):
        self.sleeps: list[float] = []
        self.reviewer = Publisher(remote, self.stage, publish)
        return self.run_cycle(
            Scripted("codex"), self.reviewer, start_from=self.stage,
            change_request_id=REQUEST, history_remote=remote,
            trusted_authors=("reviewer",), publication_sleep=self.sleeps.append)

    def history(self) -> ch.CycleHistoryStore:
        return ch.CycleHistoryStore(self.store.path)

    def attempt(self) -> str:
        return attempt_in(self.reviewer.prompts[-1])

    def warnings(self, report) -> list[str]:
        return [w for detail in report.stage_details for w in detail.warnings
                if "publication" in w]

    def events(self):
        return [row for row in cycle_journal.events(self.store.path, REPO)
                if row.get("component") == "publication_reread"]

    def test_a_delayed_publication_is_confirmed_by_a_reread(self) -> None:
        remote = DelayedRemote(th.CLAIMED)

        report = self.drive(remote, post(30, until=2))

        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual([], self.warnings(report))
        self.assertEqual([1.0, 2.0], self.sleeps)
        attempt = self.attempt()
        self.assertEqual("published", publication(self.history(), attempt)["state"])
        self.assertEqual(2, check_of(self.history(), attempt)["rereads"])
        (event,) = self.events()
        self.assertEqual("confirmed", event["status"])
        self.assertEqual(1, len(self.reviewer.prompts))

    def test_a_publication_seen_at_once_needs_no_wait(self) -> None:
        remote = DelayedRemote(th.CLAIMED)

        report = self.drive(remote, post(30))

        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual([], self.sleeps)
        self.assertEqual(0, check_of(self.history(), self.attempt())["rereads"])

    def test_an_absent_publication_is_failed_after_three_rereads(self) -> None:
        remote = DelayedRemote(th.CLAIMED)

        report = self.drive(remote, silent)

        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual([1.0, 2.0, 4.0], self.sleeps)
        attempt = self.attempt()
        self.assertEqual("failed", publication(self.history(), attempt)["state"])
        self.assertEqual(3, check_of(self.history(), attempt)["rereads"])
        self.assertTrue([w for w in self.warnings(report) if attempt in w and "marked failed" in w])
        (event,) = self.events()
        self.assertEqual("not_observed", event["status"])

    def test_the_retry_reuses_a_late_comment_and_never_duplicates_it(self) -> None:
        remote = DelayedRemote(th.CLAIMED)
        # The comment exists on the host but stays invisible to every read.
        self.drive(remote, post(30, until=99))
        lost = self.attempt()
        self.assertEqual("failed", publication(self.history(), lost)["state"])
        remote.items.extend(remote.hidden)
        remote.hidden = []

        report = self.drive(remote, silent)

        # The resume's sync finds the late comment: nothing is retried.
        self.assertEqual("published", publication(self.history(), lost)["state"])
        self.assertNotIn("instead of posting a new one", self.reviewer.prompts[0])
        self.assertEqual(1, remote.receipts(lost))
        self.assertEqual(rc.APPROVED_END, report.status)

    def test_a_confirmed_absence_is_retried_once_idempotently(self) -> None:
        remote = DelayedRemote(th.CLAIMED)
        self.drive(remote, silent)
        lost = self.attempt()

        self.drive(remote, post(40))
        retry = self.attempt()

        self.assertIn(lost, self.reviewer.prompts[0])
        self.assertIn("instead of posting a new one", self.reviewer.prompts[0])
        self.assertEqual("superseded", publication(self.history(), lost)["state"])
        self.assertEqual("published", publication(self.history(), retry)["state"])
        self.assertEqual(retry, check_of(self.history(), lost)["retried_by"])
        self.assertEqual(lost, check_of(self.history(), retry)["retry_of"])
        self.assertEqual(1, remote.receipts(retry))
        # The budget is spent: a later dispatch carries no retry directive.
        self.drive(remote, post(50))
        self.assertNotIn("instead of posting a new one", self.reviewer.prompts[0])

    def test_a_failed_retry_is_recorded_and_not_retried_again(self) -> None:
        remote = DelayedRemote(th.CLAIMED)
        self.drive(remote, silent)
        lost = self.attempt()

        report = self.drive(remote, silent)
        retry = self.attempt()
        self.drive(remote, silent)

        self.assertEqual("failed", publication(self.history(), retry)["state"])
        self.assertTrue([w for w in self.warnings(report) if retry in w and "not retried again" in w])
        self.assertNotIn(lost, self.reviewer.prompts[0])
        self.assertNotIn("instead of posting a new one", self.reviewer.prompts[0])

    def test_a_persistent_read_failure_is_a_verification_error(self) -> None:
        remote = DelayedRemote(th.CLAIMED)

        report = self.drive(remote, unreadable_after_posting)

        self.assertEqual("BLOCKED", report.status)
        self.assertEqual("publication_unverified", report.stop_reason)
        stopped = report.stopped_because or ""
        self.assertIn("could not be verified", stopped)
        self.assertNotIn("not observed", stopped)
        self.assertEqual([1.0, 2.0, 4.0], self.sleeps)
        self.assertEqual(1, len(self.reviewer.prompts))
        attempt = self.attempt()
        self.assertEqual("verification_error", publication(self.history(), attempt)["state"])
        check = check_of(self.history(), attempt)
        self.assertEqual(3, check["rereads"])
        self.assertIn("HTTP 502", check["last_error"])
        (event,) = self.events()
        self.assertEqual("verification_error", event["status"])
        # Nothing was published again, and the failed reads left no new comment.
        self.assertEqual(1, remote.receipts(attempt))

    def test_a_resume_verifies_the_original_publication_before_dispatching(self) -> None:
        remote = DelayedRemote(th.CLAIMED)
        self.drive(remote, unreadable_after_posting)
        unverified = self.attempt()
        # The comment is visible once the host answers again.
        remote.items.extend(remote.hidden)
        remote.hidden, remote.fail_after = [], None

        report = self.drive(remote, silent)

        self.assertEqual("published", publication(self.history(), unverified)["state"])
        self.assertNotIn("instead of posting a new one", self.reviewer.prompts[0])
        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual(1, remote.receipts(unverified))

    def test_a_resume_that_still_cannot_read_stops_without_dispatching(self) -> None:
        remote = DelayedRemote(th.CLAIMED)
        self.drive(remote, unreadable_after_posting)
        # The history check of the resume succeeds, the verification read fails.
        remote.hidden, remote.reads, remote.fail_after = [], 0, 1

        report = self.drive(remote, silent)

        self.assertEqual("BLOCKED", report.status)
        self.assertEqual("publication_unverified", report.stop_reason)
        self.assertEqual([], self.reviewer.prompts)

    def test_a_resume_that_finds_the_original_absent_retries_it_once(self) -> None:
        remote = DelayedRemote(th.CLAIMED)
        self.drive(remote, unreadable_without_posting)
        unverified = self.attempt()
        remote.fail_after = None

        self.drive(remote, post(40))

        self.assertIn(unverified, self.reviewer.prompts[0])
        self.assertIn("instead of posting a new one", self.reviewer.prompts[0])
        self.assertEqual("superseded", publication(self.history(), unverified)["state"])


class ReviewRereadTests(_RereadCases):
    stage = "review"


class RereviewRereadTests(_RereadCases):
    stage = "rereview"


# The shared cases run through the two classes above, not on their own.
del _RereadCases


class ResolveKeepsItsSingleReadTests(RunCycleTestCase):
    """`resolve` belongs to #364's mechanism and is not re-read."""

    def test_a_resolve_publication_is_not_waited_for(self) -> None:
        remote = DelayedRemote(th.CLAIMED)
        sleeps: list[float] = []

        report = self.run_cycle(
            Scripted("codex", resolutions=[th.claimed()]),
            Scripted("claude", reviews=[APPROVED]), start_from="resolve",
            change_request_id=REQUEST, history_remote=remote,
            trusted_authors=("reviewer",), publication_sleep=sleeps.append)

        # Only the rereview that follows is re-read; the resolve stage is not.
        self.assertEqual([1.0, 2.0, 4.0], sleeps)
        (warning,) = [w for w in report.stage_details[0].warnings if "not observed" in w]
        self.assertNotIn("re-reads", warning)
        self.assertEqual("resolve", report.stages[0].role)


if __name__ == "__main__":
    unittest.main()
