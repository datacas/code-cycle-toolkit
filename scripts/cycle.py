"""Run a stage and record it, in one call that cannot do only half.

The telemetry store knows how to hold a row. Nothing was writing one. The
obvious fix — instructions telling the orchestrator to call `record_stage` after
each dispatch — is the shape of contract that has failed repeatedly in this
codebase: a promise stated somewhere, enforced nowhere, discovered dead months
later when the data it should have produced is missing.

So the recorder does not ask to be remembered. `stage()` routes, dispatches and
writes the row as one operation. There is no path through it that dispatches
without recording, because the recording is not a step the caller performs.

Two things it deliberately does not do.

**It does not decide anything.** `router` chooses, `executors` runs, and this
observes. Neither of those modules learns that SQLite exists; a decision engine
that writes to a database is one that cannot be tested as a decision engine.

**It does not hide the rerouting.** Eligible failures can use a stage-local
fallback once, after exit proof and effect reconciliation. Both decisions and
the evidence behind the reroute are recorded; later stages retain their own
availability. A fallback whose first attempt left no trace makes retries look free.

Friction a person must clear is the other half of that rule, and the one easier
to get wrong: a sign-in screen is evidence about availability too, so "we
learned something" is not a usable test for whether to go around it. The recorder asks the narrower question instead, and a
dispatch waiting on a human stops where it is, unrerouted and with its
availability untouched, so the question stays visible.
"""

from __future__ import annotations

import time
import uuid
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import partial
from typing import Any, cast

from cycle_journal import Journal, redact
from execution_plan import RoleContract, role_contracts
from executors import (
    Availability,
    DispatchOutcome,
    DispatchResult,
    ReadinessPolicy,
    Registry,
    WorkspacePolicy,
    dispatch,
)
from harness import build_harness_snapshot
from grants import (
    ALLOW_CYCLE, ALLOW_PERSISTENT, ALLOW_GLOBAL, DENY, PENDING, ConsentBroker, GRANTS,
    denial_context, granted_context,
)
from environment_recovery import (
    ENVIRONMENT_RECOVERY_GUIDANCE, denied_docker_blocker, recovery_context, sandbox_blocker,
    observed_sandbox_blocker, denied_result,
)
from reroute import effect_snapshot, reconcile
from quota_state import Handoffs
from jev_shadow import JevShadow, telemetry_fields as shadow_fields
from router import (
    ProfileSelector,
    RoutingDecision,
    RoutingMode,
    RoutingStrategy,
    Target,
    TaskSignals,
    escalation_selector,
    models_from_profiles,
    stage_fallback,
    route,
    rule_selector,
)
from stage_signals import CHANGE_OBSERVED_ROLES, RESOLUTION_ROLES, ChangeSignals
from native_prerequisites import known_failure
from telemetry import (
    AWAITING_CI_STATUS, CI_PASSED, CI_POLICIES, CYCLE_STARTS, DEFINITIVE_FAILURES,
    Telemetry, TelemetryError, WORK_ITEM_PROVIDERS, routing_decision_fields,
    validate_reference,
)

SCHEMA_VERSION = 1

#: Roles whose functional verdict is a review outcome rather than a dispatch one.
REVIEW_ROLES = frozenset({"review", "rereview"})

#: Verdict fields that become the next stage's `prior_*` signals.
FINDING_FIELDS = (
    "findings_total", "findings_blocking", "findings_critical", "findings_high",
    "findings_medium", "findings_low",
)

#: Review statuses that settle a round. Anything else leaves the review outcome
#: unknown rather than failed.
TERMINAL_REVIEW_STATUSES = frozenset({"APPROVED", "CHANGES_REQUESTED"})

#: A review that completed with nothing blocking. Under the parallel CI policy
#: it becomes an approval only once the CI gate passes the same SHA.
CLEAN_REVIEW_STATUSES = frozenset({"APPROVED", AWAITING_CI_STATUS})

#: Failures a stage-local fallback may absorb, once, after exit proof.
TRANSIENT_FAILURES = frozenset({"capacity", "transport", "timeout", "quota"})

#: `CodexAdapter.probe_git_metadata_write`: whether a linked worktree's Git
#: metadata is writable, and why not.
GitMetadataProbe = Callable[[Target, str, tuple[str, ...]], tuple[bool, str]]

ROLE_CONTRACTS = role_contracts()
DEFAULT_ROLE_CONTRACT = RoleContract(WorkspacePolicy.READ_ONLY)

def role_contract(role: str) -> RoleContract:
    """Return a role's contract, defaulting unknown roles to least privilege."""
    return ROLE_CONTRACTS.get(role, DEFAULT_ROLE_CONTRACT)


#: How each publication permission is described to the agent.
PUBLICATION_OPERATIONS = {
    "comment": "comment on the work item or change request of this cycle",
    "create_pr": "create the change request for the working branch",
    "push_branch": "push the working branch",
}

#: Forbidden in every stage, whatever its contract allows.
PUBLICATION_PROHIBITIONS = (
    "Never merge, force-push, delete remote refs, push to or otherwise modify "
    "the base branch, close an issue or change request unless explicitly "
    "instructed, or publish anything that belongs to another stage."
)


def publication_policy(role: str, publication_permissions: tuple[str, ...]) -> str:
    """The stage's publication rules, stated to the agent.

    On a GitHub cycle (#142) the rules are also enforced for native Claude
    dispatches: each stage receives only its closed command table
    (`stage_permissions`), derived from these contracts, and a hook denies any
    other Bash command. Codex stages are confined by their sandbox profile and
    run with `--ignore-rules`, which filters files and network, not commands.
    Elsewhere — another code host or issue provider, or an Orca worker — the
    agent keeps its normal `gh`, `git` and network access, and this prompt is
    the boundary. Published comments and telemetry are how a person audits
    that it was followed either way.
    """
    if not publication_permissions:
        return (f"Publication policy for this {role} stage: it publishes nothing. "
                "Do not comment, push, create a change request, or change any "
                f"remote state. {PUBLICATION_PROHIBITIONS}")
    allowed = "; ".join(PUBLICATION_OPERATIONS[name]
                        for name in publication_permissions)
    denied = [text for name, text in PUBLICATION_OPERATIONS.items()
              if name not in publication_permissions]
    rule = f"Publication policy for this {role} stage: you may {allowed}."
    if denied:
        rule += f" You may not {'; '.join(denied)}."
    rule += (
        " For a GitHub code host, when `gh` is authenticated, use `gh` for every "
        "read and write on this change request; use a GitHub connector or MCP "
        "tool only when `gh` is unavailable. If a GitHub publication attempt "
        "returns HTTP 403 or 404 through another tool, retry once with `gh` "
        "before reporting `BLOCKED`, and name the failed tool in that report. "
        "For Bitbucket, use its configured tooling."
    )
    return f"{rule} {PUBLICATION_PROHIBITIONS}"


def routing_context(decision: RoutingDecision) -> str:
    """The routing decision this attempt runs under, stated to the agent.

    A run line records the profile, the requested model and the effort. An
    agent left to name them answers from its own configuration, which is the
    very thing a run line exists to audit, so the runtime supplies them. The
    resolved model is not stated: only the executor can report it, afterwards.
    """
    target = decision.target
    line = (f"Routing for this stage, decided by the runtime: profile "
            f"`{decision.profile}`, requested model "
            f"`{target.provider}/{target.model}`, effort `{target.effort}`. "
            "Copy these values verbatim into any run line you write; do not "
            "infer them from your own configuration. Write the resolved model "
            "as `?`, because the executor has not reported it to you.")
    route_note = next((reason for reason in decision.reasons
                       if reason.startswith("Codex linked-worktree Git metadata probe failed:")), None)
    return f"{line} {route_note}" if route_note else line


class CycleError(ValueError):
    """The recorder was asked for something inconsistent."""


class CycleInterrupted(BaseException):
    """The process running the cycle was told to stop: SIGTERM, SIGHUP, SIGINT.

    A `BaseException`, like `KeyboardInterrupt`, so that no `except Exception`
    on the way up can read it as a stage failure and carry on. When it arrives
    during a dispatch, `CycleRecorder.stage()` records that stage as
    interrupted and attaches it here as `outcome` before letting it continue.
    """

    def __init__(self, signal_name: str) -> None:
        super().__init__(signal_name)
        self.signal_name = signal_name
        self.outcome: StageOutcome | None = None


@dataclass
class StageOutcome:
    """What a stage did, including any decision that was abandoned on the way."""

    role: str
    decision: RoutingDecision
    result: DispatchResult | None
    attempts: list[tuple[RoutingDecision, DispatchResult]] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)

    @property
    def rerouted(self) -> bool:
        return len(self.attempts) > 1

    @property
    def succeeded(self) -> bool:
        return self.result is not None and self.result.outcome is DispatchOutcome.SUCCEEDED


@dataclass
class _StageRun:
    """One `stage()` call: what it was asked, and what one attempt leaves the next."""

    role: str
    #: The prompt, with the stage's publication policy and recovery guidance.
    #: A quota handoff appends its resume note once, for every later attempt.
    task: str
    escalated: bool
    resolve_escalated: bool
    #: The effort a resolve primary is pinned to; unused by other roles.
    resolve_effort: str
    target_override: Target | None
    target_override_used_fallback: bool
    workspace_policy: WorkspacePolicy
    writes: bool
    publishes: bool
    #: The caller's keyword arguments for `executors.dispatch`, which hands
    #: them to the adapter as its own untyped keywords.
    dispatch_kwargs: dict[str, Any]
    #: Signals that belong to the stage, not the attempt, such as a resolve
    #: escalation's acceptance; every attempt's signals start from them.
    stage_signals: dict[str, object]
    #: Observed once, before the first routing: the diff does not change
    #: between an attempt and its reroute.
    change: ChangeSignals | None
    parent_attempt_id: str | None = None
    parent_executor: str | None = None
    #: The decision the next attempt runs under, when this one chose it.
    fallback_decision: RoutingDecision | None = None
    #: Why the previous attempt was rerouted, carried into the next one's row.
    parent_failure: dict[str, object] = field(default_factory=dict)
    environment_context: str = ""
    consent_requested: bool = False
    denial_affected: set[str] = field(default_factory=set)
    #: Codex's pre-dispatch Compose consents (#398), compared after the run.
    compose_consents: list[dict[str, Any]] = field(default_factory=list)
    #: The sandbox grants the latest attempt was dispatched with.
    sandbox_grants: tuple[str, ...] = ()
    #: The first routing and its signals, for the shadow.
    first: tuple[RoutingDecision, dict[str, object]] | None = None
    decision: RoutingDecision | None = None
    result: DispatchResult | None = None
    attempts: list[tuple[RoutingDecision, DispatchResult]] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)

    @property
    def selector(self) -> ProfileSelector:
        return escalation_selector if self.escalated else rule_selector

    def outcome(self) -> StageOutcome:
        assert self.decision is not None, "every stage ends with a routing decision"
        return StageOutcome(self.role, self.decision, self.result, self.attempts, self.rows)


@dataclass
class _Attempt:
    """One dispatch attempt within a stage, from its routing to its row."""

    number: int
    #: This attempt's row: pre-routing signals, then what the attempt showed.
    signals: dict[str, object]
    eligible: frozenset[str]
    decision: RoutingDecision
    #: A quota handoff record this attempt resumes, when it resumes one.
    handoff: dict[str, Any] | None = None
    relationship: str | None = None
    started: float = 0.0
    attempt_id: str | None = None
    tool_activity: bool = False
    #: The effect snapshot taken before the dispatch.
    before: dict[str, Any] | None = None
    #: Grants already denied when this attempt was dispatched.
    denied_before: frozenset[str] = frozenset()
    blocker: dict[str, Any] | None = None
    known_prerequisite: bool = False
    retry: bool = False

    @property
    def target(self) -> Target:
        assert self.decision.target is not None, "only a routed attempt is dispatched"
        return self.decision.target


class CycleRecorder:
    """One work item's journey, observed as it happens.

    Construct it once per task, call `stage()` for each dispatch and
    `record_verdict()` for each functional outcome, and `close()` at the end.
    """

    def __init__(
        self,
        telemetry: Telemetry,
        repo_id: str,
        task_id: str,
        signals: TaskSignals,
        *,
        availability: dict[str, Availability],
        registry: Registry | None = None,
        mode: RoutingMode = RoutingMode.PRODUCTION,
        policy: ReadinessPolicy | None = None,
        probes: dict | None = None,
        profiles: dict | None = None,
        routing_strategy: RoutingStrategy = RoutingStrategy.FIXED,
        local_only: bool = False,
        continue_work: bool = False,
        started_from: str = "implement",
        change_observer: Callable[[], ChangeSignals | None] | None = None,
        verification_available: bool | None = None,
        cycle_id: str | None = None,
        work_item_provider: str | None = None,
        work_item_repository: str | None = None,
        shadow: JevShadow | None = None,
        stage_started: Callable[[str, RoutingDecision], None] | None = None,
        on_progress: Callable[..., None] | None = None,
        skills_by_role: dict[str, str] | None = None,
        change_request_id: str | None = None,
        resolve_effort: str | None = None,
        ci_policy: str = "strict",
        consent: ConsentBroker | None = None,
        journal: Journal | None = None,
    ) -> None:
        self.telemetry = telemetry
        # The cycle journal (#306): stage timings, failures and refusals as they
        # happen. Best effort; nothing here reads it back to decide anything.
        self.journal = journal
        # One per run, so two runs of the same work item stay apart. Checked
        # now rather than on the first row: a refused id found after a paid
        # dispatch leaves a stage nobody can record.
        self.cycle_id = cast(str, validate_reference(
            "cycle_id", cycle_id or f"cycle-{uuid.uuid4().hex}"))
        self.repo_id = repo_id
        self.task_id = task_id
        if (work_item_provider is None) != (work_item_repository is None):
            raise CycleError("work item provider and repository must be supplied together")
        if work_item_provider is not None:
            if work_item_provider not in WORK_ITEM_PROVIDERS:
                raise CycleError("unknown work item provider")
            if not work_item_repository:
                raise CycleError("work item repository must be a short opaque reference")
            try:
                work_item_repository = Telemetry._work_item_reference(
                    work_item_repository, "work_item_repository"
                )
            except TelemetryError as error:
                raise CycleError(str(error)) from error
        self.work_item_identity_fields = (
            {"work_item_provider": work_item_provider,
             "work_item_repository": work_item_repository}
            if work_item_provider is not None else {}
        )
        self.signals = signals
        self.availability = dict(availability)
        self.quota_exhaustions: list[dict] = []
        self.exhausted_until: dict[tuple[str, str], str] = {}
        if mode is RoutingMode.PRODUCTION:
            try:
                self.quota_exhaustions = telemetry.active_quota_exhaustions(repo_id)
                self.exhausted_until = {
                    (row["executor"], row["model"]): row["expires_at"] or "operator retry required"
                    for row in self.quota_exhaustions
                }
            except (OSError, sqlite3.Error, TelemetryError, ValueError):
                # Quota persistence is an optimization; a telemetry outage never
                # prevents the selected cycle from running.
                self.quota_exhaustions = []
                self.exhausted_until = {}
        self.registry = registry or Registry()
        self.mode = mode
        self.policy = policy or ReadinessPolicy.for_mode(mode)
        # What the probes found, kept for the whole cycle. Without it every
        # dispatch probes again on its own, so an availability could change
        # between two stages with nothing recording that it had — and the
        # decisions on either side of the change become unexplainable.
        self.probes = probes
        self.skills_by_role = dict(skills_by_role or {})
        self.change_request_id = change_request_id
        self.resolve_effort = resolve_effort or "high"
        if self.resolve_effort not in {"low", "medium", "high", "max"}:
            raise CycleError("resolve effort must be one of low, medium, high, max")
        if ci_policy not in CI_POLICIES:
            raise CycleError(f"the CI policy must be one of {', '.join(CI_POLICIES)}")
        self.ci_policy = ci_policy
        # Already resolved by whoever loaded the repository's configuration.
        # The recorder does not go looking for a file: a component that reads
        # configuration on its own is one that can disagree with the caller
        # about what the configuration says.
        self.profiles = profiles
        self.routing_strategy = routing_strategy
        # Sensitive sandbox grants are decided by a person, never by a stage.
        self.consent = consent
        if self.consent is not None:
            self.consent.cycle_id = self.consent.cycle_id or self.cycle_id
            self.consent.task_id = self.task_id
        self.first_pass_rate = (
            telemetry.first_pass_rate(self.repo_id)
            if routing_strategy is RoutingStrategy.MEASURED else None
        )
        if profiles is not None:
            telemetry.add_known_models(self.repo_id, models_from_profiles(profiles))
        self.local_only = local_only
        # Where this cycle began. Recorded on every row, because a cycle that
        # resumed an existing change request reviews an earlier run's work and
        # must not be read as that implementation's first pass.
        if started_from not in CYCLE_STARTS:
            raise CycleError(
                f"a cycle starts at one of {', '.join(CYCLE_STARTS)}, not {started_from!r}")
        self.started_from = started_from
        # Pre-routing state. Each is what the cycle knew before routing the
        # next stage, never what that stage went on to produce; `None` or an
        # empty mapping is "not known", which is recorded by omission.
        self.change_observer = change_observer
        self.verification_available = verification_available
        self.failed_attempts = 0
        # How many findings survived a claimed fix, as last evaluated. Set by
        # the driver that reads the structured results; `None` is a recorder
        # whose caller has not evaluated it, and is left off every row. Each
        # later dispatch carries it as a pre-routing signal and the closing
        # row keeps the last value, so a survival found by the final rereview
        # is not lost when no dispatch follows it. Not `failed_attempts`: that
        # counts dispatches, not fixes.
        self.repeated_findings: int | None = None
        self.prior_findings: dict[str, int] = {}
        self.iteration = 0
        self.stages: list[StageOutcome] = []
        # Correlation and observed outcomes. Kept apart from the pre-routing
        # state above, and written only on verdict and closing rows.
        self.stage_seq = 0
        self._latest_seq: dict[str, int] = {}
        self.first_review_status: str | None = None
        self.final_review_status: str | None = None
        # The SHA each of those reviews completed on, and what the CI gate made
        # of each SHA it waited for (#228). Under the parallel policy a clean
        # review is an approval only with a CI pass of its own SHA.
        self.first_review_head: str | None = None
        self.final_review_head: str | None = None
        self.ci_outcomes: dict[str, str] = {}
        self.final_ci_status: str | None = None
        self.ci_polls = 0
        self.ci_wait_ms = 0
        self.ci_recorded = False
        # A review of a head that already had a clean review in this run. The
        # CI gate exists so that this stays zero; it is measured to show it.
        self.clean_review_heads: set[str] = set()
        self.redundant_reviews = 0
        self.reviews_measured = False
        # Set by the driver when a resume reused a clean review's receipt
        # instead of dispatching one; `None` is a cycle that could not have.
        self.review_reused: bool | None = None
        # `manual` when the cycle closed on a manual rereview (#346).
        self.review_provenance: str | None = None
        self.tests_passed: bool | None = None
        self.tests_basis: str | None = None
        self.verification: str | None = None
        self.boundary_verified: bool | None = None
        # The change signals the latest stage was routed knowing, kept so its
        # verdict can apply the boundary rule. `None` when nothing was observed.
        self.latest_change: ChangeSignals | None = None
        # An observer of the rules' choice, never a selector: nothing it
        # returns reaches `route()` or a dispatch. `None` means no shadow at
        # all, so a disabled one costs no client, no key read and no I/O.
        self.shadow = shadow
        self.stage_started = stage_started
        self.on_progress = on_progress
        self.on_workspace: Callable[..., None] | None = None
        # Called at the adapter invocation boundary, after routing/preflight.
        self.on_dispatch_started: Callable[[], None] | None = None

    def stage(self, role: str, task: str, *, escalated: bool = False,
              effort_override: str | None = None, target_override: Target | None = None,
              target_override_used_fallback: bool = False,
              resolve_escalated: bool = False,
              escalation_finding_ids: tuple[str, ...] = (),
              **dispatch_kwargs) -> StageOutcome:
        """Route, dispatch and record. One call, no half-done state.

        Returns the outcome rather than raising on a blocked dispatch: a stage
        that could not run is a fact the caller has to act on, and it has
        already been written down by the time this returns.

        `escalated` routes a role to its escalation profile instead of its rule
        and records that it did, as a pre-routing signal: the reason for the
        stronger profile was known before this routing.

        The steps are separate methods, each with one job: `_prepare_stage`
        checks the stage contract, `_route_attempt` routes, `_dispatch_attempt`
        runs the executor, `_settle_attempt` reads what the attempt showed and
        decides whether another attempt follows, and `_persist_attempt` writes
        its row. This method is their only caller, so every attempt it starts
        ends in a recorded row, whichever way it ends.
        """
        run = self._prepare_stage(
            role, task, escalated=escalated, effort_override=effort_override,
            target_override=target_override,
            target_override_used_fallback=target_override_used_fallback,
            resolve_escalated=resolve_escalated,
            escalation_finding_ids=escalation_finding_ids,
            dispatch_kwargs=dispatch_kwargs,
        )
        # A primary, its fallback, and one rerun after a permission answer.
        for number in range(3):
            attempt = self._route_attempt(run, number)
            if attempt.decision.blocked:
                self._record_blocked(run, attempt)
                break
            result = self._dispatch_attempt(run, attempt)
            result = self._settle_attempt(run, attempt, result)
            self._persist_attempt(run, attempt, result)
            if not attempt.retry:
                break
            run.parent_attempt_id = attempt.attempt_id
            run.parent_executor = attempt.target.executor
        return self._finish_stage(run)

    # -- preparation ---------------------------------------------------------

    def _prepare_stage(self, role: str, task: str, *, escalated: bool,
                       effort_override: str | None, target_override: Target | None,
                       target_override_used_fallback: bool, resolve_escalated: bool,
                       escalation_finding_ids: tuple[str, ...],
                       dispatch_kwargs: dict[str, Any]) -> _StageRun:
        """Hold the stage to its role's contract and open its run."""
        contract = role_contract(role)
        workspace_policy = contract.workspace_policy
        writes = workspace_policy in {
            WorkspacePolicy.WORKSPACE_WRITE, WorkspacePolicy.DISPOSABLE,
        }
        publishes = contract.publishes and not self.local_only
        publication_permissions = (
            contract.publication_permissions if publishes else ()
        )
        self._enforce_contract(role, dispatch_kwargs, writes=writes, publishes=publishes,
                               publication_permissions=publication_permissions,
                               workspace_policy=workspace_policy)
        task = (f"{task}\n\n{publication_policy(role, publication_permissions)}"
                f"\n\n{ENVIRONMENT_RECOVERY_GUIDANCE}")
        resolve_effort = effort_override or self.resolve_effort
        stage_signals: dict[str, object] = {}
        if role == "resolve":
            if resolve_effort not in {"low", "medium", "high", "max"}:
                raise CycleError("resolve effort must be one of low, medium, high, max")
            stage_signals = {
                "escalated": bool(resolve_escalated),
                "escalation_finding_ids": list(escalation_finding_ids),
                "escalation_acceptance": "unknown" if resolve_escalated else "not_applicable",
            }
        self.stage_seq += 1
        self._latest_seq[role] = self.stage_seq
        change = (
            self.change_observer()
            if self.change_observer is not None and role in CHANGE_OBSERVED_ROLES
            else None
        )
        self.latest_change = change
        return _StageRun(
            role=role, task=task, escalated=escalated,
            resolve_escalated=resolve_escalated, resolve_effort=resolve_effort,
            target_override=target_override,
            target_override_used_fallback=target_override_used_fallback,
            workspace_policy=workspace_policy, writes=writes, publishes=publishes,
            dispatch_kwargs=dispatch_kwargs, stage_signals=stage_signals, change=change,
        )

    @staticmethod
    def _enforce_contract(role: str, dispatch_kwargs: dict[str, Any], *, writes: bool,
                          publishes: bool, publication_permissions: tuple[str, ...],
                          workspace_policy: WorkspacePolicy) -> None:
        """Refuse a caller's override of what the role's contract fixes."""
        requested_writes = dispatch_kwargs.pop("writes", writes)
        if requested_writes is not writes:
            raise CycleError(
                f"{role!r} stages must use writes={writes}, not {requested_writes!r}")
        dispatch_kwargs["writes"] = writes
        requested_publishes = dispatch_kwargs.pop("publishes", publishes)
        if requested_publishes is not publishes:
            raise CycleError(
                f"{role!r} stages must use publishes={publishes}, not {requested_publishes!r}")
        dispatch_kwargs["publishes"] = publishes
        requested_publication_permissions = dispatch_kwargs.pop(
            "publication_permissions", publication_permissions,
        )
        if requested_publication_permissions != publication_permissions:
            raise CycleError(
                f"{role!r} stages must use publication_permissions="
                f"{publication_permissions!r}, not {requested_publication_permissions!r}"
            )
        dispatch_kwargs["publication_permissions"] = publication_permissions
        requested_policy = dispatch_kwargs.pop("workspace_policy", workspace_policy)
        try:
            requested_policy = WorkspacePolicy(requested_policy)
        except ValueError as exc:
            raise CycleError(
                f"{role!r} stages must use workspace_policy={workspace_policy.value!r}, "
                f"not {requested_policy!r}") from exc
        if requested_policy is not workspace_policy:
            raise CycleError(
                f"{role!r} stages must use workspace_policy={workspace_policy.value!r}, "
                f"not {requested_policy!r}")
        dispatch_kwargs["workspace_policy"] = workspace_policy

    # -- routing -------------------------------------------------------------

    def _route_attempt(self, run: _StageRun, number: int) -> _Attempt:
        """Route one attempt: the rule, then each override that may replace it."""
        signals: dict[str, object] = {
            **self._pre_routing(run.role, run.change), **run.stage_signals,
            **run.parent_failure,
        }
        if run.escalated:
            signals["escalated"] = True
        eligible = self.registry.compatible_executors(
            run.workspace_policy, **run.dispatch_kwargs,
        )
        decision = self._route(run, eligible, signals)
        if run.role == "resolve" and not decision.blocked and decision.target is not None:
            decision = self._resolve_target(run, decision, decision.target, eligible)
        if run.fallback_decision is not None:
            decision = run.fallback_decision
        decision = self._check_codex_effort(run, decision, eligible, signals)
        decision, eligible = self._check_codex_git_metadata(run, decision, eligible, signals)
        if number == 0:
            run.first = (decision, signals)
        attempt = _Attempt(number=number, signals=signals, eligible=eligible,
                           decision=decision)
        self._match_handoff(run, attempt)
        return attempt

    def _route(self, run: _StageRun, candidates: frozenset[str],
               signals: dict[str, object]) -> RoutingDecision:
        rate = self.first_pass_rate
        decision = route(
            run.role, self.signals, self.availability,
            mode=self.mode, profiles=self.profiles,
            eligible_executors=candidates,
            strategy=self.routing_strategy,
            selector=run.selector,
            first_pass_rate=rate.value if rate is not None and rate.known else None,
            rate_observations=rate.observations if rate is not None else None,
            rate_minimum=rate.minimum if rate is not None else None,
            rate_known=rate.known if rate is not None else None,
            rate_explanation=rate.explain() if rate is not None else None,
            exhausted_until=self.exhausted_until,
        )
        if (self.mode is RoutingMode.PRODUCTION
                and decision.used_fallback and decision.target is not None
                and any(f"{row['executor']}/{row['model']} quota exhausted until "
                        f"{row['expires_at'] or 'operator retry required'}" in decision.reasons
                        for row in self.quota_exhaustions)):
            signals["quota_resume_reason"] = "recorded_quota_exhaustion"
        return decision

    def _dispatchable(self, executor: str, eligible: frozenset[str]) -> bool:
        return (executor in eligible
                and self.availability.get(executor, Availability.UNKNOWN).dispatchable)

    def _resolve_target(self, run: _StageRun, decision: RoutingDecision, target: Target,
                        eligible: frozenset[str]) -> RoutingDecision:
        """A resolve's effort: pinned, escalated, or the previous target's."""
        override = run.target_override
        if override is not None and (override.executor, override.model) in self.exhausted_until:
            return replace(decision, reasons=decision.reasons +
                           ("previous resolve target has recorded quota exhaustion; keep the available route",))
        if override is not None:
            if not self._dispatchable(override.executor, eligible):
                return replace(
                    decision, target=None, blocked=True,
                    reasons=decision.reasons +
                    ("the previous resolve target is no longer available",),
                )
            return replace(
                decision,
                target=replace(override, effort="max"),
                used_fallback=run.target_override_used_fallback,
                reasons=decision.reasons +
                ("a finding survived a claimed fix; retry at max on the previous target",),
            )
        if decision.used_fallback:
            return decision
        if run.resolve_escalated:
            # A resumed run has no previous resolve target; the seeded
            # history still escalates, on the routed primary.
            return replace(
                decision, target=replace(target, effort="max"),
                reasons=decision.reasons +
                ("a finding survived a claimed fix; retry at max on the routed primary",),
            )
        return replace(
            decision, target=replace(target, effort=run.resolve_effort),
            reasons=decision.reasons +
            (f"resolve primary effort pinned to {run.resolve_effort}",),
        )

    def _check_codex_effort(self, run: _StageRun, decision: RoutingDecision,
                            eligible: frozenset[str],
                            signals: dict[str, object]) -> RoutingDecision:
        """Use the fallback when Codex's catalog says it cannot run at max.

        The Codex catalog is an optional pre-dispatch capability check.
        Missing catalogs and unlisted models remain unknown and dispatch.
        """
        target = decision.target
        if (run.role != "resolve" or decision.blocked or target is None
                or target.executor != "codex" or target.effort != "max"):
            return decision
        try:
            from profile_config import codex_models
            catalog = codex_models()
        except Exception:
            catalog = None
        supported = catalog.get(target.model) if catalog else None
        if supported is None or target.effort in supported:
            return decision
        if run.resolve_escalated:
            run.stage_signals["escalation_acceptance"] = "unsupported"
            signals["escalation_acceptance"] = "unsupported"
        fallback = stage_fallback(decision, self.profiles)
        if fallback is None or not self._dispatchable(fallback.executor, eligible):
            return replace(
                decision, target=None, blocked=True,
                reasons=decision.reasons +
                (f"Codex does not support effort {target.effort}; "
                 "the configured fallback is unavailable",),
            )
        run.fallback_decision = replace(
            decision, target=fallback, used_fallback=True,
            reasons=decision.reasons +
            ("Codex model cache confirms the requested effort is unsupported; using the configured fallback",),
        )
        return run.fallback_decision

    def _check_codex_git_metadata(
        self, run: _StageRun, decision: RoutingDecision, eligible: frozenset[str],
        signals: dict[str, object],
    ) -> tuple[RoutingDecision, frozenset[str]]:
        """Route away from Codex before dispatch when it cannot write Git metadata."""
        target = decision.target
        if not (run.publishes and run.writes and not decision.blocked
                and target is not None and target.executor == "codex"
                and run.dispatch_kwargs.get("writable_dirs")):
            return decision, eligible
        # Only the Codex adapter defines this probe; another adapter registered
        # under that name may not.
        probe_write = cast("GitMetadataProbe | None", getattr(
            self.registry.get("codex"), "probe_git_metadata_write", None))
        if not callable(probe_write):
            return decision, eligible
        allowed, detail = probe_write(
            target, run.dispatch_kwargs["cwd"], tuple(run.dispatch_kwargs["writable_dirs"]),
        )
        if allowed:
            return decision, eligible
        eligible = frozenset(name for name in eligible
                             if name != "codex" and self._publishes_from(name))
        decision = self._route(run, eligible, signals)
        note = ("Codex linked-worktree Git metadata probe failed: "
                f"{detail}; routed to "
                f"{decision.target.executor if decision.target else 'no available executor'} "
                "before dispatch.")
        run.fallback_decision = None
        return replace(decision, reasons=decision.reasons + (note,)), eligible

    def _publishes_from(self, executor: str) -> bool:
        adapter = self.registry.get(executor)
        allowed, _ = adapter.publication_access(
            (self.probes or {}).get(executor) or adapter.probe(), writes=True,
        )
        return allowed

    def _match_handoff(self, run: _StageRun, attempt: _Attempt) -> None:
        """Find the quota handoff a resumed stage continues, or block it."""
        decision = attempt.decision
        if not (attempt.number == 0 and run.role == self.started_from
                and self.started_from != "implement"
                and self.mode is RoutingMode.PRODUCTION
                and run.dispatch_kwargs.get("cwd") and not decision.blocked):
            return
        try:
            from executors import _git_output, _workspace_fingerprint
            cwd = run.dispatch_kwargs["cwd"]
            if _git_output(cwd, "status", "--porcelain", "--untracked-files=all"):
                attempt.handoff = Handoffs(self.telemetry.path).match(
                    repo=self.repo_id, task=self.task_id, stage=run.role,
                    pr=self.change_request_id, workspace=_workspace_fingerprint(cwd),
                    destination=decision.target)
        except (OSError, ValueError, sqlite3.Error) as error:
            attempt.decision = replace(decision, blocked=True, target=None,
                                       reasons=decision.reasons + (str(error),))

    def _record_blocked(self, run: _StageRun, attempt: _Attempt) -> None:
        """A routing that reached no executor still leaves its row."""
        decision = attempt.decision
        run.decision = decision
        self._journal("failure", role=run.role, component="router",
                      status="blocked", reason="routing found no dispatchable target",
                      detail="; ".join(decision.reasons))
        run.rows.append(self._record(run.role, decision, None, attempt.signals))
        self.failed_attempts += 1

    def _finish_stage(self, run: _StageRun) -> StageOutcome:
        outcome = run.outcome()
        self.stages.append(outcome)
        assert run.first is not None, "a stage routes at least once"
        self._observe_shadow(run.role, *run.first)
        return outcome

    # -- dispatch ------------------------------------------------------------

    def _dispatch_attempt(self, run: _StageRun, attempt: _Attempt) -> DispatchResult:
        """Run the executor; an interruption is recorded before it continues."""
        decision, target = attempt.decision, attempt.target
        if self.stage_started is not None:
            self.stage_started(run.role, decision)
        # Per attempt: a reroute runs under the fallback's target, and its
        # run line has to name that one.
        attempt.started = time.monotonic()
        if run.parent_attempt_id is not None:
            attempt.relationship = (
                "fallback" if run.parent_executor != target.executor else "retry"
            )
        attempt.before = (self._effect_snapshot(run) if (run.writes or run.publishes)
                          and self.mode is not RoutingMode.CALIBRATION else None)
        if attempt.handoff is not None:
            self._claim_handoff(run, attempt, attempt.handoff)
        permission_context, grant_kwargs = self._permission_context(run, attempt)
        run.sandbox_grants = tuple(grant_kwargs.get("sandbox_grants", ()))
        try:
            return dispatch(decision, f"{run.task}\n\n{run.environment_context}\n\n{permission_context}\n\n{routing_context(decision)}",
                            self.registry,
                            policy=self.policy, probes=self.probes,
                            on_progress=partial(self._observe_progress, attempt),
                            on_workspace=self.on_workspace,
                            on_dispatch_attempt=partial(self._begin_dispatch_attempt, run, attempt),
                            **run.dispatch_kwargs, **grant_kwargs)
        except CycleInterrupted as interruption:
            self._record_interruption(run, attempt, interruption)
            raise

    def _effect_snapshot(self, run: _StageRun) -> dict[str, Any] | None:
        try:
            return effect_snapshot(
                run.dispatch_kwargs.get("cwd"), self.repo_id, publishes=run.publishes,
                change_request_id=self.change_request_id,
                issue_repo=self.work_item_identity_fields.get("work_item_repository"),
                issue_id=self.task_id if run.publishes else None,
            )
        except Exception:
            return None

    def _claim_handoff(self, run: _StageRun, attempt: _Attempt, handoff: dict[str, Any]) -> None:
        """Claim the handoff for this attempt and tell the agent what it resumes."""
        before = attempt.before
        try:
            if before is None or before["workspace"]["fingerprint"] != handoff["fingerprint"]:
                raise ValueError("quota handoff diff changed before dispatch")
            Handoffs(self.telemetry.path).claim(handoff)
        except (OSError, ValueError, sqlite3.Error) as error:
            raise CycleError(str(error)) from error
        run.task += (
            "\n\nQuota worktree resume: inspect the existing staged, unstaged and untracked "
            "changes before editing. Preserve correct partial work; do not reset or overwrite it. "
            f"Original executor: {handoff['original_executor']}; continuing executor: "
            f"{attempt.target.executor}; diff SHA-256: {handoff['fingerprint']}; "
            f"provenance: {handoff['provenance']}. This is not functional verification.")
        self._journal("stage_result", role=run.role, component="quota", status="handoff",
                      correlation={"original_executor": handoff["original_executor"],
                                   "destination_executor": attempt.target.executor,
                                   "fingerprint": handoff["fingerprint"],
                                   "provenance": handoff["provenance"]})

    def _permission_context(self, run: _StageRun,
                            attempt: _Attempt) -> tuple[str, dict[str, tuple[str, ...]]]:
        """The grants and denials this attempt runs with, stated to the agent."""
        executor = attempt.target.executor
        consent = self.consent
        granted = consent.granted(executor) if consent is not None else ()
        # This dispatch already ran with these denials; rerunning it with
        # the same denial context cannot change its outcome.
        attempt.denied_before = (frozenset(consent.denied_ids)
                                 if consent is not None else frozenset())
        context = ""
        if consent is not None:
            for grant_id in granted:
                scope = consent.registry[grant_id].scope_for(executor, consent.platform)
                if scope is not None:
                    context += "\n\n" + granted_context(consent.registry[grant_id], scope)
            context += "\n\n" + "\n".join(
                denial_context(consent.registry[grant_id]) for grant_id in consent.denied_ids)
            if run.dispatch_kwargs.get("command_policy") is not None:
                run.dispatch_kwargs["command_policy"] = replace(
                    run.dispatch_kwargs["command_policy"],
                    command_permissions=consent.command_entries(executor, run.role))
                if executor == "codex" and "docker_host" in granted:
                    # Codex has no command hook: its declared Compose checks get
                    # the same #409 consent before dispatch (#398).
                    from command_permissions import codex_compose_consent
                    compose_context, run.compose_consents = codex_compose_consent(
                        run.dispatch_kwargs["command_policy"], consent, executor, run.role,
                        run.dispatch_kwargs.get("cwd"))
                    if compose_context:
                        context += "\n\n" + compose_context
        sandbox_granted = tuple(grant_id for grant_id in granted if grant_id in GRANTS)
        return context, ({"sandbox_grants": sandbox_granted} if sandbox_granted else {})

    def _begin_dispatch_attempt(self, run: _StageRun, attempt: _Attempt) -> str:
        """Open the attempt's record at the adapter boundary, after preflight."""
        decision, target = attempt.decision, attempt.target
        harness_snapshot = build_harness_snapshot(
            role=run.role, skill=self.skills_by_role.get(run.role), profile=decision.profile,
            routing_strategy=getattr(
                decision.strategy, "value", self.routing_strategy.value,
            ),
            readiness_policy=self.policy.value,
            executor=target.executor,
            probe=(self.probes or {}).get(target.executor),
            profiles=self.profiles,
            project_root=run.dispatch_kwargs.get("cwd"),
        )
        attempt_id = self.telemetry.create_dispatch_attempt(
            self.repo_id, self.task_id, self.cycle_id, self.stage_seq, run.role,
            executor=target.executor,
            provider=target.provider,
            model_requested=target.model,
            effort=target.effort,
            profile=decision.profile,
            harness_snapshot=harness_snapshot,
            parent_attempt_id=run.parent_attempt_id,
            relationship=attempt.relationship,
        )
        attempt.attempt_id = attempt_id
        if self.registry.get(target.executor).completes_work:
            self.telemetry.update_dispatch_attempt(
                attempt_id, {"lifecycle_state": "running"},
                update_id=f"update-{uuid.uuid4().hex}", source="runtime",
            )
        self._journal("stage_start", role=run.role, attempt_id=attempt_id,
                      component=target.executor, status=decision.profile,
                      detail=f"{target.provider}/{target.model} {target.effort}",
                      correlation={"parent_attempt_id": run.parent_attempt_id,
                                   "relationship": attempt.relationship})
        if self.on_dispatch_started is not None:
            self.on_dispatch_started()
        return attempt_id

    def _observe_progress(self, attempt: _Attempt, **activity: object) -> None:
        attempt.tool_activity |= bool(activity.get("tool"))
        if self.on_progress is not None:
            self.on_progress(**activity)

    def _record_interruption(self, run: _StageRun, attempt: _Attempt,
                             interruption: CycleInterrupted) -> None:
        """No half-done state, even now: the stage that was running is the
        row a later question about this run needs most."""
        decision, target = attempt.decision, attempt.target
        result = DispatchResult(
            DispatchOutcome.INTERRUPTED, target.executor, target,
            detail=f"interrupted by {interruption.signal_name}",
            duration_ms=max(0, round((time.monotonic() - attempt.started) * 1000)),
            lifecycle_state="interrupted",
            start_state="started" if attempt.tool_activity else "unknown",
        )
        if attempt.attempt_id is not None:
            self.telemetry.update_dispatch_attempt(
                attempt.attempt_id,
                {"lifecycle_state": "interrupted", "outcome": "interrupted",
                 "duration_ms": result.duration_ms,
                 "finished_at": datetime.now(timezone.utc).isoformat(),
                 "error_code": "interrupted", "start_state": result.start_state},
                update_id=f"update-{uuid.uuid4().hex}", source="runtime",
            )
        run.decision = decision
        run.result = result
        if attempt.attempt_id is not None:
            run.attempts.append((decision, result))
        run.rows.append(self._record(
            run.role, decision, result, attempt.signals, attempt_id=attempt.attempt_id,
            parent_attempt_id=run.parent_attempt_id, relationship=attempt.relationship,
        ))
        self.failed_attempts += 1
        outcome = run.outcome()
        self.stages.append(outcome)
        self._journal_attempt(run.role, result, attempt.attempt_id, None, attempt.signals)
        interruption.outcome = outcome

    # -- settling: what the attempt showed, and whether another follows ------

    def _settle_attempt(self, run: _StageRun, attempt: _Attempt,
                        result: DispatchResult) -> DispatchResult:
        """Read the attempt's evidence and decide whether the stage reroutes."""
        if attempt.tool_activity:
            result = replace(result, start_state="started")
        self._note_escalation_acceptance(run, attempt, result)
        result = self._unsupported_effort(run, attempt, result)
        result = self._find_blocker(run, attempt, result)
        result = self._environment_restriction(attempt, result)
        if result.failure_code in TRANSIENT_FAILURES:
            result = self._transient_failure(run, attempt, result)
        elif (result.outcome is DispatchOutcome.SUCCEEDED
                and self.mode is RoutingMode.PRODUCTION):
            self._clear_quota(attempt)
        return self._consent_answer(run, attempt, result)

    @staticmethod
    def _set_acceptance(run: _StageRun, attempt: _Attempt, acceptance: str) -> None:
        run.stage_signals["escalation_acceptance"] = acceptance
        attempt.signals["escalation_acceptance"] = acceptance

    def _note_escalation_acceptance(self, run: _StageRun, attempt: _Attempt,
                                    result: DispatchResult) -> None:
        if run.role != "resolve" or not run.resolve_escalated:
            return
        if (result.outcome is DispatchOutcome.SUCCEEDED
                and result.requested.effort == "max"
                and attempt.signals.get("escalation_acceptance") == "unknown"):
            self._set_acceptance(run, attempt, "accepted")
        elif (result.failure_code != "unsupported_effort"
              and result.outcome is not DispatchOutcome.SUCCEEDED
              and result.requested.effort == "max" and not attempt.decision.used_fallback):
            # Only the escalated dispatch can leave the acceptance
            # unknown; a failed fallback keeps the signal already set.
            self._set_acceptance(run, attempt, "unknown")

    def _reroute_gate(self, run: _StageRun, attempt: _Attempt,
                      result: DispatchResult) -> tuple[str, str]:
        """The #205 invariant: a stage that may have started is rerouted only
        once its workspace effects are reconciled."""
        if result.asynchronous or not result.process_exited:
            return "stop", "exit_unproven"
        if result.needs_human_action:
            return "stop", "human_action"
        if result.start_state == "not_started":
            return "reroute", "not_started"
        if not run.writes and not run.publishes:
            return "reroute", "no_effects"
        return reconcile(attempt.before, self._effect_snapshot(run))

    def _unsupported_effort(self, run: _StageRun, attempt: _Attempt,
                            result: DispatchResult) -> DispatchResult:
        """A CLI that rejected the pinned resolve effort uses the fallback once."""
        if result.failure_code != "unsupported_effort" or run.role != "resolve":
            return result
        if run.resolve_escalated:
            self._set_acceptance(run, attempt, "rejected")
        if attempt.number != 0:
            return result
        decision = attempt.decision
        fallback = stage_fallback(decision, self.profiles)
        stopped_reason = "no available configured fallback"
        reason = None
        if (fallback is not None and fallback != decision.target
                and self._dispatchable(fallback.executor, attempt.eligible)):
            if self.mode is RoutingMode.CALIBRATION:
                action, reason = "stop", "calibration"
            elif attempt.attempt_id is None:
                action, reason = "stop", "exit_unproven"
            else:
                action, reason = self._reroute_gate(run, attempt, result)
            attempt.signals.update(reroute_action=action, reroute_reason=reason,
                                   process_exited=result.process_exited,
                                   reroute_error_code="unsupported_effort",
                                   reroute_start_state=result.start_state)
            attempt.retry = action == "reroute"
            stopped_reason = f"reroute stopped: {reason}"
        if not attempt.retry:
            return replace(
                result, detail=f"{result.detail}; {stopped_reason} for unsupported effort",
            )
        run.fallback_decision = replace(
            decision, target=fallback, used_fallback=True,
            reasons=decision.reasons +
            ("the CLI rejected the pinned resolve effort; using the configured fallback",),
        )
        run.parent_failure = {
            "reroute_error_code": "unsupported_effort",
            "reroute_start_state": result.start_state,
            "reroute_action": "reroute", "reroute_reason": reason,
        }
        return result

    def _find_blocker(self, run: _StageRun, attempt: _Attempt,
                      result: DispatchResult) -> DispatchResult:
        """Find what stopped the agent: a refused command or a sandbox report."""
        command_blocker = None
        if isinstance(result.agent_output, str) and result.agent_output:
            # Whatever the agent put under a runtime-owned key is set aside
            # first, so what remains there afterwards is the runtime's own.
            from command_permissions import claim_runtime_keys
            result = replace(result, agent_output=claim_runtime_keys(result.agent_output))
        command_policy = run.dispatch_kwargs.get("command_policy")
        if (command_policy is not None and attempt.target.executor == "claude"
                and "docker_host" in run.sandbox_grants):
            # Read the refusals as the dispatch's hook saw them: with the socket
            # granted, a refused `compose run` is an exact-command question (#398).
            command_policy = replace(command_policy, docker=True)
        if run.compose_consents and isinstance(result.agent_output, str) and result.agent_output:
            from command_permissions import add_outcomes, compose_drift
            result = replace(result, agent_output=add_outcomes(result.agent_output, compose_drift(
                run.compose_consents, command_policy, run.dispatch_kwargs.get("cwd"))))
        if self.consent is not None and command_policy is not None:
            from command_permissions import refused_operations, reconcile_result
            command_blockers = refused_operations(
                result.artifacts.get("command_refusals") or result.artifacts.get("permission_denials"),
                command_policy, self.consent, attempt.target.executor,
                run.dispatch_kwargs.get("cwd"))
            for refused in command_blockers:
                denied = refused["resource"] in attempt.denied_before
                spoken, essential = reconcile_result(
                    result.agent_output, refused, DENY if denied else None, run.role)
                result = replace(result, agent_output=spoken)
                # A configured check is never essential, yet an unanswered one
                # is still asked before it is left unverified (#398).
                ask = (refused.get("check") and not denied
                       and refused["resource"] not in self.consent.granted_ids)
                if (essential or ask) and command_blocker is None:
                    command_blocker = refused
        blocker = command_blocker or (observed_sandbox_blocker(result.agent_output,
                                                               result.artifacts.get("sandbox_denials"))
                                      or sandbox_blocker(result.agent_output)
                                      or denied_docker_blocker(result.agent_output,
                                                               result.artifacts.get("permission_denials"))
                                      if result.outcome is DispatchOutcome.SUCCEEDED
                                      and not result.asynchronous else None)
        attempt.blocker = blocker
        if self.consent is not None and blocker is not None:
            denied_grant = self.consent.grant_for(blocker)
            if denied_grant is not None and denied_grant.id in self.consent.denied_ids:
                run.denial_affected.add(denied_grant.id)
        if run.denial_affected:
            result = replace(result, agent_output=denied_result(
                result.agent_output, sorted(run.denial_affected)))
        return result

    def _environment_restriction(self, attempt: _Attempt,
                                 result: DispatchResult) -> DispatchResult:
        """A restriction keeps the selected executor; a known prerequisite says so."""
        blocker = attempt.blocker
        known_prerequisite = (
            known_failure(blocker["command"], blocker["error"]) if blocker is not None
            else result.failure_code == "environment_restricted"
            and known_failure("codex sandbox" if result.executor == "codex" else "claude",
                              result.detail)
        )
        preflight = result.artifacts.get("native_sandbox_preflight")
        if (result.failure_code == "environment_restricted" and isinstance(preflight, dict)
                and isinstance(preflight.get("functional_check"), dict)
                and preflight["functional_check"].get("passed") is False):
            known_prerequisite = True
        attempt.known_prerequisite = bool(known_prerequisite)
        if blocker is not None:
            # A completed CLI call is not a completed functional stage.
            # This claim enables reconciliation, never grants permissions.
            result = replace(
                result, outcome=DispatchOutcome.FAILED,
                error_code="environment_restricted", lifecycle_state="failed",
                detail=f"agent reported {blocker['kind']}: {blocker['error']}",
            )
        if known_prerequisite:
            # This selected executor cannot repair native nesting by
            # redispatching. A residual BLOCKED without independently
            # proved completion remains conservative; never infer completion
            # from the mere presence of a draft PR.
            attempt.signals.update(reroute_action="stop", reroute_reason="same_sandbox",
                                   reroute_error_code="environment_restricted",
                                   reroute_start_state=result.start_state,
                                   process_exited=result.process_exited)
            result = replace(result, detail=result.detail +
                             "; known native prerequisite: checks not_verified; no executor reroute")
        if result.failure_code == "environment_restricted" and not known_prerequisite:
            attempt.signals.update(reroute_action="stop", reroute_reason="same_sandbox",
                                   reroute_error_code="environment_restricted",
                                   reroute_start_state=result.start_state,
                                   process_exited=result.process_exited)
            result = replace(result, detail=result.detail +
                             "; sandbox restriction: keep the selected executor; checks not_verified")
        return result

    def _transient_failure(self, run: _StageRun, attempt: _Attempt,
                           result: DispatchResult) -> DispatchResult:
        """Capacity, transport, timeout or quota: the stage fallback, once."""
        decision, target = attempt.decision, attempt.target
        action, reason = "stop", "retry_exhausted"
        fallback = stage_fallback(decision, self.profiles)
        exhaustion_expiry: str | None = None
        reset_reported = False
        if result.failure_code == "quota" and self.mode is RoutingMode.PRODUCTION:
            exhaustion_expiry, reset_reported = self._record_quota_exhaustion(
                run, attempt, result, fallback)
        if self.mode is RoutingMode.CALIBRATION:
            reason = "calibration"
        elif attempt.number == 0 and attempt.attempt_id is not None:
            action, reason = self._reroute_gate(run, attempt, result)
            if action == "reroute":
                if fallback is None or fallback == target:
                    action, reason = "stop", "no_fallback"
                elif not self._dispatchable(fallback.executor, attempt.eligible):
                    action, reason = "stop", "fallback_unavailable"
        attempt.signals.update(reroute_action=action, reroute_reason=reason,
                               process_exited=result.process_exited,
                               reroute_error_code=result.failure_code,
                               reroute_start_state=result.start_state)
        attempt.retry = action == "reroute"
        if attempt.retry:
            run.fallback_decision = replace(
                decision, target=fallback, used_fallback=True,
                reasons=decision.reasons + (f"stage fallback after {result.failure_code}",),
            )
            run.parent_failure = {
                "reroute_error_code": result.failure_code,
                "reroute_start_state": result.start_state,
                "reroute_action": action, "reroute_reason": reason,
            }
            return result
        detail = f"{redact(result.detail) if result.failure_code == 'quota' else result.detail}; reroute stopped: {reason}"
        if (result.failure_code == "quota"
                and self.mode is RoutingMode.PRODUCTION):
            if exhaustion_expiry and reset_reported:
                detail += (f"; quota exhaustion recorded until {exhaustion_expiry}; "
                           "wait for the reset or " + self._quota_resume_command(run.role))
            elif exhaustion_expiry:
                detail += ("; quota reset is unknown; retry after 30 minutes; "
                           f"temporary quota window recorded until {exhaustion_expiry}; "
                           + self._quota_resume_command(run.role))
            else:
                detail += ("; quota reset is ambiguous; executor unavailable until explicit operator retry; "
                           + self._quota_resume_command(run.role))
        return replace(result, detail=detail)

    def _record_quota_exhaustion(self, run: _StageRun, attempt: _Attempt,
                                 result: DispatchResult,
                                 fallback: Target | None) -> tuple[str | None, bool]:
        """Persist the exhaustion, and who owns a dirty tree it left.

        Returns the recorded expiry and whether the executor reported a reset
        time, as far as recording got: a telemetry failure must not block the
        writing stage, so it ends the recording, never the stage.
        """
        target = attempt.target
        exhaustion_expiry: str | None = None
        reset_reported = False
        try:
            _, reset_reported = self.telemetry.quota_expiry(result.detail)
            exhaustion_expiry = self.telemetry.record_quota_exhaustion(
                self.repo_id, target.executor, target.model, result.detail,
            )
            self.exhausted_until[(target.executor, target.model)] = (
                exhaustion_expiry or "operator retry required")
            self._journal("failure", role=run.role, component="quota", status="exhausted",
                          detail=result.detail,
                          correlation={"expires_at": exhaustion_expiry,
                                       "ambiguity": "reset_time_ambiguous" if exhaustion_expiry is None else None})
            before = attempt.before
            after = self._effect_snapshot(run) if self.change_request_id else None
            # Only a finished executor with a clean initial tree,
            # or a validated prior ownership record, can own edits.
            if (result.process_exited and not result.asynchronous
                    and not result.needs_human_action and fallback is not None
                    and before is not None and after is not None
                    and (not before["dirty"] or attempt.handoff is not None)
                    and after["dirty"]
                    and before["workspace"]["head"] == after["workspace"]["head"]
                    and reconcile({**before, "dirty": False},
                                  {**after, "dirty": False, "workspace": before["workspace"]})[0] == "reroute"):
                record_id = Handoffs(self.telemetry.path).record(
                    repo=self.repo_id, task=self.task_id, stage=run.role,
                    pr=self.change_request_id, workspace=after["workspace"],
                    original=target, destination=fallback)
                self._journal("stage_result", role=run.role, component="quota", status="provenance_recorded",
                              correlation={"record_id": record_id,
                                           "original_executor": target.executor,
                                           "destination_executor": fallback.executor,
                                           "fingerprint": after["workspace"]["fingerprint"]})
        except (OSError, sqlite3.Error, TelemetryError, ValueError):
            pass
        return exhaustion_expiry, reset_reported

    def _clear_quota(self, attempt: _Attempt) -> None:
        try:
            if attempt.handoff is not None:
                Handoffs(self.telemetry.path).complete(attempt.handoff)
            self.telemetry.clear_quota_exhaustion(
                self.repo_id, attempt.target.executor, attempt.target.model,
            )
        except (OSError, sqlite3.Error, TelemetryError, ValueError):
            pass

    def _consent_answer(self, run: _StageRun, attempt: _Attempt,
                        result: DispatchResult) -> DispatchResult:
        """A restriction requests a grant on the chosen executor, never a fallback.

        Consent never selects a different target, even when an availability
        fallback was selected earlier in this stage.
        """
        consent, blocker = self.consent, attempt.blocker
        grant = (consent.grant_for(blocker)
                 if blocker is not None and consent is not None
                 and not attempt.known_prerequisite
                 and self.mode is not RoutingMode.CALIBRATION else None)
        if grant is None or consent is None or blocker is None:
            return result
        if grant.id in attempt.denied_before:
            return replace(result, detail=f"{result.detail}; {grant.id} was denied "
                                          "before this dispatch: no rerun")
        decision = attempt.decision
        executor = attempt.target.executor
        answer = None
        if (not attempt.retry and not run.consent_requested
                and grant.id not in consent.granted(executor)):
            run.consent_requested = True
            answer = consent.request(grant, executor, role=run.role, blocker=blocker)
        if answer == PENDING:
            # Nobody could answer: the sandbox stays as it is, no other
            # executor is tried in its place, and the cycle stops to ask.
            attempt.signals.update(reroute_action="stop", reroute_reason="permission_required",
                                   process_exited=result.process_exited,
                                   reroute_error_code="environment_restricted",
                                   reroute_start_state=result.start_state)
            return replace(result, detail=f"{result.detail}; permission required: {grant.id}")
        if answer in {ALLOW_CYCLE, ALLOW_PERSISTENT, ALLOW_GLOBAL, DENY}:
            if answer == DENY:
                run.denial_affected.add(grant.id)
            # An answer permits access, not replaying effects. Reconcile
            # every full rerun, including one on the same executor.
            action, stop_reason = (("stop", "exit_unproven")
                                   if attempt.attempt_id is None
                                   else self._reroute_gate(run, attempt, result))
            safe = action == "reroute"
            attempt.signals.update(reroute_action="reroute" if safe else "stop",
                                   reroute_reason="permission_answered" if safe else stop_reason,
                                   process_exited=result.process_exited,
                                   reroute_error_code="environment_restricted",
                                   reroute_start_state=result.start_state)
            if not safe:
                return replace(result, detail=f"{result.detail}; reroute stopped: {stop_reason}")
            run.environment_context = recovery_context(blocker)
            run.fallback_decision = replace(
                decision, reasons=decision.reasons +
                (f"rerun after the {grant.id} permission answer: {answer}",),
            )
            run.parent_failure = {
                "reroute_error_code": "environment_restricted",
                "reroute_start_state": result.start_state,
                "reroute_action": "reroute", "reroute_reason": "permission_answered",
            }
            attempt.retry = True
            return result
        if not attempt.retry:
            return replace(result, detail=f"{result.detail}; reroute stopped: "
                                          f"{attempt.signals.get('reroute_reason', 'no_fallback')}")
        return result

    # -- recording -----------------------------------------------------------

    def _persist_attempt(self, run: _StageRun, attempt: _Attempt,
                         result: DispatchResult) -> None:
        """Close the attempt's record and write its row and journal entries."""
        decision, attempt_id = attempt.decision, attempt.attempt_id
        state = result.lifecycle_state or (
            "launched" if result.asynchronous else
            "completed" if result.outcome is DispatchOutcome.SUCCEEDED else
            "interrupted" if result.outcome is DispatchOutcome.INTERRUPTED else "failed"
        )
        partial_result: dict[str, object] = {
            "lifecycle_state": state,
            "outcome": result.outcome.value,
            "model_resolved": result.model_resolved,
            "duration_ms": result.duration_ms,
            "usage_observations": list(result.usage_observations),
            "cost_measures": list(result.cost_measures),
            "start_state": result.start_state,
        }
        if state in {"completed", "failed", "timed_out", "interrupted"}:
            partial_result["finished_at"] = datetime.now(timezone.utc).isoformat()
        external_dispatch_id = (result.artifacts or {}).get("dispatchId")
        if external_dispatch_id is None:
            external_dispatch_id = (result.artifacts or {}).get("dispatch_id")
        if external_dispatch_id is not None:
            partial_result["external_dispatch_id"] = external_dispatch_id
        if result.failure_code is not None:
            partial_result["error_code"] = result.failure_code
        if attempt_id is not None:
            self.telemetry.update_dispatch_attempt(
                attempt_id, partial_result,
                update_id=f"update-{uuid.uuid4().hex}", source="runtime",
            )
            result = replace(result, artifacts={**result.artifacts, "dispatch_attempt_id": attempt_id})
        run.decision = decision
        run.result = result
        if attempt_id is not None:
            run.attempts.append((decision, result))
        run.rows.append(self._record(
            run.role, decision, result, attempt.signals, attempt_id=attempt_id,
            parent_attempt_id=run.parent_attempt_id, relationship=attempt.relationship,
        ))
        if result.outcome is not DispatchOutcome.SUCCEEDED:
            self.failed_attempts += 1
        self._journal_attempt(run.role, result, attempt_id, attempt.blocker, attempt.signals)

    def _quota_resume_command(self, role: str) -> str:
        if self.started_from == "implement" and role == "implement":
            return "run --continue to use the configured fallback"
        return (f"resume with --from {role} --pr {self.change_request_id} "
                "to use the configured fallback")

    def _journal(self, kind: str, **fields) -> None:
        if self.journal is not None:
            fields.setdefault("stage_seq", self.stage_seq)
            self.journal.record(kind, **fields)

    def _journal_attempt(self, role: str, result: DispatchResult, attempt_id: str | None,
                         blocker: dict[str, Any] | None, signals: dict[str, Any]) -> None:
        """One attempt's end, its failure and every refusal its result reports.

        The stage hook records its own denials as they happen; these are the
        refusals only the result shows: Claude's permission layer, the
        runtime's preflight of the Claude configuration, and an executor's
        sandbox report (Codex reports no individual command).
        """
        if self.journal is None:
            return
        executor = result.executor
        artifacts = result.artifacts or {}
        command_policy = artifacts.get("command_policy")
        preflight = artifacts.get("native_sandbox_preflight") or (
            command_policy.get("native_sandbox_preflight")
            if isinstance(command_policy, dict) else None)
        if isinstance(preflight, dict):
            # Reuse the existing event schema. Each probe gets its own bounded,
            # redacted row so one long error cannot truncate later observations.
            def observation(name, status, observed=None, detail=None):
                self._journal("stage_end", role=role, attempt_id=attempt_id,
                              component="sandbox_preflight", reason=name,
                              status=status, command=observed, detail=detail,
                              correlation={"executor": executor})

            checks = ("functional_check", "preparation_isolation", "codex_socket")
            summary = {key: value for key, value in preflight.items()
                       if key not in ("probes", *checks)}
            observation("summary", preflight.get("status"), detail=summary)
            for probe in preflight.get("probes", []):
                if isinstance(probe, dict):
                    observation(probe.get("name"), probe.get("outcome"),
                                probe.get("observed") or probe.get("path"),
                                probe.get("error") or probe.get("version"))
            for name in checks:
                check = preflight.get(name)
                if isinstance(check, dict):
                    status = ("verified" if check["passed"] else "unverified") if "passed" in check else check.get("outcome")
                    observation(name, status, check.get("probe"),
                                check.get("evidence") or check.get("reason"))
                elif check is not None:
                    observation(name, check)
        self._journal("stage_end", role=role, attempt_id=attempt_id, component=executor,
                      status=result.outcome.value, reason=result.failure_code,
                      duration_ms=result.duration_ms,
                      correlation={"reroute_action": signals.get("reroute_action"),
                                   "reroute_reason": signals.get("reroute_reason")})
        if result.outcome is not DispatchOutcome.SUCCEEDED:
            self._journal("failure", role=role, attempt_id=attempt_id, component=executor,
                          status=result.outcome.value,
                          reason=result.failure_code or result.outcome.value,
                          detail=result.detail)
        preflight = artifacts.get("policy_preflight_refusal")
        if isinstance(preflight, str):
            self._journal("refusal", role=role, attempt_id=attempt_id,
                          component="policy_preflight", status="refused", reason=preflight)
        for denial in artifacts.get("claude_permission_denials") or []:
            if isinstance(denial, dict):
                self._journal("refusal", role=role, attempt_id=attempt_id,
                              component="claude_permission", status="denied",
                              command=denial.get("command") or denial.get("tool"),
                              reason=f"Claude's permission layer denied {denial.get('tool')}",
                              correlation={"tool_use_id": denial.get("tool_use_id")})
        for failure in artifacts.get("sandbox_startup_failures") or []:
            if isinstance(failure, dict):
                self._journal("refusal", role=role, attempt_id=attempt_id,
                              component="claude_sandbox", status="sandbox_startup_failure",
                              command=failure.get("command"),
                              reason=f"the Claude Bash sandbox could not start: {failure.get('path')}",
                              detail=f"{failure.get('cause')}; {failure.get('limitation')}")
        if blocker is not None:
            self._journal("refusal", role=role, attempt_id=attempt_id,
                          component=f"{executor}_sandbox", status=str(blocker.get("kind")),
                          command=blocker.get("command"),
                          reason=f"the agent reported {blocker.get('kind')}: "
                                 f"{blocker.get('error')}")

    def _observe_shadow(self, role: str, decision: RoutingDecision,
                        signals: dict) -> None:
        """Ask the shadow about the stage's first routing, and only record it.

        The first routing, because it is the one the rules made from the
        pre-routing signals alone; a reroute is availability, not selection.
        Written after the stage's own rows so no reader can take the suggestion
        for the dispatch. Nothing here may stop the cycle: the adapter turns
        failures into categories, and a row this store refuses is dropped.
        """
        if self.shadow is None or not self.shadow.applies_to(role):
            return
        try:
            suggestion = self.shadow.suggest(role, signals)
            self.telemetry.record_stage(
                self.repo_id, self.task_id, role,
                iteration=self.iteration,
                cycle_id=self.cycle_id, stage_seq=self.stage_seq,
                record_kind="shadow",
                routing_strategy=self.routing_strategy.value,
                local_only=self.local_only,
                started_from=self.started_from,
                **shadow_fields(self.shadow.config, decision.profile, suggestion),
            )
        except Exception:
            return

    def record_verdict(self, role: str, status: str, **fields) -> int:
        """Record a functional outcome that is not itself a dispatch.

        A review's verdict arrives after its dispatch succeeded, and it is the
        fact `first_pass_rate` actually reads. Recording it separately keeps the
        dispatch row honest about what the dispatch did.
        """
        if role in REVIEW_ROLES and not status:
            raise CycleError(f"a {role} verdict needs a status; an empty one counts as nothing")
        # The latest verdict is what the next stage is routed knowing. One that
        # reported no findings leaves them unknown rather than zero.
        self.prior_findings = {
            f"prior_{key}": fields[key] for key in FINDING_FIELDS
            if fields.get(key) is not None
        }
        # A verdict with no dispatch of its role in this run points at none.
        source = ({"stage_seq": self._latest_seq[role]}
                  if role in self._latest_seq else {})
        row = self.telemetry.record_stage(
            self.repo_id, self.task_id, role,
            iteration=self.iteration, status=status,
            routing_strategy=self.routing_strategy.value,
            local_only=self.local_only,
            started_from=self.started_from,
            cycle_id=self.cycle_id, record_kind="verdict",
            **self.work_item_identity_fields,
            **source, **fields,
        )
        # Tracked only after the row was accepted, so a refused verdict cannot
        # reach the closing row either.
        if role in REVIEW_ROLES and self._completed_review(status):
            head = fields.get("reviewed_head_sha")
            if role == "review" and self.first_review_status is None:
                self.first_review_status = status
                self.first_review_head = head
            self.final_review_status = status
            self.final_review_head = head
            if isinstance(head, str):
                self.reviews_measured = True
                if head in self.clean_review_heads:
                    self.redundant_reviews += 1
                if status in CLEAN_REVIEW_STATUSES:
                    self.clean_review_heads.add(head)
        if (fields.get("tests_passed") is not None
                or fields.get("verification") is not None
                or fields.get("boundary_verified") is not None):
            # Test outcome and conclusion belong to one report. A later stage
            # that reports only one must not inherit the other's stale value.
            self.tests_passed = fields.get("tests_passed")
            self.tests_basis = fields.get("tests_basis")
            self.verification = fields.get("verification")
            self.boundary_verified = fields.get("boundary_verified")
        return row

    def _completed_review(self, status: str) -> bool:
        """A review status that settles its round under this cycle's CI policy."""
        return (status in TERMINAL_REVIEW_STATUSES
                or (status == AWAITING_CI_STATUS and self.ci_policy == "parallel"))

    def _approved(self, status: str, head: str | None) -> bool | None:
        """Whether a review settled as an approval, or None when it is not known.

        Under `strict` the reviewer's `APPROVED` is the approval, as before.
        Under `parallel` a clean review approves only once the CI gate passed
        its own SHA, and fails to once the gate definitively ruled it out.
        """
        if self.ci_policy != "parallel":
            return status == "APPROVED"
        if status not in CLEAN_REVIEW_STATUSES:
            return False
        gate = self.ci_outcomes.get(head) if head else None
        if gate == CI_PASSED:
            return True
        if gate in DEFINITIVE_FAILURES:
            return False
        return None

    def record_ci(self, result, *, mode: str, role: str | None = None) -> int:
        """Record one read (`once`) or wait (`wait`) of the CI gate (#228).

        A row of its own beside the review's verdict, because the review
        outcome and the CI outcome are separate facts about one SHA, and each
        names that SHA. It points at the review it followed through
        `stage_seq`. Only a wait decides approval; a read only informs.
        """
        counts = result.counts()
        source = ({"stage_seq": self._latest_seq[role]}
                  if role in self._latest_seq else {})
        waited = int(round(result.waited_seconds * 1000))
        row = self.telemetry.record_stage(
            self.repo_id, self.task_id, "coordinate",
            iteration=self.iteration,
            routing_strategy=self.routing_strategy.value,
            local_only=self.local_only,
            started_from=self.started_from,
            cycle_id=self.cycle_id, record_kind="ci",
            ci_policy=self.ci_policy,
            ci_status=result.status, ci_mode=mode, ci_head_sha=result.head_sha,
            ci_required_source=result.required_source,
            checks_passed=counts["passed"], checks_failed=counts["failed"],
            checks_pending=counts["pending"], checks_skipped=counts["skipped"],
            checks_missing=counts["missing"], checks_late=counts["late"],
            ci_polls=result.polls, ci_wait_ms=waited,
            **self.work_item_identity_fields, **source,
        )
        self.ci_recorded = True
        self.ci_polls += result.polls
        if mode == "wait":
            self.ci_wait_ms += waited
            self.ci_outcomes[result.head_sha] = result.status
            self.final_ci_status = result.status
        return row

    def next_iteration(self) -> int:
        self.iteration += 1
        return self.iteration

    def close(self, final_status: str, **fields) -> int:
        """Record how the cycle ended, with the rounds it took."""
        return self.telemetry.record_stage(
            self.repo_id, self.task_id, "coordinate",
            iteration=self.iteration, status=final_status,
            iterations=self.iteration,
            routing_strategy=self.routing_strategy.value,
            local_only=self.local_only,
            started_from=self.started_from,
            cycle_id=self.cycle_id, record_kind="cycle",
            ci_policy=self.ci_policy,
            **self.work_item_identity_fields,
            **{**self.observed_outcome(), **fields},
        )

    def observed_outcome(self) -> dict:
        """What this run showed, and nothing it did not.

        The review outcomes exist only once a review reached a verdict: a cycle
        that stopped before one has not been approved, has not failed its first
        pass and has not needed zero rounds — it has not been judged. Fallbacks
        and contract violations are always known, because every dispatch in the
        run went through this recorder.
        """
        attempts = [result for stage in self.stages for _, result in stage.attempts]
        outcome = {
            "fallback_stages": sum(
                1 for stage in self.stages
                if stage.decision is not None and stage.decision.used_fallback
            ),
            "contract_violations": sum(
                1 for result in attempts
                if result.outcome is DispatchOutcome.CONTRACT_VIOLATION
            ),
        }
        first = self.first_review_status
        if first is not None:
            outcome.update(
                first_review_status=first,
                resolution_needed=first == "CHANGES_REQUESTED",
                resolution_rounds=sum(1 for stage in self.stages if stage.role == "resolve"),
            )
            # A first pass is an implementation's first review. A resumed
            # cycle's review judged work from an earlier run.
            approved = self._approved(first, self.first_review_head)
            if self.started_from == "implement" and approved is not None:
                outcome["first_pass_approved"] = approved
        if self.final_review_status is not None:
            outcome["final_review_status"] = self.final_review_status
            approved = self._approved(self.final_review_status, self.final_review_head)
            if approved is not None:
                outcome["final_approved"] = approved
        if self.ci_recorded:
            outcome.update(ci_polls=self.ci_polls, ci_wait_ms=self.ci_wait_ms)
        if self.final_ci_status is not None:
            outcome["final_ci_status"] = self.final_ci_status
        if self.reviews_measured:
            outcome["redundant_reviews"] = self.redundant_reviews
        if self.review_reused is not None:
            outcome["review_reused"] = self.review_reused
        if self.review_provenance is not None:
            outcome["review_provenance"] = self.review_provenance
        if self.tests_passed is not None:
            outcome.update(
                tests_passed=self.tests_passed,
                tests_basis=self.tests_basis,
            )
        if self.verification is not None:
            outcome["verification"] = self.verification
        if self.boundary_verified is not None:
            outcome["boundary_verified"] = self.boundary_verified
        if self.repeated_findings is not None:
            outcome["repeated_findings"] = self.repeated_findings
        return outcome

    def _pre_routing(self, role: str, change: ChangeSignals | None) -> dict:
        """What was known before this routing, with unknown signals left out."""
        signals = {
            "difficulty": self.signals.difficulty,
            "verifiability": self.signals.verifiability,
            "security_sensitive": self.signals.security_sensitive,
            "previous_failed_attempts": self.failed_attempts,
            **self.prior_findings,
        }
        if change is not None:
            signals.update(change.telemetry_fields())
        if self.verification_available is not None:
            signals["verification_available"] = self.verification_available
        if role in RESOLUTION_ROLES:
            signals["resolution_round"] = self.iteration
            if self.repeated_findings is not None:
                signals["repeated_findings"] = self.repeated_findings
        return signals

    def _record(self, role: str, decision: RoutingDecision,
                result: DispatchResult | None, signals: dict, *,
                attempt_id: str | None = None,
                parent_attempt_id: str | None = None,
                relationship: str | None = None) -> int:
        if result is None:
            # A blocked routing decision never reached an executor, so there is
            # no dispatch to describe — but the decision still happened, and a
            # run that stopped here would otherwise leave no trace of why.
            return self.telemetry.record_stage(
                self.repo_id, self.task_id, role,
                iteration=self.iteration,
                cycle_id=self.cycle_id, stage_seq=self.stage_seq,
                record_kind="dispatch",
                profile=decision.profile,
                used_fallback=decision.used_fallback,
                outcome=DispatchOutcome.BLOCKED.value,
                routing_reason_count=len(decision.reasons or ()),
                **routing_decision_fields(decision),
                local_only=self.local_only,
                started_from=self.started_from,
                **signals,
                **self.work_item_identity_fields,
            )
        return self.telemetry.record_dispatch(
            self.repo_id, self.task_id, role, decision, result,
            iteration=self.iteration,
            local_only=self.local_only,
            started_from=self.started_from,
            cycle_id=self.cycle_id, stage_seq=self.stage_seq,
            record_kind="dispatch",
            dispatch_attempt_id=attempt_id,
            parent_attempt_id=parent_attempt_id,
            dispatch_relationship=relationship,
            **signals,
            **self.work_item_identity_fields,
        )
