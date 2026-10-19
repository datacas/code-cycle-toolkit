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

    def publication_snapshots(self, change, *, seed=None, change_request_id=None):
        """Snapshot a fake GitHub before and after `change` mutates its state.

        The cycle publishes as `cycle-bot`; PR #7 on the branch is its own.
        """
        self.git("remote", "add", "origin", "https://github.com/owner/repo.git")
        real_run = subprocess.run
        calls = []
        state = {"pulls": [{"number": 7, "headRefOid": "head", "state": "OPEN",
                            "author": {"login": "cycle-bot"}, "title": "Fix",
                            "body": "Closes #173"}],
                 "comments": {}}
        if seed is not None:
            seed(state)

        def run(argv, **kwargs):
            if argv[0] != "gh":
                return real_run(argv, **kwargs)
            calls.append(argv)
            if argv[1:3] == ["api", "user"]:
                result = {"login": "cycle-bot"}
            elif argv[1:3] == ["pr", "list"]:
                result = state["pulls"]
            elif argv[1:3] == ["pr", "view"]:
                result = state["pulls"][0]
            else:
                result = [state["comments"].get(argv[-1], [])]
            return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

        with patch("subprocess.run", side_effect=run), patch("reroute._remote_ref_fingerprint", return_value="remote"):
            before = reroute.effect_snapshot(self.root, "owner/repo", publishes=True,
                                             issue_id="173", change_request_id=change_request_id)
            change(state)
            after = reroute.effect_snapshot(self.root, "owner/repo", publishes=True,
                                            issue_id="173", change_request_id=change_request_id)
        return before, after, calls

    @staticmethod
    def comment(author, body="ORCHESTRATION_RESULT", **extra):
        return {"id": 12, "user": {"login": author}, "body": body,
                "updated_at": "2026-10-19T10:00:00Z", **extra}

    def test_github_comment_review_and_issue_boundaries_are_read(self):
        def publish(state):
            state["comments"]["repos/owner/repo/issues/173/comments"] = [self.comment("cycle-bot")]

        before, after, calls = self.publication_snapshots(publish)
        self.assertEqual(("stop", "publication_changed"), reroute.reconcile(before, after))
        endpoints = [argv[-1] for argv in calls if argv[1] == "api"]
        for endpoint in ("issues/7/comments", "pulls/7/comments", "pulls/7/reviews", "issues/173/comments"):
            self.assertIn("repos/owner/repo/" + endpoint, endpoints)

    def test_only_the_cycle_accounts_publications_stop_a_reroute(self):
        """#268 Q-001: created or edited by the cycle's own account, nothing else."""
        issue = "repos/owner/repo/issues/173/comments"
        review = "repos/owner/repo/pulls/7/reviews"
        own = self.comment("Cycle-Bot")  # logins compare case-insensitively
        changes = {
            "own issue comment": (lambda s: s["comments"].update({issue: [own]}), "publication_changed"),
            "own review": (lambda s: s["comments"].update(
                {review: [{**own, "state": "COMMENTED"}]}), "publication_changed"),
            "own new PR": (lambda s: s["pulls"].append(
                {"number": 8, "headRefOid": "h8", "state": "OPEN",
                 "author": {"login": "cycle-bot"}, "title": "", "body": ""}), "publication_changed"),
            "own PR edited": (lambda s: s["pulls"][0].update(body="edited"), "publication_changed"),
            "own PR merged": (lambda s: s["pulls"][0].update(state="MERGED"), "publication_changed"),
            "other account comment": (lambda s: s["comments"].update(
                {issue: [self.comment("someone-else")]}), "no_effects"),
            "other account PR": (lambda s: s["pulls"].append(
                {"number": 9, "headRefOid": "h9", "state": "OPEN",
                 "author": {"login": "someone-else"}, "title": "", "body": ""}), "no_effects"),
            # A PR's title and body carry no editor, so any edit to the cycle's
            # own PR stops conservatively rather than being presumed foreign.
            "other account edits the cycle PR": (lambda s: s["pulls"][0].update(
                title="Retitled by a maintainer"), "publication_changed"),
        }
        for name, (change, reason) in changes.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as root:
                self.root = root
                self.git("init", "--initial-branch=task")
                self.git("commit", "--allow-empty", "-m", "baseline")
                before, after, _ = self.publication_snapshots(change)
                expected = "stop" if reason != "no_effects" else "reroute"
                self.assertEqual((expected, reason), reroute.reconcile(before, after))

    def test_existing_foreign_pull_edits_stop_a_reroute(self):
        """REV-001: PR authorship does not identify who later edited it."""
        changes = {
            "unchanged": ({}, "no_effects"),
            "closed": ({"state": "CLOSED"}, "publication_changed"),
            "reopened": ({"state": "OPEN"}, "publication_changed"),
            "merged": ({"state": "MERGED"}, "publication_changed"),
            "head pushed": ({"headRefOid": "new-head"}, "publication_changed"),
            "retitled": ({"title": "Edited by the cycle"}, "publication_changed"),
            "body edited": ({"body": "Edited by the cycle"}, "publication_changed"),
            "metadata only": ({"updatedAt": "later"}, "no_effects"),
        }
        for change_request_id in (None, "7"):
            for name, (fields, reason) in changes.items():
                with self.subTest(scope=change_request_id, change=name), tempfile.TemporaryDirectory() as root:
                    self.root = root
                    self.git("init", "--initial-branch=task")
                    self.git("commit", "--allow-empty", "-m", "baseline")

                    def seed(state):
                        state["pulls"][0]["author"] = {"login": "someone-else"}
                        if name == "reopened":
                            state["pulls"][0]["state"] = "CLOSED"

                    before, after, _ = self.publication_snapshots(
                        lambda state: state["pulls"][0].update(fields),
                        seed=seed, change_request_id=change_request_id)
                    expected = "stop" if reason == "publication_changed" else "reroute"
                    self.assertEqual((expected, reason), reroute.reconcile(before, after))

    def test_reactions_and_metadata_on_own_comments_are_not_publications(self):
        issue = "repos/owner/repo/issues/173/comments"

        def seed(state):
            state["comments"][issue] = [self.comment("cycle-bot")]

        def react(state):
            state["comments"][issue] = [self.comment(
                "cycle-bot", updated_at="2026-10-19T11:00:00Z",
                reactions={"+1": 1, "total_count": 1})]

        def edit(state):
            state["comments"][issue] = [self.comment("cycle-bot", body="edited")]

        for change, expected in ((react, ("reroute", "no_effects")),
                                 (edit, ("stop", "publication_changed"))):
            with self.subTest(change.__name__), tempfile.TemporaryDirectory() as root:
                self.root = root
                self.git("init", "--initial-branch=task")
                self.git("commit", "--allow-empty", "-m", "baseline")
                before, after, _ = self.publication_snapshots(change, seed=seed)
                self.assertEqual(expected, reroute.reconcile(before, after))

    def test_unknown_publication_identity_is_not_clean_evidence(self):
        self.git("remote", "add", "origin", "https://github.com/owner/repo.git")
        real_run = subprocess.run

        def run(argv, **kwargs):
            if argv[0] != "gh":
                return real_run(argv, **kwargs)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"login": ""}), "")

        with patch("subprocess.run", side_effect=run), patch("reroute._remote_ref_fingerprint", return_value="remote"):
            with self.assertRaises(ValueError):
                reroute.effect_snapshot(self.root, "owner/repo", publishes=True, issue_id="173")

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
