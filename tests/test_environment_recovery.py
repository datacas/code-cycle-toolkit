"""Sandbox recovery uses the existing exit, effect and routing boundaries."""
from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import environment_recovery as recovery
import grants
import executors as ex
import router
import cycle_journal as journal
from test_cycle import CycleTestCase, ScriptedAdapter
from test_run_cycle import RunCycleTestCase, Talker, block


BLOCKER = {
    "kind": "sandbox_local_access", "command": "docker info",
    "error": "sandbox denied access to /var/run/docker.sock",
    "attempted": ["checked existing execution capabilities; none permits host access"],
    "requires_human": False,
}
CLEAN = {"dirty": False, "workspace": {}, "remote": "unchanged", "comments": []}
#: `docker_host` as it was before Claude had a scope (#142), for the paths a
#: fallback without a scope takes.
CODEX_ONLY = replace(grants.DOCKER_HOST, scopes=tuple(
    scope for scope in grants.DOCKER_HOST.scopes if scope.executor == "codex"))


def codex_only_grant():
    return patch.dict(grants.GRANTS, {"docker_host": CODEX_ONLY})


class RestrictedAdapter(ScriptedAdapter):
    def __init__(self, name="codex", *, blocker=None, exited=True, asynchronous=False):
        super().__init__(name)
        self.blocker = BLOCKER if blocker is None else blocker
        self.exited = exited
        self.asynchronous = asynchronous

    def dispatch(self, target, task, **kwargs):
        self.dispatched.append(task)
        self.dispatch_kwargs.append(kwargs)
        return ex.DispatchResult(
            ex.DispatchOutcome.SUCCEEDED, self.name, target,
            model_resolved=target.model,
            agent_output=block("BLOCKED", environment_blocker=self.blocker),
            start_state="started", process_exited=self.exited,
            asynchronous=self.asynchronous,
        )


class SandboxReportTests(unittest.TestCase):
    def test_codex_detection_uses_paired_tool_results_and_preserves_errors(self):
        def events(error):
            return [
                {"type": "response_item", "payload": {"type": "function_call", "call_id": "c1",
                    "name": "exec_command", "arguments": json.dumps({"cmd": "docker compose ps"})}},
                {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1",
                    "output": error}},
            ]
        error = "tool failed: SandboxDenied: sandbox denied access to /var/run/docker.sock"
        [observed] = recovery.codex_sandbox_denials(events(error))
        self.assertEqual(error, observed["error"])
        self.assertEqual("docker compose ps", observed["command"])
        self.assertEqual(observed, recovery.observed_sandbox_blocker(block("BLOCKED"), [observed]))
        for genuine in ("docker: command not found", "connection refused", "invalid credentials",
                        "permission denied: bad filesystem owner", "HTTP 401"):
            self.assertEqual([], recovery.codex_sandbox_denials(events(genuine)))
        self.assertEqual([], recovery.codex_sandbox_denials([
            {"type": "response_item", "payload": {"type": "message", "content": error}},
            events(error)[1]]))

    def test_an_earlier_unrelated_denial_does_not_hide_the_grantable_one(self):
        def call(call_id, command, output):
            return [
                {"type": "function_call", "call_id": call_id, "name": "exec_command",
                 "arguments": json.dumps({"cmd": command})},
                {"type": "function_call_output", "call_id": call_id, "output": output},
            ]
        network = call("n", "npm view left-pad",
                       "SandboxDenied: domain is not on the allowlist")
        docker = call("d", "docker compose up -d",
                      "SandboxDenied: sandbox denied access to /var/run/docker.sock "
                      "(network namespace)")
        denials = recovery.codex_sandbox_denials(network + docker)
        self.assertEqual(["sandbox_network_access", "sandbox_local_access"],
                         [denial["kind"] for denial in denials])
        reported = {**BLOCKER, "resource": "docker_host"}
        spoken = block("BLOCKED", environment_blocker=reported)
        chosen = recovery.observed_sandbox_blocker(spoken, denials)
        self.assertEqual("docker compose up -d", chosen["command"])
        self.assertIs(grants.DOCKER_HOST, grants.ConsentBroker("owner/api").grant_for(chosen))
        # Without an observed grantable denial, the agent's matching blocker wins.
        self.assertEqual(reported, recovery.observed_sandbox_blocker(spoken, denials[:1]))
        # Nothing grantable anywhere: the runtime's own first observation stands.
        self.assertEqual(denials[0], recovery.observed_sandbox_blocker(block("BLOCKED"),
                                                                      denials[:1]))

    def test_only_the_final_report_can_trigger_recovery(self):
        spoken = block("BLOCKED", environment_blocker=BLOCKER)
        self.assertEqual(BLOCKER, recovery.sandbox_blocker("echoed prompt\n" + spoken))
        for ending in (block("IMPLEMENTED"), block("BLOCKED"),
                       "ORCHESTRATION_RESULT\n[]\nEND_ORCHESTRATION_RESULT",
                       "ORCHESTRATION_RESULT\n{bad json}\nEND_ORCHESTRATION_RESULT"):
            self.assertIsNone(recovery.sandbox_blocker(spoken + "\n" + ending))
        self.assertIsNone(recovery.sandbox_blocker("sandbox denied access to docker.sock"))

    def test_a_named_resource_is_kept_only_as_a_plain_identifier(self):
        named = {**BLOCKER, "resource": "docker_host"}
        self.assertEqual(named, recovery.sandbox_blocker(
            block("BLOCKED", environment_blocker=named)))
        for resource in ("Docker Host", "x" * 80, 7, "docker_host; rm -rf /"):
            with self.subTest(resource=resource):
                self.assertEqual(BLOCKER, recovery.sandbox_blocker(
                    block("BLOCKED", environment_blocker={**BLOCKER, "resource": resource})))

    def test_incomplete_or_human_blockers_are_not_recoverable(self):
        for change in (
            {"kind": "service_unavailable"}, {"kind": "approval_denied"},
            {"kind": []}, {"requires_human": True}, {"requires_human": 0},
            {"command": ""}, {"command": None}, {"error": " "},
            {"attempted": []}, {"attempted": "retry"}, {"attempted": [None]},
        ):
            with self.subTest(change=change):
                self.assertIsNone(recovery.sandbox_blocker(block(
                    "BLOCKED", environment_blocker={**BLOCKER, **change})))
        for key in BLOCKER:
            incomplete = {k: v for k, v in BLOCKER.items() if k != key}
            self.assertIsNone(recovery.sandbox_blocker(block(
                "BLOCKED", environment_blocker=incomplete)))
        self.assertIsNone(recovery.sandbox_blocker(block(
            "BLOCKED", environment_blocker=BLOCKER, questions=[{"prompt": "Allow?"}])))

    def test_guidance_names_the_nested_codex_limit_without_a_grant(self):
        """#268: report a nested sandbox limitation without widening it."""
        guidance = recovery.ENVIRONMENT_RECOVERY_GUIDANCE
        self.assertIn("/tmp/codex-daemon-<uid>", guidance)
        self.assertIn("known nested", guidance)
        self.assertIn("not_verified", guidance)
        self.assertIn("codex sandbox`, `codex exec` or `srt`", guidance)
        self.assertNotIn("unix_sockets", guidance)


class SandboxRerouteTests(CycleTestCase):
    def test_runtime_observed_native_probe_failure_does_not_reroute(self):
        class ProbeFailure(ScriptedAdapter):
            def dispatch(self, target, task, **kwargs):
                self.dispatched.append(task)
                return ex.DispatchResult(
                    ex.DispatchOutcome.FAILED, self.name, target,
                    error_code="environment_restricted", start_state="not_started", process_exited=True,
                    detail="native probe timed out", artifacts={"native_sandbox_preflight": {
                        "status": "unverified", "functional_check": {"passed": False}}})
        _, primary, fallback, outcome = self.execute(primary=ProbeFailure("codex"))
        self.assertEqual(1, len(primary.dispatched))
        self.assertFalse(fallback.dispatched)
        self.assertFalse(outcome.rerouted)
        self.assertFalse(outcome.succeeded)

    def test_top_level_preflight_is_persisted_in_existing_journal(self):
        adapter = ScriptedAdapter("codex")
        recorder = self.recorder([adapter])
        recorder.journal = journal.Journal(self.store.path, "owner/repo", "API-055", recorder.cycle_id)
        # A full observation exceeds the journal's per-field limit. Every tool
        # and functional probe must survive, even when an earlier error is long.
        evidence = {"status": "unverified", "missing": ["rg"], "probes": [
            {"name": "rg", "outcome": "not_verified", "observed": None, "error": "x" * 900},
            {"name": "srt", "outcome": "reused", "observed": "/tools/srt", "version": "1.0.0"}],
            "functional_check": {"probe": "bwrap", "passed": False, "evidence": "namespace denied"},
            "preparation_isolation": {"passed": True, "evidence": "isolation verified"}}
        result = ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, "codex",
                                   router.parse_target("codex:openai/gpt-6-luna high"),
                                   artifacts={"command_policy": "active",
                                              "native_sandbox_preflight": evidence})
        recorder._journal_attempt("implement", result, "attempt-test", None, {})
        # Read through a fresh SQLite connection, not a mocked record method.
        recorded = [row for row in journal.events(self.store.path, "owner/repo")
                    if row["component"] == "sandbox_preflight"]
        self.assertFalse(recorder.journal.errors)
        by_probe = {row["reason"]: row for row in recorded}
        self.assertEqual({"summary", "rg", "srt", "functional_check", "preparation_isolation"}, set(by_probe))
        self.assertEqual("not_verified", by_probe["rg"]["status"])
        self.assertEqual("/tools/srt", by_probe["srt"]["command"])
        self.assertEqual("reused", by_probe["srt"]["status"])
        self.assertEqual("unverified", by_probe["functional_check"]["status"])
        self.assertIn("namespace denied", by_probe["functional_check"]["detail"])
        self.assertEqual("verified", by_probe["preparation_isolation"]["status"])
        self.assertTrue(all(row["attempt_id"] == "attempt-test" for row in recorded))
        self.assertTrue(all(len(row["detail"] or "") <= journal.TEXT_LIMIT + 1 for row in recorded))
        summary = recorder.journal.summary()
        self.assertIn("preflight implement #0 rg: not_verified", summary)
        self.assertNotIn("preflight implement #0 preparation_isolation: verified", summary)
        self.assertNotIn("stage implement #0: reused", summary)

    def test_other_tool_restrictions_also_keep_the_selected_executor(self):
        for command, error in (
            ("cargo test", "open /work/target/debug/.cargo-lock: Operation not permitted"),
            ("gradle test", "/cache/.gradle/caches/org.gradle: No such file or directory"),
            ("python3 check.py", "large argv merge: operation not permitted"),
            ("rg needle /private", "rg: /private: Operation not permitted"),
        ):
            with self.subTest(command=command):
                blocker = {**BLOCKER, "kind": "sandbox_filesystem_access",
                           "command": command, "error": error}
                _, primary, fallback, outcome = self.execute(primary=RestrictedAdapter(blocker=blocker))
                self.assertFalse(outcome.rerouted)
                self.assertEqual(1, len(primary.dispatched))
                self.assertEqual(0, len(fallback.dispatched))

    def test_completed_work_with_known_pending_checks_continues_to_review(self):
        for absent in ({"srt", "rg"}, {"srt"}, {"rg"}, set()):
            class CompletedAdapter(ScriptedAdapter):
                def dispatch(self, target, task, **kwargs):
                    self.dispatched.append(task)
                    return ex.DispatchResult(
                        ex.DispatchOutcome.SUCCEEDED, self.name, target,
                        process_exited=True, start_state="started",
                        agent_output=block("IMPLEMENTED" if kwargs.get("writes") else "CHANGES_REQUESTED",
                                           tests={"conclusion": "not_verified", "ran": False},
                                           native_sandbox_preflight={"missing": sorted(absent)}))
            with self.subTest(absent=absent):
                primary = CompletedAdapter("codex")
                fallback = ScriptedAdapter("claude")
                recorder = self.recorder([primary, fallback])
                implemented = recorder.stage("implement", "complete main work")
                reviewed = recorder.stage("review", "review statically")
                self.assertTrue(implemented.succeeded)
                self.assertTrue(reviewed.succeeded)
                self.assertFalse(implemented.rerouted)
                self.assertFalse(reviewed.rerouted)
                self.assertFalse(fallback.dispatched)

    def test_known_prerequisites_never_reroute_but_incomplete_work_stays_blocked(self):
        for command, error in (
            ("codex sandbox -- true", "app-server socket directory must be a user-owned directory with mode 0700"),
            ("srt -- npm ci", "srt: not found"),
            ("rg x", "rg: command not found"),
            ("bwrap -- true", "bwrap: Creating new namespace failed: Operation not permitted"),
            ("socat -V", "socat: not installed"),
        ):
            with self.subTest(command=command):
                blocker = {**BLOCKER, "command": command, "error": error}
                _, primary, fallback, outcome = self.execute(primary=RestrictedAdapter(blocker=blocker))
                self.assertFalse(outcome.rerouted)
                self.assertEqual(1, len(primary.dispatched))
                self.assertFalse(fallback.dispatched)
                self.assertFalse(outcome.succeeded)
                self.assertIn("not_verified", outcome.result.detail)

    def execute(self, primary=None, fallback=None, **kwargs):
        primary = primary or RestrictedAdapter()
        fallback = fallback or ScriptedAdapter("claude")
        recorder = self.recorder([primary, fallback], **kwargs)
        with patch("cycle.effect_snapshot", return_value=CLEAN):
            outcome = recorder.stage("implement", "implement the work")
        return recorder, primary, fallback, outcome

    def test_restrictions_request_on_chosen_executor_even_with_available_fallback(self):
        for executor in ("codex", "claude"):
            with self.subTest(executor=executor):
                primary = RestrictedAdapter(executor)
                fallback = ScriptedAdapter("claude" if executor == "codex" else "codex")
                consent = grants.ConsentBroker("owner/repo", platform="linux")
                profiles = router.load_profiles()
                profile = profiles["cheap_coder"]
                profiles["cheap_coder"] = router.Profile(profile.name,
                    router.parse_target(f"{executor}:openai/test high"),
                    router.parse_target(f"{fallback.name}:openai/test high"))
                recorder = self.recorder([primary, fallback], consent=consent, profiles=profiles)
                with patch("cycle.effect_snapshot", return_value=CLEAN):
                    outcome = recorder.stage("implement", "implement")
                self.assertFalse(outcome.succeeded)
                self.assertEqual(1, len(primary.dispatched))
                self.assertFalse(fallback.dispatched)
                self.assertEqual(executor, consent.pending[0].scope.executor)
                self.assertEqual(BLOCKER["error"], recovery.sandbox_blocker(
                    outcome.result.agent_output)["error"])
                self.assertIn("permission required", outcome.result.detail)

    def test_without_a_fallback_the_same_executor_reruns_with_the_answer(self):
        consent = grants.ConsentBroker("owner/repo", platform="linux",
                                       prompt=lambda q: "cycle")
        primary = RestrictedAdapter()
        recorder = self.recorder([primary], consent=consent)
        with patch("cycle.effect_snapshot", return_value=CLEAN):
            outcome = recorder.stage("implement", "implement the work")

        self.assertEqual(2, len(primary.dispatched))
        self.assertNotIn("sandbox_grants", primary.dispatch_kwargs[0])
        self.assertEqual(("docker_host",), primary.dispatch_kwargs[1]["sandbox_grants"])
        attempts = self.store.dispatch_attempts("owner/repo", cycle_id=recorder.cycle_id)
        self.assertEqual("retry", attempts[1]["relationship"])
        first = self.store.rows("owner/repo")[-2]
        self.assertEqual("permission_answered", first["payload"]["reroute_reason"])
        self.assertFalse(outcome.succeeded)

    def test_consent_answers_never_replay_changed_or_unknown_effects(self):
        changes = (
            (None, "evidence_unavailable"),
            ({**CLEAN, "dirty": True}, "dirty_workspace"),
            ({**CLEAN, "workspace": {"head": "new commit"}}, "workspace_changed"),
            ({**CLEAN, "remote": "pushed"}, "remote_changed"),
            ({**CLEAN, "comments": ["published"]}, "publication_changed"),
            ({**CLEAN, "pulls": ["new PR"]}, "publication_changed"),
        )
        for answer in ("cycle", "persistent", "deny"):
            for after, reason in changes:
                with self.subTest(answer=answer, reason=reason):
                    primary = RestrictedAdapter()
                    store = grants.ConsentStore(self.store.path.parent / "consents.json")
                    store.revoke("owner/repo", grants.DOCKER_HOST,
                                 grants.DOCKER_HOST.scope_for("codex", "linux"))
                    # Keep persistent answers inside this test's workspace.
                    consent = grants.ConsentBroker(
                        "owner/repo", platform="linux",
                        store=grants.ConsentStore(self.store.path.parent / "consents.json"),
                        prompt=lambda q, answer=answer: answer)
                    recorder = self.recorder([primary], consent=consent)
                    with patch("cycle.effect_snapshot", side_effect=lambda *a, **k:
                               CLEAN if not primary.dispatched else after):
                        outcome = recorder.stage("implement", "implement the work")

                    self.assertEqual(1, len(primary.dispatched))
                    self.assertFalse(outcome.succeeded)
                    self.assertIn(f"reroute stopped: {reason}", outcome.result.detail)
                    row = self.store.rows("owner/repo")[-1]["payload"]
                    self.assertEqual("stop", row["reroute_action"])
                    self.assertEqual(reason, row["reroute_reason"])

    def test_consent_keeps_the_full_guards_conservative_dirty_workspace_stop(self):
        consent = grants.ConsentBroker("owner/repo", platform="linux",
                                       prompt=lambda q: "cycle")
        primary = RestrictedAdapter()
        recorder = self.recorder([primary], consent=consent)
        with patch("cycle.effect_snapshot", return_value={**CLEAN, "dirty": True}):
            outcome = recorder.stage("implement", "implement the work")
        self.assertEqual(1, len(primary.dispatched))
        self.assertIn("reroute stopped: dirty_workspace", outcome.result.detail)

    def test_consent_answer_without_exit_proof_never_repeats_the_stage(self):
        for answer in ("cycle", "persistent", "deny"):
            with self.subTest(answer=answer):
                primary = RestrictedAdapter(exited=False)
                store = grants.ConsentStore(self.store.path.parent / "consents.json")
                store.revoke("owner/repo", grants.DOCKER_HOST,
                             grants.DOCKER_HOST.scope_for("codex", "linux"))
                consent = grants.ConsentBroker(
                    "owner/repo", platform="linux",
                    store=grants.ConsentStore(self.store.path.parent / "consents.json"),
                    prompt=lambda q, answer=answer: answer)
                recorder = self.recorder([primary], consent=consent)
                with patch("cycle.effect_snapshot", return_value=CLEAN):
                    outcome = recorder.stage("implement", "implement the work")
                self.assertEqual(1, len(primary.dispatched))
                self.assertIn("reroute stopped: exit_unproven", outcome.result.detail)

    def test_authorization_in_advance_reaches_first_dispatch(self):
        for source in ({"answers": {"docker_host": "cycle"}},
                       {"preauthorized": frozenset({"docker_host"})}):
            with self.subTest(source=source):
                consent = grants.ConsentBroker("owner/repo", platform="linux", **source)
                _, primary, fallback, outcome = self.execute(consent=consent)
                self.assertEqual(1, len(primary.dispatched))
                self.assertFalse(fallback.dispatched)
                self.assertEqual(("docker_host",), primary.dispatch_kwargs[0]["sandbox_grants"])
                self.assertFalse(outcome.succeeded)

    def test_revocation_reaches_the_next_dispatch_of_a_running_recorder(self):
        store = grants.ConsentStore(self.store.path.parent / "consents.json")
        scope = grants.DOCKER_HOST.scope_for("codex", "linux")
        store.save("owner/repo", grants.DOCKER_HOST, scope, "allow")
        consent = grants.ConsentBroker("owner/repo", platform="linux", store=store)
        primary = RestrictedAdapter()
        recorder = self.recorder([primary], consent=consent)
        with patch("cycle.effect_snapshot", return_value=CLEAN):
            recorder.stage("implement", "first dispatch")
            store.revoke("owner/repo", grants.DOCKER_HOST, scope)
            outcome = recorder.stage("implement", "second dispatch")
        self.assertEqual(("docker_host",), primary.dispatch_kwargs[0]["sandbox_grants"])
        self.assertNotIn("sandbox_grants", primary.dispatch_kwargs[1])
        self.assertNotIn("The user granted", primary.dispatched[1])
        self.assertFalse(outcome.succeeded)
        self.assertEqual(1, len(consent.pending))

    def test_runtime_codex_denial_requests_consent_without_an_agent_blocker(self):
        class ObservedAdapter(RestrictedAdapter):
            def dispatch(self, target, task, **kwargs):
                result = super().dispatch(target, task, **kwargs)
                return replace(result, agent_output=block("BLOCKED"),
                               artifacts={"sandbox_denials": [BLOCKER]})
        consent = grants.ConsentBroker("owner/repo", platform="linux")
        _, primary, fallback, outcome = self.execute(ObservedAdapter(), consent=consent)
        self.assertFalse(fallback.dispatched)
        self.assertEqual(1, len(primary.dispatched))
        request = consent.pending[0].record()
        self.assertEqual(BLOCKER["command"], request["operation"])
        self.assertEqual(BLOCKER["error"], request["observed_error"])
        self.assertIn("CI", request["alternative"])
        self.assertIn("permission required", outcome.result.detail)

    def test_an_earlier_network_denial_keeps_the_agents_docker_request(self):
        network = {"kind": "sandbox_network_access", "command": "npm view left-pad",
                   "error": "SandboxDenied: domain is not on the allowlist",
                   "attempted": ["runtime observed this command's sandbox denial"],
                   "requires_human": False}

        class TwoDenials(RestrictedAdapter):
            def dispatch(self, target, task, **kwargs):
                result = super().dispatch(target, task, **kwargs)
                return replace(result, artifacts={"sandbox_denials": [network]})
        consent = grants.ConsentBroker("owner/repo", platform="linux")
        adapter = TwoDenials(blocker={**BLOCKER, "resource": "docker_host"})
        _, primary, fallback, outcome = self.execute(adapter, consent=consent)
        self.assertFalse(fallback.dispatched)
        self.assertEqual(1, len(primary.dispatched))
        self.assertEqual(BLOCKER["command"], consent.pending[0].record()["operation"])
        self.assertIn("permission required", outcome.result.detail)

    def test_denied_permission_is_not_a_verified_pass(self):
        with patch("cycle.effect_snapshot", return_value=CLEAN):
            consent = grants.ConsentBroker("owner/repo", platform="linux",
                store=grants.ConsentStore(self.store.path.parent / "consents.json"),
                answers={"docker_host": "deny"})
            adapter = ScriptedAdapter("codex")
            recorder = self.recorder([adapter], consent=consent)
            outcome = recorder.stage("implement", "complete independent work")
        self.assertIn("denied", adapter.dispatched[0])
        # The normalizer also rejects an agent's explicit verification pass.
        spoken = recovery.denied_result(block("IMPLEMENTED", tests={"ran": True, "passed": True}), ["docker_host"])
        self.assertIn('"conclusion": "not_verified"', spoken)
        self.assertNotIn('"passed": true', spoken)

    def test_a_primary_without_a_scope_cannot_borrow_a_fallback_scope(self):
        consent = grants.ConsentBroker("owner/repo", platform="macos")
        _, primary, fallback, outcome = self.execute(consent=consent)
        self.assertEqual(1, len(primary.dispatched))
        self.assertFalse(fallback.dispatched)
        self.assertFalse(consent.pending)
        self.assertFalse(outcome.succeeded)
        self.assertTrue(any("not available" in note for note in consent.notes))

    def test_an_interactive_grant_does_not_replay_published_effects(self):
        consent = grants.ConsentBroker("owner/repo", platform="linux", prompt=lambda q: "cycle")
        primary, fallback = RestrictedAdapter(), RestrictedAdapter("claude")
        recorder = self.recorder([primary, fallback], consent=consent)
        with patch("cycle.effect_snapshot", side_effect=[CLEAN, {**CLEAN, "comments": ["published"]}]):
            outcome = recorder.stage("implement", "implement")
        self.assertFalse(outcome.succeeded)
        self.assertEqual((1, 0), (len(primary.dispatched), len(fallback.dispatched)))
        self.assertIn("publication_changed", outcome.result.detail)

    def test_observed_restrictions_preserve_evidence_without_switching(self):
        for kind in recovery.SANDBOX_BLOCKERS:
            diagnostic = {**BLOCKER, "kind": kind}
            _, primary, fallback, outcome = self.execute(RestrictedAdapter(blocker=diagnostic))
            self.assertFalse(outcome.succeeded)
            self.assertFalse(outcome.rerouted)
            self.assertEqual((1, 0), (len(primary.dispatched), len(fallback.dispatched)))
            self.assertEqual(diagnostic, recovery.sandbox_blocker(outcome.result.agent_output))
            self.assertIn("not_verified", outcome.result.detail)

    def test_no_exit_proof_or_async_execution_never_reroutes(self):
        for primary in (RestrictedAdapter(exited=False), RestrictedAdapter(asynchronous=True)):
            _, _, fallback, outcome = self.execute(primary)
            self.assertFalse(outcome.rerouted)
            self.assertFalse(fallback.dispatched)

    def test_executor_failures_override_an_agent_recovery_claim(self):
        for failure, fields in (
            (ex.DispatchOutcome.CONTRACT_VIOLATION, {}),
            (ex.DispatchOutcome.INTERRUPTED, {}),
            (ex.DispatchOutcome.BLOCKED, {"missing_capability": "authenticated_session"}),
            (ex.DispatchOutcome.FAILED, {"error_code": "auth"}),
        ):
            class FailedAdapter(RestrictedAdapter):
                def dispatch(self, target, task, **kwargs):
                    return replace(super().dispatch(target, task, **kwargs),
                                   outcome=failure, **fields)
            _, _, fallback, outcome = self.execute(FailedAdapter())
            self.assertFalse(outcome.rerouted)
            self.assertFalse(fallback.dispatched)

    def test_changed_unknown_or_dirty_effects_never_reroute(self):
        for after in (None, {**CLEAN, "dirty": True},
                      {**CLEAN, "workspace": {"changed": True}},
                      {**CLEAN, "remote": "changed"},
                      {**CLEAN, "comments": ["published"]}):
            primary, fallback = RestrictedAdapter(), ScriptedAdapter("claude")
            recorder = self.recorder([primary, fallback])
            with patch("cycle.effect_snapshot", side_effect=[CLEAN, after]):
                outcome = recorder.stage("implement", "implement")
            self.assertFalse(outcome.rerouted)
            self.assertFalse(fallback.dispatched)

    def test_no_second_recovery_or_calibration_substitution(self):
        _, _, fallback, outcome = self.execute(fallback=RestrictedAdapter("claude"))
        self.assertFalse(outcome.succeeded)
        self.assertEqual(1, len(outcome.attempts))
        self.assertIn("not_verified", outcome.result.detail)
        _, _, fallback, outcome = self.execute(mode=router.RoutingMode.CALIBRATION)
        self.assertFalse(outcome.rerouted)
        self.assertFalse(fallback.dispatched)

    def test_same_executor_fallback_cannot_fix_its_sandbox(self):
        profiles = router.load_profiles()
        profile = profiles["cheap_coder"]
        profiles["cheap_coder"] = router.Profile(
            profile.name, profile.primary, router.parse_target("codex:openai/gpt-6-sol high"))
        _, primary, fallback, outcome = self.execute(profiles=profiles)
        self.assertFalse(outcome.rerouted)
        self.assertEqual(1, len(primary.dispatched))
        self.assertFalse(fallback.dispatched)
        self.assertIn("selected executor", outcome.result.detail)

    def test_missing_or_unavailable_fallback_is_not_invented(self):
        for unavailable in (False, True):
            profiles = router.load_profiles()
            if not unavailable:
                profile = profiles["cheap_coder"]
                profiles["cheap_coder"] = router.Profile(profile.name, profile.primary, None)
            _, _, fallback, outcome = self.execute(
                profiles=profiles, availability={"codex": ex.Availability.READY,
                    "claude": ex.Availability.INSTALLED if unavailable else ex.Availability.READY})
            self.assertFalse(outcome.rerouted)
            self.assertFalse(fallback.dispatched)


class SandboxCycleTests(RunCycleTestCase):
    def test_an_essential_restriction_stops_without_changing_executor(self):
        fallback = Talker("claude")
        with patch("cycle.effect_snapshot", return_value=CLEAN):
            report = self.run_cycle(RestrictedAdapter(), fallback)
        self.assertEqual("HUMAN_INTERVENTION", report.status)
        self.assertFalse(report.stages[0].rerouted)
        self.assertFalse(fallback.dispatched)

    def test_a_regular_blocker_does_not_gain_recovery(self):
        fallback = Talker("claude")
        report = self.run_cycle(Talker("codex", block("BLOCKED", error="missing credentials")),
                                fallback)
        self.assertEqual("HUMAN_INTERVENTION", report.status)
        self.assertEqual([], fallback.dispatched)


if __name__ == "__main__":
    unittest.main()
