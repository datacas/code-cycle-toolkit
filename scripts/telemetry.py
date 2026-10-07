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
import hashlib
import os
import re
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

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
#: 7: `resolve` and `rereview` rows carry `repeated_findings`, an observed
#: pre-routing signal, and the `cycle` row carries `stop_reason`. Both are
#: payload-only; an older row reads as unknown.
#: 8: test outcomes carry `tests_basis`, the evidence level they rest on, and
#: verdicts carry the agent's separate `verification` conclusion.
#: 9: a verdict whose change required a boundary run carries
#: `boundary_verified`; one that did not require it carries nothing new.
#: 11: the optional `issue_review` stage. Its verdict carries the readiness
#: fields below, its escalated dispatch carries `escalated`, and the cycle row
#: carries `issue_review` and the stop reasons `needs_refinement` and
#: `readiness_unconfirmed`. All payload-only; an older row has no such stage.
#: 12: a dispatch the host interrupted is recorded with the outcome
#: `interrupted`, and its cycle row with the stop reason `interrupted`. An
#: older store has no such rows: its interrupted runs left none at all. A
#: cycle that continued an interrupted implementation carries `continued` on
#: its cycle row, payload-only.
#: 13: additive attempt, execution-variant, harness-snapshot, usage, cost,
#: update, and conflict tables. Existing stage rows are not rewritten; old
#: attempts and usage remain unknown rather than being filled with zero.
#: 15: start evidence and classified attempt errors; migrate only the projection.
#: 16: append-only recovery references; attempts retain their failed outcome.
SCHEMA_VERSION = 16
APP_DIRNAME = "code-cycle-toolkit"
DATABASE_NAME = "telemetry.sqlite"

#: Below this many observations a rate is reported as unknown. Not a
#: significance test — just a refusal to let three runs set a routing constant.
MINIMUM_SAMPLE = 10

TEST_BASE_ORDER = (
    "claimed", "agent_reported", "runtime_observed", "externally_verified",
)
TEST_BASES = frozenset(TEST_BASE_ORDER)
VERIFICATION_CONCLUSION_ORDER = (
    "verified", "verified_with_reservations", "not_verified", "failed",
)
VERIFICATION_CONCLUSIONS = frozenset(VERIFICATION_CONCLUSION_ORDER)

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

#: Why a cycle stopped, one token per exit condition of the driver. A row from
#: before schema 7 has none, which reads as unknown rather than as any of these.
STOP_REASONS = frozenset({
    "approved", "iteration_limit", "no_progress", "repeated_findings",
    "stage_not_completed", "dispatch_failed", "local_only",
    "needs_refinement", "readiness_unconfirmed", "interrupted",
})

#: What a new cycle did about the optional issue review (schema 11).
ISSUE_REVIEW_DECISIONS = frozenset({"off", "skipped", "dispatched"})

#: The categorical confidence an issue review reports: evidence quality, never
#: a probability.
READINESS_CONFIDENCES = frozenset({"high", "medium", "low"})

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
    "dispatch_attempt_id": ("identifier", None),
    "recovery_head_sha": ("identifier", None),
    "recovery_comment_id": ("identifier", None),
    "recovery_state": ("token", frozenset({"recovered_pending_verification"})),
    "execution_variant_id": ("identifier", None),
    "harness_snapshot_id": ("identifier", None),
    "model_resolution": ("token", frozenset({
        "matched", "mismatch_known", "mismatch_unrecognized", "unreported",
    })),
    # closed vocabularies
    "role": ("token", frozenset({
        "implement", "review", "rereview", "resolve", "verify", "run",
        "bootstrap", "coordinate", "security", "triage", "issue_review",
    })),
    "skill": ("token", frozenset({
        "cc-implement-issue", "cc-initial-review", "cc-resolve-comments",
        "cc-rereview", "cc-orchestrator", "cc-orca-orchestrator", "cc-pr-review",
        "cc-code-review", "cc-security-review", "cc-verify", "cc-run",
        "cc-provider-bootstrap", "cc-issue-review",
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
        "succeeded", "blocked", "failed", "contract_violation", "interrupted",
    })),
    "status": ("token", frozenset({
        "APPROVED", "CHANGES_REQUESTED", "BLOCKED", "FAILED", "RESOLVED",
        "PARTIALLY_RESOLVED", "IMPLEMENTED", "READY_FOR_MANUAL_MERGE",
        "HUMAN_INTERVENTION", "IN_PROGRESS", "READY", "NEEDS_REFINEMENT",
    })),
    "missing_capability": ("token", frozenset({
        "operating_quota", "operating_availability", "proven_readiness",
        "authenticated_session", "folder_trust", "hook_trust",
        "bypass_acknowledgement", "trusted_directory", "orchestration_context",
        "provider_agent_mapping", "read_only_enforcement", "disposable_workspace",
        "review_workspace_isolation", "review_workspace_mismatch",
        "review_workspace_conflict", "publication_access", "read_only_verification",
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
    # implementation forecast: a post-routing claim, never a routing signal
    "forecast_changed_files_count": ("count", None),
    "forecast_changed_lines_estimate": ("count", None),
    "forecast_has_tests": ("flag", None),
    "forecast_touches_dependencies": ("flag", None),
    "forecast_touches_database": ("flag", None),
    "forecast_touches_auth": ("flag", None),
    "forecast_touches_api": ("flag", None),
    "forecast_touches_migrations": ("flag", None),
    "forecast_touches_ci": ("flag", None),
    "prior_findings_total": ("count", None),
    "prior_findings_blocking": ("count", None),
    "prior_findings_critical": ("count", None),
    "prior_findings_high": ("count", None),
    "prior_findings_medium": ("count", None),
    "prior_findings_low": ("count", None),
    "verification_available": ("flag", None),
    "previous_failed_attempts": ("count", None),
    "resolution_round": ("count", None),
    # findings that survived a claimed fix before this routing (schema 7)
    "repeated_findings": ("count", None),
    # cycle correlation (schema 3)
    "cycle_id": ("identifier", None),
    "work_item_provider": ("token", frozenset({"github", "plane", "jira"})),
    "work_item_repository": ("identifier", None),
    # numeric pull-request reference emitted by an implementation verdict
    "pull_request_id": ("identifier", None),
    "stage_seq": ("count", None),
    "record_kind": ("token", frozenset({"dispatch", "verdict", "cycle", "shadow"})),
    # where the cycle began (schema 5)
    "started_from": ("token", frozenset(CYCLE_STARTS)),
    # observed outcomes, written only once they are known (schema 3)
    "tests_passed": ("flag", None),
    # the evidence level and agent conclusion for a test outcome (schema 8)
    "tests_basis": ("token", TEST_BASES),
    "verification": ("token", VERIFICATION_CONCLUSIONS),
    # whether a required boundary run has evidence; absent when not required (schema 9)
    "boundary_verified": ("flag", None),
    "first_review_status": ("token", frozenset({"APPROVED", "CHANGES_REQUESTED"})),
    "final_review_status": ("token", frozenset({"APPROVED", "CHANGES_REQUESTED"})),
    "first_pass_approved": ("flag", None),
    "resolution_needed": ("flag", None),
    "resolution_rounds": ("count", None),
    "final_approved": ("flag", None),
    "fallback_stages": ("count", None),
    "contract_violations": ("count", None),
    # why the cycle stopped where it did (schema 7)
    "stop_reason": ("token", STOP_REASONS),
    # the cycle continued an interrupted implementation (schema 12)
    "continued": ("flag", None),
    # the optional issue review (schema 11)
    "escalated": ("flag", None),
    "issue_review": ("token", ISSUE_REVIEW_DECISIONS),
    "readiness_result_valid": ("flag", None),
    "readiness_confidence": ("token", READINESS_CONFIDENCES),
    "readiness_findings_total": ("count", None),
    "readiness_findings_blocking": ("count", None),
    "readiness_uncertainties_material": ("count", None),
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
    # per-dispatch usage accounting references (schema 13)
    "parent_attempt_id": ("identifier", None),
    "dispatch_relationship": ("token", frozenset({"retry", "fallback"})),
    "attempt_lifecycle_state": ("token", frozenset({
        "launched", "running", "completed", "failed", "timed_out", "interrupted",
    })),
    "attempt_error_code": ("token", frozenset({
        "capacity", "quota", "auth", "transport", "timeout", "unreadable_result",
        "unavailable", "contract_violation", "interrupted", "precondition",
        "executor_error", "no_error_report",
    })),
    "attempt_start_state": ("token", frozenset({"started", "not_started", "unknown"})),
    "external_dispatch_id": ("identifier", None),
    "pricing_snapshot_id": ("identifier", None),
    "attempt_source": ("token", frozenset({"runtime", "codex", "claude", "orca", "provider", "operator"})),
    "usage_category": ("token", frozenset({
        "input_total", "input", "cached_input", "cache_read_input",
        "cache_write_input", "cache_creation_input", "output_total", "output",
        "reasoning_output",
    })),
    "usage_source_event": ("token", frozenset({"turn.completed", "result.modelUsage"})),
    "cost_basis": ("token", frozenset({
        "actual_billed", "api_equivalent_estimated", "subscription_consumption", "unknown",
    })),
    "cost_source": ("token", frozenset({"codex", "claude", "orca", "provider", "operator"})),
    "cost_component": ("token", frozenset({"reported_cli_cost", "provider_charge", "api_estimate", "subscription_usage"})),
    "correction_reason": ("token", frozenset({
        "provider_correction", "parser_correction", "operator_correction", "reconciliation",
    })),
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
            "forecast_changed_files_count", "forecast_changed_lines_estimate",
            "prior_findings_total", "prior_findings_blocking",
            "prior_findings_critical", "prior_findings_high",
            "prior_findings_medium", "prior_findings_low",
            "previous_failed_attempts", "resolution_round",
            "repeated_findings", "stage_seq", "resolution_rounds", "fallback_stages",
            "contract_violations", "jev_duration_ms",
            "readiness_findings_total", "readiness_findings_blocking",
            "readiness_uncertainties_material",
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
        "repeated_findings", "escalated",
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
        "tests_basis", "verification", "boundary_verified",
        "forecast_changed_files_count", "forecast_changed_lines_estimate",
        "forecast_has_tests", "forecast_touches_dependencies",
        "forecast_touches_database", "forecast_touches_auth", "forecast_touches_api",
        "forecast_touches_migrations", "forecast_touches_ci",
        "checks_passed", "checks_failed", "checks_pending",
        "readiness_result_valid", "readiness_confidence",
        "readiness_findings_total", "readiness_findings_blocking",
        "readiness_uncertainties_material",
    }),
    "cycle": frozenset({
        "status", "iterations", "first_review_status", "final_review_status",
        "first_pass_approved", "resolution_needed", "resolution_rounds",
        "final_approved", "tests_passed", "tests_basis", "verification",
        "boundary_verified", "fallback_stages", "contract_violations", "stop_reason",
        "issue_review", "continued",
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

WORK_ITEM_PROVIDERS = frozenset({"github", "plane", "jira"})
WORK_ITEM_OUTCOMES = frozenset({
    "resolved", "closed_unresolved", "pr_merged", "reopened", "reverted", "unknown",
})
WORK_ITEM_SOURCES = frozenset({"provider_query", "explicit"})
WORK_ITEM_RECONCILIATION_FAILURES = frozenset({
    "provider_unavailable", "provider_query_failed", "invalid_snapshot",
    "pull_request_query_failed",
})


@dataclass(frozen=True)
class WorkItemIdentity:
    """The canonical identity of an external tracker work item (Issue #185).

    Every metric added to Code Cycle Toolkit declares its grouping identity
    first: per cycle, work item, stage, attempt, or dispatch.
    """
    provider: str
    repository: str
    work_item_id: str
    work_item_type: str = "issue"
    host: str | None = None

    def __post_init__(self) -> None:
        prov = (self.provider or "").strip().lower()
        repo = (self.repository or "").strip()
        host = self.host
        if repo.startswith(("http://", "https://")):
            parsed = urlparse(repo)
            if not host:
                host = parsed.netloc.lower()
            repo = parsed.path.strip("/")
        elif "@" in repo and ":" in repo:
            parts = repo.split(":", 1)
            if not host:
                host = parts[0].split("@")[-1].lower()
            repo = parts[1].strip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        repo = repo.strip("/")
        if prov in {"github", "gitlab"} or (host and "github" in host):
            repo = repo.lower()

        item_id = (str(self.work_item_id) if self.work_item_id is not None else "").strip().lstrip("#")
        item_type = (self.work_item_type or "issue").strip().lower()

        object.__setattr__(self, "provider", prov)
        object.__setattr__(self, "repository", repo)
        object.__setattr__(self, "work_item_id", item_id)
        object.__setattr__(self, "work_item_type", item_type)
        object.__setattr__(self, "host", host.lower() if host else None)

    @property
    def is_unknown(self) -> bool:
        """Return True if this identity represents an unknown or unobserved work item."""
        return (
            not self.provider
            or not self.repository
            or not self.work_item_id
            or self.provider == "unknown"
            or self.repository == "unknown"
            or self.work_item_id == "unknown"
        )

    @classmethod
    def unknown(cls) -> WorkItemIdentity:
        """Return the canonical unknown work-item identity."""
        return cls(provider="unknown", repository="unknown", work_item_id="unknown")

    def as_tuple(self) -> tuple[str, str, str]:
        """Return (provider, repository, work_item_id)."""
        return (self.provider, self.repository, self.work_item_id)

    def as_scoped_tuple(self, repo_id: str) -> tuple[str, str, str, str]:
        """Return (repo_id, provider, repository, work_item_id)."""
        return (str(repo_id), self.provider, self.repository, self.work_item_id)

    def __str__(self) -> str:
        if self.is_unknown:
            return "unknown"
        host_prefix = f"{self.host}/" if self.host else ""
        return f"{self.provider}:{host_prefix}{self.repository}#{self.work_item_id}"

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

-- Additive schema 13: one row per physical executor invocation. Existing
-- stage rows are left untouched and remain queryable as unmeasured history.
CREATE TABLE IF NOT EXISTS execution_variants (
    execution_variant_id TEXT PRIMARY KEY,
    executor             TEXT NOT NULL,
    provider             TEXT NOT NULL,
    model_requested      TEXT NOT NULL,
    model_resolved       TEXT,
    effort               TEXT NOT NULL,
    fingerprint          TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS harness_snapshots (
    harness_snapshot_id TEXT PRIMARY KEY,
    fingerprint         TEXT NOT NULL UNIQUE,
    components_json     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_attempts (
    attempt_id              TEXT PRIMARY KEY,
    repo_id                 TEXT NOT NULL,
    task_id                 TEXT NOT NULL,
    cycle_id                TEXT NOT NULL,
    stage_seq               INTEGER NOT NULL CHECK(stage_seq >= 1),
    role                    TEXT NOT NULL,
    executor                TEXT NOT NULL,
    provider                TEXT NOT NULL,
    model_requested         TEXT NOT NULL,
    model_resolved          TEXT,
    model_resolution        TEXT NOT NULL DEFAULT 'unreported',
    effort                  TEXT NOT NULL,
    profile                 TEXT NOT NULL,
    execution_variant_id    TEXT NOT NULL REFERENCES execution_variants(execution_variant_id),
    harness_snapshot_id     TEXT NOT NULL REFERENCES harness_snapshots(harness_snapshot_id),
    parent_attempt_id       TEXT REFERENCES dispatch_attempts(attempt_id),
    relationship            TEXT CHECK(relationship IN ('retry', 'fallback')),
    lifecycle_state         TEXT NOT NULL CHECK(lifecycle_state IN
                              ('launched', 'running', 'completed', 'failed', 'timed_out', 'interrupted')),
    outcome                 TEXT CHECK(outcome IN
                              ('succeeded', 'blocked', 'failed', 'contract_violation', 'interrupted')),
    error_code              TEXT,
    start_state             TEXT NOT NULL DEFAULT 'unknown'
                            CHECK(start_state IN ('started', 'not_started', 'unknown')),
    external_dispatch_id    TEXT,
    started_at              TEXT NOT NULL,
    finished_at             TEXT,
    duration_ms             INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0),
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dispatch_attempts_cycle
    ON dispatch_attempts (repo_id, cycle_id, stage_seq, attempt_id);
CREATE INDEX IF NOT EXISTS dispatch_attempts_task
    ON dispatch_attempts (repo_id, task_id, cycle_id);
CREATE INDEX IF NOT EXISTS dispatch_attempts_external_id
    ON dispatch_attempts (external_dispatch_id);
CREATE TABLE IF NOT EXISTS stage_recoveries (
    attempt_id TEXT PRIMARY KEY REFERENCES dispatch_attempts(attempt_id),
    change_request_id TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state = 'recovered_pending_verification'),
    created_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS stage_recoveries_no_update
BEFORE UPDATE ON stage_recoveries BEGIN SELECT RAISE(ABORT, 'recovery is append-only'); END;
CREATE TRIGGER IF NOT EXISTS stage_recoveries_no_delete
BEFORE DELETE ON stage_recoveries BEGIN SELECT RAISE(ABORT, 'recovery is append-only'); END;
CREATE TABLE IF NOT EXISTS dispatch_attempt_updates (
    update_id             TEXT PRIMARY KEY,
    attempt_id            TEXT NOT NULL REFERENCES dispatch_attempts(attempt_id),
    source                TEXT NOT NULL,
    observed_at           TEXT NOT NULL,
    correction_reason     TEXT,
    patch_hash            TEXT NOT NULL,
    patch_json            TEXT NOT NULL,
    correction_of_update  TEXT REFERENCES dispatch_attempt_updates(update_id)
);
CREATE INDEX IF NOT EXISTS attempt_updates_attempt
    ON dispatch_attempt_updates (attempt_id, observed_at);
CREATE TABLE IF NOT EXISTS dispatch_conflicts (
    conflict_id       TEXT PRIMARY KEY,
    attempt_id        TEXT NOT NULL REFERENCES dispatch_attempts(attempt_id),
    field             TEXT NOT NULL,
    existing_hash     TEXT NOT NULL,
    incoming_hash     TEXT NOT NULL,
    observed_at       TEXT NOT NULL,
    source            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_observations (
    observation_id       TEXT PRIMARY KEY,
    attempt_id           TEXT NOT NULL REFERENCES dispatch_attempts(attempt_id),
    source               TEXT NOT NULL,
    source_event         TEXT NOT NULL,
    event_ordinal        INTEGER NOT NULL CHECK(event_ordinal >= 0),
    source_model         TEXT,
    category             TEXT NOT NULL,
    amount               INTEGER NOT NULL CHECK(amount >= 0),
    observed_at          TEXT NOT NULL,
    supersedes_id        TEXT REFERENCES usage_observations(observation_id),
    superseded_by_id     TEXT REFERENCES usage_observations(observation_id),
    correction_reason    TEXT
);
CREATE INDEX IF NOT EXISTS usage_attempt_active
    ON usage_observations (attempt_id, superseded_by_id, category);
CREATE TABLE IF NOT EXISTS cost_measures (
    measure_id            TEXT PRIMARY KEY,
    attempt_id            TEXT NOT NULL REFERENCES dispatch_attempts(attempt_id),
    basis                 TEXT NOT NULL CHECK(basis IN
                              ('actual_billed', 'api_equivalent_estimated',
                               'subscription_consumption', 'unknown')),
    attribution_key       TEXT NOT NULL,
    component             TEXT NOT NULL,
    source                TEXT NOT NULL,
    unit                  TEXT,
    amount                TEXT,
    reported_amount       TEXT,
    pricing_snapshot_id   TEXT,
    pricing_date          TEXT,
    observed_at           TEXT NOT NULL,
    supersedes_id         TEXT REFERENCES cost_measures(measure_id),
    superseded_by_id      TEXT REFERENCES cost_measures(measure_id),
    correction_reason     TEXT
);
CREATE INDEX IF NOT EXISTS cost_attempt_active
    ON cost_measures (attempt_id, superseded_by_id, basis, component);
-- Additive schema 14: provider-observed work-item transitions are separate
-- from cycle rows so review or merge status cannot become issue resolution.
CREATE TABLE IF NOT EXISTS work_item_disposition_events (
    event_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version       INTEGER NOT NULL,
    repo_id              TEXT NOT NULL,
    provider             TEXT NOT NULL CHECK(provider IN ('github', 'plane', 'jira')),
    external_repository  TEXT NOT NULL,
    work_item_id         TEXT NOT NULL,
    outcome              TEXT NOT NULL CHECK(outcome IN
                             ('resolved', 'closed_unresolved', 'pr_merged',
                              'reopened', 'reverted', 'unknown')),
    source               TEXT NOT NULL CHECK(source IN ('provider_query', 'explicit')),
    observed_at          TEXT NOT NULL,
    recorded_at          TEXT NOT NULL,
    cycle_ids            TEXT NOT NULL,
    pull_request_ids     TEXT NOT NULL,
    merged_pr_ids        TEXT NOT NULL,
    resolving_pr_ids     TEXT NOT NULL,
    reverted_pr_ids      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS work_item_disposition_latest
    ON work_item_disposition_events
       (repo_id, provider, external_repository, work_item_id,
        observed_at, recorded_at, event_id);
CREATE TABLE IF NOT EXISTS work_item_reconciliation_failures (
    failure_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version       INTEGER NOT NULL,
    repo_id              TEXT NOT NULL,
    provider             TEXT NOT NULL CHECK(provider IN ('github', 'plane', 'jira')),
    external_repository  TEXT NOT NULL,
    work_item_id         TEXT NOT NULL,
    failure_kind         TEXT NOT NULL CHECK(failure_kind IN
                             ('provider_unavailable', 'provider_query_failed',
                              'invalid_snapshot', 'pull_request_query_failed')),
    pull_request_id      TEXT,
    observed_at          TEXT NOT NULL,
    recorded_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS work_item_reconciliation_failure_latest
    ON work_item_reconciliation_failures
       (repo_id, provider, external_repository, work_item_id,
        observed_at, recorded_at, failure_id);
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


# Keep direct telemetry writes aligned with scripts/usage.py cost limits.
_MAX_COST_TEXT_LENGTH = 128
_MAX_COST_EXPONENT = 128


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
            self._migrate_dispatch_attempts(connection)
            connection.commit()

    @staticmethod
    def _migrate_dispatch_attempts(connection) -> None:
        """Schema 15: upgrade the projection without rewriting its audit log."""
        connection.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(dispatch_attempts)")}
        if "start_state" not in columns:
            connection.execute(
                "ALTER TABLE dispatch_attempts ADD COLUMN start_state TEXT NOT NULL "
                "DEFAULT 'unknown' CHECK(start_state IN ('started', 'not_started', 'unknown'))"
            )
            connection.execute(
                "UPDATE dispatch_attempts SET error_code = CASE error_code "
                "WHEN 'operating_quota' THEN 'quota' "
                "WHEN 'operating_availability' THEN 'unavailable' "
                "ELSE 'executor_error' END WHERE error_code IN "
                "('operating_quota', 'operating_availability', 'dispatch_failed', 'executor_error', 'unknown')"
            )

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
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _timestamp(value=None) -> str:
        if value is None:
            return datetime.now(timezone.utc).isoformat()
        if not isinstance(value, str):
            raise TelemetryError("timestamps must be ISO-8601 strings with a timezone")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TelemetryError("timestamps must be ISO-8601 strings with a timezone") from exc
        if parsed.tzinfo is None:
            raise TelemetryError("timestamps must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _fingerprint(value) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _safe_external_reference(self, key: str, value: str):
        checked = self._checked(key, value)
        if not isinstance(checked, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", checked):
            raise TelemetryError(f"{key} must be a short opaque reference without path separators")
        return checked

    def _execution_variant(self, connection, *, executor, provider,
                           model_requested, model_resolved, effort, repo_id):
        executor = self._checked("executor", executor, repo_id=repo_id)
        provider = self._checked("provider", provider, repo_id=repo_id)
        model_requested = self._checked("model_requested", model_requested, repo_id=repo_id)
        effort = self._checked("effort", effort, repo_id=repo_id)
        if model_resolved is not None:
            model_resolved, _resolution = self._model_observation(
                repo_id, model_requested, model_resolved,
            )
        variant = {
            "executor": executor, "provider": provider,
            "model_requested": model_requested,
            "model_resolved": model_resolved, "effort": effort,
        }
        fingerprint = self._fingerprint(variant)
        variant_id = f"variant-{fingerprint}"
        connection.execute(
            "INSERT OR IGNORE INTO execution_variants "
            "(execution_variant_id, executor, provider, model_requested, model_resolved, effort, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (variant_id, executor, provider, model_requested, model_resolved, effort, fingerprint),
        )
        return variant_id, variant

    def _harness_snapshot(self, connection, snapshot):
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("components"), dict):
            raise TelemetryError("a harness snapshot needs normalized components")
        components = snapshot["components"]
        allowed = {
            "toolkit_release", "toolkit_commit", "runtime_manifest_sha256",
            "routing_policy_sha256", "prompt_template_sha256", "executor",
            "executor_version", "profile", "profile_config_sha256",
            "routing_strategy", "readiness_policy", "role", "skill", "skill_sha256",
        }
        if set(components) != allowed:
            raise TelemetryError("harness snapshot components do not match the safe field allowlist")
        normalized = {}
        for key, value in components.items():
            if value is None:
                normalized[key] = None
            elif key.endswith("_sha256"):
                if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                    raise TelemetryError(f"{key} must be a SHA-256 digest")
                normalized[key] = value
            elif key == "toolkit_commit":
                if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
                    raise TelemetryError("toolkit_commit must be a full commit hash")
                normalized[key] = value
            elif key == "toolkit_release":
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", value):
                    raise TelemetryError("toolkit_release must be a short version identifier")
                normalized[key] = value
            elif key == "executor_version":
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", value):
                    raise TelemetryError("executor_version must be a short version identifier")
                normalized[key] = value
            else:
                field_name = {
                    "routing_strategy": "routing_strategy",
                    "readiness_policy": "readiness_policy",
                    "executor": "executor", "profile": "profile", "role": "role",
                    "skill": "skill",
                }[key]
                normalized[key] = self._checked(field_name, value)
        fingerprint = self._fingerprint(normalized)
        supplied_fingerprint = snapshot.get("fingerprint")
        snapshot_id = snapshot.get("harness_snapshot_id")
        if supplied_fingerprint != fingerprint or snapshot_id != f"harness-{fingerprint}":
            raise TelemetryError("harness snapshot fingerprint does not match its components")
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "INSERT OR IGNORE INTO harness_snapshots "
            "(harness_snapshot_id, fingerprint, components_json) VALUES (?, ?, ?)",
            (snapshot_id, fingerprint, encoded),
        )
        return snapshot_id

    def create_dispatch_attempt(
        self, repo_id: str, task_id: str, cycle_id: str, stage_seq: int, role: str,
        *, executor: str, provider: str, model_requested: str, effort: str,
        profile: str, harness_snapshot: dict, parent_attempt_id: str | None = None,
        relationship: str | None = None, attempt_id: str | None = None,
        started_at: str | None = None,
    ) -> str:
        """Persist a physical attempt before invoking or launching its executor."""
        for key, value in (("repo_id", repo_id), ("task_id", task_id),
                           ("cycle_id", cycle_id), ("role", role),
                           ("executor", executor), ("provider", provider),
                           ("model_requested", model_requested), ("effort", effort),
                           ("profile", profile)):
            self._checked(key, value, repo_id=repo_id)
        if isinstance(stage_seq, bool) or not isinstance(stage_seq, int) or stage_seq < 1:
            raise TelemetryError("stage_seq must be a positive integer")
        if relationship is not None:
            relationship = self._checked("dispatch_relationship", relationship)
        if parent_attempt_id is not None:
            parent_attempt_id = self._checked("parent_attempt_id", parent_attempt_id)
        if (relationship is None) != (parent_attempt_id is None):
            raise TelemetryError("a retry or fallback relationship must name its parent attempt")
        attempt_id = attempt_id or f"attempt-{uuid.uuid4().hex}"
        attempt_id = self._safe_external_reference("dispatch_attempt_id", attempt_id)
        started_at = self._timestamp(started_at)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            variant_id, _ = self._execution_variant(
                connection, executor=executor, provider=provider,
                model_requested=model_requested, model_resolved=None, effort=effort,
                repo_id=repo_id,
            )
            harness_id = self._harness_snapshot(connection, harness_snapshot)
            if parent_attempt_id is not None:
                parent = connection.execute(
                    "SELECT repo_id, task_id, cycle_id, stage_seq FROM dispatch_attempts WHERE attempt_id=?",
                    (parent_attempt_id,),
                ).fetchone()
                if parent is None or (parent["repo_id"], parent["task_id"], parent["cycle_id"], parent["stage_seq"]) != (
                    repo_id, task_id, cycle_id, stage_seq,
                ):
                    raise TelemetryError("a retry or fallback parent must be from the same logical stage")
            connection.execute(
                "INSERT INTO dispatch_attempts (attempt_id, repo_id, task_id, cycle_id, stage_seq, role, "
                "executor, provider, model_requested, effort, profile, execution_variant_id, "
                "harness_snapshot_id, parent_attempt_id, relationship, lifecycle_state, started_at, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'launched', ?, ?, ?)",
                (attempt_id, repo_id, task_id, cycle_id, stage_seq, role, executor, provider,
                 model_requested, effort, profile, variant_id, harness_id, parent_attempt_id,
                 relationship, started_at, started_at, started_at),
            )
            connection.commit()
        return attempt_id

    @staticmethod
    def _decimal(value, field_name: str, *, required: bool = False) -> str | None:
        if value is None and not required:
            return None
        if isinstance(value, bool):
            raise TelemetryError(f"{field_name} must be a non-negative decimal")
        raw = str(value)
        if len(raw) > _MAX_COST_TEXT_LENGTH:
            raise TelemetryError(f"{field_name} is outside the supported non-negative range")
        try:
            from decimal import Decimal, InvalidOperation
            parsed = Decimal(raw)
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise TelemetryError(f"{field_name} must be a non-negative decimal") from exc
        if (
            not parsed.is_finite()
            or abs(parsed.as_tuple().exponent) > _MAX_COST_EXPONENT
            or parsed < 0
            or parsed > Decimal("1000000000000000000")
        ):
            raise TelemetryError(f"{field_name} is outside the supported non-negative range")
        canonical = format(parsed, "f")
        if len(canonical) > _MAX_COST_TEXT_LENGTH:
            raise TelemetryError(f"{field_name} is outside the supported non-negative range")
        return canonical

    def _conflict(self, connection, attempt_id: str, field_name: str,
                  existing, incoming, observed_at: str, source: str) -> None:
        old_hash = self._fingerprint(existing)
        new_hash = self._fingerprint(incoming)
        identity = "\0".join((attempt_id, field_name, old_hash, new_hash, observed_at, source))
        conflict_id = "conflict-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT OR IGNORE INTO dispatch_conflicts "
            "(conflict_id, attempt_id, field, existing_hash, incoming_hash, observed_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conflict_id, attempt_id, field_name, old_hash, new_hash, observed_at, source),
        )

    def _insert_usage_observation(self, connection, attempt_id: str, value: dict,
                                  observed_at: str, correction_reason: str | None,
                                  source: str) -> bool:
        allowed = {"observation_id", "source", "source_event", "event_ordinal", "source_model",
                   "category", "amount", "observed_at", "supersedes_id"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise TelemetryError("usage observation contains an unrecognized field")
        observation_id = self._safe_external_reference("dispatch_attempt_id", value.get("observation_id"))
        row_source = self._checked("attempt_source", value.get("source", source))
        event = self._checked("usage_source_event", value.get("source_event"))
        category = self._checked("usage_category", value.get("category"))
        ordinal = value.get("event_ordinal")
        amount = value.get("amount")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise TelemetryError("event_ordinal must be a non-negative integer")
        if (isinstance(amount, bool) or not isinstance(amount, int)
                or amount < 0 or amount > 9_223_372_036_854_775_807):
            raise TelemetryError("usage amount must be a non-negative integer; missing is not zero")
        model = value.get("source_model")
        if model is not None:
            attempt = connection.execute(
                "SELECT repo_id, model_requested FROM dispatch_attempts WHERE attempt_id=?", (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise TelemetryError("usage observation refers to an unknown attempt")
            model, _resolution = self._model_observation(
                attempt["repo_id"], attempt["model_requested"], model,
            )
        row_time = self._timestamp(value.get("observed_at") or observed_at)
        supersedes = value.get("supersedes_id")
        if supersedes is not None:
            if correction_reason is None:
                raise TelemetryError("superseding usage needs an explicit correction_reason")
            self._checked("correction_reason", correction_reason)
        normalized = (attempt_id, row_source, event, ordinal, model, category, amount, row_time, supersedes)
        prior = connection.execute(
            "SELECT attempt_id, source, source_event, event_ordinal, source_model, category, amount, observed_at, supersedes_id "
            "FROM usage_observations WHERE observation_id=?", (observation_id,),
        ).fetchone()
        if prior is not None:
            prior_values = tuple(prior)
            if prior_values[:7] + prior_values[8:] != normalized[:7] + normalized[8:]:
                self._conflict(connection, attempt_id, "usage_observation", tuple(prior), normalized, row_time, row_source)
                return True
            return False
        if supersedes is not None:
            previous = connection.execute(
                "SELECT attempt_id, superseded_by_id FROM usage_observations WHERE observation_id=?",
                (supersedes,),
            ).fetchone()
            if previous is None or previous["attempt_id"] != attempt_id or previous["superseded_by_id"]:
                raise TelemetryError("superseded usage must be an active observation from the same attempt")
        connection.execute(
            "INSERT INTO usage_observations (observation_id, attempt_id, source, source_event, event_ordinal, "
            "source_model, category, amount, observed_at, supersedes_id, correction_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (observation_id, *normalized, correction_reason),
        )
        if supersedes:
            connection.execute(
                "UPDATE usage_observations SET superseded_by_id=? WHERE observation_id=?",
                (observation_id, supersedes),
            )
        return False

    def _insert_cost_measure(self, connection, attempt_id: str, value: dict,
                             observed_at: str, correction_reason: str | None,
                             source: str) -> bool:
        allowed = {"measure_id", "attribution_key", "basis", "component", "source", "unit", "amount",
                   "reported_amount", "pricing_snapshot_id", "pricing_date", "observed_at", "supersedes_id"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise TelemetryError("cost measure contains an unrecognized field")
        measure_id = self._safe_external_reference("dispatch_attempt_id", value.get("measure_id"))
        attribution_key = self._safe_external_reference(
            "dispatch_attempt_id", value.get("attribution_key", measure_id),
        )
        basis = self._checked("cost_basis", value.get("basis"))
        component = self._checked("cost_component", value.get("component"))
        row_source = self._checked("cost_source", value.get("source", source))
        unit = value.get("unit")
        if unit is not None and unit not in {"USD", "EUR", "GBP", "tokens", "requests", "credits", "seconds"}:
            raise TelemetryError("cost unit must be a supported currency or usage unit")
        amount = self._decimal(value.get("amount"), "amount", required=basis != "unknown")
        reported = self._decimal(value.get("reported_amount"), "reported_amount")
        if basis == "unknown" and amount is not None:
            raise TelemetryError("unknown basis cannot contain an attributable amount")
        if basis == "actual_billed" and unit not in {"USD", "EUR", "GBP"}:
            raise TelemetryError("actual billed measures need an attributable currency")
        if basis == "api_equivalent_estimated" and unit != "USD":
            raise TelemetryError("API-equivalent estimates must use USD")
        if basis == "api_equivalent_estimated":
            snapshot_id = self._safe_external_reference("pricing_snapshot_id", value.get("pricing_snapshot_id"))
            try:
                date_value = datetime.fromisoformat(value.get("pricing_date", "")).date().isoformat()
            except (ValueError, TypeError) as exc:
                raise TelemetryError("an API-equivalent estimate needs a pricing snapshot date") from exc
        else:
            snapshot_id = value.get("pricing_snapshot_id")
            date_value = value.get("pricing_date")
            if snapshot_id is not None or date_value is not None:
                raise TelemetryError("pricing snapshots apply only to API-equivalent estimates")
        if basis == "unknown" and reported is None and value.get("amount") is not None:
            raise TelemetryError("unknown basis may retain a reported amount only in reported_amount")
        if basis == "subscription_consumption" and unit not in {"tokens", "requests", "credits", "seconds"}:
            raise TelemetryError("subscription consumption must retain its non-monetary usage unit")
        row_time = self._timestamp(value.get("observed_at") or observed_at)
        supersedes = value.get("supersedes_id")
        if supersedes is not None:
            if correction_reason is None:
                raise TelemetryError("superseding cost needs an explicit correction_reason")
            self._checked("correction_reason", correction_reason)
        normalized = (attempt_id, attribution_key, basis, component, row_source, unit, amount, reported,
                      snapshot_id, date_value, row_time, supersedes)
        prior = connection.execute(
            "SELECT attempt_id, attribution_key, basis, component, source, unit, amount, reported_amount, "
            "pricing_snapshot_id, pricing_date, observed_at, supersedes_id "
            "FROM cost_measures WHERE measure_id=?", (measure_id,),
        ).fetchone()
        if prior is not None:
            prior_values = tuple(prior)
            if prior_values[:10] + prior_values[11:] != normalized[:10] + normalized[11:]:
                self._conflict(connection, attempt_id, "cost_measure", tuple(prior), normalized, row_time, row_source)
                return True
            return False
        if supersedes is not None:
            previous = connection.execute(
                "SELECT attempt_id, superseded_by_id FROM cost_measures WHERE measure_id=?", (supersedes,),
            ).fetchone()
            if previous is None or previous["attempt_id"] != attempt_id or previous["superseded_by_id"]:
                raise TelemetryError("superseded cost must be active and belong to the same attempt")
        connection.execute(
            "INSERT INTO cost_measures (measure_id, attempt_id, attribution_key, basis, component, source, unit, amount, "
            "reported_amount, pricing_snapshot_id, pricing_date, observed_at, supersedes_id, correction_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (measure_id, *normalized, correction_reason),
        )
        if supersedes:
            connection.execute(
                "UPDATE cost_measures SET superseded_by_id=? WHERE measure_id=?",
                (measure_id, supersedes),
            )
        return False

    def update_dispatch_attempt(
        self, attempt_id: str, partial_result: dict, *, update_id: str, source: str = "runtime",
        observed_at: str | None = None, correction_reason: str | None = None,
    ) -> dict:
        """Idempotently merge an async or synchronous partial result into one attempt."""
        attempt_id = self._safe_external_reference("dispatch_attempt_id", attempt_id)
        update_id = self._safe_external_reference("dispatch_attempt_id", update_id)
        source = self._checked("attempt_source", source)
        observed_at = self._timestamp(observed_at)
        if correction_reason is not None:
            correction_reason = self._checked("correction_reason", correction_reason)
        if not isinstance(partial_result, dict):
            raise TelemetryError("partial_result must be a mapping of typed fields")
        allowed = {
            "lifecycle_state", "outcome", "model_resolved", "duration_ms", "started_at",
            "finished_at", "external_dispatch_id", "error_code", "start_state", "usage_observations", "cost_measures",
        }
        unknown = set(partial_result) - allowed
        if unknown:
            raise TelemetryError(f"unrecognized attempt update fields: {', '.join(sorted(unknown))}")
        clean = {}
        attempt = self.attempt(attempt_id)
        if attempt is None:
            raise TelemetryError("unknown dispatch attempt")
        for key in ("lifecycle_state", "outcome", "error_code", "start_state", "external_dispatch_id"):
            if key not in partial_result or partial_result[key] is None:
                continue
            value = partial_result[key]
            if key == "lifecycle_state":
                value = self._checked("attempt_lifecycle_state", value)
            elif key == "outcome":
                value = self._checked("outcome", value)
            elif key == "error_code":
                value = self._checked("attempt_error_code", value)
            elif key == "start_state":
                value = self._checked("attempt_start_state", value)
            else:
                value = self._safe_external_reference("external_dispatch_id", value)
            clean[key] = value
        if "model_resolved" in partial_result and partial_result["model_resolved"] is not None:
            resolved, resolution = self._model_observation(
                attempt["repo_id"], attempt["model_requested"], partial_result["model_resolved"],
            )
            clean["model_resolved"] = resolved
            clean["model_resolution"] = resolution
        for key in ("duration_ms",):
            if key in partial_result and partial_result[key] is not None:
                value = partial_result[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise TelemetryError(f"{key} must be a non-negative integer")
                clean[key] = value
        for key in ("started_at", "finished_at"):
            if key in partial_result and partial_result[key] is not None:
                clean[key] = self._timestamp(partial_result[key])
        usage = partial_result.get("usage_observations", ())
        costs = partial_result.get("cost_measures", ())
        if not isinstance(usage, (list, tuple)) or len(usage) > 10000:
            raise TelemetryError("usage_observations must be a bounded sequence")
        if not isinstance(costs, (list, tuple)) or len(costs) > 1000:
            raise TelemetryError("cost_measures must be a bounded sequence")
        clean_usage = []
        for observation in usage:
            if not isinstance(observation, dict):
                clean_usage.append(observation)
                continue
            observation = dict(observation)
            if observation.get("source_model") is not None:
                observation["source_model"], _resolution = self._model_observation(
                    attempt["repo_id"], attempt["model_requested"], observation["source_model"],
                )
            clean_usage.append(observation)
        clean_costs = list(costs)
        clean["usage_observations"] = clean_usage
        clean["cost_measures"] = clean_costs
        patch_json = json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str)
        patch_hash = self._fingerprint({
            "fields": clean, "source": source, "correction_reason": correction_reason,
        })
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior_update = connection.execute(
                "SELECT attempt_id, patch_hash FROM dispatch_attempt_updates WHERE update_id=?",
                (update_id,),
            ).fetchone()
            if prior_update is not None:
                if prior_update["attempt_id"] == attempt_id and prior_update["patch_hash"] == patch_hash:
                    connection.commit()
                    return {"duplicate": True, "conflicts": []}
                self._conflict(connection, attempt_id, "update_id", prior_update["patch_hash"], patch_hash,
                               observed_at, source)
                connection.commit()
                return {"duplicate": False, "conflicts": ["update_id"]}
            latest_update = connection.execute(
                "SELECT update_id FROM dispatch_attempt_updates WHERE attempt_id=? ORDER BY rowid DESC LIMIT 1",
                (attempt_id,),
            ).fetchone() if correction_reason else None
            connection.execute(
                "INSERT INTO dispatch_attempt_updates "
                "(update_id, attempt_id, source, observed_at, correction_reason, patch_hash, patch_json, correction_of_update) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (update_id, attempt_id, source, observed_at, correction_reason, patch_hash, patch_json,
                 latest_update["update_id"] if latest_update else None),
            )
            row = connection.execute("SELECT * FROM dispatch_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            columns = {}
            conflicts = []
            state_rank = {"launched": 0, "running": 1, "completed": 2, "failed": 2,
                          "timed_out": 2, "interrupted": 2}
            for key, value in clean.items():
                if key in {"usage_observations", "cost_measures", "model_resolution"}:
                    continue
                current = row[key]
                if key == "lifecycle_state":
                    allowed_transition = state_rank[value] >= state_rank[current]
                    if not allowed_transition and correction_reason is None:
                        self._conflict(connection, attempt_id, key, current, value, observed_at, source)
                        conflicts.append(key)
                        continue
                    if current in {"completed", "failed", "timed_out", "interrupted"} and value != current and correction_reason is None:
                        self._conflict(connection, attempt_id, key, current, value, observed_at, source)
                        conflicts.append(key)
                        continue
                elif key == "start_state":
                    if current != value and not (
                        current == "unknown" or (current == "not_started" and value == "started")
                    ):
                        self._conflict(connection, attempt_id, key, current, value, observed_at, source)
                        conflicts.append(key)
                        continue
                elif current is not None and current != value and correction_reason is None:
                    self._conflict(connection, attempt_id, key, current, value, observed_at, source)
                    conflicts.append(key)
                    continue
                columns[key] = value
            if "model_resolution" in clean:
                current_resolution = row["model_resolution"]
                resolution = clean["model_resolution"]
                if current_resolution not in {"unreported", resolution} and correction_reason is None:
                    self._conflict(connection, attempt_id, "model_resolution", current_resolution,
                                   resolution, observed_at, source)
                    conflicts.append("model_resolution")
                    columns.pop("model_resolved", None)
                else:
                    columns["model_resolution"] = resolution
            if columns.get("model_resolved"):
                variant_id, _ = self._execution_variant(
                    connection, executor=row["executor"], provider=row["provider"],
                    model_requested=row["model_requested"], model_resolved=columns["model_resolved"],
                    effort=row["effort"], repo_id=row["repo_id"],
                )
                columns["execution_variant_id"] = variant_id
            columns["updated_at"] = observed_at
            connection.execute(
                "UPDATE dispatch_attempts SET " + ", ".join(f"{key}=?" for key in columns) +
                " WHERE attempt_id=?", (*columns.values(), attempt_id),
            )
            for observation in clean_usage:
                observation_conflict = self._insert_usage_observation(
                    connection, attempt_id, observation, observed_at, correction_reason, source,
                )
                if observation_conflict and "usage_observations" not in conflicts:
                    conflicts.append("usage_observations")
            for measure in clean_costs:
                cost_conflict = self._insert_cost_measure(
                    connection, attempt_id, measure, observed_at, correction_reason, source,
                )
                if cost_conflict and "cost_measures" not in conflicts:
                    conflicts.append("cost_measures")
            connection.commit()
        return {"duplicate": False, "conflicts": conflicts}

    def attempt(self, attempt_id: str) -> dict | None:
        attempt_id = self._checked("dispatch_attempt_id", attempt_id)
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM dispatch_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            return dict(row) if row else None

    def record_recovery(self, attempt_id: str, *, change_request_id: str,
                        head_sha: str, comment_id: str) -> None:
        """Append references to provisional evidence; leave the attempt failed."""
        attempt_id = self._checked("dispatch_attempt_id", attempt_id)
        change_request_id = self._checked("pull_request_id", change_request_id)
        head_sha = self._checked("recovery_head_sha", head_sha)
        comment_id = self._checked("recovery_comment_id", comment_id)
        state = self._checked("recovery_state", "recovered_pending_verification")
        if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise TelemetryError("recovery requires a full head SHA")
        with closing(self._connect()) as connection:
            attempt = connection.execute(
                "SELECT outcome FROM dispatch_attempts WHERE attempt_id=?", (attempt_id,),
            ).fetchone()
            if attempt is None or attempt["outcome"] != "failed":
                raise TelemetryError("only a failed attempt can be recovered")
            connection.execute(
                "INSERT INTO stage_recoveries VALUES (?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, change_request_id, head_sha, comment_id, state,
                 self._timestamp(), SCHEMA_VERSION),
            )
            connection.commit()

    def recoveries(self, attempt_id: str) -> list[dict]:
        attempt_id = self._checked("dispatch_attempt_id", attempt_id)
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM stage_recoveries WHERE attempt_id=?", (attempt_id,),
            )]

    def execution_variant(self, variant_id: str) -> dict | None:
        variant_id = self._checked("execution_variant_id", variant_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM execution_variants WHERE execution_variant_id=?", (variant_id,),
            ).fetchone()
            return dict(row) if row else None

    def harness_snapshot(self, snapshot_id: str) -> dict | None:
        snapshot_id = self._checked("harness_snapshot_id", snapshot_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM harness_snapshots WHERE harness_snapshot_id=?", (snapshot_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["components"] = json.loads(result.pop("components_json"))
            return result

    def attempt_by_external_dispatch_id(self, dispatch_id: str) -> dict | None:
        dispatch_id = self._safe_external_reference("external_dispatch_id", dispatch_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM dispatch_attempts WHERE external_dispatch_id=? "
                "ORDER BY created_at DESC LIMIT 1", (dispatch_id,),
            ).fetchone()
            return dict(row) if row else None

    def dispatch_attempts(self, repo_id: str, *, task_id: str | None = None,
                          cycle_id: str | None = None, stage_seq: int | None = None) -> list[dict]:
        repo_id = self._checked("repo_id", repo_id)
        clauses, params = ["repo_id=?"], [repo_id]
        for key, value in (("task_id", task_id), ("cycle_id", cycle_id), ("stage_seq", stage_seq)):
            if value is not None:
                self._checked(key, value)
                clauses.append(f"{key}=?")
                params.append(value)
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM dispatch_attempts WHERE " + " AND ".join(clauses) +
                " ORDER BY cycle_id, stage_seq, started_at, attempt_id", params,
            )]
            variants, snapshots, usage, costs = {}, {}, {}, {}
            for start in range(0, len(rows), 400):
                chunk = rows[start:start + 400]
                attempt_ids = [row["attempt_id"] for row in chunk]
                variant_ids = sorted({row["execution_variant_id"] for row in chunk})
                snapshot_ids = sorted({row["harness_snapshot_id"] for row in chunk})
                attempt_marks = ",".join("?" for _ in attempt_ids)
                variant_marks = ",".join("?" for _ in variant_ids)
                snapshot_marks = ",".join("?" for _ in snapshot_ids)
                for value in connection.execute(
                    f"SELECT * FROM execution_variants WHERE execution_variant_id IN ({variant_marks})",
                    variant_ids,
                ):
                    variants[value["execution_variant_id"]] = dict(value)
                for value in connection.execute(
                    f"SELECT * FROM harness_snapshots WHERE harness_snapshot_id IN ({snapshot_marks})",
                    snapshot_ids,
                ):
                    item = dict(value)
                    item["components"] = json.loads(item.pop("components_json"))
                    snapshots[item["harness_snapshot_id"]] = item
                for value in connection.execute(
                    f"SELECT * FROM usage_observations WHERE attempt_id IN ({attempt_marks}) "
                    "AND superseded_by_id IS NULL ORDER BY observed_at, observation_id",
                    attempt_ids,
                ):
                    usage.setdefault(value["attempt_id"], []).append(dict(value))
                for value in connection.execute(
                    f"SELECT * FROM cost_measures WHERE attempt_id IN ({attempt_marks}) "
                    "AND superseded_by_id IS NULL ORDER BY observed_at, measure_id",
                    attempt_ids,
                ):
                    costs.setdefault(value["attempt_id"], []).append(dict(value))
            for row in rows:
                row["execution_variant"] = variants.get(row["execution_variant_id"])
                row["harness_snapshot"] = snapshots.get(row["harness_snapshot_id"])
                row["usage_observations"] = usage.get(row["attempt_id"], [])
                row["cost_measures"] = costs.get(row["attempt_id"], [])
            return rows

    def usage_totals(self, repo_id: str, *, task_id: str | None = None,
                     cycle_id: str | None = None, stage_seq: int | None = None) -> list[dict]:
        """Aggregate active observations by category; never combine overlapping categories."""
        clauses, params = ["a.repo_id=?", "u.superseded_by_id IS NULL"], [self._checked("repo_id", repo_id)]
        for key, value in (("task_id", task_id), ("cycle_id", cycle_id), ("stage_seq", stage_seq)):
            if value is not None:
                self._checked(key, value)
                clauses.append(f"a.{key}=?")
                params.append(value)
        query = (
            "SELECT a.task_id, a.cycle_id, a.stage_seq, u.category, u.amount FROM usage_observations u "
            "JOIN dispatch_attempts a ON a.attempt_id=u.attempt_id WHERE " + " AND ".join(clauses) +
            " ORDER BY a.task_id, a.cycle_id, a.stage_seq, u.category"
        )
        with closing(self._connect()) as connection:
            grouped = {}
            for row in connection.execute(query, params):
                key = (row["task_id"], row["cycle_id"], row["stage_seq"], row["category"])
                item = grouped.setdefault(key, {
                    "task_id": key[0], "cycle_id": key[1], "stage_seq": key[2],
                    "category": key[3], "observations": 0, "amount": 0,
                })
                item["observations"] += 1
                item["amount"] += row["amount"]
            return list(grouped.values())

    def cost_totals(self, repo_id: str, *, task_id: str | None = None,
                    cycle_id: str | None = None, stage_seq: int | None = None) -> list[dict]:
        """Aggregate costs separately by basis and component, retaining unknown reports."""
        clauses, params = ["a.repo_id=?", "c.superseded_by_id IS NULL"], [self._checked("repo_id", repo_id)]
        for key, value in (("task_id", task_id), ("cycle_id", cycle_id), ("stage_seq", stage_seq)):
            if value is not None:
                self._checked(key, value)
                clauses.append(f"a.{key}=?")
                params.append(value)
        query = (
            "SELECT a.task_id, a.cycle_id, a.stage_seq, c.attempt_id, c.attribution_key, c.basis, c.component, c.unit, "
            "c.amount, c.reported_amount FROM cost_measures c "
            "JOIN dispatch_attempts a ON a.attempt_id=c.attempt_id WHERE " +
            " AND ".join(clauses) +
            " ORDER BY a.task_id, a.cycle_id, a.stage_seq, c.basis, c.component"
        )
        with closing(self._connect()) as connection:
            from decimal import Decimal
            rows = list(connection.execute(query, params))
            actual = {
                (row["attempt_id"], row["attribution_key"])
                for row in rows if row["basis"] == "actual_billed"
            }
            grouped = {}
            for row in rows:
                if (row["basis"] == "api_equivalent_estimated"
                        and (row["attempt_id"], row["attribution_key"]) in actual):
                    continue
                key = tuple(row[name] for name in (
                    "task_id", "cycle_id", "stage_seq", "basis", "component", "unit",
                ))
                item = grouped.setdefault(key, {
                    "task_id": key[0], "cycle_id": key[1], "stage_seq": key[2],
                    "basis": key[3], "component": key[4], "unit": key[5],
                    "measures": 0, "amount": None, "reported_amount": None,
                })
                item["measures"] += 1
                if row["amount"] is not None:
                    current = Decimal(item["amount"] or "0")
                    item["amount"] = format(current + Decimal(row["amount"]), "f")
                if row["reported_amount"] is not None:
                    current = Decimal(item["reported_amount"] or "0")
                    item["reported_amount"] = format(current + Decimal(row["reported_amount"]), "f")
            return list(grouped.values())

    def attempt_updates(self, attempt_id: str) -> list[dict]:
        attempt_id = self._checked("dispatch_attempt_id", attempt_id)
        with closing(self._connect()) as connection:
            result = []
            for row in connection.execute(
                "SELECT * FROM dispatch_attempt_updates WHERE attempt_id=? ORDER BY rowid", (attempt_id,),
            ):
                item = dict(row)
                item["patch"] = json.loads(item.pop("patch_json"))
                result.append(item)
            return result

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
        has_test_outcome = fields.get("tests_passed") is not None
        has_test_basis = fields.get("tests_basis") is not None
        if has_test_outcome != has_test_basis:
            raise TelemetryError(
                "tests_passed and tests_basis must be recorded together"
            )
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
        attempt_id = extra.get("dispatch_attempt_id")
        if attempt_id is not None:
            attempt = self.attempt(attempt_id)
            if attempt is not None:
                extra.setdefault("execution_variant_id", attempt["execution_variant_id"])
                extra.setdefault("harness_snapshot_id", attempt["harness_snapshot_id"])
        observations = getattr(result, "usage_observations", ()) or ()
        totals = {}
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            category = observation.get("category")
            if category not in {"input_total", "output_total"}:
                continue
            amount = observation.get("amount")
            if isinstance(amount, int) and not isinstance(amount, bool):
                totals[category] = totals.get(category, 0) + amount
        if "input_total" in totals:
            extra.setdefault("tokens_in", totals["input_total"])
        if "output_total" in totals:
            extra.setdefault("tokens_out", totals["output_total"])
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

    @staticmethod
    def _work_item_reference(value: str, field: str) -> str:
        checked = _checked("task_id", value)
        if (not checked or not isinstance(checked, str) or
                not IDENTIFIER_PATTERN.fullmatch(checked)):
            raise TelemetryError(f"{field} must be a short opaque reference")
        return checked

    @classmethod
    def _work_item_reference_list(cls, values, field: str) -> list[str]:
        if values is None:
            return []
        if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
            raise TelemetryError(f"{field} must be a list of opaque references")
        return list(dict.fromkeys(cls._work_item_reference(value, field) for value in values))

    @staticmethod
    def _decode_work_item_disposition(row) -> dict:
        event = dict(row)
        for column, key in (("cycle_ids", "cycle_ids"),
                            ("pull_request_ids", "pull_request_ids"),
                            ("merged_pr_ids", "merged_pr_ids"),
                            ("resolving_pr_ids", "resolving_pr_ids"),
                            ("reverted_pr_ids", "reverted_pr_ids")):
            try:
                event[key] = json.loads(event[column])
            except (TypeError, json.JSONDecodeError):
                event[key] = []
        event["repository"] = event.pop("external_repository")
        return event

    def latest_work_item_disposition(
        self, repo_id: str, provider: str, repository: str, work_item_id: str
    ) -> dict | None:
        """Return the latest recorded transition for one external work item."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM work_item_disposition_events "
                "WHERE repo_id = ? AND provider = ? AND external_repository = ? "
                "AND work_item_id = ? "
                "ORDER BY observed_at DESC, recorded_at DESC, event_id DESC LIMIT 1",
                (repo_id, provider, repository, work_item_id),
            ).fetchone()
        return self._decode_work_item_disposition(row) if row else None

    def work_item_disposition_events(self, repo_id: str | None = None) -> list[dict]:
        """Return append-only disposition transitions, optionally repository-scoped."""
        query = "SELECT * FROM work_item_disposition_events"
        params = ()
        if repo_id is not None:
            query += " WHERE repo_id = ?"
            params = (repo_id,)
        query += " ORDER BY observed_at, recorded_at, event_id"
        with closing(self._connect()) as connection:
            return [self._decode_work_item_disposition(row)
                    for row in connection.execute(query, params)]

    def record_work_item_reconciliation_failure(
        self, *, repo_id: str, provider: str, repository: str, work_item_id: str,
        failure_kind: str, pull_request_id: str | None = None,
        observed_at: str | None = None,
    ) -> dict:
        """Append a safe failure category without changing a disposition."""
        repo_id = self._work_item_reference(repo_id, "repo_id")
        repository = self._work_item_reference(repository, "repository")
        work_item_id = self._work_item_reference(work_item_id, "work_item_id")
        if not isinstance(provider, str) or provider not in WORK_ITEM_PROVIDERS:
            raise TelemetryError("provider must be github, plane, or jira")
        if not isinstance(failure_kind, str) or failure_kind not in WORK_ITEM_RECONCILIATION_FAILURES:
            raise TelemetryError("unknown work-item reconciliation failure kind")
        if pull_request_id is not None:
            pull_request_id = self._work_item_reference(pull_request_id, "pull_request_id")
        observed = self._timestamp(observed_at)
        recorded = self._timestamp()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO work_item_reconciliation_failures "
                "(schema_version, repo_id, provider, external_repository, work_item_id, "
                " failure_kind, pull_request_id, observed_at, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (SCHEMA_VERSION, repo_id, provider, repository, work_item_id,
                 failure_kind, pull_request_id, observed, recorded),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM work_item_reconciliation_failures WHERE failure_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return dict(row)

    def work_item_reconciliation_failures(self, repo_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM work_item_reconciliation_failures"
        params = ()
        if repo_id is not None:
            query += " WHERE repo_id = ?"
            params = (repo_id,)
        query += " ORDER BY observed_at, recorded_at, failure_id"
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def record_work_item_disposition(
        self, *, repo_id: str, provider: str, repository: str, work_item_id: str,
        outcome: str, source: str, observed_at: str | None = None,
        cycle_ids=(), pull_request_ids=(), merged_pr_ids=(), resolving_pr_ids=(), reverted_pr_ids=(),
    ) -> dict:
        """Append a work-item transition; unchanged and stale snapshots are no-ops."""
        repo_id = self._work_item_reference(repo_id, "repo_id")
        repository = self._work_item_reference(repository, "repository")
        work_item_id = self._work_item_reference(work_item_id, "work_item_id")
        if not isinstance(provider, str) or provider not in WORK_ITEM_PROVIDERS:
            raise TelemetryError("provider must be github, plane, or jira")
        if not isinstance(outcome, str) or outcome not in WORK_ITEM_OUTCOMES:
            raise TelemetryError("unknown work-item disposition")
        if not isinstance(source, str) or source not in WORK_ITEM_SOURCES:
            raise TelemetryError("source must be provider_query or explicit")
        cycle_ids = self._work_item_reference_list(cycle_ids, "cycle_id")
        pull_request_ids = self._work_item_reference_list(pull_request_ids, "pull_request_id")
        merged_pr_ids = self._work_item_reference_list(merged_pr_ids, "merged_pr_id")
        resolving_pr_ids = self._work_item_reference_list(resolving_pr_ids, "resolving_pr_id")
        reverted_pr_ids = self._work_item_reference_list(reverted_pr_ids, "reverted_pr_id")
        if outcome != "reverted" and reverted_pr_ids:
            raise TelemetryError("reverted pull requests require the reverted outcome")
        observed = self._timestamp(observed_at)
        recorded = self._timestamp()
        with closing(self._connect()) as connection:
            previous = connection.execute(
                "SELECT * FROM work_item_disposition_events "
                "WHERE repo_id = ? AND provider = ? AND external_repository = ? "
                "AND work_item_id = ? "
                "ORDER BY observed_at DESC, recorded_at DESC, event_id DESC LIMIT 1",
                (repo_id, provider, repository, work_item_id),
            ).fetchone()
            if previous:
                current = self._decode_work_item_disposition(previous)
                if observed < current["observed_at"]:
                    return {"created": False, "reason": "stale", "event": current}
                explicit_resolver_unchanged = (
                    source != "explicit" or
                    set(resolving_pr_ids).issubset(set(current["resolving_pr_ids"]))
                )
                if (outcome == current["outcome"] and
                        set(cycle_ids).issubset(set(current["cycle_ids"])) and
                        set(pull_request_ids).issubset(set(current["pull_request_ids"])) and
                        set(merged_pr_ids).issubset(set(current["merged_pr_ids"])) and
                        set(reverted_pr_ids).issubset(set(current["reverted_pr_ids"])) and
                        explicit_resolver_unchanged):
                    return {"created": False, "reason": "unchanged", "event": current}
                cycle_ids = list(dict.fromkeys(current["cycle_ids"] + cycle_ids))
                pull_request_ids = list(dict.fromkeys(current["pull_request_ids"] + pull_request_ids))
                merged_pr_ids = list(dict.fromkeys(current["merged_pr_ids"] + merged_pr_ids))
                reverted_pr_ids = list(dict.fromkeys(current["reverted_pr_ids"] + reverted_pr_ids))
                if outcome == current["outcome"]:
                    # A newly merged PR is a transition for that PR, but it
                    # must not rewrite the resolving-PR snapshot of an item.
                    # A person may explicitly associate an additional PR later.
                    if source == "explicit" and outcome == "resolved":
                        resolving_pr_ids = list(dict.fromkeys(
                            current["resolving_pr_ids"] + resolving_pr_ids
                        ))
                    else:
                        resolving_pr_ids = current["resolving_pr_ids"]
                elif outcome in {"reopened", "reverted"}:
                    if outcome == "reverted" and current["outcome"] not in {"resolved", "reverted"}:
                        raise TelemetryError("only a currently resolved work item can be marked reverted")
                    resolving_pr_ids = current["resolving_pr_ids"]
            elif outcome == "reverted":
                raise TelemetryError("a reverted outcome needs a previously resolved work item")
            if not set(resolving_pr_ids).issubset(pull_request_ids):
                raise TelemetryError("resolving pull requests must also be linked pull requests")
            if not set(merged_pr_ids).issubset(pull_request_ids):
                raise TelemetryError("merged pull requests must also be linked pull requests")
            if outcome == "pr_merged" and not merged_pr_ids:
                raise TelemetryError("pr_merged requires at least one linked PR observed as merged")
            if outcome == "reverted":
                if not reverted_pr_ids:
                    raise TelemetryError("a reverted outcome must name the resolving pull request that was reverted")
                if not set(reverted_pr_ids).issubset(resolving_pr_ids):
                    raise TelemetryError("only resolving pull requests can revoke work-item resolution")
                if not set(reverted_pr_ids).issubset(merged_pr_ids):
                    raise TelemetryError("only merged pull requests can be recorded as reverted")
            cursor = connection.execute(
                "INSERT INTO work_item_disposition_events "
                "(schema_version, repo_id, provider, external_repository, work_item_id, outcome, source, "
                " observed_at, recorded_at, cycle_ids, pull_request_ids, merged_pr_ids, resolving_pr_ids, "
                " reverted_pr_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (SCHEMA_VERSION, repo_id, provider, repository, work_item_id, outcome, source,
                 observed, recorded, json.dumps(cycle_ids), json.dumps(pull_request_ids),
                 json.dumps(merged_pr_ids), json.dumps(resolving_pr_ids), json.dumps(reverted_pr_ids)),
            )
            connection.commit()
            event = connection.execute(
                "SELECT * FROM work_item_disposition_events WHERE event_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return {"created": True, "event": self._decode_work_item_disposition(event)}

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
        attempts = self.dispatch_attempts(repo_id, task_id=rows[0]["task_id"], cycle_id=cycle_id)
        return {
            "cycle_id": cycle_id,
            "task_id": rows[0]["task_id"],
            "closed": bool(closing_rows),
            "outcome": fields(closing_rows[-1], OUTCOME_FIELDS["cycle"]) if closing_rows else {},
            "dispatches": [
                {"stage_seq": row["payload"].get("stage_seq"), "role": row["role"],
                 "profile": row["profile"], "outcome": row["outcome"],
                 "used_fallback": bool(row["used_fallback"]),
                 "dispatch_attempt_id": row["payload"].get("dispatch_attempt_id"),
                 "execution_variant_id": row["payload"].get("execution_variant_id"),
                 "harness_snapshot_id": row["payload"].get("harness_snapshot_id")}
                for row in rows if row["payload"].get("record_kind") == "dispatch"
            ],
            "attempts": attempts,
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
