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


class DependencyPreparationDispatchTests(DispatchTestCase):
    def test_claude_selected_prerequisites_reach_all_consumers(self):
        evidence = {"missing": [], "status": "observed", "probes": [
            {"name": "srt", "path": "/tools/srt", "outcome": "reused"},
            {"name": "rg", "path": "/managed/rg/rg", "outcome": "installed", "executable": "rg"}]}
        paths = []
        def runner(argv, **kw):
            paths.append(kw["env"]["PATH"])
            return completed('{"type":"result","result":"done"}')
        with (mock.patch.object(ex, "_run", side_effect=runner) as launch,
              mock.patch.object(ex.native, "observe", return_value=evidence),
              mock.patch.object(ex.native, "isolation_probe", return_value=(True, "fixture")) as probe,
              mock.patch.object(ex.dependency_setup, "prepare", return_value={
                  "status": "not_required", "items": []}) as prepare,
              mock.patch.object(ex.sys, "platform", "linux")):
            result = ex.ClaudeAdapter().dispatch(
                CLAUDE, "implement", cwd=str(self.repo), writes=True,
                command_policy=self.policy(test_command="pytest", registry_domains=("registry.npmjs.org",)),
                runner=launch)
        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)
        expected = os.pathsep.join((str(Path("/managed/rg/rg").parent), os.environ.get("PATH", "")))
        self.assertEqual([expected], paths)
        self.assertEqual(expected, prepare.call_args.kwargs["prerequisite_path"])
        self.assertEqual(["bwrap", "srt"], [call.args[0] for call in probe.call_args_list])
        self.assertTrue(all(call.kwargs["path"] == expected for call in probe.call_args_list))

    def test_claude_unavailable_isolation_keeps_static_guards_and_pending_tests(self):
        evidence = {"missing": ["bwrap", "socat"], "probes": [], "status": "unverified"}
        payload = {"status": "IMPLEMENTED", "tests": {"passed": True, "conclusion": "verified"}}
        spoken = "ORCHESTRATION_RESULT\n" + json.dumps(payload) + "\nEND_ORCHESTRATION_RESULT"
        stdout = json.dumps({"type": "result", "result": spoken})
        seen = []
        def runner(argv, **kw):
            settings = settings_of(argv)
            self.assertNotIn("sandbox", settings)
            prompt = argv[argv.index("-p") + 1]
            self.assertIn("not_verified", prompt)
            self.assertEqual("claude", kw["env"]["CODE_CYCLE_STAGE_EXECUTOR"])
            seen.append(settings)
            return completed(stdout)
        with (mock.patch.object(ex, "_run", side_effect=runner) as launch,
              mock.patch.object(ex.native, "observe", return_value=evidence),
              mock.patch.object(ex.native, "isolation_probe", return_value=(False, "namespace unavailable")),
              mock.patch.object(ex.dependency_setup, "prepare") as prepare,
              mock.patch.object(ex.sys, "platform", "linux")):
            result = ex.ClaudeAdapter().dispatch(
                CLAUDE, "implement", cwd=str(self.repo), writes=True,
                command_policy=self.policy(test_command="pytest", registry_domains=("registry.npmjs.org",)),
                runner=launch)
        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)
        self.assertTrue(seen)
        prepare.assert_not_called()
        parsed = json.loads(result.agent_output.split("ORCHESTRATION_RESULT\n")[1].split("\nEND_")[0])
        self.assertEqual("IMPLEMENTED", parsed["status"])
        self.assertEqual("not_verified", parsed["tests"]["conclusion"])
        self.assertNotIn("passed", parsed["tests"])
        self.assertEqual("pytest", parsed["tests"]["evidence"][0]["command"])

    def test_preparation_role_gating_and_evidence_reach_each_native_prompt(self) -> None:
        for executor, adapter_type, target in (("codex", ex.CodexAdapter, CODEX),
                                                ("claude", ex.ClaudeAdapter, CLAUDE)):
            for role in ("implement", "resolve", "review", "rereview",
                         "issue_review", "security", "bootstrap"):
                with self.subTest(executor=executor, role=role):
                    expected = role in {"implement", "resolve", "review", "rereview"}
                    report = {"status": "prepared", "items": [], "evidence": "fixture-report.json"}
                    def runner(argv, **options):
                        prompt = argv[-1] if executor == "codex" else argv[argv.index("-p") + 1]
                        self.assertEqual(expected, "Runtime dependency preparation evidence" in prompt)
                        if expected:
                            self.assertIn(json.dumps(report), prompt)
                        return completed('{"type":"turn.completed"}')
                    with mock.patch.object(ex.dependency_setup, "prepare", return_value=report) as prepare:
                        result = adapter_type().dispatch(
                            target, "inspect fixture", cwd=str(self.repo),
                            writes=role in {"implement", "resolve"},
                            command_policy=self.policy(role, registry_domains=("packages.example.org",)),
                            runner=runner)
                    self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)
                    if expected:
                        prepare.assert_called_once_with(str(self.repo), executor, executor,
                                                        domains=("packages.example.org",),
                                                        prerequisite_path=os.environ.get("PATH", ""))
                        self.assertEqual(report, result.artifacts["dependency_preparation"])
                    else:
                        prepare.assert_not_called()
                        self.assertNotIn("dependency_preparation", result.artifacts)

    def test_preparation_exception_reaches_prompt_and_artifacts_without_skipping_agent(self) -> None:
        for adapter, target in ((ex.CodexAdapter(), CODEX), (ex.ClaudeAdapter(), CLAUDE)):
            with self.subTest(executor=adapter.name):
                seen = []
                def runner(argv, **options):
                    prompt = argv[-1] if adapter.name == "codex" else argv[argv.index("-p") + 1]
                    seen.append(prompt)
                    self.assertIn('"status": "incomplete"', prompt)
                    self.assertIn("fixture setup unavailable", prompt)
                    return completed('{"type":"turn.completed"}')
                with (mock.patch.object(ex.dependency_setup, "prepare",
                                        side_effect=OSError("fixture setup unavailable")),
                      mock.patch.object(ex.native, "observe", return_value={
                          "status": "unverified", "missing": ["rg"], "probes": []})):
                    result = adapter.dispatch(target, "inspect fixture", cwd=str(self.repo),
                                              writes=True, command_policy=self.policy(), runner=runner)
                self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)
                self.assertEqual(1, len(seen))
                self.assertEqual({"status": "incomplete", "items": [],
                                  "error": "fixture setup unavailable"},
                                 result.artifacts["dependency_preparation"])
                preflight = result.artifacts["native_sandbox_preflight"]
                self.assertEqual("unverified", preflight["status"])
                self.assertTrue(preflight["missing"])

    def test_codex_nonpublishing_dispatches_do_not_get_default_code_host_domains(self) -> None:
        # Includes local-only implement/resolve and inactive command policies.
        for role in ("implement", "resolve", "review", "rereview", "security", "bootstrap"):
            for active in (True, False):
                for registry_domains in ((), ("packages.example.org",)):
                    with self.subTest(role=role, active=active, registries=registry_domains):
                        seen = []
                        def runner(argv, **options):
                            seen.extend(argv)
                            return completed('{"type":"turn.completed"}')
                        result = ex.CodexAdapter().dispatch(
                            CODEX, "inspect supplied context", cwd=str(self.repo),
                            writes=role in {"implement", "resolve"}, publishes=False,
                            review_clone=str(self.repo) if role in {"review", "rereview"} else None,
                            command_policy=self.policy(role, code_host="github" if active else "bitbucket",
                                                       registry_domains=registry_domains), runner=runner)
                        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)
                        for domain in sp.REVIEW_SANDBOX_DOMAINS:
                            self.assertNotIn(domain, " ".join(seen))
                        self.assertNotIn("code_cycle_publish", " ".join(seen))
                        if registry_domains:
                            self.assertIn('"packages.example.org"="allow"', " ".join(seen))
                            self.assertIn("code_cycle_registry", " ".join(seen))
                        else:
                            self.assertFalse(any(".network.enabled=true" in arg for arg in seen))
                            if role in {"review", "rereview"}:
                                self.assertIn('default_permissions="code_cycle_review_read"', seen)

    def test_codex_publishing_dispatch_keeps_code_host_and_registry_domains(self) -> None:
        seen = []
        def runner(argv, **options):
            seen.extend(argv)
            return completed('{"type":"turn.completed"}')
        result = ex.CodexAdapter().dispatch(
            CODEX, "resolve fixture", cwd=str(self.repo), writes=True, publishes=True,
            command_policy=self.policy("resolve", registry_domains=("packages.example.org",)),
            runner=runner)
        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)
        for domain in (*sp.REVIEW_SANDBOX_DOMAINS, "packages.example.org"):
            self.assertIn(json.dumps(domain) + '="allow"', " ".join(seen))
        self.assertIn('default_permissions="code_cycle_publish_write"', seen)

    def test_codex_issue_review_can_read_github_without_publication(self) -> None:
        seen = []
        policy = self.policy("issue_review")
        def runner(argv, **options):
            seen.extend(argv)
            return completed('{"type":"turn.completed"}')
        result = ex.CodexAdapter().dispatch(
            CODEX, "read work item", cwd=str(self.repo), writes=False, publishes=False,
            command_policy=policy, runner=runner)
        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)
        for domain in ("github.com", "api.github.com"):
            self.assertIn(json.dumps(domain) + '="allow"', " ".join(seen))
        self.assertIn('permissions.code_cycle_registry_read.extends=":read-only"', seen)
        self.assertNotIn("code_cycle_publish", " ".join(seen))
        self.assertTrue(sp.check("gh issue view 142 --repo owner/repo", policy).allowed)
        for command in ("gh issue comment 142 --repo owner/repo --body forbidden",
                        "gh pr create --repo owner/repo --title forbidden --body forbidden",
                        "git push origin HEAD"):
            self.assertFalse(sp.check(command, policy).allowed, command)

    def test_preparation_registries_reach_worker_even_when_setup_is_incomplete(self) -> None:
        for adapter, target in ((ex.CodexAdapter(), CODEX), (ex.ClaudeAdapter(), CLAUDE)):
            for role in ("implement", "resolve", "review", "rereview"):
                for status in ("prepared", "incomplete"):
                    with self.subTest(executor=adapter.name, role=role, status=status):
                        seen = []
                        report = {"status": status, "items": [{"manager": "pnpm"}]}
                        def runner(argv, **options):
                            seen.extend(argv)
                            return completed('{"type":"turn.completed"}')
                        policy = self.policy(role, registry_domains=("packages.example.org",))
                        with mock.patch.object(ex.dependency_setup, "prepare", return_value=report):
                            result = adapter.dispatch(
                                target, "verify existing work", cwd=str(self.repo),
                                writes=role in {"implement", "resolve"}, publishes=True,
                                command_policy=policy, runner=runner)
                        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)
                        domains = (*ex.dependency_setup.REGISTRIES, "packages.example.org")
                        if adapter.name == "codex":
                            for domain in domains:
                                self.assertIn(json.dumps(domain) + '="allow"', " ".join(seen))
                        else:
                            allowed = settings_of(seen)["sandbox"]["network"]["allowedDomains"]
                            self.assertTrue(set(domains).issubset(allowed))
                        self.assertEqual(("packages.example.org",), policy.registry_domains)


class ClaudePolicyTests(DispatchTestCase):
    def test_project_choices_reach_both_barriers_and_private_registry_profile(self) -> None:
        def runner(argv, timeout=None, cwd=None, env=None):
            settings = settings_of(argv)
            command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
            policy_path = Path(command.rsplit(" ", 1)[1].strip("'\""))
            policy = sp.StagePolicy.from_json(policy_path.read_text())
            patterns = argv[argv.index("--allowedTools") + 1:]
            for tool in ("bespoke-search", "rg"):
                self.assertIn(f"Bash({tool}:*)", patterns)
                self.assertTrue(sp.check(f"{tool} -n evidence .", policy).allowed)
            self.assertIn("mcp__company__query", patterns)
            self.assertEqual(env["TMPDIR"], policy.tmpdir)
            self.assertIn("packages.example.org", settings["sandbox"]["network"]["allowedDomains"])
            helper = str(Path(policy.tmpdir) / "pr-comment.md")
            self.assertTrue(sp.check_file_edit("Write", {"file_path": helper}, policy).allowed)
            self.assertFalse(sp.check_file_edit("Write", {
                "file_path": str(Path(policy.tmpdir).parent / "pr-comment.md")},
                replace(policy, role="review")).allowed)
            self.assertTrue(sp.check(f"gh pr comment 7 --repo owner/repo --body-file {json.dumps(helper)}",
                                     policy).allowed)
            self.assertFalse(sp.check("gh pr merge 7 --repo owner/repo", policy).allowed)
            return completed()
        result = ex.ClaudeAdapter().dispatch(
            CLAUDE, "implement it", cwd=str(self.repo), writes=True,
            command_policy=self.policy(tools=("bespoke-search", "rg"),
                                       integrations=("mcp__company__query",),
                                       registry_domains=("packages.example.org",)), runner=runner)
        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)

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
        self.assertEqual(["acceptEdits"], [seen["argv"][i + 1] for i, word in
                                           enumerate(seen["argv"]) if word == "--permission-mode"])
        self.assertTrue(seen["script"])
        tmpdir = Path(seen["env"]["TMPDIR"])
        self.assertEqual(str(tmpdir), seen["policy"]["tmpdir"])
        self.assertEqual("implement", seen["policy"]["role"])
        for writable in (self.repo, tmpdir):
            self.assertNotIn(writable, seen["policy_path"].parents)
        self.assertFalse(seen["policy_path"].parent.exists())
        sandbox = settings_of(seen["argv"])["sandbox"]
        self.assertTrue(sandbox["enabled"])
        self.assertTrue(sandbox["failIfUnavailable"])
        self.assertFalse(sandbox["allowUnsandboxedCommands"])

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

    def test_pre_approved_non_bash_tools_are_reported_and_change_nothing(self) -> None:
        def dispatch(role="rereview"):
            return ex.ClaudeAdapter().dispatch(
                CLAUDE, "review it", cwd=str(self.repo), writes=role == "implement",
                command_policy=self.policy(role), runner=lambda *a, **k: completed())

        quiet = dispatch()
        self.assertNotIn("preapproved_tools", quiet.artifacts)
        (self.claude_home / "settings.json").write_text(json.dumps({"permissions": {"allow": [
            "Bash(git status:*)", "mcp__ctx__ctx_execute"]}}), encoding="utf-8")
        for role in ("rereview", "implement"):
            with self.subTest(role=role):
                noted = dispatch(role)
                self.assertEqual(["mcp__ctx__ctx_execute"], noted.artifacts["preapproved_tools"])
                self.assertEqual((quiet.outcome, quiet.error_code, quiet.lifecycle_state),
                                 (noted.outcome, noted.error_code, noted.lifecycle_state))
                self.assertEqual("active", noted.artifacts["command_policy"])
        inactive = ex.ClaudeAdapter().dispatch(
            CLAUDE, "review it", cwd=str(self.repo),
            command_policy=self.policy(code_host="bitbucket"), runner=lambda *a, **k: completed())
        self.assertNotIn("preapproved_tools", inactive.artifacts)

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
        # Only the minimal foreground hook (#276): no table, no sandbox.
        settings = settings_of(seen["argv"])
        self.assertEqual({"hooks", "env"}, set(settings))
        self.assertEqual("Bash", settings["hooks"]["PreToolUse"][0]["matcher"])
        self.assertTrue(result.artifacts["command_policy"].startswith("inactive: "))
        self.assertIn("GitHub only", result.artifacts["command_policy"])

    def test_every_claude_stage_gets_the_fixed_bash_timeout(self) -> None:
        for fields in ({}, {"code_host": "bitbucket", "issue_provider": "jira"}):
            with self.subTest(fields=fields):
                seen = {}

                def runner(argv, timeout=None, cwd=None, env=None):
                    seen["env"] = settings_of(argv)["env"]
                    command = settings_of(argv)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
                    seen["policy"] = sp.StagePolicy.from_json(
                        Path(command.rsplit(" ", 1)[1].strip("'\"")).read_text())
                    return completed()

                result = ex.ClaudeAdapter().dispatch(
                    CLAUDE, "implement it", cwd=str(self.repo), writes=True,
                    command_policy=self.policy(**fields), runner=runner)
                self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)
                self.assertEqual({"BASH_DEFAULT_TIMEOUT_MS": "600000",
                                  "BASH_MAX_TIMEOUT_MS": "600000"}, seen["env"])
                self.assertEqual(not fields, seen["policy"].active)

    def test_a_slow_verification_still_yields_a_structured_result(self) -> None:
        # The configured suite outlasts Claude's default foreground timeout:
        # the stage ran it in the foreground and returned its result.
        stream = "\n".join(json.dumps(event) for event in (
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {
                    "command": "python3 -m unittest", "timeout": 600000}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "OK"}]}},
            {"type": "result", "result": 'ORCHESTRATION_RESULT: {"status": "PASS"}'},
        ))
        result = ex.ClaudeAdapter().dispatch(
            CLAUDE, "implement it", cwd=str(self.repo), writes=True,
            command_policy=self.policy(test_command="python3 -m unittest"),
            runner=lambda *a, **k: completed(stream))
        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertIn("ORCHESTRATION_RESULT", result.agent_output)
        self.assertNotIn("background_command", result.artifacts)

    def test_a_pending_background_command_is_reported_only_with_evidence(self) -> None:
        def stream(*events) -> str:
            return "\n".join(json.dumps(event) for event in events)

        def background(command_id="t1") -> dict:
            return {"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": command_id, "name": "Bash", "input": {
                    "command": "python3 -m unittest", "run_in_background": True}}]}}

        waiting = {"type": "result", "result": "The full suite is still running."}
        adapter = ex.ClaudeAdapter()
        self.assertEqual({"background_command": "confirmed"},
                         adapter.extra_artifacts(stream(background(), waiting)))
        self.assertEqual({"background_command": "suspected"},
                         adapter.extra_artifacts(stream(waiting)))
        denied = {"type": "user", "message": {"content": [{
            "type": "tool_result", "tool_use_id": "t1", "is_error": True,
            "content": f"code-cycle implement {sp.HOOK_DENIAL_MARKER}: foreground"}]}}
        self.assertNotIn("background_command", adapter.extra_artifacts(
            stream(background(), denied, {"type": "result", "result": "done"})))
        self.assertEqual({}, adapter.extra_artifacts(stream({"type": "result", "result": "done"})))

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
        artifacts = ex.ClaudeAdapter().extra_artifacts(stream)
        self.assertEqual(["python3 -m unittest", "docker compose up -d"],
                         artifacts["permission_denials"])
        self.assertEqual(["Bash", "Write", "Bash"],
                         [item["tool"] for item in artifacts["claude_permission_denials"]])
        self.assertEqual("/x", artifacts["claude_permission_denials"][1]["command"])
        self.assertEqual({}, ex.ClaudeAdapter().extra_artifacts('{"type": "result"}'))

    def test_claude_bwrap_missing_source_is_a_sandbox_startup_failure(self) -> None:
        stream = "\n".join((
            json.dumps({"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": "bash-1", "name": "Bash",
                "input": {"command": "ls -l"},
            }]}}),
            json.dumps({"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "bash-1", "is_error": True,
                "content": "bwrap: Can't find source path /work/.claude/settings.json: "
                           "No such file or directory",
            }]}}),
        ))

        failures = ex.ClaudeAdapter().extra_artifacts(stream)["sandbox_startup_failures"]
        self.assertEqual(1, len(failures))
        self.assertEqual("sandbox_startup_failure", failures[0]["capability"])
        self.assertEqual("/work/.claude/settings.json", failures[0]["path"])
        self.assertIn("bubblewrap", failures[0]["cause"])
        self.assertIn("Claude Code", failures[0]["limitation"])
        self.assertEqual("ls -l", failures[0]["command"])

    def test_claude_bwrap_text_without_failed_bash_context_is_not_classified(self) -> None:
        stream = "\n".join((
            json.dumps({"type": "assistant", "message": {"content": [{
                "type": "text", "text": "bwrap: Can't find source path /work/.claude/settings.json",
            }]}}),
            json.dumps({"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "bash-1", "is_error": True,
                "content": "bwrap: Can't find source path /work/.claude/settings.json: "
                           "No such file or directory",
            }]}}),
        ))

        self.assertNotIn("sandbox_startup_failures", ex.ClaudeAdapter().extra_artifacts(stream))

    def test_claude_bwrap_abort_after_an_exit_code_line_is_classified(self) -> None:
        stream = "\n".join((
            json.dumps({"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": "bash-1", "name": "Bash", "input": {"command": "ls -l"},
            }]}}),
            json.dumps({"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "bash-1", "is_error": True,
                "content": [{"type": "text", "text": "Exit code 1\nbwrap: Can't find source path "
                             "/work/.claude/settings.json: No such file or directory\n"}],
            }]}}),
        ))

        failures = ex.ClaudeAdapter().extra_artifacts(stream)["sandbox_startup_failures"]
        self.assertEqual(["/work/.claude/settings.json"], [item["path"] for item in failures])

    def test_claude_bwrap_text_quoted_by_a_command_that_ran_is_not_classified(self) -> None:
        quoted = ("bwrap: Can't find source path /work/.claude/settings.json: "
                  "No such file or directory")
        for output in (f"FAIL: test_x\nAssertionError: {quoted!r} != ''\nExit code 1",
                       f"Exit code 1\n{quoted}\nand a later line from the command",
                       f"Exit code 1\nbefore\n{quoted}"):
            with self.subTest(output=output):
                stream = "\n".join((
                    json.dumps({"type": "assistant", "message": {"content": [{
                        "type": "tool_use", "id": "bash-1", "name": "Bash",
                        "input": {"command": "python3 -m unittest"},
                    }]}}),
                    json.dumps({"type": "user", "message": {"content": [{
                        "type": "tool_result", "tool_use_id": "bash-1", "is_error": True,
                        "content": output,
                    }]}}),
                ))

                self.assertNotIn("sandbox_startup_failures",
                                 ex.ClaudeAdapter().extra_artifacts(stream))

    def test_preflight_refusal_is_exposed_for_the_cycle_journal(self) -> None:
        adapter = ex.ClaudeAdapter()
        with mock.patch.object(adapter, "policy_preflight", return_value="unsafe tool.check mod"):
            result = adapter.dispatch(CLAUDE, "implement it", cwd=str(self.repo), writes=True,
                                      command_policy=self.policy(),
                                      runner=lambda *a, **k: self.fail("preflight must stop dispatch"))
        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
        self.assertEqual("unsafe tool.check mod", result.artifacts["policy_preflight_refusal"])

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
        # REV-006: the user's `defaultMode` (`auto` on the probe host) must not
        # approve an edit for a reviewer, and the hook also sees the edit tools.
        argv = seen["argv"]
        self.assertEqual("default", argv[argv.index("--permission-mode") + 1])
        [entry] = settings_of(argv)["hooks"]["PreToolUse"]
        self.assertIn("Edit", entry["matcher"].split("|"))

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
        self.assertIn("bubblewrap and socat", result.detail)


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

    def test_a_compose_redirected_into_tmpdir_enters_the_grant_flow(self) -> None:
        # The stage allowed the redirect; only the grant was missing (REV-004).
        for command in ("docker compose logs > $TMPDIR/l.txt",
                        "docker compose ps 2> ${TMPDIR}/err.txt"):
            with self.subTest(command=command):
                blocker = recovery.denied_docker_blocker(self.BLOCKED, [command])
                self.assertEqual("docker_host", blocker["resource"])
                self.assertEqual(command, blocker["command"])

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

    def test_reading_denials_runs_nothing(self) -> None:
        ran = []
        with mock.patch.object(recovery, "_never_run",
                               side_effect=lambda *a, **_: ran.append(a) or 1 / 0):
            self.assertIsNone(recovery.denied_docker_blocker(
                self.BLOCKED, ["git push origin HEAD", "git status"]))
        # The push rule asked for its branch and got the refusing runner.
        self.assertEqual([(["git", "rev-parse", "--abbrev-ref", "HEAD"],)], ran)


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
        self.assertIn("available search alternative", prompt)
        self.assertIn("Do not bypass a forbidden operation", prompt)
        inactive = rc.compose("implement", "owner/api", "142",
                              command_policy=replace(policy, code_host="bitbucket"),
                              allowed_test_command="python3 -m unittest")
        self.assertNotIn("closed table", inactive)
        self.assertIn("permits this exact test command", inactive)
        for text in (prompt, inactive):
            self.assertIn("Never use `run_in_background: true`", text)
            self.assertIn("below 10 minutes", text)


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

    def test_tools_outside_the_table_are_noticed_once_and_never_stop_the_cycle(self) -> None:
        from test_run_cycle import Talker

        class Noting(Talker):
            def dispatch(self, target, task, **kwargs):
                result = super().dispatch(target, task, **kwargs)
                return replace(result, artifacts={**result.artifacts, "preapproved_tools": [
                    "mcp__ctx__ctx_execute", "mcp__docs__query-docs"]})

        def cycle(implementer, reviewer):
            return rc.run_cycle(
                "owner/api", "142", router.TaskSignals(), self.store,
                registry=ex.Registry([implementer, reviewer]),
                availability={"codex": ex.Availability.READY, "claude": ex.Availability.READY},
                issue_review="off", code_host="github", base_branch="main")

        plain = cycle(Talker("codex"), Talker("claude"))
        noted = cycle(Noting("codex"), Noting("claude"))
        self.assertGreater(len(noted.stage_details), 1)
        notices = [warning for detail in noted.stage_details for warning in detail.warnings
                   if "outside the stage table" in warning]
        self.assertEqual(1, len(notices))
        self.assertIn(": mcp__ctx__ctx_execute can run code or write files", notices[0])
        self.assertIn("; also mcp__docs__query-docs (the table governs", notices[0])
        self.assertEqual((plain.status, plain.stop_reason, [s.role for s in plain.stages]),
                         (noted.status, noted.stop_reason, [s.role for s in noted.stages]))
        self.assertFalse(any("outside the stage table" in warning
                             for detail in plain.stage_details for warning in detail.warnings))

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
