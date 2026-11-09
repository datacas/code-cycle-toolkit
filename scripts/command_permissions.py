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
                               consent_role, split_environment, same_operation, configured_check_id)


#: Top-level keys only the runtime writes into a stage's result, after the agent
#: produced it. They are the consent record, not part of any role's contract.
RUNTIME_RESULT_KEYS = ("permission_outcomes", "essential_permission", "stop_reason", "reconciliation")
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


#: What a refused operation turns out to be, decided by the runtime (#364).
AUXILIARY_DENIAL = "auxiliary_denial"
REDUNDANT_OPERATION = "redundant_operation"
ESSENTIAL_PERMISSION = "essential_permission"
ENVIRONMENT_BLOCKER = "environment_blocker"
UNKNOWN_DENIAL = "unknown"
DENIAL_CLASSES = (AUXILIARY_DENIAL, REDUNDANT_OPERATION, ESSENTIAL_PERMISSION,
                  ENVIRONMENT_BLOCKER, UNKNOWN_DENIAL)
#: Only these two let a stage that reported `BLOCKED` be reconciled. `unknown`
#: is neither assumed essential nor assumed auxiliary: it prevents completion.
RECONCILABLE_DENIALS = frozenset({AUXILIARY_DENIAL, REDUNDANT_OPERATION})
#: The roles whose publication the runtime can verify against the code host.
PUBLISHING_ROLES = frozenset({"implement", "resolve"})

_READ_ONLY = frozenset({
    "pwd", "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "stat", "echo",
    "printf", "true", "which", "sort", "uniq", "cut", "tr", "diff", "test", "[", "id",
    "whoami", "basename", "dirname", "realpath", "file", "tree", "env", "printenv"})
_GIT_READ_ONLY = frozenset({
    "grep", "log", "show", "status", "diff", "rev-parse", "ls-files", "ls-remote", "describe",
    "blame", "rev-list", "cat-file", "show-ref", "merge-base", "symbolic-ref", "shortlog",
    "for-each-ref", "name-rev", "diff-tree"})
_GH_READ_ONLY = frozenset({"view", "list", "checks", "diff", "status"})
_INTERPRETERS = frozenset({"python", "python3", "node", "ruby", "perl"})
_LINE_SEPARATORS = {"|", "||", "&&", ";", "&", ";;"}


def _shell_kind(command: str) -> str:
    """What a refused shell command would have done, from its text alone.

    One of `read_only`, `interpreter`, `push`, `pr_create`, `pr_edit`, `commit`,
    `comment`, `write` and `unknown`. The text is never executed; a command the
    lexer cannot read, or any substitution, is `unknown`.
    """
    line = command.split("<<", 1)[0] if "<<" in command else command
    line = line.replace("\n", " ; ")
    if "`" in line or "$(" in line:
        return "unknown"
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return "unknown"
    segments: list[list[str]] = [[]]
    kinds: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _LINE_SEPARATORS:
            segments.append([])
        elif token and set(token) <= set("<>") and ">" in token:
            target = tokens[index + 1] if index + 1 < len(tokens) else ""
            index += 1
            if not (target.isdigit() or target == "/dev/null" or target.startswith("$TMPDIR/")
                    or target.startswith("${TMPDIR}/")):
                kinds.append("write")
        elif token and set(token) <= set("<>"):
            index += 1  # an input redirection names a file to read
        elif token in {"(", ")"}:
            return "unknown"
        else:
            segments[-1].append(token)
        index += 1
    for words in segments:
        while words and "=" in words[0] and not words[0].startswith(("-", "=")):
            words.pop(0)
        if words[:1] == ["rtk"]:
            words = words[2:] if words[1:2] == ["proxy"] else words[1:]
        if words:
            kinds.append(_word_kind(words))
    kinds = [kind for kind in kinds if kind != "read_only"]
    if len(kinds) > 1:
        # A chain such as `git push && gh pr edit …` has no single intent: each
        # part would need its own check, so none is inferred from the first (#364).
        return "write" if "write" in kinds else "unknown"
    return kinds[0] if kinds else "read_only"


def _word_kind(words: list[str]) -> str:
    program = Path(words[0]).name
    if program == "find":
        return "write" if any(word in {"-delete", "-exec", "-execdir", "-fprint", "-ok"}
                              for word in words) else "read_only"
    if program in {"sed", "date", "ps"}:
        # Read-only only in the forms the stage table accepts (#464); `sed -i`,
        # `date -s` and a `ps` that shows command lines are not inspection.
        import stage_permissions as sp
        check = {"sed": sp.check_sed, "date": sp.check_date, "ps": sp.check_ps}[program]
        try:
            check(words[1:])
        except sp.PolicyError:
            return "write" if program in {"sed", "date"} else "unknown"
        return "read_only"
    if program in _READ_ONLY:
        return "read_only"
    if program in _INTERPRETERS:
        return "interpreter"
    if program == "git":
        rest, skip = [], False
        for word in words[1:]:
            if skip:
                skip = False
            elif word in {"-C", "-c"}:
                skip = True
            elif not word.startswith("-"):
                rest.append(word)
        sub = rest[0] if rest else ""
        if sub in _GIT_READ_ONLY:
            return "read_only"
        return {"push": "push", "commit": "commit"}.get(sub, "write" if sub else "unknown")
    if program == "gh":
        area = words[1] if len(words) > 1 else ""
        action = words[2] if len(words) > 2 else ""
        if area in {"pr", "issue", "repo", "run", "release"} and action in _GH_READ_ONLY:
            return "read_only"
        if area == "auth" and action == "status":
            return "read_only"
        if area == "pr" and action in {"create", "edit"}:
            return "pr_create" if action == "create" else "pr_edit"
        if area in {"pr", "issue"} and action in {"comment", "review"}:
            return "comment"
        return "unknown"
    return "unknown"


def _pull_matches(pull: dict[str, Any], intent: object) -> bool | None:
    """Whether the change request already has what a refused edit would set.

    None when the intent is unknown or names a field the runtime did not read:
    nothing is inferred from a missing field (#364).
    """
    if not isinstance(intent, dict) or intent.get("unknown") or not intent.get("fields"):
        return None
    import hashlib
    observed_labels = pull.get("labels")
    for name, wanted in intent["fields"].items():
        if name == "title" and isinstance(pull.get("title"), str):
            same = pull["title"] == wanted
        elif name == "body" and isinstance(pull.get("body"), str) and isinstance(wanted, dict):
            same = hashlib.sha256(pull["body"].strip().encode()).hexdigest() == wanted.get("sha256")
        elif name == "base" and isinstance(pull.get("base"), str):
            same = pull["base"] == wanted
        elif name == "milestone" and "milestone" in pull:
            same = pull["milestone"] == wanted
        elif name == "labels_add" and isinstance(observed_labels, list):
            same = set(wanted) <= set(observed_labels)
        elif name == "labels_remove" and isinstance(observed_labels, list):
            same = not set(wanted) & set(observed_labels)
        else:
            return None
        if not same:
            return False
    return True


def publication_shas(publication: dict[str, Any] | None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    pull = publication.get("pr") if isinstance(publication, dict) else None
    reported = (payload or {}).get("head_sha")
    return {"local": (publication or {}).get("local_head"), "remote": (publication or {}).get("remote_head"),
            "pull_request": pull.get("head_sha") if isinstance(pull, dict) else None,
            "reported": reported if isinstance(reported, str) else None}


def publication_agrees(publication: dict[str, Any] | None,
                       payload: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Whether the remote head, the local HEAD and the PR head are one commit.

    The branch must be the change request's head, the worktree clean and, when
    the result names a head or a change request, those must be the observed
    ones. Anything unobserved fails: absence is not agreement.
    """
    if not isinstance(publication, dict):
        return False, "the published state could not be observed"
    shas = publication_shas(publication, payload)
    pull = publication.get("pr")
    missing = [name for name in ("local", "remote", "pull_request") if not shas[name]]
    if missing:
        return False, f"no observed head for: {', '.join(missing)}"
    if len({shas["local"], shas["remote"], shas["pull_request"]}) != 1:
        return False, (f"the heads diverge: local {shas['local']}, remote {shas['remote']}, "
                       f"pull request {shas['pull_request']}")
    if not isinstance(pull, dict) or pull.get("state") != "OPEN":
        return False, "the change request is not open"
    if pull.get("head_ref") != publication.get("branch"):
        return False, "the change request head is not the cycle branch"
    if publication.get("clean") is not True:
        return False, "the worktree has uncommitted changes"
    if payload is not None:
        if shas["reported"] != shas["local"]:
            return False, f"the result reports head {shas['reported']}, not the published head"
        for key in ("change_request_id", "pr_number"):
            claimed = payload.get(key)
            if claimed not in (None, "") and str(claimed).rstrip("/").rsplit("/", 1)[-1].lstrip("#") != pull.get("number"):
                return False, f"the result names change request {claimed}, not {pull.get('number')}"
    return True, "the remote head, the local HEAD and the pull request head are one commit"


def denial_records(denials: object, policy: Any, executor: str,
                   cwd: str | None = None) -> list[dict[str, Any]]:
    """One record for every refusal the stage met, grantable or not (#364).

    Each carries the command, the stage, the reason, the rule and an operation
    identifier. Recording a denial never authorizes the operation, and nothing
    is skipped: a refusal the runtime cannot classify is `unknown`, not absent.
    """
    from stage_permissions import FILE_EDIT_TOOLS, check, check_file_edit, denial_operation_id
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for event in denials if isinstance(denials, list) else []:
        if isinstance(event, str):
            event = {"tool": "Bash", "input": {"command": event}}
        if not isinstance(event, dict):
            continue
        tool, data = event.get("tool", "Bash"), event.get("input", {})
        if not isinstance(tool, str) or not isinstance(data, dict):
            continue
        observed = replace(policy, tmpdir=event.get("tmpdir") or policy.tmpdir)
        target = None
        if tool == "Bash":
            if not isinstance(data.get("command"), str):
                continue
            command = data["command"]
        else:
            target = data.get(FILE_EDIT_TOOLS.get(tool, ""))
            command = f"{tool} {target}" if isinstance(target, str) else tool
        try:
            decision = (check(command, observed, cwd=cwd) if tool == "Bash"
                        else check_file_edit(tool, data, observed, cwd=cwd))
            rule = event.get("rule") or (decision.rule if not decision.allowed else "")
            reason = decision.reason if not decision.allowed else "executor permission layer refused the operation"
        except Exception:
            rule, reason = event.get("rule") or "", "the refusal could not be re-evaluated"
        try:
            requested = event.get("operation") or operation(tool, data, observed, cwd)
        except (ValueError, TypeError, OSError):
            requested = None
        identity = event.get("operation_id") or denial_operation_id(requested, command)
        records.setdefault((identity, command), {
            "tool": tool, "command": command, "stage": policy.role, "reason": reason,
            "rule": rule or "executor_permission_layer", "operation": identity,
            "target": target if isinstance(target, str) else None,
            "intent": event.get("intent") if isinstance(event.get("intent"), dict) else None,
            "class": UNKNOWN_DENIAL, "why": "not classified"})
    return list(records.values())


def classify_denial(record: dict[str, Any], payload: dict[str, Any] | None, *,
                    mandatory: Any = None, publication: dict[str, Any] | None = None,
                    environment: tuple[str, ...] = (), cwd: str | None = None) -> tuple[str, str]:
    """Decide what one refusal was, from runtime evidence and not the agent's word.

    The agent's `permission_effects` can only make a denial more serious, never
    less: an `essential: true` there is honoured, an `essential: false` is not.
    `mandatory` says whether a command is a configured or required check.
    """
    payload = payload if isinstance(payload, dict) else {}
    command = record["command"]
    blocker = payload.get("environment_blocker")
    if (any(same_command(command, item) for item in environment)
            or (isinstance(blocker, dict) and same_command(command, blocker.get("command")))):
        return ENVIRONMENT_BLOCKER, "a required resource was unreachable"
    effects = payload.get("permission_effects")
    if any(isinstance(item, dict) and same_command(item.get("command"), command)
           and item.get("essential") is True
           for item in (effects if isinstance(effects, list) else [])):
        return ESSENTIAL_PERMISSION, "the stage reported the operation as essential"
    tests = payload.get("tests")
    checks = tests.get("checks") if isinstance(tests, dict) else None
    if ((callable(mandatory) and mandatory(command))
            or any(isinstance(check, dict) and same_command(check.get("command"), command)
                   and check.get("required") is not False for check in checks or [])):
        return ESSENTIAL_PERMISSION, "the refused command is a mandatory check"
    if record["tool"] != "Bash":
        target = record.get("target")
        if cwd and isinstance(target, str) and Path(target).is_absolute():
            try:
                Path(target).resolve(strict=False).relative_to(Path(cwd).resolve())
            except ValueError:
                return AUXILIARY_DENIAL, "the file is outside the checkout, so it is not a deliverable"
        return UNKNOWN_DENIAL, "the refused edit may be part of the result"
    kind = _shell_kind(command)
    agrees = publication_agrees(publication, None)[0]
    pull = publication.get("pr") if isinstance(publication, dict) else None
    if kind == "read_only":
        return AUXILIARY_DENIAL, "the command only inspects"
    if kind == "interpreter":
        return ((AUXILIARY_DENIAL, "a helper whose result is already published and verified")
                if agrees else (UNKNOWN_DENIAL, "a helper that may have shaped an unpublished result"))
    if kind == "push":
        if agrees:
            return REDUNDANT_OPERATION, "the pushed head is already the remote and pull request head"
        if isinstance(publication, dict) and publication.get("remote_head") != publication.get("local_head"):
            return ESSENTIAL_PERMISSION, "the local commit is not on the remote"
        return UNKNOWN_DENIAL, "the publication could not be verified"
    if kind == "pr_create":
        if agrees:
            return REDUNDANT_OPERATION, "the change request already exists at the published head"
        if isinstance(publication, dict) and pull is None:
            return ESSENTIAL_PERMISSION, "no change request exists for the cycle branch"
        return UNKNOWN_DENIAL, "the publication could not be verified"
    if kind == "pr_edit":
        if not isinstance(publication, dict):
            return UNKNOWN_DENIAL, "the published state could not be observed"
        if pull is None:
            return ESSENTIAL_PERMISSION, "no change request exists for the cycle branch"
        matches = _pull_matches(pull, record.get("intent"))
        if matches is None:
            return UNKNOWN_DENIAL, "the metadata the command meant to change is unknown"
        if matches and agrees:
            return REDUNDANT_OPERATION, "the change request already has the metadata the command set"
        return ESSENTIAL_PERMISSION, ("the change request metadata differs from what the command set"
                                      if not matches else "the publication could not be verified")
    return UNKNOWN_DENIAL, f"the runtime cannot tell whether a {kind} operation was needed"


def classify_denials(records: list[dict[str, Any]], payload: dict[str, Any] | None,
                     **evidence: Any) -> list[dict[str, Any]]:
    for record in records:
        record["class"], record["why"] = classify_denial(record, payload, **evidence)
    return records


def _compact(record: dict[str, Any]) -> dict[str, Any]:
    return {key: (str(record[key])[:300] if key in {"command", "reason", "why"} else record[key])
            for key in ("command", "stage", "reason", "rule", "operation", "class", "why")}


def _reconcile_blocked(payload: dict[str, Any], role: str, completed: frozenset[str],
                       denials: list[dict[str, Any]], publication: dict[str, Any] | None) -> None:
    """Complete a `BLOCKED` stage only when the runtime verified why it may.

    All of it must hold: a completion status valid for the role, only auxiliary
    or redundant denials, no pending publication or environment blocker, no
    failed required check and, for a publishing role, one commit at the remote
    head, the local HEAD and the PR head. Otherwise the stage stays `BLOCKED`
    and the record says which condition failed.
    """
    if "reconciliation" in payload:
        return
    claimed = payload.get("completed_status")
    record: dict[str, Any] = {
        "reconciled": False, "original_status": "BLOCKED", "completed_status": claimed,
        "denials": [_compact(item) for item in denials[:20]],
        "shas": publication_shas(publication, payload), "reason": ""}
    tests = payload.get("tests")
    checks = tests.get("checks") if isinstance(tests, dict) else None
    reason = None
    serious = next((item for item in denials if item["class"] not in RECONCILABLE_DENIALS), None)
    if claimed not in completed:
        reason = f"the block declares no completion status valid for the {role} role"
    elif serious is not None:
        reason = f"{serious['class']} denial: {serious['command']}"
    elif payload.get("publication_pending") is True:
        reason = "the result says its publication is pending"
    elif payload.get("environment_blocker") or payload.get("essential_permission"):
        reason = "the result reports an unresolved blocker"
    elif any(isinstance(check, dict) and check.get("result") == "fail" for check in checks or []):
        reason = "a check failed"
    elif role in PUBLISHING_ROLES:
        agreed, why = publication_agrees(publication, payload)
        if not agreed:
            reason = why
    if reason is None:
        payload["status"] = claimed
        record.update(reconciled=True, reason=(
            "every denial is auxiliary or redundant and the published state is verified"))
    else:
        record["reason"] = reason
    payload["reconciliation"] = record


def reconcile_result(spoken: str, blocker: dict[str, Any], answer: str | None, role: str, *,
                     denials: list[dict[str, Any]] | None = None,
                     publication: dict[str, Any] | None = None,
                     configured_checks: tuple[str, ...] = ()) -> tuple[str, bool]:
    """Use structured evidence to distinguish an essential denial from inspection.

    Completion claims still pass the runtime's normal publication/effect guards.
    A check linked to the refused operation can never retain a pass.

    With `denials`, every refusal of the stage is already classified by the
    runtime (`classify_denials`) and the agent's own `permission_effects` no
    longer decide: a refusal is essential unless it is auxiliary or redundant,
    and a `BLOCKED` stage is completed only when all of them are, the claimed
    completion is valid for the role and, for a publishing role, `publication`
    shows the remote head, the local HEAD and the PR head to be one commit
    (#364). Without `denials` the behaviour of #409 is unchanged.

    `configured_checks` names the refused commands of the stage that are
    configured-check consent questions. They are recorded unverified and never
    keep a stage blocked (#398), whichever refusal is reconciled first.
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
    own = next((item for item in denials if same_command(item.get("command"), blocker["command"])),
               None) if denials is not None else None
    own_class = (own["class"] if own is not None else UNKNOWN_DENIAL) if denials is not None else None
    # The classes that stop a stage whatever it reports. The agent's
    # `essential: false` can never downgrade them.
    hard = own_class in {ESSENTIAL_PERMISSION, ENVIRONMENT_BLOCKER}
    # Only a publishing role has a published state the runtime can verify. Any
    # other role keeps the #409 behaviour: its refusals are recorded, but a
    # `BLOCKED` result is never completed from them (#364).
    classified = denials is not None and role in PUBLISHING_ROLES
    if configured:
        # A configured check the person declines is recorded unverified; it
        # never stops the cycle by itself (#398, #414): the final state names
        # it. Its mandatory-check class therefore does not make it hard.
        essential = False
    elif classified:
        # A stage that reports a completed status is not stopped by a refusal
        # that is not hard; a stage that does not stays stopped until the
        # runtime has verified why it may complete (below).
        essential = hard or payload.get("status") not in completed
    else:
        essential = (effect.get("essential") is not False if effect is not None
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
    elif classified:
        if payload.get("status") == "BLOCKED":
            # A declined configured check is recorded unverified, so it is not
            # one of the denials that keep the stage blocked (#398). Every call
            # leaves all of them out: the first call decides the reconciliation.
            _reconcile_blocked(payload, role, completed, [
                item for item in denials or []
                if not ((configured and item is own)
                        or any(same_command(item.get("command"), check) for check in configured_checks))],
                publication)
            if not configured:
                essential = hard or payload.get("status") not in completed
    elif not essential and payload.get("status") == "BLOCKED":
        # A distinct structured completion claim is required; prose is irrelevant.
        if (payload.get("completed_status") in completed
                and (role != "implement" or (payload.get("head_sha") and
                     (payload.get("change_request_id") or payload.get("pr_number"))))):
            payload["status"] = payload["completed_status"]
    outcomes = payload.get("permission_outcomes")
    if not isinstance(outcomes, list):
        outcomes = []
    outcome = {
        "grant": blocker["resource"], "command": blocker["command"],
        "answer": answer or "pending", "essential": essential, "checks": affected,
        **{key: blocker[key] for key in ("check", "config_sha256", "warnings") if key in blocker}}
    if own is not None:
        outcome.update({key: own[key] for key in ("stage", "rule", "operation", "class")})
    outcomes.append(outcome)
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
    `compose_drift` compares the digest afterwards. The runtime calls it only
    once `docker_host` is granted, the condition for resolving a configuration.
    """
    policy = replace(policy, docker=True)
    context: list[str] = []
    records: list[dict[str, Any]] = []
    for command in declared_compose_checks(policy):
        try:
            op = operation("Bash", {"command": command}, policy, cwd)
        except (ValueError, OSError, TypeError) as error:
            context.append(f"Declared Compose check `{command}` could not be identified ({error}). "
                           "Do not run it; record it unverified with that cause.")
            try:
                check_id = configured_check_id(list(parse_command(command, policy.tmpdir).words), policy)
            except ValueError:
                check_id = None
            records.append({"command": command, "answer": "unidentified", "error": str(error),
                            **({"check": check_id} if check_id else {})})
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
    downgrade the check (#398). The records exist only for a dispatch with
    `docker_host` granted.
    """
    policy = replace(policy, docker=True)
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


def stored_check_denials(policy: Any, executor: str, platform: str, cwd: str | None,
                         reported: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Configured checks this dispatch's stored decisions deny, as outcomes (#398).

    A denied check is often never attempted again: the agent is told it was
    denied and skips it, so no refusal is observed. The runtime still knows:
    the exact identity of each configured command, recomputed now, matches a
    stored `deny`. That, not what the agent reported, keeps it `unverified`.
    """
    if policy is None or not policy.command_permissions:
        return []
    seen = {item.get("check") for item in reported or () if isinstance(item, dict)}
    outcomes = []
    for command in dict.fromkeys(policy.verification_commands):
        try:
            op = operation("Bash", {"command": command}, policy, cwd)
        except (ValueError, OSError, TypeError) as error:
            # Its identity, and so any stored denial of it, cannot be read now;
            # the hook refuses such a run too. It stays unverified, never passed.
            try:
                check_id = configured_check_id(list(parse_command(command, policy.tmpdir).words), policy)
            except ValueError:
                check_id = None
            if check_id and check_id not in seen:
                outcomes.append({"grant": None, "command": command, "answer": "unidentified",
                                 "essential": False, "checks": [], "check": check_id,
                                 "error": str(error), "source": "identity unavailable"})
                seen.add(check_id)
            continue
        if not op.get("check") or op["check"] in seen:
            continue
        if not any(same_operation(entry.get("operation"), op) and entry.get("decision") == "deny"
                   for entry in policy.command_permissions):
            continue
        grant = make_grant(op, executor, platform)
        compose = op.get("compose") if isinstance(op.get("compose"), dict) else None
        outcomes.append({"grant": grant.id, "command": command, "answer": DENY,
                         "essential": False, "checks": [], "check": op["check"],
                         "source": "stored decision",
                         **({"config_sha256": compose["config_sha256"],
                             "warnings": list(compose.get("warnings") or [])} if compose else {})})
        seen.add(op["check"])
    return outcomes


def add_stored_denials(spoken: str, policy: Any, executor: str, platform: str,
                       cwd: str | None) -> str:
    """`spoken` with an outcome for each configured check a stored decision denies."""
    block = _result_block(spoken)
    if block is None:
        return spoken
    reported = block[2].get("permission_outcomes")
    return add_outcomes(spoken, stored_check_denials(
        policy, executor, platform, cwd, reported if isinstance(reported, list) else None))


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
