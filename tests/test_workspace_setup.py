"""Rollback at the real Git boundary, limited to this run's resources."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import test_run_cycle as fixtures
from test_run_cycle import RunCycleTestCase, Talker, rc
from workspace_setup import WorkspaceSetup, own_file


def setUpModule():
    global restore_environment
    restore_environment = fixtures.isolate_host_environment()


def tearDownModule():
    restore_environment()


class WorkspaceTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repo"
        self.fixture = fixtures.WorkspaceSelectionTests()
        self.config = self.fixture.repository(self.root)
        self.target = self.root / ".worktree" / "task-189"

    def git(self, *args):
        return self.fixture.git(self.root, *args)

    def select(self, **kw):
        return rc.select_workspace("189", "owner/api", self.config, cwd=str(self.root), **kw)

    def assert_removed(self):
        self.assertFalse(self.target.exists())
        self.assertNotIn("task-189", self.git("branch", "--format=%(refname:short)").splitlines())
        self.assertNotIn(str(self.target), self.git("worktree", "list", "--porcelain"))

    def test_failure_after_selection_removes_owned_worktree_and_branch(self):
        with WorkspaceSetup() as setup:
            self.select()
            self.assertEqual(["directory", "branch", "worktree"], [r.kind for r in setup.resources])
        self.assertEqual([], setup.errors)
        self.assert_removed()

    def test_failure_after_manager_created_resources_is_rolled_back(self):
        def run(command, **kw):
            result = subprocess.run(command, **kw)
            if command[:3] == ["git", "worktree", "add"] and result.returncode == 0:
                return subprocess.CompletedProcess(command, 1, "", "injected manager failure")
            return result
        with self.assertRaisesRegex(rc.CycleDriverError, "injected manager failure"):
            self.select(run=run)
        self.assert_removed()

    def test_preexisting_branch_is_retained(self):
        self.git("branch", "task-189", "main")
        with WorkspaceSetup():
            self.select()
        self.assertFalse(self.target.exists())
        self.assertIn("task-189", self.git("branch", "--format=%(refname:short)").splitlines())

    def test_continue_reuses_worktree_without_owning_it(self):
        selected, _ = self.select()
        with WorkspaceSetup() as setup:
            self.select(continue_work=True)
        self.assertEqual([], setup.resources)
        self.assertTrue(Path(selected).is_dir())

    def test_herdr_created_worktree_has_provenance_and_is_rolled_back(self):
        def run(command, **kw):
            if command[:3] == ["herdr", "worktree", "create"]:
                command = ["git", "worktree", "add", "-b",
                           command[command.index("--branch") + 1],
                           command[command.index("--path") + 1],
                           command[command.index("--base") + 1]]
            return subprocess.run(command, **kw)
        with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), WorkspaceSetup() as setup:
            self.select(run=run)
            self.assertEqual("Herdr", setup.resources[-1].provenance)
        self.assert_removed()

    def test_preexisting_herdr_worktree_is_not_owned(self):
        self.select()
        def run(command, **kw):
            if command[:3] == ["herdr", "worktree", "open"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.run(command, **kw)
        with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), WorkspaceSetup() as setup:
            self.select(continue_work=True, run=run)
        self.assertEqual([], setup.resources)
        self.assertTrue(self.target.exists())

    def pr_setup(self, *, existing=False):
        # A local origin exports a pull ref at a commit not on main.
        self.git("checkout", "-b", "pr-source")
        (self.root / "feature").write_text("PR\n")
        self.git("add", "feature")
        self.git("commit", "-m", "PR head")
        sha = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/pull/74/head", sha)
        self.git("checkout", "main")
        self.git("branch", "-D", "pr-source")
        self.git("remote", "add", "origin", str(self.root))
        if existing:
            self.git("update-ref", "refs/code-cycle/pulls/74/head", sha)
        return sha

    def test_created_pr_ref_is_removed_after_worktree_and_branch(self):
        self.pr_setup()
        with mock.patch.object(rc, "_pull_request_branch", return_value="feature-189"), WorkspaceSetup() as setup:
            self.select(change_request_id="74")
        self.assertEqual([], setup.errors)
        self.assertFalse(self.target.exists())
        self.assertNotIn("feature-189", self.git("branch", "--format=%(refname:short)").splitlines())
        self.assertEqual("", self.git("for-each-ref", "--format=%(refname)", "refs/code-cycle/pulls/74"))

    def test_existing_pr_ref_survives_rollback(self):
        sha = self.pr_setup(existing=True)
        with mock.patch.object(rc, "_pull_request_branch", return_value="feature-189"), WorkspaceSetup():
            self.select(change_request_id="74")
        self.assertEqual(sha, self.git("rev-parse", "refs/code-cycle/pulls/74/head"))

    def test_created_pr_ref_in_use_is_retained_with_error(self):
        sha = self.pr_setup()
        with mock.patch.object(rc, "_pull_request_branch", return_value="feature-189"), WorkspaceSetup() as setup:
            self.select(change_request_id="74")
            self.git("branch", "another-user", sha)
        self.assertEqual(sha, self.git("rev-parse", "refs/code-cycle/pulls/74/head"))
        self.assertTrue(any("fetched ref is in use" in error for error in setup.errors))

    def test_main_failure_before_run_cycle_rolls_back_and_exits_two(self):
        config = self.root / ".code-cycle.yml"
        config.write_text("code_cycle:\n  repository:\n    default_branch: main\n")
        stderr = io.StringIO()
        with mock.patch.object(rc, "code_host_of", side_effect=rc.CycleDriverError("injected setup failure")):
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    rc.main(["--repo", "owner/api", "--task", "189", "--cwd", str(self.root),
                             "--config", str(config)])
        self.assertEqual(2, error.exception.code)
        self.assertIn("injected setup failure", stderr.getvalue())
        self.assert_removed()

    def test_rollback_collects_errors_and_continues_in_reverse_order(self):
        seen = []
        with WorkspaceSetup() as setup:
            setup.record("fixture", "first", "test", lambda: seen.append("first"))
            def fail():
                seen.append("second")
                raise RuntimeError("injected")
            setup.record("fixture", "second", "test", fail)
            setup.record("fixture", "third", "test", lambda: seen.append("third"))
        self.assertEqual(["third", "second", "first"], seen)
        self.assertEqual(["fixture second: injected"], setup.errors)
        setup.rollback()
        self.assertEqual(3, len(seen))

    def test_successful_detach_commits_and_failed_handoff_rolls_back(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                with tempfile.TemporaryDirectory() as temporary:
                    database = str(Path(temporary) / "telemetry.sqlite")
                    owned = Path(temporary) / "owned"
                    owned.write_text("owned")
                    with WorkspaceSetup() as setup:
                        setup.record("fixture", owned, "test", owned.unlink)
                        with mock.patch.object(rc.subprocess, "Popen", side_effect=OSError("handoff failed") if fails else None) as popen:
                            popen.return_value.pid = 42
                            with contextlib.redirect_stdout(io.StringIO()):
                                if fails:
                                    with self.assertRaises(OSError):
                                        rc.detach([], database)
                                else:
                                    self.assertEqual(0, rc.detach([], database))
                    self.assertEqual(not fails, owned.exists())
                    if fails:
                        self.assertEqual([], list((Path(temporary) / "status").glob("detached-*")))


class DispatchCommitTests(RunCycleTestCase):
    def test_routing_stop_keeps_rollback_active_and_removes_owned_status(self):
        with WorkspaceSetup() as setup:
            report = rc.run_cycle(
                "owner/api", "API-7", fixtures.router.TaskSignals(), self.store,
                registry=fixtures.ex.Registry([Talker("codex"), Talker("claude")]),
                availability={"codex": fixtures.ex.Availability.UNKNOWN,
                              "claude": fixtures.ex.Availability.UNKNOWN},
                issue_review="off")
            self.assertFalse(setup.committed)
            status_files = list((Path(self.store.path).parent / "status").glob("*.json"))
            self.assertTrue(status_files)
            self.assertEqual(rc.UNRESOLVED_END, report.status)
        self.assertTrue(all(not path.exists() for path in status_files))

    def test_stage_failure_after_dispatch_never_rolls_back_setup(self):
        with WorkspaceSetup() as setup:
            marker = self.home / "owned"
            marker.parent.mkdir(parents=True, exist_ok=True)
            own_file(marker, "test")
            marker.write_text("owned")
            self.run_cycle(Talker("codex", "no structured result"), Talker("claude"))
            self.assertTrue(setup.committed)
        self.assertTrue(marker.exists())

    def test_pre_dispatch_failure_rolls_back_setup(self):
        with WorkspaceSetup() as setup:
            marker = self.home / "owned"
            marker.parent.mkdir(parents=True, exist_ok=True)
            own_file(marker, "test")
            marker.write_text("owned")
            with mock.patch.object(rc, "CycleRecorder", side_effect=rc.CycleDriverError("before dispatch")):
                with self.assertRaises(rc.CycleDriverError):
                    self.run_cycle(Talker("codex"), Talker("claude"))
            self.assertFalse(setup.committed)
        self.assertFalse(marker.exists())
