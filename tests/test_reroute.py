from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import executors as ex
import reroute


class ReconciliationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = self.directory.name
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        self.git("init", "--initial-branch=task")
        self.git("commit", "--allow-empty", "-m", "baseline")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.root, *args], env=self.env,
                              check=True, capture_output=True, text=True).stdout

    def test_real_workspace_and_remote_changes_are_detected(self):
        remote = str(Path(self.root) / "remote.git")
        subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
        self.git("remote", "add", "origin", remote)
        self.git("push", "origin", "HEAD:refs/heads/task")
        # Keep the bare remote outside the workspace evidence.
        Path(self.root, ".git", "info", "exclude").write_text("remote.git/\n")
        before = reroute.effect_snapshot(self.root, "owner/repo", publishes=False)
        self.assertEqual(("reroute", "no_effects"), reroute.reconcile(before, before))
        Path(self.root, "dirty.txt").write_text("new work")
        after = reroute.effect_snapshot(self.root, "owner/repo", publishes=False)
        self.assertEqual(("stop", "dirty_workspace"), reroute.reconcile(before, after))
        Path(self.root, "dirty.txt").unlink()
        self.git("push", "origin", "HEAD:refs/heads/new-publication")
        after = reroute.effect_snapshot(self.root, "owner/repo", publishes=False)
        self.assertEqual(("stop", "remote_changed"), reroute.reconcile(before, after))

    def test_github_comment_review_and_issue_boundaries_are_read(self):
        self.git("remote", "add", "origin", "https://github.com/owner/repo.git")
        real_run = subprocess.run
        calls = []
        published = []

        def run(argv, **kwargs):
            if argv[0] != "gh":
                return real_run(argv, **kwargs)
            calls.append(argv)
            result = ([{"number": 7, "headRefOid": "head", "state": "OPEN"}]
                      if argv[1:3] == ["pr", "list"] else [published.copy()])
            return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

        with patch("subprocess.run", side_effect=run), patch("reroute._remote_ref_fingerprint", return_value="remote"):
            before = reroute.effect_snapshot(self.root, "owner/repo", publishes=True, issue_id="173")
            published.append({"id": 12, "body": "ORCHESTRATION_RESULT"})
            after = reroute.effect_snapshot(self.root, "owner/repo", publishes=True, issue_id="173")
        self.assertEqual(("stop", "publication_changed"), reroute.reconcile(before, after))
        endpoints = [argv[-1] for argv in calls if argv[1] == "api"]
        for endpoint in ("issues/7/comments", "pulls/7/comments", "pulls/7/reviews", "issues/173/comments"):
            self.assertIn("repos/owner/repo/" + endpoint, endpoints)

    def test_unavailable_publication_boundary_is_not_clean_evidence(self):
        with patch("reroute.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            with self.assertRaises(Exception):
                reroute.effect_snapshot(self.root, "owner/repo", publishes=True)
        self.assertEqual(("stop", "evidence_unavailable"), reroute.reconcile(None, None))


@unittest.skipUnless(os.name == "posix", "native group exit proof is POSIX-only")
class NativeProcessBoundaryTests(unittest.TestCase):
    def test_zero_and_nonzero_processes_report_exit_proof(self):
        for code in (0, 3):
            result = ex._run([sys.executable, "-c", f"print('boundary'); raise SystemExit({code})"])
            self.assertEqual(code, result.returncode)
            self.assertEqual("boundary\n", result.stdout)
            self.assertTrue(result.process_exited)

    def test_timeout_stops_the_group_before_returning_evidence(self):
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            ex._run([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"],
                    timeout=0.1)
        self.assertTrue(caught.exception.process_exited)
        self.assertIn("started", caught.exception.stdout)

    def test_parent_exit_does_not_leave_a_child_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "late-write"
            child = f"import time; from pathlib import Path; time.sleep(0.5); Path({str(marker)!r}).touch()"
            parent = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
            result = ex._run([sys.executable, "-c", parent], timeout=3)
            self.assertTrue(result.process_exited)
            # Waiting beyond the child's write deadline observes the process boundary.
            import time
            time.sleep(0.7)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
