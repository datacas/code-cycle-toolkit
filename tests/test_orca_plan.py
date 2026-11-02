"""Shared policy parity and fail-closed asynchronous Orca boundaries (#330)."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import executors as ex
import orca_plan
from test_run_cycle import RunCycleTestCase, Talker
import router
from cycle_journal import events
from cycle_history import CycleHistoryStore
from execution_plan import ExecutionState, ContractError
from execution_profiles import (ExecutionSettings, build_plan, invocation_settings,
    resolve_execution, apply_rereview_decision, rereview_decision)
from plan_service import (delegation_spec, invalidate_head, persist_plan,
    receipt_for, reconcile_plan, require_security)
from telemetry import Telemetry
from verification_plan import Check, CheckResult, VerificationPlan, parse_criteria

HEAD = "a" * 40
NEXT = "b" * 40
MODES = ("prototype", "standard", "full", "hotfix", "custom")


def sample(mode="standard"):
    return replace(build_plan(resolve_execution(ExecutionSettings(),
        invocation_settings(execution_profile=mode)),
        repository="datacas/code-cycle-toolkit", work_item="330",
        plan_id="plan-test", provenance="run"), head_sha=HEAD)


class OrcaPlanTests(unittest.TestCase):
    def test_frozen_check_recovery_preserves_partial_history(self):
        command = "python3 -m unittest"
        verification = VerificationPlan(parse_criteria("github", "## Acceptance criteria\n- Works"))
        verification = verification.extend((Check("check-1", command, "codex", "deep_coder",
            "project", True, ("AC-1",), provenance="runtime", reason="runtime"),))
        verification = verification.record(CheckResult("check-1", HEAD, "pass",
            ({"exit_code": 0, "executed": 1},)))
        with tempfile.TemporaryDirectory() as directory:
            store = CycleHistoryStore(Telemetry(Path(directory) / "db.sqlite").path)
            plan, _ = persist_plan(replace(sample(), verification=verification), store, cycle_id="cycle")
            record = store.latest_plan(plan.repository, plan.work_item, None)
            with self.assertRaisesRegex(ValueError, "authorized command"):
                reconcile_plan(record, (), head=HEAD)
            current = reconcile_plan(record, (command,), head=HEAD)
            self.assertEqual(current.verification.current("check-1", HEAD), "pass")
            moved = reconcile_plan(record, (command,), head=NEXT)
            self.assertEqual(moved.verification.current("check-1", NEXT), "unverified")
            self.assertEqual(moved.verification.results, verification.results)
            self.assertEqual(moved.verification.checks, verification.checks)

    def test_equivalent_inputs_and_task_specs_in_every_mode(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                native = sample(mode)
                context = ex.OrcaDispatchContext("coordinator", "run", "task",
                    execution_plan=native, stage="implement")
                spec = context.task_spec()
                self.assertEqual(spec["plan"], native.to_dict())
                self.assertEqual(spec["execution_profile"], mode)
                self.assertEqual({key: spec[key] for key in receipt_for(native)}, receipt_for(native))
                self.assertEqual(spec["publication_permissions"], ["comment", "create_pr", "push_branch"])

    def test_rereview_is_conditional_in_every_mode_and_retains_blocks(self):
        for mode in MODES:
            for changes, findings in ((False, False), (False, True), (True, True)):
                with self.subTest(mode=mode, changes=changes, findings=findings):
                    decision = rereview_decision(changes_after_review=changes, open_findings=findings)
                    original = sample(mode)
                    if mode == "prototype":
                        original = replace(build_plan(resolve_execution(ExecutionSettings(),
                            invocation_settings(execution_profile=mode, objective="merge_ready",
                                stages=("review=required",))), repository="datacas/code-cycle-toolkit",
                            work_item="330", plan_id="plan-test"), head_sha=HEAD)
                    plan = apply_rereview_decision(original, decision)
                    self.assertEqual(plan.stage("rereview").selected, changes)
                    self.assertEqual(decision.blocks_retained, findings and not changes)

    def test_head_invalidation_retains_required_security_and_old_evidence(self):
        plan = require_security(sample(), "gate")
        plan = replace(plan, stages=tuple(replace(stage, state=ExecutionState.COMPLETED,
            conclusion="completed") if stage.stage_id == "security" else stage for stage in plan.stages))
        changed = invalidate_head(plan, NEXT)
        self.assertEqual(changed.stage("security").state, ExecutionState.PLANNED)
        self.assertTrue(changed.stage("security").selected)
        self.assertEqual(changed.stage("implement"), plan.stage("implement"))
        self.assertEqual(changed.verification, plan.verification)
        self.assertIs(invalidate_head(plan, HEAD), plan)

    def test_canonical_history_and_reconcile_preserve_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CycleHistoryStore(Telemetry(Path(directory) / "db.sqlite").path)
            plan, receipt = persist_plan(sample("full"), store, cycle_id="cycle")
            self.assertEqual(receipt, receipt_for(plan))
            record = store.latest_plan(plan.repository, plan.work_item, None)
            restored = reconcile_plan(record, (), head=HEAD,
                changes_after_review=False, open_findings=True)
            self.assertEqual(restored.plan_id, plan.plan_id)
            self.assertGreater(restored.revision, plan.revision)
            self.assertFalse(restored.stage("rereview").selected)
            moved = reconcile_plan(record, (), head=NEXT)
            self.assertEqual(moved.head_sha, NEXT)

    def test_authoritative_outcome_requires_all_bindings(self):
        plan = sample()
        context = ex.OrcaDispatchContext("coordinator", "run", "task", execution_plan=plan, stage="implement")
        valid = {"schema": 2, **receipt_for(plan), "run_id": "run", "task_id": "task",
            "repo": plan.repository, "stage": "implement", "head_sha": HEAD,
            "terminal": True, "evidence_valid": True, "status": "IMPLEMENTED"}
        coordinator = Mock()
        coordinator.functional_outcome.return_value = valid
        self.assertEqual(orca_plan.reconcile_outcome(context, plan, coordinator=coordinator), valid)
        for key, value in (("run_id", "other"), ("task_id", "other"), ("revision", 90),
                ("digest", "x"), ("head_sha", NEXT), ("terminal", False),
                ("evidence_valid", False), ("status", "worker_done"), ("schema", 1)):
            with self.subTest(key=key):
                coordinator.functional_outcome.return_value = {**valid, key: value}
                self.assertEqual(orca_plan.reconcile_outcome(context, plan, coordinator=coordinator)["status"], "BLOCKED")
        coordinator.functional_outcome.return_value = {"ok": True, "result": {"state": "ready"}}
        self.assertEqual(orca_plan.reconcile_outcome(context, plan, coordinator=coordinator)["status"], "BLOCKED")
        coordinator.functional_outcome.return_value = None
        self.assertEqual(orca_plan.reconcile_outcome(context, plan, coordinator=coordinator)["status"], "RESULT_PENDING")
        self.assertEqual(orca_plan.reconcile_outcome(context, plan)["status"], "BLOCKED")
        self.assertEqual(orca_plan.reconcile_outcome(context, plan, cancel=True)["status"], "BLOCKED")
        coordinator.request_cancellation.return_value = {"accepted": True, "run_id": "run", "task_id": "task"}
        self.assertEqual(orca_plan.reconcile_outcome(context, plan, coordinator=coordinator, cancel=True)["status"], "RESULT_PENDING")
        coordinator.request_cancellation.assert_called_once_with(run_id="run", task_id="task")
        coordinator.request_cancellation.return_value = {"accepted": False}
        self.assertEqual(orca_plan.reconcile_outcome(context, plan, coordinator=coordinator, cancel=True)["status"], "BLOCKED")

    def test_launch_is_only_pending_and_review_workspace_is_preserved(self):
        target = SimpleNamespace(provider="openai", model="gpt-test", effort="medium")
        plan = sample()
        context = ex.OrcaDispatchContext("coordinator", "run", "task", execution_plan=plan, stage="implement")
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"ok": True,
            "result": {"dispatchId": "dispatch", "state": "ready"}}), ""))
        adapter = ex.OrcaAdapter()
        unsupported = adapter.dispatch(target, "ignored", cwd="/tmp/source", context=context,
                                       writes=True, runner=runner)
        self.assertEqual(unsupported.outcome, ex.DispatchOutcome.BLOCKED)
        runner.assert_not_called()
        mismatch = replace(context, task_spec_reader=lambda **_: {"revision": 99})
        self.assertEqual(adapter.dispatch(target, "ignored", context=mismatch,
            writes=True, runner=runner).outcome, ex.DispatchOutcome.BLOCKED)
        runner.assert_not_called()
        context = replace(context, task_spec_reader=lambda **_: context.task_spec())
        result = adapter.dispatch(target, "ignored", cwd="/tmp/source", context=context,
                                  writes=True, runner=runner)
        self.assertTrue(result.asynchronous)
        self.assertEqual(result.artifacts["functional_state"], "result_pending")
        self.assertEqual(result.artifacts["task_spec"]["revision"], plan.revision)
        self.assertFalse(result.agent_output)
        review = replace(context, stage="review", task_spec_reader=lambda **_: review.task_spec())
        refused = adapter.dispatch(target, "ignored", cwd="/tmp/source", context=review, runner=runner)
        self.assertEqual(refused.missing_capability, "review_workspace_isolation")
        review = replace(review, review_workspace=ex.OrcaReviewWorkspace(
            "/tmp/review", "/tmp/source", "disposable"))
        launched = adapter.dispatch(target, "ignored", cwd="/tmp/source", context=review, runner=runner)
        self.assertTrue(launched.asynchronous)
        self.assertEqual(launched.artifacts["reviewWorkspace"]["path"], "/tmp/review")
        self.assertEqual(launched.artifacts["task_spec"]["publication_permissions"], ["comment"])

    def test_helper_boundary_and_installed_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "plan.json").write_text(json.dumps(sample().to_dict()))
            base = [sys.executable, str(ROOT / "scripts/orca_plan.py")]
            args = ["--database", str(path / "db.sqlite"), "--repo", "datacas/code-cycle-toolkit",
                "--task", "330", "--cycle", "cycle", "--head", HEAD]
            result = subprocess.run([*base, "initialize", *args, "--plan", str(path / "plan.json")],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            initial = json.loads(result.stdout)
            spec = subprocess.run([*base, "spec", *args, "--stage", "implement"], capture_output=True, text=True)
            self.assertEqual(spec.returncode, 0, spec.stderr)
            self.assertEqual(json.loads(spec.stdout)["digest"], initial["digest"])
            duplicate = subprocess.run([*base, "initialize", *args, "--plan", str(path / "plan.json")],
                capture_output=True, text=True)
            self.assertEqual(duplicate.returncode, 1)
        manifest = (ROOT / "scripts/runtime.manifest").read_text()
        self.assertIn("orca_plan.py", manifest)
        self.assertIn("plan_service.py", manifest)

    def test_modes_and_live_evidence_obligation_are_documented(self):
        document = (ROOT / "docs/orca-execution-plan.md").read_text()
        for mode in MODES:
            self.assertIn(f"--execution-profile {mode}", document)
        for token in ("orca-live", "unverified", "commit SHA", "worker_done", "1.4.209"):
            self.assertIn(token, document)


class FixtureOrca(ex.OrcaAdapter):
    requires_publication_preflight = False

    def __init__(self, *, bound=True):
        super().__init__()
        self.contexts = []
        self.bound = bound

    def probe(self):
        return ex.ProbeResult(self.name, ex.Availability.READY, "fixture")

    def publication_access(self, probe, *, writes):
        return True, "fixture"

    def dispatch(self, target, task, **kw):
        context = kw.get("context")
        if context is not None and self.bound:
            # Fixture of an existing coordinator reading its actual Task spec.
            context = replace(context, task_spec_reader=lambda **_: context.task_spec())
            kw["context"] = context
        self.contexts.append(kw.get("context"))
        return super().dispatch(target, task, **kw, runner=lambda *args, **kwargs:
            subprocess.CompletedProcess([], 0, json.dumps({"ok": True,
                "result": {"dispatchId": "fixture-dispatch", "state": "ready"}}), ""))


class OrcaDriverTests(RunCycleTestCase):
    def launch(self, *, bound=True, **kw):
        adapter = FixtureOrca(bound=bound)
        profiles = router.load_profiles({"code_cycle": {"profiles": {
            "cheap_coder": {"primary": "orca:openai/gpt-test medium"},
            "deep_coder": {"primary": "orca:openai/gpt-test medium"}}}})
        report = self.run_cycle(adapter, Talker("claude"), profiles=profiles,
            orca_context=ex.OrcaDispatchContext("coordinator", "run", "task"), **kw)
        return report, adapter

    def test_driver_constructs_context_and_persists_pending_launch(self):
        report, adapter = self.launch()
        self.assertEqual(report.status, "IN_PROGRESS", report.stopped_because)
        self.assertEqual(len(adapter.contexts), 1)
        context = adapter.contexts[0]
        self.assertEqual(context.stage, "implement")
        self.assertEqual(context.execution_plan.repository, "owner/api")
        self.assertEqual(context.execution_plan.work_item, "API-7")
        launches = [row for row in events(self.store.path, "owner/api", task_id="API-7")
                    if row["component"] == "orca_launch"]
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0]["correlation"]["dispatch_id"], "fixture-dispatch")

    def test_resume_and_cancellation_do_not_relaunch_or_accept_partial_results(self):
        self.launch()
        for action in ("resume", "cancel"):
            report, adapter = self.launch(orca_action=action)
            self.assertEqual(report.status, "BLOCKED")
            self.assertIn("no verified authoritative", report.stopped_because)
            self.assertFalse(adapter.contexts)

    def test_missing_history_blocks_without_a_launch(self):
        report, adapter = self.launch(orca_action="resume")
        self.assertEqual(report.status, "BLOCKED")
        self.assertFalse(adapter.contexts)

    def test_missing_task_binding_is_blocked_in_the_driver(self):
        report, _ = self.launch(bound=False)
        self.assertEqual(report.status, "BLOCKED")
        self.assertIn("no verified Orca Task-spec", report.stopped_because)

    def test_resume_rejects_a_different_existing_task(self):
        self.launch()
        report = self.run_cycle(FixtureOrca(), Talker("claude"), orca_action="resume",
            orca_context=ex.OrcaDispatchContext("coordinator", "run", "other-task"))
        self.assertEqual(report.status, "BLOCKED")
        self.assertIn("persisted launch identities", report.stopped_because)


if __name__ == "__main__":
    unittest.main()
