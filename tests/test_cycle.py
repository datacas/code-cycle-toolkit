from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cycle as cy  # noqa: E402
import executors as ex  # noqa: E402
import router  # noqa: E402
import telemetry as tm  # noqa: E402


class ScriptedAdapter(ex.Adapter):
    """Answers with whatever the test says, and remembers being asked."""

    provable_ceiling = ex.Availability.AUTHENTICATED
    enforces_read_only = True

    def __init__(self, name, availability=ex.Availability.AUTHENTICATED, outcomes=None):
        self.name = name
        self._availability = availability
        self._outcomes = list(outcomes or [])
        self.dispatched = []
        self.probes = 0

    def probe(self):
        self.probes += 1
        return ex.ProbeResult(self.name, self._availability, "scripted",
                              provable_ceiling=self.provable_ceiling)

    def dispatch(self, target, task, **kw):
        self.dispatched.append(task)
        if self._outcomes:
            outcome, capability = self._outcomes.pop(0)
        else:
            outcome, capability = ex.DispatchOutcome.SUCCEEDED, None
        return ex.DispatchResult(
            outcome, self.name, target,
            model_resolved=target.model if outcome is ex.DispatchOutcome.SUCCEEDED else None,
            missing_capability=capability,
            readiness_policy=ex.ReadinessPolicy.ATTEMPT,
            dispatched_from=self._availability,
        )


class CycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = tm.Telemetry(Path(temporary.name) / "t.sqlite")

    def recorder(self, adapters, availability=None, **kw):
        registry = ex.Registry(adapters)
        availability = availability or {a.name: ex.Availability.READY for a in adapters}
        return cy.CycleRecorder(
            self.store, "owner/repo", "API-055",
            router.TaskSignals(difficulty=2, verifiability="auto"),
            availability=availability, registry=registry, **kw,
        )


class FullCycleTests(CycleTestCase):
    """implement -> review -> resolve -> rereview, every step persisted."""

    def test_a_whole_cycle_leaves_a_row_for_every_stage(self) -> None:
        codex = ScriptedAdapter("codex")
        claude = ScriptedAdapter("claude")
        recorder = self.recorder([codex, claude])

        recorder.stage("implement", "implement the issue")
        recorder.stage("review", "review the change")
        recorder.record_verdict("review", "CHANGES_REQUESTED",
                                findings_total=3, findings_blocking=1)
        recorder.next_iteration()
        recorder.stage("resolve", "resolve the findings")
        recorder.stage("rereview", "rereview the change")
        recorder.record_verdict("rereview", "APPROVED", findings_total=0)
        recorder.close("READY_FOR_MANUAL_MERGE")

        rows = self.store.rows("owner/repo")
        roles = [row["role"] for row in rows]

        self.assertEqual(
            ["implement", "review", "review", "resolve", "rereview", "rereview", "coordinate"],
            roles,
        )
        self.assertEqual(7, len(rows))

    def test_the_routing_decision_is_on_every_dispatch_row(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])

        recorder.stage("implement", "work")

        row = self.store.rows("owner/repo")[0]
        self.assertEqual("cheap_coder", row["profile"])
        self.assertEqual("codex", row["executor"])
        self.assertEqual("gpt-5.6-luna", row["model_requested"])
        self.assertEqual("gpt-5.6-luna", row["model_resolved"])
        self.assertEqual("attempt", row["readiness_policy"])
        self.assertEqual("authenticated", row["dispatched_from"])
        self.assertEqual("succeeded", row["outcome"])

    def test_the_task_signals_are_recorded_with_the_dispatch(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])

        recorder.stage("implement", "work")

        row = self.store.rows("owner/repo")[0]
        self.assertEqual(2, row["difficulty"])
        self.assertEqual("auto", row["verifiability"])

    def test_iterations_are_carried_onto_the_rows(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])
        recorder.stage("implement", "work")
        recorder.next_iteration()
        recorder.stage("resolve", "fix")

        rows = self.store.rows("owner/repo")

        self.assertEqual([0, 1], [row["iteration"] for row in rows])

    def test_the_close_row_carries_the_round_count(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])
        recorder.next_iteration()
        recorder.next_iteration()

        recorder.close("READY_FOR_MANUAL_MERGE")

        row = self.store.rows("owner/repo")[-1]
        self.assertEqual("coordinate", row["role"])
        self.assertEqual(2, row["payload"]["iterations"])

    def test_the_cycle_feeds_the_first_pass_rate(self) -> None:
        """What all of this is for."""
        for index in range(10):
            recorder = cy.CycleRecorder(
                self.store, "owner/repo", f"task-{index}",
                router.TaskSignals(),
                availability={"codex": ex.Availability.READY, "claude": ex.Availability.READY},
                registry=ex.Registry([ScriptedAdapter("codex"), ScriptedAdapter("claude")]),
            )
            recorder.stage("implement", "work")
            recorder.stage("review", "review")
            recorder.record_verdict("review", "APPROVED" if index < 6 else "CHANGES_REQUESTED")

        rate = self.store.first_pass_rate("owner/repo")

        self.assertTrue(rate.known)
        self.assertEqual(0.6, rate.value)
        self.assertEqual(10, rate.observations)


class RerouteRecordingTests(CycleTestCase):
    """A fallback whose first attempt left no trace makes fallbacks look free."""

    def quota_then_fallback(self):
        codex = ScriptedAdapter(
            "codex", outcomes=[(ex.DispatchOutcome.BLOCKED, "operating_quota")]
        )
        claude = ScriptedAdapter("claude")
        return codex, claude, self.recorder([codex, claude])

    def test_both_decisions_are_recorded_not_only_the_final_one(self) -> None:
        codex, claude, recorder = self.quota_then_fallback()

        outcome = recorder.stage("implement", "work")

        rows = self.store.rows("owner/repo")
        self.assertEqual(2, len(rows))
        self.assertEqual(["codex", "claude"], [row["executor"] for row in rows])
        self.assertEqual(["blocked", "succeeded"], [row["outcome"] for row in rows])
        self.assertTrue(outcome.rerouted)

    def test_the_abandoned_attempt_keeps_the_capability_that_stopped_it(self) -> None:
        self.quota_then_fallback()[2].stage("implement", "work")

        first = self.store.rows("owner/repo")[0]

        self.assertEqual("operating_quota", first["missing_capability"])
        self.assertEqual({"operating_quota": 1}, self.store.dispatch_failures("owner/repo"))

    def test_the_second_row_says_it_was_a_fallback(self) -> None:
        self.quota_then_fallback()[2].stage("implement", "work")

        second = self.store.rows("owner/repo")[1]

        self.assertEqual(1, second["used_fallback"])

    def test_the_work_actually_reached_the_fallback_executor(self) -> None:
        codex, claude, recorder = self.quota_then_fallback()

        recorder.stage("implement", "the real task")

        self.assertEqual(["the real task"], codex.dispatched)
        self.assertEqual(["the real task"], claude.dispatched)

    def test_it_reroutes_at_most_once(self) -> None:
        codex = ScriptedAdapter("codex", outcomes=[(ex.DispatchOutcome.BLOCKED, "operating_quota")])
        claude = ScriptedAdapter("claude", outcomes=[(ex.DispatchOutcome.BLOCKED, "operating_quota")])
        recorder = self.recorder([codex, claude])

        outcome = recorder.stage("implement", "work")

        self.assertEqual(2, len(self.store.rows("owner/repo")))
        self.assertFalse(outcome.succeeded)

    def test_friction_a_person_must_clear_does_not_trigger_a_reroute(self) -> None:
        """A trust dialog teaches nothing worth acting on; retrying elsewhere
        would hide a question somebody has to answer."""
        codex = ScriptedAdapter("codex", outcomes=[(ex.DispatchOutcome.BLOCKED, "folder_trust")])
        claude = ScriptedAdapter("claude")
        recorder = self.recorder([codex, claude])

        outcome = recorder.stage("implement", "work")

        self.assertEqual(1, len(self.store.rows("owner/repo")))
        self.assertFalse(outcome.rerouted)
        self.assertEqual([], claude.dispatched)

    def test_a_login_prompt_is_evidence_but_not_permission_to_go_around_it(self) -> None:
        """`learned_availability` is non-null for a sign-in screen too, so
        "we learned something" is not the test for whether to reroute."""
        codex = ScriptedAdapter(
            "codex", outcomes=[(ex.DispatchOutcome.BLOCKED, "authenticated_session")]
        )
        claude = ScriptedAdapter("claude")
        recorder = self.recorder([codex, claude])

        outcome = recorder.stage("implement", "work")

        self.assertIsNotNone(outcome.result.learned_availability)
        self.assertTrue(outcome.result.needs_human_action)
        self.assertFalse(outcome.rerouted)
        self.assertEqual([], claude.dispatched)

    def test_the_question_stays_where_a_person_can_see_it(self) -> None:
        """Downgrading availability would reroute every later stage instead,
        which is the same silent switch one stage further on."""
        codex = ScriptedAdapter(
            "codex", outcomes=[(ex.DispatchOutcome.BLOCKED, "authenticated_session")]
        )
        recorder = self.recorder([codex, ScriptedAdapter("claude")])

        recorder.stage("implement", "work")

        self.assertEqual(ex.Availability.READY, recorder.availability["codex"])

    def test_a_calibration_never_reroutes(self) -> None:
        codex = ScriptedAdapter("codex", ex.Availability.READY,
                                [(ex.DispatchOutcome.BLOCKED, "operating_quota")])
        claude = ScriptedAdapter("claude", ex.Availability.READY)
        recorder = self.recorder([codex, claude], mode=router.RoutingMode.CALIBRATION)

        outcome = recorder.stage("implement", "work")

        self.assertEqual(1, len(self.store.rows("owner/repo")))
        self.assertFalse(outcome.rerouted)
        self.assertEqual([], claude.dispatched)


class ProbeTests(CycleTestCase):
    """One probe map for the whole cycle, or the decisions stop being comparable."""

    def test_the_probes_it_was_given_are_the_ones_every_stage_uses(self) -> None:
        codex, claude = ScriptedAdapter("codex"), ScriptedAdapter("claude")
        registry = ex.Registry([codex, claude])
        probes = registry.probe_all()
        before = (codex.probes, claude.probes)

        recorder = cy.CycleRecorder(
            self.store, "owner/repo", "API-055", router.TaskSignals(),
            availability=registry.availability(ex.ReadinessPolicy.ATTEMPT, probes),
            registry=registry, probes=probes,
        )
        recorder.stage("implement", "work")
        recorder.stage("review", "review")

        self.assertEqual(before, (codex.probes, claude.probes))

    def test_without_them_a_dispatch_has_to_find_out_for_itself(self) -> None:
        """Stated so the difference is visible: this is the behaviour the
        orchestration contract tells a run not to rely on."""
        codex, claude = ScriptedAdapter("codex"), ScriptedAdapter("claude")
        recorder = self.recorder([codex, claude])

        recorder.stage("implement", "work")

        self.assertGreater(codex.probes, 0)


class ProfileTests(CycleTestCase):
    """Whoever loaded the configuration decides; the recorder only carries it."""

    def test_the_profiles_it_was_given_are_the_ones_it_routes_with(self) -> None:
        profiles = router.load_profiles({"code_cycle": {"profiles": {
            "cheap_coder": {"primary": "claude:anthropic/claude-opus-5 high"}}}})
        codex, claude = ScriptedAdapter("codex"), ScriptedAdapter("claude")
        recorder = self.recorder([codex, claude], profiles=profiles)

        outcome = recorder.stage("implement", "work")

        self.assertEqual("claude", outcome.decision.target.executor)
        self.assertEqual("claude-opus-5", outcome.decision.target.model)
        self.assertEqual([], codex.dispatched)

    def test_without_them_the_built_in_defaults_apply(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])

        outcome = recorder.stage("implement", "work")

        self.assertEqual("codex", outcome.decision.target.executor)

    def test_it_does_not_go_looking_for_a_configuration_file(self) -> None:
        """A component that reads configuration on its own can disagree with
        the caller about what the configuration says."""
        import inspect

        source = inspect.getsource(cy)

        self.assertNotIn("code-cycle.yml", source)
        self.assertNotIn("load_profiles", source)


class BlockedRoutingTests(CycleTestCase):
    def test_a_routing_that_never_reached_an_executor_still_leaves_a_row(self) -> None:
        """A run that stopped here would otherwise leave no trace of why."""
        recorder = self.recorder(
            [ScriptedAdapter("codex"), ScriptedAdapter("claude")],
            availability={"codex": ex.Availability.INSTALLED,
                          "claude": ex.Availability.INSTALLED},
        )

        outcome = recorder.stage("implement", "work")

        rows = self.store.rows("owner/repo")
        self.assertEqual(1, len(rows))
        self.assertEqual("blocked", rows[0]["outcome"])
        self.assertIsNone(rows[0]["executor"])
        self.assertTrue(outcome.decision.blocked)


class SeparationTests(unittest.TestCase):
    """Router decides, executors run, telemetry observes."""

    def test_the_router_does_not_know_the_store_exists(self) -> None:
        source = (ROOT / "scripts" / "router.py").read_text(encoding="utf-8")

        self.assertNotIn("telemetry", source)
        self.assertNotIn("sqlite", source.lower())

    def test_the_adapters_do_not_know_the_store_exists(self) -> None:
        source = (ROOT / "scripts" / "executors.py").read_text(encoding="utf-8")

        self.assertNotIn("telemetry", source)
        self.assertNotIn("sqlite", source.lower())

    def test_recording_is_not_a_step_the_caller_performs(self) -> None:
        """There is no way through stage() that dispatches without recording."""
        import inspect

        source = inspect.getsource(cy.CycleRecorder.stage)

        self.assertIn("self._record", source)
        self.assertNotIn("return result", source)


class VerdictTests(CycleTestCase):
    def test_a_review_verdict_needs_a_status(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])

        with self.assertRaises(cy.CycleError):
            recorder.record_verdict("review", "")

    def test_a_verdict_row_carries_the_findings(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])

        recorder.record_verdict("review", "CHANGES_REQUESTED",
                                findings_total=4, findings_blocking=2,
                                security_audit_ran=True, security_gate_half="deterministic")

        row = self.store.rows("owner/repo")[0]
        self.assertEqual(4, row["findings_total"])
        self.assertEqual(2, row["findings_blocking"])
        self.assertIs(True, row["payload"]["security_audit_ran"])
        self.assertEqual("deterministic", row["payload"]["security_gate_half"])


if __name__ == "__main__":
    unittest.main()
