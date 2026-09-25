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

**It does not hide the rerouting.** When a dispatch discovers that an executor's
window is exhausted — the only moment that is knowable for a native executor —
the recorder updates availability and routes once more, and **both** decisions
are recorded. The abandoned one is the whole point: a fallback whose first
attempt left no trace makes fallbacks look free.

Friction a person must clear is the other half of that rule, and the one easier
to get wrong: a sign-in screen is evidence about availability too, so "we
learned something" is not a usable test for whether to go around it. The recorder asks the narrower question instead, and a
dispatch waiting on a human stops where it is, unrerouted and with its
availability untouched, so the question stays visible.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from executors import (
    Availability,
    DispatchOutcome,
    DispatchResult,
    ReadinessPolicy,
    Registry,
    WorkspacePolicy,
    dispatch,
)
from jev_shadow import JevShadow, telemetry_fields as shadow_fields
from router import (
    RoutingDecision,
    RoutingMode,
    RoutingStrategy,
    TaskSignals,
    models_from_profiles,
    route,
)
from stage_signals import CHANGE_OBSERVED_ROLES, RESOLUTION_ROLES, ChangeSignals
from telemetry import CYCLE_STARTS, Telemetry, routing_decision_fields, validate_reference

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

    This is a behavioural boundary. Agents keep their normal `gh`, `git` and
    network access; nothing here or in the adapters prevents an operation the
    contract does not list. The prompt states the rule, and the published
    comments and telemetry are how a person audits that it was followed.
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
    return (f"Routing for this stage, decided by the runtime: profile "
            f"`{decision.profile}`, requested model "
            f"`{target.provider}/{target.model}`, effort `{target.effort}`. "
            "Copy these values verbatim into any run line you write; do not "
            "infer them from your own configuration. Write the resolved model "
            "as `?`, because the executor has not reported it to you.")


class CycleError(ValueError):
    """The recorder was asked for something inconsistent."""


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
        shadow: JevShadow | None = None,
        stage_started: Callable[[str, RoutingDecision], None] | None = None,
        on_progress: Callable[..., None] | None = None,
    ) -> None:
        self.telemetry = telemetry
        # One per run, so two runs of the same work item stay apart. Checked
        # now rather than on the first row: a refused id found after a paid
        # dispatch leaves a stage nobody can record.
        self.cycle_id = validate_reference(
            "cycle_id", cycle_id or f"cycle-{uuid.uuid4().hex}")
        self.repo_id = repo_id
        self.task_id = task_id
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
        # Already resolved by whoever loaded the repository's configuration.
        # The recorder does not go looking for a file: a component that reads
        # configuration on its own is one that can disagree with the caller
        # about what the configuration says.
        self.profiles = profiles
        self.routing_strategy = routing_strategy
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
        # How many findings survived a claimed fix before the next routing.
        # Set by the driver that reads the structured results; `None` is a
        # recorder whose caller does not track it, and is left off the row.
        # Not `failed_attempts`: that counts dispatches, not fixes.
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
        self.tests_passed: bool | None = None
        # An observer of the rules' choice, never a selector: nothing it
        # returns reaches `route()` or a dispatch. `None` means no shadow at
        # all, so a disabled one costs no client, no key read and no I/O.
        self.shadow = shadow
        self.stage_started = stage_started
        self.on_progress = on_progress

    def stage(self, role: str, task: str, **dispatch_kwargs) -> StageOutcome:
        """Route, dispatch and record. One call, no half-done state.

        Returns the outcome rather than raising on a blocked dispatch: a stage
        that could not run is a fact the caller has to act on, and it has
        already been written down by the time this returns.
        """
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
        task = f"{task}\n\n{publication_policy(role, publication_permissions)}"
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
        self.stage_seq += 1
        self._latest_seq[role] = self.stage_seq

        # Observed once per stage, before the first routing: the diff does not
        # change between an attempt and its reroute.
        change = (
            self.change_observer()
            if self.change_observer is not None and role in CHANGE_OBSERVED_ROLES
            else None
        )
        for attempt in range(2):
            signals = self._pre_routing(role, change)
            eligible = self.registry.compatible_executors(
                workspace_policy, **dispatch_kwargs,
            )
            decision = route(
                role, self.signals, self.availability,
                mode=self.mode, profiles=self.profiles,
                eligible_executors=eligible,
                strategy=self.routing_strategy,
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
            if attempt == 0:
                first = (decision, signals)
            if decision.blocked:
                outcome.decision = decision
                outcome.rows.append(self._record(role, decision, None, signals))
                self.failed_attempts += 1
                self.stages.append(outcome)
                self._observe_shadow(role, *first)
                return outcome

            if self.stage_started is not None:
                self.stage_started(role, decision)

            # Per attempt: a reroute runs under the fallback's target, and its
            # run line has to name that one.
            result = dispatch(decision, f"{task}\n\n{routing_context(decision)}",
                              self.registry,
                              policy=self.policy, probes=self.probes,
                              on_progress=self.on_progress,
                              **dispatch_kwargs)
            outcome.decision = decision
            outcome.result = result
            outcome.attempts.append((decision, result))
            outcome.rows.append(self._record(role, decision, result, signals))
            if result.outcome is not DispatchOutcome.SUCCEEDED:
                self.failed_attempts += 1

            learned = result.learned_availability
            can_retry = (
                attempt == 0
                and learned is not None
                and not result.needs_human_action
                and self.mode is not RoutingMode.CALIBRATION
            )
            if not can_retry:
                break
            # The evidence a probe could not have had. Recorded already, above,
            # so the abandoned attempt keeps its row.
            self.availability[result.executor] = learned

        self.stages.append(outcome)
        self._observe_shadow(role, *first)
        return outcome

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
            **source, **fields,
        )
        # Tracked only after the row was accepted, so a refused verdict cannot
        # reach the closing row either.
        if role in REVIEW_ROLES and status in TERMINAL_REVIEW_STATUSES:
            if role == "review" and self.first_review_status is None:
                self.first_review_status = status
            self.final_review_status = status
        if fields.get("tests_passed") is not None:
            self.tests_passed = fields["tests_passed"]
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
            if self.started_from == "implement":
                outcome["first_pass_approved"] = first == "APPROVED"
        if self.final_review_status is not None:
            outcome.update(
                final_review_status=self.final_review_status,
                final_approved=self.final_review_status == "APPROVED",
            )
        if self.tests_passed is not None:
            outcome["tests_passed"] = self.tests_passed
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
                result: DispatchResult | None, signals: dict) -> int:
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
            )
        return self.telemetry.record_dispatch(
            self.repo_id, self.task_id, role, decision, result,
            iteration=self.iteration,
            local_only=self.local_only,
            started_from=self.started_from,
            cycle_id=self.cycle_id, stage_seq=self.stage_seq,
            record_kind="dispatch",
            **signals,
        )
