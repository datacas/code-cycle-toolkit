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
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: 1: the original stage row. 2: adds the pre-routing signals below, all in
#: `payload`, so a version-1 row is still read as it was written; the version
#: only tells a reader which signals the writer could have supplied. 3: adds the
#: cycle correlation keys and the observed outcomes below, again only in
#: `payload`; a row from an earlier version simply has no cycle to belong to.
#: 4: adds `shadow` rows holding an optional selector's suggestion, whose
#: fields below are payload-only and appear on no other kind of row.
#: 5: adds `started_from`, the stage a cycle began at, so a cycle that resumed
#: an existing change request is not read as an implementation's first pass.
#: 6: verdict rows carry the code-host checks of the head a stage finished on,
#: as `checks_passed`, `checks_failed` and the new `checks_pending`.
SCHEMA_VERSION = 6
APP_DIRNAME = "code-cycle-toolkit"
DATABASE_NAME = "telemetry.sqlite"

#: Below this many observations a rate is reported as unknown. Not a
#: significance test — just a refusal to let three runs set a routing constant.
MINIMUM_SAMPLE = 10

#: TypeSafe model identifiers are constrained to its alias and version format.
#: This records future concrete versions without accepting arbitrary strings.
JEV_MODELS = frozenset({"jev-latest"})
JEV_MODEL_VERSION_PATTERN = re.compile(r"jev-[0-9]+\.[0-9]+\.[0-9]+\Z")


def is_jev_model(value) -> bool:
    return (isinstance(value, str) and len(value) <= 64
            and (value in JEV_MODELS
                 or JEV_MODEL_VERSION_PATTERN.fullmatch(value) is not None))

#: The stages a cycle can begin at. `implement` is a whole cycle; the others
#: resume a change request that already exists.
CYCLE_STARTS = ("implement", "review", "resolve", "rereview")

#: The profiles a shadow selector compares, which are the `implement` and
#: `resolve` candidates in `router.ROLE_CANDIDATES`.
SHADOW_PROFILES = frozenset({"cheap_coder", "deep_coder"})

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
#:   jev_model  the constrained TypeSafe alias/version grammar above
#:   identifier a short reference with no whitespace: prose has spaces
FIELD_SPECS: dict[str, tuple[str, frozenset | None]] = {
    # identifiers supplied by the caller
    "repo_id": ("identifier", None),
    "task_id": ("identifier", None),
    "model_requested": ("identifier", None),
    "model_resolved": ("identifier", None),
    "model_resolution": ("token", frozenset({
        "matched", "mismatch_known", "mismatch_unrecognized", "unreported",
    })),
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
        "cheap_tool", "auxiliary_tool", "coordinator", "cheap_coder", "deep_coder", "reviewer",
        "senior_reviewer", "security",
    })),
    "executor": ("token", frozenset({"codex", "claude", "orca"})),
    "provider": ("token", frozenset({"openai", "anthropic"})),
    "effort": ("token", frozenset({"low", "medium", "high", "max"})),
    "readiness_policy": ("token", frozenset({"proven", "attempt"})),
    "read_only_mode": ("token", frozenset({"enforced", "detected"})),
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
        "provider_agent_mapping", "read_only_enforcement", "disposable_workspace",
        "review_workspace_isolation", "review_workspace_mismatch",
        "review_workspace_conflict", "publication_access",
    })),
    "verifiability": ("token", frozenset({"auto", "partial", "human"})),
    "routing_strategy": ("token", frozenset({"fixed", "measured"})),
    "routing_cost_status": ("token", frozenset({"available", "unavailable"})),
    "routing_rate_source": ("token", frozenset({
        "default", "measured", "conservative_default",
    })),
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
    "routing_rate_observations": ("count", None),
    "routing_rate_minimum": ("count", None),
    "iterations": ("count", None),
    "findings_critical": ("count", None),
    "findings_high": ("count", None),
    "findings_medium": ("count", None),
    "findings_low": ("count", None),
    "checks_passed": ("count", None),
    "checks_failed": ("count", None),
    "checks_pending": ("count", None),
    "exit_code": ("count", None),
    "tokens_in": ("count", None),
    "tokens_out": ("count", None),
    "cost_usd": ("amount", None),
    "routing_cost_implementation": ("amount", None),
    "routing_cost_expected_resolutions": ("amount", None),
    "routing_cost_review": ("amount", None),
    "routing_cost_total": ("amount", None),
    "routing_rate_value": ("amount", None),
    "routing_rate_used": ("amount", None),
    "security_audit_ran": ("flag", None),
    "local_only": ("flag", None),
    "routing_rate_known": ("flag", None),
    "security_gate_half": ("token", frozenset({
        "deterministic", "reviewer", "both", "none",
    })),
    # pre-routing signals, observed before the stage was routed (schema 2)
    "changed_files_count": ("count", None),
    "changed_lines_estimate": ("count", None),
    "has_tests": ("flag", None),
    "test_count_estimate": ("count", None),
    "touches_dependencies": ("flag", None),
    "touches_database": ("flag", None),
    "touches_auth": ("flag", None),
    "touches_api": ("flag", None),
    "touches_migrations": ("flag", None),
    "touches_ci": ("flag", None),
    "changed_python_files": ("count", None),
    "changed_javascript_files": ("count", None),
    "changed_typescript_files": ("count", None),
    "changed_go_files": ("count", None),
    "changed_rust_files": ("count", None),
    "changed_java_files": ("count", None),
    "changed_csharp_files": ("count", None),
    "changed_ruby_files": ("count", None),
    "changed_php_files": ("count", None),
    "changed_shell_files": ("count", None),
    "changed_sql_files": ("count", None),
    "changed_markdown_files": ("count", None),
    "changed_other_files": ("count", None),
    "prior_findings_total": ("count", None),
    "prior_findings_blocking": ("count", None),
    "prior_findings_critical": ("count", None),
    "prior_findings_high": ("count", None),
    "prior_findings_medium": ("count", None),
    "prior_findings_low": ("count", None),
    "verification_available": ("flag", None),
    "previous_failed_attempts": ("count", None),
    "resolution_round": ("count", None),
    # cycle correlation (schema 3)
    "cycle_id": ("identifier", None),
    "stage_seq": ("count", None),
    "record_kind": ("token", frozenset({"dispatch", "verdict", "cycle", "shadow"})),
    # where the cycle began (schema 5)
    "started_from": ("token", frozenset(CYCLE_STARTS)),
    # observed outcomes, written only once they are known (schema 3)
    "tests_passed": ("flag", None),
    "first_review_status": ("token", frozenset({"APPROVED", "CHANGES_REQUESTED"})),
    "final_review_status": ("token", frozenset({"APPROVED", "CHANGES_REQUESTED"})),
    "first_pass_approved": ("flag", None),
    "resolution_needed": ("flag", None),
    "resolution_rounds": ("count", None),
    "final_approved": ("flag", None),
    "fallback_stages": ("count", None),
    "contract_violations": ("count", None),
    # a shadow selector's suggestion, on `shadow` rows only (schema 4)
    "jev_status": ("token", frozenset({
        "suggested", "unavailable", "timeout", "rate_limited", "http_error",
        "invalid_response",
    })),
    "jev_rule_profile": ("token", SHADOW_PROFILES),
    "jev_suggested_profile": ("token", SHADOW_PROFILES),
    "jev_agreement": ("flag", None),
    "jev_confidence": ("amount", None),
    "jev_probability_cheap_coder": ("amount", None),
    "jev_probability_deep_coder": ("amount", None),
    "jev_model_requested": ("jev_model", None),
    "jev_model_resolved": ("jev_model", None),
    "jev_model_resolution": ("token", frozenset({
        "matched", "mismatch_known", "mismatch_unrecognized", "unreported",
    })),
    "jev_duration_ms": ("count", None),
}

#: Inclusive bounds for counts that have them. A count outside its range is a
#: caller bug, not an observation, and is refused like a wrong type.
_UPPER_COUNT = 1_000_000
FIELD_LIMITS: dict[str, tuple[int, int]] = {
    "difficulty": (1, 3),
    **{
        name: (0, _UPPER_COUNT) for name in (
            "changed_files_count", "changed_lines_estimate", "test_count_estimate",
            "changed_python_files", "changed_javascript_files",
            "changed_typescript_files", "changed_go_files", "changed_rust_files",
            "changed_java_files", "changed_csharp_files", "changed_ruby_files",
            "changed_php_files", "changed_shell_files", "changed_sql_files",
            "changed_markdown_files", "changed_other_files",
            "prior_findings_total", "prior_findings_blocking",
            "prior_findings_critical", "prior_findings_high",
            "prior_findings_medium", "prior_findings_low",
            "previous_failed_attempts", "resolution_round",
            "stage_seq", "resolution_rounds", "fallback_stages",
            "contract_violations", "jev_duration_ms",
        )
    },
}

#: Amounts that are probabilities. Refused outside [0, 1] like a count out of
#: its range: a confidence of 7 is a parsing bug, not an observation.
PROBABILITY_FIELDS = frozenset({
    "jev_confidence", "jev_probability_cheap_coder", "jev_probability_deep_coder",
})

#: Where each pre-routing signal comes from. `declared` is a judgement somebody
#: made about the work, `observed` is read deterministically off the diff or
#: the cycle's own state, and `estimated` is derived by a heuristic and named as
#: an estimate. A query that mixes them should know it is doing so.
PRE_ROUTING_SIGNALS: dict[str, frozenset[str]] = {
    "declared": frozenset({"difficulty", "verifiability", "security_sensitive"}),
    "observed": frozenset({
        "role", "changed_files_count", "has_tests", "touches_dependencies",
        "touches_database", "touches_auth", "touches_api", "touches_migrations",
        "touches_ci", "changed_python_files", "changed_javascript_files",
        "changed_typescript_files", "changed_go_files", "changed_rust_files",
        "changed_java_files", "changed_csharp_files", "changed_ruby_files",
        "changed_php_files", "changed_shell_files", "changed_sql_files",
        "changed_markdown_files", "changed_other_files",
        "prior_findings_total", "prior_findings_blocking",
        "prior_findings_critical", "prior_findings_high",
        "prior_findings_medium", "prior_findings_low",
        "verification_available", "previous_failed_attempts", "resolution_round",
    }),
    "estimated": frozenset({"changed_lines_estimate", "test_count_estimate"}),
}

#: What ties a row to one run of one cycle. `cycle_id` is minted once per
#: `CycleRecorder`; `stage_seq` numbers its `stage()` calls, so a rerouted
#: attempt shares the number of the stage it belongs to and a verdict carries the
#: number of the dispatch it reports on. A later suggestion from another selector
#: can point at the same pair without the rows it annotates being rewritten.
CORRELATION_FIELDS = frozenset({"cycle_id", "stage_seq", "record_kind"})

#: What a `shadow` row may say about a suggestion: the rules' profile, the
#: suggested one and the evidence beside it. It holds no pre-routing signal and
#: no outcome — both are read from their own rows by `(cycle_id, stage_seq)` —
#: so a comparison can never be fitted on a field the suggestion did not have.
SHADOW_FIELDS = frozenset({
    "jev_status", "jev_rule_profile", "jev_suggested_profile", "jev_agreement",
    "jev_confidence", "jev_probability_cheap_coder", "jev_probability_deep_coder",
    "jev_model_requested", "jev_model_resolved", "jev_model_resolution",
    "jev_duration_ms",
})

#: What was learned after a routing, by the row that is its source. None of it
#: is ever written onto a `dispatch` row: those carry the pre-routing signals,
#: and an outcome beside them would leak into any evaluation that reads them.
#: A field that was not observed is absent, never a default. Only what the
#: driver can read off a structured result is listed. The check counts come from
#: a stage's `checks` block and describe the head it finished on, so they are
#: recorded only when that block names the same head as the result.
OUTCOME_FIELDS: dict[str, frozenset[str]] = {
    "verdict": frozenset({
        "status", "findings_total", "findings_blocking", "findings_critical",
        "findings_high", "findings_medium", "findings_low", "tests_passed",
        "checks_passed", "checks_failed", "checks_pending",
    }),
    "cycle": frozenset({
        "status", "iterations", "first_review_status", "final_review_status",
        "first_pass_approved", "resolution_needed", "resolution_rounds",
        "final_approved", "tests_passed", "fallback_stages", "contract_violations",
    }),
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


def started_at_implement(row: dict) -> bool:
    """Whether a row belongs to a cycle that began by implementing.

    A resumed cycle reviews an implementation some earlier run produced, so its
    review says nothing about a first pass. A row written before `started_from`
    existed came from a driver that could only start at `implement`.
    """
    payload = row.get("payload") or {}
    return payload.get("started_from", "implement") == "implement"

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


def validate_reference(field: str, value, *, model_names: Iterable[str] | None = None):
    """Apply this store's rule for a field, before anything is spent on it.

    The same check `record_stage` would make, exported so a caller can make it
    first. A repository or work item that this store will refuse is worth
    refusing before a stage is dispatched under it: the alternative is an
    executor run, paid for, whose row cannot be written.

    Model names from repository configuration must be supplied through
    `model_names`; this function never reads configuration itself. When omitted,
    only the built-in model policy is used.

    It is deliberately the same function rather than a second copy of the
    rules. Two validators agree until one of them is edited.
    """
    permitted = frozenset(model_names) if model_names is not None else None
    return _checked(field, value, model_names=permitted)


def _checked(key: str, value, *, model_names: frozenset | None = None):
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
        if key in FIELD_LIMITS:
            low, high = FIELD_LIMITS[key]
            if not low <= value <= high:
                raise TelemetryError(f"{key!r} must be between {low} and {high}, got {value}")
        return value

    if kind == "amount":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TelemetryError(f"{key!r} is an amount and must be a number, got {type(value).__name__}")
        if key in PROBABILITY_FIELDS and not 0.0 <= value <= 1.0:
            raise TelemetryError(f"{key!r} is a probability and must be between 0 and 1, got {value}")
        return float(value)
    if kind == "flag":
        if not isinstance(value, bool):
            raise TelemetryError(f"{key!r} is a flag and must be a boolean, got {type(value).__name__}")
        return value
    if kind == "jev_model":
        if not is_jev_model(value):
            raise TelemetryError(
                f"{key!r} must be a TypeSafe alias or versioned Jev model identifier, not {value!r}"
            )
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
        if key == "model_requested":
            # No `and known_models()` guard: an unavailable set raises rather
            # than waving the value through. A check that switches itself off
            # when it cannot run is not a check.
            permitted = model_names if model_names is not None else known_models()
            if value not in permitted:
                raise TelemetryError(
                    f"{key!r} must name a model this toolkit knows, not {value!r}. "
                    "A closed set is the only real guarantee here: a model name "
                    "and a credential have the same shape."
                )
        return value
    raise TelemetryError(f"{key!r} declares an unknown field kind {kind!r}")


def routing_decision_fields(decision) -> dict:
    """Return the persisted routing fields shared by every stage row."""
    cost = getattr(decision, "cost", None)
    strategy = getattr(decision, "strategy", None)
    return {
        "routing_strategy": getattr(strategy, "value", "fixed"),
        "routing_cost_status": getattr(decision, "cost_status", None)
        or ("available" if cost is not None else "unavailable"),
        "routing_cost_implementation": getattr(cost, "implementation", None),
        "routing_cost_expected_resolutions": getattr(
            cost, "expected_resolutions", None,
        ),
        "routing_cost_review": getattr(cost, "review", None),
        "routing_cost_total": getattr(cost, "total", None),
        "routing_rate_source": getattr(decision, "rate_source", "default"),
        "routing_rate_value": getattr(decision, "rate_value", None),
        "routing_rate_used": getattr(decision, "rate_used", None),
        "routing_rate_observations": getattr(decision, "rate_observations", None),
        "routing_rate_minimum": getattr(decision, "rate_minimum", None),
        "routing_rate_known": getattr(decision, "rate_known", None),
    }


class Telemetry:
    """Append-only record of what each stage did."""

    def __init__(self, path: Path | str | None = None,
                 known_models: Iterable[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_database_path()
        # These are the names supplied by the component that already parsed
        # repository configuration. They are deliberately per instance: two
        # repositories in one process must not share their additions.
        self._configured_models = frozenset(known_models or ())
        self._configured_models_by_repo: dict[str, frozenset[str]] = {}
        self._effective_models: frozenset | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def _model_names(self, repo_id: str | None = None) -> frozenset:
        """Build this instance's closed model set, raising if defaults fail."""
        if self._effective_models is None:
            self._effective_models = _known_models() | self._configured_models
        return self._effective_models | self._configured_models_by_repo.get(
            repo_id, frozenset()
        )

    def add_known_models(self, repo_id: str, models: Iterable[str]) -> None:
        """Inject resolved configuration models without crossing repository scopes."""
        current = self._configured_models_by_repo.get(repo_id, frozenset())
        self._configured_models_by_repo[repo_id] = current | frozenset(models)

    def _model_observation(self, repo_id: str, requested, resolved) -> tuple[str | None, str]:
        """Keep unknown executor observations out of the store without losing the row."""
        if resolved is None:
            return None, "unreported"
        if isinstance(resolved, str) and requested is not None and resolved == requested:
            return resolved, "matched"
        if isinstance(resolved, str) and resolved in self._model_names(repo_id):
            return resolved, "mismatch_known"
        return None, "mismatch_unrecognized"

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

        fields = dict(fields)
        if "model_resolved" in fields:
            stored, resolution = self._model_observation(
                repo_id, fields.get("model_requested"), fields["model_resolved"]
            )
            fields["model_resolved"] = stored
            fields["model_resolution"] = resolution
        elif "model_requested" in fields:
            fields["model_resolution"] = "unreported"

        row = {
            "repo_id": self._checked("repo_id", repo_id, repo_id=repo_id),
            "task_id": self._checked("task_id", task_id, repo_id=repo_id),
            "role": self._checked("role", role, repo_id=repo_id),
        }
        payload = {}
        for key, value in fields.items():
            checked = self._checked(key, value, repo_id=repo_id)
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

    def _checked(self, key: str, value, *, repo_id: str | None = None):
        model_names = self._model_names(repo_id) if key == "model_requested" else None
        return _checked(key, value, model_names=model_names)

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
            read_only_mode=(getattr(result, "artifacts", {}) or {}).get("read_only_mode"),
            duration_ms=getattr(result, "duration_ms", None),
            # The reasons themselves are prose and belong in the published
            # comment, not in a store that promises to hold none. How many there
            # were is still useful for spotting a decision that needed
            # explaining.
            routing_reason_count=len(getattr(decision, "reasons", ()) or ()),
            **routing_decision_fields(decision),
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

        A review from a cycle that resumed a change request is not counted: it
        judged an implementation some earlier run produced, perhaps after fixes.
        """
        implementers = {}
        for row in self.rows(repo_id):
            # A shadow row is a suggestion, not the implementation that ran.
            if row["payload"].get("record_kind") == "shadow":
                continue
            if row["role"] == "implement" and row["task_id"] not in implementers:
                implementers[row["task_id"]] = row["profile"]

        passed = total = 0
        seen_reviews = set()
        for row in self.rows(repo_id):
            if row["role"] != "review" or row["task_id"] in seen_reviews:
                continue
            if not started_at_implement(row):
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
        drift = []
        for row in self.rows(repo_id):
            resolution = row["payload"].get("model_resolution")
            if resolution is None:
                # Rows written before model_resolution existed still carry the
                # two columns that established drift. Keep that history
                # visible rather than treating an absent token as agreement.
                is_drift = (
                    row["model_resolved"]
                    and row["model_requested"]
                    and row["model_resolved"] != row["model_requested"]
                )
            else:
                is_drift = resolution in {"mismatch_known", "mismatch_unrecognized"}
            if is_drift:
                drift.append({
                    "task_id": row["task_id"], "role": row["role"],
                    "requested": row["model_requested"],
                    "resolved": row["model_resolved"],
                })
        return drift

    def cycle_outcome(self, repo_id: str, cycle_id: str) -> dict | None:
        """One run of a cycle, reassembled from its rows.

        Returns `None` for a cycle this store has no rows for — including any
        written before schema 3, which carry no `cycle_id`. Otherwise the
        outcome holds only what the closing row observed, with `closed` saying
        whether there was one: a run that never closed has an unknown outcome,
        not an unapproved one. Dispatches and verdicts are listed by
        `stage_seq`, the verdicts with the fields they reported and nothing
        filled in.
        """
        rows = [row for row in self.rows(repo_id)
                if row["payload"].get("cycle_id") == cycle_id]
        if not rows:
            return None

        def fields(row: dict, names: frozenset) -> dict:
            merged = {**row["payload"], **{key: row[key] for key in _COLUMNS}}
            return {key: merged[key] for key in sorted(names)
                    if merged.get(key) is not None}

        closing_rows = [row for row in rows if row["payload"].get("record_kind") == "cycle"]
        return {
            "cycle_id": cycle_id,
            "task_id": rows[0]["task_id"],
            "closed": bool(closing_rows),
            "outcome": fields(closing_rows[-1], OUTCOME_FIELDS["cycle"]) if closing_rows else {},
            "dispatches": [
                {"stage_seq": row["payload"].get("stage_seq"), "role": row["role"],
                 "profile": row["profile"], "outcome": row["outcome"],
                 "used_fallback": bool(row["used_fallback"])}
                for row in rows if row["payload"].get("record_kind") == "dispatch"
            ],
            "verdicts": [
                {"stage_seq": row["payload"].get("stage_seq"), "role": row["role"],
                 **fields(row, OUTCOME_FIELDS["verdict"])}
                for row in rows if row["payload"].get("record_kind") == "verdict"
            ],
            "shadows": [
                {"stage_seq": row["payload"].get("stage_seq"), "role": row["role"],
                 **fields(row, SHADOW_FIELDS)}
                for row in rows if row["payload"].get("record_kind") == "shadow"
            ],
        }

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
