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


class RouterError(ValueError):
    """The router was asked for something it must not answer."""


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

    def explain(self) -> str:
        head = "BLOCKED" if self.blocked else str(self.target)
        return f"{self.profile} -> {head}: " + "; ".join(self.reasons)


# Conservative defaults. The escalation to a deeper profile happens on declared
# difficulty, not on a model's own guess about the work, and the two efforts
# remain separate roles because production may still want the distinction even
# though the calibration could not measure one.
DEFAULT_PROFILES: dict[str, dict] = {
    "cheap_tool":     {"primary": "claude:anthropic/claude-haiku-4-5-20251001 low"},
    "coordinator":    {"primary": "codex:openai/gpt-5.6-luna medium",
                       "fallback": "claude:anthropic/claude-sonnet-5 low"},
    "cheap_coder":    {"primary": "codex:openai/gpt-5.6-luna high",
                       "fallback": "claude:anthropic/claude-sonnet-5 high"},
    "deep_coder":     {"primary": "codex:openai/gpt-5.6-luna max",
                       "fallback": "claude:anthropic/claude-sonnet-5 high"},
    "reviewer":       {"primary": "codex:openai/gpt-5.6-terra high"},
    "senior_reviewer": {"primary": "codex:openai/gpt-5.6-terra max"},
    "security":       {"primary": "claude:anthropic/claude-opus-5 high"},
}

# Relative cost per profile, same unit as CostEstimate.
PROFILE_COST = {
    "cheap_tool": 0.1,
    "coordinator": 0.2,
    "cheap_coder": 1.0,
    "deep_coder": 2.5,
    "reviewer": 3.0,
    "senior_reviewer": 6.0,
    "security": 6.0,
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


def profile_for(role: str, signals: TaskSignals) -> tuple[str, tuple[str, ...]]:
    """Choose the profile name for a role, and say why.

    Only two rules escalate, and both come from declared signals rather than
    from a model's opinion about its own work:

    - a security-sensitive change goes to the security profile for its audit;
    - difficulty 3 implementation work goes to the deeper coder.

    Everything else stays where the plan put it. There is no evidence for finer
    rules, and inventing them would make the router look calibrated when it is
    not.
    """
    reasons = []
    if role == "security":
        return "security", ("a security audit always uses the security profile",)
    if role in ("implement", "resolve"):
        # Resolving findings is implementation work: it edits code and is judged
        # by tests, so it routes where implementation routes rather than to a
        # reviewer profile.
        if signals.difficulty >= 3:
            return "deep_coder", ("declared difficulty 3 escalates to the deeper coder",)
        return "cheap_coder", ("difficulty below 3 starts on the cheap coder",)
    if role in ("review", "rereview"):
        if signals.security_sensitive:
            reasons.append("security-sensitive change reviewed by the senior profile")
            return "senior_reviewer", tuple(reasons)
        return "reviewer", ("ordinary change uses the standard reviewer",)
    if role in ("coordinate", "verify", "run", "bootstrap"):
        return ("coordinator" if role == "coordinate" else "cheap_tool"), (
            "a step whose result is judged by execution, not by judgement",
        )
    raise RouterError(f"unknown role: {role!r}")


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
) -> RoutingDecision:
    """Resolve a role to a concrete target, or block.

    The gate runs before the choice: an executor that is merely installed or
    authenticated is not dispatchable, and one with exhausted quota is not a
    candidate whatever its profile scores.
    """
    profiles = profiles or load_profiles()
    name, reasons = profile_for(role, signals)
    profile = profiles[name]

    primary_state = availability.get(profile.primary.executor, Availability.UNKNOWN)
    for index, target in enumerate(profile.targets()):
        state = availability.get(target.executor, Availability.UNKNOWN)
        if state.dispatchable:
            if index == 0:
                return RoutingDecision(name, target, mode, reasons=reasons)
            if mode is RoutingMode.CALIBRATION:
                break
            # Describe the primary with the primary's own state. Reusing the
            # fallback's state here reported a healthy primary while routing
            # around it, and a routing decision is only auditable if its reasons
            # are true.
            return RoutingDecision(
                name,
                target,
                mode,
                used_fallback=True,
                reasons=reasons
                + (f"primary executor {profile.primary.executor} is"
                   f" {primary_state.value} -> fell back to {target.executor}",),
            )
        reasons = reasons + (f"{target.executor} is {state.value}",)

    blocked_because = (
        "a calibration never substitutes an arm: an unavailable executor blocks and waits"
        if mode is RoutingMode.CALIBRATION
        else "no executor for this profile is ready"
    )
    return RoutingDecision(
        name, None, mode, blocked=True, reasons=reasons + (blocked_because,)
    )
