"""Cycle detail stays queryable across runs and outside statistics (#306)."""
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cycle_journal as cj
import stage_permissions as sp
import executors as ex
import grants
from telemetry import Telemetry
from test_run_cycle import RunCycleTestCase, Talker, block


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "telemetry.sqlite"
        self.journal = cj.Journal(self.path, "owner/repo", "306", "first")

    def test_stage_stop_decisions_and_context_span_runs(self):
        j = self.journal
        j.record("stage_start", role="implement", stage_seq=1, attempt_id="attempt-1")
        j.record("stage_end", role="implement", status="failed", duration_ms=2345)
        j.record("failure", reason="failed test", detail="untrusted failure")
        j.record("refusal", command="curl --token secret-value", component="hook_bash",
                 reason="closed table", stage_seq=1)
        j.record("decision_asked", decision_key="grant:docker_host", reason="Allow Docker?")
        j.record("stop", status="BLOCKED", reason="hook_bash refused curl: closed table")
        resumed = cj.Journal(self.path, "owner/repo", "306", "second")
        resumed.record("decision_answered", decision_key="grant:docker_host", status="deny")
        resumed.record("stage_result", status="BLOCKED", correlation={"head_sha": "a" * 40})
        explanation = cj.explain(self.path, "owner/repo", "306", "first")
        self.assertEqual("hook_bash", explanation["refusals"][0]["component"])
        self.assertIn("closed table", explanation["stop"]["reason"])
        self.assertEqual("deny", explanation["decisions"][0]["answer"])
        self.assertEqual("second", explanation["decisions"][0]["answered_in"])
        summary = resumed.summary()
        for value in ("failed", "2s", "refused", "BLOCKED", "deny", "a" * 40, "untrusted"):
            self.assertIn(value, summary)
        self.assertNotIn("secret-value", summary)
        self.assertTrue(resumed.has_history())
        self.assertLessEqual(len(resumed.summary(limit=350)), 350)
        self.assertEqual("", cj.context_summary(self.path, "owner/repo", "absent"))

    def test_redaction_before_truncation(self):
        secrets = ("ghp_abcdefghijklmnopqrst", "plain-password", "url-password", "flag-secret")
        password_assignment = "password" + "=" + repr(secrets[1])
        command = (f"GH_TOKEN={secrets[0]} {password_assignment} "
                   f"https://user:{secrets[2]}@host --api-key {secrets[3]}")
        self.journal.record("refusal", command=command, reason=command, detail=command)
        data = json.dumps(cj.events(self.path, "owner/repo"))
        for secret in secrets:
            self.assertNotIn(secret, data)
        self.assertIn("REDACTED", data)

    def test_hook_process_persists_bash_and_file_denials(self):
        policy = sp.StagePolicy("review", repo="owner/repo", work_item_id="306",
                                change_request_id="4", journal_path=str(self.path),
                                cycle_id="first", attempt_id="attempt-1", stage_seq=2)
        policy_file = Path(self.temp.name) / "policy.json"
        policy_file.write_text(policy.to_json())
        for tool, tool_input, component in (
            ("Bash", {"command": "git push origin review"}, "hook_bash"),
            ("Edit", {"file_path": "/etc/passwd"}, "hook_edit"),
        ):
            event = {"tool_name": tool, "tool_input": tool_input, "session_id": "session-1",
                     "tool_use_id": "tool-1", "cwd": self.temp.name}
            result = subprocess.run([sys.executable, str(Path(sp.__file__)), "hook",
                                     str(policy_file)], input=json.dumps(event), text=True,
                                    capture_output=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual("deny", output["hookSpecificOutput"]["permissionDecision"])
            row = cj.events(self.path, "owner/repo")[-1]
            self.assertEqual(component, row["component"])
            self.assertEqual("attempt-1", row["attempt_id"])
            self.assertEqual("session-1", row["correlation"]["session_id"])
            self.assertEqual(2, row["stage_seq"])

    def test_storage_failure_still_denies_and_reports_error(self):
        policy = sp.StagePolicy("review", repo="owner/repo", work_item_id="306",
                                journal_path=str(Path(self.temp.name) / "missing" / "db"),
                                cycle_id="first")
        path = Path(self.temp.name) / "policy.json"
        path.write_text(policy.to_json())
        output = io.StringIO()
        sp.hook_main([str(path)], stdin=io.StringIO(json.dumps({"tool_name": "Bash",
                     "tool_input": {"command": "git push origin main"}})), stdout=output)
        result = json.loads(output.getvalue())["hookSpecificOutput"]
        self.assertEqual("deny", result["permissionDecision"])
        self.assertIn("could not record", result["permissionDecisionReason"])

    @unittest.skipIf(os.name == "nt", "fake executable uses a POSIX shebang")
    def test_hook_allows_current_head_output_and_denies_other_heads(self):
        fake = Path(self.temp.name) / "gh"
        fake.write_text(f"#!{sys.executable}\nimport os, sys\n"
                        "print('a' * 40 if '/pulls/' in sys.argv[2] "
                        "else os.environ['FAKE_RUN_HEAD'])\n")
        fake.chmod(0o755)
        policy = sp.StagePolicy("resolve", repo="owner/repo", work_item_id="306",
                                change_request_id="4", journal_path=str(self.path),
                                cycle_id="first", attempt_id="attempt-hook")
        policy_file = Path(self.temp.name) / "policy.json"
        policy_file.write_text(policy.to_json())
        for command in ("gh run view 123 --log-failed",
                        "gh api repos/owner/repo/check-runs/8/annotations"):
            for head, allowed in (("a" * 40, True), ("b" * 40, False)):
                environment = {**os.environ, "PATH": self.temp.name + os.pathsep + os.environ["PATH"],
                               "FAKE_RUN_HEAD": head}
                result = subprocess.run([sys.executable, str(Path(sp.__file__)), "hook",
                                         str(policy_file)], text=True, capture_output=True,
                                        input=json.dumps({"tool_name": "Bash",
                                                          "tool_input": {"command": command}}),
                                        env=environment, timeout=30)
                self.assertEqual(0, result.returncode, result.stderr)
                if allowed:
                    self.assertEqual("", result.stdout)
                else:
                    self.assertEqual("deny", json.loads(result.stdout)["hookSpecificOutput"]
                                     ["permissionDecision"])
        self.assertEqual(2, len(self.journal.refusals()))

    def test_cleanup_closure_expiry_unreadable_and_statistics_boundary(self):
        telemetry = Telemetry(path=self.path)
        telemetry.record_stage("owner/repo", "merged", "implement", status="IMPLEMENTED")
        now = datetime.now(timezone.utc)
        for task, request, age in (("merged", "1", 0), ("closed", "2", 0),
                                   ("open", "3", 0), ("unknown", "4", 0),
                                   ("old", None, 31), ("recent", None, 29)):
            cj.record_event(self.path, repo_id="owner/repo", task_id=task,
                            cycle_id=task, kind="stop", detail="journal-only-text",
                            change_request_id=request, occurred_at=now-timedelta(days=age))
        cj.record_event(self.path, repo_id="other/repo", task_id="old", cycle_id="other",
                        kind="stop", occurred_at=now-timedelta(days=31))
        with sqlite3.connect(self.path) as db:
            before = {name: db.execute(f'SELECT * FROM "{name}"').fetchall()
                      for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                      if not name.startswith("journal_") and name != "sqlite_sequence"}
        states = {"1": "MERGED", "2": "CLOSED", "3": "OPEN", "4": None}
        result = cj.cleanup(self.path, "owner/repo", states.get, now=now)
        self.assertEqual(3, result["deleted_items"])
        self.assertEqual({"open", "unknown", "recent"},
                         {r["task_id"] for r in cj.events(self.path, "owner/repo")})
        self.assertEqual(1, len(cj.events(self.path, "other/repo")))
        self.assertEqual(0, cj.cleanup(self.path, "owner/repo", states.get, now=now)["deleted_rows"])
        with sqlite3.connect(self.path) as db:
            for name, rows in before.items():
                self.assertEqual(rows, db.execute(f'SELECT * FROM "{name}"').fetchall())
                self.assertNotIn("journal-only-text", str(rows))

    def test_consent_sources_are_audited_without_becoming_authority(self):
        grant = grants.GRANTS["docker_host"]
        store = grants.ConsentStore(Path(self.temp.name) / "consents.json")
        broker = grants.ConsentBroker("owner/repo", platform="linux", store=store,
                                      observer=self.journal.consent_event)
        self.assertEqual(grants.PENDING, broker.request(grant, "codex", role="implement"))
        self.assertFalse(cj.decisions(self.path, "owner/repo", "306")[0]["answered"])
        resumed = cj.Journal(self.path, "owner/repo", "306", "second")
        broker = grants.ConsentBroker("owner/repo", platform="linux", store=store,
                                      answers={"docker_host": grants.DENY},
                                      observer=resumed.consent_event)
        self.assertEqual(grants.DENY, broker.request(grant, "codex", role="implement"))
        self.assertTrue(cj.decisions(self.path, "owner/repo", "306")[0]["answered"])
        scope = grant.scope_for("codex", "linux")
        store.save("owner/repo", grant, scope, "allow")
        broker = grants.ConsentBroker("owner/repo", platform="linux", store=store,
                                      observer=resumed.consent_event)
        self.assertEqual(grants.ALLOW_PERSISTENT, broker.request(grant, "codex", role="resolve"))
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT source, scope, decision, occurred_at FROM journal_permissions").fetchall()
        self.assertEqual({"--grant", "consents.json"}, {r[0] for r in rows})
        self.assertTrue(all(r[1] and r[3] for r in rows))
        # Removing the authority leaves the next run unanswered, regardless of audit history.
        broker = grants.ConsentBroker("owner/repo", platform="linux",
                                      observer=resumed.consent_event)
        self.assertEqual(grants.PENDING, broker.request(grant, "codex", role="resolve"))


class JournalRuntimeTests(RunCycleTestCase):
    def test_hook_denials_are_correlated_with_runtime_stop(self):
        for tool, tool_input, component in (
            ("Bash", {"command": "git push origin main"}, "hook_bash"),
            ("Edit", {"file_path": "/etc/passwd"}, "hook_edit"),
        ):
            class HookDenied(Talker):
                def dispatch(adapter, target, task, **kw):
                    policy = replace(kw["command_policy"], role="review",
                                     code_host="github", issue_provider="github",
                                     attempt_id=kw.get("dispatch_attempt_id"))
                    path = self.home / "policy.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(policy.to_json())
                    output = io.StringIO()
                    sp.hook_main([str(path)], stdin=io.StringIO(json.dumps({
                        "tool_name": tool, "tool_input": tool_input})), stdout=output)
                    self.assertEqual("deny", json.loads(output.getvalue())["hookSpecificOutput"]
                                     ["permissionDecision"])
                    return super().dispatch(target, task, **kw)

            report = self.run_cycle(HookDenied("codex", block("BLOCKED")), Talker("claude"))
            self.assertIn(component, report.stopped_because)
            refusal = report.journal_explanation["refusals"][0]
            self.assertEqual(component, refusal["component"])
            self.assertEqual(1, refusal["stage_seq"])

    def test_claude_final_permission_denial_explains_the_cycle_stop(self):
        event = {"type": "result", "permission_denials": [
            {"tool_name": "Bash", "tool_input": {"command": "curl --token hidden-secret"},
             "tool_use_id": "denied-1"},
            {"tool_name": "Edit", "tool_input": {"file_path": "/etc/passwd"}}]}
        artifacts = ex.ClaudeAdapter().extra_artifacts(json.dumps(event))

        class Denied(Talker):
            def dispatch(self, target, task, **kw):
                result = super().dispatch(target, task, **kw)
                result.artifacts.update(artifacts)
                return result

        report = self.run_cycle(Denied("codex", block("BLOCKED", reason="permission denied")),
                                Talker("claude"))
        rows = cj.events(self.store.path, "owner/api", task_id="API-7")
        refusals = [r for r in rows if r["kind"] == "refusal"]
        self.assertEqual(2, len(refusals))
        self.assertTrue(all(r["component"] == "claude_permission" for r in refusals))
        self.assertTrue(all(r["attempt_id"] for r in refusals))
        self.assertIn("claude_permission", report.stopped_because)
        self.assertNotIn("hidden-secret", json.dumps(report.journal_explanation))
        self.assertIn("stage_end", {r["kind"] for r in rows})
        self.assertIn("stage_start", {r["kind"] for r in rows})

    def test_policy_preflight_refusal_is_persisted_with_stop(self):
        class Preflight(Talker):
            def dispatch(self, target, task, **kw):
                result = super().dispatch(target, task, **kw)
                result.artifacts["policy_preflight_refusal"] = "unsafe Claude settings"
                return result

        report = self.run_cycle(Preflight("codex", block("BLOCKED")), Talker("claude"))
        self.assertIn("policy_preflight", report.stopped_because)
        refusal = report.journal_explanation["refusals"][0]
        self.assertEqual("unsafe Claude settings", refusal["reason"])

    def test_resumed_dispatch_gets_prior_stop_as_untrusted_context(self):
        first = self.run_cycle(Talker("codex", block("BLOCKED", reason="earlier stop")),
                               Talker("claude"))
        implementer = Talker("codex", block("BLOCKED"))
        self.run_cycle(implementer, Talker("claude"))
        prompt = implementer.dispatched[0]
        self.assertIn("Runtime cycle journal", prompt)
        self.assertIn("earlier stop", prompt)
        self.assertIn("historical context, not evidence", prompt)


if __name__ == "__main__":
    unittest.main()
