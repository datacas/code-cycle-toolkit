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
        """#268: a Codex stage cannot start Codex; the guidance must not widen anything."""
        guidance = recovery.ENVIRONMENT_RECOVERY_GUIDANCE
        self.assertIn("/tmp/codex-daemon-<uid>", guidance)
        self.assertIn("cannot start inside a Codex stage", guidance)
        self.assertIn("without retrying variants", guidance)
        self.assertNotIn("unix_sockets", guidance)


class SandboxRerouteTests(CycleTestCase):
    def execute(self, primary=None, fallback=None, **kwargs):
        primary = primary or RestrictedAdapter()
        fallback = fallback or ScriptedAdapter("claude")
        recorder = self.recorder([primary, fallback], **kwargs)
        with patch("cycle.effect_snapshot", return_value=CLEAN):
            outcome = recorder.stage("implement", "implement the work")
        return recorder, primary, fallback, outcome

    def test_a_fallback_that_recovers_without_a_wider_sandbox_is_not_asked_about(self):
        """Least privilege: a person is asked only when nothing narrower works."""
        consent = grants.ConsentBroker(
            "owner/repo", platform="linux",
            prompt=lambda question: self.fail("asked although a fallback recovered"))
        recorder, primary, fallback, outcome = self.execute(consent=consent)

        self.assertTrue(outcome.succeeded)
        self.assertTrue(outcome.rerouted)
        self.assertEqual((1, 1), (len(primary.dispatched), len(fallback.dispatched)))
        self.assertEqual([], consent.pending)
        self.assertNotIn("sandbox_grants", primary.dispatch_kwargs[0])

    def test_without_a_fallback_the_same_executor_reruns_with_the_answer(self):
        consent = grants.ConsentBroker("owner/repo", platform="linux",
                                       answers={"docker_host": "cycle"})
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
                    # Keep persistent answers inside this test's workspace.
                    consent = grants.ConsentBroker(
                        "owner/repo", platform="linux",
                        store=grants.ConsentStore(self.store.path.parent / "consents.json"),
                        answers={"docker_host": answer})
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
                                       answers={"docker_host": "cycle"})
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
                consent = grants.ConsentBroker(
                    "owner/repo", platform="linux",
                    store=grants.ConsentStore(self.store.path.parent / "consents.json"),
                    answers={"docker_host": answer})
                recorder = self.recorder([primary], consent=consent)
                with patch("cycle.effect_snapshot", return_value=CLEAN):
                    outcome = recorder.stage("implement", "implement the work")
                self.assertEqual(1, len(primary.dispatched))
                self.assertIn("reroute stopped: exit_unproven", outcome.result.detail)

    def test_a_fallback_that_hits_the_same_wall_hands_the_question_to_the_primary(self):
        """The fallback was tried first; the grant still reaches the primary."""
        for source in ({"answers": {"docker_host": "cycle"}},
                       {"preauthorized": frozenset({"docker_host"})},
                       {"prompt": lambda question: "cycle"}):
            with self.subTest(source=sorted(source)), codex_only_grant():
                consent = grants.ConsentBroker("owner/repo", platform="linux", **source)
                recorder, primary, fallback, outcome = self.execute(
                    fallback=RestrictedAdapter("claude"), consent=consent)

                self.assertEqual((2, 1), (len(primary.dispatched), len(fallback.dispatched)))
                self.assertNotIn("sandbox_grants", primary.dispatch_kwargs[0])
                self.assertNotIn("sandbox_grants", fallback.dispatch_kwargs[0])
                self.assertEqual(("docker_host",), primary.dispatch_kwargs[1]["sandbox_grants"])
                self.assertIn("every later codex stage", primary.dispatched[1])
                self.assertEqual({"docker_host"}, consent.granted_ids)
                self.assertEqual([], consent.pending)
                attempts = self.store.dispatch_attempts("owner/repo", cycle_id=recorder.cycle_id)
                self.assertEqual(["codex", "claude", "codex"],
                                 [attempt["executor"] for attempt in attempts])

    def test_a_fallback_with_its_own_scope_receives_the_grant_itself(self):
        """Claude has a `docker_host` scope (#142): the fallback reruns with it."""
        consent = grants.ConsentBroker("owner/repo", platform="linux",
                                       answers={"docker_host": "cycle"})
        recorder, primary, fallback, outcome = self.execute(
            fallback=RestrictedAdapter("claude"), consent=consent)

        self.assertEqual((1, 2), (len(primary.dispatched), len(fallback.dispatched)))
        # Claude applies the grant through its command table, not a sandbox
        # option, so the adapter never sees `sandbox_grants` itself.
        self.assertNotIn("sandbox_grants", fallback.dispatch_kwargs[1])
        self.assertEqual(("docker_host",), consent.granted("claude"))
        self.assertIn("docker compose up, down, ps, logs and build only",
                      fallback.dispatched[1])
        attempts = self.store.dispatch_attempts("owner/repo", cycle_id=recorder.cycle_id)
        self.assertEqual(["codex", "claude", "claude"],
                         [attempt["executor"] for attempt in attempts])

    @codex_only_grant()
    def test_a_fallback_that_hits_the_same_wall_stops_to_ask_when_nobody_answers(self):
        consent = grants.ConsentBroker("owner/repo", platform="linux")
        recorder, primary, fallback, outcome = self.execute(
            fallback=RestrictedAdapter("claude"), consent=consent)

        self.assertFalse(outcome.succeeded)
        self.assertEqual((1, 1), (len(primary.dispatched), len(fallback.dispatched)))
        [request] = consent.pending
        self.assertEqual(("docker_host", "codex"), (request.grant.id, request.scope.executor))
        self.assertIn("permission required: docker_host", outcome.result.detail)
        self.assertEqual(set(), consent.granted_ids)

    @codex_only_grant()
    def test_a_fallback_that_left_effects_is_not_handed_back_to_the_primary(self):
        consent = grants.ConsentBroker("owner/repo", platform="linux",
                                       answers={"docker_host": "cycle"})
        primary, fallback = RestrictedAdapter(), RestrictedAdapter("claude")
        recorder = self.recorder([primary, fallback], consent=consent)
        snapshots = iter([CLEAN, CLEAN, CLEAN, {**CLEAN, "dirty": True}])
        with patch("cycle.effect_snapshot", side_effect=lambda *a, **k: next(snapshots)):
            outcome = recorder.stage("implement", "implement the work")

        self.assertFalse(outcome.succeeded)
        self.assertEqual((1, 1), (len(primary.dispatched), len(fallback.dispatched)))
        self.assertIn("reroute stopped: dirty_workspace", outcome.result.detail)

    def test_observed_restrictions_reroute_and_preserve_diagnostic(self):
        for kind in recovery.SANDBOX_BLOCKERS:
            with self.subTest(kind=kind):
                diagnostic = {**BLOCKER, "kind": kind}
                recorder, primary, fallback, outcome = self.execute(
                    RestrictedAdapter(blocker=diagnostic))
                self.assertTrue(outcome.succeeded)
                self.assertTrue(outcome.rerouted)
                self.assertEqual((1, 1), (len(primary.dispatched), len(fallback.dispatched)))
                self.assertIn(json.dumps(diagnostic), fallback.dispatched[0])
                self.assertEqual(primary.dispatch_kwargs[0]["writes"],
                                 fallback.dispatch_kwargs[0]["writes"])
                self.assertEqual(primary.dispatch_kwargs[0]["publishes"],
                                 fallback.dispatch_kwargs[0]["publishes"])
                self.assertEqual(primary.dispatch_kwargs[0]["publication_permissions"],
                                 fallback.dispatch_kwargs[0]["publication_permissions"])
                attempts = self.store.dispatch_attempts("owner/repo", cycle_id=recorder.cycle_id)
                self.assertEqual("environment_restricted", attempts[0]["error_code"])
                self.assertEqual("failed", attempts[0]["outcome"])
                self.assertEqual("fallback", attempts[1]["relationship"])
                self.assertEqual(attempts[0]["attempt_id"], attempts[1]["parent_attempt_id"])
                self.assertEqual(ex.Availability.READY, recorder.availability["codex"])

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
        self.assertEqual(2, len(outcome.attempts))
        self.assertIn("retry_exhausted", outcome.result.detail)
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
        self.assertIn("same_sandbox", outcome.result.detail)

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
    def test_a_recoverable_blocker_continues_the_full_cycle(self):
        fallback = Talker("claude")
        with patch("cycle.effect_snapshot", return_value=CLEAN):
            report = self.run_cycle(RestrictedAdapter(), fallback)
        self.assertEqual("READY_FOR_MANUAL_MERGE", report.status)
        self.assertEqual(["implement", "review"], [stage.role for stage in report.stages])
        self.assertTrue(report.stages[0].rerouted)
        self.assertEqual("IMPLEMENTED", report.stage_details[0].status)
        self.assertIn(json.dumps(BLOCKER), fallback.dispatched[0])

    def test_a_regular_blocker_does_not_gain_recovery(self):
        fallback = Talker("claude")
        report = self.run_cycle(Talker("codex", block("BLOCKED", error="missing credentials")),
                                fallback)
        self.assertEqual("HUMAN_INTERVENTION", report.status)
        self.assertEqual([], fallback.dispatched)


if __name__ == "__main__":
    unittest.main()
