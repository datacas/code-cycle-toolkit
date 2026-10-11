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


class SandboxRerouteTests(CycleTestCase):
    def execute(self, primary=None, fallback=None, **kwargs):
        primary = primary or RestrictedAdapter()
        fallback = fallback or ScriptedAdapter("claude")
        recorder = self.recorder([primary, fallback], **kwargs)
        with patch("cycle.effect_snapshot", return_value=CLEAN):
            outcome = recorder.stage("implement", "implement the work")
        return recorder, primary, fallback, outcome

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
