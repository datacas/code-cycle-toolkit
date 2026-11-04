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
# A manual check names evidence a person or a host supplies. It is an identity,
# not a command: nothing may execute it, and restoring it never verifies it.
MANUAL_COMMAND = re.compile(r"manual:[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def is_manual_command(command: Any) -> bool:
    """Whether a check command is the fixed `manual:<name>` grammar."""
    return isinstance(command, str) and MANUAL_COMMAND.fullmatch(command) is not None


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
            raise _invalid_field("criteria.items", items, "an array of criterion strings")
        result = cls(_required(value, "provider", "text"),
                     _required(value, "content_sha256", "a SHA-256 digest or empty text"),
                     _required(value, "status", "evaluable, needs_refinement, or not_evaluable"),
                     tuple(items), value.get("reason", ""))
        if not isinstance(result.provider, str) or not result.provider.strip():
            raise _invalid_field("criteria.provider", result.provider, "non-empty text")
        if not isinstance(result.content_sha256, str) or (result.content_sha256 and
                re.fullmatch(r"[a-fA-F0-9]{64}", result.content_sha256) is None):
            raise _invalid_field("criteria.content_sha256", result.content_sha256,
                                 "empty text or a 64-character hexadecimal SHA-256 digest")
        if not isinstance(result.status, str) or result.status not in {
                "evaluable", "needs_refinement", "not_evaluable"}:
            raise _invalid_field("criteria.status", result.status,
                                 "evaluable, needs_refinement, or not_evaluable")
        if not isinstance(result.reason, str):
            raise _invalid_field("criteria.reason", result.reason, "text")
        if any(not isinstance(item, str) or not item.strip() for item in result.items):
            raise _invalid_field("criteria.items", result.items, "an array of non-empty strings")
        if (result.status == "evaluable") != bool(result.items):
            raise _invalid_field("criteria.items", result.items,
                                 "a non-empty array only for evaluable criteria")
        return result


# Closed grammar: a section consists of one flat list, not arbitrary prose.
_LABEL = r"(?:Acceptance criteria|Criterios de aceptación)"
_HEADING = re.compile(rf"^\s*(?:#{{1,6}}\s+({_LABEL})\s*[:.]?\s*#*|\*\*({_LABEL})[:.]?\*\*[:.]?)\s*$", re.I)
_ITEM = re.compile(r"^(?:[-+*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(\S.*)$")
_SECTION = re.compile(r"^\s*(?:#{1,6}\s+|\*\*[^*]+\*\*[:.]?\s*$|\*\*[^*]+[.:]\*\*[:.]?\s+\S.*$)")


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
        expected = {"id": "a non-empty string", "command": "text", "executor": "text",
                    "profile": "text", "context": "text", "required": "a boolean"}
        values = {key: _required(item, key, format_) for key, format_ in expected.items()}
        criteria = item.get("criteria", ())
        if not isinstance(criteria, (list, tuple)):
            raise _invalid_field("criteria", criteria, "an array of criterion IDs")
        return cls(values["id"], values["command"], values["executor"], values["profile"],
                   values["context"], values["required"], tuple(criteria),
                   item.get("level", "test"), item.get("provenance", "agent"),
                   item.get("reason", "proposed verification"))

    def validate(self, criterion_ids: set[str]) -> None:
        for field in ("id", "command", "executor", "profile", "context", "provenance", "reason"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise _invalid_field(f"check.{field}", value, "non-empty text")
        if type(self.required) is not bool:
            raise _invalid_field("check.required", self.required, "a boolean")
        if not isinstance(self.level, str) or self.level not in LEVELS:
            raise _invalid_field("check.level", self.level, "static, test, or boundary")
        if any(not isinstance(value, str) or not value.strip() for value in self.criteria):
            raise _invalid_field("check.criteria", self.criteria, "an array of non-empty criterion IDs")
        if len(set(self.criteria)) != len(self.criteria):
            raise _invalid_field("check.criteria", self.criteria, "unique criterion IDs")
        if not set(self.criteria) <= criterion_ids:
            raise _invalid_field("check.criteria", self.criteria, "IDs present in the criteria list")
        if self.criteria and not self.required:
            raise _invalid_field("check.required", self.required, "true when criteria are linked")


#: What a check executes, where and by whom. Changing any of them is another
#: check: it needs another ID and never inherits this one's evidence.
IDENTITY_FIELDS = ("command", "executor", "profile", "context", "level")
_FINGERPRINT = re.compile(r"sha256:[a-f0-9]{64}")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,64}")


def persisted_token(value: str) -> str:
    """How the history store keeps an ID, executor, profile or context value."""
    return (value if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value)
            else "ref-" + hashlib.sha256(value.encode()).hexdigest())


def identity_conflict(frozen: Check, proposed: Check) -> tuple[str, ...]:
    """The identity fields in which two checks with one ID differ.

    A frozen executor, profile or context restored from history may be the
    store's reference for the proposed value; that is the same identity. A
    command is never matched by its fingerprint here: only today's
    authorization may bind one (`cycle_history.restore_plan`).
    """
    def same(field: str, old: Any, new: Any) -> bool:
        return old == new or (field in {"executor", "profile", "context"}
                              and isinstance(new, str) and old == persisted_token(new))
    return tuple(field for field in IDENTITY_FIELDS
                 if not same(field, getattr(frozen, field), getattr(proposed, field)))


def safe_value(field: str, value: Any) -> str:
    """A diagnostic form that never reveals a command, path or free text.

    A command is shown as a short fingerprint, marked `unbound` while it is
    still the persisted fingerprint, so an operator can tell a missing binding
    from a different command without the command itself.
    """
    if field == "command":
        if isinstance(value, str) and _FINGERPRINT.fullmatch(value):
            return "unbound " + value[:19]
        return "sha256:" + hashlib.sha256(str(value).encode()).hexdigest()[:12]
    if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value):
        return value
    return "ref-" + hashlib.sha256(repr(value).encode()).hexdigest()[:12]


def _received(value: Any) -> str:
    """Describe a rejected value without echoing free-form agent content."""
    if value is _MISSING:
        return "missing"
    return f"{type(value).__name__}"


_MISSING = object()


def _invalid_field(field: str, value: Any, expected: str) -> VerificationError:
    return VerificationError(f"{field}: received {_received(value)}; expected {expected}")


def _required(item: Mapping[str, Any], key: str, expected: str) -> Any:
    value = item.get(key, _MISSING)
    if value is _MISSING:
        raise _invalid_field(key, value, expected)
    return value


def _as_items(value: Any, field: str, *, singleton: bool = False) -> tuple[Any, ...]:
    if value is None:
        return ()
    if singleton and isinstance(value, Mapping):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(value)
    raise _invalid_field(field, value, "an array of objects" + (" or one object" if singleton else ""))


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
        evidence = _as_items(item.get("evidence"), "evidence", singleton=True)
        attempts = _as_items(item.get("attempts"), "attempts")
        cause = item.get("cause")
        if cause is None:
            cause = ""
        return cls(_required(item, "id", "a non-empty string"),
                   _required(item, "head_sha", "a full SHA or null for unverified results"),
                   _required(item, "result", "pass, fail, or unverified"),
                   evidence, cause, attempts)

    def validate(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise _invalid_field("id", self.id, "a non-empty string")
        if self.head_sha is None and self.result != "unverified":
            raise _invalid_field("head_sha", self.head_sha, "a full SHA for pass/fail results")
        if self.head_sha is not None and (not isinstance(self.head_sha, str)
                or not re.fullmatch(r"[a-fA-F0-9]{40}", self.head_sha)):
            raise _invalid_field("head_sha", self.head_sha, "a 40-character hexadecimal SHA")
        if not isinstance(self.result, str) or self.result not in {"pass", "fail", "unverified"}:
            raise _invalid_field("result", self.result, "pass, fail, or unverified")
        if not isinstance(self.cause, str):
            raise _invalid_field("cause", self.cause, "omitted or text")
        if any(not isinstance(v, Mapping) for v in self.evidence):
            raise _invalid_field("evidence", self.evidence, "an array of objects")
        if any(not isinstance(v, Mapping) for v in self.attempts):
            raise _invalid_field("attempts", self.attempts, "an array of objects")
        if self.result == "unverified":
            if not self.cause.strip():
                raise _invalid_field("cause", self.cause, "non-empty text for unverified results")
            return
        if not self.evidence:
            raise _invalid_field("evidence", self.evidence, "at least one evidence object for pass/fail results")
        codes = []
        for item in self.evidence:
            code, executed = item.get("exit_code"), item.get("executed")
            if any(key in item and (not isinstance(item[key], str)
                   or re.fullmatch(r"[a-f0-9]{64}", item[key]) is None)
                   for key in ("test_sha256", "context_sha256", "output_sha256")):
                raise _invalid_field("evidence fingerprint", item, "a 64-character hexadecimal SHA-256 digest")
            if (type(code) is not int or "executed" not in item
                    or (executed is not None and (type(executed) is not int or executed < 0))):
                field = "executed" if "executed" not in item or (executed is not None and type(executed) is not int) else "exit_code"
                value = item.get(field, _MISSING)
                raise _invalid_field(f"evidence.{field}", value, "an integer" + (" or null" if field == "executed" else ""))
            codes.append(code)
        if (self.result == "pass") != all(code == 0 for code in codes):
            raise _invalid_field("result", self.result, "pass when all exit_code values are 0, otherwise fail")


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
                raise _invalid_field("check.id", check.id, "a unique check ID")
            ids.add(check.id)
        for result in self.results:
            result.validate()
            if result.id not in ids:
                raise _invalid_field("result.id", result.id, "an ID in the frozen checks")
            command = next(check.command for check in self.checks if check.id == result.id)
            for item in result.evidence:
                if item.get("command", command) != command:
                    raise _invalid_field("result.evidence.command", item.get("command", _MISSING),
                                         "the frozen check command " + safe_value("command", command))

    def extend(self, additions: tuple[Check, ...]) -> VerificationPlan:
        """Freeze identity and obligations. Promotion and additions are allowed.

        The execution identity is `IDENTITY_FIELDS`. Provenance and reason are
        descriptive: persistence normalizes them, so the frozen values are kept
        and a different description of the same check is not a conflict.
        """
        checks = {check.id: check for check in self.checks}
        for check in additions:
            old = checks.get(check.id)
            if old is not None:
                differing = identity_conflict(old, check)
                if differing:
                    raise VerificationError(
                        f"cannot rewrite frozen check identity: check {safe_value('id', old.id)} "
                        "differs in " + ", ".join(
                            f"{field} (frozen {safe_value(field, getattr(old, field))}, "
                            f"proposed {safe_value(field, getattr(check, field))})"
                            for field in differing))
                demoted = [name for name, lost in (
                    ("required", old.required and not check.required),
                    ("criteria", not set(old.criteria) <= set(check.criteria))) if lost]
                if demoted:
                    raise VerificationError(
                        f"cannot demote a frozen obligation: check {safe_value('id', old.id)} "
                        "would lose " + ", ".join(demoted))
                check = replace(old, required=check.required, criteria=check.criteria)
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
            raise _invalid_field("tests.conclusion", conclusion,
                                 "failed, not_verified, verified_with_reservations, or verified")
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
        raise _invalid_field("tests.version", version, "integer 1 or 2")
    if version == 1 and plan is None:
        return dict(tests), None  # legacy consumers retain their historical contract
    if not re.fullmatch(r"[a-fA-F0-9]{40}", head_sha):
        raise _invalid_field("head_sha", head_sha, "the verified 40-character hexadecimal HEAD SHA")
    if plan is None:
        # Standalone v2 reports still need a real criterion list to claim coverage.
        plan = VerificationPlan(Criteria.from_dict(tests.get("criteria", {
            "provider": "unknown", "content_sha256": "", "status": "not_evaluable",
            "reason": "runtime criteria unavailable"})))
    entries = tests.get("checks", []) if version == 2 else tests.get("evidence", [])
    if not isinstance(entries, list):
        field = "checks" if version == 2 else "evidence"
        raise _invalid_field(f"tests.{field}", entries, "an array of check objects")
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
                raise _invalid_field("evidence.command", entry.get("command", _MISSING)
                                     if isinstance(entry, dict) else entry,
                                     "text in each legacy evidence object")
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
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise _invalid_field(f"checks[{index}]", entry, "a check object")
        try:
            check = Check.from_dict(entry)
        except (VerificationError, KeyError, TypeError, ValueError) as error:
            detail = str(error) or type(error).__name__
            raise VerificationError(f"checks[{index}] {detail}") from error
        if check.id in seen:
            raise _invalid_field(f"checks[{index}].id", check.id, "a unique reported check ID")
        seen.add(check.id)
        # Runtime criteria links promote agent obligations rather than accepting a demotion.
        if check.criteria:
            check = replace(check, required=True)
        check.validate(plan.criterion_ids)
        plan = plan.extend((check,))
        history = entry.get("history", [])
        if not isinstance(history, list):
            raise _invalid_field(f"checks[{index}].history", history, "an array of prior check results")
        for history_index, previous in enumerate(history):
            if not isinstance(previous, Mapping):
                raise _invalid_field(f"checks[{index}].history[{history_index}]", previous,
                                     "a prior check result object")
            prior = CheckResult.from_dict({**previous, "id": check.id})
            if prior not in plan.results:
                plan = plan.record(prior)
        for field in ("evidence", "attempts"):
            values = _as_items(entry.get(field), f"checks[{index}].{field}", singleton=field == "evidence")
            for position, item in enumerate(values):
                if not isinstance(item, Mapping):
                    raise _invalid_field(f"checks[{check.id}].{field}[{position}]", item, "an object")
        evidence = _as_items(entry.get("evidence"), f"checks[{check.id}].evidence", singleton=True)
        attempts = _as_items(entry.get("attempts"), f"checks[{check.id}].attempts")
        cause = entry.get("cause")
        if cause is None:
            cause = ""
        result = CheckResult(check.id, entry.get("head_sha"), entry.get("result", ""),
                             evidence, cause, attempts)
        if result.result == "unverified" and not cause.strip():
            raise _invalid_field(f"checks[{check.id}].cause", entry.get("cause", _MISSING),
                                 "non-empty text for unverified results")
        if result.result in {"pass", "fail"} and not evidence:
            raise _invalid_field(f"checks[{check.id}].evidence",
                                 entry.get("evidence", _MISSING),
                                 "at least one evidence object for pass/fail results")
        try:
            result.validate()
        except VerificationError as error:
            raise VerificationError(f"check {check.id} {error}") from error
        for item in result.evidence:
            if item.get("command", check.command) != check.command:
                raise _invalid_field(f"checks[{check.id}].evidence.command",
                                     item.get("command", _MISSING),
                                     "the frozen check command " + safe_value("command", check.command))
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
