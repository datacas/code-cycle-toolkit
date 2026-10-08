from __future__ import annotations

import ast
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import executors as ex  # noqa: E402
import cycle_status  # noqa: E402
import router  # noqa: E402
from test_support import isolate_host_environment  # noqa: E402


_restore_host_environment = None


def setUpModule() -> None:
    global _restore_host_environment
    _restore_host_environment = isolate_host_environment()


def tearDownModule() -> None:
    if _restore_host_environment is not None:
        _restore_host_environment()


def present(_name):
    """Pretend the binary is on PATH: CI has neither agent installed."""
    return "/usr/bin/fake"


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


TARGET = router.parse_target("codex:openai/gpt-6-luna high")


class DispatchTimeoutDefaultsTests(unittest.TestCase):
    def test_every_executor_defaults_to_six_hours_per_dispatch(self) -> None:
        adapters = (
            ex.NativeAdapter,
            ex.CodexAdapter,
            ex.ClaudeAdapter,
            ex.OrcaAdapter,
        )
        for adapter in adapters:
            with self.subTest(adapter=adapter.__name__):
                default = inspect.signature(adapter.dispatch).parameters["timeout"].default
                self.assertEqual(6 * 60 * 60, default)


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


def orca_context(**kwargs):
    return ex.OrcaDispatchContext(
        coordinator=kwargs.pop("coordinator", "term_1"),
        run_id=kwargs.pop("run_id", "run_1"),
        task_id=kwargs.pop("task_id", "task_1"),
        worker_agent=kwargs.pop("worker_agent", None),
        review_workspace=kwargs.pop(
            "review_workspace",
            ex.OrcaReviewWorkspace(
                path="/repo/review",
                implementer_path="/repo/implementer",
                isolation="disposable",
            ),
        ),
        **kwargs,
    )


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def git_repo(test):
    """A committed repository that lives as long as `test`."""
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    repo = Path(temporary.name) / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Cycle Test")
    git(repo, "config", "user.email", "cycle@example.invalid")
    (repo / "tracked.txt").write_text("reviewed\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-qm", "initial")
    return repo


class ProbeHonestyTests(unittest.TestCase):
    """A probe reports what it demonstrated, never what it hopes."""

    def test_executor_runner_detaches_stdin(self) -> None:
        result = ex._run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"])
        self.assertEqual("''\n", result.stdout)

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

    def test_codex_probe_records_the_cli_version_for_permission_readiness(self) -> None:
        adapter = ex.CodexAdapter()
        adapter.auth_evidence = lambda: (True, "credential file present")

        result = adapter.probe(runner=lambda *a, **k: completed("codex-cli 0.138.2"), which=present)

        self.assertEqual((0, 138, 2), result.version)

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

        result = ex.dispatch(decision(), "work", registry, policy=ex.ReadinessPolicy.ATTEMPT,
                             writes=True)

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

        result = ex.dispatch(decision(), "work", registry, writes=True)

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(ex.ReadinessPolicy.ATTEMPT, result.readiness_policy)

    def test_a_calibration_dispatch_still_refuses_an_unproven_executor(self) -> None:
        adapter = FakeAdapter(probe(ex.Availability.AUTHENTICATED))
        registry = ex.Registry([adapter])

        result = ex.dispatch(decision(mode=router.RoutingMode.CALIBRATION), "work", registry,
                             writes=True)

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual([], adapter.dispatched)


class DispatchDurationTests(unittest.TestCase):
    """How long an executor ran, measured where it runs."""

    @staticmethod
    def clock(*readings):
        values = iter(readings)
        return lambda: next(values)

    def test_an_executed_dispatch_reports_its_monotonic_duration(self) -> None:
        registry = ex.Registry([FakeAdapter(probe(ex.Availability.AUTHENTICATED))])

        result = ex.dispatch(decision(), "work", registry,
                             writes=True, clock=self.clock(100.0, 102.5))

        self.assertEqual(2500, result.duration_ms)

    def test_a_failed_execution_still_took_time(self) -> None:
        failed = ex.DispatchResult(ex.DispatchOutcome.FAILED, "codex", TARGET)
        registry = ex.Registry([FakeAdapter(probe(ex.Availability.AUTHENTICATED), failed)])

        result = ex.dispatch(decision(), "work", registry, writes=True,
                             clock=self.clock(1.0, 1.004))

        self.assertEqual(4, result.duration_ms)

    def test_an_attempt_refused_before_executing_has_no_duration(self) -> None:
        def clock():
            raise AssertionError("nothing ran, so nothing is timed")

        registry = ex.Registry([FakeAdapter(probe(ex.Availability.INSTALLED))])

        result = ex.dispatch(decision(), "work", registry, writes=True, clock=clock)

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertIsNone(result.duration_ms)

    def test_work_that_finishes_elsewhere_has_no_duration(self) -> None:
        """Timing the launch would report a stage as fast nobody saw finish."""
        adapter = FakeAdapter(probe(ex.Availability.AUTHENTICATED))
        adapter.completes_work = False
        registry = ex.Registry([adapter])

        result = ex.dispatch(decision(), "work", registry, writes=True,
                             clock=self.clock(0.0, 3.0))

        self.assertTrue(result.asynchronous)
        self.assertIsNone(result.duration_ms)


class DispatchGateTests(unittest.TestCase):
    def test_an_unavailable_executor_blocks_naming_the_capability(self) -> None:
        registry = ex.Registry([FakeAdapter(probe(ex.Availability.INSTALLED, proof="no credential"))])

        result = ex.dispatch(decision(), "work", registry, policy=ex.ReadinessPolicy.ATTEMPT)

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("operating_availability", result.missing_capability)
        self.assertIn("no credential", result.detail)

    def test_missing_publication_permission_blocks_before_invoking_the_agent(self) -> None:
        adapter = ex.CodexAdapter()

        def runner(*args, **kwargs):
            raise AssertionError("an unsupported publication profile must not dispatch")

        result = ex.dispatch(
            decision(), "publish", ex.Registry([adapter]), publishes=True, writes=True,
            probes={"codex": ex.ProbeResult(
                "codex", ex.Availability.AUTHENTICATED, "credential present",
                provable_ceiling=ex.Availability.AUTHENTICATED, version=(0, 137, 0),
            )},
        )

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("publication_access", result.missing_capability)
        self.assertIn("0.138.0", result.detail)

    def test_failed_remote_preflight_is_recorded_before_agent_dispatch(self) -> None:
        adapter = ex.ClaudeAdapter()

        def runner(*args, **kwargs):
            raise AssertionError("the agent must not run without remote write access")

        with patch.object(ex, "_publication_preflight",
                          return_value=(False, "remote write permission is missing")):
            result = ex.dispatch(
                decision(target=router.parse_target(
                    "claude:anthropic/claude-sonnet-5 high")),
                "publish", ex.Registry([adapter]), publishes=True, writes=True,
                runner=runner,
                probes={"claude": ex.ProbeResult(
                    "claude", ex.Availability.AUTHENTICATED, "credential present",
                    provable_ceiling=ex.Availability.AUTHENTICATED,
                )},
            )

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("publication_access", result.missing_capability)
        self.assertIn("remote write", result.detail)

    def test_codex_publish_dispatch_uses_verified_scoped_permissions(self) -> None:
        seen = {}
        adapter = ex.CodexAdapter()
        ready_probe = ex.ProbeResult(
            "codex", ex.Availability.AUTHENTICATED, "credential present",
            provable_ceiling=ex.Availability.AUTHENTICATED, version=(0, 138, 0),
        )

        def runner(argv, timeout=None, cwd=None):
            seen["argv"] = argv
            return completed("done")

        with patch.object(ex, "_publication_preflight", return_value=(True, "verified")):
            result = ex.dispatch(
                decision(), "publish", ex.Registry([adapter]), publishes=True, writes=True,
                runner=runner, probes={"codex": ready_probe},
            )

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertIn('default_permissions="code_cycle_publish_write"', seen["argv"])

    def test_git_common_dir_reaches_only_writing_codex_dispatches(self) -> None:
        cwd = git_repo(self)
        common_dir = str(cwd / ".git")
        cases = (
            (ex.CodexAdapter(), TARGET, True, True),
            (ex.CodexAdapter(), TARGET, False, False),
            (ex.ClaudeAdapter(), router.parse_target(
                "claude:anthropic/claude-sonnet-5-5 high"), True, False),
        )

        for adapter, target, writes, should_forward in cases:
            with self.subTest(executor=adapter.name, writes=writes):
                readiness = ex.ProbeResult(
                    adapter.name, ex.Availability.AUTHENTICATED, "credential present",
                    provable_ceiling=ex.Availability.AUTHENTICATED,
                )
                result = ex.DispatchResult(
                    ex.DispatchOutcome.SUCCEEDED, adapter.name, target,
                    model_resolved=target.model,
                )
                with patch.object(adapter, "dispatch", return_value=result) as invoke:
                    actual = ex.dispatch(
                        decision(target), "work", ex.Registry([adapter]),
                        cwd=str(cwd), writes=writes,
                        writable_dirs=(common_dir,),
                        probes={adapter.name: readiness},
                    )

                self.assertEqual(ex.DispatchOutcome.SUCCEEDED, actual.outcome)
                forwarded = invoke.call_args.kwargs.get("writable_dirs")
                if should_forward:
                    self.assertEqual((common_dir,), forwarded)
                else:
                    self.assertIsNone(forwarded)

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

        result = ex.dispatch(decision(mode=router.RoutingMode.CALIBRATION), "work", registry,
                             writes=True)

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
            return completed('{"model": "gpt-6-luna"}')

        result = ex.CodexAdapter().dispatch(TARGET, "do the thing", cwd="/tmp/x", runner=runner)

        self.assertIn("exec", seen["argv"])
        self.assertIn("gpt-6-luna", seen["argv"])
        self.assertIn("model_reasoning_effort=high", seen["argv"])
        self.assertIn("/tmp/x", seen["argv"])
        self.assertEqual("do the thing", seen["argv"][-1])
        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertTrue(result.model_matches_request)

    def test_codex_adds_git_common_dir_only_for_workspace_write(self) -> None:
        common_dir = "/repo/.git"
        write_argv = ex.CodexAdapter().argv(
            TARGET, "implement", cwd="/repo/.worktree/task-1", writes=True,
            writable_dirs=(common_dir,),
        )
        read_argv = ex.CodexAdapter().argv(
            TARGET, "review", cwd="/repo/.worktree/task-1", writes=False,
            writable_dirs=(common_dir,),
        )

        self.assertEqual(
            ["--add-dir", common_dir],
            write_argv[write_argv.index("--add-dir"):write_argv.index("--add-dir") + 2],
        )
        self.assertEqual("implement", write_argv[-1])
        self.assertNotIn("--add-dir", read_argv)
        self.assertEqual("review", read_argv[-1])

    def test_claude_runs_in_print_mode_and_never_with_a_bypass_flag(self) -> None:
        """A run needing elevated permissions is a run a human should see."""
        seen = {}

        def runner(argv, timeout=None, cwd=None):
            seen["argv"] = argv
            return completed("{}")

        target = router.parse_target("claude:anthropic/claude-opus-5-5 high")
        ex.ClaudeAdapter().dispatch(target, "review it", runner=runner)

        self.assertIn("-p", seen["argv"])
        self.assertIn("claude-opus-5-5", seen["argv"])
        self.assertNotIn("--dangerously-skip-permissions", seen["argv"])

    def test_an_interactive_screen_blocks_instead_of_being_answered(self) -> None:
        runner = lambda argv, timeout=None, cwd=None: completed(
            returncode=1, stderr="WARNING: Claude Code running in Bypass Permissions mode"
        )
        target = router.parse_target("claude:anthropic/claude-opus-5-5 high")

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
        runner = lambda argv, timeout=None, cwd=None: completed('{"model": "gpt-6-sol"}')

        result = ex.CodexAdapter().dispatch(TARGET, "work", runner=runner)

        self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, result.outcome)
        self.assertEqual("gpt-6-sol", result.model_resolved)
        self.assertEqual("gpt-6-luna", result.requested.model)
        self.assertFalse(result.model_matches_request)
        self.assertIn("requested gpt-6-luna", result.detail)

    def test_native_dispatch_reports_its_effective_workspace(self) -> None:
        observed = []
        runner_cwds = []

        def runner(argv, timeout=None, cwd=None):
            runner_cwds.append(cwd)
            return completed("done")

        ex.CodexAdapter().dispatch(
            TARGET, "work", cwd="/tmp/worker-checkout", runner=runner,
            on_workspace=observed.append,
        )

        self.assertEqual(["/tmp/worker-checkout", "/tmp/worker-checkout"], observed)
        self.assertEqual(["/tmp/worker-checkout"], runner_cwds)


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

        target = router.parse_target("claude:anthropic/claude-opus-5-5 high")
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


class ReadDirectoryTests(unittest.TestCase):
    """Evidence written outside the workspace must be readable by the stage (#88)."""

    def argv(self, adapter, target, **kw) -> list[str]:
        seen = {}

        def runner(argv, timeout=None, cwd=None):
            seen["argv"] = argv
            return completed("{}")

        adapter.dispatch(target, "review", cwd="/repo/api", runner=runner, **kw)
        return seen["argv"]

    def test_claude_is_granted_the_directory_last_so_nothing_positional_follows(self) -> None:
        target = router.parse_target("claude:anthropic/claude-opus-5-5 high")

        argv = self.argv(ex.ClaudeAdapter(), target, read_dirs=("/tmp/code-cycle-diff-x",))

        self.assertEqual(["--add-dir", "/tmp/code-cycle-diff-x"], argv[-2:])
        self.assertEqual("review", argv[2])

    def test_claude_without_evidence_is_granted_nothing(self) -> None:
        target = router.parse_target("claude:anthropic/claude-opus-5-5 high")

        self.assertNotIn("--add-dir", self.argv(ex.ClaudeAdapter(), target))

    def test_a_writing_stage_is_never_granted_the_evidence_directory(self) -> None:
        """Claude's added directories are editable under `acceptEdits` (REV-001)."""
        seen = {}

        def runner(argv, timeout=None, cwd=None):
            seen["argv"] = argv
            return completed("{}")

        result = ex.dispatch(
            decision(target=router.parse_target("claude:anthropic/claude-sonnet-5 high")),
            "resolve", ex.Registry([ex.ClaudeAdapter()]), writes=True,
            policy=ex.ReadinessPolicy.ATTEMPT, cwd="/repo/api", runner=runner,
            read_dirs=("/tmp/code-cycle-diff-x",),
            probes={"claude": ex.ProbeResult(
                "claude", ex.Availability.AUTHENTICATED, "credential present",
                provable_ceiling=ex.Availability.AUTHENTICATED,
            )},
        )

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertIn("acceptEdits", seen["argv"])
        self.assertNotIn("--add-dir", seen["argv"])
        self.assertNotIn("/tmp/code-cycle-diff-x", seen["argv"])

    def test_codex_read_only_sandbox_already_reads_it_and_keeps_the_task_last(self) -> None:
        argv = self.argv(ex.CodexAdapter(), TARGET, writes=False,
                         read_dirs=("/tmp/code-cycle-diff-x",))

        self.assertNotIn("--add-dir", argv)
        self.assertEqual(["-s", "read-only"], argv[argv.index("-s"):argv.index("-s") + 2])
        self.assertEqual("review", argv[-1])


class OrcaDispatchTests(unittest.TestCase):
    """The backend that can actually report which model it launched."""

    TARGET = router.parse_target("orca:openai/gpt-6-luna high")

    def receipt(self, model="gpt-6-luna", state="ready", ok=True, error=None):
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
            self.TARGET, "ignored-prompt", cwd="/repo/implementer",
            context=orca_context(), runner=runner
        )

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("gpt-6-luna", result.model_resolved)
        self.assertTrue(result.model_matches_request)
        self.assertIn("worker-start", seen["argv"])
        self.assertIn("gpt-6-luna", seen["argv"])

    def test_it_reports_the_actual_review_workspace(self) -> None:
        observed = []
        seen = {}
        context = orca_context(review_workspace=ex.OrcaReviewWorkspace(
            path="/tmp/orca-review-worktree",
            implementer_path="/repo/implementer",
            isolation="disposable",
        ))

        def runner(argv, timeout=None, cwd=None):
            seen["cwd"] = cwd
            return self.receipt()

        ex.OrcaAdapter().dispatch(
            self.TARGET, "ignored-prompt", cwd="/repo/implementer",
            context=context, runner=runner, on_workspace=observed.append,
        )

        self.assertEqual("/tmp/orca-review-worktree", seen["cwd"])
        self.assertEqual([seen["cwd"], seen["cwd"]], observed)

    def test_it_refuses_to_invent_a_coordinator_or_a_run(self) -> None:
        """A dispatcher that quietly spawns terminals is one nobody can reason about."""
        result = ex.OrcaAdapter().dispatch(self.TARGET, "prompt", runner=lambda *a, **k: self.receipt())

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("orchestration_context", result.missing_capability)

    def test_a_receipt_reporting_another_model_is_a_contract_violation(self) -> None:
        result = ex.OrcaAdapter().dispatch(
            self.TARGET, "ignored-prompt", cwd="/repo/implementer",
            context=orca_context(),
            runner=lambda *a, **k: self.receipt(model="gpt-6-sol"),
        )

        self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, result.outcome)
        self.assertEqual("gpt-6-sol", result.model_resolved)

    def test_a_worker_that_did_not_reach_ready_is_a_failure(self) -> None:
        result = ex.OrcaAdapter().dispatch(
            self.TARGET, "ignored-prompt", cwd="/repo/implementer",
            context=orca_context(),
            runner=lambda *a, **k: self.receipt(state="failed"),
        )

        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)

    def test_a_rejected_worker_start_carries_its_error(self) -> None:
        result = ex.OrcaAdapter().dispatch(
            self.TARGET, "ignored-prompt", cwd="/repo/implementer",
            context=orca_context(),
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
            writes=True,
            probes={"orca": ex.ProbeResult("orca", ex.Availability.READY, "runtime ready")},
        )

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("orca", result.executor)

    def test_a_review_fallback_carries_the_isolation_contract(self) -> None:
        seen = {}

        def runner(argv, timeout=None, cwd=None):
            seen["cwd"] = cwd
            return self.receipt()

        adapter = ex.OrcaAdapter()
        implementer = git_repo(self)
        routing = router.RoutingDecision(
            "reviewer", self.TARGET, router.RoutingMode.PRODUCTION)
        result = ex.dispatch(
            routing, "review it", ex.Registry([adapter]), writes=False,
            cwd=str(implementer), runner=runner,
            context=orca_context(review_workspace=ex.OrcaReviewWorkspace(
                path="/repo/review", implementer_path=str(implementer),
                isolation="disposable")),
            probes={"orca": ex.ProbeResult("orca", ex.Availability.READY, "test")},
        )

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("/repo/review", seen["cwd"])


class OrcaContractTests(unittest.TestCase):
    """The Orca command has to say what it means."""

    OPENAI = router.parse_target("orca:openai/gpt-6-luna high")
    ANTHROPIC = router.parse_target("orca:anthropic/claude-opus-5-5 high")

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
            review_workspace=ctx.get("review_workspace", orca_context().review_workspace),
        )
        ex.OrcaAdapter().dispatch(
            target, "the prompt", cwd=ctx.get("cwd", "/repo/implementer"),
            context=context, runner=runner)
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
        self.assertEqual("claude-opus-5-5", anthropic[anthropic.index("--model") + 1])

    def test_an_explicit_agent_overrides_the_mapping(self) -> None:
        argv = self.argv_for(self.OPENAI, worker_agent="cursor")

        self.assertEqual("cursor", argv[argv.index("--agent") + 1])

    def test_an_unmappable_provider_is_refused_rather_than_guessed(self) -> None:
        target = router.parse_target("orca:someone/their-model high")
        context = orca_context(coordinator="t", run_id="r", task_id="k")

        result = ex.OrcaAdapter().dispatch(target, "p", cwd="/repo/implementer", context=context,
                                           runner=lambda *a, **k: completed("{}"))

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("provider_agent_mapping", result.missing_capability)

    def test_an_incomplete_context_is_refused_at_construction(self) -> None:
        for kw in ({"coordinator": ""}, {"run_id": ""}, {"task_id": ""}):
            with self.subTest(**kw):
                args = {"coordinator": "t", "run_id": "r", "task_id": "k", **kw}
                with self.assertRaises(ex.ExecutorError):
                    ex.OrcaDispatchContext(**args)

    def test_a_review_requires_an_explicit_isolated_workspace(self) -> None:
        result = ex.OrcaAdapter().dispatch(
            self.OPENAI, "the prompt",
            cwd="/repo/implementer",
            context=ex.OrcaDispatchContext(
                coordinator="term_1", run_id="run_1", task_id="task_1"),
            runner=lambda *args, **kwargs: self.fail("the worker must not start"),
        )

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("review_workspace_isolation", result.missing_capability)

    def test_a_review_workspace_cannot_be_the_implementer_workspace(self) -> None:
        cases = (
            ("/repo/implementer", "/repo/implementer"),
            ("/repo", "/repo/implementer"),
            ("/repo/implementer/.review", "/repo/implementer"),
        )
        for path, implementer_path in cases:
            with self.subTest(path=path, implementer_path=implementer_path):
                with self.assertRaises(ex.ExecutorError):
                    ex.OrcaReviewWorkspace(
                        path=path,
                        implementer_path=implementer_path,
                        isolation="disposable",
                    )

    def test_orca_uses_a_declared_workspace_contract_not_read_only_claim(self) -> None:
        adapter = ex.OrcaAdapter()

        self.assertFalse(adapter.enforces_read_only)
        self.assertTrue(adapter.supports_non_writing(context=orca_context()))
        self.assertFalse(adapter.supports_non_writing())

    def test_a_review_with_no_cwd_cannot_verify_the_implementer_workspace(self) -> None:
        result = ex.OrcaAdapter().dispatch(
            self.OPENAI, "the prompt", context=orca_context(),
            runner=lambda *args, **kwargs: self.fail("the worker must not start"),
        )

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("review_workspace_mismatch", result.missing_capability)

    def test_a_writing_dispatch_rejects_a_review_workspace(self) -> None:
        result = ex.OrcaAdapter().dispatch(
            self.OPENAI, "the prompt", cwd="/repo/implementer",
            context=orca_context(), writes=True,
            runner=lambda *args, **kwargs: self.fail("the worker must not start"),
        )

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("review_workspace_conflict", result.missing_capability)

    def test_a_review_runs_in_the_isolated_workspace(self) -> None:
        seen = {}

        def runner(argv, timeout=None, cwd=None):
            seen["argv"] = argv
            seen["cwd"] = cwd
            return completed(json.dumps({"ok": True, "result": {
                "state": "ready",
                "launch": {"effective": {"model": self.OPENAI.model}},
            }}))

        result = ex.OrcaAdapter().dispatch(
            self.OPENAI, "the prompt", cwd="/repo/implementer",
            context=orca_context(), runner=runner,
        )

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("/repo/review", seen["cwd"])
        self.assertEqual(
            "path:/repo/review",
            seen["argv"][seen["argv"].index("--worktree") + 1],
        )
        self.assertNotEqual("/repo/implementer", seen["cwd"])
        self.assertEqual("disposable", result.artifacts["reviewWorkspace"]["isolation"])


class OrcaExitStatusTests(unittest.TestCase):
    """The CLI exits 0 only for ready; the body alone is not the answer."""

    TARGET = router.parse_target("orca:openai/gpt-6-luna high")
    CONTEXT = None

    def setUp(self) -> None:
        self.CONTEXT = orca_context(coordinator="t", run_id="r", task_id="k")
        self.ready_receipt = json.dumps({"ok": True, "result": {
            "state": "ready", "stage": "dispatch_input",
            "launch": {"effective": {"model": "gpt-6-luna"}}}})
        self.ready_status = json.dumps({"result": {"runtime": {"state": "ready", "reachable": True}}})

    def dispatch(self, returncode):
        return ex.OrcaAdapter().dispatch(
            self.TARGET, "p", cwd="/repo/implementer", context=self.CONTEXT,
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
            self.TARGET, "p", cwd="/repo/implementer", context=self.CONTEXT,
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


class PermissionTests(unittest.TestCase):
    """What a stage is allowed to touch is what the stage is for.

    Four canary runs had an implementer that could not implement: `codex exec`
    is read-only unless told otherwise and nothing here ever told it. Claude is
    the opposite — a dispatched one edited a file with no flag at all.
    """

    def codex(self, **kw):
        return ex.CodexAdapter().argv(TARGET, "work", **kw)

    def claude(self, **kw):
        return ex.ClaudeAdapter().argv(
            router.parse_target("claude:anthropic/claude-sonnet-5 high"), "work", **kw)

    def test_a_writing_stage_may_write_where_it_was_sent(self) -> None:
        argv = self.codex(writes=True)

        self.assertIn("-s", argv)
        self.assertEqual("workspace-write", argv[argv.index("-s") + 1])

    def test_codex_publish_profile_keeps_review_read_only_and_limits_network(self) -> None:
        argv = ex.CodexAdapter().argv(TARGET, "review", writes=False, publishes=True)

        self.assertIn('default_permissions="code_cycle_publish_read"', argv)
        self.assertIn('permissions.code_cycle_publish_read.extends=":read-only"', argv)
        self.assertIn("permissions.code_cycle_publish_read.network.enabled=true", argv)
        self.assertIn(
            'permissions.code_cycle_publish_read.network.domains={"github.com"="allow",'
            '"api.github.com"="allow","bitbucket.org"="allow",'
            '"api.bitbucket.org"="allow"}',
            argv,
        )
        self.assertFalse(any('network.domains."' in arg for arg in argv))
        self.assertNotIn("-s", argv)

    def test_codex_publish_profile_preserves_workspace_write_for_implementers(self) -> None:
        argv = ex.CodexAdapter().argv(TARGET, "implement", writes=True, publishes=True)

        self.assertIn('default_permissions="code_cycle_publish_write"', argv)
        self.assertIn('permissions.code_cycle_publish_write.extends=":workspace"', argv)
        self.assertIn("permissions.code_cycle_publish_write.network.enabled=true", argv)

    def test_codex_publish_profile_reopens_only_the_supplied_git_roots(self) -> None:
        common = "/repo/.git"
        worktree = "/repo/.git/worktrees/task-203"
        argv = ex.CodexAdapter().argv(
            TARGET, "implement", writes=True, publishes=True,
            writable_dirs=(common, worktree),
        )

        filesystem = next(
            value.split("=", 1)[1]
            for flag, value in zip(argv, argv[1:])
            if flag == "-c" and value.startswith(
                "permissions.code_cycle_publish_write.filesystem=",
            )
        )
        self.assertIn("--add-dir", argv)
        self.assertIn(common, argv)
        self.assertIn(worktree, argv)
        self.assertRegex(filesystem, r'^\{.*\}$')
        entries = {
            json.loads(path): json.loads(access)
            for path, access in re.findall(r'("(?:\\.|[^"])*")=("(?:\\.|[^"])*")', filesystem)
        }
        self.assertEqual(
            {
                common: "write",
                worktree: "write",
                str(Path(common) / "hooks"): "read",
                str(Path(common) / "config"): "read",
                str(Path(worktree) / "hooks"): "read",
                str(Path(worktree) / "config.worktree"): "read",
            },
            entries,
        )
        self.assertTrue(filesystem.startswith("{"))

    def test_git_metadata_probe_uses_publish_argv_and_cleans_its_markers(self) -> None:
        adapter = ex.CodexAdapter()
        with tempfile.TemporaryDirectory() as temporary:
            common = Path(temporary) / ".git"
            worktree = common / "worktrees" / "task-203"
            common.mkdir()
            worktree.mkdir(parents=True)
            roots = (str(common), str(worktree))
            seen = {}

            def runner(argv, **kwargs):
                seen["argv"] = argv
                seen["kwargs"] = kwargs
                script = argv[-1]
                for path_text, token_text in re.findall(
                    r"Path\((.+?)\)\.write_text\((.+?)\)", script,
                ):
                    Path(ast.literal_eval(path_text)).write_text(ast.literal_eval(token_text))
                return subprocess.CompletedProcess(argv, 0, "", "")

            allowed, detail = adapter.probe_git_metadata_write(
                TARGET, temporary, roots, runner=runner,
            )

            self.assertTrue(allowed, detail)
            self.assertEqual("sandbox", seen["argv"][1])
            self.assertIn("--permission-profile", seen["argv"])
            self.assertNotIn("exec", seen["argv"])
            self.assertNotIn("-m", seen["argv"])
            self.assertEqual(temporary, seen["kwargs"]["cwd"])
            self.assertEqual(15, seen["kwargs"]["timeout"])
            self.assertFalse(list(common.glob(".code-cycle-write-probe-*")))

    @unittest.skipIf(os.name == "nt", "fake Codex executable uses a POSIX shebang")
    def test_git_metadata_probe_crosses_the_cli_process_boundary(self) -> None:
        fake_cli = (
            "#!/usr/bin/env python3\n"
            "import os, subprocess, sys\n"
            "if os.getenv('CODE_CYCLE_PROBE_FAIL'): sys.exit(7)\n"
            "command = sys.argv[sys.argv.index('--') + 1:]\n"
            "sys.exit(subprocess.run(command, check=False).returncode)\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = root / ".git"
            worktree = common / "worktrees" / "task-203"
            common.mkdir()
            worktree.mkdir(parents=True)
            binary = root / "codex"
            binary.write_text(fake_cli)
            binary.chmod(0o755)
            roots = (str(common), str(worktree))
            with patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}):
                allowed, detail = ex.CodexAdapter().probe_git_metadata_write(
                    TARGET, str(root), roots,
                )

            self.assertTrue(allowed, detail)
            self.assertFalse(list(common.glob(".code-cycle-write-probe-*")))
            with patch.dict(os.environ, {
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
                "CODE_CYCLE_PROBE_FAIL": "1",
            }):
                allowed, detail = ex.CodexAdapter().probe_git_metadata_write(
                    TARGET, str(root), roots,
                )
            self.assertFalse(allowed)
            self.assertIn("sandbox probe exited 7", detail)

    def test_a_reading_stage_says_so_rather_than_relying_on_a_default(self) -> None:
        """A default that changes is a permission nobody chose."""
        argv = self.codex(writes=False)

        self.assertEqual("read-only", argv[argv.index("-s") + 1])

    def test_asking_for_nothing_asks_for_the_smaller_permission(self) -> None:
        self.assertEqual("read-only", self.codex()[self.codex().index("-s") + 1])

    def test_no_argument_list_ever_asks_for_full_access(self) -> None:
        """The CLIs offer it; nothing here builds a command that requests it.

        Asserted over what is executed rather than over the source text, which
        mentions these names to say they are not used.
        """
        forbidden = ("danger-full-access", "--dangerously-bypass-approvals-and-sandbox",
                     "--dangerously-skip-permissions", "bypassPermissions")

        for writes in (False, True):
            for argv in (self.codex(writes=writes), self.claude(writes=writes)):
                for token in forbidden:
                    with self.subTest(writes=writes, token=token, argv=argv[0]):
                        self.assertNotIn(token, argv)

    def test_claude_declares_a_writing_stage(self) -> None:
        argv = self.claude(writes=True)

        self.assertIn("--permission-mode", argv)
        self.assertEqual("acceptEdits", argv[argv.index("--permission-mode") + 1])

    def test_claude_uses_verbose_jsonl_output(self) -> None:
        argv = self.claude()

        self.assertEqual("stream-json", argv[argv.index("--output-format") + 1])
        self.assertIn("--verbose", argv)

    def test_claude_publishing_stage_keeps_its_github_tooling(self) -> None:
        """The publication boundary is behavioural: gh and git stay available,
        and the stage's prompt says which operations it may perform."""
        for permissions in (("comment",), ("comment", "push_branch"),
                            ("comment", "create_pr", "push_branch")):
            argv = self.claude(
                writes="push_branch" in permissions, publishes=True,
                publication_permissions=permissions,
            )
            self.assertEqual(["--allowedTools", "Bash(gh:*)", "Bash(git:*)"], argv[-3:])

    def test_claude_non_publishing_stage_gets_no_extra_tools(self) -> None:
        self.assertNotIn("--allowedTools", self.claude(writes=True))

    def test_claude_claims_no_confinement_it_does_not_have(self) -> None:
        """A live probe wrote the file anyway with the edit tools disallowed,
        and plan mode refuses the edit by turning a review into planning. So a
        reading stage keeps its tools; the harness detects writes instead."""
        argv = self.claude(writes=False)

        self.assertNotIn("--permission-mode", argv)
        self.assertNotIn("--disallowedTools", argv)


    def _git(self, cwd, *args):
        return git(cwd, *args)

    def _git_repo(self):
        return git_repo(self)

    def test_remote_ref_errors_do_not_expose_embedded_credentials(self) -> None:
        remote_url = "https://user:secret@example.invalid/repo.git"
        with patch.object(
            ex, "_git_output",
            side_effect=[
                "origin", remote_url, remote_url,
                ex.ExecutorError(f"authentication failed for {remote_url}"),
            ],
        ):
            with self.assertRaises(ex.ExecutorError) as raised:
                ex._remote_ref_fingerprint("/repo")

        self.assertIn("could not inspect configured remote refs", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_git_verification_commands_are_bounded_and_noninteractive(self) -> None:
        with patch.object(ex.subprocess, "run", return_value=completed("head" + chr(10))) as run:
            self.assertEqual("head", ex._git_output("/repo", "rev-parse", "HEAD"))

        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(10, run.call_args.kwargs["timeout"])

    def test_claude_review_uses_an_independent_disposable_clone(self) -> None:
        repo = self._git_repo()
        (repo / ".git" / "info" / "exclude").write_text(".cache/", encoding="utf-8")
        (repo / ".cache").mkdir()
        (repo / ".cache" / "artifact").write_text("ignored", encoding="utf-8")
        reviewed_head = self._git(repo, "rev-parse", "HEAD")
        seen = {}
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")

        def runner(argv, timeout=None, cwd=None):
            seen["argv"], seen["cwd"] = argv, cwd
            self.assertNotEqual(str(repo), cwd)
            self.assertFalse(ex._paths_overlap(cwd, str(repo)))
            self.assertEqual(reviewed_head, self._git(cwd, "rev-parse", "HEAD"))
            self.assertTrue((Path(cwd) / ".git").is_dir())
            self.assertFalse((Path(cwd) / ".git" / "objects" / "info" / "alternates").exists())
            self.assertEqual("", self._git(cwd, "remote"))
            self._git(cwd, "branch", "reviewer-local", reviewed_head)
            self._git(cwd, "config", "cycle.shared-test", "changed")
            self.assertIn("Do not modify files, commit, or push", argv[argv.index("-p") + 1])
            (Path(cwd) / "reviewer-created.txt").write_text("discard me", encoding="utf-8")
            self._git(cwd, "add", "reviewer-created.txt")
            self._git(cwd, "config", "user.name", "Reviewer Test")
            self._git(cwd, "config", "user.email", "reviewer@example.invalid")
            self._git(cwd, "commit", "-qm", "agent edit in isolated clone")
            return completed("{}")

        result = ex.dispatch(
            router.RoutingDecision("reviewer", target, router.RoutingMode.PRODUCTION),
            "review the change", ex.Registry([ex.ClaudeAdapter()]), cwd=str(repo),
            runner=runner,
            probes={"claude": ex.ProbeResult("claude", ex.Availability.READY, "test")},
        )

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("detected", result.artifacts["read_only_mode"])
        self.assertEqual(reviewed_head, result.artifacts["implementer_head_before"])
        self.assertEqual(64, len(result.artifacts["implementer_status_fingerprint_before"]))
        self.assertIn("warning", result.detail)
        self.assertFalse(Path(seen["cwd"]).exists())
        self.assertFalse((repo / "reviewer-created.txt").exists())
        self.assertEqual("", self._git(repo, "branch", "--list", "reviewer-local"))
        shared_config = subprocess.run(
            ["git", "-C", str(repo), "config", "--local", "--get", "cycle.shared-test"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(0, shared_config.returncode)
        self.assertNotIn("--disallowedTools", seen["argv"])
        self.assertNotIn("--permission-mode", seen["argv"])

    def test_claude_review_fails_if_the_implementer_branch_moves(self) -> None:
        repo = self._git_repo()
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")
        before = self._git(repo, "rev-parse", "HEAD")

        def runner(argv, timeout=None, cwd=None):
            (repo / "branch-change.txt").write_text("changed", encoding="utf-8")
            self._git(repo, "add", "branch-change.txt")
            self._git(repo, "commit", "-qm", "unexpected branch change")
            return completed("{}")

        result = ex.dispatch(
            router.RoutingDecision("reviewer", target, router.RoutingMode.PRODUCTION),
            "review the change", ex.Registry([ex.ClaudeAdapter()]), cwd=str(repo),
            runner=runner,
            probes={"claude": ex.ProbeResult("claude", ex.Availability.READY, "test")},
        )

        self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, result.outcome)
        self.assertIn("implementer branch, HEAD", result.detail)
        self.assertNotEqual(before, self._git(repo, "rev-parse", "HEAD"))
    def test_claude_review_rejects_a_dirty_implementer_checkout(self) -> None:
        repo = self._git_repo()
        (repo / "tracked.txt").write_text("dirty before review\\n", encoding="utf-8")
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")

        result = ex.dispatch(
            router.RoutingDecision("reviewer", target, router.RoutingMode.PRODUCTION),
            "review the change", ex.Registry([ex.ClaudeAdapter()]), cwd=str(repo),
            runner=lambda *args, **kwargs: self.fail("a dirty checkout must not be reviewed"),
            probes={"claude": ex.ProbeResult("claude", ex.Availability.READY, "test")},
        )

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("review_workspace_isolation", result.missing_capability)
        self.assertNotIn("read_only_mode", result.artifacts)

    def test_claude_review_detects_a_remote_branch_push(self) -> None:
        repo = self._git_repo()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        remote = Path(temporary.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        self._git(repo, "remote", "add", "origin", str(remote))
        self._git(repo, "push", "origin", "HEAD")
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")

        def runner(argv, timeout=None, cwd=None):
            (Path(cwd) / "reviewer.txt").write_text("review result\\n", encoding="utf-8")
            self._git(cwd, "add", "reviewer.txt")
            self._git(cwd, "config", "user.name", "Reviewer Test")
            self._git(cwd, "config", "user.email", "reviewer@example.invalid")
            self._git(cwd, "commit", "-qm", "reviewer change")
            self._git(cwd, "push", str(remote), "HEAD:refs/heads/review")
            return completed("{}")

        result = ex.dispatch(
            router.RoutingDecision("reviewer", target, router.RoutingMode.PRODUCTION),
            "review the change", ex.Registry([ex.ClaudeAdapter()]), cwd=str(repo),
            runner=runner,
            probes={"claude": ex.ProbeResult("claude", ex.Availability.READY, "test")},
        )

        self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, result.outcome)
        self.assertIn("configured remote refs changed", result.detail)
        self.assertNotIn("read_only_mode", result.artifacts)
        self.assertNotEqual(
            result.artifacts["remote_refs_fingerprint_before"],
            result.artifacts["remote_refs_fingerprint_after"],
        )
        self.assertIn("reviewer.txt", self._git(remote, "ls-tree", "-r", "--name-only", "refs/heads/review"))

    def test_failed_isolation_does_not_claim_verified_mode(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")

        result = ex.dispatch(
            router.RoutingDecision("reviewer", target, router.RoutingMode.PRODUCTION),
            "review the change", ex.Registry([ex.ClaudeAdapter()]), cwd=temporary.name,
            runner=lambda *args, **kwargs: self.fail("isolation setup must fail first"),
            probes={"claude": ex.ProbeResult("claude", ex.Availability.READY, "test")},
        )

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertNotIn("read_only_mode", result.artifacts)

    @unittest.skipUnless(hasattr(ex.os, "geteuid") and ex.os.geteuid() != 0,
                         "needs POSIX permissions enforced for a non-root user")
    def test_cleanup_never_follows_a_reviewer_symlink_out_of_the_clone(self) -> None:
        repo = self._git_repo()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        external = Path(temporary.name) / "external-secret"
        external.write_text("keep me", encoding="utf-8")
        external.chmod(0o600)
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")
        seen = {}

        def runner(argv, timeout=None, cwd=None):
            seen["cwd"] = cwd
            locked = Path(cwd) / "locked"
            locked.mkdir()
            (locked / "link").symlink_to(external)
            locked.chmod(0o500)
            return completed("{}")

        result = ex.dispatch(
            router.RoutingDecision("reviewer", target, router.RoutingMode.PRODUCTION),
            "review the change", ex.Registry([ex.ClaudeAdapter()]), cwd=str(repo),
            runner=runner,
            probes={"claude": ex.ProbeResult("claude", ex.Availability.READY, "test")},
        )
        locked = Path(seen["cwd"]) / "locked"
        if locked.exists():
            self.addCleanup(ex.shutil.rmtree, seen["cwd"], ignore_errors=True)
            self.addCleanup(locked.chmod, 0o700)

        self.assertEqual(0o600, external.stat().st_mode & 0o777)
        self.assertEqual("keep me", external.read_text(encoding="utf-8"))
        self.assertFalse(Path(seen["cwd"]).exists())
        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)

    def test_a_review_clone_that_cannot_be_removed_fails_without_a_read_only_mode(self) -> None:
        repo = self._git_repo()
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")
        seen = {}
        real_rmtree = ex.shutil.rmtree

        def runner(argv, timeout=None, cwd=None):
            seen["cwd"] = cwd
            (Path(cwd) / "reviewer-created.txt").write_text("left", encoding="utf-8")
            return completed("{}")

        def locked_rmtree(path, *args, **kwargs):
            if str(path) == seen.get("cwd"):
                if kwargs.get("ignore_errors"):
                    return None
                raise PermissionError("locked")
            return real_rmtree(path, *args, **kwargs)

        with patch.object(ex.shutil, "rmtree", side_effect=locked_rmtree):
            result = ex.dispatch(
                router.RoutingDecision("reviewer", target, router.RoutingMode.PRODUCTION),
                "review the change", ex.Registry([ex.ClaudeAdapter()]), cwd=str(repo),
                runner=runner,
                probes={"claude": ex.ProbeResult("claude", ex.Availability.READY, "test")},
            )
        self.addCleanup(real_rmtree, seen["cwd"], ignore_errors=True)

        self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
        self.assertNotIn("read_only_mode", result.artifacts)
        self.assertIn("could not be removed", result.detail)
        self.assertIn(seen["cwd"], result.detail)
        self.assertTrue(Path(seen["cwd"]).exists())


class PublicationPreflightTests(unittest.TestCase):
    def test_github_write_permission_uses_repository_permission_write_value(self) -> None:
        observed_stdin = []

        def run(argv, **_kwargs):
            observed_stdin.append(_kwargs.get("stdin"))
            if argv == ["git", "rev-parse", "--show-toplevel"]:
                return completed("/repo\n")
            if argv == ["git", "branch", "--show-current"]:
                return completed("feature\n")
            if argv == ["git", "remote", "get-url", "origin"]:
                return completed("https://github.com/owner/repo.git\n")
            if argv == ["gh", "repo", "view", "--json", "nameWithOwner,viewerPermission"]:
                return completed('{"nameWithOwner":"owner/repo","viewerPermission":"WRITE"}')
            if argv == ["gh", "auth", "status"]:
                return completed()
            if argv == ["git", "push", "--dry-run", "origin",
                        "HEAD:refs/heads/cc-cycle-preflight"]:
                return completed()
            self.fail(f"unexpected preflight command: {argv!r}")

        with patch.object(ex.subprocess, "run", side_effect=run), \
             patch.object(ex.shutil, "which", return_value="/usr/bin/gh"):
            ready, detail = ex._publication_preflight("/repo")

        self.assertTrue(ready)
        self.assertIn("passed", detail)
        self.assertTrue(observed_stdin)
        self.assertTrue(all(value is subprocess.DEVNULL for value in observed_stdin))

    def test_a_reading_dispatch_fails_closed_for_an_unconfined_adapter(self) -> None:
        """A configured Claude review cannot silently share the writable tree."""
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")
        decision = router.RoutingDecision("reviewer", target, router.RoutingMode.PRODUCTION)
        adapter = ex.ClaudeAdapter()

        def runner(*args, **kwargs):
            raise AssertionError("the adapter must not be invoked")

        result = ex.dispatch(
            decision, "review it", ex.Registry([adapter]), writes=False, runner=runner,
            probes={"claude": ex.ProbeResult("claude", ex.Availability.READY, "test")},
        )

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("read_only_enforcement", result.missing_capability)

    def test_an_omitted_permission_defaults_to_the_same_read_only_boundary(self) -> None:
        """The dispatch contract must not become fail-open outside CycleRecorder."""
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")
        decision = router.RoutingDecision("reviewer", target, router.RoutingMode.PRODUCTION)
        adapter = ex.ClaudeAdapter()

        result = ex.dispatch(
            decision, "review it", ex.Registry([adapter]),
            runner=lambda *args, **kwargs: self.fail("the adapter must not be invoked"),
            probes={"claude": ex.ProbeResult("claude", ex.Availability.READY, "test")},
        )

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("read_only_enforcement", result.missing_capability)

    def test_a_reading_dispatch_reaches_an_adapter_with_enforced_sandboxing(self) -> None:
        target = router.parse_target("codex:openai/gpt-6-sol high")
        decision = router.RoutingDecision("reviewer", target, router.RoutingMode.PRODUCTION)
        adapter = ex.CodexAdapter()
        seen = {}

        def runner(argv, timeout=None, cwd=None):
            seen["argv"] = argv
            return completed("{}")

        result = ex.dispatch(
            decision, "review it", ex.Registry([adapter]), writes=False, runner=runner,
            probes={"codex": ex.ProbeResult("codex", ex.Availability.READY, "test")},
        )

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("enforced", result.artifacts["read_only_mode"])
        self.assertEqual("read-only", seen["argv"][seen["argv"].index("-s") + 1])

    def test_disposable_dispatch_requires_an_isolated_workspace(self) -> None:
        target = router.parse_target("codex:openai/gpt-6-sol high")
        decision = router.RoutingDecision("cheap_tool", target, router.RoutingMode.PRODUCTION)
        adapter = ex.CodexAdapter()

        result = ex.dispatch(
            decision, "run checks", ex.Registry([adapter]), writes=True,
            workspace_policy=ex.WorkspacePolicy.DISPOSABLE,
            runner=lambda *args, **kwargs: self.fail("the adapter must not be invoked"),
            probes={"codex": ex.ProbeResult("codex", ex.Availability.READY, "test")},
        )

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("disposable_workspace", result.missing_capability)
        self.assertIn("DisposableWorkspace", result.detail)

    def test_disposable_dispatch_fails_closed_for_unconfined_claude(self) -> None:
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")
        decision = router.RoutingDecision("auxiliary_tool", target, router.RoutingMode.PRODUCTION)
        adapter = ex.ClaudeAdapter()
        workspace = ex.DisposableWorkspace("/tmp/verification-worktree", "/repo/source")

        result = ex.dispatch(
            decision, "run checks", ex.Registry([adapter]), cwd=workspace.path, writes=True,
            workspace=workspace, workspace_policy=ex.WorkspacePolicy.DISPOSABLE,
            runner=lambda *args, **kwargs: self.fail("the adapter must not be invoked"),
            probes={"claude": ex.ProbeResult("claude", ex.Availability.READY, "test")},
        )

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("disposable_workspace", result.missing_capability)
        self.assertIn("cannot confine writes", result.detail)

    def test_disposable_dispatch_uses_the_declared_workspace(self) -> None:
        target = router.parse_target("codex:openai/gpt-6-sol high")
        decision = router.RoutingDecision("cheap_tool", target, router.RoutingMode.PRODUCTION)
        adapter = ex.CodexAdapter()
        seen = {}
        workspace = ex.DisposableWorkspace("/tmp/verification-worktree", "/repo/source")

        def runner(argv, timeout=None, cwd=None):
            seen["argv"] = argv
            seen["cwd"] = cwd
            return completed("{}")

        result = ex.dispatch(
            decision, "run checks", ex.Registry([adapter]), cwd=workspace.path, writes=True,
            workspace=workspace, workspace_policy=ex.WorkspacePolicy.DISPOSABLE,
            runner=runner,
            probes={"codex": ex.ProbeResult("codex", ex.Availability.READY, "test")},
        )

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(workspace.path, seen["cwd"])
        self.assertEqual(workspace.path, seen["argv"][seen["argv"].index("-C") + 1])

    def test_disposable_workspace_cannot_overlap_the_source(self) -> None:
        with self.assertRaises(ex.ExecutorError):
            ex.DisposableWorkspace("/repo/source/build", "/repo/source")

    def test_the_permission_reaches_the_adapter_from_the_dispatch(self) -> None:
        seen = {}

        class Watching(ex.CodexAdapter):
            def argv(self, target, task, cwd=None, writes=False):
                seen["writes"] = writes
                return ["true"]

        adapter = Watching()
        adapter.dispatch(TARGET, "work", writes=True,
                         runner=lambda *a, **k: completed("{}"))

        self.assertIs(True, seen["writes"])


class AgentOutputTests(unittest.TestCase):
    """Each CLI wraps the reply, and each adapter unwraps its own.

    The fixtures are live captures, not guesses. Before them the driver read the
    envelope as if it were the reply, found `ORCHESTRATION_RESULT` in it, and
    then failed to parse the block because the newlines and quotes inside a JSON
    string are still escapes. No fake caught that, because the fakes printed the
    block as plain text.
    """

    FIXTURES = Path(__file__).resolve().parent / "fakes"

    def fixture(self, name: str) -> str:
        return (self.FIXTURES / name).read_text(encoding="utf-8")

    def test_codex_speaks_through_its_agent_messages(self) -> None:
        spoken = ex.CodexAdapter().agent_output(self.fixture("codex_output.ndjson"))

        self.assertTrue(spoken.startswith("ORCHESTRATION_RESULT"))
        self.assertIn('"status": "IMPLEMENTED"', spoken)
        self.assertNotIn("turn.completed", spoken)

    def test_claude_speaks_through_its_result_field(self) -> None:
        spoken = ex.ClaudeAdapter().agent_output(self.fixture("claude_output.json"))

        self.assertIn("ORCHESTRATION_RESULT", spoken)
        self.assertNotIn("modelUsage", spoken)

    def test_claude_speaks_through_the_final_jsonl_result_event(self) -> None:
        message = json.dumps({"status": "APPROVED"})
        stdout = chr(10).join((
            json.dumps({"type": "assistant", "message": {
                "content": [{"type": "text", "text": "progress"}],
            }}),
            json.dumps({"type": "result", "result": message,
                        "modelUsage": {"claude-sonnet-5": {}}}),
        ))

        self.assertEqual(message, ex.ClaudeAdapter().agent_output(stdout))

    def test_native_json_events_update_activity_and_tool_count(self) -> None:
        codex_events = []
        claude_events = []
        codex = ex.CodexAdapter()
        codex._stream_activity(json.dumps({
            "type": "item.started", "item": {"type": "command_execution"},
        }), lambda **event: codex_events.append(event))
        codex._stream_activity(json.dumps({
            "type": "item.updated",
            "item": {"type": "agent_message", "text": "Codex is working"},
        }), lambda **event: codex_events.append(event))
        codex._stream_activity(json.dumps({
            "type": "item.started",
            "item": {
                "id": "collab-1",
                "type": "collab_tool_call",
                "tool": "spawnAgent",
                "receiver_agents": [
                    {"thread_id": "agent-1", "agent_role": "security"},
                ],
            },
        }), lambda **event: codex_events.append(event))
        codex._stream_activity(json.dumps({
            "type": "item.completed",
            "item": {"id": "collab-1", "type": "collab_tool_call"},
        }), lambda **event: codex_events.append(event))

        claude = ex.ClaudeAdapter()
        claude._stream_activity(json.dumps({
            "type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "bash-1", "name": "Bash"},
                {"type": "tool_use", "id": "task-1", "name": "Agent",
                 "input": {"subagent_type": "security"}},
                {"type": "text", "text": "Claude is working"},
            ]},
        }), lambda **event: claude_events.append(event))
        claude._stream_activity(json.dumps({
            "type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "bash-1", "content": "read"},
            ]},
        }), lambda **event: claude_events.append(event))
        claude._stream_activity(json.dumps({
            "type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "task-1", "content": "done"},
            ]},
        }), lambda **event: claude_events.append(event))

        self.assertIn({"text": "command execution started", "tool": True}, codex_events)
        self.assertIn({"text": "Codex is working"}, codex_events)
        self.assertIn({
            "text": "delegated security started",
            "tool": True,
            "delegation_id": "collab-1",
            "delegated_stage": "security",
        }, codex_events)
        self.assertIn({
            "text": "delegated call completed",
            "delegation_id": "collab-1",
            "delegation_finished": True,
        }, codex_events)
        self.assertIn({"text": "Using Bash", "tool": True}, claude_events)
        self.assertIn({"text": "Claude is working"}, claude_events)
        self.assertIn({
            "text": "delegated security started",
            "tool": True,
            "delegation_id": "task-1",
            "delegated_stage": "security",
        }, claude_events)
        self.assertIn({
            "text": "delegated stage finished",
            "delegation_id": "task-1",
            "delegation_finished": True,
        }, claude_events)
        self.assertEqual(
            ["task-1"],
            [event["delegation_id"] for event in claude_events
             if event.get("delegation_finished")],
        )

    def test_codex_spawn_status_tracks_child_after_call_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            writer = cycle_status.CycleStatusWriter(
                Path(temporary) / "telemetry.sqlite", "codex-child-test", "owner/repo", "ISSUE-7",
            )
            target = SimpleNamespace(
                executor="codex", provider="openai", model="gpt-6-luna", effort="high",
            )
            decision = SimpleNamespace(target=target, profile="cheap_coder", used_fallback=False)
            writer.stage_started("resolve", decision)

            codex = ex.CodexAdapter()
            callback = writer.activity
            codex._stream_activity(json.dumps({
                "type": "item.started",
                "item": {
                    "id": "collab-spawn",
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "prompt": "Run the security review",
                    "model": "gpt-6-sol",
                    "reasoning_effort": "high",
                    "receiver_thread_ids": [],
                    "agents_states": {},
                },
            }), callback)
            codex._stream_activity(json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "collab-spawn",
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "prompt": "Run the security review",
                    "model": "gpt-6-sol",
                    "reasoning_effort": "high",
                    "receiver_thread_ids": ["thread-child"],
                    "agents_states": {
                        "thread-child": {"status": "running", "message": None},
                    },
                },
            }), callback)

            active = json.loads(writer.path.read_text(encoding="utf-8"))["stage"]
            self.assertEqual("resolve", active["parent_stage"])
            self.assertEqual("security", active["substage"])
            self.assertTrue(active["delegated"])
            line = cycle_status.format_progress_line({"stage": active})
            self.assertIn("resolve › security (delegated)", line)
            self.assertIn("unknown/gpt-6-sol high", line)
            self.assertNotIn("openai/gpt-6-luna", line)
            self.assertIn("delegated agent thread-child active", line)
            self.assertNotIn("delegated stage finished", line)

            codex._stream_activity(json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "collab-wait",
                    "type": "collab_tool_call",
                    "tool": "wait",
                    "receiver_thread_ids": ["thread-child"],
                    "agents_states": {
                        "thread-child": {"status": "completed", "message": None},
                    },
                },
            }), callback)

            finished = json.loads(writer.path.read_text(encoding="utf-8"))["stage"]
            self.assertNotIn("parent_stage", finished)
            self.assertNotIn("substage", finished)
            self.assertNotIn("delegated", finished)

    def test_codex_json_child_events_cross_process_boundary_and_keep_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            writer = cycle_status.CycleStatusWriter(
                root / "telemetry.sqlite", "codex-process-test", "owner/repo", "ISSUE-7",
            )
            target = SimpleNamespace(
                executor="codex", provider="openai", model="gpt-6-luna", effort="high",
            )
            decision = SimpleNamespace(target=target, profile="cheap_coder", used_fallback=False)
            writer.stage_started("resolve", decision)

            events = [
                {
                    "type": "item.started",
                    "item": {
                        "id": "collab-spawn",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "prompt": "Run the security review",
                        "model": "gpt-6-sol",
                        "reasoning_effort": "high",
                        "receiver_thread_ids": [],
                        "agents_states": {},
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "collab-spawn",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "prompt": "Run the security review",
                        "model": "gpt-6-sol",
                        "reasoning_effort": "high",
                        "receiver_thread_ids": ["thread-child"],
                        "agents_states": {
                            "thread-child": {"status": "running", "message": None},
                        },
                    },
                },
            ]
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                "import json, sys\n"
                f"events = {events!r}\n"
                "for event in events:\n"
                "    print(json.dumps(event), flush=True)\n"
                "sys.exit(7)\n",
                encoding="utf-8",
            )

            adapter = ex.CodexAdapter()
            with patch.object(adapter, "argv", return_value=[sys.executable, str(fake_codex)]):
                result = adapter.dispatch(
                    TARGET, "exercise delegated JSON output", cwd=temporary,
                    runner=ex._run, on_progress=writer.activity,
                )

            self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
            self.assertEqual("failed", result.lifecycle_state)
            self.assertEqual(7, result.artifacts["returncode"])
            active = json.loads(writer.path.read_text(encoding="utf-8"))["stage"]
            self.assertEqual("security", active["substage"])
            line = cycle_status.format_progress_line({"stage": active})
            self.assertIn("unknown/gpt-6-sol high", line)
            self.assertNotIn("delegated stage finished", line)

    def test_native_runner_streams_stdout_to_its_callback(self) -> None:
        output = []
        script = "import time; print('first', flush=True); time.sleep(.02); print('second', flush=True)"
        completed = ex._run([__import__("sys").executable, "-c", script],
                            timeout=2, on_output=output.append)

        self.assertEqual(0, completed.returncode)
        self.assertEqual(["first" + chr(10), "second" + chr(10)], output)
        self.assertEqual("first" + chr(10) + "second" + chr(10), completed.stdout)

    @unittest.skipUnless(hasattr(__import__("signal"), "SIGUSR1"), "needs POSIX signals")
    def test_an_interrupted_streaming_run_stops_its_agent(self) -> None:
        """The host stops the cycle; the agent it started must not keep going."""
        import os
        import signal
        import threading

        class Stop(BaseException):
            pass

        def interrupt(signum, frame):
            raise Stop()

        pids: list[int] = []
        script = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"
        previous = signal.signal(signal.SIGUSR1, interrupt)
        timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGUSR1))
        try:
            timer.start()
            with self.assertRaises(Stop):
                ex._run([sys.executable, "-c", script], timeout=30,
                        on_output=lambda line: pids.append(int(line)))
        finally:
            timer.cancel()
            signal.signal(signal.SIGUSR1, previous)

        self.assertEqual(1, len(pids))
        with self.assertRaises(ProcessLookupError):
            os.kill(pids[0], 0)

    def test_the_block_survives_the_envelope_intact(self) -> None:
        """The whole point: escaped in the envelope, parseable once unwrapped."""
        raw = self.fixture("claude_output.json")
        spoken = ex.ClaudeAdapter().agent_output(raw)

        self.assertIn("ORCHESTRATION_RESULT\\n", raw)      # escaped in the envelope
        self.assertIn("ORCHESTRATION_RESULT\n", spoken)     # a real newline after
        body = spoken.split("ORCHESTRATION_RESULT", 1)[1].split("END_", 1)[0]
        self.assertEqual("CHANGES_REQUESTED", json.loads(body.strip())["status"])

    def test_an_envelope_it_does_not_recognise_is_passed_through(self) -> None:
        """Returning a filtered part of it would silently drop the rest."""
        for adapter in (ex.CodexAdapter(), ex.ClaudeAdapter()):
            with self.subTest(adapter=adapter.name):
                self.assertEqual("plain words", adapter.agent_output("plain words"))

    def test_a_dispatch_carries_what_the_agent_said(self) -> None:
        adapter = ex.CodexAdapter()
        result = adapter.dispatch(
            TARGET, "work",
            runner=lambda *a, **k: completed(self.fixture("codex_output.ndjson")),
        )

        self.assertIn("ORCHESTRATION_RESULT", result.agent_output)
        self.assertNotIn("thread.started", result.agent_output)

    def test_a_reply_that_keeps_talking_keeps_its_result(self) -> None:
        """A length cap on the parsed text silently deletes valid results.

        The block comes first and the agent carries on afterwards, which is
        ordinary: a summary, next steps, a closing paragraph. Cutting the tail
        cut the block's opening delimiter with it, and the cycle then recorded a
        completed stage as having reported nothing.
        """
        block = ('ORCHESTRATION_RESULT\n{"skill": "fake", "status": "IMPLEMENTED"}\n'
                 "END_ORCHESTRATION_RESULT")
        text = block + "\n" + ("and then some more. " * 600)
        line = json.dumps({"type": "item.completed",
                           "item": {"type": "agent_message", "text": text}})

        result = ex.CodexAdapter().dispatch(
            TARGET, "work", runner=lambda *a, **k: completed(line))

        self.assertEqual(len(text), len(result.agent_output))
        self.assertIn("ORCHESTRATION_RESULT", result.agent_output)

    def test_the_diagnostic_tail_is_still_bounded(self) -> None:
        """What a person reads afterwards is capped; what the caller parses is
        not. Two jobs, and only one of them may be cut."""
        line = json.dumps({"type": "item.completed",
                           "item": {"type": "agent_message", "text": "x" * 20000}})

        result = ex.CodexAdapter().dispatch(
            TARGET, "work", runner=lambda *a, **k: completed(line))

        self.assertGreaterEqual(4000, len(result.artifacts["stdout"]))
        self.assertEqual(20000, len(result.agent_output))

    def test_a_started_worker_says_nothing_of_its_own(self) -> None:
        """Orca's receipt is not a reply; its stage answers elsewhere."""
        result = ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, "orca", TARGET,
                                   asynchronous=True)

        self.assertEqual("", result.agent_output)


class AsynchronousDispatchTests(unittest.TestCase):
    """A receipt is not a result, and the difference has to survive the trip."""

    def orca_receipt(self):
        payload = json.dumps({"ok": True, "result": {
            "dispatchId": "D-1", "state": "ready",
            "launch": {"effective": {"model": "gpt-6-luna"}}}})
        return ex.OrcaAdapter().dispatch(
            router.parse_target("orca:openai/gpt-6-luna high"), "work",
            runner=lambda *a, **k: completed(payload),
            cwd="/repo/implementer",
            context=orca_context(coordinator="C", run_id="R", task_id="T"),
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
                                        "primary": "orca:openai/gpt-6-luna high"}}}}))
        result = ex.dispatch(decision, "work", ex.Registry([adapter]),
                             policy=ex.ReadinessPolicy.PROVEN, writes=True)

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
                                        "primary": "orca:openai/gpt-6-luna high"}}}}))
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
                             policy=ex.ReadinessPolicy.ATTEMPT, writes=True)

        self.assertEqual("all good", result.detail)
        self.assertEqual({"stdout": "hello"}, result.artifacts)
        self.assertEqual("gpt-6-luna", result.model_resolved)
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
        stdout = json.dumps({"result": "OK", "modelUsage": {"claude-opus-5-5": {}}})

        result = ex.ClaudeAdapter().dispatch(
            target, "work", runner=lambda *a, **k: completed(stdout)
        )

        self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, result.outcome)
        self.assertEqual("claude-opus-5-5", result.model_resolved)

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
            ex.DispatchOutcome.SUCCEEDED, "codex", TARGET, model_resolved="gpt-6-luna"
        )

        self.assertTrue(result.model_matches_request)

    def test_a_different_model_is_a_mismatch(self) -> None:
        result = ex.DispatchResult(
            ex.DispatchOutcome.SUCCEEDED, "codex", TARGET, model_resolved="gpt-6-sol"
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

        result = ex.dispatch(d, "implement issue 1", registry, writes=True)

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("gpt-6-luna", result.model_resolved)
        self.assertEqual(1, len(adapter.dispatched))

    def test_a_production_fallback_is_executed_on_the_fallback_executor(self) -> None:
        codex = FakeAdapter(probe(ex.Availability.QUOTA_EXHAUSTED))
        claude = FakeAdapter(probe(ex.Availability.READY, ceiling=ex.Availability.READY))
        claude.name = "claude"
        registry = ex.Registry([codex, claude])

        d = router.route("implement", router.TaskSignals(),
                         {"codex": ex.Availability.QUOTA_EXHAUSTED, "claude": ex.Availability.READY})
        result = ex.dispatch(d, "work", registry, writes=True)

        self.assertTrue(d.used_fallback)
        self.assertEqual("claude", result.executor)
        self.assertEqual([], codex.dispatched)
        self.assertEqual(1, len(claude.dispatched))


# A stand-in agent. It prints what every adapter here accepts as a finished
# run, and, when asked, writes to the reviewed checkout either itself or from a
# separate process it starts, which is what an MCP server or a hook is.
FAKE_AGENT = """import os, subprocess, sys
target = os.environ.get("CYCLE_TEST_WRITE")
mode = os.environ.get("CYCLE_TEST_MODE", "none")
code = "import sys; open(sys.argv[1], 'a').write('mutated\\\\n')"
if target and mode == "agent":
    open(target, "a").write("mutated\\n")
elif target and mode == "child":
    subprocess.run([sys.executable, "-c", code, target], check=True)
with open(os.environ["CYCLE_TEST_ARGV"], "w") as handle:
    handle.write("\\0".join(sys.argv[1:]))
print("{}")
"""


class ReadOnlyVerificationTests(unittest.TestCase):
    """Read-only is about the result: whatever wrote, a changed tree fails."""

    CODEX = router.parse_target("codex:openai/gpt-6-sol high")
    CLAUDE = router.parse_target("claude:anthropic/claude-sonnet-5 high")
    ORCA = router.parse_target("orca:openai/gpt-6-luna high")

    def setUp(self) -> None:
        self.repo = git_repo(self)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.agent = Path(scratch.name) / "fake_agent.py"
        self.agent.write_text(FAKE_AGENT, encoding="utf-8")
        self.argv_file = Path(scratch.name) / "argv"

    def dispatch(self, executor: str, mode: str, **kw):
        """Dispatch a review on `executor` whose agent writes as `mode` says."""
        env = {"CYCLE_TEST_MODE": mode, "CYCLE_TEST_ARGV": str(self.argv_file),
               "CYCLE_TEST_WRITE": str(self.repo / "tracked.txt")}
        probes = {executor: ex.ProbeResult(executor, ex.Availability.READY, "test")}
        if executor == "orca":
            adapter, target = ex.OrcaAdapter(), self.ORCA

            def runner(argv, timeout=None, cwd=None):
                subprocess.run([sys.executable, str(self.agent)], check=True,
                               capture_output=True)
                return completed(json.dumps({"ok": True, "result": {
                    "state": "ready", "launch": {"effective": {"model": target.model}}}}))

            kw.update(runner=runner, context=orca_context(
                review_workspace=ex.OrcaReviewWorkspace(
                    path=tempfile.gettempdir() + "/cycle-review-elsewhere",
                    implementer_path=str(self.repo), isolation="disposable")))
        else:
            base = ex.CodexAdapter if executor == "codex" else ex.ClaudeAdapter
            target = self.CODEX if executor == "codex" else self.CLAUDE
            agent = str(self.agent)

            class Launched(base):
                # The interpreter starts the fake, so no platform needs to
                # execute a script directly.
                def argv(self, *args, **kwargs):
                    return [sys.executable, agent, *super().argv(*args, **kwargs)[1:]]

            adapter = Launched()
        kw.setdefault("cwd", str(self.repo))
        with patch.dict(ex.os.environ, env):
            return ex.dispatch(
                router.RoutingDecision("reviewer", target, router.RoutingMode.PRODUCTION),
                "review it", ex.Registry([adapter]), writes=False, probes=probes, **kw,
            )

    def test_a_codex_stage_that_changes_nothing_stays_enforced(self) -> None:
        result = self.dispatch("codex", "none")

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("enforced", result.artifacts["read_only_mode"])
        self.assertEqual("verified", result.artifacts["read_only_verification"])
        self.assertEqual(result.artifacts["workspace_fingerprint_before"],
                         result.artifacts["workspace_fingerprint_after"])
        argv = self.argv_file.read_text(encoding="utf-8").split("\0")
        self.assertEqual("read-only", argv[argv.index("-s") + 1])
        self.assertFalse({"--dangerously-bypass-approvals-and-sandbox", "--yolo",
                          "--full-auto"} & set(argv))

    def test_isolated_claude_review_reports_the_disposable_clone(self) -> None:
        observed = []
        result = self.dispatch(
            "claude", "none",
            on_workspace=lambda path: observed.append((path, Path(path).exists())),
        )

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(2, len(observed))
        self.assertTrue(all(exists for _, exists in observed))
        self.assertTrue(Path(observed[0][0]).name.startswith("code-cycle-review-"))
        self.assertNotEqual(str(self.repo), observed[0][0])
        self.assertEqual(observed[0][0], observed[1][0])
        self.assertFalse(Path(observed[0][0]).exists())

    def test_a_codex_stage_that_writes_is_a_contract_violation(self) -> None:
        """The sandbox flag confines the agent's own commands, not a process
        the host launched for it, so the flag alone proves nothing."""
        for mode in ("agent", "child"):
            with self.subTest(mode=mode):
                git(self.repo, "checkout", "--", "tracked.txt")

                result = self.dispatch("codex", mode)

                self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, result.outcome)
                self.assertNotIn("read_only_mode", result.artifacts)
                self.assertIn("read-only contract violation", result.detail)

    def test_every_adapter_fails_a_stage_that_writes_the_checkout(self) -> None:
        for executor in ("codex", "claude", "orca"):
            for mode in ("agent", "child"):
                with self.subTest(executor=executor, mode=mode):
                    git(self.repo, "checkout", "--", "tracked.txt")

                    result = self.dispatch(executor, mode)

                    self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, result.outcome)
                    self.assertNotIn("read_only_mode", result.artifacts)
                    self.assertNotEqual(result.artifacts["workspace_fingerprint_before"],
                                        result.artifacts["workspace_fingerprint_after"])

    def test_a_dirty_checkout_is_compared_not_refused(self) -> None:
        (self.repo / "tracked.txt").write_text("in progress\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("new\n", encoding="utf-8")

        self.assertEqual("enforced",
                         self.dispatch("codex", "none").artifacts["read_only_mode"])
        # A second edit to an already-modified file leaves the status unchanged.
        self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION,
                         self.dispatch("codex", "child").outcome)

    def test_an_unreadable_checkout_fails_closed_before_the_stage(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            result = self.dispatch("codex", "none", cwd=plain)

        self.assertEqual(ex.DispatchOutcome.BLOCKED, result.outcome)
        self.assertEqual("read_only_verification", result.missing_capability)
        self.assertFalse(self.argv_file.exists())

    def test_an_unverifiable_checkout_after_the_stage_is_a_violation(self) -> None:
        real = ex._checkout_fingerprint
        calls = []

        def fingerprint(path):
            calls.append(path)
            if len(calls) > 1:
                raise ex.ExecutorError("git is gone")
            return real(path)

        with patch.object(ex, "_checkout_fingerprint", side_effect=fingerprint):
            result = self.dispatch("codex", "none")

        self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, result.outcome)
        self.assertIn("could not verify", result.detail)
        self.assertNotIn("read_only_mode", result.artifacts)

    def test_a_launched_stage_is_pending_until_its_completion_is_checked(self) -> None:
        """Orca returns once its worker is alive, so a write after the receipt
        is still the review's write: the launch alone verifies nothing."""
        result = self.dispatch("orca", "none")

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome)
        self.assertTrue(result.asynchronous)
        self.assertEqual("pending", result.artifacts["read_only_verification"])
        self.assertNotIn("workspace_fingerprint_after", result.artifacts)
        self.assertNotIn("read_only_mode", result.artifacts)

        # The worker writes after the receipt, and whoever sees it finish checks.
        (self.repo / "tracked.txt").write_text("written later\n", encoding="utf-8")
        finished = ex.verify_read_only_completion(result, str(self.repo))

        self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, finished.outcome)
        self.assertNotIn("read_only_verification", finished.artifacts)
        self.assertIn("read-only contract violation", finished.detail)

    def test_a_launched_stage_that_changed_nothing_verifies_on_completion(self) -> None:
        result = self.dispatch("orca", "none")

        finished = ex.verify_read_only_completion(result, str(self.repo))

        self.assertEqual(ex.DispatchOutcome.SUCCEEDED, finished.outcome)
        self.assertEqual("verified", finished.artifacts["read_only_verification"])
        self.assertEqual(finished.artifacts["workspace_fingerprint_before"],
                         finished.artifacts["workspace_fingerprint_after"])

    def test_a_timed_out_stage_is_pending_not_verified(self) -> None:
        """A timeout says the worker may still be alive; an unchanged tree at
        that moment is no evidence about what it writes next."""
        def timed_out(argv, timeout=None, cwd=None):
            raise subprocess.TimeoutExpired(argv, timeout)

        for executor, target, adapter, extra in (
            ("orca", self.ORCA, ex.OrcaAdapter(), {"context": orca_context(
                review_workspace=ex.OrcaReviewWorkspace(
                    path=tempfile.gettempdir() + "/cycle-review-elsewhere",
                    implementer_path=str(self.repo), isolation="disposable"))}),
            ("codex", self.CODEX, ex.CodexAdapter(), {}),
        ):
            with self.subTest(executor=executor):
                git(self.repo, "checkout", "--", "tracked.txt")
                result = ex.dispatch(
                    router.RoutingDecision("reviewer", target, router.RoutingMode.PRODUCTION),
                    "review it", ex.Registry([adapter]), writes=False,
                    cwd=str(self.repo), runner=timed_out,
                    probes={executor: ex.ProbeResult(executor, ex.Availability.READY, "t")},
                    **extra,
                )

                self.assertEqual(ex.DispatchOutcome.FAILED, result.outcome)
                self.assertEqual("pending", result.artifacts["read_only_verification"])
                self.assertNotIn("workspace_fingerprint_after", result.artifacts)
                self.assertNotIn("read_only_mode", result.artifacts)

                # The worker that outlived its timeout writes afterwards.
                (self.repo / "tracked.txt").write_text("late\n", encoding="utf-8")
                finished = ex.verify_read_only_completion(result, str(self.repo))

                self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, finished.outcome)

    def test_a_failed_stage_that_wrote_is_still_a_violation(self) -> None:
        def failed_after_writing(argv, timeout=None, cwd=None):
            (self.repo / "tracked.txt").write_text("partial\n", encoding="utf-8")
            return completed(returncode=1, stderr="boom")

        result = ex.dispatch(
            router.RoutingDecision("reviewer", self.CODEX, router.RoutingMode.PRODUCTION),
            "review it", ex.Registry([ex.CodexAdapter()]), writes=False,
            cwd=str(self.repo), runner=failed_after_writing,
            probes={"codex": ex.ProbeResult("codex", ex.Availability.READY, "t")},
        )

        self.assertEqual(ex.DispatchOutcome.CONTRACT_VIOLATION, result.outcome)
        self.assertNotIn("read_only_verification", result.artifacts)

    def test_only_a_pending_result_is_checked_again(self) -> None:
        result = self.dispatch("codex", "none")
        (self.repo / "tracked.txt").write_text("after the stage\n", encoding="utf-8")

        self.assertIs(result, ex.verify_read_only_completion(result, str(self.repo)))

    def test_the_check_knows_no_executor_tool_or_product(self) -> None:
        import ast
        import inspect
        import textwrap

        functions = (ex._verified_read_only, ex.verify_read_only_completion,
                     ex._compare_read_only, ex._read_only_violation,
                     ex._checkout_fingerprint)
        self.assertEqual(["cwd", "call"],
                         list(inspect.signature(ex._verified_read_only).parameters))
        self.assertEqual(["path"],
                         list(inspect.signature(ex._checkout_fingerprint).parameters))
        for function in functions:
            with self.subTest(function=function.__name__):
                self.assertFalse({"executor", "adapter", "target", "tool"}
                                 & set(inspect.signature(function).parameters))
                # The docstrings explain what is out of scope by name, so only
                # the code is searched; they are removed from the tree rather
                # than from the text, whose indentation varies by Python.
                tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
                for node in ast.walk(tree):
                    body = getattr(node, "body", None)
                    if (isinstance(body, list) and body
                            and isinstance(body[0], ast.Expr)
                            and isinstance(body[0].value, ast.Constant)
                            and isinstance(body[0].value.value, str)):
                        body.pop(0)
                code = ast.unparse(tree).lower()
                for name in ("codex", "claude", "orca", "serena", ".name", "executor"):
                    self.assertNotIn(name, code)


class UsageNormalizationTests(unittest.TestCase):
    def test_codex_dispatch_preserves_reported_cache_and_reasoning_categories(self) -> None:
        stdout = json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 1200, "cached_input_tokens": 300,
            "cache_write_input_tokens": 25, "output_tokens": 80,
            "reasoning_output_tokens": 15,
        }})
        result = ex.CodexAdapter().dispatch(
            TARGET, "work", runner=lambda *_args, **_kwargs: completed(stdout),
            dispatch_attempt_id="attempt-physical-119",
        )

        self.assertEqual("completed", result.lifecycle_state)
        self.assertEqual({"input_total", "cached_input", "cache_write_input",
                          "output_total", "reasoning_output"},
                         {row["category"] for row in result.usage_observations})
        self.assertTrue(all(row["observation_id"].startswith("usage-")
                            for row in result.usage_observations))
        self.assertEqual((), result.cost_measures)

    def test_claude_model_usage_is_partial_and_reported_cost_stays_unknown(self) -> None:
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")
        stdout = json.dumps({"type": "result", "modelUsage": {
            "claude-sonnet-5": {
                "inputTokens": 40, "cacheReadInputTokens": 60,
                "cacheCreationInputTokens": 5, "outputTokens": 25,
                "costUSD": "0.0032",
            },
        }})
        result = ex.ClaudeAdapter().dispatch(
            target, "work", runner=lambda *_args, **_kwargs: completed(stdout),
            dispatch_attempt_id="attempt-physical-120",
        )

        self.assertEqual({"input", "cache_read_input", "cache_creation_input", "output"},
                         {row["category"] for row in result.usage_observations})
        self.assertEqual("unknown", result.cost_measures[0]["basis"])
        self.assertIsNone(result.cost_measures[0]["amount"])
        self.assertEqual("0.0032", result.cost_measures[0]["reported_amount"])

    def test_missing_or_invalid_categories_are_not_filled_with_zero(self) -> None:
        stdout = json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 17, "cached_input_tokens": "not-a-count",
        }})
        observations, costs = ex.normalize_executor_output("codex", stdout, "attempt-partial")

        self.assertEqual([("input_total", 17)],
                         [(row["category"], row["amount"]) for row in observations])
        self.assertEqual((), costs)

    def test_out_of_range_reported_cost_is_omitted(self) -> None:
        def normalize_cost(raw_cost: str, attempt_id: str):
            stdout = json.dumps({"type": "result", "modelUsage": {
                "claude-sonnet-5": {"inputTokens": 17, "costUSD": raw_cost},
            }})
            return ex.normalize_executor_output("claude", stdout, attempt_id)

        usage_rows, cost_rows = normalize_cost(
            "1000000000000000001", "attempt-cost-over-limit",
        )
        self.assertEqual([("input", 17)],
                         [(row["category"], row["amount"]) for row in usage_rows])
        self.assertEqual((), cost_rows)

        _, cost_rows = normalize_cost(
            "1000000000000000000", "attempt-cost-at-limit",
        )
        self.assertEqual("1000000000000000000", cost_rows[0]["reported_amount"])

        _, cost_rows = normalize_cost(
            "1e-1000000000", "attempt-cost-tiny-exponent",
        )
        self.assertEqual((), cost_rows)

        _, cost_rows = normalize_cost(
            "0." + "0" * 127 + "1", "attempt-cost-long-decimal",
        )
        self.assertEqual((), cost_rows)

        usage_rows, cost_rows = normalize_cost("1e-128", "attempt-cost-exponent-at-limit")
        self.assertEqual([("input", 17)],
                         [(row["category"], row["amount"]) for row in usage_rows])
        self.assertEqual((), cost_rows)


if __name__ == "__main__":
    unittest.main()
