"""Transport-independent plan operations; no recorder, worker or driver state."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from cycle_history import CycleHistoryStore, plan_digest, plan_projection, restore_plan
from execution_plan import ExecutionPlan, ExecutionState, Obligation, Resolution, StagePolicy
from execution_profiles import apply_rereview_decision, rereview_decision


def receipt_for(plan: ExecutionPlan) -> dict[str, Any]:
    plan.validate()
    return {"plan_id": plan.plan_id, "revision": plan.revision,
            "digest": plan_digest(plan_projection(plan))}


def persist_plan(plan: ExecutionPlan, store: CycleHistoryStore, *, cycle_id: str,
                 change_request_id: str | None = None, provenance: str = "observed",
                 stop_reason: str | None = None) -> tuple[ExecutionPlan, dict[str, Any]]:
    """Append a revision through the same privacy-safe store for every transport."""
    plan = replace(plan, revision=plan.revision + 1)
    plan.validate()
    receipt = store.save_plan(plan, cycle_id=cycle_id,
        change_request_id=change_request_id, provenance=provenance, stop_reason=stop_reason)
    return plan, receipt


def invalidate_head(plan: ExecutionPlan, head: str | None) -> ExecutionPlan:
    """Keep obligations and historical checks, invalidate only HEAD-bound evidence."""
    if head == plan.head_sha:
        return plan
    return replace(plan, head_sha=head, stages=tuple(
        replace(item, state=ExecutionState.PLANNED, conclusion=None)
        if item.stage_id in {"verify", "security"} and item.selected else item
        for item in plan.stages), metadata={**plan.metadata,
            "verification_conclusion": "not_verified"})


def require_security(plan: ExecutionPlan, reason: str) -> ExecutionPlan:
    security = plan.stage("security")
    if security.resolution is Resolution.PENDING:
        return plan.resolve_decision("security", True, reason, "runtime")
    if security.policy is StagePolicy.DISABLED:
        return plan.settle_obligation("security_policy", True, reason)
    plan = replace(plan, stages=tuple(
        replace(item, selected=True, obligation=Obligation.REQUIRED,
            state=ExecutionState.PLANNED if not item.selected else item.state,
            resolution=Resolution.RESOLVED)
        if item.stage_id == "security" else item for item in plan.stages))
    return plan.settle_obligation("security_policy", True, reason)


def reconcile_plan(record: dict[str, Any], commands: tuple[str, ...] | list[str], *,
                   head: str | None, changes_after_review: bool | None = None,
                   open_findings: bool = False) -> ExecutionPlan:
    """Recover canonical identity and obligations before deciding any new work.

    Commands are rebound only from today's authorization. Partial results never
    erase checks, and verification results remain tied to their original HEAD.
    Review invalidation must be observed by the coordinator, not guessed here.
    """
    plan = invalidate_head(restore_plan(record, tuple(commands)), head)
    if changes_after_review is not None:
        decision = rereview_decision(changes_after_review=changes_after_review,
                                     open_findings=open_findings)
        plan = apply_rereview_decision(plan, decision)
    plan.validate()
    return plan


def delegation_spec(plan: ExecutionPlan, role: str) -> dict[str, Any]:
    """Data the coordinator puts in its existing Task before worker-start."""
    plan.check_executable(role)
    stage = plan.stage(role)
    from execution_plan import stage_definition
    definition = stage_definition(role)
    return {**receipt_for(plan), "objective": plan.objective,
        "execution_profile": plan.execution_profile, "stage": role,
        "head_sha": plan.head_sha, "provenance": stage.provenance,
        "workspace_policy": definition.workspace_policy.value,
        "publication_permissions": list(definition.publication_permissions),
        "security_required": plan.stage("security").selected,
        "verify_selected": plan.stage("verify").selected,
        "plan": plan.to_dict()}
