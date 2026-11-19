"""Parameterized sandbox consent and real adapter configuration boundaries."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import executors as ex
import grants
import router
import stage_permissions as sp
import environment_recovery as recovery
import stop_questions


class SandboxGrantTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.environment = patch("pathlib.Path.home", return_value=self.home)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.cache = self.home / ".cache" / "example"
        self.cache.mkdir(parents=True)
        self.store = grants.ConsentStore(self.home / "consents.json")

    def writable(self, value=None):
        return grants.sandbox_grant("writable", str(value or self.cache), source="explicit --grant")

    def broker(self, *args, **kwargs):
        return grants.ConsentBroker(*args, platform="linux", **kwargs)

    def symlink(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as error:
            self.skipTest(f"symlink fixture unavailable: {error}")

    def test_canonical_parameter_is_the_identity(self):
        alias = self.home / "alias"
        self.symlink(alias, self.cache)
        self.assertEqual(self.writable().id, self.writable(alias).id)
        self.assertEqual(self.writable().id, self.writable(self.cache / ".." / "example").id)
        first = grants.sandbox_grant("network", "EXAMPLE.com,bücher.de,example.com", source="explicit --grant")
        second = grants.sandbox_grant("network", ["xn--bcher-kva.de", "example.com"], source="validated configuration")
        self.assertEqual(first.id, second.id)
        self.assertEqual(["example.com", "xn--bcher-kva.de"], first.sandbox["parameter"])

    def test_protected_paths_and_ancestors_never_reach_consent(self):
        for name in (".ssh", ".gnupg", ".aws", ".config/gh", ".codex/auth.json", ".netrc",
                     ".config/code-cycle/consents.json", ".config/code-cycle-toolkit/telemetry.sqlite"):
            target = self.home / name
            target.parent.mkdir(parents=True, exist_ok=True)
            for value in (target, target / "child", self.home, target.parent):
                with self.subTest(value=value):
                    with self.assertRaises(grants.GrantError):
                        self.writable(value)
            alias = self.home / "alias"
            self.symlink(alias, target)
            with self.assertRaises(grants.GrantError):
                self.writable(alias)
            alias.unlink()
        with self.assertRaises(grants.GrantError):
            self.writable(self.cache / ".." / ".." / ".ssh")

    def test_domains_reject_patterns_urls_ports_and_empty_sets(self):
        for value in ("", "*.example.com", "https://example.com", "example.com:443", "good.com,,bad.com", "-bad.com"):
            with self.subTest(value=value):
                with self.assertRaises(grants.GrantError):
                    grants.canonical_domains(value)

    def test_explicit_cli_answers_apply_only_to_writers_and_exact_repo(self):
        for kind, parameter in (("writable", str(self.cache)), ("network", "example.com")):
            for answer in ("cycle", "persistent", "deny"):
                with self.subTest(kind=kind, answer=answer):
                    store = grants.ConsentStore(self.home / f"{kind}-{answer}.json")
                    answers = grants.parse_grant_options([f"{kind}:{parameter}={answer}"])
                    broker = self.broker("repo", store=store, answers=answers)
                    grant = next(iter(broker.operations.values()))
                    self.assertEqual((), broker.granted("codex", "review"))
                    allowed = () if answer == "deny" else (grant.id,)
                    self.assertEqual(allowed, broker.granted("codex", "implement"))
                    self.assertEqual(allowed, broker.granted("codex", "resolve"))
                    for role in ("review", "rereview", "security", "issue_review"):
                        self.assertEqual((), broker.granted("codex", role))
                    if answer == "persistent":
                        # A resumed answer persists only the exact previously asked scope.
                        scope = grant.scope_for("codex", "linux")
                        broker.apply_answer(grant, scope, answer)
                        resumed = self.broker("repo", store=store)
                        self.assertEqual(allowed, resumed.granted("codex", "implement"))
                        self.assertEqual((), resumed.granted("codex", "review"))
                        self.assertEqual((), self.broker("other", store=store).granted("codex"))
                    else:
                        self.assertEqual((), self.broker("repo", store=store).granted("codex"))

    def test_global_and_untrusted_definitions_are_rejected(self):
        with self.assertRaises(grants.GrantError):
            grants.parse_grant_options([f"writable:{self.cache}=forever"])
        with self.assertRaises(grants.GrantError):
            grants.sandbox_grant("network", "example.com", source="agent statement")

    def test_path_is_revalidated_after_consent(self):
        grant = self.writable()
        broker = self.broker("repo", operations={grant.id: grant})
        broker.apply_answer(grant, grant.scope_for("codex", "linux"), "cycle")
        self.cache.rmdir()
        self.symlink(self.cache, self.home / ".ssh")
        self.assertEqual((), broker.granted("codex"))
        self.assertIn("not_verified", " ".join(broker.notes))

    def test_local_sockets_is_codex_loopback_only_and_supports_answer_scopes(self):
        for answer in ("cycle", "persistent", "deny"):
            broker = self.broker("repo", answers={"local_sockets": answer})
            expected = () if answer == "deny" else ("local_sockets",)
            self.assertEqual(expected, broker.granted("codex"))
            self.assertEqual((), broker.granted("claude"))
        broker = self.broker("repo", answers={"local_sockets": "cycle"})
        self.assertEqual(grants.UNSUPPORTED, broker.request(grants.LOCAL_SOCKETS, "claude", role="implement"))
        self.assertIn("not_verified", " ".join(broker.notes))
        target = router.parse_target("codex:openai/gpt-6.1-sol medium")
        argv = ex.CodexAdapter().argv(target, "work", writes=True, sandbox_grants=("local_sockets",))
        self.assertIn("features.network_proxy=true", argv)
        self.assertTrue(any(".network.enabled=true" in arg for arg in argv))
        self.assertFalse(any(".network.domains=" in arg for arg in argv))
        self.assertFalse(any("dangerously_allow_all_unix_sockets" in arg for arg in argv))
        docker = ex.CodexAdapter().argv(target, "work", writes=True, sandbox_grants=("docker_host",))
        self.assertTrue(any("dangerously_allow_all_unix_sockets=true" in arg for arg in docker))

    def test_codex_profile_preserves_denials_and_separates_domains_from_docker(self):
        target = router.parse_target("codex:openai/gpt-6.1-sol medium")
        tokens = (f"writable:{self.cache}", "network:example.com")
        argv = ex.CodexAdapter().argv(target, "work", writes=True, sandbox_grants=tokens)
        self.assertTrue(any(str(self.cache) in arg and '="write"' in arg for arg in argv))
        self.assertTrue(any('"example.com"="allow"' in arg for arg in argv))
        self.assertFalse(any("dangerously_allow_all_unix_sockets" in arg for arg in argv))
        readonly = ex.CodexAdapter().argv(target, "review", writes=False, sandbox_grants=tokens)
        self.assertFalse(any(str(self.cache) in arg or "example.com" in arg for arg in readonly))

    def test_claude_emits_writable_settings_only_for_writers(self):
        adapter = ex.ClaudeAdapter()
        repo = self.home / "repo"
        repo.mkdir()
        for role in ("implement", "resolve", "review", "rereview", "security"):
            policy = sp.StagePolicy(role=role, sandbox_writable=(str(self.cache),))
            with adapter.policy_files(["claude", "--verbose", "-p", "work"], policy,
                                      scratch=str(self.home / "scratch"), cwd=str(repo)) as argv:
                settings = json.loads(argv[argv.index("--settings") + 1])
            writable = settings["sandbox"]["filesystem"]["allowWrite"]
            self.assertEqual(role in sp.WRITING_ROLES, str(self.cache) in writable)

    def test_dispatch_applies_parameters_and_records_the_stage(self):
        seen = {}

        class Recording(ex.Adapter):
            provable_ceiling = ex.Availability.AUTHENTICATED

            def __init__(self, name):
                self.name = name

            def probe(self):
                return ex.ProbeResult(self.name, ex.Availability.AUTHENTICATED, "fixture",
                                      provable_ceiling=ex.Availability.AUTHENTICATED)

            def dispatch(self, target, task, **kwargs):
                seen[self.name] = kwargs
                return ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, self.name, target,
                                         model_resolved=target.model)

        registry = ex.Registry([Recording("codex"), Recording("claude")])
        tokens = (f"writable:{self.cache}", "network:example.com")
        for spec in ("codex:openai/gpt-6.1-sol medium", "claude:anthropic/claude-sonnet-5 medium"):
            target = router.parse_target(spec)
            decision = router.RoutingDecision("deep_coder", target, router.RoutingMode.PRODUCTION)
            result = ex.dispatch(decision, "work", registry, writes=True,
                                 policy=ex.ReadinessPolicy.ATTEMPT,
                                 command_policy=sp.StagePolicy(role="implement"), sandbox_grants=tokens)
            policy = seen[target.executor]["command_policy"]
            self.assertEqual((str(self.cache),), policy.sandbox_writable)
            self.assertEqual(("example.com",), policy.registry_domains)
            self.assertEqual(list(tokens), result.artifacts["sandbox_grants"])
            if target.executor == "claude":
                self.assertNotIn("sandbox_grants", seen["claude"])
                settings = sp.review_sandbox_settings(policy.sandbox_writable, policy.registry_domains)
                self.assertIn("example.com", settings["sandbox"]["network"]["allowedDomains"])
                self.assertNotIn("allowAllUnixSockets", settings["sandbox"]["network"])
            for role in ("review", "rereview", "security", "issue_review"):
                for own in (False, True):
                    review_tokens = (f"{role}:writable:{self.cache}", f"{role}:network:review.example.com") if own else ()
                    result = ex.dispatch(decision, "review", registry, writes=False,
                                         workspace_policy=ex.WorkspacePolicy.WORKSPACE_WRITE,
                                         policy=ex.ReadinessPolicy.ATTEMPT,
                                         command_policy=sp.StagePolicy(role=role),
                                         sandbox_grants=(*tokens, *review_tokens, "review:network:other.example.com")
                                         if role != "review" else (*tokens, *review_tokens))
                    policy = seen[target.executor]["command_policy"]
                    self.assertEqual((str(self.cache),) if own else (), policy.sandbox_writable)
                    self.assertEqual(("review.example.com",) if own else (), policy.registry_domains)
                    self.assertEqual(role if own else "", policy.sandbox_grant_role)
                    if target.executor == "claude" and own:
                        repo = self.home / "review-repo"
                        repo.mkdir(exist_ok=True)
                        with ex.ClaudeAdapter().policy_files(["claude", "--verbose", "-p", "review"], policy,
                                                            scratch=str(self.home / "scratch"), cwd=str(repo)) as argv:
                            settings = json.loads(argv[argv.index("--settings") + 1])
                        self.assertIn(str(self.cache), settings["sandbox"]["filesystem"]["allowWrite"])
                        self.assertIn("review.example.com", settings["sandbox"]["network"]["allowedDomains"])

    def test_record_keeps_canonical_parameter_source_and_role(self):
        grant = self.writable()
        scope = grant.scope_for("codex", "linux")
        record = grants.PermissionRequest(grant, scope, "implement", {}).record()
        self.assertEqual(str(self.cache), record["sandbox"]["parameter"])
        self.assertEqual("explicit --grant", record["sandbox"]["source"])
        self.assertEqual("implement", record["role"])

    def test_unknown_definition_schema_cannot_replay_consent(self):
        definition = self.writable().sandbox
        self.assertIsNone(grants.from_entry({"sandbox": {**definition, "version": 2}}))
        self.assertIsNone(grants.from_entry({"sandbox": {**definition, "role": "review"}}))

    def test_runtime_denial_registers_a_parameter_but_never_authorizes_it(self):
        error = f"SandboxDenied: sandbox denied access to path '{self.cache}'"
        events = [
            {"type": "function_call", "name": "exec_command", "call_id": "1",
             "arguments": json.dumps({"cmd": "tool check"})},
            {"type": "function_call_output", "call_id": "1", "output": error},
        ]
        [blocker] = recovery.codex_sandbox_denials(events)
        broker = self.broker("repo")
        grant = broker.observe_sandbox(blocker, role="implement")
        self.assertEqual("observed sandbox denial", grant.sandbox["source"])
        self.assertEqual((), broker.granted("codex"))
        self.assertEqual(grants.PENDING, broker.request(grant, "codex", role="implement", blocker=blocker))
        broker.apply_answer(grant, grant.scope_for("codex", "linux"), "deny")
        self.assertEqual((), broker.granted("codex"))
        self.assertIn("not_verified", grants.denial_context(grant))
        self.assertEqual([], recovery.codex_sandbox_denials([
            {"type": "message", "output": error}]))

    def test_network_denial_and_explicit_missing_definition_use_distinct_sources(self):
        events = [
            {"type": "function_call", "name": "exec_command", "call_id": "1",
             "arguments": json.dumps({"cmd": "tool check"})},
            {"type": "function_call_output", "call_id": "1",
             "output": "domain 'EXAMPLE.com' is not on the allowlist"},
        ]
        [blocker] = recovery.codex_sandbox_denials(events)
        broker = self.broker("repo")
        grant = broker.observe_sandbox(blocker, role="implement")
        self.assertEqual(["example.com"], grant.sandbox["parameter"])
        missing = {"kind": "sandbox_filesystem_access", "command": "tool check", "error": "SandboxDenied"}
        self.assertIsNone(broker.request_definition(missing, "codex", role="implement"))
        self.assertIn("exact home path", broker.pending[0].question["prompt"])
        self.assertEqual([], stop_questions.questions_errors([broker.pending[0].question]))
        explicit = self.broker("repo", prompt=lambda asked: str(self.cache))
        grant = explicit.request_definition(missing, "codex", role="implement")
        self.assertEqual("validated configuration", grant.sandbox["source"])
        self.assertEqual((), explicit.granted("codex"))
        denied = self.broker("repo", prompt=lambda asked: "deny")
        self.assertIsNone(denied.request_definition(missing, "codex", role="implement"))
        self.assertIn("not_verified", denied.notes[0])

    def test_protected_observed_parameter_stays_unavailable_without_traceback(self):
        broker = self.broker("repo")
        blocker = {"sandbox_definition": {"kind": "writable", "parameter": str(self.home / ".ssh")}}
        self.assertIsNone(broker.observe_sandbox(blocker, role="implement"))
        self.assertEqual((), broker.granted("codex"))
        self.assertIn("protected store", broker.notes[0])
        with self.assertRaises(grants.GrantError):
            self.writable(self.home / ".cache")

    def test_review_requires_its_own_cycle_answer(self):
        broker = self.broker("repo", store=self.store)
        writer = self.writable()
        broker.operations[writer.id] = writer
        broker.apply_answer(writer, writer.scope_for("codex", "linux"), "persistent")
        blocker = {"sandbox_definition": {"kind": "writable", "parameter": str(self.cache)}}
        review = broker.observe_sandbox(blocker, role="review")
        self.assertNotEqual(writer.id, review.id)
        self.assertEqual((), broker.granted("codex", "review"))
        scope = review.scope_for("codex", "linux")
        self.assertEqual(2, len(grants.question(review, scope, role="review")["options"]))
        with self.assertRaises(grants.GrantError):
            broker.apply_answer(review, scope, "persistent")
        broker.apply_answer(review, scope, "cycle")
        self.assertEqual((review.id,), broker.granted("codex", "review"))
        self.assertEqual((), broker.granted("codex", "security"))
        self.assertEqual((), self.broker("repo", store=self.store).granted("codex", "review"))
        self.assertEqual(review.id, grants.from_entry({"sandbox": review.sandbox}).id)
        token = grants.sandbox_token(review)
        target = router.parse_target("codex:openai/gpt-6.1-sol medium")
        argv = ex.CodexAdapter().argv(target, "review", writes=False, sandbox_grants=(token,))
        self.assertTrue(any(str(self.cache) in arg and '=\"write\"' in arg for arg in argv))
        record = grants.PermissionRequest(review, scope, "review", {}).record()
        self.assertIn(f"review:writable:{self.cache}", record["answer_with"])
        options = grants.parse_grant_options([f"{token}=cycle"])
        resumed = self.broker("repo", answers=options)
        self.assertEqual((review.id,), resumed.granted("codex", "review"))
        self.assertEqual((), resumed.granted("codex", "implement"))
        with self.assertRaises(grants.GrantError):
            grants.parse_grant_options([f"{token}=persistent"])

    def test_claude_policy_uses_an_alternative_outside_granted_scratch_parent(self):
        repo = self.home / "repo"
        repo.mkdir()
        scratch = self.cache / "scratch"
        scratch.mkdir()
        policy = sp.StagePolicy(role="implement", sandbox_writable=(str(self.cache),))
        with ex.ClaudeAdapter().policy_files(["claude", "--verbose", "-p", "work"], policy,
                                            scratch=str(scratch), cwd=str(repo)) as argv:
            settings = json.loads(argv[argv.index("--settings") + 1])
            hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
            self.assertNotIn(str(self.cache), hook)
            self.assertIn("stage_permissions.py", hook)


if __name__ == "__main__":
    unittest.main()
