"""What the cycle did, recorded so a later question can be answered from data.

This exists to replace one guess in particular. `router.DEFAULT_FIRST_PASS_RATE`
is `0.0` because five real implementations out of five needed a correction
round, and the cost model has carried that number ever since. Five is not a
measurement; it is the smallest sample that produced a unanimous answer. Every
routing cost estimate is built on it until a repository measures its own.

Two design decisions are worth stating, because both are arguable.

**The schema will change.** A schema designed before its questions are known is
a schema that gets migrated, and that risk was accepted deliberately: recording
now, with adapters that were just proven correct, beats recording later from a
larger but unexamined pile. So only the fields already known to be queried get
columns, a short allowlist of counts and flags travels in `payload`, and
`schema_version` is on every row from the first one.

**A rate from too few rows is not reported.** `first_pass_rate()` returns the
sample size alongside the value and returns `None` for the value when the sample
is too small to mean anything. An estimate that silently hardens from three
observations into a routing decision is worse than the honest constant it would
replace.

The database lives outside every repository and never enters Git. It holds
references, statuses and counts — no diffs and no prose — and that
boundary is enforced by a typed allowlist rather than asserted in this
paragraph. A field nobody has considered is refused by name, and an approved
field that arrives with the wrong shape is refused too: a name says who may
write, a type says what. Both are needed, because a secret can be short and
prose can hide one level down inside a container.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
APP_DIRNAME = "code-cycle-toolkit"
DATABASE_NAME = "telemetry.sqlite"

#: Below this many observations a rate is reported as unknown. Not a
#: significance test — just a refusal to let three runs set a routing constant.
MINIMUM_SAMPLE = 10

#: Every field this store accepts, column or payload, and the shape it may hold.
#:
#: One table rather than two, because this boundary has now been breached three
#: times and each breach was the same mistake in different clothes: the promise
#: was enforced for the part that had just been pointed out — first a docstring,
#: then payload key names, then payload value types — while another door stayed
#: open. A promoted column is not safer than a payload key; it is just a
#: different door. So there are no exempt fields.
#:
#: Kinds:
#:   count      an integer
#:   amount     a number
#:   flag       a boolean
#:   token      a value from a closed vocabulary, never free text
#:   identifier a short reference with no whitespace: prose has spaces
FIELD_SPECS: dict[str, tuple[str, frozenset | None]] = {
    # identifiers supplied by the caller
    "repo_id": ("identifier", None),
    "task_id": ("identifier", None),
    "model_requested": ("identifier", None),
    "model_resolved": ("identifier", None),
    # closed vocabularies
    "role": ("token", frozenset({
        "implement", "review", "rereview", "resolve", "verify", "run",
        "bootstrap", "coordinate", "security", "triage",
    })),
    "skill": ("token", frozenset({
        "cc-implement-issue", "cc-initial-review", "cc-resolve-comments",
        "cc-rereview", "cc-orchestrator", "cc-orca-orchestrator", "cc-pr-review",
        "cc-code-review", "cc-security-review", "cc-verify", "cc-run",
        "cc-provider-bootstrap",
    })),
    "profile": ("token", frozenset({
        "cheap_tool", "coordinator", "cheap_coder", "deep_coder", "reviewer",
        "senior_reviewer", "security",
    })),
    "executor": ("token", frozenset({"codex", "claude", "orca"})),
    "provider": ("token", frozenset({"openai", "anthropic"})),
    "effort": ("token", frozenset({"low", "medium", "high", "max"})),
    "readiness_policy": ("token", frozenset({"proven", "attempt"})),
    "dispatched_from": ("token", frozenset({
        "unknown", "installed", "authenticated", "quota_exhausted", "ready",
    })),
    "outcome": ("token", frozenset({
        "succeeded", "blocked", "failed", "contract_violation",
    })),
    "status": ("token", frozenset({
        "APPROVED", "CHANGES_REQUESTED", "BLOCKED", "FAILED", "RESOLVED",
        "PARTIALLY_RESOLVED", "IMPLEMENTED", "READY_FOR_MANUAL_MERGE",
        "HUMAN_INTERVENTION", "IN_PROGRESS",
    })),
    "missing_capability": ("token", frozenset({
        "operating_quota", "operating_availability", "proven_readiness",
        "authenticated_session", "folder_trust", "hook_trust",
        "bypass_acknowledgement", "trusted_directory", "orchestration_context",
        "provider_agent_mapping",
    })),
    "verifiability": ("token", frozenset({"auto", "partial", "human"})),
    # numbers and flags
    "iteration": ("count", None),
    "difficulty": ("count", None),
    "findings_total": ("count", None),
    "findings_blocking": ("count", None),
    "duration_ms": ("count", None),
    "used_fallback": ("flag", None),
    "security_sensitive": ("flag", None),
    # payload-only
    "routing_reason_count": ("count", None),
    "iterations": ("count", None),
    "findings_critical": ("count", None),
    "findings_high": ("count", None),
    "findings_medium": ("count", None),
    "findings_low": ("count", None),
    "checks_passed": ("count", None),
    "checks_failed": ("count", None),
    "exit_code": ("count", None),
    "tokens_in": ("count", None),
    "tokens_out": ("count", None),
    "cost_usd": ("amount", None),
    "security_audit_ran": ("flag", None),
    "security_gate_half": ("token", frozenset({
        "deterministic", "reviewer", "both", "none",
    })),
}

#: An identifier is a reference, not a sentence, and not a secret.
#:
#: What a validator can and cannot do here is worth being exact about, because
#: this boundary was claimed too strongly four times. `gpt-5.6-luna` and
#: `sk-live-abc123` have the same shape: lowercase letters, digits, hyphens. No
#: grammar separates a model name from a credential, so the guarantees are:
#:
#:   - model names are checked against the models this toolkit knows, which is
#:     a closed set and therefore a real guarantee;
#:   - repository and task references must match a reference grammar and must
#:     not carry a published credential prefix, which rejects the accidents that
#:     actually happen;
#:   - nothing proves an arbitrary caller-supplied reference is not a secret.
#:
#: That last line is the honest limit. It is mitigated by where these values
#: come from — a repository selector out of `.code-cycle.yml` and a work-item id
#: out of the issue provider, both derived by the toolkit rather than typed into
#: a field — and not by pretending the validator settles it.
MAX_IDENTIFIER_LENGTH = 200

#: A reference: alphanumerics and the punctuation real selectors use.
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/#@:+-]*$")

#: Published credential formats. Not a guess about what a secret looks like —
#: these are documented prefixes, and rejecting them catches the paste that
#: happens by accident.
CREDENTIAL_PREFIXES = (
    "sk-", "sk_", "pk_", "rk_",
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "xox", "AKIA", "ASIA", "AIza", "ya29.",
    "glpat-", "dop_v1_", "shpat_", "npm_",
    "-----BEGIN",
)

def _known_models() -> frozenset:
    """Model names this toolkit knows, read from the router's profiles.

    A closed set is the guarantee a grammar cannot give, so it has to actually
    be available. Both supported import shapes are tried — `telemetry` with
    `scripts/` on the path, and `scripts.telemetry` as a package module — and if
    neither yields the router this raises rather than returning an empty set.

    Returning empty used to be the fallback, and `_checked` read empty as
    permission to skip the check. That is the failure this project already named
    once, in the security gate: an absent configuration must not silently
    disable the thing it configures, because it makes the harmless-looking case
    the unsafe one. Here it made a guarantee depend on import topology.
    """
    errors = []
    for loader in (_router_flat, _router_packaged):
        try:
            profiles, parse_target = loader()
        except Exception as exc:  # pragma: no cover - depends on import shape
            errors.append(f"{loader.__name__}: {exc}")
            continue
        models = set()
        for spec in profiles.values():
            for key in ("primary", "fallback"):
                value = spec.get(key)
                if isinstance(value, str):
                    models.add(parse_target(value).model)
        if models:
            return frozenset(models)
        errors.append(f"{loader.__name__}: no models in profiles")
    raise TelemetryError(
        "the known-model set could not be built, so a model name cannot be "
        "checked against it: " + "; ".join(errors)
    )


def _router_flat():
    from router import DEFAULT_PROFILES, parse_target
    return DEFAULT_PROFILES, parse_target


def _router_packaged():
    from scripts.router import DEFAULT_PROFILES, parse_target  # noqa: F401
    return DEFAULT_PROFILES, parse_target


def known_models() -> frozenset:
    """Cached accessor. Raises when the set cannot be built; never empty."""
    global _KNOWN_MODELS_CACHE
    if _KNOWN_MODELS_CACHE is None:
        _KNOWN_MODELS_CACHE = _known_models()
    return _KNOWN_MODELS_CACHE


_KNOWN_MODELS_CACHE: frozenset | None = None

#: Kept for callers that still read it.
MAX_PAYLOAD_VALUE_LENGTH = 120

#: Review statuses that settle whether the first pass succeeded. A row with any
#: other status, or none, has not finished saying what happened, and counting it
#: as a failure would invent an outcome.
TERMINAL_REVIEW_STATUSES = frozenset({"APPROVED", "CHANGES_REQUESTED"})
PASSING_REVIEW_STATUSES = frozenset({"APPROVED"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS stages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version     INTEGER NOT NULL,
    recorded_at        TEXT    NOT NULL,
    repo_id            TEXT    NOT NULL,
    task_id            TEXT    NOT NULL,
    role               TEXT    NOT NULL,
    skill              TEXT,
    iteration          INTEGER NOT NULL DEFAULT 0,
    profile            TEXT,
    executor           TEXT,
    provider           TEXT,
    model_requested    TEXT,
    model_resolved     TEXT,
    effort             TEXT,
    readiness_policy   TEXT,
    dispatched_from    TEXT,
    used_fallback      INTEGER NOT NULL DEFAULT 0,
    outcome            TEXT,
    status             TEXT,
    missing_capability TEXT,
    difficulty         INTEGER,
    verifiability      TEXT,
    security_sensitive INTEGER,
    findings_total     INTEGER,
    findings_blocking  INTEGER,
    duration_ms        INTEGER,
    payload            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS stages_repo_task ON stages (repo_id, task_id);
CREATE INDEX IF NOT EXISTS stages_profile   ON stages (repo_id, profile, role);
"""


class TelemetryError(ValueError):
    """The store was asked for something it must not answer."""


def default_database_path() -> Path:
    """Outside every repository, beside the other host-local state."""
    override = os.environ.get("CODE_CYCLE_HOME")
    if override:
        base = Path(override)
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) / APP_DIRNAME if appdata else Path.home() / APP_DIRNAME
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = (Path(xdg) if xdg else Path.home() / ".config") / APP_DIRNAME
    return base / DATABASE_NAME


@dataclass(frozen=True)
class Rate:
    """A measured rate, or an honest refusal to give one.

    `value` is `None` when `observations` is below the minimum. Callers get the
    count either way, so "we do not know yet" and "it is genuinely zero" never
    look the same.
    """

    value: float | None
    observations: int
    minimum: int = MINIMUM_SAMPLE

    @property
    def known(self) -> bool:
        return self.value is not None

    def explain(self) -> str:
        if self.known:
            return f"{self.value:.2f} from {self.observations} observations"
        return (f"unknown: {self.observations} observation(s), "
                f"fewer than the {self.minimum} required")


#: Columns promoted out of `payload` because they are already queried.
_COLUMNS = (
    "repo_id", "task_id", "role", "skill", "iteration", "profile", "executor",
    "provider", "model_requested", "model_resolved", "effort", "readiness_policy",
    "dispatched_from", "used_fallback", "outcome", "status", "missing_capability",
    "difficulty", "verifiability", "security_sensitive", "findings_total",
    "findings_blocking", "duration_ms",
)


def validate_reference(field: str, value):
    """Apply this store's rule for a field, before anything is spent on it.

    The same check `record_stage` would make, exported so a caller can make it
    first. A repository or work item that this store will refuse is worth
    refusing before a stage is dispatched under it: the alternative is an
    executor run, paid for, whose row cannot be written.

    It is deliberately the same function rather than a second copy of the
    rules. Two validators agree until one of them is edited.
    """
    return _checked(field, value)


def _checked(key: str, value):
    """Return the value if its shape matches what the field may hold.

    Typed rather than length-limited, and applied to every field. A string is
    not safe because it is short — `TOP-SECRET` is ten characters — a container
    is not safe because its key was approved, and a column is not safe because
    it has a name in the schema.
    """
    if key not in FIELD_SPECS:
        raise TelemetryError(
            f"{key!r} is not a telemetry field. This store holds counts, flags, "
            "identifiers and closed-vocabulary tokens, never prose or "
            "credentials; add it to FIELD_SPECS deliberately if it belongs."
        )
    kind, vocabulary = FIELD_SPECS[key]
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, set, bytes)):
        raise TelemetryError(
            f"{key!r} may not hold a {type(value).__name__}: a container moves "
            "prose one level down rather than keeping it out"
        )
    if kind == "count":
        if isinstance(value, bool) or not isinstance(value, int):
            raise TelemetryError(f"{key!r} is a count and must be an integer, got {type(value).__name__}")
        return value
    if kind == "amount":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TelemetryError(f"{key!r} is an amount and must be a number, got {type(value).__name__}")
        return float(value)
    if kind == "flag":
        if not isinstance(value, bool):
            raise TelemetryError(f"{key!r} is a flag and must be a boolean, got {type(value).__name__}")
        return value
    if kind == "token":
        if value not in (vocabulary or frozenset()):
            raise TelemetryError(
                f"{key!r} accepts only {sorted(vocabulary or ())}, not {value!r}; "
                "a token field is a closed vocabulary, not short free text"
            )
        return value
    if kind == "identifier":
        if not isinstance(value, str):
            raise TelemetryError(f"{key!r} is an identifier and must be a string, got {type(value).__name__}")
        if len(value) > MAX_IDENTIFIER_LENGTH:
            raise TelemetryError(
                f"{key!r} is {len(value)} characters, over the {MAX_IDENTIFIER_LENGTH} "
                "allowed for an identifier"
            )
        if not IDENTIFIER_PATTERN.match(value):
            raise TelemetryError(
                f"{key!r} is not a reference: {value!r} contains whitespace or "
                "characters a repository, work item or model name does not use"
            )
        for prefix in CREDENTIAL_PREFIXES:
            if value.startswith(prefix):
                raise TelemetryError(
                    f"{key!r} starts with {prefix!r}, a published credential "
                    "prefix. Telemetry stores references, never secrets."
                )
        if key in ("model_requested", "model_resolved"):
            # No `and known_models()` guard: an unavailable set raises rather
            # than waving the value through. A check that switches itself off
            # when it cannot run is not a check.
            if value not in known_models():
                raise TelemetryError(
                    f"{key!r} must name a model this toolkit knows, not {value!r}. "
                    "A closed set is the only real guarantee here: a model name "
                    "and a credential have the same shape."
                )
        return value
    raise TelemetryError(f"{key!r} declares an unknown field kind {kind!r}")


class Telemetry:
    """Append-only record of what each stage did."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def record_stage(self, repo_id: str, task_id: str, role: str, **fields) -> int:
        """Append one stage execution.

        A keyword that is neither a column nor an allowed payload key is
        refused by name. Refusing loudly is the point: storing it would break
        the no-prose promise, and dropping it silently would lose an
        observation the caller believed it had recorded.
        """
        if not repo_id or not task_id or not role:
            raise TelemetryError("a stage needs repo_id, task_id and role")

        row = {
            "repo_id": _checked("repo_id", repo_id),
            "task_id": _checked("task_id", task_id),
            "role": _checked("role", role),
        }
        payload = {}
        for key, value in fields.items():
            checked = _checked(key, value)
            if key in _COLUMNS:
                row[key] = checked
            else:
                payload[key] = checked
        for flag in ("used_fallback", "security_sensitive"):
            if flag in row and row[flag] is not None:
                row[flag] = int(bool(row[flag]))
        row.setdefault("iteration", 0)
        row.setdefault("used_fallback", 0)

        columns = ["schema_version", "recorded_at", *row, "payload"]
        values = [SCHEMA_VERSION, datetime.now(timezone.utc).isoformat(),
                  *row.values(), json.dumps(payload, sort_keys=True, default=str)]
        placeholders = ", ".join("?" * len(columns))
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                f"INSERT INTO stages ({', '.join(columns)}) VALUES ({placeholders})", values
            )
            connection.commit()
            return int(cursor.lastrowid)

    def record_dispatch(self, repo_id: str, task_id: str, role: str, decision, result,
                        **extra) -> int:
        """Record a routing decision and what dispatching it did.

        Takes the objects rather than a hand-copied dict, so the row cannot
        disagree with what actually happened.
        """
        target = decision.target
        return self.record_stage(
            repo_id, task_id, role,
            profile=decision.profile,
            used_fallback=decision.used_fallback,
            executor=getattr(target, "executor", None),
            provider=getattr(target, "provider", None),
            model_requested=getattr(target, "model", None),
            effort=getattr(target, "effort", None),
            model_resolved=getattr(result, "model_resolved", None),
            outcome=getattr(getattr(result, "outcome", None), "value", None),
            missing_capability=getattr(result, "missing_capability", None),
            readiness_policy=getattr(getattr(result, "readiness_policy", None), "value", None),
            dispatched_from=getattr(getattr(result, "dispatched_from", None), "value", None),
            # The reasons themselves are prose and belong in the published
            # comment, not in a store that promises to hold none. How many there
            # were is still useful for spotting a decision that needed
            # explaining.
            routing_reason_count=len(getattr(decision, "reasons", ()) or ()),
            **extra,
        )

    def rows(self, repo_id: str | None = None, task_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM stages"
        clauses, params = [], []
        if repo_id:
            clauses.append("repo_id = ?")
            params.append(repo_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        with closing(self._connect()) as connection:
            out = []
            for row in connection.execute(query, params):
                record = dict(row)
                record["payload"] = json.loads(record["payload"] or "{}")
                out.append(record)
            return out

    def first_pass_rate(
        self, repo_id: str, profile: str | None = None, *, minimum: int = MINIMUM_SAMPLE
    ) -> Rate:
        """How often an implementation passed its first review, per repository.

        This is the number `router.estimate_cost` needs and currently guesses.
        A task counts once: its first review that reached a verdict, whatever
        happened afterwards. A task with no review recorded is not counted at
        all — an implementation nobody reviewed is not evidence that it would
        have passed — and neither is one whose review has not said yet.

        The profile filter names the *implementer* whose work was reviewed, not
        the reviewer, because the question is which implementer is good enough.
        """
        implementers = {}
        for row in self.rows(repo_id):
            if row["role"] == "implement" and row["task_id"] not in implementers:
                implementers[row["task_id"]] = row["profile"]

        passed = total = 0
        seen_reviews = set()
        for row in self.rows(repo_id):
            if row["role"] != "review" or row["task_id"] in seen_reviews:
                continue
            if row["task_id"] not in implementers:
                continue
            if profile is not None and implementers[row["task_id"]] != profile:
                continue
            status = (row["status"] or "").upper()
            if status not in TERMINAL_REVIEW_STATUSES:
                # Not yet an outcome. The task stays unconsumed so a later
                # terminal review still counts: a row recorded before its
                # verdict arrived — which an append-only API makes ordinary —
                # must not be read as a failed first pass.
                continue
            seen_reviews.add(row["task_id"])
            total += 1
            if status in PASSING_REVIEW_STATUSES:
                passed += 1

        if total < minimum:
            return Rate(None, total, minimum)
        return Rate(passed / total, total, minimum)

    def dispatch_failures(self, repo_id: str | None = None) -> dict[str, int]:
        """Which capability blocked dispatches, and how often.

        A campaign was lost to an exhausted window read as evidence about a
        model. Counting the capability by name keeps that distinction visible.
        """
        counts: dict[str, int] = {}
        for row in self.rows(repo_id):
            capability = row["missing_capability"]
            if capability:
                counts[capability] = counts.get(capability, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def model_drift(self, repo_id: str | None = None) -> list[dict]:
        """Rows where the executor ran a model other than the one requested.

        Rows with no reported model are excluded rather than counted as
        matching: Codex reports none at all, and treating that silence as
        agreement would hide exactly what this is for.
        """
        return [
            {"task_id": row["task_id"], "role": row["role"],
             "requested": row["model_requested"], "resolved": row["model_resolved"]}
            for row in self.rows(repo_id)
            if row["model_resolved"] and row["model_requested"]
            and row["model_resolved"] != row["model_requested"]
        ]

    def summary(self, repo_id: str) -> dict:
        rows = self.rows(repo_id)
        return {
            "repo_id": repo_id,
            "stages": len(rows),
            "tasks": len({row["task_id"] for row in rows}),
            "first_pass_rate": self.first_pass_rate(repo_id).explain(),
            "dispatch_failures": self.dispatch_failures(repo_id),
            "model_drift": len(self.model_drift(repo_id)),
        }
