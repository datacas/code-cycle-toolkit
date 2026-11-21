"""#456: direct baselines and exact consent through the shared hook, per executor.

These are process-boundary tests, not native agent runs. gh and docker are
fixture executables; native PA-22 proof requires a real fixture repository.
"""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import command_permissions as cp
import grants
import stage_permissions as sp


@unittest.skipIf(os.name == "nt", "POSIX fixture executables; Linux CI runs both executor cases")
class TableConsentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("gh", "docker"):
            binary = self.bin / name
            binary.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
            binary.chmod(0o755)
        env = patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)
        self.run_command("git init")
        self.policy = sp.StagePolicy("implement", tmpdir=str(self.root), repo="owner/repo", change_request_id="1", docker=True)

    def run_command(self, command):
        return subprocess.run(shlex.split(command), cwd=self.root, capture_output=True, text=True, timeout=30)

    def hook(self, policy, command):
        path = self.root / "policy.json"
        path.write_text(policy.to_json())
        result = subprocess.run([sys.executable, str(Path(sp.__file__)), "hook", str(path)],
            cwd=self.root, input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command},
                                            "cwd": str(self.root)}), capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        return not result.stdout

    def parity(self, executor, row):
        commands = {"PA-20": "git ls-tree --name-only --empty",  # replaced below by a real tree
                    "PA-21": "gh search issues fixture --repo owner/other",
                    "PA-22": "gh issue create --repo owner/fixture --title fixture --body fixture",
                    "PA-23": "git -c alias.fixture=status fixture --untracked-files=no",
                    "PA-24": "docker compose run --rm fixture"}
        if row == "PA-20":
            tree = self.run_command("git hash-object -t tree /dev/null").stdout.strip()
            commands[row] = "git ls-tree " + tree
            # hash-object does not store without -w.
            self.run_command("git hash-object -w -t tree /dev/null")
        command = commands[row]
        direct = self.run_command(command)
        self.assertEqual(0, direct.returncode, direct.stderr)
        resolver = patch.object(sp, "COMPOSE_RESOLVER", side_effect=sp.ComposeResolutionTimeout("budget expired"))
        with resolver:
            self.assertFalse(sp.check(command, self.policy, cwd=str(self.root)).allowed)
            store = grants.ConsentStore(self.root / "consents.json")
            broker = grants.ConsentBroker("owner/repo", store=store, platform="linux", cycle_id="cycle", task_id="456")
            request = cp.refused_operation([command], self.policy, broker, executor, str(self.root))
            self.assertIsNotNone(request)
            grant = broker.operations[request["resource"]]
            self.assertTrue(grant.operation["guard_consent"])
            asked = grants.question(grant, grant.scopes[0], role="implement", blocker=request)
            self.assertIn("Exact", asked["prompt"])
            broker.apply_answer(grant, grant.scopes[0], "cycle" if row == "PA-24" else "persistent")
            entries = broker.command_entries(executor, "implement")
            allowed = replace(self.policy, command_permissions=entries)
            self.assertTrue(sp.check(command, allowed, cwd=str(self.root)).allowed)
            if row != "PA-24":  # the subprocess hook has no resolver mock
                self.assertTrue(self.hook(allowed, command))
            else:
                path = self.root / "timeout-policy.json"
                path.write_text(allowed.to_json())
                output = io.StringIO()
                sp.hook_main([str(path)], io.StringIO(json.dumps({"tool_name": "Bash",
                    "tool_input": {"command": command}, "cwd": str(self.root)})), output)
                self.assertEqual("", output.getvalue())
            guarded = self.run_command(command)
            self.assertEqual((direct.returncode, direct.stdout), (guarded.returncode, guarded.stdout))
            broker.record_executions([command], executor, allowed, str(self.root))
            [saved] = store.list(broker.cycle_scope if row == "PA-24" else "owner/repo")
            self.assertEqual("allow", saved["decision"])
            self.assertEqual(command, saved["executed_commands"][0]["command"])
            self.assertFalse(sp.check(command + " --different", allowed, cwd=str(self.root)).allowed)

    def test_claude_pa20_direct_baseline(self): self.parity("claude", "PA-20")
    def test_codex_pa20_direct_baseline(self): self.parity("codex", "PA-20")
    def test_claude_pa21_direct_baseline(self): self.parity("claude", "PA-21")
    def test_codex_pa21_direct_baseline(self): self.parity("codex", "PA-21")
    def test_claude_pa22_direct_baseline(self): self.parity("claude", "PA-22")
    def test_codex_pa22_direct_baseline(self): self.parity("codex", "PA-22")
    def test_claude_pa23_direct_baseline(self): self.parity("claude", "PA-23")
    def test_codex_pa23_direct_baseline(self): self.parity("codex", "PA-23")
    def test_claude_pa24_direct_baseline(self): self.parity("claude", "PA-24")
    def test_codex_pa24_direct_baseline(self): self.parity("codex", "PA-24")

    def test_patterns_and_prior_roles_never_lift_converted_guards(self):
        for command in ("git ls-tree HEAD", "git -C . status", "GIT_PAGER=cat git status",
                        "gh issue list", "gh pr comment 2 --body fixture"):
            with self.subTest(command=command):
                op = sp.operation("Bash", {"command": command}, self.policy, str(self.root))
                pattern = grants.make_pattern_grant(".+", "implement", "claude", "linux",
                    proposer="test", justification="test exact guard", positive=[command], negative=["\n"])
                current = replace(self.policy, command_permissions=({"operation": pattern.operation, "decision": "allow"},))
                self.assertFalse(sp.check(command, current, cwd=str(self.root)).allowed)
                self.assertFalse(grants.pattern_covers_operation(pattern.operation, op))
                review = replace(self.policy, role="review", command_permissions=({"operation": op, "decision": "allow"},))
                self.assertIsNone(sp.matching_answer("Bash", {"command": command}, review, str(self.root)))
        for command in ("gh issue list", "git branch --list", "git stash list", "git config --get user.name"):
            review = replace(self.policy, role="review")
            op = sp.operation("Bash", {"command": command}, review, str(self.root))
            fresh = replace(review, command_permissions=({"operation": op, "decision": "allow"},))
            self.assertTrue(sp.check(command, fresh, cwd=str(self.root)).allowed)

    def test_real_compose_failures_are_not_consent(self):
        for error in ("missing Compose file", "missing env_file", "docker executable unavailable"):
            with self.subTest(error=error), patch.object(sp, "COMPOSE_RESOLVER", side_effect=sp.PolicyError(error)):
                command = "docker compose run fixture"
                decision = sp.check(command, self.policy, cwd=str(self.root))
                self.assertEqual("compose_unresolved", decision.rule)
                self.assertEqual(error, decision.reason)
                self.assertIsNone(cp.refused_operation([command], self.policy,
                    grants.ConsentBroker("owner/repo", platform="linux"), "claude", str(self.root)))

    def test_timeout_cannot_be_persistent_or_pattern(self):
        with patch.object(sp, "COMPOSE_RUN", side_effect=subprocess.TimeoutExpired(["docker"], 1)):
            op = sp.operation("Bash", {"command": "docker compose run fixture"}, self.policy, str(self.root))
        grant = grants.make_grant(op, "claude", "linux")
        self.assertIsNotNone(grants.from_entry({"operation": op, "executor": "claude", "platform": "linux"}))
        self.assertIn("were not inspected", grant.scopes[0].risks)
        self.assertIn("without a configuration digest", grants.granted_context(grant, grant.scopes[0]))
        broker = grants.ConsentBroker("owner/repo", platform="linux")
        for answer in ("persistent", "forever"):
            with self.assertRaises(grants.GrantError): broker.apply_answer(grant, grant.scopes[0], answer)
        question = grants.question(grant, grant.scopes[0], role="implement", blocker={"command": "docker compose run fixture"})
        self.assertEqual(["this cycle", "deny"], question["options"])
        store = grants.ConsentStore(self.root / "invalid-persistent.json")
        store.save("owner/repo", grant, grant.scopes[0], "allow")
        broker = grants.ConsentBroker("owner/repo", platform="linux", store=store,
                                      preauthorized=frozenset({grant.id}))
        broker.operations[grant.id] = grant
        self.assertEqual((), broker.command_entries("claude", "implement"))
        self.assertEqual(grants.PENDING, broker.request(grant, "claude", role="implement"))

    def test_role_and_publication_contracts_still_hold(self):
        for role, command in (("review", "gh issue create --title fixture"),
                ("issue_review", "gh pr comment 1 --body fixture"),
                ("review", "git stash"), ("review", "git worktree add fixture"),
                ("implement", "git -c color.ui=never push --force origin main"),
                ("implement", "git -C . branch -D fixture"),
                ("implement", "gh pr merge 1"), ("implement", "gh pr close 1"),
                ("implement", "gh pr review 1 --approve")):
            policy = replace(self.policy, role=role)
            with self.subTest(role=role, command=command):
                self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, policy, str(self.root)))

    def test_git_aliases_require_disclosed_interpreter_consent(self):
        # Git configuration is executable input. Do not claim to inspect its
        # hidden operations, or execute these destructive examples in a test.
        commands = (
            "git -c 'alias.x=push --force origin main' x",
            "git -c 'alias.y=branch -D' y topic",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.x "
            "GIT_CONFIG_VALUE_0='push --force origin main' git x",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.y "
            "GIT_CONFIG_VALUE_0='branch -D' git y topic",
        )
        for executor in ("claude", "codex"):
            for command in commands:
                with self.subTest(executor=executor, command=command):
                    self.assertFalse(self.hook(self.policy, command))
                    broker = grants.ConsentBroker("owner/repo", platform="linux")
                    request = cp.refused_operation([command], self.policy, broker, executor, str(self.root))
                    self.assertIsNotNone(request)
                    grant = broker.operations[request["resource"]]
                    self.assertTrue(grant.operation["guard_consent"])
                    question = grants.question(grant, grant.scopes[0], role="implement", blocker=request)
                    self.assertIn("interpreter consent", question["prompt"])
                    self.assertIn("publication guards cannot inspect hidden operations", question["prompt"])
                    self.assertIn("publication guards cannot inspect hidden operations", grant.scopes[0].risks)
                    broker.apply_answer(grant, grant.scopes[0], "cycle")
                    allowed = replace(self.policy, command_permissions=broker.command_entries(executor, "implement"))
                    self.assertTrue(self.hook(allowed, command))
                    self.assertFalse(self.hook(allowed, command + " different"))

    def test_git_attr_source_keeps_direct_publication_guards(self):
        # These commands are inspected, never executed against a remote.
        for executor in ("claude", "codex"):
            for option in ("--attr-source HEAD", "--attr-source=HEAD"):
                for operation in ("push --force origin main", "push origin HEAD:main",
                                  "branch -D topic", "tag -d fixture", "update-ref -d refs/heads/topic"):
                    command = f"git {option} {operation}"
                    with self.subTest(executor=executor, command=command):
                        reason = sp.protected_reason("Bash", {"command": command}, self.policy, str(self.root))
                        self.assertIsNotNone(reason)
                        op = sp.operation("Bash", {"command": command}, self.policy, str(self.root))
                        allowed = replace(self.policy, command_permissions=({"operation": op, "decision": "allow"},))
                        self.assertFalse(self.hook(allowed, command))
                        self.assertIsNone(cp.refused_operation([command], self.policy,
                            grants.ConsentBroker("owner/repo", platform="linux"), executor, str(self.root)))
        self.assertIn("needs a value", sp.protected_reason("Bash", {"command": "git --attr-source"},
                                                         self.policy, str(self.root)) or "")

    def test_git_shallow_file_keeps_direct_publication_guards(self):
        # Reproduce REV-004 through inspection and the hook, never execute a
        # force-push or deletion. The exact grant must not erase role guards.
        for executor in ("claude", "codex"):
            for role in ("implement", "resolve"):
                policy = replace(self.policy, role=role)
                for operation in ("push --force origin main", "branch -D topic"):
                    command = f"git --shallow-file HEAD {operation}"
                    with self.subTest(executor=executor, role=role, command=command):
                        self.assertIsNotNone(sp.protected_reason(
                            "Bash", {"command": command}, policy, str(self.root)))
                        op = sp.operation("Bash", {"command": command}, policy, str(self.root))
                        allowed = replace(policy, command_permissions=({"operation": op, "decision": "allow"},))
                        self.assertFalse(self.hook(allowed, command))
                        self.assertIsNone(cp.refused_operation([command], policy,
                            grants.ConsentBroker("owner/repo", platform="linux"), executor, str(self.root)))

    def test_git_global_options_do_not_guess_value_arity(self):
        value_options = ("--shallow-file HEAD", "--shallow-file=HEAD",
                         "--super-prefix fixture/", "--super-prefix=fixture/",
                         "--attr-source HEAD", "--attr-source=HEAD",
                         "--config-env fixture=FIXTURE", "--config-env=fixture=FIXTURE")
        flags = ("-p", "--paginate", "-P", "--no-pager", "--bare",
                 "--no-replace-objects", "--no-lazy-fetch", "--no-optional-locks", "--no-advice",
                 "--literal-pathspecs", "--no-literal-pathspecs", "--glob-pathspecs",
                 "--noglob-pathspecs", "--icase-pathspecs", "--exec-path=/fixture")
        for option in (*value_options, *flags):
            for operation, expected in (("push --force origin main", "never allowed"),
                                        ("branch -D topic", "deletion of Git refs")):
                command = f"git {option} {operation}"
                with self.subTest(command=command):
                    self.assertIn(expected, sp.protected_reason(
                        "Bash", {"command": command}, self.policy, str(self.root)) or "")
        for option in ("--shallow-file", "--super-prefix"):
            with self.subTest(missing=option):
                self.assertIn("needs a value", sp.protected_reason(
                    "Bash", {"command": f"git {option}"}, self.policy, str(self.root)) or "")
        for option in ("--future-global-option", "--future-global-option=HEAD", "-z"):
            command = f"git {option} HEAD push --force origin main"
            with self.subTest(unknown=option):
                self.assertIsNone(sp.protected_reason("Bash", {"command": command}, self.policy, str(self.root)))
                self.assertIn("opaque", sp.guard_consent_reason(
                    "Bash", {"command": command}, self.policy, str(self.root)) or "")

    def test_git_global_queries_stop_before_workspace_commands(self):
        for query in ("--version", "-v", "--help", "-h", "--exec-path", "--html-path",
                      "--man-path", "--info-path", "--list-cmds=main"):
            command = f"git {query} push --force origin main"
            with self.subTest(query=query), patch.object(sp.subprocess, "run",
                    side_effect=AssertionError("query inspection ran a program")):
                self.assertIsNone(sp.protected_reason("Bash", {"command": command}, self.policy, str(self.root)))

    def test_git_unknown_global_option_retains_disclosed_exact_consent(self):
        # PA-23 permits a user-selected Git wrapper with its own option grammar.
        # Its harmless direct baseline must remain executable after consent.
        (self.bin / "git").symlink_to("/bin/echo")
        command = f"PATH={shlex.quote(str(self.bin))} git --fixture-global-option HEAD status"
        direct = subprocess.run(["git", "--fixture-global-option", "HEAD", "status"],
            cwd=self.root, env={**os.environ, "PATH": str(self.bin)},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(0, direct.returncode, direct.stderr)
        self.assertEqual("--fixture-global-option HEAD status\n", direct.stdout)
        for executor in ("claude", "codex"):
            with self.subTest(executor=executor):
                self.assertFalse(self.hook(self.policy, command))
                broker = grants.ConsentBroker("owner/repo", platform="linux")
                request = cp.refused_operation([command], self.policy, broker, executor, str(self.root))
                self.assertIsNotNone(request)
                grant = broker.operations[request["resource"]]
                question = grants.question(grant, grant.scopes[0], role="implement", blocker=request)
                self.assertIn("Unknown Git option arity", question["prompt"])
                self.assertIn("opaque", grant.scopes[0].risks)
                broker.apply_answer(grant, grant.scopes[0], "cycle")
                allowed = replace(self.policy, command_permissions=broker.command_entries(executor, "implement"))
                self.assertTrue(self.hook(allowed, command))
                self.assertFalse(self.hook(allowed, command + " different"))
        for guarded in (command + " > " + shlex.quote(str(self.root.parent / "outside.txt")),
                        command + " " + shlex.quote(str(Path.home() / ".codex/auth.json"))):
            self.assertIsNotNone(sp.protected_reason("Bash", {"command": guarded}, self.policy, str(self.root)))

    def test_git_shallow_file_read_retains_exact_consent(self):
        command = "git --shallow-file HEAD status --porcelain"
        direct = self.run_command(command)
        self.assertEqual(0, direct.returncode, direct.stderr)
        for executor in ("claude", "codex"):
            with self.subTest(executor=executor):
                self.assertFalse(self.hook(self.policy, command))
                broker = grants.ConsentBroker("owner/repo", platform="linux")
                request = cp.refused_operation([command], self.policy, broker, executor, str(self.root))
                self.assertIsNotNone(request)
                grant = broker.operations[request["resource"]]
                broker.apply_answer(grant, grant.scopes[0], "cycle")
                allowed = replace(self.policy, command_permissions=broker.command_entries(executor, "implement"))
                self.assertTrue(self.hook(allowed, command))
                self.assertFalse(self.hook(allowed, command.replace("HEAD", "other")))

    def test_runner_wrappers_keep_direct_guards(self):
        for executor in ("claude", "codex"):
            for prefix in ("timeout 5", "timeout -s KILL -k 2s 5s", "nice -n 5", "command --",
                           "env FIXTURE=1", "env -i -u FIXTURE", "env -C . timeout 5 nice -n 5",
                           "rtk proxy timeout --signal=KILL 5"):
                for operation in ("gh auth token", "gh auth status -at", "git push --force origin main",
                                  "git branch -D fixture", "gh pr merge 1"):
                    command = f"{prefix} {operation}"
                    with self.subTest(executor=executor, command=command):
                        self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, self.policy, str(self.root)))
                        op = sp.operation("Bash", {"command": command}, self.policy, str(self.root))
                        allowed = replace(self.policy, command_permissions=({"operation": op, "decision": "allow"},))
                        self.assertFalse(self.hook(allowed, command))
                        self.assertIsNone(cp.refused_operation([command], self.policy,
                            grants.ConsentBroker("owner/repo", platform="linux"), executor, str(self.root)))

    def test_runner_reads_keep_whole_command_consent(self):
        for executor in ("claude", "codex"):
            command = "timeout 5 git --attr-source HEAD status"
            with self.subTest(executor=executor):
                broker = grants.ConsentBroker("owner/repo", platform="linux")
                request = cp.refused_operation([command], self.policy, broker, executor, str(self.root))
                self.assertIsNotNone(request)
                grant = broker.operations[request["resource"]]
                self.assertTrue(grant.operation["guard_consent"])
                broker.apply_answer(grant, grant.scopes[0], "cycle")
                allowed = replace(self.policy, command_permissions=broker.command_entries(executor, "implement"))
                self.assertTrue(self.hook(allowed, command))
                self.assertFalse(self.hook(allowed, command.replace("timeout 5", "timeout 6")))
        for command in ("echo timeout 5 gh auth token", "printf gh auth token", "command -v gh"):
            self.assertIsNone(sp.protected_reason("Bash", {"command": command}, self.policy, str(self.root)))

    def test_runner_directory_and_environment_reach_git_inspector(self):
        command = "env -C /fixture GIT_DIR=repo.git timeout 5 git push origin HEAD"
        with patch.object(sp.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "main\n", "")) as run:
            self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, self.policy, str(self.root)))
        self.assertEqual("/fixture", run.call_args.kwargs["cwd"])
        self.assertEqual(["git", "--no-pager", "--git-dir=repo.git", "rev-parse", "--abbrev-ref", "HEAD"],
                         run.call_args.args[0])

    def test_runner_directory_keeps_relative_credential_guard(self):
        # Only inspect strings: never execute a read of a credential store.
        for reader in ("grep token", "rg token", "cat"):
            command = f"env -C {shlex.quote(str(Path.home()))} {reader} .codex/auth.json"
            with self.subTest(reader=reader):
                self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, self.policy, str(self.root)))
                self.assertIsNone(sp.protected_reason("Bash", {"command": "echo " + command}, self.policy, str(self.root)))

    def test_gh_credentials_stay_protected_after_exact_consent(self):
        commands = (
            "gh auth token", "gh auth status --show-token", "gh auth status -t",
            "gh --hostname github.com auth status -at", "gh auth status --show-token=true",
            "gh auth refresh", "gh auth logout", "gh auth setup-git", "gh auth login", "gh auth switch",
        )
        for role in ("implement", "resolve", "review", "rereview", "issue_review"):
            policy = replace(self.policy, role=role)
            for command in commands:
                with self.subTest(role=role, command=command):
                    self.assertIn("credential", sp.protected_reason("Bash", {"command": command}, policy, str(self.root)) or "")
                    op = sp.operation("Bash", {"command": command}, policy, str(self.root))
                    allowed = replace(policy, command_permissions=({"operation": op, "decision": "allow"},))
                    self.assertFalse(self.hook(allowed, command))
                    self.assertIsNone(cp.refused_operation([command], policy,
                        grants.ConsentBroker("owner/repo", platform="linux"), "codex", str(self.root)))
            self.assertTrue(self.hook(policy, "gh auth status"))

    def test_inspection_never_executes_consented_pager_or_config(self):
        command = "git --paginate -c core.pager=unsafe -C /fixture push origin HEAD"
        with patch.object(sp.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "topic\n", "")) as run:
            self.assertIsNone(sp.protected_reason("Bash", {"command": command}, self.policy, str(self.root)))
        self.assertEqual(["git", "--no-pager", "-C", "/fixture", "rev-parse", "--abbrev-ref", "HEAD"], run.call_args.args[0])
        with patch.object(sp.subprocess, "run", side_effect=AssertionError("identity ran a program")):
            self.assertIsNone(sp.guard_consent_reason("Bash", {"command": "git push origin HEAD"},
                                                      self.policy, str(self.root)))
        for method in ("-XPOST", "-X POST", "--method=POST", "-Fstate=closed"):
            command = "gh api repos/owner/repo/pulls/1 " + method
            policy = replace(self.policy, role="review")
            self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, policy, str(self.root)))

    def test_documentation_tracks_all_converted_rows(self):
        root = Path(__file__).resolve().parents[1]
        audit = (root / "docs/capability-parity-audit.md").read_text()
        page = (root / "docs/host-permissions.md").read_text()
        for row in range(20, 25):
            line = next(line for line in audit.splitlines() if line.startswith(f"| PA-{row} |"))
            self.assertIn("#456", line)
            self.assertIn("consent", line.lower())
        self.assertIn("#456", page)


if __name__ == "__main__":
    unittest.main()
