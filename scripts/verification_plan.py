"""Verification contracts; data only, never a command executor or CI substitute.

Plan persistence and verification-only resume belong to the cycle history driver
(#329). These values share their owning ExecutionPlan's identity and revision.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from typing import Any


TESTS_VERSION = 2
CONCLUSIONS = ("failed", "not_verified", "verified_with_reservations", "verified")
LEVELS = {"static", "test", "boundary"}


class VerificationError(ValueError):
    """Invalid criteria, check identity, or result representation."""


@dataclass(frozen=True)
class Criteria:
    provider: str
    content_sha256: str
    status: str  # evaluable | needs_refinement | not_evaluable
    items: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "items": list(self.items)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Criteria:
        items = value.get("items", ())
        if not isinstance(items, (list, tuple)):
            raise VerificationError("criteria items must be an array")
        result = cls(value["provider"], value["content_sha256"], value["status"],
                     tuple(items), value.get("reason", ""))
        if (result.status not in {"evaluable", "needs_refinement", "not_evaluable"}
                or not isinstance(result.provider, str)
                or not isinstance(result.content_sha256, str)
                or not isinstance(result.reason, str)
                or any(not isinstance(item, str) or not item.strip() for item in result.items)
                or (result.status == "evaluable") != bool(result.items)):
            raise VerificationError("invalid criteria source")
        return result


# Closed grammar: a section consists of one flat list, not arbitrary prose.
_LABEL = r"(?:Acceptance criteria|Criterios de aceptación)"
_HEADING = re.compile(rf"^\s*(?:#{{1,6}}\s+({_LABEL})\s*[:.]?\s*#*|\*\*({_LABEL})[:.]?\*\*[:.]?)\s*$", re.I)
_ITEM = re.compile(r"^(?:[-+*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(\S.*)$")
_SECTION = re.compile(r"^\s*(?:#{1,6}\s+|\*\*[^*]+\*\*[:.]?\s*$)")


def parse_criteria(provider: str, body: str | None) -> Criteria:
    """Provider-independent interface; only GitHub has a body reader today."""
    if provider != "github":
        return Criteria(provider, "", "not_evaluable", reason="criteria reader unavailable")
    if not isinstance(body, str):
        raise VerificationError("GitHub work-item body is unreadable")
    digest = hashlib.sha256(body.encode()).hexdigest()
    lines = body.splitlines()
    starts = [index for index, line in enumerate(lines) if _HEADING.fullmatch(line)]
    if len(starts) != 1:
        return Criteria(provider, digest, "needs_refinement", reason="one acceptance-criteria section required")
    items: list[str] = []
    for line in lines[starts[0] + 1:]:
        if _SECTION.match(line):
            break
        if not line.strip():
            continue
        match = _ITEM.fullmatch(line)
        if not match:
            return Criteria(provider, digest, "needs_refinement", reason="criteria must be a flat Markdown list")
        text = match.group(1).strip()
        if text in {"...", "TODO", "TBD"}:
            return Criteria(provider, digest, "needs_refinement", reason="placeholder criterion")
        items.append(text)
    return Criteria(provider, digest, "evaluable" if items else "needs_refinement",
                    tuple(items), "" if items else "acceptance-criteria list is empty")


def read_criteria(provider: str, repository: str, work_item: str, *,
                  cwd: str | None = None,
                  runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> Criteria:
    if provider != "github":
        return parse_criteria(provider, None)
    try:
        result = runner(["gh", "issue", "view", work_item, "--repo", repository,
                         "--json", "body"], cwd=cwd, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise VerificationError("GitHub criteria read failed")
        return parse_criteria(provider, json.loads(result.stdout)["body"])
    except (OSError, subprocess.TimeoutExpired, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise VerificationError("GitHub criteria read unavailable or invalid") from exc


@dataclass(frozen=True)
class Check:
    id: str
    command: str
    executor: str
    profile: str
    context: str
    required: bool
    criteria: tuple[str, ...] = ()
    level: str = "test"
    provenance: str = "agent"
    reason: str = "proposed verification"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "criteria": list(self.criteria)}

    @classmethod
    def from_dict(cls, item: Mapping[str, Any]) -> Check:
        return cls(item["id"], item["command"], item["executor"], item["profile"],
                   item["context"], item["required"], tuple(item.get("criteria", ())),
                   item.get("level", "test"), item.get("provenance", "agent"),
                   item.get("reason", "proposed verification"))

    def validate(self, criterion_ids: set[str]) -> None:
        if (any(not isinstance(v, str) or not v.strip() for v in (
                self.id, self.command, self.executor, self.profile, self.context,
                self.provenance, self.reason)) or type(self.required) is not bool
                or self.level not in LEVELS or len(set(self.criteria)) != len(self.criteria)
                or not set(self.criteria) <= criterion_ids):
            raise VerificationError("invalid check or criterion link")
        if self.criteria and not self.required:
            raise VerificationError("a criterion-dependent check must be required")


@dataclass(frozen=True)
class CheckResult:
    id: str
    head_sha: str | None
    result: str
    evidence: tuple[Mapping[str, Any], ...] = ()
    cause: str = ""
    attempts: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "evidence": [dict(v) for v in self.evidence],
                "attempts": [dict(v) for v in self.attempts]}

    @classmethod
    def from_dict(cls, item: Mapping[str, Any]) -> CheckResult:
        return cls(item["id"], item["head_sha"], item["result"],
                   tuple(item.get("evidence", ())), item.get("cause", ""),
                   tuple(item.get("attempts", ())))

    def validate(self) -> None:
        if (not isinstance(self.id, str) or not self.id.strip()
                or (self.head_sha is None and self.result != "unverified")
                or (self.head_sha is not None and (not isinstance(self.head_sha, str)
                    or not re.fullmatch(r"[a-fA-F0-9]{40}", self.head_sha)))
                or self.result not in {"pass", "fail", "unverified"}
                or not isinstance(self.cause, str)
                or any(not isinstance(v, Mapping) for v in (*self.evidence, *self.attempts))):
            raise VerificationError("invalid check result")
        if self.result == "unverified":
            if not self.cause.strip():
                raise VerificationError("unverified result requires a cause")
            return
        if not self.evidence:
            raise VerificationError("executed result requires evidence")
        codes = []
        for item in self.evidence:
            code, executed = item.get("exit_code"), item.get("executed")
            if any(key in item and (not isinstance(item[key], str)
                   or re.fullmatch(r"[a-f0-9]{64}", item[key]) is None)
                   for key in ("test_sha256", "context_sha256", "output_sha256")):
                raise VerificationError("evidence fingerprints must be SHA-256 digests")
            if (type(code) is not int or "executed" not in item
                    or (executed is not None and (type(executed) is not int or executed < 0))):
                raise VerificationError("executed evidence requires integer exit_code and count")
            codes.append(code)
        if (self.result == "pass") != all(code == 0 for code in codes):
            raise VerificationError("check result contradicts evidence")


@dataclass(frozen=True)
class VerificationPlan:
    criteria: Criteria
    checks: tuple[Check, ...] = ()
    results: tuple[CheckResult, ...] = ()  # append-only history, including stale HEADs

    @property
    def criterion_ids(self) -> set[str]:
        return {f"AC-{i}" for i in range(1, len(self.criteria.items) + 1)}

    def validate(self) -> None:
        Criteria.from_dict(self.criteria.to_dict())
        ids: set[str] = set()
        for check in self.checks:
            check.validate(self.criterion_ids)
            if check.id in ids:
                raise VerificationError("duplicate check identity")
            ids.add(check.id)
        for result in self.results:
            result.validate()
            if result.id not in ids:
                raise VerificationError("result for unknown check")
            command = next(check.command for check in self.checks if check.id == result.id)
            if any(item.get("command", command) != command for item in result.evidence):
                raise VerificationError("result evidence command differs from check")

    def extend(self, additions: tuple[Check, ...]) -> VerificationPlan:
        """Freeze identity and obligations. Promotion and additions are allowed."""
        checks = {check.id: check for check in self.checks}
        for check in additions:
            old = checks.get(check.id)
            if old is not None:
                if replace(check, required=old.required, criteria=old.criteria) != old:
                    raise VerificationError("cannot rewrite frozen check identity")
                if (old.required and not check.required) or not set(old.criteria) <= set(check.criteria):
                    raise VerificationError("cannot demote a frozen obligation")
            checks[check.id] = check
        plan = replace(self, checks=tuple(checks.values()))
        plan.validate()
        return plan

    def record(self, result: CheckResult) -> VerificationPlan:
        plan = replace(self, results=(*self.results, result))
        plan.validate()
        return plan

    def current(self, check_id: str, head_sha: str) -> str:
        latest = next((result for result in reversed(self.results)
                       if result.id == check_id and result.head_sha == head_sha), None)
        return latest.result if latest is not None else "unverified"

    def cap(self, conclusion: str, head_sha: str) -> str:
        """Deterministic caps never raise the agent's conclusion."""
        if not isinstance(conclusion, str) or conclusion not in CONCLUSIONS:
            raise VerificationError("unknown verification conclusion")
        required = [check for check in self.checks if check.required]
        states = [self.current(check.id, head_sha) for check in required]
        cap = "verified"
        if "fail" in states:
            cap = "failed"
        elif ("unverified" in states or self.criteria.status != "evaluable"
              or any(not any(cid in check.criteria for check in required)
                     for cid in self.criterion_ids)):
            cap = "not_verified"
        return CONCLUSIONS[min(CONCLUSIONS.index(conclusion), CONCLUSIONS.index(cap))]

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {"criteria": self.criteria.to_dict(),
                "checks": [check.to_dict() for check in self.checks],
                "results": [result.to_dict() for result in self.results]}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VerificationPlan:
        try:
            plan = cls(Criteria.from_dict(value["criteria"]),
                       tuple(Check.from_dict(item) for item in value.get("checks", ())),
                       tuple(CheckResult.from_dict(item) for item in value.get("results", ())))
            plan.validate()
            return plan
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise VerificationError(f"invalid verification plan: {exc}") from exc


def configured_check(command: str, executor: str, profile: str) -> Check:
    return Check("configured-test", command, executor, profile, "project", True,
                 provenance="project", reason="configured test command")


def normalize_tests(tests: Mapping[str, Any], head_sha: str, *,
                    plan: VerificationPlan | None = None) -> tuple[dict[str, Any], VerificationPlan | None]:
    """Normalize v2 (or the one-release v1 adapter) against runtime obligations.

    The normalized evidence is only a compatibility view for telemetry consumers;
    explicit check states decide caps. No code-host check can substitute for one.
    """
    version = tests.get("version", 1)
    if type(version) is not int or version not in {1, TESTS_VERSION}:
        raise VerificationError("unsupported tests.version")
    if version == 1 and plan is None:
        return dict(tests), None  # legacy consumers retain their historical contract
    if not re.fullmatch(r"[a-fA-F0-9]{40}", head_sha):
        raise VerificationError("per-check results require the verified HEAD")
    if plan is None:
        # Standalone v2 reports still need a real criterion list to claim coverage.
        plan = VerificationPlan(Criteria.from_dict(tests.get("criteria", {
            "provider": "unknown", "content_sha256": "", "status": "not_evaluable",
            "reason": "runtime criteria unavailable"})))
    entries = tests.get("checks", []) if version == 2 else tests.get("evidence", [])
    if not isinstance(entries, list):
        raise VerificationError("checks/evidence must be an array")
    if version == 1:
        # Only match an exact known command. Missing evidence leaves it unverified.
        converted = []
        known_commands = {check.command for check in plan.checks}
        for check in plan.checks:
            evidence = [v for v in entries if isinstance(v, dict) and v.get("command") == check.command]
            if evidence:
                converted.append({**check.to_dict(), "head_sha": head_sha,
                                  "result": "pass" if all(v.get("exit_code") == 0 for v in evidence) else "fail",
                                  "evidence": evidence})
        # Keep other executed commands too: an unrelated failing legacy entry
        # must not disappear while converting the report to per-check results.
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
                raise VerificationError("invalid legacy evidence")
            command = entry["command"]
            if command in known_commands:
                continue
            spec = Check("legacy-" + hashlib.sha256(command.encode()).hexdigest()[:16],
                         command, "legacy", "legacy", "legacy", False,
                         level=entry.get("level", "test"), provenance="legacy",
                         reason="version 1 evidence adapter")
            plan = plan.extend((spec,))
            converted.append({**spec.to_dict(), "head_sha": head_sha,
                              "result": "pass" if entry.get("exit_code") == 0 else "fail",
                              "evidence": [entry]})
        entries = converted
    seen: set[str] = set()
    evidence_view: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise VerificationError("check must be an object")
        check = Check.from_dict(entry)
        if check.id in seen:
            raise VerificationError("duplicate reported check")
        seen.add(check.id)
        # Runtime criteria links promote agent obligations rather than accepting a demotion.
        if check.criteria:
            check = replace(check, required=True)
        plan = plan.extend((check,))
        history = entry.get("history", [])
        if not isinstance(history, list):
            raise VerificationError("check history must be an array")
        for previous in history:
            prior = CheckResult.from_dict({**previous, "id": check.id})
            if prior not in plan.results:
                plan = plan.record(prior)
        result = CheckResult(check.id, entry["head_sha"], entry["result"],
                             tuple(entry.get("evidence", ())), entry.get("cause", ""),
                             tuple(entry.get("attempts", ())))
        for item in result.evidence:
            if item.get("command", check.command) != check.command:
                raise VerificationError("evidence command does not match frozen check")
        plan = plan.record(result)
        if result.head_sha == head_sha and result.result != "unverified":
            evidence_view.extend({**item, "command": check.command, "level": check.level}
                                 for item in result.evidence)
    for check in plan.checks:
        if not any(result.id == check.id for result in plan.results):
            plan = plan.record(CheckResult(check.id, head_sha, "unverified",
                                           cause="check has no execution result"))
    conclusion = plan.cap(tests.get("conclusion", "not_verified"), head_sha)
    normalized = {**tests, "version": 2, "conclusion": conclusion,
                  "passed": conclusion in {"verified", "verified_with_reservations"},
                  "evidence": evidence_view,
                  "criteria": plan.criteria.to_dict(),
                  "checks": [{**check.to_dict(), **next(
                      result.to_dict() for result in reversed(plan.results) if result.id == check.id)}
                      for check in plan.checks]}
    # An unexecuted required check still records not_verified; ran:false is not
    # allowed to suppress deterministic caps in telemetry.
    if conclusion in {"not_verified", "failed"}:
        normalized.pop("ran", None)
    return normalized, plan
