"""Installed plan helper for the existing Orca coordinator, not a coordinator.

No Orca CLI or undocumented endpoint is invoked here. Run/Task ownership and
the source of authoritative outcomes remain with the installed coordinator.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, TYPE_CHECKING

from cycle_history import CycleHistoryStore
from execution_plan import ContractError, ExecutionPlan
if TYPE_CHECKING:
    from .plan_service import (delegation_spec, persist_plan, receipt_for,
                              reconcile_plan, require_security)
else:
    from plan_service import (delegation_spec, persist_plan, receipt_for,
                             reconcile_plan, require_security)
from telemetry import Telemetry


def reconcile_outcome(context, plan, *, coordinator=None, cancel=False) -> dict[str, Any]:
    """Consume a capability supplied by the existing coordinator.

    The stock CLI has no verified binding for this capability. Tests inject a
    coordinator, never convert a launch receipt or worker_done to an outcome.
    """
    capability = "request_cancellation" if cancel else "functional_outcome"
    if (context.execution_plan is not None
            and receipt_for(context.execution_plan) != receipt_for(plan)):
        return {"status": "BLOCKED", "reason": "delegation plan was invalidated; reconcile its evidence",
                **receipt_for(plan)}
    method = getattr(coordinator, capability, None)
    if not callable(method):
        return {"status": "BLOCKED", "capability": capability,
                "reason": "no verified authoritative Orca coordinator interface",
                **receipt_for(plan)}
    if not isinstance(plan.head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", plan.head_sha):
        return {"status": "BLOCKED", "reason": "authoritative outcome needs the applicable full HEAD",
                **receipt_for(plan)}
    result = method(run_id=context.run_id, task_id=context.task_id)
    if cancel:
        # Cancellation acknowledgement is not functional completion either.
        if (not isinstance(result, dict) or result.get("accepted") is not True
                or result.get("run_id") != context.run_id
                or result.get("task_id") != context.task_id):
            return {"status": "BLOCKED", "reason": "coordinator did not confirm the cancellation request",
                    **receipt_for(plan)}
        return {"status": "RESULT_PENDING", "cancellation_requested": True,
                **receipt_for(plan)}
    if result is None:
        return {"status": "RESULT_PENDING", **receipt_for(plan)}
    expected = {**receipt_for(plan), "run_id": context.run_id,
                "task_id": context.task_id, "head_sha": plan.head_sha,
                "stage": context.stage, "repo": plan.repository}
    if (not isinstance(result, dict) or type(result.get("schema")) is not int
            or result.get("schema") != 2 or type(result.get("revision")) is not int
            or any(result.get(key) != value for key, value in expected.items())
            or result.get("evidence_valid") is not True
            or result.get("terminal") is not True):
        return {"status": "BLOCKED", "reason": "stale, partial or mismatched coordinator outcome",
                **receipt_for(plan)}
    from execution_plan import stage_definition
    accepted = stage_definition(context.stage).completes_with | {"BLOCKED", "FAILED", "CANCELLED"}
    if (context.stage in {"review", "rereview"}
            and plan.control("ci").timing == "parallel"):
        accepted = accepted | {"REVIEW_PASSED_AWAITING_CI"}
    if result.get("status") not in accepted:
        return {"status": "BLOCKED", "reason": "no terminal functional outcome",
                **receipt_for(plan)}
    # This only recovers an outcome. Existing stage/check validators and exit
    # gates must still judge its functional payload, findings and evidence.
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("initialize", "reconcile", "spec", "security", "rereview"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task", required=True, help="work-item ID, not Orca Task ID")
    parser.add_argument("--cycle", required=True)
    parser.add_argument("--pr")
    parser.add_argument("--plan", help="ExecutionPlan JSON from run_cycle --dry-run --json")
    parser.add_argument("--head", required=True)
    parser.add_argument("--command", action="append", default=[], help="currently authorized binding")
    parser.add_argument("--stage")
    parser.add_argument("--changes-after-review", action="store_true")
    parser.add_argument("--open-findings", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.head):
            raise ContractError("--head needs the full current commit SHA")
        # Telemetry owns the schema. This never changes a project database.
        telemetry = Telemetry(Path(args.database))
        store = CycleHistoryStore(telemetry.path)
        record = store.latest_plan(args.repo, args.task, args.pr)
        if args.action == "initialize":
            if record is not None:
                raise ContractError("a persisted plan exists; reconcile it instead of replacing it")
            if not args.plan:
                raise ContractError("initialize needs --plan from the shared resolver")
            plan = ExecutionPlan.from_dict(json.loads(Path(args.plan).read_text(encoding="utf-8")))
            if plan.repository != args.repo or plan.work_item != args.task:
                raise ContractError("plan scope does not match the requested work item")
            plan = replace(plan, head_sha=args.head)
        else:
            if record is None:
                raise ContractError("no recoverable shared plan")
            plan = reconcile_plan(record, args.command, head=args.head,
                changes_after_review=args.changes_after_review if args.action == "rereview" else None,
                open_findings=args.open_findings)
        if args.action == "security":
            plan = require_security(plan, "Orca coordinator observed security gate")
        if args.action == "spec":
            assert record is not None
            if plan.head_sha != record["head_sha"]:
                raise ContractError("HEAD changed; reconcile and persist before delegation")
            output = delegation_spec(plan, args.stage)
        else:
            plan, receipt = persist_plan(plan, store, cycle_id=args.cycle,
                change_request_id=args.pr,
                provenance="initial" if args.action == "initialize" else "recovered")
            output = {**receipt, "plan": plan.to_dict()}
        print(json.dumps(output, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
