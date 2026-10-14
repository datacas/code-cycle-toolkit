"""The Codex Git metadata sandbox must leave no trace in `.git` (#229).

Codex's Linux sandbox mounts each filesystem rule on its destination and
creates a missing destination as an empty file, which survives the dispatch.
These tests run the real profile through a fake `codex` that reproduces that
materialisation, so they check the repository itself, during and after the
dispatch, rather than only the serialized rules. A live Codex CLI is not
available in CI; docs/verification.md describes the manual probe.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import executors as ex  # noqa: E402
import router  # noqa: E402
from test_support import isolate_host_environment  # noqa: E402


_restore_host_environment = None


def setUpModule() -> None:
    global _restore_host_environment
    _restore_host_environment = isolate_host_environment()


def tearDownModule() -> None:
    if _restore_host_environment is not None:
        _restore_host_environment()


TARGET = router.parse_target("codex:openai/gpt-6-luna high")
POSIX = os.name == "posix"

# A stand-in for `codex` that does what its Linux sandbox did in the incident:
# every rule destination that does not exist is created as an empty file.
FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
if os.getenv("FAKE_CODEX_EXIT"): sys.exit(int(os.environ["FAKE_CODEX_EXIT"]))
filesystem = next((v.split("=", 1)[1] for v in args
                   if re.match(r"permissions\.[a-z_]+\.filesystem=", v)), "{}")
for path, _access in re.findall(r'("(?:\\.|[^"])*")=("(?:\\.|[^"])*")', filesystem):
    path = json.loads(path)
    if not os.path.lexists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
def snapshot(root):
    entries = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            if ".code-cycle-" in name: continue
            path = os.path.join(directory, name)
            key = os.path.relpath(path, root)
            if os.path.isdir(path): entries[key] = "dir"
            else:
                with open(path, "rb") as handle: entries[key] = handle.read().hex()
    return entries
record = {
    "command": args[0],
    "during": snapshot(os.environ["FAKE_CODEX_GIT_DIR"]),
    "git_config_parameters": os.environ.get("GIT_CONFIG_PARAMETERS"),
    "add_dirs": [v for f, v in zip(args, args[1:]) if f == "--add-dir"],
}
with open(os.environ["FAKE_CODEX_LOG"], "w") as log: json.dump(record, log)
if args[0] == "sandbox":
    script = args[args.index("--") + 1:][-1]
    pattern = (r'create_exclusive\(("(?:\\.|[^"])*\.code-cycle-write-probe-[^"]*"), '
               r'("(?:\\.|[^"])*")\)')
    for path, token in re.findall(pattern, script):
        with open(json.loads(path), "w") as marker: marker.write(json.loads(token))
else:
    print(json.dumps({"type": "item.completed",
                      "item": {"type": "agent_message", "text": "done"}}))
    print(json.dumps({"type": "turn.completed"}))
'''


def snapshot(root: Path) -> dict[str, str]:
    """Every path below `root` with its type and contents, minus probe markers."""
    entries = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            if ".code-cycle-" in name:
                continue
            path = Path(directory) / name
            key = os.path.relpath(path, root)
            entries[key] = "dir" if path.is_dir() else path.read_bytes().hex()
    return entries


def filesystem_rules(argv: list[str]) -> dict[str, str]:
    value = next(
        item.split("=", 1)[1] for item in argv
        if item.startswith("permissions.code_cycle_publish_write.filesystem=")
    )
    pairs = re.findall(r'("(?:\\.|[^"])*")=("(?:\\.|[^"])*")', value)
    rules = {json.loads(path): json.loads(access) for path, access in pairs}
    assert len(rules) == len(pairs), "a path appears twice in the profile"
    return rules


class LinkedRepository(unittest.TestCase):
    """A real repository with one linked worktree and a bare remote."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Cycle Test", "GIT_AUTHOR_EMAIL": "cycle@example.invalid",
            "GIT_COMMITTER_NAME": "Cycle Test", "GIT_COMMITTER_EMAIL": "cycle@example.invalid",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.remote = self.root / "remote.git"
        self.main = self.root / "repo"
        self.workspace = self.root / "repo-task"
        self.git(self.root, "init", "--bare", "-b", "main", str(self.remote))
        self.git(self.root, "init", "-b", "main", str(self.main))
        (self.main / "README").write_text("hello\n")
        self.git(self.main, "add", "README")
        self.git(self.main, "commit", "-m", "initial")
        self.git(self.main, "remote", "add", "origin", str(self.remote))
        self.git(self.main, "push", "origin", "main")
        self.git(self.main, "worktree", "add", "-b", "task", str(self.workspace))
        self.common = self.main / ".git"
        self.private = self.common / "worktrees" / self.workspace.name
        self.roots = (str(self.common), str(self.private))

    def git(self, cwd: Path, *args: str, check: bool = True, env=None) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, env=env,
        )
        if check and completed.returncode:
            self.fail(f"git {' '.join(args)} failed: {completed.stderr}")
        return completed

    def profile(self, roots=None) -> dict[str, str]:
        argv = ex.CodexAdapter().argv(
            TARGET, "implement", cwd=str(self.workspace), writes=True, publishes=True,
            writable_dirs=roots or self.roots,
        )
        return filesystem_rules(argv)


class ProfileTests(LinkedRepository):
    def test_common_root_is_read_only_and_every_rule_names_an_existing_path(self) -> None:
        rules = self.profile()

        self.assertEqual({
            str(self.common): "read",
            str(self.common / "objects"): "write",
            str(self.common / "refs"): "write",
            str(self.common / "logs"): "write",
            str(self.private): "write",
            str(self.private / "commondir"): "read",
            str(self.private / "gitdir"): "read",
            str(self.workspace / ".git"): "read",
            # The main worktree's reflog sits below the writable `logs` root.
            str(self.common / "logs" / "HEAD"): "read",
        }, rules)
        for path in rules:
            self.assertTrue(Path(path).exists(), path)

    def test_existing_protected_state_stays_read_only_and_absent_state_is_not_named(self) -> None:
        (self.common / "refs" / "bisect").mkdir()
        (self.common / "logs" / "refs" / "rewritten").write_text("")
        (self.private / "config.worktree").write_text("[core]\n")

        rules = self.profile()

        self.assertEqual("read", rules[str(self.common / "refs" / "bisect")])
        self.assertEqual("read", rules[str(self.common / "logs" / "refs" / "rewritten")])
        self.assertEqual("read", rules[str(self.private / "config.worktree")])
        for absent in (
            self.common / "commondir", self.common / "gitdir", self.common / "modules",
            self.common / "MERGE_HEAD", self.common / "index.lock",
            self.common / "refs" / "worktree", self.private / "config.worktree.lock",
            self.private / "hooks",
        ):
            self.assertNotIn(str(absent), rules)

    def test_main_worktree_profile_fails_closed_and_the_probe_routes_it_away(self) -> None:
        rules = self.profile((str(self.common),))

        self.assertEqual({
            str(self.common): "read",
            str(self.common / "objects"): "write",
            str(self.common / "refs"): "write",
            str(self.common / "logs"): "write",
        }, rules)
        allowed, detail = ex.CodexAdapter().probe_git_metadata_write(
            TARGET, str(self.main), (str(self.common),),
            runner=lambda *args, **kwargs: self.fail("unexpected sandbox launch"),
        )
        self.assertFalse(allowed)
        self.assertIn("main worktree", detail)

    def test_publishing_dispatch_disables_automatic_git_maintenance(self) -> None:
        seen = {}

        def runner(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.dict(os.environ, {"GIT_CONFIG_PARAMETERS": "'user.useconfigonly=true'"}):
            ex.CodexAdapter().dispatch(
                TARGET, "implement", cwd=str(self.workspace), runner=runner,
                writes=True, publishes=True, writable_dirs=self.roots,
            )
        parameters = seen["env"]["GIT_CONFIG_PARAMETERS"]
        self.assertIn("'user.useconfigonly=true'", parameters)
        self.assertIn("'gc.auto=0'", parameters)
        self.assertIn("'maintenance.auto=false'", parameters)

        seen.clear()
        ex.CodexAdapter().dispatch(
            TARGET, "review", cwd=str(self.workspace), runner=runner,
            writes=False, publishes=True, writable_dirs=self.roots,
        )
        self.assertNotIn("env", seen)


class RouteAwayTests(LinkedRepository):
    def probe(self):
        return ex.CodexAdapter().probe_git_metadata_write(
            TARGET, str(self.workspace), self.roots,
            runner=lambda *args, **kwargs: self.fail("unexpected sandbox launch"),
        )

    def test_worktree_config_without_its_private_files_routes_away(self) -> None:
        self.git(self.main, "config", "extensions.worktreeConfig", "true")

        allowed, detail = self.probe()

        self.assertFalse(allowed)
        self.assertIn("extensions.worktreeConfig", detail)
        self.assertIn("config.worktree", detail)
        self.assertFalse((self.private / "config.worktree").exists())

    def test_worktree_config_with_existing_private_files_is_protected(self) -> None:
        self.git(self.main, "config", "extensions.worktreeConfig", "true")
        (self.private / "config.worktree").write_text("")
        (self.private / "config.worktree.lock").write_text("")

        self.assertIsNone(ex.CodexAdapter().unprotectable_git_paths(
            str(self.workspace), self.roots,
        ))
        rules = self.profile()
        self.assertEqual("read", rules[str(self.private / "config.worktree")])
        self.assertEqual("read", rules[str(self.private / "config.worktree.lock")])

    def test_hooks_path_inside_a_writable_git_directory_routes_away(self) -> None:
        cases = {
            "relative into refs": os.path.relpath(self.common / "refs" / "hooks", self.workspace),
            "absolute private": str(self.private / "hooks"),
        }
        for label, value in cases.items():
            with self.subTest(label):
                self.git(self.workspace, "config", "core.hooksPath", value)

                allowed, detail = self.probe()

                self.assertFalse(allowed)
                self.assertIn("core.hooksPath", detail)

    def test_worktree_specific_hooks_path_is_resolved(self) -> None:
        self.git(self.main, "config", "extensions.worktreeConfig", "true")
        self.git(self.workspace, "config", "--worktree", "core.hooksPath",
                 str(self.private / "hooks"))
        (self.private / "config.worktree.lock").write_text("")

        detail = ex.CodexAdapter().unprotectable_git_paths(str(self.workspace), self.roots)

        self.assertIsNotNone(detail)
        self.assertIn("core.hooksPath", detail)

    def test_default_or_workspace_hooks_are_not_route_away_triggers(self) -> None:
        adapter = ex.CodexAdapter()
        self.assertIsNone(adapter.unprotectable_git_paths(str(self.workspace), self.roots))
        self.git(self.workspace, "config", "core.hooksPath", ".githooks")
        self.assertIsNone(adapter.unprotectable_git_paths(str(self.workspace), self.roots))
        self.git(self.workspace, "config", "core.hooksPath", str(self.common / "hooks"))
        self.assertIsNone(adapter.unprotectable_git_paths(str(self.workspace), self.roots))

    def test_unreadable_configuration_fails_closed(self) -> None:
        detail = ex.CodexAdapter().unprotectable_git_paths(
            str(self.workspace), self.roots, git=lambda *args: (128, ""),
        )
        self.assertIn("could not read", detail)

    def test_cycle_reroutes_a_failed_probe_before_dispatch(self) -> None:
        # The routing itself lives in cycle.py and is covered by test_cycle;
        # this pins the detail it records for a dangerous absent path.
        self.git(self.main, "config", "extensions.worktreeConfig", "true")
        allowed, detail = self.probe()
        self.assertFalse(allowed)
        self.assertTrue(detail.startswith("extensions.worktreeConfig is enabled"))


class ProbeMarkerTests(LinkedRepository):
    @staticmethod
    def write_markers(argv) -> list[Path]:
        written = []
        pattern = (r'create_exclusive\(("(?:\\.|[^"])*\.code-cycle-write-probe-[^"]*"), '
                   r'("(?:\\.|[^"])*")\)')
        for path, token in re.findall(pattern, argv[-1]):
            marker = Path(json.loads(path))
            marker.write_text(json.loads(token))
            written.append(marker)
        return written

    def markers(self) -> list[Path]:
        return list(self.common.rglob(".code-cycle-*"))

    def test_probe_script_checks_the_read_only_root_and_existing_pointers(self) -> None:
        seen = {}

        def runner(argv, **kwargs):
            seen["argv"], seen["kwargs"] = argv, kwargs
            self.write_markers(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        allowed, detail = ex.CodexAdapter().probe_git_metadata_write(
            TARGET, str(self.workspace), self.roots, runner=runner,
        )

        self.assertTrue(allowed, detail)
        argv, script = seen["argv"], seen["argv"][-1]
        self.assertEqual(["sandbox", "--permission-profile"], argv[1:3])
        self.assertNotIn("--add-dir", argv)
        self.assertEqual(15, seen["kwargs"]["timeout"])
        for directory in (self.common, self.common / "worktrees", self.common / "hooks"):
            self.assertIn(json.dumps(str(directory / ".code-cycle-deny-probe-"))[:-1], script)
        for path in (self.common / "config", self.private / "commondir",
                     self.private / "gitdir", self.workspace / ".git"):
            self.assertIn(f"os.open({json.dumps(str(path))}, os.O_WRONLY|os.O_APPEND)", script)
            self.assertIn(f"Path({json.dumps(str(path))}).read_bytes()", script)
        # Absent protected names are never opened, so they cannot reappear.
        for absent in ("commondir", "gitdir", "MERGE_HEAD", "index.lock", "modules"):
            self.assertNotIn(json.dumps(str(self.common / absent)), script)
        self.assertEqual([], self.markers())

    def test_leftover_synthetic_files_no_longer_block_the_probe(self) -> None:
        # The state this repository was left in by the incident's dispatches.
        leftovers = [self.common / "logs" / "refs" / name for name in ("bisect", "worktree")]
        for path in leftovers:
            path.write_text("")

        allowed, detail = ex.CodexAdapter().probe_git_metadata_write(
            TARGET, str(self.workspace), self.roots,
            runner=lambda argv, **kwargs: (self.write_markers(argv),
                                           subprocess.CompletedProcess(argv, 0, "", ""))[1],
        )

        self.assertTrue(allowed, detail)
        for path in leftovers:
            self.assertEqual("", path.read_text())

    def test_probe_keeps_a_marker_it_does_not_own(self) -> None:
        def runner(argv, **kwargs):
            foreign = self.write_markers(argv)[0]
            foreign.write_text("not this probe's token")
            seen.append(foreign)
            return subprocess.CompletedProcess(argv, 1, "", "simulated failure")

        seen: list[Path] = []
        allowed, detail = ex.CodexAdapter().probe_git_metadata_write(
            TARGET, str(self.workspace), self.roots, runner=runner,
        )

        self.assertFalse(allowed)
        self.assertIn("sandbox probe exited 1", detail)
        self.assertEqual("not this probe's token", seen[0].read_text())

    def test_interrupted_probe_removes_its_markers(self) -> None:
        def runner(argv, **kwargs):
            self.write_markers(argv)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            ex.CodexAdapter().probe_git_metadata_write(
                TARGET, str(self.workspace), self.roots, runner=runner,
            )
        self.assertEqual([], self.markers())

    def test_timed_out_probe_removes_its_markers(self) -> None:
        def runner(argv, **kwargs):
            self.write_markers(argv)
            raise subprocess.TimeoutExpired(argv, 15)

        allowed, detail = ex.CodexAdapter().probe_git_metadata_write(
            TARGET, str(self.workspace), self.roots, runner=runner,
        )
        self.assertFalse(allowed)
        self.assertIn("timed out", detail)
        self.assertEqual([], self.markers())


@unittest.skipUnless(POSIX, "the fake Codex executable uses a POSIX shebang")
class SandboxSideEffectTests(LinkedRepository):
    """Dispatch through a fake CLI that materialises missing mount destinations."""

    def setUp(self) -> None:
        super().setUp()
        binaries = self.root / "bin"
        binaries.mkdir()
        binary = binaries / "codex"
        binary.write_text(FAKE_CODEX)
        binary.chmod(0o755)
        self.log = self.root / "codex-log.json"
        environment = patch.dict(os.environ, {
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "FAKE_CODEX_LOG": str(self.log),
            "FAKE_CODEX_GIT_DIR": str(self.common),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def record(self) -> dict:
        return json.loads(self.log.read_text())

    def assert_git_still_works(self) -> None:
        status = self.git(self.workspace, "status", "--porcelain", check=False)
        self.assertEqual(0, status.returncode, status.stderr)
        main = self.git(self.main, "rev-parse", "--git-dir", "--git-common-dir", check=False)
        self.assertEqual(0, main.returncode, main.stderr)
        # An ordinary checkout must not turn into a linked worktree.
        self.assertEqual([".git", ".git"], main.stdout.split())

    def test_the_emulator_reproduces_the_incident(self) -> None:
        subprocess.run(
            ["codex", "sandbox", "-c",
             "permissions.code_cycle_publish_write.filesystem="
             f"{{{json.dumps(str(self.common / 'commondir'))}=\"read\"}}",
             "--", "true"],
            check=True, capture_output=True,
        )

        self.assertEqual(b"", (self.common / "commondir").read_bytes())
        broken = self.git(self.main, "status", check=False)
        self.assertNotEqual(0, broken.returncode)
        self.assertIn("commondir", broken.stderr)

    def test_probe_and_dispatch_leave_git_metadata_unchanged(self) -> None:
        def absent() -> None:
            pass

        def existing() -> None:
            (self.common / "refs" / "bisect").mkdir()
            (self.common / "logs" / "refs" / "rewritten").write_text("")
            (self.private / "config.worktree").write_text("[core]\n")

        for label, arrange in (("absent", absent), ("existing", existing)):
            with self.subTest(label):
                arrange()
                before = snapshot(self.common)

                allowed, detail = ex.CodexAdapter().probe_git_metadata_write(
                    TARGET, str(self.workspace), self.roots,
                )
                self.assertTrue(allowed, detail)
                self.assertEqual("sandbox", self.record()["command"])
                self.assertEqual(before, self.record()["during"])
                self.assertEqual(before, snapshot(self.common))

                result = ex.CodexAdapter().dispatch(
                    TARGET, "implement", cwd=str(self.workspace), timeout=60,
                    writes=True, publishes=True, writable_dirs=self.roots,
                )
                self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)
                record = self.record()
                self.assertEqual("exec", record["command"])
                self.assertEqual(before, record["during"])
                self.assertEqual(before, snapshot(self.common))
                self.assertNotIn(str(self.common), record["add_dirs"])
                self.assertIn("'gc.auto=0'", record["git_config_parameters"])
                self.assertIn("'maintenance.auto=false'", record["git_config_parameters"])
                self.assert_git_still_works()

    def test_failing_probe_leaves_git_metadata_unchanged(self) -> None:
        before = snapshot(self.common)
        with patch.dict(os.environ, {"FAKE_CODEX_EXIT": "7"}):
            allowed, detail = ex.CodexAdapter().probe_git_metadata_write(
                TARGET, str(self.workspace), self.roots,
            )
        self.assertFalse(allowed)
        self.assertIn("sandbox probe exited 7", detail)
        self.assertEqual(before, snapshot(self.common))


@unittest.skipUnless(POSIX and hasattr(os, "geteuid") and os.geteuid() != 0,
                     "directory permissions emulate the read-only root only for a non-root POSIX user")
class PublishingUnderReadOnlyRootTests(LinkedRepository):
    """The cycle's Git publishing operations need no write to the common root."""

    def read_only(self, *directories: Path) -> None:
        for directory in directories:
            mode = directory.stat().st_mode & 0o777
            directory.chmod(0o555)
            self.addCleanup(directory.chmod, mode)

    def test_commit_fetch_push_and_loose_ref_updates_work(self) -> None:
        main_state = {
            name: (self.common / name).read_bytes()
            for name in ("HEAD", "index", "logs/HEAD", "refs/heads/main")
        }
        root_entries = sorted(path.name for path in self.common.iterdir())
        self.read_only(self.common, self.common / "worktrees")
        environment = ex._sandbox_git_environment()

        (self.workspace / "change.txt").write_text("change\n")
        self.git(self.workspace, "add", "change.txt", env=environment)
        self.git(self.workspace, "commit", "-m", "change", env=environment)
        self.git(self.workspace, "update-ref", "refs/heads/scratch", "HEAD", env=environment)
        self.git(self.workspace, "fetch", "origin", env=environment)
        self.git(self.workspace, "push", "origin", "task", env=environment)

        pushed = self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "task")
        local = self.git(self.workspace, "rev-parse", "HEAD")
        self.assertEqual(local.stdout, pushed.stdout)
        self.assertEqual(root_entries, sorted(path.name for path in self.common.iterdir()))
        for name, content in main_state.items():
            self.assertEqual(content, (self.common / name).read_bytes(), name)
        # Documented limitation: packing refs needs the read-only root.
        packed = self.git(self.workspace, "pack-refs", "--all", check=False, env=environment)
        self.assertNotEqual(0, packed.returncode)


@unittest.skipUnless(POSIX, "process groups and signals are POSIX-only")
class StagedTerminationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def assert_gone(self, pid_file: Path) -> None:
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"descendant {pid} outlived its dispatch")

    def test_timeout_lets_the_group_tear_down_before_it_is_killed(self) -> None:
        marker, pid_file = self.root / "torn-down", self.root / "child.pid"
        script = (f'trap \'echo done > "{marker}"; exit 0\' TERM; '
                  f'sleep 30 & echo $! > "{pid_file}"; wait')

        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            ex._run(["sh", "-c", script], timeout=1)

        self.assertTrue(caught.exception.process_exited)
        self.assertEqual("done\n", marker.read_text())
        self.assert_gone(pid_file)

    def test_a_group_that_ignores_sigterm_is_killed_after_the_grace_period(self) -> None:
        pid_file = self.root / "child.pid"
        script = f'trap "" TERM; sleep 30 & echo $! > "{pid_file}"; wait'
        started = time.monotonic()

        with patch.object(ex, "PROCESS_GROUP_GRACE_SECONDS", 0.5):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                ex._run(["sh", "-c", script], timeout=1)

        self.assertTrue(caught.exception.process_exited)
        self.assertLess(time.monotonic() - started, 10)
        self.assert_gone(pid_file)

    def test_a_finished_dispatch_leaves_no_descendants(self) -> None:
        pid_file = self.root / "child.pid"
        started = time.monotonic()

        completed = ex._run(["sh", "-c", f'sleep 30 & echo $! > "{pid_file}"'], timeout=10)

        self.assertEqual(0, completed.returncode)
        self.assertTrue(completed.process_exited)
        self.assertLess(time.monotonic() - started, 5)
        self.assert_gone(pid_file)


if __name__ == "__main__":
    unittest.main()
