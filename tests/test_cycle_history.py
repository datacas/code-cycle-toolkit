"""#223: the local cycle history, verified against the change request before use."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import cycle_history as ch  # noqa: E402
import review_contract as contract  # noqa: E402
import run_cycle as rc  # noqa: E402
import telemetry as tm  # noqa: E402
from test_run_cycle import (  # noqa: E402
    APPROVED,
    RunCycleTestCase,
    Scripted,
    claimed,
    rereview,
)

REPO = "owner/api"
REQUEST = "74"
HEAD = "a" * 40


@contextmanager
def database(path):
    """A committed and closed connection: an open one keeps Windows from
    deleting the temporary database."""
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            yield connection


def _time(second: int) -> str:
    return f"2026-10-13T10:{second // 60:02d}:{second % 60:02d}Z"


def _receipt(attempt: str, stage: str = "resolve", *, request: str = REQUEST) -> str:
    value = {"schema": 2, "repo": REPO, "change_request_id": request, "stage": stage,
             "attempt_id": attempt, "head_sha": "b" * 40, "status": "RESOLVED",
             "finding_outcomes": []}
    return "<!-- code-cycle-stage " + json.dumps(value) + " -->"


def comment(identifier: int, kind: str | None, number: int = 1, status: str = "open", *,
            author: str = "reviewer", edited: int | None = None,
            extra: str = "") -> contract.ProviderComment:
    """A comment publishing REV-001 in a review or triage run, or plain prose."""
    lines = ["Summary prose that must never be stored."]
    if kind == "review":
        lines.append(f"#### [CCR-20261013-{number:03d}] · senior_reviewer · "
                     "anthropic/sonnet-5→sonnet-5 · high · schema:2")
    elif kind == "triage":
        lines.append(f"#### [CCT-20261013-{number:03d}] · cheap_coder · "
                     f"openai/luna→luna · high · triaged:{'0' * 40} · schema:2")
    if kind is not None:
        lines.append(f"#### [REV-001] · high · {status} · valid · blocks:yes — Secret title")
    if extra:
        lines.append(extra)
    return contract.ProviderComment(
        author=author, body="\n".join(lines), id=str(identifier),
        updated_at=_time(edited if edited is not None else identifier))


CLAIMED = [comment(10, "review"), comment(20, "triage", status="resolved")]
SURVIVED_ONCE = CLAIMED + [comment(30, "review", 2)]


class FakeRemote:
    """A change request in memory, counting how the store reads it."""

    def __init__(self, comments=(), head: str = HEAD) -> None:
        self.items = list(comments)
        self.head = head
        self.probes = 0
        self.fetches: list[str | None] = []
        self.failure: str | None = None

    def probe(self, repo_id, change_request_id):
        self.probes += 1
        if self.failure:
            raise rc.CycleDriverError(self.failure)
        latest = max((item.updated_at for item in self.items if item.updated_at), default=None)
        return ch.RemoteMark(self.head, len(self.items), latest)

    def comments(self, repo_id, change_request_id, since=None):
        self.fetches.append(since)
        return [item for item in self.items
                if since is None or (item.updated_at and item.updated_at >= since)]

    def replace(self, identifier: int, new: contract.ProviderComment) -> None:
        self.items = [new if item.id == str(identifier) else item for item in self.items]


def statuses(runs):
    return [(run.kind, dict(run.statuses)) for run in runs]


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "t.sqlite"
        tm.Telemetry(self.path)
        self.store = ch.CycleHistoryStore(self.path)

    def sync(self, remote, trusted=("reviewer",)):
        return self.store.sync(REPO, REQUEST, trusted_authors=trusted, remote=remote)

    def count(self, table: str) -> int:
        with database(self.path) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class RebuildAndVerifyTests(StoreTestCase):
    def test_a_missing_history_is_rebuilt_from_the_trusted_comments(self) -> None:
        remote = FakeRemote(SURVIVED_ONCE)

        synced = self.sync(remote)

        self.assertEqual("rebuilt", synced.source)
        self.assertEqual([None], remote.fetches)
        expected = contract.comment_run_history(SURVIVED_ONCE, trusted_authors=("reviewer",))
        self.assertEqual(statuses(expected), statuses(synced.runs))
        self.assertEqual({"REV-001": {"status": "open", "claims": 1, "survivals": 1}},
                         self.store.finding_state(REPO, REQUEST))

    def test_a_verified_resume_checks_once_and_fetches_nothing(self) -> None:
        remote = FakeRemote(SURVIVED_ONCE)
        first = self.sync(remote)
        remote.probes, remote.fetches = 0, []

        again = self.sync(remote)

        self.assertEqual("local", again.source)
        self.assertEqual(1, remote.probes)
        self.assertEqual([], remote.fetches)
        self.assertEqual(statuses(first.runs), statuses(again.runs))

    def test_an_empty_change_request_is_verified_too(self) -> None:
        remote = FakeRemote([])
        self.sync(remote)
        remote.fetches = []

        self.assertEqual("local", self.sync(remote).source)
        self.assertEqual([], remote.fetches)

    def test_a_new_comment_is_reconciled_from_the_watermark(self) -> None:
        remote = FakeRemote(CLAIMED)
        self.sync(remote)
        remote.items.append(comment(30, "review", 2))
        remote.fetches = []

        synced = self.sync(remote)

        self.assertEqual("reconciled", synced.source)
        self.assertEqual([_time(20)], remote.fetches)
        self.assertEqual(1, self.store.finding_state(REPO, REQUEST)["REV-001"]["survivals"])

    def test_an_edited_earlier_comment_is_reconciled(self) -> None:
        remote = FakeRemote(SURVIVED_ONCE)
        self.sync(remote)
        # The second review is edited to agree the claimed fix holds.
        remote.replace(30, comment(30, "review", 2, "resolved", edited=99))

        synced = self.sync(remote)

        self.assertEqual("reconciled", synced.source)
        self.assertEqual(0, self.store.finding_state(REPO, REQUEST)["REV-001"]["survivals"])
        self.assertEqual(_time(99), self.store.watermark(REPO, REQUEST)["latest_updated_at"])

    def test_a_deleted_comment_rebuilds_the_history(self) -> None:
        remote = FakeRemote(SURVIVED_ONCE)
        self.sync(remote)
        remote.items = remote.items[:2]
        remote.fetches = []

        synced = self.sync(remote)

        self.assertEqual("rebuilt", synced.source)
        self.assertEqual([_time(30), None], remote.fetches)
        self.assertEqual(0, self.store.finding_state(REPO, REQUEST)["REV-001"]["survivals"])
        self.assertEqual(2, self.count("cycle_history_comments"))

    def test_a_trusted_author_change_rebuilds_the_history(self) -> None:
        remote = FakeRemote(SURVIVED_ONCE)
        self.sync(remote)
        remote.fetches = []

        synced = self.sync(remote, trusted=("someone-else",))

        self.assertEqual("rebuilt", synced.source)
        self.assertEqual([None], remote.fetches)
        self.assertEqual([], synced.runs)

    def test_a_head_change_alone_reconciles_without_new_rows(self) -> None:
        remote = FakeRemote(SURVIVED_ONCE)
        self.sync(remote)
        remote.head = "c" * 40

        self.assertEqual("reconciled", self.sync(remote).source)
        self.assertEqual("c" * 40, self.store.watermark(REPO, REQUEST)["head_sha"])
        self.assertEqual("local", self.sync(remote).source)

    def test_processing_the_same_comments_twice_duplicates_nothing(self) -> None:
        remote = FakeRemote(SURVIVED_ONCE)
        self.sync(remote)
        tables = ("cycle_history_comments", "cycle_history_runs",
                  "cycle_history_findings", "cycle_history_finding_state")
        before = {table: self.count(table) for table in tables}
        # A reconciliation from the start reprocesses every comment.
        with database(self.path) as connection:
            connection.execute("UPDATE cycle_history_sync SET latest_updated_at=?, head_sha=?",
                               (_time(0), "f" * 40))

        synced = self.sync(remote)

        self.assertEqual("reconciled", synced.source)
        self.assertEqual(before, {table: self.count(table) for table in tables})
        self.assertEqual(1, self.store.finding_state(REPO, REQUEST)["REV-001"]["claims"])

    def test_an_unreadable_comment_keeps_the_previous_history(self) -> None:
        remote = FakeRemote(CLAIMED)
        self.sync(remote)
        watermark = self.store.watermark(REPO, REQUEST)
        remote.items.append(contract.ProviderComment(
            author="reviewer", id="40", updated_at=_time(40),
            body="#### [CCR-20261013-009] · senior_reviewer · high"))

        with self.assertRaisesRegex(contract.ContractError, "comment 40.*cannot be parsed"):
            self.sync(remote)
        self.assertEqual(watermark, self.store.watermark(REPO, REQUEST))
        self.assertEqual(2, self.count("cycle_history_comments"))

    def test_a_comment_without_an_author_is_never_read_as_absent(self) -> None:
        remote = FakeRemote(CLAIMED + [contract.ProviderComment(
            author=None, body="hi", id="50", updated_at=_time(50))])

        with self.assertRaisesRegex(contract.ContractError, "comment 50.*no author"):
            self.sync(remote)
        self.assertIsNone(self.store.watermark(REPO, REQUEST))

    def test_a_failed_freshness_check_propagates(self) -> None:
        remote = FakeRemote(CLAIMED)
        remote.failure = "gh exited 1"

        with self.assertRaises(rc.CycleDriverError):
            self.sync(remote)
        self.assertEqual([], remote.fetches)

    def test_a_remote_without_a_check_rebuilds_every_time(self) -> None:
        remote = rc.ReaderRemote(lambda *_: SURVIVED_ONCE, None)

        self.assertEqual("rebuilt", self.sync(remote).source)
        self.assertEqual("rebuilt", self.sync(remote).source)
        self.assertIsNone(self.store.watermark(REPO, REQUEST))


class PrivacyTests(StoreTestCase):
    def test_the_history_tables_hold_no_prose_titles_or_logins(self) -> None:
        remote = FakeRemote(SURVIVED_ONCE + [
            comment(40, None, author="reviewer", extra=_receipt("attempt-1")),
            comment(50, "review", 3, author="stranger")])
        self.store.expect_publication("attempt-1", REPO, REQUEST, "resolve")
        self.sync(remote)
        self.store.acquire(REPO, REQUEST)

        with database(self.path) as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND "
                "(name LIKE 'cycle_history_%' OR name='cycle_publications')")]
            values = [value for table in tables
                      for row in connection.execute(f"SELECT * FROM {table}")
                      for value in row if isinstance(value, str)]
        self.assertEqual(8, len(tables))
        self.assertTrue(values)
        for value in values:
            self.assertNotRegex(value, r"\s", value)
            for word in ("Secret", "Summary", "reviewer", "stranger", "sonnet", "luna"):
                self.assertNotIn(word, value)

    def test_the_schema_refuses_tokens_outside_the_closed_vocabulary(self) -> None:
        with database(self.path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO cycle_history_findings VALUES "
                    "('r', '1', '1', 0, 'REV-001', 'high', 'fixed', 'valid', 1)")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO cycle_publications VALUES "
                    "('a', 'r', '1', 'resolve', 'lost', NULL, NULL, 't', 't', 1)")


class OutboxTests(StoreTestCase):
    def test_a_publication_is_confirmed_by_its_receipt_once(self) -> None:
        remote = FakeRemote(CLAIMED)
        self.sync(remote)
        self.store.expect_publication("attempt-1", REPO, REQUEST, "resolve")
        self.store.expect_publication("attempt-1", REPO, REQUEST, "resolve")
        self.assertEqual(["attempt-1"], [item["attempt_id"] for item in
                                         self.store.pending_publications(REPO, REQUEST)])

        remote.items.append(comment(30, "triage", 2, "resolved",
                                    extra=_receipt("attempt-1")))
        synced = self.sync(remote)
        self.sync(remote)

        self.assertEqual((), synced.foreign)
        publication = self.store.publication("attempt-1")
        self.assertEqual(("published", "30"), (publication["state"], publication["comment_id"]))
        self.assertEqual(1, self.count("cycle_publications"))
        self.assertEqual([], self.store.pending_publications(REPO, REQUEST))

    def test_a_late_publication_is_found_by_the_next_sync(self) -> None:
        remote = FakeRemote(CLAIMED)
        self.sync(remote)
        self.store.expect_publication("attempt-1", REPO, REQUEST, "resolve")
        self.sync(remote)
        self.assertEqual("pending", self.store.publication("attempt-1")["state"])

        remote.items.append(comment(30, None, extra=_receipt("attempt-1")))
        self.sync(remote)

        self.assertEqual("published", self.store.publication("attempt-1")["state"])

    def test_a_later_publication_of_the_stage_supersedes_a_lost_one(self) -> None:
        remote = FakeRemote(CLAIMED)
        self.sync(remote)
        self.store.expect_publication("attempt-lost", REPO, REQUEST, "resolve")
        self.store.expect_publication("attempt-2", REPO, REQUEST, "resolve")
        remote.items.append(comment(30, None, extra=_receipt("attempt-2")))

        self.sync(remote)

        self.assertEqual("superseded", self.store.publication("attempt-lost")["state"])
        self.assertEqual([], self.store.pending_publications(REPO, REQUEST, "resolve"))

    def test_a_receipt_nobody_here_dispatched_is_foreign(self) -> None:
        remote = FakeRemote(CLAIMED)
        self.sync(remote)
        remote.items.append(comment(30, None, extra=_receipt("attempt-elsewhere")))
        remote.items.append(comment(31, None, extra=_receipt("attempt-other-cr", request="9")))

        self.assertEqual(("attempt-elsewhere",), self.sync(remote).foreign)

    def test_a_first_sync_has_nothing_to_call_foreign(self) -> None:
        remote = FakeRemote(CLAIMED + [comment(30, None, extra=_receipt("attempt-old"))])

        self.assertEqual((), self.sync(remote).foreign)


class LockTests(StoreTestCase):
    def test_a_live_writer_holds_the_change_request(self) -> None:
        owner = self.store.acquire(REPO, REQUEST)

        with self.assertRaisesRegex(ch.LockHeld, "already being driven"):
            self.store.acquire(REPO, REQUEST)
        self.store.acquire(REPO, "75")
        self.store.release(REPO, REQUEST, owner)
        self.store.acquire(REPO, REQUEST)

    def test_releasing_a_lock_someone_else_took_over_changes_nothing(self) -> None:
        owner = self.store.acquire(REPO, REQUEST)
        self.age_lock(hours=25)
        self.store.acquire(REPO, REQUEST)

        self.store.release(REPO, REQUEST, owner)

        with self.assertRaises(ch.LockHeld):
            self.store.acquire(REPO, REQUEST)

    def age_lock(self, hours: int) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with database(self.path) as connection:
            connection.execute("UPDATE cycle_history_locks SET acquired_at=?", (old,))

    def test_an_old_lock_is_stale_and_taken_over(self) -> None:
        self.store.acquire(REPO, REQUEST)
        self.age_lock(hours=25)

        self.store.acquire(REPO, REQUEST)

    def lock_by(self, pid: int) -> None:
        with database(self.path) as connection:
            connection.execute(
                "INSERT INTO cycle_history_locks VALUES (?, ?, 'lock-old', ?, ?)",
                (REPO, REQUEST, pid, datetime.now(timezone.utc).isoformat()))

    def test_a_lock_whose_process_is_gone_is_stale(self) -> None:
        # Runs on Windows too, where liveness goes through the Windows API.
        finished = subprocess.Popen([sys.executable, "-c", ""])
        finished.wait()
        self.lock_by(finished.pid)

        self.store.acquire(REPO, REQUEST)

    def test_a_lock_whose_process_still_runs_is_held(self) -> None:
        running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(running.wait)
        self.addCleanup(running.kill)
        self.lock_by(running.pid)

        with self.assertRaisesRegex(ch.LockHeld, "already being driven"):
            self.store.acquire(REPO, REQUEST)


class FakeKernel32:
    """The three Windows calls `_windows_process_alive` makes."""

    def __init__(self, handle: int, waited: int = ch._WAIT_TIMEOUT) -> None:
        self.handle, self.waited, self.closed = handle, waited, []

    def OpenProcess(self, access, inherit, pid):  # noqa: N802 - Windows API name
        return self.handle

    def WaitForSingleObject(self, handle, milliseconds):  # noqa: N802
        return self.waited

    def CloseHandle(self, handle):  # noqa: N802
        self.closed.append(handle)
        return True


class WindowsLivenessTests(unittest.TestCase):
    """A Windows lock is stale once its process is gone, not after 24 hours (REV-001)."""

    def alive(self, kernel32, error: int = 0):
        return ch._windows_process_alive(1234, kernel32, lambda: error)

    def test_an_exited_or_missing_process_is_dead(self) -> None:
        exited = FakeKernel32(7, ch._WAIT_OBJECT_0)
        self.assertIs(False, self.alive(exited))
        self.assertEqual([7], exited.closed)
        self.assertIs(False, self.alive(FakeKernel32(0), ch._ERROR_INVALID_PARAMETER))

    def test_a_running_or_foreign_process_is_alive(self) -> None:
        running = FakeKernel32(7, ch._WAIT_TIMEOUT)
        self.assertIs(True, self.alive(running))
        self.assertEqual([7], running.closed)
        self.assertIs(True, self.alive(FakeKernel32(0), ch._ERROR_ACCESS_DENIED))

    def test_an_unexpected_answer_leaves_only_the_age_rule(self) -> None:
        self.assertIsNone(self.alive(FakeKernel32(0), 6))
        self.assertIsNone(self.alive(FakeKernel32(7, 0xFFFFFFFF)))
        for pid in (-1, 0, 2 ** 32):
            self.assertIsNone(ch._windows_process_alive(pid, FakeKernel32(7), lambda: 0))

    def test_windows_asks_the_windows_api(self) -> None:
        with mock.patch.object(ch, "_windows_process_alive", return_value=False) as probe, \
                mock.patch.object(ch.os, "name", "nt"):
            alive = ch._process_alive(os.getpid() + 1)

        self.assertIs(False, alive)
        probe.assert_called_once_with(os.getpid() + 1)


class GitHubRemoteTests(unittest.TestCase):
    def test_the_freshness_check_is_one_graphql_call(self) -> None:
        response = json.dumps({"data": {"repository": {"pullRequest": {
            "headRefOid": HEAD, "comments": {"totalCount": 3, "nodes": [
                {"updatedAt": _time(30)}]}}}}})
        done = subprocess.CompletedProcess([], 0, stdout=response, stderr="")
        with mock.patch.object(rc.subprocess, "run", return_value=done) as run:
            mark = rc.probe_change_request(REPO, REQUEST, None)

        self.assertEqual(ch.RemoteMark(HEAD, 3, _time(30)), mark)
        (call,) = run.call_args_list
        command = call.args[0]
        self.assertEqual(["gh", "api", "graphql"], command[:3])
        self.assertIn("orderBy:{field:UPDATED_AT", " ".join(command))
        self.assertIn("number=74", command)

    def test_a_failed_or_unexpected_check_raises(self) -> None:
        for stdout, code in (("", 1), ("not json", 0),
                             ('{"data": {"repository": {"pullRequest": null}}}', 0)):
            with self.subTest(stdout=stdout):
                done = subprocess.CompletedProcess([], code, stdout=stdout, stderr="")
                with mock.patch.object(rc.subprocess, "run", return_value=done):
                    with self.assertRaisesRegex(rc.CycleDriverError, "freshness check"):
                        rc.probe_change_request(REPO, REQUEST, None)

    def test_a_reconciliation_reads_only_comments_edited_since(self) -> None:
        pages = json.dumps([[{"id": 11, "user": {"login": "reviewer"}, "body": "one",
                              "updated_at": _time(11)}]])
        done = subprocess.CompletedProcess([], 0, stdout=pages, stderr="")
        with mock.patch.object(rc.subprocess, "run", return_value=done) as run:
            (only,) = rc.read_change_request_comments(REPO, REQUEST, None, since=_time(10))

        self.assertEqual(_time(11), only.updated_at)
        self.assertEqual(f"repos/{REPO}/issues/{REQUEST}/comments?since="
                         "2026-10-13T10%3A00%3A10Z", run.call_args.args[0][-1])


class ResumedCycleTests(RunCycleTestCase):
    """The driver uses the local history only when the remote check verifies it."""

    def resume(self, remote, *, resolutions=(), reviews=(), coder=None, **kw):
        self.implementer = coder or Scripted("codex", resolutions=list(resolutions))
        self.reviewer = Scripted("claude", reviews=list(reviews))
        return self.run_cycle(
            self.implementer, self.reviewer, start_from="resolve",
            change_request_id=REQUEST, history_remote=remote,
            trusted_authors=("reviewer",), **kw)

    def resolve_prompts(self) -> list[str]:
        return [task for task in self.implementer.dispatched if "cc-resolve-comments" in task]

    def history(self) -> ch.CycleHistoryStore:
        return ch.CycleHistoryStore(self.store.path)

    def test_a_second_resume_reads_the_verified_local_history(self) -> None:
        remote = FakeRemote(SURVIVED_ONCE)
        approved = [rereview("APPROVED", "b" * 40, REV_001="resolved")]
        self.resume(remote, resolutions=[claimed()], reviews=approved)
        self.assertEqual([None], remote.fetches)

        report = self.resume(remote, resolutions=[claimed()], reviews=approved)

        # The seeded survival still contests the finding, with no fetch at all.
        self.assertEqual([None], remote.fetches)
        (prompt,) = self.resolve_prompts()
        self.assertIn("survived a claimed fix", prompt)
        self.assertEqual(rc.APPROVED_END, report.status)

    def test_a_failed_freshness_check_stops_before_any_dispatch(self) -> None:
        remote = FakeRemote(CLAIMED)
        remote.failure = "gh exited 1"

        report = self.resume(remote)

        self.assertEqual("BLOCKED", report.status)
        self.assertIn("unreadable", report.stopped_because)
        self.assertIn("gh exited 1", report.stopped_because)
        self.assertEqual([], self.implementer.dispatched)

    def test_a_live_writer_on_this_machine_blocks_the_resume(self) -> None:
        self.history().acquire(REPO, REQUEST)

        report = self.resume(FakeRemote(CLAIMED))

        self.assertEqual("BLOCKED", report.status)
        self.assertIn("already being driven", report.stopped_because)
        self.assertEqual([], self.implementer.dispatched)

    def test_the_lock_is_released_when_the_cycle_ends(self) -> None:
        self.resume(FakeRemote(CLAIMED), resolutions=[claimed()], reviews=[APPROVED])

        self.history().acquire(REPO, REQUEST)

    def test_another_writers_publication_during_a_stage_blocks(self) -> None:
        remote = FakeRemote(CLAIMED)

        class Racing(Scripted):
            def spoken(self, task):
                remote.items.append(comment(30, None, extra=_receipt("attempt-elsewhere")))
                return super().spoken(task)

        report = self.resume(remote, coder=Racing("codex", resolutions=[claimed()]),
                             reviews=[APPROVED])

        self.assertEqual("BLOCKED", report.status)
        self.assertIn("attempt-elsewhere", report.stopped_because)
        self.assertEqual([], self.reviewer.dispatched)
        self.assertIn("30", [row["comment_id"] for row in self.comment_rows()])

    def comment_rows(self):
        with database(self.store.path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(
                "SELECT * FROM cycle_history_comments")]

    def test_a_stage_publication_is_confirmed_by_its_receipt(self) -> None:
        remote = FakeRemote(CLAIMED)
        attempts = []

        class Publishing(Scripted):
            def spoken(self, task):
                attempt = re.search(r"Runtime attempt_id: ([^. ]+)\.", task).group(1)
                attempts.append(attempt)
                remote.items.append(comment(30, "triage", 2, "resolved",
                                            extra=_receipt(attempt)))
                return super().spoken(task)

        report = self.resume(remote, coder=Publishing("codex", resolutions=[claimed()]),
                             reviews=[APPROVED])

        self.assertEqual(rc.APPROVED_END, report.status)
        (attempt,) = attempts
        self.assertEqual("published", self.history().publication(attempt)["state"])
        resolve = report.stage_details[0]
        self.assertFalse([w for w in resolve.warnings if "not observed" in w])

    def test_an_unobserved_publication_is_flagged_and_retried(self) -> None:
        remote = FakeRemote(CLAIMED)
        report = self.resume(remote, resolutions=[claimed()], reviews=[APPROVED])
        (lost,) = [item["attempt_id"] for item in
                   self.history().pending_publications(REPO, REQUEST, "resolve")]
        self.assertTrue([w for w in report.stage_details[0].warnings
                         if lost in w and "not observed" in w])

        self.resume(remote, resolutions=[claimed()], reviews=[APPROVED])

        (prompt,) = self.resolve_prompts()
        self.assertIn(lost, prompt)
        self.assertIn("instead of posting a new one", prompt)

    def test_an_unusable_database_falls_back_to_the_comments(self) -> None:
        remote = FakeRemote(SURVIVED_ONCE)
        with mock.patch.object(ch.CycleHistoryStore, "sync",
                               side_effect=sqlite3.DatabaseError("malformed")):
            report = self.resume(remote, resolutions=[claimed()],
                                 reviews=[rereview("APPROVED", "b" * 40, REV_001="resolved")])

        (prompt,) = self.resolve_prompts()
        self.assertIn("survived a claimed fix", prompt)
        self.assertEqual(rc.APPROVED_END, report.status)
        # The lock taken before the database failed is still released.
        self.history().acquire(REPO, REQUEST)


if __name__ == "__main__":
    unittest.main()
