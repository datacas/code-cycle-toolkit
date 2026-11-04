"""Exact operation consent layered onto the stage table, never sandbox authority."""
from __future__ import annotations

import json
import shlex
from dataclasses import replace
from pathlib import Path
from typing import Any

from execution_plan import completion_statuses
from grants import (COMMAND_OPTIONS, SensitiveGrant, identifier, make_grant, from_entry, refusal_question)
from stage_permissions import operation, matching_answer, protected_reason, credential_reason, parse_command


def observed_pattern_matches(op: dict[str, Any], tool: str, data: dict[str, Any], policy: Any) -> bool:
    """Metadata is a request hint. It cannot change the observed action or arguments."""
    if tool != "Bash" or not isinstance(data.get("command"), str):
        return False
    try:
        parsed = parse_command(data["command"], policy.tmpdir)
        words = list(parsed.words)
        if words[:1] == ["rtk"]:
            words = words[2:] if words[1:2] == ["proxy"] else words[1:]
        expected = op["pattern"]["words"]
        if not words or len(words) != len(expected):
            return False
        def temporary(raw: str, relative: str) -> bool:
            return (bool(policy.tmpdir) and Path(raw).is_absolute()
                    and Path(raw).is_relative_to(policy.tmpdir)
                    and not Path(relative).is_absolute() and ".." not in Path(relative).parts)
        data_read = words[0] in {"stat", "ls", "cat", "head", "tail", "wc", "grep"}
        for index, (raw, pattern) in enumerate(zip(words, expected)):
            if raw == pattern:
                continue
            if not (index and data_read and isinstance(pattern, dict)
                    and temporary(raw, pattern["tmpdir"])):
                return False
        redirects = op["pattern"]["redirects"]
        if len(redirects) != len(parsed.redirects):
            return False
        for (fd, operator, raw), (other_fd, other_operator, pattern) in zip(parsed.redirects, redirects):
            if (fd, operator) != (other_fd, other_operator):
                return False
            if raw is None and pattern is None:
                continue
            if not (isinstance(raw, str) and isinstance(pattern, str)
                    and pattern.startswith("<TMPDIR>/") and temporary(raw, pattern[9:])):
                return False
        return True
    except (ValueError, TypeError, KeyError):
        return False


def request_record(grant: SensitiveGrant, command: str, reason: str) -> dict[str, Any]:
    return {"kind": "command_permission", "resource": grant.id,
            "command": command, "error": reason,
            "attempted": ["stage command table refused the operation"],
            "requires_human": False}


def refused_operation(denials: object, policy: Any, broker: Any, executor: str,
                      cwd: str | None = None) -> dict[str, Any] | None:
    """Turn runtime-observed refusals into a request; never authorize from prose."""
    from stage_permissions import check, check_file_edit
    events: list[Any] = denials if isinstance(denials, list) else []
    for event in events:
        if isinstance(event, str):
            event = {"tool": "Bash", "input": {"command": event}}
        if not isinstance(event, dict):
            continue
        tool, data = event.get("tool", "Bash"), event.get("input", {})
        if not isinstance(data, dict):
            continue
        if not isinstance(tool, str) or (tool == "Bash" and not isinstance(data.get("command"), str)):
            continue
        try:
            observed_policy = replace(policy, tmpdir=event.get("tmpdir") or policy.tmpdir)
            # The native scratch directory is gone by the time dispatch returns.
            # Retain the pattern the hook verified while its symlinks existed.
            observed = from_entry({"operation": event.get("operation"),
                                   "executor": executor, "platform": broker.platform})
            if (observed is not None and observed.operation is not None
                    and observed.operation["role"] == policy.role
                    and observed.operation["tool"] == tool
                    and observed_pattern_matches(observed.operation, tool, data, observed_policy)):
                op = observed.operation
            else:
                op = operation(tool, data, observed_policy, cwd)
        except (ValueError, TypeError, OSError):
            continue
        command = str(data.get("command", ""))
        decision = (check(command, observed_policy, cwd=cwd) if tool == "Bash"
                    else check_file_edit(tool, data, observed_policy, cwd=cwd))
        reason = decision.reason if not decision.allowed else "executor permission layer refused the operation"
        guard = protected_reason(tool, data, observed_policy, cwd)
        if guard is not None:
            continue
        grant = make_grant(op, executor, broker.platform)
        broker.operations[grant.id] = grant
        command = command if tool == "Bash" else f"{tool} {op['pattern']['target']}"
        return request_record(grant, command, reason)
    return None


def refused_operations(denials: object, policy: Any, broker: Any, executor: str,
                       cwd: str | None = None) -> list[dict[str, Any]]:
    requests = {}
    for event in denials if isinstance(denials, list) else []:
        request = refused_operation([event], policy, broker, executor, cwd)
        if request is not None:
            requests[request["resource"]] = request
    return list(requests.values())


def reconcile_result(spoken: str, blocker: dict[str, Any], answer: str | None, role: str) -> tuple[str, bool]:
    """Use structured evidence to distinguish an essential denial from inspection.

    Completion claims still pass the runtime's normal publication/effect guards.
    A check linked to the refused operation can never retain a pass.
    """
    end = spoken.rfind("END_ORCHESTRATION_RESULT")
    begin = spoken.rfind("ORCHESTRATION_RESULT", 0, end) if end >= 0 else -1
    if begin < 0:
        return spoken, True
    try:
        payload = json.loads(spoken[begin + len("ORCHESTRATION_RESULT"):end].strip())
    except ValueError:
        return spoken, True
    if not isinstance(payload, dict):
        return spoken, True
    effects = payload.get("permission_effects", [])
    effect = next((item for item in effects if isinstance(item, dict)
                   and item.get("command") == blocker["command"]), None) if isinstance(effects, list) else None
    completed = completion_statuses().get(role, frozenset())
    if role in {"review", "rereview"}:
        completed |= {"REVIEW_PASSED_AWAITING_CI"}
    elif role == "verify":
        completed |= {"VERIFIED"}
    elif role == "issue_review":
        # A finished assessment can ask for refinement without needing access.
        completed |= {"NEEDS_REFINEMENT"}
    essential = (effect.get("essential") is not False if effect is not None
                 else payload.get("status") not in completed)
    tests = payload.get("tests")
    checks = tests.get("checks", []) if isinstance(tests, dict) else []
    check_ids = effect.get("check_ids", []) if effect is not None else []
    if not isinstance(check_ids, list):
        check_ids = []
    affected = []
    for check in checks if isinstance(checks, list) else []:
        if isinstance(check, dict) and (same_command(check.get("command"), blocker["command"]) or
                check.get("id") in check_ids):
            prior_attempts = check.get("attempts", [])
            if not isinstance(prior_attempts, list):
                prior_attempts = []
            previous = {key: check[key] for key in ("result", "evidence", "cause") if key in check}
            check.update(result="unverified", cause=f"permission {blocker['resource']}: {answer or 'unanswered'}",
                         evidence=[], attempts=[*prior_attempts, {"superseded": previous,
                             "reason": "runtime observed a refusal of this check"}])
            affected.append(check.get("id"))
    if affected:
        payload["tests"].update(conclusion="not_verified", passed=False)
    if answer == "deny" and essential:
        payload.update(status="BLOCKED", stop_reason="HUMAN_INTERVENTION",
                       essential_permission=blocker["resource"])
    elif not essential and payload.get("status") == "BLOCKED":
        # A distinct structured completion claim is required; prose is irrelevant.
        if (payload.get("completed_status") in completed
                and (role != "implement" or (payload.get("head_sha") and
                     (payload.get("change_request_id") or payload.get("pr_number"))))):
            payload["status"] = payload["completed_status"]
    outcomes = payload.get("permission_outcomes")
    if not isinstance(outcomes, list):
        outcomes = []
    outcomes.append({
        "grant": blocker["resource"], "command": blocker["command"],
        "answer": answer or "pending", "essential": essential, "checks": affected})
    payload["permission_outcomes"] = outcomes
    return (spoken[:begin] + "ORCHESTRATION_RESULT\n" + json.dumps(payload) + "\n" + spoken[end:], essential)


def same_command(left, right) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        def words(command):
            result = shlex.split(command)
            if result[:1] == ["rtk"]:
                result = result[2:] if result[1:2] == ["proxy"] else result[1:]
            return result
        return words(left) == words(right)
    except ValueError:
        return left == right
