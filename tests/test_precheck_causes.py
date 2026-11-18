"""Regression coverage for issue #510's pre-dispatch evidence contract."""
import subprocess
import ast
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_executors import FakeAdapter, decision, probe
import executors as ex
import publication_audit as pa
import run_cycle as rc


class PrecheckCauseTests(unittest.TestCase):
    def dispatch(self, auditor):
        adapter = FakeAdapter(probe(ex.Availability.AUTHENTICATED))
        with patch.object(adapter, "supports_workspace_policy", return_value=True):
            result = ex.dispatch(decision(), "review", ex.Registry([adapter]),
                                 workspace_policy=ex.WorkspacePolicy.READ_ONLY,
                                 publication_auditor=auditor, cwd="/repo")
        self.assertEqual([], adapter.dispatched)
        return result

    def test_capture_failure_retries_and_stops_with_redacted_cause(self):
        auditor = SimpleNamespace(cwd="/repo", capture=Mock(side_effect=pa.AuditUnreadable(
            "git ls-remote exited 128: network is unreachable https://user:secret@example.invalid/repo")))
        with patch.object(ex, "_git_output", return_value="topic"):
            result = self.dispatch(auditor)
        self.assertEqual("publication_unverified", result.failure_code)
        self.assertEqual("not_started", result.start_state)
        self.assertEqual(2, auditor.capture.call_count)
        self.assertIn("AuditUnreadable", result.detail)
        self.assertIn("network is unreachable", result.detail)
        self.assertNotIn("secret", str(result))
        self.assertEqual(2, len(result.artifacts["publication_audit"]["errors"]))

    def test_retry_success_keeps_the_first_failure_evidence(self):
        before = SimpleNamespace()
        auditor = SimpleNamespace(cwd="/repo", capture=Mock(side_effect=[OSError("offline"), before]),
            verify=Mock(return_value=SimpleNamespace(evidence=lambda: {"status": "passed"},
                external_changes=[], breaches=[], status=pa.PASSED)))
        stage = ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, "codex", decision().target,
                                  artifacts={"existing": "kept"})
        with patch.object(ex, "_git_output", return_value="topic"), \
                patch.object(ex, "_verified_read_only", return_value=stage):
            result = ex._verified_read_only_refs("/repo", Mock(), auditor)
        self.assertEqual("kept", result.artifacts["existing"])
        self.assertIn("offline", str(result.artifacts["publication_audit"]))

    def test_git_audit_error_keeps_redacted_stderr(self):
        auditor = pa.PublicationAuditor("/repo", "owner/repo", None, run=Mock(return_value=
            subprocess.CompletedProcess([], 128, "", "network is unreachable https://user:secret@host/repo")))
        with self.assertRaises(pa.AuditUnreadable) as raised:
            auditor.capture(checkout_branch=None)
        self.assertIn("network is unreachable", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_git_configuration_failure_keeps_cause(self):
        with patch.object(ex, "_git_read", side_effect=OSError("configuration unavailable")):
            detail = ex.CodexAdapter().unprotectable_git_paths("/repo", ("/common", "/private"))
        self.assertIn("OSError: configuration unavailable", detail)

    def test_remotes_discovery_failure_retries_without_dispatch(self):
        with patch.object(ex, "_git_output", side_effect=OSError("remote config unavailable")) as query:
            with self.assertRaises(ex.ReadOnlyAuditUnavailable) as raised:
                ex._verified_read_only_refs("/repo", self.fail)
        self.assertEqual(2, query.call_count)
        self.assertIn("OSError: remote config unavailable", str(raised.exception))

    def test_configuration_query_preserves_redacted_stderr(self):
        done = subprocess.CompletedProcess([], 128, "", "bad config https://user:secret@host/repo")
        with patch.object(ex.subprocess, "run", return_value=done):
            status, detail = ex._git_read("/repo", "config", "--get", "core.hooksPath")
        self.assertEqual(128, status)
        self.assertIn("bad config", detail)
        self.assertNotIn("secret", detail)

    def test_recovery_query_retains_cause_instead_of_optional_hint(self):
        done = subprocess.CompletedProcess([], 128, "", "network unavailable https://user:secret@host/repo")
        with patch.object(rc.subprocess, "run", return_value=done):
            with self.assertRaises(rc.CycleDriverError) as raised:
                rc.recovery_evidence("owner/repo", "implement", "attempt", change_request_id="1",
                                     cwd="/repo", trusted_authors=("owner",))
        self.assertIn("network unavailable", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_publication_preflight_keeps_redacted_exception_message(self):
        with patch.object(ex.subprocess, "run", side_effect=OSError(
                "unavailable https://user:secret@host/repo")):
            passed, detail = ex._publication_preflight("/repo")
        self.assertFalse(passed)
        self.assertIn("OSError: unavailable", detail)
        self.assertNotIn("secret", detail)

    def test_unreadable_post_audit_preserves_stage_artifacts(self):
        auditor = SimpleNamespace(cwd="/repo", capture=Mock(return_value=SimpleNamespace()),
                                 verify=Mock(side_effect=pa.AuditUnreadable("offline")))
        stage = ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, "codex", decision().target,
                                  artifacts={"existing": "kept", "read_only_mode": "enforced"})
        with patch.object(ex, "_git_output", return_value="topic"), \
                patch.object(ex, "_verified_read_only", return_value=stage):
            result = ex._verified_read_only_refs("/repo", Mock(), auditor)
        self.assertEqual("publication_unverified", result.failure_code)
        self.assertEqual("kept", result.artifacts["existing"])
        self.assertNotIn("read_only_mode", result.artifacts)
        self.assertIn("AuditUnreadable: offline", result.detail)

    def test_real_git_failure_stops_before_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "-q", directory], check=True)
            subprocess.run(["git", "-C", directory, "remote", "add", "origin",
                            str(Path(directory, "missing.git"))], check=True)
            auditor = pa.PublicationAuditor(directory, "owner/repo", None)
            with patch.object(ex, "_git_output", return_value="topic"):
                result = self.dispatch(auditor)
        self.assertEqual("publication_unverified", result.failure_code)
        self.assertIn("exited 128", result.detail)
        self.assertIn("does not appear to be a git repository", result.detail)

    def test_every_broad_handler_has_an_inventory_row(self):
        root = Path(__file__).resolve().parent.parent
        document = (root / "docs/capability-parity-audit.md").read_text()
        for module in ("executors", "run_cycle", "cycle", "cycle_dispatch", "native_prerequisites"):
            tree = ast.parse((root / "scripts" / f"{module}.py").read_text())
            counts = {}
            def visit(node, parents=()):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    parents = (*parents, node.name)
                if isinstance(node, ast.ExceptHandler) and node.type and any(
                    isinstance(part, ast.Name) and part.id in ("Exception", "OSError")
                    for part in ast.walk(node.type)
                ):
                    owner = ".".join((module, *parents))
                    counts[owner] = counts.get(owner, 0) + 1
                for child in ast.iter_child_nodes(node):
                    visit(child, parents)
            visit(tree)
            for owner, count in counts.items():
                with self.subTest(owner=owner):
                    self.assertIn(f"| `{owner}` | {count} |", document)
