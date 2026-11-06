"""Exact operation consent layered onto the stage table, never sandbox authority."""
from __future__ import annotations

import json
import shlex
from dataclasses import replace
from pathlib import Path
from typing import Any

from execution_plan import completion_statuses
from grants import (COMMAND_OPTIONS, SensitiveGrant, identifier, make_grant, from_entry, refusal_question,
                    ALLOW_CYCLE, ALLOW_PERSISTENT, ALLOW_GLOBAL, DENY, denial_context, granted_context)
from stage_permissions import (operation, matching_answer, protected_reason, credential_reason, parse_command,
                               consent_role, split_environment)


#: Top-level keys only the runtime writes into a stage's result, after the agent
#: produced it. They are the consent record, not part of any role's contract.
RUNTIME_RESULT_KEYS = ("permission_outcomes", "essential_permission", "stop_reason")
#: Where an agent-supplied value of a runtime-owned key is kept apart. It is
#: outside every closed contract, so a closed contract still rejects it.
AGENT_KEY_PREFIX = "agent_"


def _result_block(spoken: object) -> tuple[int, int, dict[str, Any]] | None:
    if not isinstance(spoken, str):
        return None
    end = spoken.rfind("END_ORCHESTRATION_RESULT")
    begin = spoken.rfind("ORCHESTRATION_RESULT", 0, end) if end >= 0 else -1
    if begin < 0:
        return None
    try:
        payload = json.loads(spoken[begin + len("ORCHESTRATION_RESULT"):end].strip())
    except ValueError:
        return None
    return (begin, end, payload) if isinstance(payload, dict) else None


def claim_runtime_keys(spoken: str) -> str:
    """Move an agent-supplied runtime-owned key aside, before the runtime writes its own.

    Only the runtime may write `RUNTIME_RESULT_KEYS`. Whatever the agent put under
    those names is kept as `agent_<key>`: it can never pass for the runtime's
    record, never seeds it, and a closed contract still rejects it by name.
    """
    block = _result_block(spoken)
    if block is None or not any(key in block[2] for key in RUNTIME_RESULT_KEYS):
        return spoken
    begin, end, payload = block
    for key in RUNTIME_RESULT_KEYS:
        if key in payload:
            payload[AGENT_KEY_PREFIX + key] = payload.pop(key)
    return spoken[:begin] + "ORCHESTRATION_RESULT\n" + json.dumps(payload) + "\n" + spoken[end:]


def split_runtime_keys(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The agent's result for contract validation, and the runtime's record of it.

    Call this after `claim_runtime_keys`: what is left under a runtime-owned name
    was written by the runtime. The record belongs in the cycle record; the
    contract of a role only ever sees what the agent said.
    """
    runtime = {key: payload[key] for key in RUNTIME_RESULT_KEYS if key in payload}
    if not runtime:
        return payload, {}
    return {key: value for key, value in payload.items() if key not in runtime}, runtime


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

        def checkout(raw: str, relative: str) -> bool:
            # The clone is gone too; the hook verified membership while it existed.
            path = Path(raw)
            return (path.is_absolute() and not Path(relative).is_absolute()
                    and ".." not in Path(relative).parts
                    and (relative == "." or path.as_posix().endswith("/" + relative)))

        def placeholder(raw: str, pattern: dict[str, str]) -> bool:
            prefix = (f"{pattern['env']}=" if "env" in pattern
                      else f"{pattern['option']}=" if "option" in pattern else "")
            if not raw.startswith(prefix):
                return False
            value = raw[len(prefix):]
            if "tmpdir" in pattern:
                return temporary(value, pattern["tmpdir"])
            return "checkout" in pattern and checkout(value, pattern["checkout"])
        _, command_words = split_environment(words)
        program = Path(command_words[0]).name if command_words else ""
        data_read = program in {"stat", "ls", "cat", "head", "tail", "wc", "grep"}
        for index, (raw, pattern) in enumerate(zip(words, expected)):
            if raw == pattern:
                continue
            if not isinstance(pattern, dict):
                return False
            if set(pattern) == {"tmpdir"} and not (data_read or program == "docker"):
                return False
            if not (index or "env" in pattern) or not placeholder(raw, pattern):
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
    op = grant.operation or {}
    compose = op.get("compose") if isinstance(op.get("compose"), dict) else None
    return {"kind": "command_permission", "resource": grant.id,
            "command": command, "error": reason,
            "attempted": ["stage command table refused the operation"],
            "requires_human": False,
            # Evidence ties the decision to the exact check and configuration (#398).
            **({"check": op["check"]} if op.get("check") else {}),
            **({"config_sha256": compose["config_sha256"],
                "warnings": list(compose.get("warnings") or [])} if compose else {})}


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
        observed_policy = replace(policy, tmpdir=event.get("tmpdir") or policy.tmpdir)
        if tool == "Bash" and check(data["command"], observed_policy, cwd=cwd).needs_docker:
            # The docker_host socket permission is asked first, on its own (#398);
            # an exact-command consent never stands in for it.
            continue
        try:
            # The native scratch directory is gone by the time dispatch returns.
            # Retain the pattern the hook verified while its symlinks existed.
            observed = from_entry({"operation": event.get("operation"),
                                   "executor": executor, "platform": broker.platform})
            if (observed is not None and observed.operation is not None
                    and consent_role(observed.operation["role"]) == consent_role(policy.role)
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
    configured = blocker.get("check")
    # A configured check the person declines is recorded unverified; it never
    # stops the cycle by itself (#398, #414): the final state names it.
    essential = (False if configured
                 else effect.get("essential") is not False if effect is not None
                 else payload.get("status") not in completed)
    tests = payload.get("tests")
    checks = tests.get("checks", []) if isinstance(tests, dict) else []
    check_ids = effect.get("check_ids", []) if effect is not None else []
    if not isinstance(check_ids, list):
        check_ids = []
    if configured:
        check_ids = [*check_ids, configured]
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
        "answer": answer or "pending", "essential": essential, "checks": affected,
        **{key: blocker[key] for key in ("check", "config_sha256", "warnings") if key in blocker}})
    payload["permission_outcomes"] = outcomes
    return (spoken[:begin] + "ORCHESTRATION_RESULT\n" + json.dumps(payload) + "\n" + spoken[end:], essential)


def declared_compose_checks(policy: Any) -> list[str]:
    """The configured verification commands that run `docker compose`."""
    commands = []
    for command in getattr(policy, "verification_commands", ()) or ():
        try:
            words = list(parse_command(command, None).words)
        except ValueError:
            continue
        _, command_words = split_environment(words)
        if command_words[:1] and Path(command_words[0]).name == "docker" and command_words[1:2] == ["compose"]:
            commands.append(command)
    return commands


_ALLOWS = frozenset({ALLOW_CYCLE, ALLOW_PERSISTENT, ALLOW_GLOBAL})


def codex_compose_consent(policy: Any, broker: Any, executor: str, role: str,
                          cwd: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Ask the #409 consent before a Codex dispatch for each declared Compose check.

    Codex has no command hook (#398): nothing refuses a command during its run,
    so the runtime asks before dispatch, with the same identity the Claude hook
    computes, and tells the agent each answer. It cannot limit the dispatch to
    the consented command or abort when the configuration changes mid-run;
    `compose_drift` compares the digest afterwards.
    """
    context: list[str] = []
    records: list[dict[str, Any]] = []
    for command in declared_compose_checks(policy):
        try:
            op = operation("Bash", {"command": command}, policy, cwd)
        except (ValueError, OSError, TypeError) as error:
            context.append(f"Declared Compose check `{command}` could not be identified ({error}). "
                           "Do not run it; record it unverified with that cause.")
            records.append({"command": command, "answer": "unidentified", "error": str(error)})
            continue
        grant = make_grant(op, executor, broker.platform)
        broker.operations[grant.id] = grant
        request = request_record(grant, command, "Codex has no command hook; consent is asked before dispatch")
        answer = broker.request(grant, executor, role=role, blocker=request)
        records.append({**{key: request[key] for key in ("check", "config_sha256", "warnings")
                           if key in request},
                        "grant": grant.id, "command": command, "answer": answer})
        if answer in _ALLOWS:
            scope = grant.scope_for(executor, broker.platform)
            context.append((granted_context(grant, scope) if scope is not None else "")
                           + f" Run the declared check exactly as `{command}`. On Codex nothing "
                           "limits this dispatch to that command or aborts a changed configuration; "
                           "the runtime compares the configuration digest after the run.")
        elif answer == DENY:
            context.append(denial_context(grant))
        else:
            context.append(f"Declared Compose check `{command}` awaits the user's consent ({grant.id}). "
                           "Do not run it; record it unverified with cause "
                           f"`permission {grant.id}: pending` and complete the other steps.")
    return "\n\n".join(context), records


def compose_drift(records: list[dict[str, Any]], policy: Any, cwd: str | None) -> list[dict[str, Any]]:
    """The evidence of each pre-dispatch Codex consent, rechecked after the run.

    Every consent becomes a permission outcome with its check, digest and
    warnings. For an allowed one the configuration is resolved again; a change
    is disclosed with both digests and a warning, and does not by itself
    downgrade the check (#398).
    """
    outcomes = []
    for record in records:
        outcome = {"grant": record.get("grant"), "command": record["command"],
                   "answer": record["answer"], "essential": False, "checks": [],
                   "executor_hook": "none: consent asked before dispatch",
                   **{key: record[key] for key in ("check", "config_sha256", "warnings", "error")
                      if key in record}}
        if record.get("answer") in _ALLOWS and "config_sha256" in record:
            try:
                after = operation("Bash", {"command": record["command"]}, policy, cwd)["compose"]["config_sha256"]
            except (ValueError, OSError, TypeError, KeyError):
                after = None
            if after != record["config_sha256"]:
                outcome.update(config_sha256_before=record["config_sha256"], config_sha256_after=after,
                               warning="the effective Compose configuration changed during the "
                                       "Codex run, or could no longer be resolved")
        outcomes.append(outcome)
    return outcomes


def add_outcomes(spoken: str, outcomes: list[dict[str, Any]]) -> str:
    """Append runtime-observed permission outcomes to a stage's result block."""
    block = _result_block(spoken)
    if block is None or not outcomes:
        return spoken
    begin, end, payload = block
    existing = payload.get("permission_outcomes")
    payload["permission_outcomes"] = [*(existing if isinstance(existing, list) else []), *outcomes]
    return spoken[:begin] + "ORCHESTRATION_RESULT\n" + json.dumps(payload) + "\n" + spoken[end:]


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
