"""Whether a work item is ready to implement, read from a structured result.

The implementer already diagnoses before editing: it reproduces a defect,
searches related work, and chooses a fix shape. That is diagnosis of the code,
done by the stage that is about to change it. It is not an independent gate on
the specification, and it has no way to say "a person has to decide this
first" before anything is built. `cc-issue-review` is that gate, and this module
is the part of it the runtime trusts: the closed result shape, and the rule
that turns a result into continue, escalate or stop.

Three decisions are worth stating.

**Only a confirmed `READY` continues.** `NEEDS_REFINEMENT`, `BLOCKED`, a
missing block and a malformed one all stop before `implement`. A result the
runtime cannot read is not a result that said "go ahead".

**Confidence is evidence quality, not a probability.** It is a closed category
the agent assigns from what it could check. A `READY` with `low` confidence, or
with a material uncertainty left unresolved, is not refused outright and not
waved through either: it is escalated once to the stronger profile, and stops
for a person when that one cannot confirm it.

**Issue findings are not review findings.** Their identifiers are `IR-NNN`,
never `REV-xxx`, and they carry no code-change `status` and no resolver
`disposition`. Those belong to the change-request contract, and a result that
mixes them in is refused rather than half-read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

#: The skill this stage runs, and the role it is recorded under.
SKILL = "cc-issue-review"
ROLE = "issue_review"

#: The readiness outcomes. Only `READY` may continue the cycle.
STATUSES = ("READY", "NEEDS_REFINEMENT", "BLOCKED")
CONTINUING_STATUS = "READY"

#: Categorical confidence in the evidence behind the outcome.
CONFIDENCES = ("high", "medium", "low")

#: What an issue review may evaluate. A review names only the relevant ones.
DIMENSIONS = (
    "applicability", "existing_work", "overlap_dependency",
    "scope_architecture", "acceptance_verification", "compatibility",
    "security", "data", "operations",
)

#: The shared severity vocabulary; its meaning fits an issue finding as well.
SEVERITIES = ("critical", "high", "medium", "low")

#: Where a finding's evidence lives.
EVIDENCE_KINDS = ("repository", "work_item", "change_request", "commit")

FINDING_ID = re.compile(r"IR-[0-9]{3}")
UNCERTAINTY_ID = re.compile(r"IU-[0-9]{3}")

#: The most entries a list may hold before the result is treated as malformed.
MAX_ENTRIES = 100
MAX_REFERENCE_LENGTH = 200

RESULT_KEYS = frozenset({
    "skill", "status", "issue_provider", "issue_id", "issue_number",
    "code_host", "repo", "confidence", "dimensions", "findings",
    "uncertainties", "summary", "error", "blocking",
})
REQUIRED_RESULT_KEYS = frozenset({
    "skill", "status", "issue_id", "confidence", "dimensions", "findings",
    "uncertainties",
})
FINDING_KEYS = frozenset({
    "id", "dimension", "severity", "blocks_readiness", "evidence", "summary",
    "proposed_change",
})
REQUIRED_FINDING_KEYS = FINDING_KEYS - {"proposed_change"}
EVIDENCE_KEYS = frozenset({"kind", "ref"})
UNCERTAINTY_KEYS = frozenset({"id", "material", "resolved", "summary"})


class IssueReviewMode(str, Enum):
    """Whether a new cycle reviews its work item before implementing it."""

    AUTO = "auto"
    OFF = "off"


class IssueReviewConfigError(ValueError):
    """`code_cycle.issue_review` says something this version cannot honour."""


def load_issue_review_mode(config: dict | None = None) -> IssueReviewMode:
    """Read `code_cycle.issue_review.mode`, defaulting to `auto`."""
    section = (config or {}).get("code_cycle")
    declared = section.get("issue_review") if isinstance(section, dict) else None
    if declared is None:
        return IssueReviewMode.AUTO
    if not isinstance(declared, dict):
        raise IssueReviewConfigError(
            "code_cycle.issue_review must be a mapping, not "
            f"{type(declared).__name__}")
    unknown = sorted(str(key) for key in set(declared) - {"mode"})
    if unknown:
        raise IssueReviewConfigError(
            "unknown key under code_cycle.issue_review: " + ", ".join(unknown)
            + "; the known key is mode")
    mode = declared.get("mode", IssueReviewMode.AUTO.value)
    try:
        return IssueReviewMode(mode)
    except (TypeError, ValueError) as exc:
        raise IssueReviewConfigError(
            f"unknown code_cycle.issue_review.mode value: {mode!r} "
            "(expected 'auto' or 'off')") from exc


def dispatch_decision(mode: IssueReviewMode, signals) -> str:
    """`off`, `skipped` or `dispatched`, from the mode and the declared signals.

    Only a task declared trivial — difficulty 1 — and not security-sensitive is
    skipped. The router's default difficulty is 2, so a task nobody classified
    is reviewed: unknown risk is never read as no risk.
    """
    if IssueReviewMode(mode) is IssueReviewMode.OFF:
        return "off"
    if signals.difficulty == 1 and not signals.security_sensitive:
        return "skipped"
    return "dispatched"


def result_errors(payload: object) -> list[str]:
    """What is wrong with one issue-review result, as the skill defines it.

    Empty means the result has the closed shape. Every problem is named, so the
    validator and a test can say which rule a result broke.
    """
    if not isinstance(payload, dict):
        return ["the result is not an object"]
    problems: list[str] = []
    missing = sorted(REQUIRED_RESULT_KEYS - set(payload))
    if missing:
        problems.append(f"missing keys: {', '.join(missing)}")
    extra = sorted(str(key) for key in set(payload) - RESULT_KEYS)
    if extra:
        problems.append(f"keys outside the issue-review contract: {', '.join(extra)}")
    if payload.get("skill") != SKILL:
        problems.append(f"`skill` must be {SKILL}")
    if not _text(payload.get("issue_id")) or len(payload["issue_id"]) > MAX_REFERENCE_LENGTH:
        problems.append("`issue_id` must name the work item that was reviewed")
    status = payload.get("status")
    if status not in STATUSES:
        problems.append(f"`status` must be one of {', '.join(STATUSES)}")
    if payload.get("confidence") not in CONFIDENCES:
        problems.append(f"`confidence` must be one of {', '.join(CONFIDENCES)}")

    dimensions = payload.get("dimensions")
    if (not isinstance(dimensions, list) or len(dimensions) > len(DIMENSIONS)
            or any(not isinstance(item, str) or item not in DIMENSIONS
                   for item in dimensions)
            or len(set(dimensions)) != len(dimensions)):
        problems.append("`dimensions` must be a list of distinct dimension tokens")

    findings = payload.get("findings")
    if not isinstance(findings, list) or len(findings) > MAX_ENTRIES:
        problems.append("`findings` must be a list")
        findings = []
    seen: set[str] = set()
    for index, finding in enumerate(findings):
        problems.extend(_finding_errors(index, finding, seen))

    uncertainties = payload.get("uncertainties")
    if not isinstance(uncertainties, list) or len(uncertainties) > MAX_ENTRIES:
        problems.append("`uncertainties` must be a list")
        uncertainties = []
    seen = set()
    for index, item in enumerate(uncertainties):
        problems.extend(_uncertainty_errors(index, item, seen))

    if problems:
        return problems
    blocking = sum(1 for item in findings if item["blocks_readiness"])
    open_material = sum(1 for item in uncertainties
                        if item["material"] and not item["resolved"])
    if status == "READY" and blocking:
        problems.append("a READY result cannot carry a finding that blocks readiness")
    if status == "NEEDS_REFINEMENT" and not (blocking or open_material):
        problems.append(
            "a NEEDS_REFINEMENT result names at least one finding that blocks "
            "readiness or one unresolved material uncertainty")
    return problems


def _text(value: object, *, required: bool = True) -> bool:
    if value is None:
        return not required
    return isinstance(value, str) and bool(value.strip())


def _finding_errors(index: int, finding: object, seen: set[str]) -> list[str]:
    where = f"`findings[{index}]`"
    if not isinstance(finding, dict):
        return [f"{where} must be an object"]
    problems = []
    missing = sorted(REQUIRED_FINDING_KEYS - set(finding))
    if missing:
        problems.append(f"{where} is missing {', '.join(missing)}")
    extra = sorted(str(key) for key in set(finding) - FINDING_KEYS)
    if extra:
        # `status` and `disposition` land here: they are the change-request
        # contract's, and an issue finding has neither.
        problems.append(f"{where} carries keys outside the contract: {', '.join(extra)}")
    identifier = finding.get("id")
    if not isinstance(identifier, str) or FINDING_ID.fullmatch(identifier) is None:
        problems.append(f"{where}.id must be an IR-NNN identifier")
    elif identifier in seen:
        problems.append(f"{where}.id duplicates {identifier}")
    else:
        seen.add(identifier)
    if finding.get("dimension") not in DIMENSIONS:
        problems.append(f"{where}.dimension is not a known dimension")
    if finding.get("severity") not in SEVERITIES:
        problems.append(f"{where}.severity is not a known severity")
    if not isinstance(finding.get("blocks_readiness"), bool):
        problems.append(f"{where}.blocks_readiness must be true or false")
    if not _text(finding.get("summary")):
        problems.append(f"{where}.summary must be text")
    if not _text(finding.get("proposed_change"), required=False):
        problems.append(f"{where}.proposed_change must be text when present")
    evidence = finding.get("evidence")
    if not isinstance(evidence, list) or not evidence or len(evidence) > MAX_ENTRIES:
        problems.append(f"{where}.evidence must be a non-empty list")
        return problems
    for position, item in enumerate(evidence):
        if (not isinstance(item, dict) or set(item) != EVIDENCE_KEYS
                or item.get("kind") not in EVIDENCE_KINDS
                or not _text(item.get("ref"))
                or len(item["ref"]) > MAX_REFERENCE_LENGTH):
            problems.append(
                f"{where}.evidence[{position}] must be a kind from "
                f"{', '.join(EVIDENCE_KINDS)} and a non-empty ref")
    return problems


def _uncertainty_errors(index: int, item: object, seen: set[str]) -> list[str]:
    where = f"`uncertainties[{index}]`"
    if not isinstance(item, dict):
        return [f"{where} must be an object"]
    problems = []
    if set(item) != UNCERTAINTY_KEYS:
        problems.append(
            f"{where} keys {sorted(map(str, item))} differ from {sorted(UNCERTAINTY_KEYS)}")
    identifier = item.get("id")
    if not isinstance(identifier, str) or UNCERTAINTY_ID.fullmatch(identifier) is None:
        problems.append(f"{where}.id must be an IU-NNN identifier")
    elif identifier in seen:
        problems.append(f"{where}.id duplicates {identifier}")
    else:
        seen.add(identifier)
    for key in ("material", "resolved"):
        if not isinstance(item.get(key), bool):
            problems.append(f"{where}.{key} must be true or false")
    if not _text(item.get("summary")):
        problems.append(f"{where}.summary must be text")
    return problems


#: What the runtime does after an issue review.
CONTINUE, ESCALATE, STOP = "continue", "escalate", "stop"


@dataclass(frozen=True)
class Readiness:
    """One issue-review result, judged. `gate` is the only thing that decides."""

    gate: str
    status: str | None
    valid: bool
    confidence: str | None = None
    findings_total: int | None = None
    findings_blocking: int | None = None
    uncertainties_material: int | None = None
    problems: tuple[str, ...] = ()
    findings: tuple[dict, ...] = field(default_factory=tuple)
    uncertainties: tuple[dict, ...] = field(default_factory=tuple)

    def telemetry_fields(self) -> dict:
        """Counts and tokens only: the prose stays in the report."""
        fields = {"readiness_result_valid": self.valid}
        if self.valid:
            fields.update(
                readiness_confidence=self.confidence,
                readiness_findings_total=self.findings_total,
                readiness_findings_blocking=self.findings_blocking,
                readiness_uncertainties_material=self.uncertainties_material,
            )
        return fields

    def explain(self) -> str:
        """Why the gate is what it is, in one line."""
        if not self.valid:
            return ("the issue review reported a result outside its contract: "
                    + "; ".join(self.problems[:3]))
        if self.gate == CONTINUE:
            return "the issue review confirmed the work item is READY"
        if self.gate == ESCALATE:
            reasons = []
            if self.confidence == "low":
                reasons.append("low confidence")
            count = self.uncertainties_material
            if count:
                reasons.append(f"{count} unresolved material "
                               f"uncertaint{'y' if count == 1 else 'ies'}")
            return ("the issue review reported READY with "
                    + " and ".join(reasons) + ", which does not confirm readiness")
        return f"the issue review reported {self.status}"


def _reference(value: str) -> str:
    return value.strip().lstrip("#").casefold()


def names_work_item(reported: str, requested: str) -> bool:
    """Whether a result's `issue_id` names the work item the runtime asked about.

    Case, surrounding space and a leading `#` do not matter. A work item given
    as a provider URL matches the identifier in its last path segment, which is
    where GitHub, Plane and Jira put it. Nothing else is inferred: an identifier
    the runtime cannot tie to the request is a result about another item.
    """
    reported, requested = _reference(reported), _reference(requested)
    if not reported:
        return False
    if reported == requested:
        return True
    if "://" in requested:
        return requested.rstrip("/").rsplit("/", 1)[-1] == reported
    return False


def assess(payload: dict | None, work_item: str | None = None) -> Readiness:
    """Judge one result. Anything but a confirmed READY does not continue.

    With `work_item`, the result must also name that work item: a READY about
    another item says nothing about this one.
    """
    if payload is None:
        return Readiness(STOP, None, False,
                         problems=("no readable structured result",))
    problems = result_errors(payload)
    if (not problems and work_item is not None
            and not names_work_item(payload["issue_id"], work_item)):
        # The reported value is not echoed: it is agent output, and this text
        # reaches the operator's terminal.
        problems.append("`issue_id` does not name the requested work item")
    status = payload.get("status") if isinstance(payload.get("status"), str) else None
    if problems:
        return Readiness(STOP, status, False, problems=tuple(problems))
    findings = tuple(payload["findings"])
    uncertainties = tuple(payload["uncertainties"])
    open_material = sum(1 for item in uncertainties
                        if item["material"] and not item["resolved"])
    confidence = payload["confidence"]
    if status != CONTINUING_STATUS:
        gate = STOP
    elif confidence == "low" or open_material:
        gate = ESCALATE
    else:
        gate = CONTINUE
    return Readiness(
        gate, status, True,
        confidence=confidence,
        findings_total=len(findings),
        findings_blocking=sum(1 for item in findings if item["blocks_readiness"]),
        uncertainties_material=open_material,
        findings=findings,
        uncertainties=uncertainties,
    )
