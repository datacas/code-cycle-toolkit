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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, cast

from cycle_journal import Journal
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
    ALLOW_CYCLE, ALLOW_PERSISTENT, DENY, PENDING, ConsentBroker,
    denial_context, granted_context,
)
from environment_recovery import (
    ENVIRONMENT_RECOVERY_GUIDANCE, denied_docker_blocker, recovery_context, sandbox_blocker,
)
from reroute import effect_snapshot, reconcile
from jev_shadow import JevShadow, telemetry_fields as shadow_fields
from router import (
    RoutingDecision,
    RoutingMode,
    RoutingStrategy,
    TaskSignals,
    escalation_selector,
    models_from_profiles,
    stage_fallback,
    route,
    rule_selector,
)
from stage_signals import CHANGE_OBSERVED_ROLES, RESOLUTION_ROLES, ChangeSignals
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

@dataclass(frozen=True)
class RoleContract:
    """The workspace boundary a role must receive before it can run."""

    workspace_policy: WorkspacePolicy
    publishes: bool = False
    publication_permissions: tuple[str, ...] = ()

ROLE_CONTRACTS = {
    "implement": RoleContract(
        WorkspacePolicy.WORKSPACE_WRITE, publishes=True,
        publication_permissions=("comment", "create_pr", "push_branch"),
    ),
    "resolve": RoleContract(
        WorkspacePolicy.WORKSPACE_WRITE, publishes=True,
        publication_permissions=("comment", "push_branch"),
    ),
    "review": RoleContract(
        WorkspacePolicy.READ_ONLY, publishes=True,
        publication_permissions=("comment",),
    ),
    "rereview": RoleContract(
        WorkspacePolicy.READ_ONLY, publishes=True,
        publication_permissions=("comment",),
    ),
    # Judges the work item against the repository; it edits neither and
    # publishes nothing, not even a comment on the item it judged.
    "issue_review": RoleContract(WorkspacePolicy.READ_ONLY),
    "security": RoleContract(WorkspacePolicy.READ_ONLY),
    "bootstrap": RoleContract(WorkspacePolicy.READ_ONLY),
    "verify": RoleContract(WorkspacePolicy.DISPOSABLE),
    "run": RoleContract(WorkspacePolicy.DISPOSABLE),
    "coordinate": RoleContract(WorkspacePolicy.READ_ONLY),
}
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
        """
        selector = escalation_selector if escalated else rule_selector
        outcome = StageOutcome(role=role, decision=None, result=None)  # type: ignore[arg-type]
        contract = role_contract(role)
        workspace_policy = contract.workspace_policy
        writes = workspace_policy in {
            WorkspacePolicy.WORKSPACE_WRITE, WorkspacePolicy.DISPOSABLE,
        }
        requested_writes = dispatch_kwargs.pop("writes", writes)
        if requested_writes is not writes:
            raise CycleError(
                f"{role!r} stages must use writes={writes}, not {requested_writes!r}")
        dispatch_kwargs["writes"] = writes
        publishes = contract.publishes and not self.local_only
        requested_publishes = dispatch_kwargs.pop("publishes", publishes)
        if requested_publishes is not publishes:
            raise CycleError(
                f"{role!r} stages must use publishes={publishes}, not {requested_publishes!r}")
        dispatch_kwargs["publishes"] = publishes
        publication_permissions = (
            contract.publication_permissions if publishes else ()
        )
        requested_publication_permissions = dispatch_kwargs.pop(
            "publication_permissions", publication_permissions,
        )
        if requested_publication_permissions != publication_permissions:
            raise CycleError(
                f"{role!r} stages must use publication_permissions="
                f"{publication_permissions!r}, not {requested_publication_permissions!r}"
            )
        dispatch_kwargs["publication_permissions"] = publication_permissions
        task = (f"{task}\n\n{publication_policy(role, publication_permissions)}"
                f"\n\n{ENVIRONMENT_RECOVERY_GUIDANCE}")
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
        if role == "resolve":
            effort_override = effort_override or self.resolve_effort
            if effort_override not in {"low", "medium", "high", "max"}:
                raise CycleError("resolve effort must be one of low, medium, high, max")
            stage_signals = {
                "escalated": bool(resolve_escalated),
                "escalation_finding_ids": list(escalation_finding_ids),
                "escalation_acceptance": "unknown" if resolve_escalated else "not_applicable",
            }
        else:
            stage_signals = {}
        self.stage_seq += 1
        self._latest_seq[role] = self.stage_seq

        # Observed once per stage, before the first routing: the diff does not
        # change between an attempt and its reroute.
        change = (
            self.change_observer()
            if self.change_observer is not None and role in CHANGE_OBSERVED_ROLES
            else None
        )
        self.latest_change = change
        parent_attempt_id = None
        parent_executor = None
        fallback_decision = None
        parent_failure = {}
        environment_context = ""
        primary_decision = None
        consent_requested = False
        # A primary, its fallback, and one rerun after a permission answer.
        for attempt in range(3):
            signals = {**self._pre_routing(role, change), **stage_signals, **parent_failure}
            if escalated:
                signals["escalated"] = True
            eligible = self.registry.compatible_executors(
                workspace_policy, **dispatch_kwargs,
            )
            def select(candidates):
                return route(
                    role, self.signals, self.availability,
                    mode=self.mode, profiles=self.profiles,
                    eligible_executors=candidates,
                    strategy=self.routing_strategy,
                    selector=selector,
                    first_pass_rate=(
                        self.first_pass_rate.value
                        if self.first_pass_rate is not None and self.first_pass_rate.known
                        else None
                    ),
                    rate_observations=(
                        self.first_pass_rate.observations
                        if self.first_pass_rate is not None else None
                    ),
                    rate_minimum=(
                        self.first_pass_rate.minimum
                        if self.first_pass_rate is not None else None
                    ),
                    rate_known=(
                        self.first_pass_rate.known
                        if self.first_pass_rate is not None else None
                    ),
                    rate_explanation=(
                        self.first_pass_rate.explain()
                        if self.first_pass_rate is not None else None
                    ),
                )

            decision = select(eligible)
            if role == "resolve" and not decision.blocked and decision.target is not None:
                if target_override is not None:
                    if (target_override.executor not in eligible
                            or not self.availability.get(
                                target_override.executor, Availability.UNKNOWN).dispatchable):
                        decision = replace(
                            decision, target=None, blocked=True,
                            reasons=decision.reasons +
                            ("the previous resolve target is no longer available",),
                        )
                    else:
                        decision = replace(
                            decision,
                            target=replace(target_override, effort="max"),
                            used_fallback=target_override_used_fallback,
                            reasons=decision.reasons +
                            ("a finding survived a claimed fix; retry at max on the previous target",),
                        )
                elif resolve_escalated and not decision.used_fallback:
                    # A resumed run has no previous resolve target; the seeded
                    # history still escalates, on the routed primary.
                    decision = replace(
                        decision, target=replace(decision.target, effort="max"),
                        reasons=decision.reasons +
                        ("a finding survived a claimed fix; retry at max on the routed primary",),
                    )
                elif not decision.used_fallback:
                    decision = replace(
                        decision, target=replace(decision.target, effort=effort_override),
                        reasons=decision.reasons +
                        (f"resolve primary effort pinned to {effort_override}",),
                    )
            if fallback_decision is not None:
                decision = fallback_decision

            # The Codex catalog is an optional pre-dispatch capability check.
            # Missing catalogs and unlisted models remain unknown and dispatch.
            if (role == "resolve" and not decision.blocked
                    and decision.target is not None and decision.target.executor == "codex"
                    and decision.target.effort == "max"):
                try:
                    from profile_config import codex_models
                    catalog = codex_models()
                except Exception:
                    catalog = None
                supported = catalog.get(decision.target.model) if catalog else None
                if supported is not None and decision.target.effort not in supported:
                    if resolve_escalated:
                        stage_signals["escalation_acceptance"] = "unsupported"
                        signals["escalation_acceptance"] = "unsupported"
                    fallback = stage_fallback(decision, self.profiles)
                    if (fallback is None or fallback.executor not in eligible
                            or not self.availability.get(
                                fallback.executor, Availability.UNKNOWN).dispatchable):
                        decision = replace(
                            decision, target=None, blocked=True,
                            reasons=decision.reasons +
                            (f"Codex does not support effort {decision.target.effort}; "
                             "the configured fallback is unavailable",),
                        )
                    else:
                        fallback_decision = replace(
                            decision, target=fallback, used_fallback=True,
                            reasons=decision.reasons +
                            ("Codex model cache confirms the requested effort is unsupported; using the configured fallback",),
                        )
                        decision = fallback_decision
            if (publishes and writes
                    and not decision.blocked and decision.target.executor == "codex"
                    and dispatch_kwargs.get("writable_dirs")):
                adapter = self.registry.get("codex")
                probe_write = getattr(adapter, "probe_git_metadata_write", None)
                if callable(probe_write):
                    allowed, detail = probe_write(
                        decision.target, dispatch_kwargs["cwd"],
                        tuple(dispatch_kwargs["writable_dirs"]),
                    )
                    if not allowed:
                        eligible = [name for name in eligible if name != "codex"]
                        eligible = [
                            name for name in eligible
                            if self.registry.get(name).publication_access(
                                (self.probes or {}).get(name)
                                or self.registry.get(name).probe(),
                                writes=True,
                            )[0]
                        ]
                        decision = select(eligible)
                        note = ("Codex linked-worktree Git metadata probe failed: "
                                f"{detail}; routed to "
                                f"{decision.target.executor if decision.target else 'no available executor'} "
                                "before dispatch.")
                        decision = replace(
                            decision, reasons=decision.reasons + (note,),
                        )
                        fallback_decision = None
            if attempt == 0:
                first = (decision, signals)
                primary_decision = decision
            if decision.blocked:
                outcome.decision = decision
                self._journal("failure", role=role, component="router",
                              status="blocked", reason="routing found no dispatchable target",
                              detail="; ".join(decision.reasons))
                outcome.rows.append(self._record(role, decision, None, signals))
                self.failed_attempts += 1
                self.stages.append(outcome)
                self._observe_shadow(role, *first)
                return outcome

            if self.stage_started is not None:
                self.stage_started(role, decision)

            # Per attempt: a reroute runs under the fallback's target, and its
            # run line has to name that one.
            started = time.monotonic()
            relationship = None
            if parent_attempt_id is not None:
                relationship = (
                    "fallback" if parent_executor != decision.target.executor else "retry"
                )
            skill = self.skills_by_role.get(role)
            attempt_id = None

            def begin_dispatch_attempt() -> str:
                nonlocal attempt_id
                harness_snapshot = build_harness_snapshot(
                    role=role, skill=skill, profile=decision.profile,
                    routing_strategy=getattr(
                        decision.strategy, "value", self.routing_strategy.value,
                    ),
                    readiness_policy=self.policy.value,
                    executor=decision.target.executor,
                    probe=(self.probes or {}).get(decision.target.executor),
                    profiles=self.profiles,
                    project_root=dispatch_kwargs.get("cwd"),
                )
                attempt_id = self.telemetry.create_dispatch_attempt(
                    self.repo_id, self.task_id, self.cycle_id, self.stage_seq, role,
                    executor=decision.target.executor,
                    provider=decision.target.provider,
                    model_requested=decision.target.model,
                    effort=decision.target.effort,
                    profile=decision.profile,
                    harness_snapshot=harness_snapshot,
                    parent_attempt_id=parent_attempt_id,
                    relationship=relationship,
                )
                if self.registry.get(decision.target.executor).completes_work:
                    self.telemetry.update_dispatch_attempt(
                        attempt_id, {"lifecycle_state": "running"},
                        update_id=f"update-{uuid.uuid4().hex}", source="runtime",
                    )
                target = decision.target
                self._journal("stage_start", role=role, attempt_id=attempt_id,
                              component=target.executor, status=decision.profile,
                              detail=f"{target.provider}/{target.model} {target.effort}",
                              correlation={"parent_attempt_id": parent_attempt_id,
                                           "relationship": relationship})
                return attempt_id

            tool_activity = False

            def snapshot():
                try:
                    return effect_snapshot(
                        dispatch_kwargs.get("cwd"), self.repo_id, publishes=publishes,
                        change_request_id=self.change_request_id,
                        issue_repo=self.work_item_identity_fields.get("work_item_repository"),
                        issue_id=self.task_id if publishes else None,
                    )
                except Exception:
                    return None

            before = (snapshot() if (writes or publishes)
                      and self.mode is not RoutingMode.CALIBRATION else None)

            def observe_progress(**activity):
                nonlocal tool_activity
                tool_activity |= bool(activity.get("tool"))
                if self.on_progress is not None:
                    self.on_progress(**activity)

            granted = (self.consent.granted(decision.target.executor)
                       if self.consent is not None else ())
            grant_kwargs = {"sandbox_grants": granted} if granted else {}
            try:
                result = dispatch(decision, f"{task}\n\n{environment_context}\n\n{routing_context(decision)}",
                                  self.registry,
                                  policy=self.policy, probes=self.probes,
                                  on_progress=observe_progress,
                                  on_workspace=self.on_workspace,
                                  on_dispatch_attempt=begin_dispatch_attempt,
                                  **dispatch_kwargs, **grant_kwargs)
            except CycleInterrupted as interruption:
                # No half-done state, even now: the stage that was running is
                # the row a later question about this run needs most.
                result = DispatchResult(
                    DispatchOutcome.INTERRUPTED, decision.target.executor, decision.target,
                    detail=f"interrupted by {interruption.signal_name}",
                    duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                    lifecycle_state="interrupted",
                    start_state="started" if tool_activity else "unknown",
                )
                if attempt_id is not None:
                    self.telemetry.update_dispatch_attempt(
                        attempt_id,
                        {"lifecycle_state": "interrupted", "outcome": "interrupted",
                         "duration_ms": result.duration_ms,
                         "finished_at": datetime.now(timezone.utc).isoformat(),
                         "error_code": "interrupted", "start_state": result.start_state},
                        update_id=f"update-{uuid.uuid4().hex}", source="runtime",
                    )
                outcome.decision = decision
                outcome.result = result
                if attempt_id is not None:
                    outcome.attempts.append((decision, result))
                outcome.rows.append(self._record(
                    role, decision, result, signals, attempt_id=attempt_id,
                    parent_attempt_id=parent_attempt_id, relationship=relationship,
                ))
                self.failed_attempts += 1
                self.stages.append(outcome)
                self._journal_attempt(role, result, attempt_id, None, signals)
                interruption.outcome = outcome
                raise
            if tool_activity:
                result = replace(result, start_state="started")
            if role == "resolve" and resolve_escalated:
                if (result.outcome is DispatchOutcome.SUCCEEDED
                        and result.requested.effort == "max"
                        and signals.get("escalation_acceptance") == "unknown"):
                    stage_signals["escalation_acceptance"] = "accepted"
                    signals["escalation_acceptance"] = "accepted"
                elif (result.failure_code != "unsupported_effort"
                      and result.outcome is not DispatchOutcome.SUCCEEDED
                      and result.requested.effort == "max" and not decision.used_fallback):
                    # Only the escalated dispatch can leave the acceptance
                    # unknown; a failed fallback keeps the signal already set.
                    stage_signals["escalation_acceptance"] = "unknown"
                    signals["escalation_acceptance"] = "unknown"

            def reroute_gate():
                # The #205 invariant: a stage that may have started is rerouted
                # only once its workspace effects are reconciled.
                if result.asynchronous or not result.process_exited:
                    return "stop", "exit_unproven"
                if result.needs_human_action:
                    return "stop", "human_action"
                if result.start_state == "not_started":
                    return "reroute", "not_started"
                if not writes and not publishes:
                    return "reroute", "no_effects"
                return reconcile(before, snapshot())

            retry = False
            if result.failure_code == "unsupported_effort" and role == "resolve":
                if resolve_escalated:
                    stage_signals["escalation_acceptance"] = "rejected"
                    signals["escalation_acceptance"] = "rejected"
                if attempt == 0:
                    fallback = stage_fallback(decision, self.profiles)
                    fallback_available = (
                        fallback is not None and fallback != decision.target
                        and fallback.executor in eligible
                        and self.availability.get(
                            fallback.executor, Availability.UNKNOWN).dispatchable
                    )
                    stopped_reason = "no available configured fallback"
                    if fallback_available:
                        if self.mode is RoutingMode.CALIBRATION:
                            action, reason = "stop", "calibration"
                        elif attempt_id is None:
                            action, reason = "stop", "exit_unproven"
                        else:
                            action, reason = reroute_gate()
                        signals.update(reroute_action=action, reroute_reason=reason,
                                       process_exited=result.process_exited,
                                       reroute_error_code="unsupported_effort",
                                       reroute_start_state=result.start_state)
                        retry = action == "reroute"
                        stopped_reason = f"reroute stopped: {reason}"
                    if retry:
                        fallback_decision = replace(
                            decision, target=fallback, used_fallback=True,
                            reasons=decision.reasons +
                            ("the CLI rejected the pinned resolve effort; using the configured fallback",),
                        )
                        parent_failure = {
                            "reroute_error_code": "unsupported_effort",
                            "reroute_start_state": result.start_state,
                            "reroute_action": "reroute", "reroute_reason": reason,
                        }
                    else:
                        result = replace(
                            result,
                            detail=f"{result.detail}; {stopped_reason} for unsupported effort",
                        )
            blocker = (sandbox_blocker(result.agent_output)
                       or denied_docker_blocker(result.agent_output,
                                                result.artifacts.get("permission_denials"))
                       if result.outcome is DispatchOutcome.SUCCEEDED
                       and not result.asynchronous else None)
            if blocker is not None:
                # A completed CLI call is not a completed functional stage.
                # This claim enables reconciliation, never grants permissions.
                result = replace(
                    result, outcome=DispatchOutcome.FAILED,
                    error_code="environment_restricted", lifecycle_state="failed",
                    detail=f"agent reported {blocker['kind']}: {blocker['error']}",
                )
            if result.failure_code in {"capacity", "transport", "timeout", "quota",
                                       "environment_restricted"}:
                action, reason = "stop", "retry_exhausted"
                fallback = stage_fallback(decision, self.profiles)
                if self.mode is RoutingMode.CALIBRATION:
                    reason = "calibration"
                elif attempt == 0 and attempt_id is not None:
                    action, reason = reroute_gate()
                    if action == "reroute":
                        if fallback is None or fallback == decision.target:
                            action, reason = "stop", "no_fallback"
                        elif (result.failure_code == "environment_restricted"
                              and fallback.executor == decision.target.executor):
                            action, reason = "stop", "same_sandbox"
                        elif (fallback.executor not in eligible or not self.availability.get(
                                fallback.executor, Availability.UNKNOWN).dispatchable):
                            action, reason = "stop", "fallback_unavailable"
                signals.update(reroute_action=action, reroute_reason=reason,
                               process_exited=result.process_exited,
                               reroute_error_code=result.failure_code,
                               reroute_start_state=result.start_state)
                retry = action == "reroute"
                if retry:
                    if blocker is not None:
                        environment_context = recovery_context(blocker)
                    fallback_decision = replace(
                        decision, target=fallback, used_fallback=True,
                        reasons=decision.reasons + (f"stage fallback after {result.failure_code}",),
                    )
                    parent_failure = {
                        "reroute_error_code": result.failure_code,
                        "reroute_start_state": result.start_state,
                        "reroute_action": action, "reroute_reason": reason,
                    }
                elif not (blocker is not None and self.consent is not None
                          and self.consent.grant_for(blocker) is not None
                          and reason != "calibration"):
                    diagnostic = ""
                    if result.failure_code == "environment_restricted" and reason == "publication_changed":
                        diagnostic = (
                            "; this attempt already published: preserve that result and "
                            "reconcile an explicit continuation before another executor runs. "
                            "An alternative executor has not demonstrated recovery; this is "
                            "an environment restriction, not model-quality evidence")
                    result = replace(result, detail=f"{result.detail}; reroute stopped: {reason}{diagnostic}")
            # Only when no executor could recover without a wider sandbox does
            # a person get asked: least privilege first.
            grant = (self.consent.grant_for(blocker)
                     if blocker is not None and self.consent is not None
                     and self.mode is not RoutingMode.CALIBRATION else None)
            answer = None
            # The grant is for the target that has a scope for it: a fallback
            # that hit the same wall hands the question back to the primary.
            consent_decision = decision
            if (grant is not None and grant.scope_for(
                    decision.target.executor, self.consent.platform) is None
                    and primary_decision is not None and primary_decision.target is not None
                    and grant.scope_for(primary_decision.target.executor,
                                        self.consent.platform) is not None):
                consent_decision = primary_decision
            if (grant is not None and not retry and not consent_requested
                    and grant.id not in self.consent.granted(consent_decision.target.executor)):
                consent_requested = True
                answer = self.consent.request(grant, consent_decision.target.executor, role=role)
            if answer == PENDING:
                # Nobody could answer: the sandbox stays as it is, no other
                # executor is tried in its place, and the cycle stops to ask.
                result = replace(result, detail=f"{result.detail}; permission required: {grant.id}")
                signals.update(reroute_action="stop", reroute_reason="permission_required",
                               process_exited=result.process_exited,
                               reroute_error_code="environment_restricted",
                               reroute_start_state=result.start_state)
            elif answer in {ALLOW_CYCLE, ALLOW_PERSISTENT, DENY}:
                # An answer permits access, not replaying effects. Reconcile
                # every full rerun, including one on the same executor.
                action, stop_reason = (("stop", "exit_unproven")
                                       if attempt_id is None else reroute_gate())
                safe = action == "reroute"
                signals.update(reroute_action="reroute" if safe else "stop",
                               reroute_reason="permission_answered" if safe else stop_reason,
                               process_exited=result.process_exited,
                               reroute_error_code="environment_restricted",
                               reroute_start_state=result.start_state)
                if safe:
                    scope = grant.scope_for(consent_decision.target.executor,
                                            self.consent.platform)
                    environment_context = recovery_context(blocker) + "\n\n" + (
                        denial_context(grant) if answer == DENY
                        else granted_context(grant, scope))
                    fallback_decision = replace(
                        consent_decision, reasons=consent_decision.reasons +
                        (f"rerun after the {grant.id} permission answer: {answer}",),
                    )
                    parent_failure = {
                        "reroute_error_code": "environment_restricted",
                        "reroute_start_state": result.start_state,
                        "reroute_action": "reroute", "reroute_reason": "permission_answered",
                    }
                    retry = True
                else:
                    result = replace(result, detail=f"{result.detail}; reroute stopped: {stop_reason}")
            elif grant is not None and not retry:
                result = replace(result, detail=f"{result.detail}; reroute stopped: "
                                                f"{signals.get('reroute_reason', 'no_fallback')}")
            state = result.lifecycle_state or (
                "launched" if result.asynchronous else
                "completed" if result.outcome is DispatchOutcome.SUCCEEDED else
                "interrupted" if result.outcome is DispatchOutcome.INTERRUPTED else "failed"
            )
            partial_result = {
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
            outcome.decision = decision
            outcome.result = result
            if attempt_id is not None:
                outcome.attempts.append((decision, result))
            outcome.rows.append(self._record(
                role, decision, result, signals, attempt_id=attempt_id,
                parent_attempt_id=parent_attempt_id, relationship=relationship,
            ))
            if result.outcome is not DispatchOutcome.SUCCEEDED:
                self.failed_attempts += 1
            self._journal_attempt(role, result, attempt_id, blocker, signals)

            if not retry:
                break
            parent_attempt_id = attempt_id
            parent_executor = decision.target.executor

        self.stages.append(outcome)
        self._observe_shadow(role, *first)
        return outcome

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
        preflight = (command_policy.get("native_sandbox_preflight")
                     if isinstance(command_policy, dict) else None)
        if isinstance(preflight, dict):
            # The cycle journal is the existing durable per-attempt evidence
            # store; keep only the explicit probes and their observed outcome.
            self._journal("sandbox_preflight", role=role, attempt_id=attempt_id,
                          component=executor, status=preflight.get("status"),
                          detail=preflight)
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
