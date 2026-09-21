from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import executors as ex  # noqa: E402
import router  # noqa: E402


def present(_name):
    """Pretend the binary is on PATH: CI has neither agent installed."""
    return "/usr/bin/fake"


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


TARGET = router.parse_target("codex:openai/gpt-5.6-luna high")


class FakeAdapter(ex.Adapter):
    name = "codex"
    provable_ceiling = ex.Availability.AUTHENTICATED

    def __init__(self, probe_result, dispatch_result=None):
        self._probe = probe_result
        self._dispatch = dispatch_result
        self.dispatched = []

    def probe(self):
        return self._probe

    def dispatch(self, target, task, **kw):
        self.dispatched.append((target, task))
        return self._dispatch or ex.DispatchResult(
            ex.DispatchOutcome.SUCCEEDED, self.name, target, model_resolved=target.model
        )


def probe(state, ceiling=ex.Availability.AUTHENTICATED, proof="test"):
    return ex.ProbeResult("codex", state, proof, provable_ceiling=ceiling)


def decision(target=TARGET, mode=router.RoutingMode.PRODUCTION, blocked=False):
    return router.RoutingDecision("cheap_coder", None if blocked else target, mode, blocked=blocked)


class ProbeHonestyTests(unittest.TestCase):
    """A probe reports what it demonstrated, never what it hopes."""

    def test_a_missing_binary_is_unknown_not_installed(self) -> None:
        result = ex.CodexAdapter().probe(which=lambda _name: None)

        self.assertEqual(ex.Availability.UNKNOWN, result.availability)
        self.assertIn("not on PATH", result.proof)

    def test_native_adapters_never_claim_ready(self) -> None:
        """Remaining quota is not observable without spending it."""
        for adapter in (ex.CodexAdapter(), ex.ClaudeAdapter()):
            with self.subTest(executor=adapter.name):
                self.assertEqual(ex.Availability.AUTHENTICATED, adapter.provable_ceiling)

    def test_a_credential_gets_authenticated_and_says_why_not_more(self) -> None:
        adapter = ex.CodexAdapter()
        adapter.auth_evidence = lambda: (True, "credential file present")

        result = adapter.probe(runner=lambda *a, **k: completed("codex-cli 0.1"), which=present)

        self.assertEqual(ex.Availability.AUTHENTICATED, result.availability)
        self.assertIn("quota is not observable", result.detail)
        self.assertTrue(result.honest_ceiling_reached)

    def test_no_credential_stops_at_installed(self) -> None:
        adapter = ex.CodexAdapter()
        adapter.auth_evidence = lambda: (False, "no credential")

        result = adapter.probe(runner=lambda *a, **k: completed("codex-cli 0.1"), which=present)

        self.assertEqual(ex.Availability.INSTALLED, result.availability)

    def test_orca_can_prove_ready(self) -> None:
        payload = json.dumps({"result": {"runtime": {"state": "ready", "reachable": True}}})
        adapter = ex.OrcaAdapter()

        result = adapter.probe(runner=lambda *a, **k: completed(payload), which=present)

        self.assertEqual(ex.Availability.READY, result.availability)
        self.assertEqual(ex.Availability.READY, result.provable_ceiling)

    def test_orca_not_reachable_is_only_installed(self) -> None:
        payload = json.dumps({"result": {"runtime": {"state": "not_running", "reachable": False}}})
        adapter = ex.OrcaAdapter()

        self.assertEqual(
            ex.Availability.INSTALLED,
            adapter.probe(runner=lambda *a, **k: completed(payload), which=present).availability,
        )


class ReadinessPolicyTests(unittest.TestCase):
    def test_proven_policy_will_not_dispatch_from_authenticated(self) -> None:
        registry = ex.Registry([FakeAdapter(probe(ex.Availability.AUTHENTICATED))])
        probes = registry.probe_all()

        self.assertEqual(
            {"codex": ex.Availability.AUTHENTICATED},
            registry.availability(ex.ReadinessPolicy.PROVEN, probes),
        )

    def test_attempt_policy_promotes_only_a_ceiling_reached(self) -> None:
        registry = ex.Registry([FakeAdapter(probe(ex.Availability.AUTHENTICATED))])

        self.assertEqual(
            {"codex": ex.Availability.READY},
            registry.availability(ex.ReadinessPolicy.ATTEMPT, registry.probe_all()),
        )

    def test_attempt_policy_does_not_promote_a_lower_state(self) -> None:
        registry = ex.Registry([FakeAdapter(probe(ex.Availability.INSTALLED))])

        self.assertEqual(
            {"codex": ex.Availability.INSTALLED},
            registry.availability(ex.ReadinessPolicy.ATTEMPT, registry.probe_all()),
        )

    def test_the_promotion_is_recorded_as_policy_not_as_proof(self) -> None:
        adapter = FakeAdapter(probe(ex.Availability.AUTHENTICATED))
        registry = ex.Registry([adapter])

        result = ex.dispatch(decision(), "work", registry, policy=ex.ReadinessPolicy.ATTEMPT)

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(ex.Availability.AUTHENTICATED, result.dispatched_from)
        self.assertIn("not proven ready", result.explain())


class DispatchGateTests(unittest.TestCase):
    def test_an_unavailable_executor_blocks_naming_the_capability(self) -> None:
        registry = ex.Registry([FakeAdapter(probe(ex.Availability.INSTALLED, proof="no credential"))])

        result = ex.dispatch(decision(), "work", registry, policy=ex.ReadinessPolicy.ATTEMPT)

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("operating_availability", result.missing_capability)
        self.assertIn("no credential", result.detail)

    def test_a_blocked_decision_is_never_re_routed_here(self) -> None:
        """The router already decided; substituting now would falsify the record."""
        with self.assertRaises(ex.ExecutorError):
            ex.dispatch(decision(blocked=True), "work", ex.Registry([FakeAdapter(probe(ex.Availability.READY))]))

    def test_calibration_requires_demonstrated_readiness(self) -> None:
        adapter = FakeAdapter(probe(ex.Availability.AUTHENTICATED))
        registry = ex.Registry([adapter])

        result = ex.dispatch(
            decision(mode=router.RoutingMode.CALIBRATION), "work", registry,
            policy=ex.ReadinessPolicy.ATTEMPT,
        )

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("proven_readiness", result.missing_capability)
        self.assertEqual([], adapter.dispatched)

    def test_calibration_proceeds_when_readiness_is_proven(self) -> None:
        adapter = FakeAdapter(probe(ex.Availability.READY, ceiling=ex.Availability.READY))
        registry = ex.Registry([adapter])

        result = ex.dispatch(decision(mode=router.RoutingMode.CALIBRATION), "work", registry)

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)

    def test_an_unknown_executor_is_refused(self) -> None:
        with self.assertRaises(ex.ExecutorError):
            ex.dispatch(decision(router.parse_target("nope:x/y z")), "work",
                        ex.Registry([FakeAdapter(probe(ex.Availability.READY))]))


class InteractiveFrictionTests(unittest.TestCase):
    """A screen only a human can answer is BLOCKED, never answered blind."""

    def test_codex_recognises_a_hook_review_screen(self) -> None:
        capability, detail = ex.CodexAdapter().classify_output("Hooks need review: 5 hooks are new")

        self.assertEqual("hook_trust", capability)
        self.assertIn("interactive screen", detail)

    def test_claude_recognises_the_bypass_acknowledgement(self) -> None:
        capability, _ = ex.ClaudeAdapter().classify_output(
            "WARNING: Claude Code running in Bypass Permissions mode"
        )

        self.assertEqual("bypass_acknowledgement", capability)

    def test_claude_recognises_a_folder_trust_prompt(self) -> None:
        capability, _ = ex.ClaudeAdapter().classify_output(
            "Is this a project you created or one you trust?"
        )

        self.assertEqual("folder_trust", capability)

    def test_an_exhausted_window_is_named_as_quota_not_as_failure(self) -> None:
        capability, detail = ex.CodexAdapter().classify_output(
            "You have 2 usage limit resets available"
        )

        self.assertEqual("operating_quota", capability)
        self.assertIn("exhausted window", detail)

    def test_ordinary_output_is_not_misread_as_friction(self) -> None:
        self.assertIsNone(ex.CodexAdapter().classify_output("wrote 3 files, tests passed"))


class NativeDispatchTests(unittest.TestCase):
    """The adapters actually build a command and read what came back."""

    def test_codex_runs_non_interactively_with_the_requested_model_and_effort(self) -> None:
        seen = {}

        def runner(argv, timeout=None):
            seen["argv"] = argv
            return completed('{"model": "gpt-5.6-luna"}')

        result = ex.CodexAdapter().dispatch(TARGET, "do the thing", cwd="/tmp/x", runner=runner)

        self.assertIn("exec", seen["argv"])
        self.assertIn("gpt-5.6-luna", seen["argv"])
        self.assertIn("model_reasoning_effort=high", seen["argv"])
        self.assertIn("/tmp/x", seen["argv"])
        self.assertEqual("do the thing", seen["argv"][-1])
        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertTrue(result.model_matches_request)

    def test_claude_runs_in_print_mode_and_never_with_a_bypass_flag(self) -> None:
        """A run needing elevated permissions is a run a human should see."""
        seen = {}

        def runner(argv, timeout=None):
            seen["argv"] = argv
            return completed("{}")

        target = router.parse_target("claude:anthropic/claude-opus-5 high")
        ex.ClaudeAdapter().dispatch(target, "review it", runner=runner)

        self.assertIn("-p", seen["argv"])
        self.assertIn("claude-opus-5", seen["argv"])
        self.assertNotIn("--dangerously-skip-permissions", seen["argv"])

    def test_an_interactive_screen_blocks_instead_of_being_answered(self) -> None:
        runner = lambda argv, timeout=None: completed(
            "WARNING: Claude Code running in Bypass Permissions mode"
        )
        target = router.parse_target("claude:anthropic/claude-opus-5 high")

        result = ex.ClaudeAdapter().dispatch(target, "work", runner=runner)

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("bypass_acknowledgement", result.missing_capability)

    def test_an_exhausted_window_blocks_rather_than_failing(self) -> None:
        """Quota is a missing capability, not a broken run: retrying is useless."""
        runner = lambda argv, timeout=None: completed("You have 2 usage limit resets available")

        result = ex.CodexAdapter().dispatch(TARGET, "work", runner=runner)

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("operating_quota", result.missing_capability)

    def test_a_nonzero_exit_is_a_failure_with_its_output(self) -> None:
        runner = lambda argv, timeout=None: completed("", returncode=2, stderr="boom")

        result = ex.CodexAdapter().dispatch(TARGET, "work", runner=runner)

        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
        self.assertIn("boom", result.detail)
        self.assertEqual(2, result.artifacts["returncode"])

    def test_a_timeout_does_not_claim_the_run_is_dead(self) -> None:
        def runner(argv, timeout=None):
            raise subprocess.TimeoutExpired(argv, timeout or 1)

        result = ex.CodexAdapter().dispatch(TARGET, "work", timeout=5, runner=runner)

        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
        self.assertIn("may still be alive", result.detail)

    def test_silence_about_the_model_stays_silence(self) -> None:
        runner = lambda argv, timeout=None: completed("done, no json here")

        result = ex.CodexAdapter().dispatch(TARGET, "work", runner=runner)

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertIsNone(result.model_resolved)
        self.assertIsNone(result.model_matches_request)

    def test_a_reported_mismatch_is_visible(self) -> None:
        runner = lambda argv, timeout=None: completed('{"model": "gpt-5.6-terra"}')

        result = ex.CodexAdapter().dispatch(TARGET, "work", runner=runner)

        self.assertEqual("gpt-5.6-terra", result.model_resolved)
        self.assertFalse(result.model_matches_request)


class ModelVerificationTests(unittest.TestCase):
    def test_a_matching_model_is_confirmed(self) -> None:
        result = ex.DispatchResult(
            ex.DispatchOutcome.SUCCEEDED, "codex", TARGET, model_resolved="gpt-5.6-luna"
        )

        self.assertTrue(result.model_matches_request)

    def test_a_different_model_is_a_mismatch(self) -> None:
        result = ex.DispatchResult(
            ex.DispatchOutcome.SUCCEEDED, "codex", TARGET, model_resolved="gpt-5.6-terra"
        )

        self.assertFalse(result.model_matches_request)

    def test_an_unreported_model_is_unknown_not_a_mismatch(self) -> None:
        """Recording silence as drift would invent a drift nobody observed."""
        result = ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, "codex", TARGET)

        self.assertIsNone(result.model_matches_request)
        self.assertIn("?", result.explain())


class EndToEndTests(unittest.TestCase):
    def test_route_then_dispatch_reaches_the_chosen_target(self) -> None:
        adapter = FakeAdapter(probe(ex.Availability.READY, ceiling=ex.Availability.READY))
        registry = ex.Registry([adapter])
        d = router.route("implement", router.TaskSignals(), {"codex": ex.Availability.READY})

        result = ex.dispatch(d, "implement issue 1", registry)

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("gpt-5.6-luna", result.model_resolved)
        self.assertEqual(1, len(adapter.dispatched))

    def test_a_production_fallback_is_executed_on_the_fallback_executor(self) -> None:
        codex = FakeAdapter(probe(ex.Availability.QUOTA_EXHAUSTED))
        claude = FakeAdapter(probe(ex.Availability.READY, ceiling=ex.Availability.READY))
        claude.name = "claude"
        registry = ex.Registry([codex, claude])

        d = router.route("implement", router.TaskSignals(),
                         {"codex": ex.Availability.QUOTA_EXHAUSTED, "claude": ex.Availability.READY})
        result = ex.dispatch(d, "work", registry)

        self.assertTrue(d.used_fallback)
        self.assertEqual("claude", result.executor)
        self.assertEqual([], codex.dispatched)
        self.assertEqual(1, len(claude.dispatched))


if __name__ == "__main__":
    unittest.main()
