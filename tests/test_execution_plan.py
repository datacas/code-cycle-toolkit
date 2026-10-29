from __future__ import annotations

import importlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cycle  # noqa: E402
import execution_plan as plan  # noqa: E402
import execution_profiles as profiles  # noqa: E402
import run_cycle  # noqa: E402


class StageCatalogTests(unittest.TestCase):
    def test_catalog_snapshot_and_compatibility_adapters(self) -> None:
        self.assertEqual(
            set(plan.STAGES_BY_ID),
            {
                "issue_review", "bootstrap", "implement", "verify", "review",
                "resolve", "rereview", "security", "run", "coordinate",
            },
        )
        self.assertEqual(
            set(plan.CONTROLS_BY_ID), {"ci_gate", "security_gate", "worktree"}
        )
        self.assertEqual(set(cycle.ROLE_CONTRACTS), set(plan.STAGES_BY_ROLE))
        self.assertTrue(all(item.skill or item.function for item in plan.STAGES))
        self.assertEqual(
            run_cycle.SKILL_FOR_ROLE,
            {
                "issue_review": "cc-issue-review",
                "implement": "cc-implement-issue",
                "review": "cc-initial-review",
                "resolve": "cc-resolve-comments",
                "rereview": "cc-rereview",
            },
        )
        self.assertEqual(
            run_cycle.COMPLETES,
            {
                "issue_review": frozenset({"READY"}),
                "implement": frozenset({"IMPLEMENTED"}),
                "resolve": frozenset({"RESOLVED", "PARTIALLY_RESOLVED"}),
                "review": frozenset({"APPROVED", "CHANGES_REQUESTED"}),
                "rereview": frozenset({"APPROVED", "CHANGES_REQUESTED"}),
            },
        )

    def test_control_targets_resolve_to_existing_functions(self) -> None:
        for control in plan.CONTROLS:
            module_name, function_name = control.function.rsplit(".", 1)
            self.assertTrue(callable(getattr(
                importlib.import_module(module_name), function_name
            )), control.function)

    def test_unknown_stages_and_controls_fail_before_dispatch(self) -> None:
        with self.assertRaisesRegex(plan.ContractError, "unknown stage"):
            plan.stage_definition("invented")
        with self.assertRaisesRegex(plan.ContractError, "unknown control"):
            plan.control_definition("invented")

    def test_catalog_duplicate_detection(self) -> None:
        definition = plan.STAGES[0]
        with self.assertRaisesRegex(plan.ContractError, "duplicate stage"):
            plan._index_unique((definition, definition), "stage_id", "stage")


class ExecutionPlanTests(unittest.TestCase):
    def sample(self) -> plan.ExecutionPlan:
        return plan.ExecutionPlan(
            plan_id="run-1",
            revision=1,
            repository="datacas/code-cycle-toolkit",
            work_item="327",
            objective="Define stage and execution-plan contracts",
            execution_profile="cheap_coder",
            effective_configuration={"ci": {"policy": "parallel"}},
            effective_policy={"base_branch": "main"},
            plan_provenance="runtime",
            review={"status": "unreviewed", "provenance": "runtime"},
            stages=(
                plan.PlannedStage(
                    stage_id="implement",
                    obligation=plan.Obligation.REQUIRED,
                    selected=True,
                    state=plan.ExecutionState.PLANNED,
                    condition="work item is ready",
                    reason="requested by the cycle",
                    provenance="runtime policy",
                    executor=plan.ExecutorKind.CLAUDE,
                ),
                plan.PlannedStage(
                    stage_id="rereview",
                    obligation=plan.Obligation.OPTIONAL,
                    selected=False,
                    state=plan.ExecutionState.NOT_SELECTED,
                    condition="implementation changed after review",
                    reason="no preceding review finding yet",
                    provenance="cycle policy",
                ),
            ),
            base_ref="main",
        )

    def test_serialization_round_trip_preserves_plan_reservations(self) -> None:
        sample = self.sample()
        self.assertEqual(plan.ExecutionPlan.from_dict(sample.to_dict()), sample)

    def test_unknown_version_and_stage_are_rejected(self) -> None:
        payload = self.sample().to_dict()
        payload["contract_version"] = plan.CONTRACT_VERSION + 1
        with self.assertRaisesRegex(plan.ContractError, "unsupported execution-plan"):
            plan.ExecutionPlan.from_dict(payload)

        payload = self.sample().to_dict()
        payload["stages"][0]["stage_id"] = "invented"
        with self.assertRaisesRegex(plan.ContractError, "unknown stage"):
            plan.ExecutionPlan.from_dict(payload)

    def test_duplicate_stages_and_omitted_required_stage_are_rejected(self) -> None:
        sample = self.sample()
        duplicate = plan.ExecutionPlan(
            **{**sample.__dict__, "stages": (sample.stages[0], sample.stages[0])}
        )
        with self.assertRaisesRegex(plan.ContractError, "duplicate planned stage"):
            duplicate.validate()

        omitted = plan.ExecutionPlan(
            **{
                **sample.__dict__,
                "stages": (
                    plan.PlannedStage(
                        stage_id="implement",
                        obligation=plan.Obligation.REQUIRED,
                        selected=False,
                        state=plan.ExecutionState.NOT_SELECTED,
                        condition="always",
                        reason="disabled by policy",
                        provenance="runtime",
                    ),
                ),
            }
        )
        with self.assertRaisesRegex(plan.ContractError, "cannot be omitted"):
            omitted.validate()

    def test_unknown_dependency_is_rejected(self) -> None:
        sample = self.sample()
        dependent = plan.PlannedStage(
            **{**sample.stages[0].__dict__, "depends_on": ("made_up",)}
        )
        invalid = plan.ExecutionPlan(
            **{**sample.__dict__, "stages": (dependent,)}
        )
        with self.assertRaisesRegex(plan.ContractError, "unknown stage"):
            invalid.validate()

    def test_invalid_types_are_rejected_as_contract_errors(self) -> None:
        invalid_payloads = (
            ("revision", "1"),
            ("plan_id", 5),
            ("head_sha", []),
        )
        for field, value in invalid_payloads:
            with self.subTest(field=field):
                payload = self.sample().to_dict()
                payload[field] = value
                with self.assertRaises(plan.ContractError):
                    plan.ExecutionPlan.from_dict(payload)

        payload = self.sample().to_dict()
        payload["stages"][0]["selected"] = "no"
        with self.assertRaisesRegex(plan.ContractError, "selected must be a boolean"):
            plan.ExecutionPlan.from_dict(payload)

    def test_dependency_cycles_and_omitted_dependencies_are_rejected(self) -> None:
        payload = self.sample().to_dict()
        payload["stages"][0]["depends_on"] = ["rereview"]
        payload["stages"][1]["depends_on"] = ["implement"]
        payload["stages"][1]["selected"] = True
        payload["stages"][1]["state"] = "planned"
        with self.assertRaisesRegex(plan.ContractError, "cyclic stage dependency"):
            plan.ExecutionPlan.from_dict(payload)

        payload = self.sample().to_dict()
        payload["stages"][0]["depends_on"] = ["rereview"]
        with self.assertRaisesRegex(plan.ContractError, "depends on omitted stage"):
            plan.ExecutionPlan.from_dict(payload)

    def test_unknown_executor_is_rejected(self) -> None:
        payload = self.sample().to_dict()
        payload["stages"][0]["executor"] = "invented"
        with self.assertRaisesRegex(plan.ContractError, "invalid execution plan"):
            plan.ExecutionPlan.from_dict(payload)


class ContractVersionTwoTests(unittest.TestCase):
    def v1_payload(self) -> dict:
        payload = ExecutionPlanTests().sample().to_dict(contract_version=1)
        self.assertEqual(payload["contract_version"], 1)
        self.assertNotIn("policy", payload["stages"][0])
        return payload

    def standard(self) -> plan.ExecutionPlan:
        return profiles.build_plan(
            profiles.resolve_execution(profiles.ExecutionSettings(),
                                       profiles.ExecutionSettings()),
            repository="o/r", work_item="328")

    def test_v2_round_trip_keeps_decisions_obligations_and_controls(self) -> None:
        sample = self.standard()
        payload = sample.to_dict()
        self.assertEqual(payload["contract_version"], 2)
        self.assertEqual({item["control_id"] for item in payload["controls"]}, {"ci", "merge"})
        self.assertTrue(payload["decisions"] and payload["obligations"])
        self.assertEqual(plan.ExecutionPlan.from_dict(payload), sample)
        self.assertEqual(plan.ExecutionPlan.from_dict(
            json.loads(json.dumps(payload))).to_dict(), payload)

    def test_v1_payload_is_read_and_projected_back_unchanged(self) -> None:
        payload = self.v1_payload()
        upgraded = plan.ExecutionPlan.from_dict(payload)
        self.assertEqual(upgraded.contract_version, 2)
        self.assertEqual(upgraded.stages[0].resolution, plan.Resolution.RESOLVED)
        self.assertIsNone(upgraded.stages[0].policy)
        self.assertEqual(upgraded.to_dict(contract_version=1), payload)

    def test_v1_projection_refuses_pending_decisions(self) -> None:
        with self.assertRaisesRegex(plan.ContractError, "decision for security.*version 2"):
            self.standard().to_dict(contract_version=1)

    def test_v1_projection_carries_controls_instead_of_dropping_them(self) -> None:
        local = profiles.build_plan(
            profiles.resolve_execution(
                profiles.ExecutionSettings(),
                profiles.invocation_settings(execution_profile="prototype")),
            repository="o/r", work_item="328")
        projected = local.to_dict(contract_version=1)
        carried = projected["effective_policy"][plan.V1_PROJECTION_KEY]
        self.assertEqual({item["control_id"] for item in carried["controls"]}, {"ci", "merge"})
        self.assertEqual(carried["stages"]["review"]["resolution"], "not_applicable")
        upgraded = plan.ExecutionPlan.from_dict(projected)
        self.assertEqual(upgraded, local)
        self.assertEqual(upgraded.to_dict(contract_version=1), projected)

    def test_v1_projection_envelope_is_validated_on_upgrade(self) -> None:
        local = profiles.build_plan(
            profiles.resolve_execution(profiles.ExecutionSettings(),
                                       profiles.invocation_settings(execution_profile="prototype")),
            repository="o/r", work_item="328")
        for field, value in (("controls", [{"control_id": "merge"}]),
                             ("stages", {"unknown": {}}),
                             ("obligations", [{"subject": "review"}])):
            with self.subTest(field=field):
                payload = local.to_dict(contract_version=1)
                payload["effective_policy"][plan.V1_PROJECTION_KEY][field] = value
                with self.assertRaises(plan.ContractError):
                    plan.ExecutionPlan.from_dict(payload)

    def test_unsupported_versions_are_rejected_not_read_as_v1(self) -> None:
        for version in (0, 3, "1", None, True):
            with self.subTest(version=version):
                payload = self.v1_payload()
                payload["contract_version"] = version
                with self.assertRaisesRegex(plan.ContractError, "unsupported"):
                    plan.ExecutionPlan.from_dict(payload)
        with self.assertRaisesRegex(plan.ContractError, "unsupported"):
            self.standard().to_dict(contract_version=3)
        for version in (True, False, 1.0, 2.0, "2"):
            with self.subTest(output_version=version):
                with self.assertRaisesRegex(plan.ContractError, "unsupported"):
                    self.standard().to_dict(contract_version=version)
        payload = self.v1_payload()
        payload["controls"] = []
        with self.assertRaisesRegex(plan.ContractError, "version 1 plan cannot carry"):
            plan.ExecutionPlan.from_dict(payload)
        payload = self.v1_payload()
        payload["stages"][0]["policy"] = "auto"
        with self.assertRaisesRegex(plan.ContractError, "does not define"):
            plan.ExecutionPlan.from_dict(payload)

    def test_v2_validation(self) -> None:
        def mutated(change) -> dict:
            payload = self.standard().to_dict()
            change(payload)
            return payload

        def stage(payload, stage_id):
            return next(item for item in payload["stages"] if item["stage_id"] == stage_id)

        cases = (
            (lambda p: p["decisions"].pop(), "pending without a pending decision"),
            (lambda p: p["decisions"].append(dict(p["decisions"][0], subject="implement")),
             "which is not pending"),
            (lambda p: p["decisions"][0].update(awaits="weather"), "unknown signal"),
            (lambda p: p["controls"][1].update(automatic=True), "never automatic"),
            (lambda p: p["controls"][0].update(resolution="disabled"), "cannot be disabled"),
            (lambda p: p["controls"].append(dict(p["controls"][0], control_id="deploy")),
             "unknown plan control"),
            (lambda p: p["controls"][0].update(timing="eventually"), "invalid timing"),
            (lambda p: stage(p, "security").update(selected=True, state="planned"),
             "pending and cannot be selected"),
            (lambda p: stage(p, "issue_review").update(policy="disabled"),
             "only a disabled policy"),
            (lambda p: stage(p, "verify").update(embedded_in="verify"), "invalid host"),
            (lambda p: p["obligations"][0].update(subject="nowhere"), "unknown subject"),
            (lambda p: (p["obligations"][0].update(subject="ci", state="applies"),
                        p["controls"][0].update(requirement="optional")), "must be required"),
            (lambda p: (p["obligations"][0].update(subject="implement", state="applies"),
                        stage(p, "implement").update(selected=False, state="not_selected",
                                                      obligation="optional",
                                                      resolution="not_applicable"),
                        stage(p, "verify").update(depends_on=[]),
                        stage(p, "review").update(depends_on=[])), "not applicable but obligation"),
        )
        for change, pattern in cases:
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(plan.ContractError, pattern):
                    plan.ExecutionPlan.from_dict(mutated(change))

    def test_a_pending_required_decision_prevents_execution(self) -> None:
        standard = self.standard()
        self.assertEqual({item.subject for item in standard.pending_required_decisions()},
                         {"rereview"})
        with self.assertRaisesRegex(plan.ContractError, "pending required decision"):
            standard.check_executable("rereview")
        resolved = standard.resolve_decision("security", True, "auth/ changed", "diff")
        self.assertEqual(resolved.revision, 2)
        self.assertIs(resolved.stage("security").obligation, plan.Obligation.REQUIRED)
        resolved.check_executable("security")
        with self.assertRaisesRegex(plan.ContractError, "no pending decision"):
            resolved.resolve_decision("security", True, "again", "diff")


if __name__ == "__main__":
    unittest.main()
