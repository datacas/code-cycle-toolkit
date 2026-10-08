from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
    enforces_workspace_boundary = True

    def __init__(self, name, availability=ex.Availability.AUTHENTICATED, outcomes=None):
        self.name = name
        self._availability = availability
        self._outcomes = list(outcomes or [])
        self.dispatched = []
        self.dispatch_kwargs = []
        self.probes = 0

    def probe(self):
        self.probes += 1
        return ex.ProbeResult(self.name, self._availability, "scripted",
                              provable_ceiling=self.provable_ceiling)

    def dispatch(self, target, task, **kw):
        self.dispatched.append(task)
        self.dispatch_kwargs.append(kw)
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
            start_state="not_started" if capability else "started",
            process_exited=True,
        )

    def publication_access(self, probe, *, writes):
        return True, "test publication permission"


class CycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        snapshot = patch("cycle.effect_snapshot", return_value=None)
        snapshot.start()
        self.addCleanup(snapshot.stop)
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

    def test_failed_codex_git_metadata_probe_routes_before_implementation_dispatch(self) -> None:
        class ProbeFailingCodex(ScriptedAdapter):
            def probe_git_metadata_write(self, target, cwd, writable_dirs):
                self.probe_arguments = (target, cwd, writable_dirs)
                return False, "worktree gitdir is read-only"

        codex = ProbeFailingCodex("codex")
        claude = ScriptedAdapter("claude")
        recorder = self.recorder([codex, claude])

        with tempfile.TemporaryDirectory() as temporary:
            roots = (str(Path(temporary) / ".git"),
                     str(Path(temporary) / ".git" / "worktrees" / "task-203"))
            outcome = recorder.stage(
                "implement", "implement issue 203", cwd=temporary,
                writable_dirs=roots,
            )

        self.assertEqual([], codex.dispatched)
        self.assertEqual(1, len(claude.dispatched))
        self.assertEqual("claude", outcome.decision.target.executor)
        self.assertIn("routed to claude before dispatch", outcome.decision.reasons[-1])
        self.assertIn("worktree gitdir is read-only", cy.routing_context(outcome.decision))
        self.assertEqual(roots, codex.probe_arguments[2])

    def test_failed_probe_does_not_route_to_an_executor_without_publish_access(self) -> None:
        class ProbeFailingCodex(ScriptedAdapter):
            def probe_git_metadata_write(self, target, cwd, writable_dirs):
                return False, "worktree gitdir is read-only"

        class NonPublisher(ScriptedAdapter):
            def publication_access(self, probe, *, writes):
                return False, "publication is unavailable"

        codex = ProbeFailingCodex("codex")
        non_publisher = NonPublisher("claude")
        recorder = self.recorder([codex, non_publisher])

        with tempfile.TemporaryDirectory() as temporary:
            outcome = recorder.stage(
                "implement", "implement issue 203", cwd=temporary,
                writable_dirs=(str(Path(temporary) / ".git"),),
            )

        self.assertTrue(outcome.decision.blocked)
        self.assertEqual([], codex.dispatched)
        self.assertEqual([], non_publisher.dispatched)
        self.assertIn("no available executor", outcome.decision.reasons[-1])

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

        outcome = recorder.stage("implement", "work")

        row = self.store.rows("owner/repo")[0]
        self.assertEqual("cheap_coder", row["profile"])
        self.assertEqual("codex", row["executor"])
        self.assertEqual("gpt-6-luna", row["model_requested"])
        self.assertEqual("gpt-6-luna", row["model_resolved"])
        self.assertEqual("attempt", row["readiness_policy"])
        self.assertEqual("authenticated", row["dispatched_from"])
        self.assertEqual("succeeded", row["outcome"])
        self.assertEqual("available", row["payload"]["routing_cost_status"])
        expected = tm.routing_decision_fields(outcome.decision)
        actual = {key: row[key] if key in row else row["payload"].get(key)
                  for key in expected}
        self.assertEqual(expected, actual)

    def test_cost_estimate_failure_is_recorded_without_stopping_dispatch(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])

        with patch.object(router, "estimate_cost", side_effect=router.RouterError("bad rate")):
            outcome = recorder.stage("implement", "work")

        row = self.store.rows("owner/repo")[0]
        self.assertFalse(outcome.decision.blocked)
        self.assertIsNone(outcome.decision.cost)
        self.assertEqual("unavailable", row["payload"]["routing_cost_status"])
        self.assertIsNone(row["payload"]["routing_cost_total"])

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


class MeasuredCostTests(CycleTestCase):
    def seed_first_passes(self, count=10, passed=6):
        for index in range(count):
            task = f"history-{index}"
            self.store.record_stage(
                "owner/repo", task, "implement", profile="cheap_coder",
            )
            self.store.record_stage(
                "owner/repo", task, "review",
                status="APPROVED" if index < passed else "CHANGES_REQUESTED",
            )

    def make_recorder(self, strategy):
        return self.recorder(
            [ScriptedAdapter("codex"), ScriptedAdapter("claude")],
            routing_strategy=strategy,
        )

    def test_measured_rate_at_the_minimum_drives_and_explains_the_estimate(self) -> None:
        self.seed_first_passes()

        outcome = self.make_recorder(router.RoutingStrategy.MEASURED).stage(
            "implement", "work",
        )

        decision = outcome.decision
        self.assertEqual(0.6, decision.rate_value)
        self.assertIs(True, decision.rate_known)
        self.assertEqual(10, decision.rate_observations)
        self.assertEqual(tm.MINIMUM_SAMPLE, decision.rate_minimum)
        self.assertEqual("0.60 from 10 observations", decision.rate_explanation)
        self.assertEqual(0.6, decision.rate_used)
        self.assertEqual(1.6, decision.cost.expected_resolutions)
        self.assertIn(decision.rate_explanation, decision.explain())

        row = self.store.rows("owner/repo")[-1]
        self.assertEqual(5.6, row["payload"]["routing_cost_total"])
        self.assertEqual("measured", row["payload"]["routing_rate_source"])
        self.assertIs(True, row["payload"]["routing_rate_known"])
        self.assertEqual(10, row["payload"]["routing_rate_observations"])
        self.assertEqual(0.6, row["payload"]["routing_rate_value"])
        self.assertEqual(0.6, row["payload"]["routing_rate_used"])

    def test_unknown_measured_rate_uses_the_default_without_fabricating_a_value(self) -> None:
        self.seed_first_passes(count=3, passed=2)

        outcome = self.make_recorder(router.RoutingStrategy.MEASURED).stage(
            "implement", "work",
        )

        decision = outcome.decision
        self.assertIsNone(decision.rate_value)
        self.assertEqual(3, decision.rate_observations)
        self.assertIs(False, decision.rate_known)
        self.assertEqual(0.0, decision.rate_used)
        self.assertEqual(
            "unknown: 3 observation(s), fewer than the 10 required",
            decision.rate_explanation,
        )
        self.assertIn("unknown", decision.explain())

        row = self.store.rows("owner/repo")[-1]
        self.assertEqual("conservative_default", row["payload"]["routing_rate_source"])
        self.assertIsNone(row["payload"]["routing_rate_value"])
        self.assertEqual(0.0, row["payload"]["routing_rate_used"])
        self.assertEqual(3, row["payload"]["routing_rate_observations"])

    def test_fixed_strategy_never_reads_the_telemetry_rate(self) -> None:
        with patch.object(
            self.store, "first_pass_rate",
            side_effect=AssertionError("fixed routing queried telemetry"),
        ) as read_rate:
            outcome = self.make_recorder(router.RoutingStrategy.FIXED).stage(
                "implement", "work",
            )

        read_rate.assert_not_called()
        self.assertEqual("default", self.store.rows("owner/repo")[-1]
                         ["payload"]["routing_rate_source"])
        self.assertEqual(0.0, outcome.decision.rate_used)

    def test_strategy_does_not_change_the_selected_target(self) -> None:
        self.seed_first_passes()
        fixed = self.make_recorder(router.RoutingStrategy.FIXED).stage("implement", "work")
        measured = self.make_recorder(router.RoutingStrategy.MEASURED).stage("implement", "work")

        self.assertEqual(fixed.decision.target, measured.decision.target)
        self.assertEqual(fixed.decision.profile, measured.decision.profile)
        self.assertIsNotNone(measured.decision.rate_value)

    def test_switching_back_to_fixed_keeps_existing_telemetry(self) -> None:
        self.seed_first_passes()
        measured = self.make_recorder(router.RoutingStrategy.MEASURED)
        measured.stage("implement", "work")
        before = self.store.rows("owner/repo")

        with patch.object(
            self.store, "first_pass_rate",
            side_effect=AssertionError("fixed routing queried telemetry"),
        ):
            self.make_recorder(router.RoutingStrategy.FIXED).stage("implement", "work")

        after = self.store.rows("owner/repo")
        self.assertEqual(before, after[:len(before)])
        self.assertGreater(len(after), len(before))


class RoleWorkspacePolicyTests(CycleTestCase):
    def test_only_change_and_review_roles_receive_publication_access(self) -> None:
        expected = {
            "implement": True, "resolve": True, "review": True, "rereview": True,
            "security": False, "bootstrap": False, "verify": False, "run": False,
        }

        for role, publishes in expected.items():
            with self.subTest(role=role):
                self.assertEqual(publishes, cy.role_contract(role).publishes)

    def test_local_only_implementation_does_not_receive_publication_access(self) -> None:
        adapter = ScriptedAdapter("codex")
        recorder = self.recorder([adapter], local_only=True)

        recorder.stage("implement", "implement locally")

        self.assertFalse(adapter.dispatch_kwargs[0]["publishes"])
        self.assertEqual((), adapter.dispatch_kwargs[0]["publication_permissions"])

    def test_publication_permissions_follow_each_role(self) -> None:
        expected = {
            "implement": ("comment", "create_pr", "push_branch"),
            "resolve": ("comment", "push_branch"),
            "review": ("comment",),
            "rereview": ("comment",),
            "security": (), "bootstrap": (), "verify": (), "run": (),
        }

        for role, permissions in expected.items():
            with self.subTest(role=role):
                self.assertEqual(permissions, cy.role_contract(role).publication_permissions)

    def test_each_stage_is_told_its_publication_policy(self) -> None:
        adapter = ScriptedAdapter("codex")
        recorder = self.recorder([adapter])

        recorder.stage("review", "review it")
        recorder.stage("resolve", "resolve it")
        recorder.stage("rereview", "rereview it")
        recorder.stage("implement", "implement it")

        review, resolve, _, implement = adapter.dispatched
        self.assertIn("you may comment on the work item", review)
        self.assertIn("You may not create the change request", review)
        self.assertIn("push the working branch", review.split("You may not")[1])
        self.assertIn("You may not create the change request", resolve)
        self.assertIn("you may comment", resolve)
        self.assertNotIn("You may not", implement)
        for task in adapter.dispatched:
            self.assertIn("Never merge, force-push, delete remote refs", task)
            self.assertIn("For a GitHub code host, when `gh` is authenticated", task)
            self.assertIn("retry once with `gh`", task)
            self.assertIn("For Bitbucket, use its configured tooling", task)

    def test_a_routed_review_is_told_the_values_its_run_line_must_carry(self) -> None:
        adapter = ScriptedAdapter("codex")
        recorder = self.recorder([adapter])

        recorder.stage("review", "review it")

        prompt = adapter.dispatched[0]
        row = self.store.rows("owner/repo")[0]
        self.assertEqual(("reviewer", "openai", "gpt-6-sol", "high"),
                         (row["profile"], row["provider"], row["model_requested"], row["effort"]))
        self.assertIn("profile `reviewer`", prompt)
        self.assertIn("requested model `openai/gpt-6-sol`", prompt)
        self.assertIn("effort `high`", prompt)
        self.assertIn("resolved model as `?`", prompt)

    def test_a_local_only_stage_is_told_it_publishes_nothing(self) -> None:
        adapter = ScriptedAdapter("codex")
        recorder = self.recorder([adapter], local_only=True)

        recorder.stage("implement", "implement locally")

        self.assertIn("it publishes nothing", adapter.dispatched[0])

    def test_missing_publication_access_is_recorded_without_running_the_adapter(self) -> None:
        class DeniedPublicationAdapter(ScriptedAdapter):
            def publication_access(self, probe, *, writes):
                return False, "permission is not configured"

        adapter = DeniedPublicationAdapter("codex")
        recorder = self.recorder([adapter])

        outcome = recorder.stage("implement", "implement and publish")

        self.assertEqual(ex.DispatchOutcome.BLOCKED, outcome.result.outcome)
        self.assertEqual([], adapter.dispatched)
        self.assertEqual([], outcome.attempts)
        self.assertEqual([], self.store.dispatch_attempts("owner/repo"))
        self.assertEqual("publication_access", self.store.rows("owner/repo")[0]["missing_capability"])

    def test_auxiliary_roles_have_explicit_least_privilege_contracts(self) -> None:
        self.assertEqual(
            cy.WorkspacePolicy.READ_ONLY,
            cy.role_contract("security").workspace_policy,
        )
        self.assertEqual(
            cy.WorkspacePolicy.READ_ONLY,
            cy.role_contract("bootstrap").workspace_policy,
        )
        for role in ("verify", "run"):
            with self.subTest(role=role):
                contract = cy.role_contract(role)
                self.assertEqual(cy.WorkspacePolicy.DISPOSABLE, contract.workspace_policy)

    def test_verify_is_blocked_without_a_disposable_workspace(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex")])

        outcome = recorder.stage("verify", "run checks")

        self.assertFalse(outcome.succeeded)
        self.assertIsNone(outcome.result)
        self.assertTrue(any("cannot satisfy the workspace policy" in reason
                            for reason in outcome.decision.reasons))

    def test_security_never_routes_to_an_adapter_without_read_only_enforcement(self) -> None:
        claude = ScriptedAdapter("claude")
        claude.enforces_read_only = False
        claude.enforces_workspace_boundary = False
        recorder = self.recorder([claude])

        outcome = recorder.stage("security", "audit the change")

        self.assertIsNone(outcome.result)
        self.assertTrue(outcome.decision.blocked)
        self.assertTrue(any("cannot satisfy the workspace policy" in reason
                            for reason in outcome.decision.reasons))

    def test_verify_can_use_an_isolated_workspace_for_generated_artifacts(self) -> None:
        source = tempfile.TemporaryDirectory()
        disposable = tempfile.TemporaryDirectory()
        self.addCleanup(source.cleanup)
        self.addCleanup(disposable.cleanup)
        workspace = ex.DisposableWorkspace(disposable.name, source.name)
        recorder = self.recorder([ScriptedAdapter("codex")])

        outcome = recorder.stage(
            "verify", "run checks", cwd=workspace.path, workspace=workspace,
        )

        self.assertTrue(outcome.succeeded)


class AsyncAttemptTests(CycleTestCase):
    def test_launch_receipt_keeps_attempt_launched_and_persists_dispatch_id(self) -> None:
        class ReceiptAdapter(ScriptedAdapter):
            completes_work = False

            def dispatch(self, target, task, **kw):
                attempts = self.store.dispatch_attempts(
                    "owner/repo", cycle_id=self.cycle_id,
                )
                self.attempt_id_seen = attempts[0]["attempt_id"]
                self.lifecycle_seen = attempts[0]["lifecycle_state"]
                return ex.DispatchResult(
                    ex.DispatchOutcome.SUCCEEDED, self.name, target,
                    model_resolved=target.model,
                    artifacts={"dispatchId": "orca-dispatch-119"},
                    asynchronous=True,
                    readiness_policy=ex.ReadinessPolicy.ATTEMPT,
                    dispatched_from=self._availability,
                )

        adapter = ReceiptAdapter("codex")
        adapter.store = self.store
        recorder = self.recorder([adapter])
        adapter.cycle_id = recorder.cycle_id

        outcome = recorder.stage("implement", "launch async work")

        attempts = self.store.dispatch_attempts("owner/repo", cycle_id=recorder.cycle_id)
        self.assertTrue(outcome.succeeded)
        self.assertEqual("launched", adapter.lifecycle_seen)
        self.assertEqual("launched", attempts[0]["lifecycle_state"])
        self.assertEqual("orca-dispatch-119", attempts[0]["external_dispatch_id"])
        self.assertEqual(attempts[0]["attempt_id"], adapter.attempt_id_seen)


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
        attempts = self.store.dispatch_attempts("owner/repo", cycle_id=recorder.cycle_id)
        self.assertEqual(2, len(attempts))
        self.assertNotEqual(attempts[0]["attempt_id"], attempts[1]["attempt_id"])
        self.assertEqual(attempts[0]["attempt_id"], attempts[1]["parent_attempt_id"])
        self.assertEqual("fallback", attempts[1]["relationship"])
        self.assertEqual("failed", attempts[0]["lifecycle_state"])
        self.assertEqual("completed", attempts[1]["lifecycle_state"])
        self.assertEqual(
            [attempt["attempt_id"] for attempt in attempts],
            [row["payload"]["dispatch_attempt_id"] for row in rows],
        )

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

        self.assertEqual(1, len(codex.dispatched))
        self.assertEqual(1, len(claude.dispatched))
        strip = lambda task: task.split("\n\nRouting for this stage")[0]  # noqa: E731
        self.assertEqual(strip(codex.dispatched[0]), strip(claude.dispatched[0]))
        self.assertTrue(codex.dispatched[0].startswith("the real task\n\n"))

    def test_the_fallback_attempt_is_told_its_own_target(self) -> None:
        codex, claude, recorder = self.quota_then_fallback()

        recorder.stage("implement", "the real task")

        rows = self.store.rows("owner/repo")
        for adapter, row in zip((codex, claude), rows):
            with self.subTest(executor=row["executor"]):
                self.assertIn(f"profile `{row['profile']}`", adapter.dispatched[0])
                self.assertIn(f"requested model `{row['provider']}/{row['model_requested']}`",
                              adapter.dispatched[0])
                self.assertIn(f"effort `{row['effort']}`", adapter.dispatched[0])

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


class FailedDispatchRerouteTests(CycleTestCase):
    def run_failure(self, role="implement", *, code="capacity", start="not_started",
                    exited=True, asynchronous=False, second_code=None, **kwargs):
        class FailureAdapter(ScriptedAdapter):
            def dispatch(self, target, task, **kw):
                self.dispatched.append(task)
                return ex.DispatchResult(
                    ex.DispatchOutcome.FAILED, self.name, target,
                    error_code=self.code, start_state=start,
                    process_exited=exited, asynchronous=asynchronous,
                )
        primary = FailureAdapter("codex")
        primary.code = code
        fallback = FailureAdapter("claude") if second_code else ScriptedAdapter("claude")
        fallback.code = second_code
        recorder = self.recorder([primary, fallback], **kwargs)
        return recorder, primary, fallback, recorder.stage(role, "work")

    def test_transient_failures_reroute_once_and_record_the_cause(self):
        for code in ("capacity", "transport", "timeout", "quota"):
            with self.subTest(code=code):
                recorder, primary, fallback, outcome = self.run_failure(code=code)
                self.assertTrue(outcome.succeeded)
                self.assertEqual((1, 1), (len(primary.dispatched), len(fallback.dispatched)))
                rows = self.store.rows("owner/repo", "API-055")[-2:]
                for row in rows:
                    self.assertEqual(code, row["payload"]["reroute_error_code"])
                    self.assertEqual("not_started", row["payload"]["reroute_start_state"])
                self.assertEqual(ex.Availability.READY, recorder.availability["codex"])
                recorder.stage("implement", "later stage")
                self.assertEqual(2, len(primary.dispatched))

    def test_no_reroute_without_exit_proof_including_async(self):
        for exited, asynchronous in ((False, False), (True, True)):
            _, _, fallback, outcome = self.run_failure(exited=exited, asynchronous=asynchronous)
            self.assertFalse(outcome.rerouted)
            self.assertFalse(fallback.dispatched)
            self.assertIn("exit_unproven", outcome.result.detail)

    def test_second_failure_stops_even_when_it_is_quota(self):
        _, _, _, outcome = self.run_failure(second_code="quota")
        self.assertTrue(outcome.rerouted)
        self.assertFalse(outcome.succeeded)
        self.assertEqual(2, len(outcome.attempts))
        self.assertIn("retry_exhausted", outcome.result.detail)

    def test_nontransient_errors_never_reroute(self):
        for code in ("auth", "unavailable", "precondition", "executor_error",
                     "unreadable_result", "no_error_report", "contract_violation", "interrupted"):
            _, _, fallback, outcome = self.run_failure(code=code)
            self.assertFalse(outcome.rerouted)
            self.assertFalse(fallback.dispatched)

    def test_calibration_never_reroutes_capacity(self):
        _, _, fallback, outcome = self.run_failure(mode=router.RoutingMode.CALIBRATION)
        self.assertFalse(outcome.rerouted)
        self.assertFalse(fallback.dispatched)

    def test_missing_identical_or_unavailable_fallback_stops(self):
        for kind in ("missing", "identical", "unavailable"):
            profiles = router.load_profiles()
            original = profiles["cheap_coder"]
            profiles["cheap_coder"] = router.Profile(
                original.name, original.primary,
                None if kind == "missing" else original.primary if kind == "identical" else original.fallback,
            )
            availability = {"codex": ex.Availability.READY,
                            "claude": ex.Availability.INSTALLED if kind == "unavailable" else ex.Availability.READY}
            _, _, fallback, outcome = self.run_failure(profiles=profiles, availability=availability)
            self.assertFalse(outcome.rerouted)
            self.assertFalse(fallback.dispatched)

    def test_started_and_unknown_writing_attempts_reconcile(self):
        clean = {"dirty": False, "workspace": {"head": "old"}, "remote": "old",
                 "pulls": [], "comments": []}
        changes = (
            ({}, True, "no_effects"),
            ({"dirty": True}, False, "dirty_workspace"),
            ({"workspace": {"head": "new"}}, False, "workspace_changed"),
            ({"remote": "new"}, False, "remote_changed"),
            ({"comments": ["published"]}, False, "publication_changed"),
            ({"pulls": ["new PR"]}, False, "publication_changed"),
        )
        for start in ("started", "unknown"):
            for change, retry, reason in changes:
                with self.subTest(start=start, change=change):
                    with patch("cycle.effect_snapshot", side_effect=[clean, {**clean, **change}]):
                        _, _, fallback, outcome = self.run_failure(start=start)
                    self.assertEqual(retry, outcome.rerouted)
                    self.assertEqual(retry, bool(fallback.dispatched))
                    first = self.store.rows("owner/repo")[-2 if retry else -1]
                    self.assertEqual(reason, first["payload"]["reroute_reason"])

    def test_review_publication_is_reconciled_despite_read_only_workspace(self):
        clean = {"dirty": False, "workspace": {}, "remote": "old", "comments": []}
        # The default review primary is Claude; make Codex the primary here.
        profiles = router.load_profiles()
        old = profiles["reviewer"]
        profiles["reviewer"] = router.Profile(old.name, profiles["cheap_coder"].primary,
                                               profiles["cheap_coder"].fallback)
        with patch("cycle.effect_snapshot", side_effect=[clean, {**clean, "comments": ["review"]}]):
            _, _, fallback, outcome = self.run_failure("review", start="started", profiles=profiles)
        self.assertFalse(outcome.rerouted)
        self.assertFalse(fallback.dispatched)
        self.assertIn("publication_changed", outcome.result.detail)

    def test_unknown_effect_evidence_stops(self):
        _, _, fallback, outcome = self.run_failure(start="unknown")
        self.assertFalse(outcome.rerouted)
        self.assertFalse(fallback.dispatched)
        self.assertIn("evidence_unavailable", outcome.result.detail)


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
            "cheap_coder": {"primary": "claude:anthropic/claude-opus-5-5 high"}}}})
        codex, claude = ScriptedAdapter("codex"), ScriptedAdapter("claude")
        recorder = self.recorder([codex, claude], profiles=profiles)

        outcome = recorder.stage("implement", "work")

        self.assertEqual("claude", outcome.decision.target.executor)
        self.assertEqual("claude-opus-5-5", outcome.decision.target.model)
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
        self.assertEqual("available", rows[0]["payload"]["routing_cost_status"])
        expected = tm.routing_decision_fields(outcome.decision)
        actual = {key: rows[0][key] if key in rows[0]
                  else rows[0]["payload"].get(key) for key in expected}
        self.assertEqual(expected, actual)


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
