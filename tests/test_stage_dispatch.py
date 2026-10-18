"""How a dispatch carries its stage's command policy to each executor (#142)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import environment_recovery as recovery  # noqa: E402
import executors as ex  # noqa: E402
import router  # noqa: E402
import run_cycle as rc  # noqa: E402
import stage_permissions as sp  # noqa: E402
from test_support import isolate_host_environment  # noqa: E402

CLAUDE = router.parse_target("claude:anthropic/claude-sonnet-5 high")
CODEX = router.parse_target("codex:openai/gpt-6-luna high")
READY = {name: ex.ProbeResult(name, ex.Availability.READY, "test")
         for name in ("claude", "codex", "orca")}

_restore_host_environment = None


def setUpModule() -> None:
    global _restore_host_environment
    _restore_host_environment = isolate_host_environment()


def tearDownModule() -> None:
    if _restore_host_environment is not None:
        _restore_host_environment()


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def completed(stdout="{}", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def settings_of(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--settings") + 1])


def allowed_of(argv: list[str]) -> list[str]:
    index = argv.index("--allowedTools") + 1
    patterns = []
    while index < len(argv) and argv[index].startswith("Bash("):
        patterns.append(argv[index])
        index += 1
    return patterns


class DispatchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repo = self.base / "source"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "issue-142")
        git(self.repo, "config", "user.name", "Cycle Test")
        git(self.repo, "config", "user.email", "cycle@example.invalid")
        (self.repo / "tracked.txt").write_text("x\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "initial")
        # The user's Claude configuration is the test's, empty unless written.
        self.claude_home = self.base / "claude-home"
        self.claude_home.mkdir()
        for patcher in (mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude_home)}),
                        mock.patch.object(sp, "managed_settings_paths", lambda: ())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def policy(self, role="implement", **fields) -> sp.StagePolicy:
        return sp.StagePolicy(**{"role": role, "repo": "owner/repo", "change_request_id": "7",
                                 "work_item_id": "142", "base_branch": "main", **fields})


class ClaudePolicyTests(DispatchTestCase):
    def test_a_writer_gets_its_table_and_a_hook_outside_its_workspace(self) -> None:
        seen = {}

        def runner(argv, timeout=None, cwd=None, env=None):
            settings = settings_of(argv)
            [entry] = settings["hooks"]["PreToolUse"]
            command = entry["hooks"][0]["command"]
            policy_path = Path(command.rsplit(" ", 1)[1].strip("'\""))
            seen.update(argv=argv, env=env, policy_path=policy_path,
                        policy=json.loads(policy_path.read_text(encoding="utf-8")),
                        script=policy_path.with_name("stage_permissions.py").is_file())
            return completed()

        result = ex.ClaudeAdapter().dispatch(
            CLAUDE, "implement it", cwd=str(self.repo), writes=True, publishes=True,
            publication_permissions=("push_branch",), command_policy=self.policy(),
            runner=runner)

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("active", result.artifacts["command_policy"])
        patterns = allowed_of(seen["argv"])
        self.assertNotIn("Bash(gh:*)", patterns)
        self.assertNotIn("Bash(git:*)", patterns)
        self.assertIn("Bash(git push:*)", patterns)
        self.assertIn("Bash(gh pr create:*)", patterns)
        self.assertNotIn("--dangerously-skip-permissions", seen["argv"])
        self.assertTrue(seen["script"])
        tmpdir = Path(seen["env"]["TMPDIR"])
        self.assertEqual(str(tmpdir), seen["policy"]["tmpdir"])
        self.assertEqual("implement", seen["policy"]["role"])
        for writable in (self.repo, tmpdir):
            self.assertNotIn(writable, seen["policy_path"].parents)
        self.assertFalse(seen["policy_path"].parent.exists())
        self.assertNotIn("sandbox", settings_of(seen["argv"]))

    def test_a_tool_check_mod_stops_the_dispatch_before_it_starts(self) -> None:
        plugin = self.claude_home / "plugins" / "cache" / "mod"
        plugin.mkdir(parents=True)
        (plugin / "hooks.js").write_text("on('tool.check', () => 'allow')", encoding="utf-8")
        (self.claude_home / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"plugins": {"mod@market": [{"installPath": str(plugin)}]}}), encoding="utf-8")
        (self.claude_home / "settings.json").write_text(json.dumps(
            {"enabledPlugins": {"mod@market": True}}), encoding="utf-8")
        runner = mock.Mock(side_effect=AssertionError("dispatched with a tool.check mod"))

        result = ex.ClaudeAdapter().dispatch(
            CLAUDE, "implement it", cwd=str(self.repo), writes=True,
            command_policy=self.policy(), runner=runner)

        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
        self.assertEqual("precondition", result.error_code)
        self.assertEqual("not_started", result.start_state)
        self.assertIn("BLOCKED", result.detail)
        self.assertIn("mod@market", result.detail)
        runner.assert_not_called()

    def test_disabled_hooks_stop_the_dispatch(self) -> None:
        (self.claude_home / "settings.json").write_text(json.dumps({"disableAllHooks": True}),
                                                        encoding="utf-8")
        result = ex.ClaudeAdapter().dispatch(
            CLAUDE, "implement it", cwd=str(self.repo), writes=True,
            command_policy=self.policy(), runner=mock.Mock())
        self.assertEqual("precondition", result.error_code)
        self.assertIn("disableAllHooks", result.detail)

    def test_an_inactive_policy_keeps_the_earlier_tooling_and_says_so(self) -> None:
        seen = {}

        def runner(argv, timeout=None, cwd=None, env=None):
            seen["argv"] = argv
            return completed()

        result = ex.ClaudeAdapter().dispatch(
            CLAUDE, "implement it", cwd=str(self.repo), writes=True, publishes=True,
            publication_permissions=("push_branch",),
            command_policy=self.policy(code_host="bitbucket"), runner=runner)

        self.assertIn("Bash(gh:*)", seen["argv"])
        self.assertNotIn("--settings", seen["argv"])
        self.assertTrue(result.artifacts["command_policy"].startswith("inactive: "))
        self.assertIn("GitHub only", result.artifacts["command_policy"])

    def test_denied_commands_are_read_from_the_complete_stream(self) -> None:
        stream = "\n".join(json.dumps(event) for event in (
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": "python3 -m unittest"}},
                {"type": "tool_use", "id": "t2", "name": "Bash",
                 "input": {"command": "git status"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                 "content": [{"type": "text", "text": "code-cycle implement "
                              f"{sp.HOOK_DENIAL_MARKER}: not in the table"}]},
                {"type": "tool_result", "tool_use_id": "t2", "content": "clean"}]}},
            {"type": "result", "result": "BLOCKED", "permission_denials": [
                {"tool_name": "Bash", "tool_input": {"command": "docker compose up -d"}},
                {"tool_name": "Write", "tool_input": {"file_path": "/x"}},
                {"tool_name": "Bash", "tool_input": {"command": "python3 -m unittest"}}]},
        ))
        self.assertEqual({"permission_denials": ["python3 -m unittest", "docker compose up -d"]},
                         ex.ClaudeAdapter().extra_artifacts(stream))
        self.assertEqual({}, ex.ClaudeAdapter().extra_artifacts('{"type": "result"}'))

    def test_the_docker_grant_opens_compose_in_the_table(self) -> None:
        seen = {}

        def runner(argv, timeout=None, cwd=None, env=None):
            seen["argv"] = argv
            seen["policy"] = json.loads(Path(settings_of(argv)["hooks"]["PreToolUse"][0]
                                             ["hooks"][0]["command"].rsplit(" ", 1)[1]
                                             .strip("'\"")).read_text(encoding="utf-8"))
            return completed()

        for grants, expected in (((), False), (("docker_host",), True)):
            with self.subTest(grants=grants):
                ex.dispatch(
                    router.RoutingDecision("deep_coder", CLAUDE, router.RoutingMode.PRODUCTION),
                    "implement it", ex.Registry([ex.ClaudeAdapter()]), cwd=str(self.repo),
                    writes=True, runner=runner, probes=READY,
                    command_policy=self.policy(), sandbox_grants=grants)
                self.assertEqual(expected, "Bash(docker compose up:*)" in seen["argv"])
                self.assertEqual(expected, seen["policy"]["docker"])

    def test_a_reviewer_that_verifies_runs_in_a_sandboxed_clone(self) -> None:
        seen = {}

        def runner(argv, timeout=None, cwd=None, env=None):
            seen.update(argv=argv, cwd=cwd, env=env, prompt=argv[-1])
            return completed()

        policy = self.policy("review", verify_prefixes=("python3 -m unittest",))
        result = ex.dispatch(
            router.RoutingDecision("reviewer", CLAUDE, router.RoutingMode.PRODUCTION),
            "review the change", ex.Registry([ex.ClaudeAdapter()]), cwd=str(self.repo),
            runner=runner, probes=READY, command_policy=policy)

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)
        self.assertNotEqual(str(self.repo), seen["cwd"])
        sandbox = settings_of(seen["argv"])["sandbox"]
        self.assertTrue(sandbox["failIfUnavailable"])
        self.assertFalse(sandbox["allowUnsandboxedCommands"])
        self.assertEqual([seen["cwd"], seen["env"]["TMPDIR"]], sandbox["filesystem"]["allowWrite"])
        patterns = allowed_of(seen["argv"])
        self.assertIn("Bash(python3 -m unittest:*)", patterns)
        self.assertNotIn("Bash(git push:*)", patterns)
        self.assertNotIn("Bash(git fetch origin:*)", patterns)  # the clone has no remote
        self.assertIn("limited to this stage's table", " ".join(seen["argv"]))

    def test_a_widening_user_sandbox_setting_stops_a_verifying_review(self) -> None:
        (self.claude_home / "settings.json").write_text(json.dumps(
            {"sandbox": {"filesystem": {"allowWrite": ["/"]}}}), encoding="utf-8")
        runner = mock.Mock(side_effect=AssertionError("dispatched with a widened sandbox"))
        result = ex.dispatch(
            router.RoutingDecision("reviewer", CLAUDE, router.RoutingMode.PRODUCTION),
            "review the change", ex.Registry([ex.ClaudeAdapter()]), cwd=str(self.repo),
            runner=runner, probes=READY,
            command_policy=self.policy("review", verify_prefixes=("python3 -m unittest",)))
        self.assertIn("widen the review sandbox", result.detail)
        runner.assert_not_called()

    def test_an_unavailable_sandbox_is_an_environment_restriction(self) -> None:
        result = ex.ClaudeAdapter().dispatch(
            CLAUDE, "review", cwd=str(self.repo),
            command_policy=self.policy("review", verify_prefixes=("python3 -m unittest",)),
            runner=lambda argv, **_: completed("", 1, "Error: sandbox required but unavailable"))
        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
        self.assertEqual("environment_restricted", result.error_code)
        self.assertEqual("blocked: sandbox unavailable", result.artifacts["command_policy"])


class CodexPolicyTests(DispatchTestCase):
    def test_an_active_policy_ignores_the_users_rules(self) -> None:
        for policy, expected in ((self.policy(), True),
                                 (self.policy(issue_provider="jira"), False), (None, False)):
            seen = {}

            def runner(argv, timeout=None, cwd=None, env=None):
                seen["argv"] = argv
                return completed('{"model": "gpt-6-luna"}')

            with self.subTest(policy=policy and policy.active):
                ex.CodexAdapter().dispatch(CODEX, "implement", cwd=str(self.repo), writes=True,
                                           command_policy=policy, runner=runner)
                self.assertEqual(expected, "--ignore-rules" in seen["argv"])
                self.assertEqual("implement", seen["argv"][-1])

    def test_a_review_profile_writes_only_its_clone(self) -> None:
        argv = ex.CodexAdapter().argv(CODEX, "review", cwd="/clone", review_clone="/clone",
                                      ignore_rules=True)
        self.assertIn('default_permissions="code_cycle_review_read"', argv)
        self.assertIn('permissions.code_cycle_review_read.extends=":read-only"', argv)
        self.assertIn('permissions.code_cycle_review_read.filesystem={"/clone"="write"}', argv)
        self.assertNotIn("workspace-write", argv)
        self.assertIn("--ignore-rules", argv)

    def test_an_unverified_review_sandbox_blocks_the_verifying_review(self) -> None:
        adapter = ex.CodexAdapter()
        policy = self.policy("review", verify_prefixes=("python3 -m unittest",))
        with mock.patch.object(ex.CodexAdapter, "probe_review_confinement",
                               return_value=(False, "a sibling was writable")):
            result = ex.dispatch(
                router.RoutingDecision("reviewer", CODEX, router.RoutingMode.PRODUCTION),
                "review the change", ex.Registry([adapter]), cwd=str(self.repo),
                runner=mock.Mock(side_effect=AssertionError("dispatched")), probes=READY,
                command_policy=policy)
        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
        self.assertEqual("environment_restricted", result.error_code)
        self.assertIn("BLOCKED", result.detail)
        self.assertIn("a sibling was writable", result.detail)

    def test_a_confirmed_review_sandbox_runs_the_reviewer_in_a_clone(self) -> None:
        seen = {}

        def runner(argv, timeout=None, cwd=None, env=None):
            seen.update(argv=argv, cwd=cwd)
            return completed('{"model": "gpt-6-luna"}')

        policy = self.policy("rereview", verify_prefixes=("python3 -m unittest",))
        with mock.patch.object(ex.CodexAdapter, "probe_review_confinement",
                               return_value=(True, "confined")):
            result = ex.dispatch(
                router.RoutingDecision("reviewer", CODEX, router.RoutingMode.PRODUCTION),
                "review the change", ex.Registry([ex.CodexAdapter()]), cwd=str(self.repo),
                runner=runner, probes=READY, command_policy=policy)
        self.assertEqual("enforced", result.artifacts.get("read_only_mode"), result.detail)
        self.assertNotEqual(str(self.repo), seen["cwd"])
        clone = json.dumps(seen["cwd"])
        self.assertTrue(any(f'{clone}="write"' in arg for arg in seen["argv"]))
        self.assertIn("--ignore-rules", seen["argv"])

    def test_the_confinement_probe_reports_what_leaked(self) -> None:
        def leaky(argv, cwd=None, **_):
            script = argv[-1]
            exec(compile(script.replace("sys.exit(", "(lambda *_: None)("), "probe", "exec"),
                 {"__name__": "probe"})
            return completed("", 0)

        confined, detail = ex.CodexAdapter().probe_review_confinement(runner=leaky)
        self.assertFalse(confined)
        self.assertIn("another code-cycle-tmp directory", detail)

        def refused(argv, cwd=None, **_):
            return completed("", 1, "sandbox: unknown profile")
        confined, detail = ex.CodexAdapter().probe_review_confinement(runner=refused)
        self.assertFalse(confined)
        self.assertIn("unknown profile", detail)


class OrcaPolicyTests(DispatchTestCase):
    def test_a_verifying_review_is_not_routed_to_orca(self) -> None:
        target = router.parse_target("orca:openai/gpt-6-luna high")
        result = ex.dispatch(
            router.RoutingDecision("reviewer", target, router.RoutingMode.PRODUCTION),
            "review", ex.Registry([ex.OrcaAdapter()]), cwd=str(self.repo), probes=READY,
            runner=mock.Mock(side_effect=AssertionError("dispatched")),
            command_policy=self.policy("review", verify_prefixes=("python3 -m unittest",)))
        # Orca's own read-only gate may refuse first; either way it never runs.
        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertIn(result.missing_capability,
                      {"review_workspace_isolation", "read_only_enforcement"})


class DockerBlockerTests(unittest.TestCase):
    BLOCKED = 'ORCHESTRATION_RESULT\n{"status": "BLOCKED"}\nEND_ORCHESTRATION_RESULT'

    def test_a_denied_compose_after_a_blocked_report_enters_the_grant_flow(self) -> None:
        for command in ("docker compose up -d", "rtk docker compose ps",
                        "rtk proxy docker compose logs"):
            with self.subTest(command=command):
                blocker = recovery.denied_docker_blocker(self.BLOCKED, ["git x", command])
                self.assertEqual("docker_host", blocker["resource"])
                self.assertEqual(command, blocker["command"])
                self.assertIs(False, blocker["requires_human"])
                self.assertEqual("docker_host", recovery.sandbox_blocker(
                    'ORCHESTRATION_RESULT\n' + json.dumps(
                        {"status": "BLOCKED", "environment_blocker": blocker})
                    + '\nEND_ORCHESTRATION_RESULT')["resource"])

    def test_nothing_else_is_a_docker_blocker(self) -> None:
        implemented = 'ORCHESTRATION_RESULT\n{"status": "IMPLEMENTED"}\nEND_ORCHESTRATION_RESULT'
        for spoken, denied in ((implemented, ["docker compose up"]),
                               (self.BLOCKED, ["docker run x", "python3 x"]),
                               (self.BLOCKED, ["docker compose run --rm app pytest",
                                               "docker compose exec api sh",
                                               "rtk docker compose cp a b",
                                               "docker compose --verbose up",
                                               "docker compose up > /etc/x"]),
                               (self.BLOCKED, None), (self.BLOCKED, "docker compose up"),
                               ("no block", ["docker compose up"])):
            self.assertIsNone(recovery.denied_docker_blocker(spoken, denied))


class RuntimePolicyTests(unittest.TestCase):
    def config(self, text: str) -> dict:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / ".code-cycle.yml"
        path.write_text(text, encoding="utf-8")
        return rc.load_config(path)

    def test_verify_is_a_validated_list_of_prefixes(self) -> None:
        config = self.config("code_cycle:\n  allowed_commands:\n    test: python3 -m unittest\n"
                             "    verify:\n      - python3 scripts/validate-package.py\n"
                             "      - 'python3  -m   unittest'\n")
        self.assertEqual(("python3 scripts/validate-package.py", "python3 -m unittest"),
                         rc.allowed_verify_commands(config))
        for verify in ("python3 -m unittest", "[]", "[python3]", "['bash -lc x']",
                       "['python3 *']", "[1]"):
            with self.subTest(verify=verify):
                with self.assertRaises(rc.CycleDriverError):
                    self.config(f"code_cycle:\n  allowed_commands:\n    verify: {verify}\n")
        with self.assertRaisesRegex(rc.CycleDriverError, "known keys are test, verify"):
            self.config("code_cycle:\n  allowed_commands:\n    lint: ruff\n")

    def test_this_repository_declares_its_verification(self) -> None:
        config = rc.load_config(ROOT / ".code-cycle.yml")
        self.assertTrue(rc.allowed_test_command(config))
        self.assertIn("python3 scripts/validate-package.py", rc.allowed_verify_commands(config))

    def test_the_base_branch_and_code_host_are_resolved_safely(self) -> None:
        def answering(stdout, code=0):
            return lambda argv, **_: subprocess.CompletedProcess(argv, code, stdout, "")
        configured = {"code_cycle": {"repository": {"default_branch": "develop"},
                                     "code_host": "bitbucket"}}
        self.assertEqual("develop", rc.base_branch_of(configured, "/x", run=answering("")))
        self.assertEqual("trunk", rc.base_branch_of({}, "/x",
                                                    run=answering("refs/remotes/origin/trunk\n")))
        self.assertIsNone(rc.base_branch_of({}, "/x", run=answering("", 1)))
        self.assertIsNone(rc.base_branch_of({}, None))
        self.assertEqual("bitbucket", rc.code_host_of(configured, "/x"))
        for url, host in (("git@github.com:owner/repo.git", "github"),
                          ("https://github.com/owner/repo", "github"),
                          ("git@bitbucket.org:w/r.git", None),
                          ("https://github.example.com/o/r", None)):
            with self.subTest(url=url):
                self.assertEqual(host, rc.code_host_of({}, "/x", run=answering(url + "\n")))

    def test_the_prompt_states_the_table_and_how_to_stop(self) -> None:
        policy = sp.StagePolicy(role="implement", repo="owner/api",
                                test_command="python3 -m unittest")
        prompt = rc.compose("implement", "owner/api", "142", command_policy=policy)
        self.assertIn("closed table", prompt)
        self.assertIn("`python3 -m unittest`", prompt)
        self.assertIn("name the exact denied command", prompt)
        inactive = rc.compose("implement", "owner/api", "142",
                              command_policy=replace(policy, code_host="bitbucket"),
                              allowed_test_command="python3 -m unittest")
        self.assertNotIn("closed table", inactive)
        self.assertIn("permits this exact test command", inactive)


class RuntimeStopTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        import telemetry as tm
        self.store = tm.Telemetry(Path(temporary.name) / "t.sqlite")
        patcher = mock.patch.dict(os.environ, {"CODE_CYCLE_HOME": str(Path(temporary.name))})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_blocked(self, **kw):
        from test_run_cycle import ScriptedAdapter, block

        class Blocked(ScriptedAdapter):
            def dispatch(self, target, task, **kwargs):
                self.dispatched.append(task)
                self.dispatch_kwargs.append(kwargs)
                spoken = block("BLOCKED", summary="needs a command")
                return ex.DispatchResult(
                    ex.DispatchOutcome.SUCCEEDED, self.name, target,
                    model_resolved=target.model, agent_output=spoken,
                    artifacts={"stdout": spoken, "permission_denials": [
                        "python3 -m unittest discover -s tests", "npm test"]})

        implementer, reviewer = Blocked("codex"), ScriptedAdapter("claude")
        report = rc.run_cycle(
            "owner/api", "142", router.TaskSignals(), self.store,
            registry=ex.Registry([implementer, reviewer]),
            availability={"codex": ex.Availability.READY, "claude": ex.Availability.READY},
            issue_review="off", code_host="github", allowed_verify_commands=("python3 scripts/validate-package.py",),
            base_branch="main", **kw)
        return report, implementer

    def test_a_permission_stop_names_the_exact_command_and_the_stage_receives_its_policy(self):
        report, implementer = self.run_blocked()
        self.assertIn("blocked command in the implement stage: "
                      "`python3 -m unittest discover -s tests` (and 1 more)",
                      report.stopped_because)
        self.assertIn("blocked command: `npm test`", report.stage_details[-1].warnings)
        policy = implementer.dispatch_kwargs[0]["command_policy"]
        self.assertEqual(("implement", "owner/api", "142", "main"),
                         (policy.role, policy.repo, policy.work_item_id, policy.base_branch))
        self.assertTrue(policy.active)
        self.assertEqual(("python3 scripts/validate-package.py",), policy.verify_prefixes)
        self.assertIn("closed table", implementer.dispatched[0])

    def test_a_non_github_cycle_receives_an_inactive_policy(self) -> None:
        from test_run_cycle import ScriptedAdapter
        implementer, reviewer = ScriptedAdapter("codex"), ScriptedAdapter("claude")
        rc.run_cycle(
            "owner/api", "ENG-1", router.TaskSignals(), self.store,
            registry=ex.Registry([implementer, reviewer]),
            availability={"codex": ex.Availability.READY, "claude": ex.Availability.READY},
            issue_review="off", code_host="bitbucket", work_item_provider="jira",
            work_item_repository="ENG")
        policy = implementer.dispatch_kwargs[0]["command_policy"]
        self.assertFalse(policy.active)
        self.assertNotIn("closed table", implementer.dispatched[0])


if __name__ == "__main__":
    unittest.main()
