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

from dataclasses import dataclass, field

from executors import (
    Availability,
    DispatchOutcome,
    DispatchResult,
    ReadinessPolicy,
    Registry,
    dispatch,
)
from router import RoutingDecision, RoutingMode, TaskSignals, route
from telemetry import Telemetry

SCHEMA_VERSION = 1

#: Roles whose functional verdict is a review outcome rather than a dispatch one.
REVIEW_ROLES = frozenset({"review", "rereview"})


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
    ) -> None:
        self.telemetry = telemetry
        self.repo_id = repo_id
        self.task_id = task_id
        self.signals = signals
        self.availability = dict(availability)
        self.registry = registry or Registry()
        self.mode = mode
        self.policy = policy or ReadinessPolicy.for_mode(mode)
        self.iteration = 0
        self.stages: list[StageOutcome] = []

    def stage(self, role: str, task: str, **dispatch_kwargs) -> StageOutcome:
        """Route, dispatch and record. One call, no half-done state.

        Returns the outcome rather than raising on a blocked dispatch: a stage
        that could not run is a fact the caller has to act on, and it has
        already been written down by the time this returns.
        """
        outcome = StageOutcome(role=role, decision=None, result=None)  # type: ignore[arg-type]

        for attempt in range(2):
            decision = route(role, self.signals, self.availability,
                             mode=self.mode, profiles=None)
            if decision.blocked:
                outcome.decision = decision
                outcome.rows.append(self._record(role, decision, None))
                self.stages.append(outcome)
                return outcome

            result = dispatch(decision, task, self.registry,
                              policy=self.policy, **dispatch_kwargs)
            outcome.decision = decision
            outcome.result = result
            outcome.attempts.append((decision, result))
            outcome.rows.append(self._record(role, decision, result))

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
        return outcome

    def record_verdict(self, role: str, status: str, **fields) -> int:
        """Record a functional outcome that is not itself a dispatch.

        A review's verdict arrives after its dispatch succeeded, and it is the
        fact `first_pass_rate` actually reads. Recording it separately keeps the
        dispatch row honest about what the dispatch did.
        """
        if role in REVIEW_ROLES and not status:
            raise CycleError(f"a {role} verdict needs a status; an empty one counts as nothing")
        return self.telemetry.record_stage(
            self.repo_id, self.task_id, role,
            iteration=self.iteration, status=status, **fields,
        )

    def next_iteration(self) -> int:
        self.iteration += 1
        return self.iteration

    def close(self, final_status: str, **fields) -> int:
        """Record how the cycle ended, with the rounds it took."""
        return self.telemetry.record_stage(
            self.repo_id, self.task_id, "coordinate",
            iteration=self.iteration, status=final_status,
            iterations=self.iteration, **fields,
        )

    def _record(self, role: str, decision: RoutingDecision,
                result: DispatchResult | None) -> int:
        if result is None:
            # A blocked routing decision never reached an executor, so there is
            # no dispatch to describe — but the decision still happened, and a
            # run that stopped here would otherwise leave no trace of why.
            return self.telemetry.record_stage(
                self.repo_id, self.task_id, role,
                iteration=self.iteration,
                profile=decision.profile,
                used_fallback=decision.used_fallback,
                outcome=DispatchOutcome.BLOCKED.value,
                routing_reason_count=len(decision.reasons or ()),
            )
        return self.telemetry.record_dispatch(
            self.repo_id, self.task_id, role, decision, result,
            iteration=self.iteration,
            difficulty=self.signals.difficulty,
            verifiability=self.signals.verifiability,
            security_sensitive=self.signals.security_sensitive,
        )
