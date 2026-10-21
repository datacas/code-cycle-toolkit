"""Strict proposal construction for runtime-assisted triage-line repair."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from review_contract import (  # pyright: ignore[reportImplicitRelativeImport]
    ContractError,
    RunLineContractError,
    parse_run_line,
    stage_evidence,
    validate_triage_comment,
)

_HEADER = re.compile(r"^(#{4})\s+\[([^]]+)\](.*)$")
_SEPARATOR = " · "
_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class RepairProposal:
    comment_id: str
    original: str
    repaired: str
    run_line: str
    repaired_line: str
    sha: str | None
    evidence: str
    digest: str


def _replace_line(body: str, old: str, new: str) -> str:
    lines = body.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == old]
    if len(matches) != 1:
        raise ContractError("repair requires one uniquely located malformed run line")
    index = matches[0]
    ending = "\r\n" if lines[index].endswith("\r\n") else "\n" if lines[index].endswith("\n") else ""
    lines[index] = new + ending
    return "".join(lines)


def _repair_line(line: str, start_sha: str | None) -> tuple[str, str | None, str] | None:
    match = _HEADER.fullmatch(line.strip())
    if match is None or not match.group(2).startswith("CCT-"):
        return None
    try:
        _ = parse_run_line(line)
    except RunLineContractError as error:
        if error.kind not in {"missing_triage_anchor", "unknown_token"}:
            return None
        original_error = error
    except ContractError:
        return None
    else:
        return None

    original_line = line.strip()
    tail = match.group(3)
    removed_extra = False
    if tail.startswith(_SEPARATOR + "resolve" + _SEPARATOR):
        if original_error.kind != "unknown_token":
            return None
        candidate = re.sub(
            r"^(####\s+\[[^]]+\]) · resolve · ", r"\1 · ", original_line, count=1)
        removed_extra = True
    elif original_error.kind == "unknown_token":
        return None
    else:
        candidate = original_line

    try:
        parsed = parse_run_line(candidate)
    except RunLineContractError as error:
        if error.kind != "missing_triage_anchor" or start_sha is None:
            return None
        if _SHA.fullmatch(start_sha) is None:
            return None
        # Insert only the missing anchor, preserving the rest of the approved line.
        schema = list(re.finditer(r" · schema:[0-9]+$", candidate))
        if len(schema) != 1:
            return None
        position = schema[0].start()
        anchor_candidate = (candidate[:position] + _SEPARATOR
                            + f"triaged:{start_sha}" + candidate[position:])
        try:
            parsed = parse_run_line(anchor_candidate)
        except ContractError:
            return None
        if parsed is None or parsed.kind != "triage":
            return None
        return anchor_candidate, start_sha, "recorded resolve start head"
    except ContractError:
        return None
    if (not removed_extra or parsed is None or parsed.kind != "triage"):
        return None
    return candidate, None, "unique extra leading resolve token"


def propose(repo: str, comment_id: str, body: str) -> RepairProposal | None:
    """Return one proof-bound minimal repair, or None when the case is ambiguous."""
    return propose_with_start(repo, comment_id, body, None)


def propose_with_start(repo: str, comment_id: str, body: str, start_sha: str | None) -> RepairProposal | None:
    """Build a proposal after resolving the exact attempt's recorded start head."""
    malformed: list[str] = []
    for line in body.splitlines():
        try:
            _ = parse_run_line(line)
        except ContractError:
            if _HEADER.match(line.strip()):
                malformed.append(line)
    if len(malformed) != 1:
        return None
    try:
        receipts: list[dict[str, object]] = stage_evidence(body)
    except ContractError:
        return None
    if len(receipts) != 1 or receipts[0].get("stage") != "resolve" or receipts[0].get("repo") != repo:
        return None
    candidate = _repair_line(malformed[0], start_sha)
    if candidate is None:
        return None
    repaired_line, sha, evidence = candidate
    repaired = _replace_line(body, malformed[0], repaired_line)
    payload = {"repo": repo, "comment_id": comment_id, "original": body,
               "repaired": repaired, "sha": sha, "evidence": evidence}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")).hexdigest()
    return RepairProposal(comment_id, body, repaired, malformed[0], repaired_line,
                          sha, evidence, digest)


def execute_approved(proposal: RepairProposal, approval: str,
                     read_comments: Callable[[], Iterable[object]],
                     edit_comment: Callable[[str, str], None]) -> tuple[str, str]:
    """Apply one content-bound approval and verify the remote comment afterward."""
    if approval != f"{proposal.comment_id}:{proposal.digest}":
        return "approval_rejected", "failed"

    def matching_body() -> str | None:
        matches = [item for item in read_comments()
                   if str(getattr(item, "id", "")) == proposal.comment_id]
        if len(matches) != 1:
            return None
        return getattr(matches[0], "body", None)

    current = matching_body()
    if current != proposal.original:
        return "approved", "failed"

    try:
        edit_comment(proposal.comment_id, proposal.repaired)
    except Exception:
        # An HTTP failure may have happened after the server committed the edit.
        # Read once to classify the outcome, but never retry the mutation.
        pass
    try:
        updated = matching_body()
        if updated is None or updated != proposal.repaired:
            return "approved", "failed"
        validate_triage_comment(updated)
    except Exception:
        return "approved", "unverified"
    return "approved", "applied"
