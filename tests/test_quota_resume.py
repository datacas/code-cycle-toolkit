"""Acceptance regressions for #389, including the real Git/CLI boundary."""
from __future__ import annotations

import sys
import unittest
import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from telemetry import Telemetry
from test_cycle import CycleTestCase, ScriptedAdapter
import cycle as cy
import executors as ex
import router
import quota_state as qs
from cycle_journal import Journal
from run_cycle import validate_start, check_change_request, CycleDriverError


class QuotaResumeTests(CycleTestCase):
    def test_from_resume_skips_recorded_primary(self):
        self.store.record_quota_exhaustion("owner/repo", "codex", "gpt-6-luna")
        primary, fallback = ScriptedAdapter("codex"), ScriptedAdapter("claude")
        recorder = self.recorder([primary, fallback], started_from="resolve",
                                 change_request_id="384")
        outcome = recorder.stage("resolve", "resume")
        self.assertTrue(outcome.succeeded)
        self.assertEqual([], primary.dispatched)
        self.assertEqual(1, len(fallback.dispatched))
        self.assertIn("quota exhausted", outcome.decision.explain())

    def test_resolve_escalation_cannot_restore_exhausted_target(self):
        self.store.record_quota_exhaustion("owner/repo", "codex", "gpt-6-luna")
        primary, fallback = ScriptedAdapter("codex"), ScriptedAdapter("claude")
        recorder = self.recorder([primary, fallback], started_from="resolve", change_request_id="384")
        outcome = recorder.stage("resolve", "resume", target_override=router.Target(
            "codex", "openai", "gpt-6-luna", "high"))
        self.assertTrue(outcome.succeeded)
        self.assertFalse(primary.dispatched)
        self.assertTrue(fallback.dispatched)


class ResetTests(unittest.TestCase):
    def test_ordinal_date_and_timezone_are_not_dropped(self):
        now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        expiry, reported = Telemetry.quota_expiry(
            "Usage limit; resets Nov 4th, 2026 3:20 PM UTC+01:00", now)
        self.assertTrue(reported)
        self.assertEqual("2026-11-04T14:20:00+00:00", expiry)

    def test_complete_dates_past_future_and_offsets(self):
        now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        for message, expected in [
            ("resets Nov 1st, 2026 3:20 PM UTC", "2026-11-01T15:20:00+00:00"),
            ("resets November 4th 2026 15:20 UTC", "2026-11-04T15:20:00+00:00"),
            ("resets 2026-11-04 15:20 -05:00", "2026-11-04T20:20:00+00:00"),
            ("resets 2026-11-04T15:20:30Z", "2026-11-04T15:20:30+00:00"),
            ("resets Nov 4th, 2026 3:20 PM", "2026-11-04T14:20:00+00:00"),
        ]:
            with self.subTest(message=message), patch("quota_state.local_zone", return_value=timezone(timedelta(hours=1))):
                self.assertEqual((expected, True), Telemetry.quota_expiry(message, now))

    def test_ambiguity_never_becomes_an_earlier_clock_only_reset(self):
        for message in ("resets Nov 40th, 2026 3:20 PM", "resets Nov 4 3:20 PM",
                        "resets on a later date", "resets Nov 4th, 2026 3:20 PM CST",
                        "resets 2026-14-04 15:20 UTC", "resets Nov 4 2026 3:20 PM UTC+unknown"):
            with self.subTest(message=message):
                self.assertEqual((None, True), Telemetry.quota_expiry(message))

    def test_dst_repeated_hour_chooses_latest_interpretation(self):
        try:
            ZoneInfo("Europe/Madrid")
        except ZoneInfoNotFoundError:
            self.skipTest("IANA timezone database is unavailable")
        with patch.dict(os.environ, {"TZ": "Europe/Madrid"}):
            expiry, _ = Telemetry.quota_expiry("resets Oct 25th, 2026 2:30 AM")
        self.assertEqual("2026-10-25T01:30:00+00:00", expiry)

    def test_missing_reset_uses_only_minimum_window(self):
        now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        self.assertEqual(("2026-11-02T12:30:00+00:00", False),
                         Telemetry.quota_expiry("quota exceeded", now))

    def test_unknown_local_zone_does_not_guess_a_dated_reset(self):
        with patch("quota_state.local_zone", return_value=None):
            self.assertEqual((None, True), Telemetry.quota_expiry("resets Nov 4th, 2026 3:20 PM"))


class GitHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "checkout"
        self.cwd.mkdir()
        self.git("init", "-b", "task-330")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        for name in ("one.py", "two.py"):
            (self.cwd / name).write_bytes(b"initial\n")
        self.git("add", ".")
        self.git("commit", "-m", "fixture")
        self.head = self.git("rev-parse", "HEAD")
        self.store = Telemetry(self.root / "telemetry.sqlite")
        self.snapshots = patch("cycle.effect_snapshot", side_effect=self.snapshot)
        self.snapshots.start()
        self.addCleanup(self.snapshots.stop)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.cwd, capture_output=True,
                              text=True, check=True).stdout.strip()

    def snapshot(self, *args, **kwargs):
        return {"dirty": bool(self.git("status", "--porcelain", "--untracked-files=all")),
                "workspace": ex._workspace_fingerprint(str(self.cwd)), "remote": "same",
                "pulls": [], "observed_pulls": {}, "comments": []}

    def recorder(self, adapters, **kw):
        recorder = cy.CycleRecorder(
            self.store, "owner/repo", "330", router.TaskSignals(difficulty=3),
            availability={a.name: ex.Availability.READY for a in adapters},
            registry=ex.Registry(adapters), started_from="resolve", change_request_id="384", **kw)
        recorder.journal = Journal(self.store.path, "owner/repo", "330", recorder.cycle_id,
                                   change_request_id="384")
        return recorder

    def edit_three_files(self):
        (self.cwd / "one.py").write_bytes(b"staged\x00bytes\n")
        self.git("add", "one.py")
        (self.cwd / "one.py").write_bytes(b"unstaged after staged\xff\n")
        (self.cwd / "two.py").write_bytes(b"second change\n")
        (self.cwd / "three.py").write_bytes(b"untracked\x00\xff\n")

    def exhaust(self, message="quota exceeded"):
        fixture = self
        class Exhausted(ScriptedAdapter):
            def dispatch(self, target, task, **kw):
                fixture.edit_three_files()
                return ex.DispatchResult(ex.DispatchOutcome.FAILED, self.name, target,
                                         error_code="quota", start_state="started",
                                         process_exited=True, detail=message)
        primary, fallback = Exhausted("codex"), ScriptedAdapter("claude")
        outcome = self.recorder([primary, fallback]).stage("resolve", "work", cwd=str(self.cwd))
        self.assertFalse(outcome.succeeded)
        self.assertEqual([], fallback.dispatched)
        assert outcome.result is not None
        self.assertIn("dirty_workspace", outcome.result.detail)
        return outcome

    def rows(self, table):
        with sqlite3.connect(self.store.path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]

    def resume(self, **kw):
        primary, fallback = ScriptedAdapter("codex"), ScriptedAdapter("claude")
        outcome = self.recorder([primary, fallback]).stage("resolve", "resume", cwd=str(self.cwd), **kw)
        return outcome, primary, fallback

    def test_dirty_fallback_receives_exact_bytes_and_head_with_audit(self):
        stopped = self.exhaust()
        before = ex._workspace_fingerprint(str(self.cwd))
        assert before is not None
        outcome, primary, fallback = self.resume()
        self.assertTrue(outcome.succeeded)
        self.assertFalse(primary.dispatched)
        self.assertEqual(1, len(fallback.dispatched))
        self.assertIn("inspect the existing", fallback.dispatched[0])
        self.assertEqual(before, ex._workspace_fingerprint(str(self.cwd)))
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))
        row = self.rows("quota_handoffs")[0]
        self.assertEqual(("resolve", "384", "codex", "claude", before["fingerprint"]),
                         (row["stage"], row["pr"], row["original_executor"],
                          row["destination_executor"], row["fingerprint"]))
        self.assertIsNotNone(row["used_at"])
        # Stop advice is accepted by the real CLI parser and start validator.
        assert stopped.result is not None
        self.assertIn("--from resolve --pr 384", stopped.result.detail)
        validate_start("resolve", "384")

    def test_altered_staged_unstaged_or_untracked_diff_is_refused(self):
        for kind in ("staged", "unstaged", "untracked"):
            with self.subTest(kind=kind):
                # Each mutation is reset only inside this disposable fixture.
                if self.git("status", "--porcelain"):
                    self.git("reset", "--hard", self.head)
                    self.git("clean", "-fd")
                with sqlite3.connect(self.store.path) as connection:
                    connection.execute("DELETE FROM quota_exhaustions")
                self.exhaust()
                path = self.cwd / ("three.py" if kind == "untracked" else "two.py")
                path.write_bytes(b"operator changed it\n")
                if kind == "staged":
                    self.git("add", "two.py")
                before = ex._workspace_fingerprint(str(self.cwd))
                outcome, primary, fallback = self.resume()
                self.assertTrue(outcome.decision.blocked)
                self.assertIn("fingerprint mismatch", outcome.decision.explain())
                self.assertFalse(primary.dispatched or fallback.dispatched)
                self.assertEqual(before, ex._workspace_fingerprint(str(self.cwd)))

    def test_unrelated_dirty_resume_has_no_ownership(self):
        self.edit_three_files()
        self.store.record_quota_exhaustion("owner/repo", "codex", "gpt-6-luna")
        outcome, primary, fallback = self.resume()
        self.assertIn("no quota worktree provenance", outcome.decision.explain())
        self.assertFalse(primary.dispatched or fallback.dispatched)

    def test_expired_window_returns_to_original_executor_keeps_provenance(self):
        self.exhaust()
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("UPDATE quota_exhaustions SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual([], self.store.active_quota_exhaustions("owner/repo"))
        self.assertEqual(1, len(self.rows("quota_handoffs")))
        before = ex._workspace_fingerprint(str(self.cwd))
        outcome, primary, fallback = self.resume()
        self.assertTrue(outcome.succeeded)
        self.assertEqual(1, len(primary.dispatched))
        self.assertFalse(fallback.dispatched)
        self.assertEqual(before, ex._workspace_fingerprint(str(self.cwd)))
        self.assertEqual(1, len(self.rows("quota_history")))

    def test_repeated_exhaustion_after_expiry_records_new_checkpoint(self):
        self.exhaust()
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("UPDATE quota_exhaustions SET expires_at='2000-01-01T00:00:00+00:00'")
        self.exhaust()
        self.assertEqual(2, len(self.rows("quota_history")))
        self.assertEqual(2, len(self.rows("quota_handoffs")))
        outcome, primary, fallback = self.resume()
        self.assertTrue(outcome.succeeded)
        self.assertFalse(primary.dispatched)
        self.assertTrue(fallback.dispatched)
        self.assertTrue(all(row["used_at"] for row in self.rows("quota_handoffs")))

    def test_wrong_stage_pr_and_original_model_cannot_match(self):
        self.exhaust()
        ledger = qs.Handoffs(self.store.path)
        values = dict(repo="owner/repo", task="330", stage="resolve", pr="384",
                      workspace=ex._workspace_fingerprint(str(self.cwd)),
                      destination=router.Target("codex", "openai", "gpt-6-luna", "high"))
        for override in ({"stage": "rereview"}, {"pr": "999"}, {"task": "999"},
                         {"destination": router.Target("codex", "openai", "gpt-6.1-sol", "high")}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                ledger.match(**{**values, **override})

    def test_ordinary_dirty_executor_change_is_refused(self):
        self.edit_three_files()
        outcome, primary, fallback = self.resume()
        self.assertTrue(outcome.decision.blocked)
        self.assertFalse(primary.dispatched or fallback.dispatched)

    def test_head_mismatch_is_still_refused(self):
        self.exhaust()
        self.git("add", ".")
        self.git("commit", "-m", "forbidden WIP fixture")
        (self.cwd / "two.py").write_bytes(b"dirty again\n")
        outcome, primary, fallback = self.resume()
        self.assertIn("HEAD mismatch", outcome.decision.explain())
        self.assertFalse(primary.dispatched or fallback.dispatched)

    def test_ambiguous_reset_keeps_redacted_message_out_of_telemetry(self):
        stopped = self.exhaust("quota exceeded; resets Nov 40th, 2026 3:20 PM; token=ghp_123456789012345678901234567890123456")
        assert stopped.result is not None
        self.assertNotIn("ghp_123456789012345678901234567890123456", stopped.result.detail)
        self.assertIsNone(self.rows("quota_history")[0]["expires_at"])
        self.assertEqual("reset_time_ambiguous", self.rows("quota_history")[0]["reason"])
        journal = json.dumps(self.rows("journal_events"))
        self.assertIn("Nov 40th", journal)
        self.assertNotIn("ghp_123456789012345678901234567890123456", journal)
        self.assertNotIn("Nov 40th", json.dumps(self.rows("quota_exhaustions")))
        outcome, primary, fallback = self.resume()
        self.assertTrue(outcome.succeeded)
        self.assertFalse(primary.dispatched)

    def test_operator_retry_names_record_and_preserves_history(self):
        self.store.record_quota_exhaustion("owner/repo", "codex", "gpt-6-luna", "reset later")
        record_id = self.rows("quota_history")[0]["id"]
        args = ["--database", str(self.store.path), "--repo", "owner/repo", "--task", "330",
                "retry", "--record", record_id, "--executor", "codex"]
        with patch("quota_state.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "operator-login\n")):
            self.assertEqual(0, qs.main(args))
        self.assertEqual([], self.store.active_quota_exhaustions("owner/repo"))
        self.assertIsNotNone(self.rows("quota_history")[0]["retry_at"])
        self.assertEqual(1, len(self.rows("quota_exhaustions")))
        with patch("quota_state.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "operator-login\n")):
            with self.assertRaises(SystemExit) as error:
                qs.main(args)
        self.assertEqual(2, error.exception.code)

    def test_legacy_attestation_requires_exact_confirmation_and_is_single_use(self):
        self.edit_three_files()
        self.store.record_quota_exhaustion("owner/repo", "codex", "gpt-6-luna")
        # Model a pre-#389 record, which has no new exhaustion history.
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("DELETE FROM quota_history")
        workspace = ex._workspace_fingerprint(str(self.cwd))
        assert workspace is not None
        fingerprint = workspace["fingerprint"]
        args = ["--database", str(self.store.path), "--repo", "owner/repo", "--task", "330",
                "attest", "--cwd", str(self.cwd), "--pr", "384", "--stage", "resolve",
                "--original", "codex", "--model", "gpt-6-luna", "--destination", "claude",
                "--head", self.head, "--confirm-fingerprint"]
        with self.assertRaises(SystemExit):
            qs.main(args + ["0" * 64])
        self.assertEqual([], self.rows("quota_handoffs"))
        run = subprocess.run
        def operator_run(command, **kwargs):
            if command[:3] == ["gh", "api", "user"]:
                return subprocess.CompletedProcess(command, 0, "operator-login\n")
            return run(command, **kwargs)
        with patch("quota_state.subprocess.run", side_effect=operator_run):
            self.assertEqual(0, qs.main(args + [fingerprint]))
        row = self.rows("quota_handoffs")[0]
        self.assertEqual(("operator_attested", "operator-login"), (row["provenance"], row["operator"]))
        outcome, primary, fallback = self.resume()
        self.assertTrue(outcome.succeeded)
        self.assertFalse(primary.dispatched)
        self.assertIn("operator_attested", fallback.dispatched[0])
        outcome, primary, fallback = self.resume()
        self.assertTrue(outcome.decision.blocked)

    def test_cli_help_accepts_operator_commands(self):
        for command in ("retry", "inspect", "attest"):
            done = subprocess.run([sys.executable, str(Path(qs.__file__)),
                                   "--database", str(self.store.path), "--repo", "owner/repo",
                                   "--task", "330", command, "--help"], capture_output=True, text=True)
            self.assertEqual(0, done.returncode, done.stderr)

    def test_new_unattested_work_cannot_use_legacy_attestation(self):
        self.edit_three_files()
        self.store.record_quota_exhaustion("owner/repo", "codex", "gpt-6-luna")
        workspace = ex._workspace_fingerprint(str(self.cwd))
        assert workspace is not None
        fingerprint = workspace["fingerprint"]
        args = ["--database", str(self.store.path), "--repo", "owner/repo", "--task", "330",
                "attest", "--cwd", str(self.cwd), "--pr", "384", "--stage", "resolve",
                "--original", "codex", "--model", "gpt-6-luna", "--destination", "claude",
                "--head", self.head, "--confirm-fingerprint", fingerprint]
        run = subprocess.run
        def operator_run(command, **kwargs):
            if command[:3] == ["gh", "api", "user"]:
                return subprocess.CompletedProcess(command, 0, "operator-login\n")
            return run(command, **kwargs)
        with patch("quota_state.subprocess.run", side_effect=operator_run), self.assertRaises(SystemExit) as error:
            qs.main(args)
        self.assertEqual(2, error.exception.code)
        self.assertEqual([], self.rows("quota_handoffs"))

    def test_stop_advice_is_accepted_at_the_real_cli_boundary(self):
        self.exhaust()
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_cycle.py"
        done = subprocess.run([sys.executable, str(script), "--repo", "owner/repo",
                               "--task", "330", "--from", "resolve", "--pr", "384",
                               "--cwd", str(self.cwd), "--dry-run"],
                              cwd=script.parents[1], capture_output=True, text=True)
        self.assertEqual(0, done.returncode, done.stderr)

    def test_existing_pr_head_guard_remains_effective(self):
        def run(command, **kw):
            if command[0] == "gh":
                body = {"state": "OPEN", "headRefName": "task-330", "headRefOid": "f" * 40}
                return subprocess.CompletedProcess(command, 0, json.dumps(body))
            return subprocess.run(command, **kw)
        with self.assertRaisesRegex(CycleDriverError, "not at"):
            check_change_request("owner/repo", "384", str(self.cwd), run=run)
