"""Router v1: pick a profile for a role, conservatively.

Two calibration campaigns produced exactly one usable finding about models: on
five real work items, an inexpensive implementer never passed review on the
first attempt, at either effort level. Nothing separated the two effort levels
beyond that. So this router does not pretend to a precision the data does not
support. It routes by role and by a handful of declared task signals, and it
refuses to invent a ranking.

Three ideas carry most of the design.

**Availability is a gate before model choice, not a consequence of it.** Asking
which profile is best is pointless when its executor has no operating quota. The
gate runs first, and it is *told* what the executor's state is rather than
guessing: there is no documented way to query remaining quota, and inferring it
from a failed dispatch mistakes one exhausted window for evidence about a model.

**Production may fall back; a calibration never may.** Substituting an arm when
quota runs out answers a different question with the same sample. In
`RoutingMode.CALIBRATION` an unavailable executor blocks and waits.

**The expected cost of implementing includes the correction round.** Five out of
five implementations needed changes, so modelling `implement → approved` as the
typical case understates the real cost of the cheap profile. The estimate always
carries at least one resolution pass unless a repository has measured otherwise.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

SCHEMA_VERSION = 1


class Availability(str, Enum):
    """What is actually known about an executor, in increasing usefulness.

    `INSTALLED` is deliberately not enough to dispatch: a binary on PATH proves
    no session, no repository access and no quota. That distinction cost a
    campaign to learn.
    """

    UNKNOWN = "unknown"
    INSTALLED = "installed"
    AUTHENTICATED = "authenticated"
    QUOTA_EXHAUSTED = "quota_exhausted"
    READY = "ready"

    @property
    def dispatchable(self) -> bool:
        return self is Availability.READY


class RoutingMode(str, Enum):
    PRODUCTION = "production"
    CALIBRATION = "calibration"


class RoutingStrategy(str, Enum):
    """The configured policy for selecting a target."""

    FIXED = "fixed"
    MEASURED = "measured"


class RouterError(ValueError):
    """The router was asked for something it must not answer."""


def load_routing_strategy(config: dict | None = None) -> RoutingStrategy:
    """Return the configured routing strategy, defaulting to fixed."""
    section = (config or {}).get("code_cycle", {})
    routing = section.get("routing") if isinstance(section, dict) else None
    if routing is None:
        routing = {}
    elif not isinstance(routing, dict):
        raise RouterError(
            f"code_cycle.routing must be a mapping, not {type(routing).__name__}"
        )
    strategy = routing.get("strategy", RoutingStrategy.FIXED.value)
    try:
        return RoutingStrategy(strategy)
    except (TypeError, ValueError) as exc:
        raise RouterError(
            f"unknown code_cycle.routing.strategy value: {strategy!r} "
            "(expected 'fixed' or 'measured')"
        ) from exc


@dataclass(frozen=True)
class Target:
    """One concrete place work can be sent."""

    executor: str
    provider: str
    model: str
    effort: str

    def __str__(self) -> str:
        return f"{self.executor}:{self.provider}/{self.model} {self.effort}"


@dataclass(frozen=True)
class Profile:
    """A role name and where it resolves to, with an optional fallback."""

    name: str
    primary: Target
    fallback: Target | None = None

    def targets(self) -> tuple[Target, ...]:
        return (self.primary,) if self.fallback is None else (self.primary, self.fallback)


@dataclass(frozen=True)
class TaskSignals:
    """What is known about the work before a model is chosen.

    Labelled before routing, never after: choosing a model from a judgement and
    then measuring results by model measures the routing, not the models.
    """

    difficulty: int = 2
    verifiability: str = "auto"
    security_sensitive: bool = False

    def __post_init__(self) -> None:
        if self.difficulty not in (1, 2, 3):
            raise RouterError(f"difficulty must be 1, 2 or 3: {self.difficulty!r}")
        if self.verifiability not in ("auto", "partial", "human"):
            raise RouterError(f"unknown verifiability: {self.verifiability!r}")


@dataclass(frozen=True)
class CostEstimate:
    """Relative cost, in units of one cheap implementation pass.

    Deliberately relative. Absolute credits depend on a rate card that changes
    and on token counts nobody has measured here, and a number that looks
    precise invites more trust than it has earned.
    """

    implementation: float
    expected_resolutions: float
    review: float

    @property
    def total(self) -> float:
        return round(self.implementation + self.expected_resolutions + self.review, 3)

    def explain(self) -> str:
        return (
            f"implementation {self.implementation} + "
            f"{self.expected_resolutions} expected resolution work + "
            f"review {self.review} = {self.total}"
        )


@dataclass(frozen=True)
class RoutingDecision:
    profile: str
    target: Target | None
    mode: RoutingMode
    blocked: bool = False
    used_fallback: bool = False
    reasons: tuple[str, ...] = ()
    cost: CostEstimate | None = None
    strategy: RoutingStrategy = RoutingStrategy.FIXED
    rate_value: float | None = None
    rate_used: float | None = None
    rate_observations: int | None = None
    rate_minimum: int | None = None
    rate_known: bool | None = None
    rate_source: str = "default"
    rate_explanation: str | None = None
    cost_status: str | None = None

    def explain(self) -> str:
        head = "BLOCKED" if self.blocked else str(self.target)
        reasons = list(self.reasons)
        if self.rate_explanation:
            reasons.append(self.rate_explanation)
        if self.cost:
            reasons.append(f"cycle cost estimate: {self.cost.explain()}")
        elif self.cost_status == "unavailable":
            reasons.append("cycle cost estimate unavailable")
        return f"{self.profile} -> {head}: " + "; ".join(reasons)


# Conservative defaults. The escalation to a deeper profile happens on declared
# difficulty, not on a model's own guess about the work, and the two efforts
# remain separate roles because production may still want the distinction even
# though the calibration could not measure one.
DEFAULT_PROFILES: dict[str, dict] = {
    "cheap_tool":     {"primary": "claude:anthropic/claude-haiku-4-5-20251001 low"},
    "auxiliary_tool": {"primary": "codex:openai/gpt-5.6-luna medium"},
    "coordinator":    {"primary": "codex:openai/gpt-5.6-luna medium",
                       "fallback": "claude:anthropic/claude-sonnet-5 low"},
    "cheap_coder":    {"primary": "codex:openai/gpt-5.6-luna high",
                       "fallback": "claude:anthropic/claude-sonnet-5 high"},
    "deep_coder":     {"primary": "codex:openai/gpt-5.6-luna max",
                       "fallback": "claude:anthropic/claude-sonnet-5 high"},
    "reviewer":       {"primary": "codex:openai/gpt-5.6-terra high"},
    "senior_reviewer": {"primary": "codex:openai/gpt-5.6-terra max"},
    # Security audits are strict read-only stages. Claude remains available
    # for write-capable roles, but its adapter cannot enforce this boundary.
    "security":       {"primary": "codex:openai/gpt-5.6-terra high",
                       # Compatibility-only registry entry: the read-only
                       # eligibility filter deliberately never selects Claude.
                       "fallback": "claude:anthropic/claude-opus-5 high"},
}

# Relative cost per profile, same unit as CostEstimate.
PROFILE_COST = {
    "cheap_tool": 0.1,
    "auxiliary_tool": 0.1,
    "coordinator": 0.2,
    "cheap_coder": 1.0,
    "deep_coder": 2.5,
    "reviewer": 3.0,
    "senior_reviewer": 6.0,
    "security": 3.0,
}

# Five of five real implementations needed changes. Until a repository measures
# its own rate, assume the correction round happens.
DEFAULT_FIRST_PASS_RATE = 0.0


def parse_target(spec: str) -> Target:
    """Parse `executor:provider/model effort`."""
    try:
        where, effort = spec.rsplit(" ", 1)
        executor, rest = where.split(":", 1)
        provider, model = rest.split("/", 1)
    except ValueError as exc:
        raise RouterError(
            f"a target is 'executor:provider/model effort': {spec!r}"
        ) from exc
    if not all((executor, provider, model, effort)):
        raise RouterError(f"a target has an empty component: {spec!r}")
    return Target(executor.strip(), provider.strip(), model.strip(), effort.strip())


def load_profiles(config: dict | None = None) -> dict[str, Profile]:
    """Read `code_cycle.profiles` from parsed config, over the defaults.

    A repository declares only what it wants to change. An unknown profile name
    is refused rather than ignored: a typo that silently routes nowhere is worse
    than one that stops the run.

    The same applies to every shape below the name. A declaration is a mapping
    of target strings, and anything else is refused here — where the file is
    being read and the error can name the profile — rather than surviving into
    a `Profile` that only fails when something tries to route with it. A
    declared `primary: 3` used to be accepted and carried as the integer 3.
    """
    declared = {}
    if config:
        section = config.get("code_cycle")
        if section is not None and not isinstance(section, dict):
            # Silently reading the defaults out of a broken file would route by
            # a policy nobody declared, which is the failure this whole function
            # exists to prevent one level up.
            raise RouterError(
                f"code_cycle must be a mapping, not {type(section).__name__}")
        declared = (section or {}).get("profiles") or {}
    if not isinstance(declared, dict):
        raise RouterError("code_cycle.profiles must be a mapping")

    unknown = sorted(set(declared) - set(DEFAULT_PROFILES))
    if unknown:
        raise RouterError("unknown profile names: " + ", ".join(unknown))

    for name, spec in declared.items():
        if not isinstance(spec, dict):
            raise RouterError(
                f"profile {name!r} must be a mapping of targets, not "
                f"{type(spec).__name__}")
        for key in ("primary", "fallback"):
            value = spec.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise RouterError(
                    f"profile {name!r} declares a {key} that is not a target "
                    f"string: {value!r}")

    profiles = {}
    for name, spec in DEFAULT_PROFILES.items():
        merged = {**spec, **declared.get(name, {})}
        primary = merged.get("primary")
        if not primary:
            raise RouterError(f"profile {name!r} has no primary target")
        fallback = merged.get("fallback")
        profiles[name] = Profile(
            name=name,
            primary=parse_target(primary),
            fallback=parse_target(fallback) if fallback else None,
        )
    return profiles


def models_from_profiles(profiles: dict[str, Profile]) -> frozenset[str]:
    """Return the model names in an already-resolved profile set.

    Configuration belongs to the caller that loaded it. This small projection
    lets that caller inject the effective model vocabulary into the observer
    without making it read `.code-cycle.yml` itself.
    """
    return frozenset(
        target.model
        for profile in profiles.values()
        for target in profile.targets()
    )


#: The profiles a role may resolve to. A selector chooses among these and
#: nothing else, so which profiles a role can ever reach stays a property of
#: the router rather than of whichever selector is plugged in.
ROLE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "implement": ("cheap_coder", "deep_coder"),
    # Resolving findings is implementation work: it edits code and is judged by
    # tests, so it routes where implementation routes rather than to a reviewer
    # profile.
    "resolve": ("cheap_coder", "deep_coder"),
    "review": ("reviewer", "senior_reviewer"),
    "rereview": ("reviewer", "senior_reviewer"),
    "security": ("security",),
    "coordinate": ("coordinator",),
    "verify": ("auxiliary_tool",),
    "run": ("auxiliary_tool",),
    "bootstrap": ("auxiliary_tool",),
}

#: `selector(role, candidates, signals) -> (profile name, reasons)`.
#:
#: The seam where profile choice can be swapped without touching anything else
#: `route()` does. A selector returns a profile name only — never a target, a
#: model or an executor — and `route()` refuses a name outside `candidates`.
#: Availability, workspace policy, fallback, target resolution and dispatch all
#: stay in `route()` and its callers, so no selector can skip a gate.
ProfileSelector = Callable[
    [str, tuple[str, ...], TaskSignals], tuple[str, tuple[str, ...]]
]


def candidates_for(role: str) -> tuple[str, ...]:
    """The profiles a role is allowed to resolve to."""
    try:
        return ROLE_CANDIDATES[role]
    except KeyError:
        raise RouterError(f"unknown role: {role!r}") from None


def rule_selector(
    role: str, candidates: tuple[str, ...], signals: TaskSignals,
) -> tuple[str, tuple[str, ...]]:
    """The default selector: choose the profile name for a role, and say why.

    Only two rules escalate, and both come from declared signals rather than
    from a model's opinion about its own work:

    - a security-sensitive change goes to the senior reviewer;
    - difficulty 3 implementation work goes to the deeper coder.

    Everything else stays where the plan put it. There is no evidence for finer
    rules, and inventing them would make the router look calibrated when it is
    not.
    """
    if role == "security":
        return "security", ("a security audit always uses the security profile",)
    if role in ("implement", "resolve"):
        if signals.difficulty >= 3:
            return "deep_coder", ("declared difficulty 3 escalates to the deeper coder",)
        return "cheap_coder", ("difficulty below 3 starts on the cheap coder",)
    if role in ("review", "rereview"):
        if signals.security_sensitive:
            return "senior_reviewer", (
                "security-sensitive change reviewed by the senior profile",
            )
        return "reviewer", ("ordinary change uses the standard reviewer",)
    if role in ("coordinate", "verify", "run", "bootstrap"):
        return ("coordinator" if role == "coordinate" else "auxiliary_tool"), (
            "a step whose result is judged by execution, not by judgement",
        )
    raise RouterError(f"unknown role: {role!r}")


def select_profile(
    role: str,
    signals: TaskSignals,
    selector: ProfileSelector = rule_selector,
) -> tuple[str, tuple[str, ...]]:
    """Ask `selector` for a profile, and refuse anything outside the candidates.

    The check lives here, not in the selector, so a selector cannot invent a
    profile a role was never allowed to reach.
    """
    candidates = candidates_for(role)
    choice = selector(role, candidates, signals)
    if not (isinstance(choice, tuple) and len(choice) == 2):
        raise RouterError(
            f"a profile selector returns (profile, reasons), not {choice!r}")
    name, reasons = choice
    if not isinstance(name, str) or name not in candidates:
        raise RouterError(
            f"selector chose {name!r} for {role!r}, outside the candidates "
            f"{', '.join(candidates)}")
    if not (isinstance(reasons, tuple)
            and all(isinstance(reason, str) for reason in reasons)):
        raise RouterError(
            f"a profile selector's reasons are a tuple of strings: {reasons!r}")
    return name, reasons


def profile_for(role: str, signals: TaskSignals) -> tuple[str, tuple[str, ...]]:
    """Choose the profile name for a role with the default rules, and say why."""
    return select_profile(role, signals)


def estimate_cost(
    implement_profile: str,
    review_profile: str,
    *,
    first_pass_rate: float = DEFAULT_FIRST_PASS_RATE,
    resolve_profile: str | None = None,
) -> CostEstimate:
    """Cost of the whole cycle, not of the first implementation.

    Modelling `implement -> approved` as typical understates the cheap profile:
    every real implementation measured so far needed a correction round, and
    that round costs another implementation pass plus another review.
    """
    if not 0.0 <= first_pass_rate <= 1.0:
        raise RouterError(f"first_pass_rate must be between 0 and 1: {first_pass_rate}")
    resolve_profile = resolve_profile or implement_profile
    expected_rounds = 1.0 - first_pass_rate
    return CostEstimate(
        implementation=PROFILE_COST[implement_profile],
        expected_resolutions=round(
            expected_rounds * (PROFILE_COST[resolve_profile] + PROFILE_COST[review_profile]), 3
        ),
        review=PROFILE_COST[review_profile],
    )


def route(
    role: str,
    signals: TaskSignals,
    availability: dict[str, Availability],
    *,
    mode: RoutingMode = RoutingMode.PRODUCTION,
    profiles: dict[str, Profile] | None = None,
    eligible_executors: frozenset[str] | set[str] | None = None,
    strategy: RoutingStrategy = RoutingStrategy.FIXED,
    first_pass_rate: float | None = None,
    rate_observations: int | None = None,
    rate_minimum: int | None = None,
    rate_known: bool | None = None,
    rate_explanation: str | None = None,
    selector: ProfileSelector = rule_selector,
) -> RoutingDecision:
    """Resolve a role to a concrete target, or block.

    The gate runs before the choice: an executor that is merely installed or
    authenticated is not dispatchable, and one with exhausted quota is not a
    candidate whatever its profile scores.

    `selector` only names a profile among the role's candidates. Everything
    after that — policy, availability, fallback and the blocking rules — is
    decided here the same way whichever selector named it.
    """
    profiles = profiles or load_profiles()
    name, reasons = select_profile(role, signals, selector)
    profile = profiles[name]
    measured_strategy = strategy is RoutingStrategy.MEASURED
    measured_value = (
        first_pass_rate
        if measured_strategy and rate_known is not False else None
    )
    rate_used = (
        DEFAULT_FIRST_PASS_RATE if measured_value is None else measured_value
    )
    try:
        # Priced with the same selector, so the estimate describes the cycle
        # this selector would actually run.
        implement_profile, _ = select_profile("implement", signals, selector)
        review_profile, _ = select_profile("review", signals, selector)
        cost = estimate_cost(
            implement_profile, review_profile, first_pass_rate=rate_used,
        )
        cost_status = "available"
    except Exception:
        # Cost is informational. A missing price for a newly added profile or
        # a bad measurement must never prevent an otherwise valid route.
        cost = None
        cost_status = "unavailable"
    rate_source = (
        "measured" if measured_value is not None else "conservative_default"
    ) if measured_strategy else "default"
    decision_fields = {
        "cost": cost,
        "strategy": strategy,
        "rate_value": measured_value,
        "rate_used": rate_used,
        "rate_observations": rate_observations if measured_strategy else None,
        "rate_minimum": rate_minimum if measured_strategy else None,
        "rate_known": (
            measured_value is not None if measured_strategy else None
        ),
        "rate_source": rate_source,
        "rate_explanation": rate_explanation if measured_strategy else None,
        "cost_status": cost_status,
    }

    primary_state = availability.get(profile.primary.executor, Availability.UNKNOWN)
    primary_policy_reason = None
    for index, target in enumerate(profile.targets()):
        if eligible_executors is not None and target.executor not in eligible_executors:
            reasons = reasons + (
                f"{target.executor} cannot satisfy the workspace policy",
            )
            if index == 0:
                primary_policy_reason = (
                    f"primary executor {target.executor} cannot satisfy the workspace policy"
                )
            continue
        state = availability.get(target.executor, Availability.UNKNOWN)
        if state.dispatchable:
            if index == 0:
                return RoutingDecision(
                    name, target, mode, reasons=reasons, **decision_fields,
                )
            if mode is RoutingMode.CALIBRATION:
                break
            # Describe the primary with the primary's own state. Reusing the
            # fallback's state here reported a healthy primary while routing
            # around it, and a routing decision is only auditable if its reasons
            # are true.
            fallback_reason = primary_policy_reason or (
                f"primary executor {profile.primary.executor} is"
                f" {primary_state.value} -> fell back to {target.executor}"
            )
            if primary_policy_reason:
                fallback_reason += f" -> fell back to {target.executor}"
            return RoutingDecision(
                name,
                target,
                mode,
                used_fallback=True,
                reasons=reasons
                + (fallback_reason,),
                **decision_fields,
            )
        reasons = reasons + (f"{target.executor} is {state.value}",)

    if (eligible_executors is not None
            and not any(target.executor in eligible_executors
                        for target in profile.targets())):
        blocked_because = "no executor for this profile satisfies the workspace policy"
    else:
        blocked_because = (
            "a calibration never substitutes an arm: an unavailable executor blocks and waits"
            if mode is RoutingMode.CALIBRATION
            else "no executor for this profile is ready"
        )
    return RoutingDecision(
        name, None, mode, blocked=True, reasons=reasons + (blocked_because,),
        **decision_fields,
    )
