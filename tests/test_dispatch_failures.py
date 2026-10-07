"""Failure evidence crosses the process boundary; prose never enters the store."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
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
        class Refused(ex.Adapter):
            name = "codex"

            def dispatch(self, target, task, **kw):
                return self._blocked(target, task, "pre-launch refusal")

        for capability, code in expected.items():
            result = ex.dispatch(
                router.RoutingDecision("implement", TARGET, router.RoutingMode.PRODUCTION),
                capability, ex.Registry([Refused()]), writes=True,
                probes={"codex": ex.ProbeResult("codex", ex.Availability.READY, "fixture")},
            )
            self.assertEqual((code, "not_started"), (result.failure_code, result.start_state))
        with self.assertRaisesRegex(ex.ExecutorError, "unmapped missing_capability"):
            ex.DispatchResult(ex.DispatchOutcome.BLOCKED, "codex", TARGET,
                              missing_capability="future_capability").failure_code

    def test_outer_prechecks_refuse_without_starting_an_attempt(self):
        cases = (
            ("publication_access", {"publishes": True}, ex.Availability.READY, False),
            ("publication_access", {"publishes": True}, ex.Availability.READY, True),
            ("operating_availability", {}, ex.Availability.UNKNOWN, False),
            ("proven_readiness", {}, ex.Availability.AUTHENTICATED, False),
            ("read_only_enforcement", {"writes": False}, ex.Availability.READY, False),
            ("disposable_workspace", {"workspace_policy": "disposable"}, ex.Availability.READY, False),
            ("workspace_policy", {"workspace_policy": "workspace_write"}, ex.Availability.READY, False),
            ("read_only_verification", {"writes": False}, ex.Availability.READY, False),
        )
        with tempfile.TemporaryDirectory() as directory:
            for capability, kwargs, availability, preflight in cases:
                with self.subTest(capability=capability, preflight=preflight):
                    adapter = ex.CodexAdapter()
                    mode = (router.RoutingMode.CALIBRATION if capability == "proven_readiness"
                            else router.RoutingMode.PRODUCTION)
                    supports = capability not in {"read_only_enforcement", "disposable_workspace", "workspace_policy"}
                    with patch.object(adapter, "dispatch") as launch, \
                            patch.object(adapter, "supports_workspace_policy", return_value=supports), \
                            patch.object(adapter, "publication_access", return_value=(preflight, "fixture")), \
                            patch.object(ex, "_publication_preflight", return_value=(False, "fixture")), \
                            patch.object(adapter, "requires_publication_preflight", preflight):
                        attempts = []
                        result = ex.dispatch(
                            router.RoutingDecision("implement", TARGET, mode), "work",
                            ex.Registry([adapter]), cwd=directory,
                            policy=ex.ReadinessPolicy.ATTEMPT,
                            probes={"codex": ex.ProbeResult("codex", availability, "fixture",
                                                          provable_ceiling=availability)},
                            on_dispatch_attempt=lambda: attempts.append("attempt"),
                            **{"writes": True, **kwargs},
                        )
                    self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
                    self.assertEqual(capability, result.missing_capability)
                    self.assertEqual((ex.CAPABILITY_ERROR_CODES[capability], "not_started"),
                                     (result.failure_code, result.start_state))
                    launch.assert_not_called()
                    self.assertEqual([], attempts)

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

    def test_redaction_preserves_words_and_masks_additional_credentials(self):
        words = "worktree task-172 disk-full risk-high"
        self.assertEqual(words, ex.failure_excerpt(words))
        for secret in ("sk-secret123", "xoxb-1234-abcd", "AKIAIOSFODNN7EXAMPLE",
                       "api_key" + "=abc123secret", "token: abc123secret", "password" + "=hunter2",
                       "secret='two words'", "access_token" + '="two words"',
                       "Authorization: Basic dXNlcjpwYXNz", "Authorization: Bearer abc.def"):
            with self.subTest(secret=secret):
                excerpt = ex.failure_excerpt(secret)
                self.assertIn("[redacted]", excerpt)
                self.assertNotIn(secret.split("=")[-1], excerpt)
        for key in ("api_key", "password", "access_token", "client_secret"):
            excerpt = ex.failure_excerpt(json.dumps({key: "two secret words"}))
            self.assertIn("[redacted]", excerpt)
            self.assertNotIn("two secret words", excerpt)
        for words in ("max_tokens=100", "monkey=1", "token expired"):
            self.assertEqual(words, ex.failure_excerpt(words))

    def test_redaction_is_linear_on_long_separator_runs(self):
        # The previous name pattern took seconds on 16 KB and minutes on 40 KB.
        for text in ("a-" * 8000, "a_" * 8000, "x-" * 8000 + "api_key" + "=abc123secret"):
            with self.subTest(size=len(text), tail=text[-12:]):
                started = time.perf_counter()
                excerpt = ex.failure_excerpt(text, len(text))
                self.assertLess(time.perf_counter() - started, 0.5)
        self.assertNotIn("abc123secret", excerpt)
        self.assertIn("[redacted]", excerpt)

    def test_success_skips_session_lookup_and_second_workspace_fingerprint(self):
        for output in (stream({"type": "thread.started", "thread_id": "T"},
                              {"type": "turn.completed"}),
                       stream({"type": "item.completed", "item": {
                           "type": "agent_message", "text": "finished"}})):
            with patch.object(ex.CodexAdapter, "_session_events") as logs, \
                    patch.object(ex, "_workspace_fingerprint", return_value={}) as fingerprints:
                result = ex.CodexAdapter().dispatch(TARGET, "work", cwd=tempfile.gettempdir(),
                    runner=lambda *a, **k: subprocess.CompletedProcess([], 0, output, ""))
            self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
            self.assertEqual("started", result.start_state)
            logs.assert_not_called()
            self.assertEqual(1, fingerprints.call_count)

    def test_missing_result_still_reads_correlated_session_on_zero_exit(self):
        with patch.object(ex.CodexAdapter, "_session_events", return_value=[failed("server_overloaded")]) as logs:
            result = ex.CodexAdapter().dispatch(TARGET, "work", cwd=tempfile.gettempdir(),
                runner=lambda *a, **k: subprocess.CompletedProcess([], 0,
                    stream({"type": "thread.started", "thread_id": "T"}), ""))
        logs.assert_called_once()
        self.assertEqual("capacity", result.failure_code)

    def test_workspace_start_budget_does_not_limit_read_only_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", directory], capture_output=True, check=True)
            subprocess.run(["git", "-C", directory, "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "fixture"],
                           capture_output=True, check=True)
            path = Path(directory) / "dirty.txt"
            path.write_bytes(b"12345")
            self.assertIsNone(ex._workspace_fingerprint(directory, max_bytes=4))
            self.assertIsNone(ex._workspace_fingerprint(directory, max_files=0))
            before = ex._checkout_fingerprint(directory)
            path.write_bytes(b"12346")
            self.assertNotEqual(before, ex._checkout_fingerprint(directory))
            with patch.object(ex, "_workspace_fingerprint", return_value=None):
                result = self.dispatch(stream(failed(started=False)))
                unknown = self.dispatch(stream(failed()))
            self.assertEqual("not_started", result.start_state)
            self.assertEqual("unknown", unknown.start_state)

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
        with closing(sqlite3.connect(old_path)) as conn, conn:
            conn.executescript(tm.SCHEMA.replace(old_column, ""))
        with patch.object(tm.Telemetry, "_migrate_dispatch_attempts", lambda *a: None):
            old = tm.Telemetry(old_path)
            ids = [self.attempt(old) for _ in range(6)]
        codes = ("operating_quota", "operating_availability", "dispatch_failed", "executor_error", "unknown", "timeout")
        with closing(sqlite3.connect(old_path)) as conn, conn:
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
        with closing(sqlite3.connect(old_path)) as conn:
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
                             capture_output=True, text=True, encoding="utf-8")
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
        with closing(sqlite3.connect(self.path)) as conn:
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
