"""Failure evidence crosses the process boundary; prose never enters the store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import cycle
import cycle_status
import executors as ex
import harness
import router
import run_cycle
import telemetry as tm
import test_run_cycle

TARGET = router.parse_target("codex:openai/gpt-6-luna high")


def stream(*events):
    return "\n".join(json.dumps(event) for event in events)


def failed(code=None, message="failure", **fields):
    return {"type": "turn.failed", "error": {"message": message, "codex_error_info": code}, **fields}


class ExecutorFailureTests(unittest.TestCase):
    def dispatch(self, stdout, stderr="Reading additional input from stdin...", returncode=1):
        with patch.object(ex.CodexAdapter, "_session_events", return_value=[]):
            return ex.CodexAdapter().dispatch(
                TARGET, "work", cwd=tempfile.gettempdir(),
                runner=lambda *a, **k: subprocess.CompletedProcess([], returncode, stdout, stderr),
            )

    def test_structured_code_precedes_stderr_and_exit_status(self):
        for raw, message, expected in (
            ("server_overloaded", "Selected model is at capacity", "capacity"),
            ("usage_limit_exceeded", "usage exhausted", "quota"),
            ("other", "401 Unauthorized", "auth"),
            ("stream_disconnected", "disconnected", "transport"),
            ("future_code", "quota exceeded at capacity", "executor_error"),
        ):
            with self.subTest(raw=raw):
                result = self.dispatch(stream(failed(raw, message)), "please sign in", 0)
                self.assertEqual(expected, result.failure_code)
                self.assertNotEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
                self.assertIn(message, result.detail)
        self.assertIn("codex_error_info=future_code", result.failure_summary())

    def test_start_evidence_and_positive_no_activity(self):
        cases = (
            ([failed("server_overloaded", started=False)], "not_started"),
            ([{"type": "thread.started", "thread_id": "T"}, failed("server_overloaded")], "started"),
            ([{"type": "item.completed", "item": {"type": "command_execution"}}, failed()], "started"),
            ([failed()], "unknown"),
            ([{"type": "item.completed", "item": {"type": "command_execution"}},
              failed(started=False)], "started"),
        )
        for events, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, self.dispatch(stream(*events)).start_state)

    def test_timeout_keeps_partial_start_evidence(self):
        for output, state in ((None, "unknown"),
                              (stream({"type": "thread.started", "thread_id": "T"}), "started")):
            def timeout(*a, **k):
                raise subprocess.TimeoutExpired("codex", 1, output=output)
            result = ex.CodexAdapter().dispatch(TARGET, "work", runner=timeout, cwd=tempfile.gettempdir())
            self.assertEqual(("timeout", state), (result.failure_code, result.start_state))

    def test_no_error_report_is_not_guessed_from_agent_prose(self):
        output = stream({"type": "item.completed", "item": {
            "type": "agent_message", "text": "quota exceeded and 401 Unauthorized"}})
        self.assertEqual("no_error_report", self.dispatch(output).failure_code)
        self.assertEqual("no_error_report", self.dispatch("").failure_code)

    def test_stderr_fallback_reads_past_stdin_banner(self):
        result = self.dispatch("", "Reading additional input from stdin...\n401 Unauthorized")
        self.assertEqual("auth", result.failure_code)
        self.assertEqual("401 Unauthorized", result.detail)

    def test_terminal_success_clears_a_recovered_stream_error(self):
        output = stream({"type": "error", "message": "stream disconnected; reconnecting"},
                        {"type": "turn.completed"})
        result = self.dispatch(output, "", 0)
        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertIsNone(result.failure_code)

    def test_claude_stream_error_and_activity(self):
        output = stream({"type": "system", "subtype": "init", "session_id": "C"},
                        {"type": "result", "is_error": True, "errors": ["401 Unauthorized"]})
        result = ex.ClaudeAdapter().dispatch(TARGET, "work", cwd=tempfile.gettempdir(),
            runner=lambda *a, **k: subprocess.CompletedProcess([], 0, output, ""))
        self.assertEqual(("auth", "started"), (result.failure_code, result.start_state))

    def test_session_log_fallback_is_correlated_and_does_not_read_another_run(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CODEX_HOME": directory}):
            root = Path(directory) / "sessions" / "2026"
            root.mkdir(parents=True)
            path = root / "rollout-thread-172.jsonl"
            path.write_text(stream({"type": "session_meta", "payload": {"id": "thread-172"}},
                {"type": "event_msg", "payload": {"type": "task_complete", "error": {
                    "codex_error_info": "server_overloaded", "message": "Selected model is at capacity"}}}))
            adapter = ex.CodexAdapter()
            def dispatch(thread):
                return adapter.dispatch(TARGET, "work", cwd=directory,
                    runner=lambda *a, **k: subprocess.CompletedProcess([], 1,
                        stream({"type": "thread.started", "thread_id": thread}), ""))
            self.assertEqual("capacity", dispatch("thread-172").failure_code)
            self.assertEqual("no_error_report", dispatch("thread-unrelated").failure_code)

    def test_capability_mapping_is_closed_and_separate_from_availability(self):
        expected = {
            "operating_quota": "quota", "operating_availability": "unavailable",
            **{key: "unavailable" for key in ex.HUMAN_ACTION_CAPABILITIES},
            **{key: "precondition" for key in (
                "orchestration_context", "review_workspace_isolation", "review_workspace_mismatch",
                "review_workspace_conflict", "provider_agent_mapping", "publication_access",
                "proven_readiness", "read_only_enforcement", "disposable_workspace", "workspace_policy")},
            "read_only_verification": "contract_violation",
        }
        self.assertEqual(expected, ex.CAPABILITY_ERROR_CODES)
        for capability, code in expected.items():
            state = "started" if capability == "read_only_verification" else "not_started"
            result = ex.DispatchResult(ex.DispatchOutcome.BLOCKED, "codex", TARGET,
                                       missing_capability=capability, start_state=state)
            self.assertEqual((code, state), (result.failure_code, result.start_state))
        with self.assertRaisesRegex(ex.ExecutorError, "unmapped missing_capability"):
            ex.DispatchResult(ex.DispatchOutcome.BLOCKED, "codex", TARGET,
                              missing_capability="future_capability").failure_code

    def test_contract_and_operator_failures(self):
        for outcome, code in ((ex.DispatchOutcome.CONTRACT_VIOLATION, "contract_violation"),
                              (ex.DispatchOutcome.INTERRUPTED, "interrupted")):
            self.assertEqual(code, ex.DispatchResult(outcome, "codex", TARGET).failure_code)

    def test_redaction_happens_before_bounding_and_ignores_terminal_codes(self):
        text = "x" * 395 + " sk-\x1b[31msecret123 ghp_secret456 Bearer abc.def"
        excerpt = ex.failure_excerpt(text)
        self.assertLessEqual(len(excerpt), 400)
        self.assertNotIn("sk-", excerpt)
        self.assertNotIn("secret", excerpt)
        self.assertIn("[redacted]", ex.failure_excerpt(text, 500))

    def test_workspace_change_is_start_evidence_without_a_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", directory], capture_output=True, check=True)
            subprocess.run(["git", "-C", directory, "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "fixture"],
                           capture_output=True, check=True)
            def runner(*a, **k):
                (Path(directory) / "agent-change.txt").write_text("changed")
                return subprocess.CompletedProcess([], 1, "", "")
            result = ex.CodexAdapter().dispatch(TARGET, "work", runner=runner, cwd=directory)
            self.assertEqual("started", result.start_state)

    def test_orca_unreadable_receipt_and_async_start_evidence(self):
        context = ex.OrcaDispatchContext(coordinator="C", run_id="R", task_id="T")
        target = router.parse_target("orca:openai/gpt-6-luna high")
        for output, code, state in (("not-json", "unreadable_result", "unknown"),
                                    ("[]", "unreadable_result", "unknown"),
                                    (json.dumps({"ok": True, "result": {"state": "ready", "dispatchId": "D"}}),
                                     None, "started")):
            result = ex.OrcaAdapter().dispatch(target, "work", writes=True, context=context,
                runner=lambda *a, **k: subprocess.CompletedProcess([], 0, output, ""))
            self.assertEqual((code, state), (result.failure_code, result.start_state))

    def test_fake_binary_at_real_process_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake_executor.py"
            fake.write_text("import sys\nprint(" + repr(stream(failed("server_overloaded", started=False)))
                            + ")\nsys.exit(1)\n")
            class FakeBinary(ex.CodexAdapter):
                def argv(self, *a, **k):
                    return [sys.executable, str(fake)]
            result = FakeBinary().dispatch(TARGET, "work", cwd=directory)
            self.assertEqual(("capacity", "not_started"), (result.failure_code, result.start_state))


class StoreFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "telemetry.sqlite"
        self.store = tm.Telemetry(self.path)

    def attempt(self, store=None):
        return (store or self.store).create_dispatch_attempt(
            "repo", "task-172", "cycle-172", 1, "implement", executor="codex", provider="openai",
            model_requested="gpt-6-luna", effort="high", profile="deep_coder",
            harness_snapshot=harness.build_harness_snapshot(
                role="implement", skill="cc-implement-issue", profile="deep_coder",
                routing_strategy="fixed", readiness_policy="attempt", executor="codex",
                probe=None, profiles=None, root=ROOT))

    def test_every_error_token_is_accepted_and_old_tokens_rejected(self):
        expected = {"capacity", "quota", "auth", "transport", "timeout", "unreadable_result",
                    "unavailable", "contract_violation", "interrupted", "precondition",
                    "executor_error", "no_error_report"}
        self.assertEqual(expected, tm.FIELD_SPECS["attempt_error_code"][1])
        for code in expected:
            attempt = self.attempt()
            self.store.update_dispatch_attempt(attempt, {"error_code": code}, update_id="update-" + code)
            self.assertEqual(code, self.store.attempt(attempt)["error_code"])
        for code in ("dispatch_failed", "operating_quota", "operating_availability", "unknown"):
            with self.assertRaises(tm.TelemetryError):
                self.store.update_dispatch_attempt(attempt, {"error_code": code}, update_id="update-old-" + code)

    def test_start_state_refines_monotonically_even_with_corrections(self):
        attempt = self.attempt()
        self.assertEqual("unknown", self.store.attempt(attempt)["start_state"])
        for index, state in enumerate(("not_started", "started", "not_started", "unknown")):
            result = self.store.update_dispatch_attempt(attempt, {"start_state": state},
                update_id=f"update-{index}", correction_reason="operator_correction")
            self.assertEqual([] if index < 2 else ["start_state"], result["conflicts"])
        self.assertEqual("started", self.store.attempt(attempt)["start_state"])
        other = self.attempt()
        result = self.store.update_dispatch_attempt(other, {"start_state": "started"}, update_id="update-direct")
        self.assertEqual([], result["conflicts"])

    def test_schema_14_migration_preserves_audit_bytes_and_is_idempotent(self):
        old_column = ("    start_state             TEXT NOT NULL DEFAULT 'unknown'\n"
                      "                            CHECK(start_state IN ('started', 'not_started', 'unknown')),\n")
        old_path = self.path.parent / "old.sqlite"
        with sqlite3.connect(old_path) as conn:
            conn.executescript(tm.SCHEMA.replace(old_column, ""))
        with patch.object(tm.Telemetry, "_migrate_dispatch_attempts", lambda *a: None):
            old = tm.Telemetry(old_path)
            ids = [self.attempt(old) for _ in range(6)]
        codes = ("operating_quota", "operating_availability", "dispatch_failed", "executor_error", "unknown", "timeout")
        with sqlite3.connect(old_path) as conn:
            for index, (attempt, code) in enumerate(zip(ids, codes)):
                conn.execute("UPDATE dispatch_attempts SET error_code=? WHERE attempt_id=?", (code, attempt))
                conn.execute("INSERT INTO dispatch_attempt_updates "
                    "(update_id, attempt_id, source, observed_at, patch_hash, patch_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (f"old-{index}", attempt, "runtime", "2026-10-01T00:00:00Z", "historical-hash", json.dumps({"error_code": code})))
            before = conn.execute("SELECT * FROM dispatch_attempt_updates").fetchall()
        migrated = tm.Telemetry(old_path)
        self.assertEqual(["quota", "unavailable", "executor_error", "executor_error", "executor_error", "timeout"],
                         [migrated.attempt(attempt)["error_code"] for attempt in ids])
        self.assertEqual(["unknown"] * 6, [migrated.attempt(attempt)["start_state"] for attempt in ids])
        with sqlite3.connect(old_path) as conn:
            self.assertEqual(before, conn.execute("SELECT * FROM dispatch_attempt_updates").fetchall())
        self.assertEqual("operating_quota", migrated.attempt_updates(ids[0])[0]["patch"]["error_code"])
        tm.Telemetry(old_path)
        self.assertEqual("quota", migrated.attempt(ids[0])["error_code"])

    def test_report_redacts_but_store_carries_only_tokens(self):
        class Failure(test_run_cycle.Talker):
            def dispatch(self, target, task, **kw):
                return ex.DispatchResult(ex.DispatchOutcome.FAILED, self.name, target,
                    error_code="executor_error", raw_error_code="future_code",
                    detail="failure sk-secret123 ghp_secret456", start_state="started")
        with patch.dict(os.environ, {"CODE_CYCLE_HOME": str(self.path.parent)}):
            report = run_cycle.run_cycle("repo", "task-172", router.TaskSignals(), self.store,
                issue_review="off", registry=ex.Registry([Failure("codex"), Failure("claude")]))
        text = report.explain()
        self.assertIn("error_code=executor_error", text)
        self.assertIn("start_state=started", text)
        self.assertIn("codex_error_info=future_code", text)
        statuses = cycle_status.read_statuses(self.path.parent / "status")
        line = cycle_status.format_progress_line(statuses[-1])
        cli = subprocess.run([sys.executable, str(ROOT / "scripts" / "cycle_status.py"),
                              "--line", "--status-dir", str(self.path.parent / "status")],
                             capture_output=True, text=True)
        self.assertEqual(0, cli.returncode, cli.stderr)
        self.assertIn("error_code=executor_error", cli.stdout)
        self.assertNotIn("sk-secret123", cli.stdout)
        self.assertIn("error_code=executor_error", line)
        self.assertIn("start_state=started", line)
        for output in (text, line):
            self.assertNotIn("sk-secret123", output)
            self.assertNotIn("ghp_secret456", output)
        attempts = self.store.dispatch_attempts("repo")
        self.assertEqual(("executor_error", "started"), (attempts[0]["error_code"], attempts[0]["start_state"]))
        with sqlite3.connect(self.path) as conn:
            database = "\n".join(conn.iterdump())
        for forbidden in ("sk-secret123", "ghp_secret456", "future_code", "failure sk", "[redacted]"):
            self.assertNotIn(forbidden, database)

    def test_unreadable_and_absent_results_update_attempt_projection(self):
        for body, code in (("malformed result", "unreadable_result"), ("", "no_error_report")):
            with self.subTest(code=code), patch.dict(os.environ, {"CODE_CYCLE_HOME": str(self.path.parent)}):
                report = run_cycle.run_cycle("repo", "task-" + code, router.TaskSignals(), self.store,
                    issue_review="off", registry=ex.Registry([test_run_cycle.Talker("codex", body), test_run_cycle.Talker("claude", body)]))
                attempt = self.store.dispatch_attempts("repo", task_id="task-" + code)[0]
                self.assertEqual((code, "failed"), (attempt["error_code"], attempt["outcome"]))
                self.assertIn("error_code=" + code, report.explain())

    def test_interruption_after_streamed_tool_activity_is_started(self):
        class Interrupted(test_run_cycle.StreamingNative):
            def dispatch(self, target, task, **kw):
                kw["on_progress"](tool=True)
                raise cycle.CycleInterrupted("SIGTERM")
        with patch.dict(os.environ, {"CODE_CYCLE_HOME": str(self.path.parent)}):
            report = run_cycle.run_cycle("repo", "task-interrupted", router.TaskSignals(), self.store,
                issue_review="off", registry=ex.Registry([Interrupted("codex", ""), Interrupted("claude", "")]))
        attempt = self.store.dispatch_attempts("repo", task_id="task-interrupted")[0]
        self.assertEqual(("interrupted", "started"), (attempt["error_code"], attempt["start_state"]))
        self.assertIn("start_state=started", report.explain())
