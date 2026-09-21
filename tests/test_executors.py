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


class DefaultPolicyTests(unittest.TestCase):
    """Neither mode should depend on someone remembering to pass a policy."""

    def test_production_defaults_to_attempt(self) -> None:
        self.assertEqual(ex.ReadinessPolicy.ATTEMPT,
                         ex.ReadinessPolicy.for_mode(router.RoutingMode.PRODUCTION))

    def test_calibration_defaults_to_proven(self) -> None:
        self.assertEqual(ex.ReadinessPolicy.PROVEN,
                         ex.ReadinessPolicy.for_mode(router.RoutingMode.CALIBRATION))

    def test_a_production_dispatch_reaches_a_native_executor_without_being_told(self) -> None:
        adapter = FakeAdapter(probe(ex.Availability.AUTHENTICATED))
        registry = ex.Registry([adapter])

        result = ex.dispatch(decision(), "work", registry)

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(ex.ReadinessPolicy.ATTEMPT, result.readiness_policy)

    def test_a_calibration_dispatch_still_refuses_an_unproven_executor(self) -> None:
        adapter = FakeAdapter(probe(ex.Availability.AUTHENTICATED))
        registry = ex.Registry([adapter])

        result = ex.dispatch(decision(mode=router.RoutingMode.CALIBRATION), "work", registry)

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual([], adapter.dispatched)


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
        capability, detail = ex.CodexAdapter().classify_failure(
            1, "Hooks need review: 5 hooks are new", ""
        )

        self.assertEqual("hook_trust", capability)
        self.assertIn("interactive screen", detail)

    def test_claude_recognises_the_bypass_acknowledgement(self) -> None:
        capability, _ = ex.ClaudeAdapter().classify_failure(
            1, "WARNING: Claude Code running in Bypass Permissions mode", ""
        )

        self.assertEqual("bypass_acknowledgement", capability)

    def test_claude_recognises_a_folder_trust_prompt(self) -> None:
        capability, _ = ex.ClaudeAdapter().classify_failure(
            1, "Is this a project you created or one you trust?", ""
        )

        self.assertEqual("folder_trust", capability)

    def test_an_exhausted_window_is_named_as_quota_not_as_failure(self) -> None:
        capability, detail = ex.CodexAdapter().classify_failure(
            1, "You have 2 usage limit resets available", ""
        )

        self.assertEqual("operating_quota", capability)
        self.assertIn("exhausted window", detail)

    def test_ordinary_output_is_not_misread_as_friction(self) -> None:
        self.assertIsNone(ex.CodexAdapter().classify_failure(1, "wrote 3 files, tests passed", ""))

    def test_a_successful_run_is_never_classified_as_friction(self) -> None:
        """The agent's own answer is not the executor's error.

        An implementation that adds rate-limit handling says "quota" for
        entirely ordinary reasons.
        """
        for text in ("Implemented quota handling and rate limit tests",
                     "Added a sign in flow",
                     "Hooks need review was the bug; fixed it"):
            with self.subTest(text=text):
                self.assertIsNone(ex.CodexAdapter().classify_failure(0, "", text))
                self.assertIsNone(ex.ClaudeAdapter().classify_failure(0, "", text))

    def test_a_successful_dispatch_survives_those_words_in_its_answer(self) -> None:
        runner = lambda argv, timeout=None, cwd=None: completed(
            "Implemented quota handling and rate limit tests"
        )

        result = ex.CodexAdapter().dispatch(TARGET, "work", runner=runner)

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertIsNone(result.missing_capability)

    def test_stderr_is_preferred_over_stdout_when_the_run_failed(self) -> None:
        capability, _ = ex.CodexAdapter().classify_failure(
            1, "usage limit reached", "Hooks need review"
        )

        self.assertEqual("operating_quota", capability)

    def test_a_failed_run_still_does_not_read_the_agent_prose(self) -> None:
        """A non-zero exit does not turn the assistant's answer into evidence."""
        self.assertIsNone(
            ex.CodexAdapter().classify_failure(1, "", "Implemented quota handling")
        )
        self.assertIsNone(
            ex.ClaudeAdapter().classify_failure(1, "", "Added a sign in flow and rate limit tests")
        )

    def test_a_structured_error_event_on_stdout_is_read(self) -> None:
        stdout = "\n".join([
            json.dumps({"type": "assistant", "text": "working on quota handling"}),
            json.dumps({"type": "error", "message": "usage limit reached"}),
        ])

        capability, _ = ex.CodexAdapter().classify_failure(1, "", stdout)

        self.assertEqual("operating_quota", capability)

    def test_a_non_error_event_mentioning_those_words_is_ignored(self) -> None:
        stdout = json.dumps({"type": "assistant", "message": "implemented usage limit handling"})

        self.assertIsNone(ex.CodexAdapter().classify_failure(1, "", stdout))

    def test_an_is_error_flag_counts_as_an_error_event(self) -> None:
        stdout = json.dumps({"is_error": True, "result": "x", "subtype": "please log in first"})

        capability, _ = ex.ClaudeAdapter().classify_failure(1, "", stdout)

        self.assertEqual("authenticated_session", capability)


class NativeDispatchTests(unittest.TestCase):
    """The adapters actually build a command and read what came back."""

    def test_codex_runs_non_interactively_with_the_requested_model_and_effort(self) -> None:
        seen = {}

        def runner(argv, timeout=None, cwd=None):
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

        def runner(argv, timeout=None, cwd=None):
            seen["argv"] = argv
            return completed("{}")

        target = router.parse_target("claude:anthropic/claude-opus-5 high")
        ex.ClaudeAdapter().dispatch(target, "review it", runner=runner)

        self.assertIn("-p", seen["argv"])
        self.assertIn("claude-opus-5", seen["argv"])
        self.assertNotIn("--dangerously-skip-permissions", seen["argv"])

    def test_an_interactive_screen_blocks_instead_of_being_answered(self) -> None:
        runner = lambda argv, timeout=None, cwd=None: completed(
            returncode=1, stderr="WARNING: Claude Code running in Bypass Permissions mode"
        )
        target = router.parse_target("claude:anthropic/claude-opus-5 high")

        result = ex.ClaudeAdapter().dispatch(target, "work", runner=runner)

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("bypass_acknowledgement", result.missing_capability)

    def test_an_exhausted_window_blocks_rather_than_failing(self) -> None:
        """Quota is a missing capability, not a broken run: retrying is useless."""
        runner = lambda argv, timeout=None, cwd=None: completed(
            returncode=1, stderr="You have 2 usage limit resets available"
        )

        result = ex.CodexAdapter().dispatch(TARGET, "work", runner=runner)

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("operating_quota", result.missing_capability)

    def test_a_nonzero_exit_is_a_failure_with_its_output(self) -> None:
        runner = lambda argv, timeout=None, cwd=None: completed("", returncode=2, stderr="boom")

        result = ex.CodexAdapter().dispatch(TARGET, "work", runner=runner)

        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
        self.assertIn("boom", result.detail)
        self.assertEqual(2, result.artifacts["returncode"])

    def test_a_timeout_does_not_claim_the_run_is_dead(self) -> None:
        def runner(argv, timeout=None, cwd=None):
            raise subprocess.TimeoutExpired(argv, timeout or 1)

        result = ex.CodexAdapter().dispatch(TARGET, "work", timeout=5, runner=runner)

        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
        self.assertIn("may still be alive", result.detail)

    def test_silence_about_the_model_stays_silence(self) -> None:
        runner = lambda argv, timeout=None, cwd=None: completed("done, no json here")

        result = ex.CodexAdapter().dispatch(TARGET, "work", runner=runner)

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertIsNone(result.model_resolved)
        self.assertIsNone(result.model_matches_request)

    def test_a_reported_mismatch_is_visible(self) -> None:
        runner = lambda argv, timeout=None, cwd=None: completed('{"model": "gpt-5.6-terra"}')

        result = ex.CodexAdapter().dispatch(TARGET, "work", runner=runner)

        self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, result.outcome)
        self.assertEqual("gpt-5.6-terra", result.model_resolved)
        self.assertEqual("gpt-5.6-luna", result.requested.model)
        self.assertFalse(result.model_matches_request)
        self.assertIn("requested gpt-5.6-luna", result.detail)


class WorkingDirectoryTests(unittest.TestCase):
    """A multi-repository dispatcher must not run in the coordinator's directory."""

    def test_codex_receives_the_directory_as_a_flag_and_on_the_process(self) -> None:
        seen = {}

        def runner(argv, timeout=None, cwd=None):
            seen.update(argv=argv, cwd=cwd)
            return completed("{}")

        ex.CodexAdapter().dispatch(TARGET, "work", cwd="/repo/api", runner=runner)

        self.assertIn("-C", seen["argv"])
        self.assertIn("/repo/api", seen["argv"])
        self.assertEqual("/repo/api", seen["cwd"])

    def test_claude_has_no_directory_flag_so_the_process_gets_one(self) -> None:
        seen = {}

        def runner(argv, timeout=None, cwd=None):
            seen.update(argv=argv, cwd=cwd)
            return completed("{}")

        target = router.parse_target("claude:anthropic/claude-opus-5 high")
        ex.ClaudeAdapter().dispatch(target, "work", cwd="/repo/api", runner=runner)

        self.assertEqual("/repo/api", seen["cwd"])
        self.assertNotIn("/repo/api", seen["argv"])

    def test_no_directory_means_no_directory_not_the_current_one(self) -> None:
        seen = {}

        def runner(argv, timeout=None, cwd=None):
            seen["cwd"] = cwd
            return completed("{}")

        ex.CodexAdapter().dispatch(TARGET, "work", runner=runner)

        self.assertIsNone(seen["cwd"])


class OrcaDispatchTests(unittest.TestCase):
    """The backend that can actually report which model it launched."""

    TARGET = router.parse_target("orca:openai/gpt-5.6-luna high")

    def receipt(self, model="gpt-5.6-luna", state="ready", ok=True, error=None):
        if not ok:
            return completed(json.dumps({"ok": False, "error": error or {"message": "nope"}}))
        return completed(json.dumps({"ok": True, "result": {
            "state": state, "dispatchId": "ctx_1",
            "launch": {"requested": {"model": model}, "effective": {"model": model}},
        }}))

    def test_it_dispatches_and_reads_the_model_from_the_receipt(self) -> None:
        seen = {}

        def runner(argv, timeout=None, cwd=None):
            seen["argv"] = argv
            return self.receipt()

        result = ex.OrcaAdapter().dispatch(
            self.TARGET, "ignored-prompt", context=ex.OrcaDispatchContext(coordinator="term_1", run_id="run_1", task_id="task_1"), runner=runner
        )

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("gpt-5.6-luna", result.model_resolved)
        self.assertTrue(result.model_matches_request)
        self.assertIn("worker-start", seen["argv"])
        self.assertIn("gpt-5.6-luna", seen["argv"])

    def test_it_refuses_to_invent_a_coordinator_or_a_run(self) -> None:
        """A dispatcher that quietly spawns terminals is one nobody can reason about."""
        result = ex.OrcaAdapter().dispatch(self.TARGET, "prompt", runner=lambda *a, **k: self.receipt())

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("orchestration_context", result.missing_capability)

    def test_a_receipt_reporting_another_model_is_a_contract_violation(self) -> None:
        result = ex.OrcaAdapter().dispatch(
            self.TARGET, "ignored-prompt", context=ex.OrcaDispatchContext(coordinator="term_1", run_id="run_1", task_id="task_1"),
            runner=lambda *a, **k: self.receipt(model="gpt-5.6-terra"),
        )

        self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, result.outcome)
        self.assertEqual("gpt-5.6-terra", result.model_resolved)

    def test_a_worker_that_did_not_reach_ready_is_a_failure(self) -> None:
        result = ex.OrcaAdapter().dispatch(
            self.TARGET, "ignored-prompt", context=ex.OrcaDispatchContext(coordinator="term_1", run_id="run_1", task_id="task_1"),
            runner=lambda *a, **k: self.receipt(state="failed"),
        )

        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)

    def test_a_rejected_worker_start_carries_its_error(self) -> None:
        result = ex.OrcaAdapter().dispatch(
            self.TARGET, "ignored-prompt", context=ex.OrcaDispatchContext(coordinator="term_1", run_id="run_1", task_id="task_1"),
            runner=lambda *a, **k: self.receipt(ok=False, error={"message": "terminal_handle_stale"}),
        )

        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
        self.assertIn("terminal_handle_stale", result.detail)

    def test_the_whole_path_runs_through_the_registry(self) -> None:
        """Target(executor='orca') reaches OrcaAdapter.dispatch, not a fake."""
        adapter = ex.OrcaAdapter()
        adapter.dispatch = lambda target, task, **kw: ex.DispatchResult(
            ex.DispatchOutcome.SUCCEEDED, "orca", target, model_resolved=target.model
        )
        registry = ex.Registry([adapter])
        d = router.RoutingDecision("cheap_coder", self.TARGET, router.RoutingMode.PRODUCTION)

        result = ex.dispatch(
            d, "prompt", registry,
            probes={"orca": ex.ProbeResult("orca", ex.Availability.READY, "runtime ready")},
        )

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("orca", result.executor)


class OrcaContractTests(unittest.TestCase):
    """The Orca command has to say what it means."""

    OPENAI = router.parse_target("orca:openai/gpt-5.6-luna high")
    ANTHROPIC = router.parse_target("orca:anthropic/claude-opus-5 high")

    def argv_for(self, target, **ctx):
        seen = {}

        def runner(argv, timeout=None, cwd=None):
            seen["argv"] = argv
            return completed(json.dumps({"ok": True, "result": {
                "state": "ready", "launch": {"effective": {"model": target.model}}}}))

        context = ex.OrcaDispatchContext(
            coordinator=ctx.get("coordinator", "term_1"),
            run_id=ctx.get("run_id", "run_1"),
            task_id=ctx.get("task_id", "task_1"),
            worker_agent=ctx.get("worker_agent"),
        )
        ex.OrcaAdapter().dispatch(target, "the prompt", context=context, runner=runner)
        return seen["argv"]

    def test_the_run_id_reaches_the_command(self) -> None:
        """Relying on whatever Run the coordinator happens to be bound to
        starts workers under the wrong Run when that binding is stale."""
        argv = self.argv_for(self.OPENAI, run_id="run_42")

        self.assertIn("--run", argv)
        self.assertEqual("run_42", argv[argv.index("--run") + 1])

    def test_the_task_id_is_sent_not_the_prompt(self) -> None:
        """--task wants Orca's Task identifier; prose there creates work
        nobody can find again."""
        argv = self.argv_for(self.OPENAI, task_id="task_99")

        self.assertEqual("task_99", argv[argv.index("--task") + 1])
        self.assertNotIn("the prompt", argv)

    def test_the_agent_follows_the_provider_not_the_executor(self) -> None:
        self.assertEqual("codex", self.argv_for(self.OPENAI)[self.argv_for(self.OPENAI).index("--agent") + 1])
        anthropic = self.argv_for(self.ANTHROPIC)
        self.assertEqual("claude", anthropic[anthropic.index("--agent") + 1])
        self.assertEqual("claude-opus-5", anthropic[anthropic.index("--model") + 1])

    def test_an_explicit_agent_overrides_the_mapping(self) -> None:
        argv = self.argv_for(self.OPENAI, worker_agent="cursor")

        self.assertEqual("cursor", argv[argv.index("--agent") + 1])

    def test_an_unmappable_provider_is_refused_rather_than_guessed(self) -> None:
        target = router.parse_target("orca:someone/their-model high")
        context = ex.OrcaDispatchContext(coordinator="t", run_id="r", task_id="k")

        result = ex.OrcaAdapter().dispatch(target, "p", context=context,
                                           runner=lambda *a, **k: completed("{}"))

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("provider_agent_mapping", result.missing_capability)

    def test_an_incomplete_context_is_refused_at_construction(self) -> None:
        for kw in ({"coordinator": ""}, {"run_id": ""}, {"task_id": ""}):
            with self.subTest(**kw):
                args = {"coordinator": "t", "run_id": "r", "task_id": "k", **kw}
                with self.assertRaises(ex.ExecutorError):
                    ex.OrcaDispatchContext(**args)


class OrcaExitStatusTests(unittest.TestCase):
    """The CLI exits 0 only for ready; the body alone is not the answer."""

    TARGET = router.parse_target("orca:openai/gpt-5.6-luna high")
    CONTEXT = None

    def setUp(self) -> None:
        self.CONTEXT = ex.OrcaDispatchContext(coordinator="t", run_id="r", task_id="k")
        self.ready_receipt = json.dumps({"ok": True, "result": {
            "state": "ready", "stage": "dispatch_input",
            "launch": {"effective": {"model": "gpt-5.6-luna"}}}})
        self.ready_status = json.dumps({"result": {"runtime": {"state": "ready", "reachable": True}}})

    def dispatch(self, returncode):
        return ex.OrcaAdapter().dispatch(
            self.TARGET, "p", context=self.CONTEXT,
            runner=lambda *a, **k: completed(self.ready_receipt, returncode=returncode),
        )

    def test_a_non_zero_exit_is_not_a_successful_launch(self) -> None:
        result = self.dispatch(1)

        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
        self.assertEqual(1, result.artifacts["returncode"])

    def test_a_zero_exit_with_a_ready_receipt_still_succeeds(self) -> None:
        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, self.dispatch(0).outcome)

    def test_the_failed_launch_keeps_its_recovery_information(self) -> None:
        body = json.dumps({"ok": True, "result": {
            "state": "failed", "failedStage": "dispatch_input",
            "lastError": "terminal_handle_stale",
            "residualResources": [{"kind": "worktree"}]}})

        result = ex.OrcaAdapter().dispatch(
            self.TARGET, "p", context=self.CONTEXT,
            runner=lambda *a, **k: completed(body, returncode=1),
        )

        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
        self.assertIn("terminal_handle_stale", result.detail)
        self.assertEqual("dispatch_input", result.artifacts["failedStage"])
        self.assertEqual([{"kind": "worktree"}], result.artifacts["residualResources"])

    def test_a_failed_status_call_does_not_prove_readiness(self) -> None:
        """Readiness is the one thing this adapter can prove, so a failed
        command does not get to prove it."""
        result = ex.OrcaAdapter().probe(
            runner=lambda *a, **k: completed(self.ready_status, returncode=1, stderr="runtime gone"),
            which=present,
        )

        self.assertEqual(ex.Availability.INSTALLED, result.availability)
        self.assertIn("exited 1", result.proof)
        self.assertIn("runtime gone", result.detail)

    def test_a_clean_status_call_still_proves_it(self) -> None:
        result = ex.OrcaAdapter().probe(
            runner=lambda *a, **k: completed(self.ready_status), which=present
        )

        self.assertEqual(ex.Availability.READY, result.availability)


class AsynchronousDispatchTests(unittest.TestCase):
    """A receipt is not a result, and the difference has to survive the trip."""

    def orca_receipt(self):
        payload = json.dumps({"ok": True, "result": {
            "dispatchId": "D-1", "state": "ready",
            "launch": {"effective": {"model": "gpt-5.6-luna"}}}})
        return ex.OrcaAdapter().dispatch(
            router.parse_target("orca:openai/gpt-5.6-luna high"), "work",
            runner=lambda *a, **k: completed(payload),
            context=ex.OrcaDispatchContext(coordinator="C", run_id="R", task_id="T"),
        )

    def test_a_started_worker_is_reported_as_asynchronous(self) -> None:
        result = self.orca_receipt()

        self.assertIs(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertTrue(result.asynchronous)

    def test_an_executor_that_runs_to_completion_is_not(self) -> None:
        result = ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, "codex", TARGET)

        self.assertFalse(result.asynchronous)

    def test_the_declaration_is_on_the_adapter_not_on_each_return(self) -> None:
        """An adapter that forgets the keyword still cannot report finished work.

        `completes_work` is the single statement of what a backend is; the
        dispatch wrapper reads it, so the property does not depend on every
        return statement remembering to say it.
        """
        class Forgetful(ex.Adapter):
            name = "orca"
            completes_work = False
            provable_ceiling = ex.Availability.READY

            def probe(self):
                return ex.ProbeResult(self.name, ex.Availability.READY, "scripted",
                                      provable_ceiling=self.provable_ceiling)

            def dispatch(self, target, task, **kw):
                return ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, self.name,
                                         target, model_resolved=target.model)

        adapter = Forgetful()
        decision = router.route("implement", router.TaskSignals(),
                                {"orca": ex.Availability.READY},
                                profiles=router.load_profiles(
                                    {"code_cycle": {"profiles": {"cheap_coder": {
                                        "primary": "orca:openai/gpt-5.6-luna high"}}}}))
        result = ex.dispatch(decision, "work", ex.Registry([adapter]),
                             policy=ex.ReadinessPolicy.PROVEN)

        self.assertTrue(result.asynchronous)

    def test_a_blocked_dispatch_started_nothing(self) -> None:
        class Refuses(ex.Adapter):
            name = "orca"
            completes_work = False
            provable_ceiling = ex.Availability.READY

            def probe(self):
                return ex.ProbeResult(self.name, ex.Availability.READY, "scripted",
                                      provable_ceiling=self.provable_ceiling)

            def dispatch(self, target, task, **kw):
                return ex.DispatchResult(
                    ex.DispatchOutcome.BLOCKED, self.name, target,
                    missing_capability="orchestration_context", detail="no run")

        decision = router.route("implement", router.TaskSignals(),
                                {"orca": ex.Availability.READY},
                                profiles=router.load_profiles(
                                    {"code_cycle": {"profiles": {"cheap_coder": {
                                        "primary": "orca:openai/gpt-5.6-luna high"}}}}))
        result = ex.dispatch(decision, "work", ex.Registry([Refuses()]),
                             policy=ex.ReadinessPolicy.PROVEN)

        self.assertFalse(result.asynchronous)

    def test_the_wrapper_keeps_what_the_adapter_reported(self) -> None:
        """It rebuilds the result, and a field it forgot would vanish silently."""
        class Detailed(ex.Adapter):
            name = "codex"
            provable_ceiling = ex.Availability.AUTHENTICATED

            def probe(self):
                return ex.ProbeResult(self.name, ex.Availability.AUTHENTICATED,
                                      "scripted", provable_ceiling=self.provable_ceiling)

            def dispatch(self, target, task, **kw):
                return ex.DispatchResult(
                    ex.DispatchOutcome.SUCCEEDED, self.name, target,
                    model_resolved=target.model, detail="all good",
                    artifacts={"stdout": "hello"})

        decision = router.route("implement", router.TaskSignals(),
                                {"codex": ex.Availability.READY})
        result = ex.dispatch(decision, "work", ex.Registry([Detailed()]),
                             policy=ex.ReadinessPolicy.ATTEMPT)

        self.assertEqual("all good", result.detail)
        self.assertEqual({"stdout": "hello"}, result.artifacts)
        self.assertEqual("gpt-5.6-luna", result.model_resolved)
        self.assertIs(ex.ReadinessPolicy.ATTEMPT, result.readiness_policy)


class LearnedAvailabilityTests(unittest.TestCase):
    """A native probe cannot see quota, so only a dispatch ever learns it."""

    def test_an_exhausted_window_is_reported_back_as_evidence(self) -> None:
        result = ex.DispatchResult(
            ex.DispatchOutcome.BLOCKED, "codex", TARGET,
            missing_capability="operating_quota", detail="exhausted",
        )

        self.assertEqual(ex.Availability.QUOTA_EXHAUSTED, result.learned_availability)

    def test_a_missing_session_is_reported_back_too(self) -> None:
        result = ex.DispatchResult(
            ex.DispatchOutcome.BLOCKED, "codex", TARGET,
            missing_capability="authenticated_session",
        )

        self.assertEqual(ex.Availability.INSTALLED, result.learned_availability)

    def test_friction_a_person_must_clear_teaches_nothing_about_availability(self) -> None:
        for capability in ("folder_trust", "hook_trust", "bypass_acknowledgement"):
            with self.subTest(capability=capability):
                result = ex.DispatchResult(
                    ex.DispatchOutcome.BLOCKED, "claude", TARGET, missing_capability=capability
                )
                self.assertIsNone(result.learned_availability)

    def test_an_interactive_screen_is_always_somebody_elses_turn(self) -> None:
        """Whatever an adapter can name as interactive friction is human action.

        Enumerating them by hand is how one gets forgotten, so the adapters are
        asked directly: a marker added to either of them without being declared
        here would let the orchestration layer route around a waiting screen."""
        for adapter in (ex.CodexAdapter, ex.ClaudeAdapter):
            for capability in adapter.interactive_markers.values():
                with self.subTest(adapter=adapter.name, capability=capability):
                    self.assertIn(capability, ex.HUMAN_ACTION_CAPABILITIES)

    def test_an_exhausted_window_is_not_somebody_elses_turn(self) -> None:
        """The one condition a fallback exists for."""
        result = ex.DispatchResult(
            ex.DispatchOutcome.BLOCKED, "codex", TARGET,
            missing_capability="operating_quota",
        )

        self.assertFalse(result.needs_human_action)

    def test_a_waiting_screen_says_so(self) -> None:
        for capability in sorted(ex.HUMAN_ACTION_CAPABILITIES):
            with self.subTest(capability=capability):
                result = ex.DispatchResult(
                    ex.DispatchOutcome.BLOCKED, "claude", TARGET,
                    missing_capability=capability,
                )
                self.assertTrue(result.needs_human_action)

    def test_a_success_teaches_nothing_that_needs_recording(self) -> None:
        result = ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, "codex", TARGET)

        self.assertIsNone(result.learned_availability)

    def test_the_evidence_makes_the_fallback_reachable(self) -> None:
        """The case the fallback exists for, end to end.

        Nobody could know Codex was exhausted before spending some, so the
        router had to pick it. Only the failed dispatch knows better.
        """
        probes = {
            "codex": ex.ProbeResult("codex", ex.Availability.AUTHENTICATED, "credential",
                                    provable_ceiling=ex.Availability.AUTHENTICATED),
            "claude": ex.ProbeResult("claude", ex.Availability.AUTHENTICATED, "onboarding",
                                     provable_ceiling=ex.Availability.AUTHENTICATED),
        }
        availability = ex.Registry().availability(ex.ReadinessPolicy.ATTEMPT, probes)
        first = router.route("implement", router.TaskSignals(), availability)
        self.assertEqual("codex", first.target.executor)
        self.assertFalse(first.used_fallback)

        blocked = ex.DispatchResult(
            ex.DispatchOutcome.BLOCKED, "codex", first.target,
            missing_capability="operating_quota",
        )
        availability["codex"] = blocked.learned_availability

        second = router.route("implement", router.TaskSignals(), availability)

        self.assertEqual("claude", second.target.executor)
        self.assertTrue(second.used_fallback)
        self.assertIn("codex is quota_exhausted", second.explain())


class LiveObservationTests(unittest.TestCase):
    """Shapes taken from a real dispatch, not from documentation."""

    def test_claude_reports_its_model_as_a_usage_key(self) -> None:
        """Observed live: no `model` field exists, so it was being lost."""
        stdout = json.dumps({
            "type": "result", "subtype": "success", "result": "OK",
            "modelUsage": {"claude-sonnet-5": {"inputTokens": 2, "outputTokens": 4}},
        })

        self.assertEqual("claude-sonnet-5", ex.ClaudeAdapter().read_resolved_model(stdout))

    def test_an_ambiguous_usage_map_reports_nothing(self) -> None:
        """Two models used means no single answer; silence beats a guess."""
        stdout = json.dumps({"modelUsage": {"claude-sonnet-5": {}, "claude-haiku-4-5": {}}})

        self.assertIsNone(ex.ClaudeAdapter().read_resolved_model(stdout))

    def test_a_claude_run_now_verifies_its_model_against_the_request(self) -> None:
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")
        stdout = json.dumps({"result": "OK", "modelUsage": {"claude-opus-5": {}}})

        result = ex.ClaudeAdapter().dispatch(
            target, "work", runner=lambda *a, **k: completed(stdout)
        )

        self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, result.outcome)
        self.assertEqual("claude-opus-5", result.model_resolved)

    def test_codex_refusing_an_untrusted_directory_is_a_capability(self) -> None:
        """Observed live: this exited non-zero with no structured event and was
        reported as a plain failure, so nobody learned what to grant."""
        stderr = ("Reading additional input from stdin...\n"
                  "Not inside a trusted directory and --skip-git-repo-check was not specified.")

        result = ex.CodexAdapter().dispatch(
            TARGET, "work", runner=lambda *a, **k: completed(returncode=1, stderr=stderr)
        )

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("trusted_directory", result.missing_capability)


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
