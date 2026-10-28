from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cycle  # noqa: E402
import execution_plan as plan  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
