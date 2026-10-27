"""Real Git processes and local bare refs behind a fake sandbox transport."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import executors
import publication
from cycle_history import CycleHistoryStore
from stage_permissions import StagePolicy, check, allowed_tool_patterns


@unittest.skipIf(os.name == "nt", "fixture executables use POSIX shebangs")
class TransportBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "checkout"
        self.bare = self.root / "remote.git"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.real_git = shutil.which("git")
        self.git("init", "--bare", str(self.bare), cwd=self.root)
        self.git("init", str(self.repo), cwd=self.root)
        self.git("config", "user.name", "Publication Test")
        self.git("config", "user.email", "publication@example.invalid")
        self.git("checkout", "-b", "task-284")
        (self.repo / "fix.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "fix.txt")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("push", str(self.bare), "HEAD:refs/heads/task-284")
        self.git("remote", "add", "origin", "git@github.com:owner/repository.git")
        (self.repo / "fix.txt").write_text("fix\n", encoding="utf-8")
        self.git("commit", "-am", "fix")
        self.head = self.git("rev-parse", "HEAD")
        self.config = (self.repo / ".git/config").read_bytes()
        git_script = f'''#!{sys.executable}
import os, subprocess, sys
args = sys.argv[1:]
if args[:1] == ['ls-remote'] and '--get-url' in args:
    print(args[-1]); sys.exit(0)
if args[:2] == ['remote', 'get-url']:
    print('git@github.com:owner/repository.git'); sys.exit(0)
if 'push' in args:
    if os.environ.get('REJECT'):
        print('remote rejected push', file=sys.stderr); sys.exit(1)
    if 'origin' in args:
        if os.environ.get('SANDBOX'):
            print('ssh: Could not resolve hostname github.com', file=sys.stderr); sys.exit(128)
        if '--dry-run' in args:
            sys.exit(0)
    if os.environ.get('NO_UPDATE') and '--dry-run' not in args:
        sys.exit(0)
args = [arg.replace('https://github.com/owner/repository.git', {str(self.bare)!r}) for arg in args]
sys.exit(subprocess.run([{self.real_git!r}, *args]).returncode)
'''
        gh_script = f'''#!{sys.executable}
import json, subprocess, sys
if sys.argv[1:3] == ['auth', 'status']:
    sys.exit(0)
if sys.argv[1:3] == ['pr', 'view']:
    head = subprocess.check_output([{self.real_git!r}, '--git-dir', {str(self.bare)!r}, 'rev-parse', 'refs/heads/task-284'], text=True).strip()
    print(json.dumps({{'headRefOid': head}})); sys.exit(0)
print(json.dumps({{'viewerPermission': 'WRITE', 'nameWithOwner': 'owner/repository'}}))
'''
        for name, content in (("git", git_script), ("gh", gh_script)):
            path = self.bin / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)
        self.environment = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                            "SANDBOX": "1"}
        self.expected_gh_environment = {
            "GH_TELEMETRY": os.environ.get("GH_TELEMETRY", "false"),
            "GH_NO_UPDATE_NOTIFIER": os.environ.get("GH_NO_UPDATE_NOTIFIER", "1"),
        }
        self.gh_environments = []

    def git(self, *args, cwd=None):
        return subprocess.run([self.real_git, *args], cwd=cwd or self.repo,
                              capture_output=True, text=True, check=True).stdout.strip()

    def run_process(self, argv, **options):
        if argv and argv[0] == "gh":
            self.gh_environments.append(options.get("env", {}).copy())
        options["env"] = {**options.get("env", {}), **self.environment}
        return subprocess.run(argv, **options)

    def publish(self):
        return publication.transport("push", "owner/repository", "main",
                                     cwd=str(self.repo), run=self.run_process)

    def test_runtime_gh_calls_receive_telemetry_defaults(self):
        self.publish()

        self.assertTrue(self.gh_environments)
        for environment in self.gh_environments:
            self.assertEqual(self.expected_gh_environment, {
                name: environment[name] for name in self.expected_gh_environment
            })

    def test_host_preflight_passes_but_sandbox_ssh_fails_and_https_recovers(self):
        host = {**self.environment}
        host.pop("SANDBOX")
        with patch.dict(os.environ, host, clear=True):
            self.assertTrue(executors._publication_preflight(str(self.repo))[0])
        preflight = publication.transport("preflight", "owner/repository", "main",
                                          cwd=str(self.repo), run=self.run_process)
        self.assertEqual("ready", preflight["status"], preflight)
        result = self.publish()
        self.assertEqual("published", result["status"], result)
        failed = [attempt for attempt in result["attempts"] if attempt["exit_code"]]
        self.assertIn("Could not resolve hostname", failed[0]["error"])
        self.assertEqual(self.head, result["remote_head_sha"])
        self.assertEqual(self.head, self.git("--git-dir", str(self.bare), "rev-parse", "refs/heads/task-284"))
        pull = self.run_process(["gh", "pr", "view", "7", "--repo", "owner/repository",
                                 "--json", "headRefOid"], capture_output=True, text=True)
        self.assertEqual(self.head, json.loads(pull.stdout)["headRefOid"])
        self.assertEqual(self.config, (self.repo / ".git/config").read_bytes())
        self.assertEqual("published", self.publish()["status"])
        self.assertEqual("2", self.git("rev-list", "--count", "HEAD"))

    def test_rejected_push_preserves_commit_and_original_error(self):
        self.environment["REJECT"] = "1"
        result = self.publish()
        self.assertEqual("failed", result["status"])
        self.assertGreaterEqual(sum(bool(attempt["exit_code"]) for attempt in result["attempts"]), 2)
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_preflight_before_branch_creation_never_publishes_a_ref(self):
        for branch in ("main", "master", "release"):
            with self.subTest(branch=branch):
                self.git("checkout", "-B", branch)
                before = self.git("--git-dir", str(self.bare), "show-ref")
                result = publication.transport("preflight", "owner/repository", "release",
                                               cwd=str(self.repo), run=self.run_process)
                self.assertEqual("ready", result["status"], result)
                self.assertEqual(before, self.git("--git-dir", str(self.bare), "show-ref"))
                pushed = publication.transport("push", "owner/repository", "release",
                                               cwd=str(self.repo), run=self.run_process)
                self.assertEqual("failed", pushed["status"], pushed)
                self.assertFalse(any("push" in item["command"] for item in pushed["attempts"]))
        self.git("checkout", "--detach")
        result = publication.transport("preflight", "owner/repository", "main",
                                       cwd=str(self.repo), run=self.run_process)
        self.assertEqual("ready", result["status"], result)
        self.assertEqual(before, self.git("--git-dir", str(self.bare), "show-ref"))
        pushed = self.publish()
        self.assertEqual("failed", pushed["status"], pushed)
        self.assertFalse(any("push" in item["command"] for item in pushed["attempts"]))

    def test_exit_zero_without_remote_update_is_failure(self):
        self.environment["NO_UPDATE"] = "1"
        result = self.publish()
        self.assertEqual("failed", result["status"])
        self.assertEqual(self.base, result["remote_head_sha"])

    def test_timed_out_original_push_also_recovers(self):
        def run(argv, **options):
            if argv[:2] == ["git", "push"] and "origin" in argv:
                raise subprocess.TimeoutExpired(argv, 60)
            return self.run_process(argv, **options)

        result = publication.transport("push", "owner/repository", "main",
                                       cwd=str(self.repo), run=run)
        self.assertEqual("published", result["status"], result)
        self.assertTrue(any(attempt["exit_code"] == 124 for attempt in result["attempts"]))

    def test_other_repository_is_never_pushed(self):
        result = publication.transport("push", "other/repository", "main",
                                       cwd=str(self.repo), run=self.run_process)
        self.assertEqual("failed", result["status"])
        self.assertFalse(any("push" in item["command"] for item in result["attempts"]))

    def test_cli_runs_in_current_environment_and_preserves_evidence(self):
        environment = {**self.environment, "TMPDIR": str(self.root)}
        script = Path(publication.__file__)
        result = subprocess.run([sys.executable, str(script), "push", "--repo",
                                 "owner/repository", "--base", "main"], cwd=self.repo,
                                env=environment, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        log = json.loads((self.root / "publication.log").read_text())
        self.assertEqual(self.head, log["remote_head_sha"])
        self.assertEqual(json.loads(result.stdout), log)

    def test_native_dispatch_retains_transport_log_before_scratch_cleanup(self):
        policy = StagePolicy(role="implement", repo="owner/repository", base_branch="main",
                             runtime_dir=str(Path(publication.__file__).parent))
        observed = []

        def stage(argv, **options):
            environment = {**self.environment, **options["env"], "PATH": self.environment["PATH"]}
            observed.append(Path(environment["TMPDIR"]))
            self.assertIn("code_cycle_publish_write", " ".join(argv))
            self.assertIn("preflight", argv[-1])
            for action in ("preflight", "push"):
                result = subprocess.run([sys.executable, publication.__file__, action,
                                         "--repo", "owner/repository", "--base", "main"],
                                        cwd=self.repo, env=environment, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"type": "turn.completed"}), "")

        result = executors.CodexAdapter().dispatch(
            executors.Target("codex", "openai", "fixture-model", "medium"), "implement",
            cwd=str(self.repo), writes=True, publishes=True, command_policy=policy, runner=stage)
        self.assertEqual(executors.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)
        self.assertEqual(["ready", "published"],
                         [event["status"] for event in result.artifacts["publication_transport"]])
        self.assertFalse(observed[0].exists())


class PublicationPolicyTests(unittest.TestCase):
    def test_unavailable_sandbox_never_allows_a_host_upload(self):
        policy = StagePolicy(role="resolve", repo="owner/repository", base_branch="main", static_only=True)
        self.assertFalse(policy.publication_commands)
        self.assertFalse(check("git push origin HEAD", policy).allowed)

    def test_helper_scope_is_exact_and_cannot_be_overridden(self):
        policy = StagePolicy(role="resolve", repo="owner/repository", base_branch="main")
        command = " ".join(policy.publication_commands[0])
        self.assertTrue(check(command, policy).allowed)
        self.assertFalse(check(command + " --repo other/repository", policy).allowed)
        self.assertFalse(check(command.replace("owner/repository", "other/repository"), policy).allowed)
        self.assertIn(f"Bash({command}:*)", allowed_tool_patterns(policy))
        self.assertFalse(StagePolicy(role="review", repo="owner/repository", base_branch="main").publication_commands)

    def test_runtime_attribution_is_scoped_and_chains_across_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CycleHistoryStore(Path(temporary) / "history.sqlite")
            store.record_unpublished_head("attempt-1", "owner/repository", "7", temporary,
                                          "task-284", "a" * 40, "b" * 40)
            store.record_unpublished_head("attempt-2", "owner/repository", "7", temporary,
                                          "task-284", "b" * 40, "c" * 40)
            self.assertEqual(("a" * 40, "c" * 40), store.unpublished_heads(
                "owner/repository", "7", temporary, "task-284", "a" * 40))
            self.assertIsNone(store.unpublished_heads(
                "owner/repository", "8", temporary, "task-284", "a" * 40))
            self.assertIsNone(store.unpublished_heads(
                "owner/repository", "7", temporary, "another-branch", "a" * 40))


if __name__ == "__main__":
    unittest.main()
