"""Execution profiles, objectives, and stage policies, resolved into a plan.

Three separate concepts, each recorded in the plan with where it came from:

- the **objective** states the outcome a cycle must reach (`local_change`,
  `verified_change`, `merge_ready`);
- the **execution profile** states how deep the cycle goes on its way there
  (`prototype`, `standard`, `full`, `hotfix`, `custom`);
- a **stage policy** (`disabled`, `auto`, `enabled`, `required`) adjusts one
  stage.

Everything here is a pure function of its arguments. Building a plan reads no
file, runs no command, and dispatches nothing; `run_cycle.py --dry-run` prints
what this module returns. Model routing (`code_cycle.profiles`) is a different
axis and is never read or changed here.

Settings resolve in layers: the compatible defaults, the profile, the
repository (`.code-cycle.yml`), and the invocation. A later layer replaces an
earlier one, but obligations are monotonic: a policy may add a requirement,
never remove one. A setting that would remove one is a conflict, rejected with
its source before any effect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from execution_plan import (
    ConditionalObligation,
    ControlRequirement,
    ExecutionPlan,
    ExecutionState,
    Obligation,
    ObligationState,
    PendingDecision,
    PlannedControl,
    PlannedStage,
    Resolution,
    StagePolicy,
)

PROFILES = ("prototype", "standard", "full", "hotfix", "custom")
DEFAULT_PROFILE = "standard"
OBJECTIVES = ("local_change", "verified_change", "merge_ready")
POLICIES = tuple(item.value for item in StagePolicy)

#: The stages a policy can address, in the order the cycle reaches them. A row
#: is not a dispatch: `verify` runs inside the implementation and `security`
#: inside the reviews, as `embedded_in` records.
PLANNED_STAGES = ("issue_review", "implement", "verify", "review", "security",
                  "resolve", "rereview")
CONTROLS = ("ci", "merge")

#: Where a resumed cycle starts, and the stages it leaves behind.
START_ORDER = ("implement", "review", "resolve", "rereview")

D, A, E, R = (StagePolicy.DISABLED, StagePolicy.AUTO, StagePolicy.ENABLED,
              StagePolicy.REQUIRED)
_STANDARD = {"issue_review": A, "implement": R, "verify": R, "review": R,
             "security": A, "resolve": A, "rereview": A}

#: The approved activation matrix (docs/ROADMAP_V2.md), restricted to the
#: stages the catalog has. Custom starts from Standard.
PROFILE_POLICIES: Mapping[str, Mapping[str, StagePolicy]] = {
    "prototype": {**_STANDARD, "review": D},
    "standard": dict(_STANDARD),
    "full": {**_STANDARD, "issue_review": R, "security": R},
    "hotfix": dict(_STANDARD),
    "custom": dict(_STANDARD),
}

PROFILE_INTENT = {
    "prototype": "fast experimentation with local output; publishing or asking "
                 "for approval activates the corresponding guarantees",
    "standard": "the current cycle: balanced depth, security, quality, cost and latency",
    "full": "every applicable declared control, at greater depth; no review is "
            "repeated without changes",
    "hotfix": "reproduce the defect, make the minimal change, and run a focused "
              "regression; no general exception to security or review",
    "custom": "Standard, adjusted by explicit stage policies; overrides never "
              "remove an obligation",
}

DEFAULT_OBJECTIVE = {"prototype": "local_change", "standard": "merge_ready",
                     "full": "merge_ready", "hotfix": "merge_ready",
                     "custom": "merge_ready"}

#: What each objective requires. `None` for verification means "as the stage
#: policy says".
OBJECTIVE_REQUIREMENTS: Mapping[str, Mapping[str, Any]] = {
    "local_change": {"verification": None, "review": False,
                     "ci": ControlRequirement.NOT_REQUIRED,
                     "merge": ControlRequirement.NOT_REQUIRED, "publication": "none"},
    "verified_change": {"verification": True, "review": True,
                        "ci": ControlRequirement.OPTIONAL,
                        "merge": ControlRequirement.NOT_REQUIRED,
                        "publication": "change_request"},
    "merge_ready": {"verification": True, "review": True,
                    "ci": ControlRequirement.REQUIRED,
                    "merge": ControlRequirement.REQUIRED,
                    "publication": "change_request"},
}

#: Layers, in the order they apply.
LAYERS = ("default", "profile", "repository", "invocation")
EXPLICIT_LAYERS = frozenset({"repository", "invocation"})

ISSUE_REVIEW_MODES = {"auto": StagePolicy.AUTO, "off": StagePolicy.DISABLED}
EXECUTION_KEYS = frozenset({"profile", "objective", "stages"})
STAGE_ASSIGNMENT = re.compile(r"^([a-z_]+)=([a-z_]+)$")


class ExecutionConfigError(ValueError):
    """An execution setting is malformed or contradicts an obligation."""


@dataclass(frozen=True)
class Setting:
    """One value and the place it came from."""

    value: str
    source: str
    layer: str

    def to_dict(self) -> dict[str, str]:
        return {"value": self.value, "source": self.source, "layer": self.layer}


@dataclass(frozen=True)
class ExecutionSettings:
    """What one layer, repository or invocation, explicitly declares."""

    profile: Setting | None = None
    objective: Setting | None = None
    stages: Mapping[str, Setting] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return self.profile is None and self.objective is None and not self.stages


@dataclass(frozen=True)
class StageSetting:
    policy: StagePolicy
    source: str
    layer: str
    overridden: tuple[Setting, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"policy": self.policy.value, "source": self.source, "layer": self.layer,
                "overridden": [item.to_dict() for item in self.overridden]}


@dataclass(frozen=True)
class ResolvedExecution:
    """The layered result, before the plan applies objective and start point."""

    profile: Setting
    objective: Setting
    stages: Mapping[str, StageSetting]
    ci_policy: Setting


@dataclass(frozen=True)
class PlanSignals:
    """The pre-routing signals a plan may read; anything else is unknown."""

    difficulty: int = 2
    security_sensitive: bool = False


# -- validation of each layer ------------------------------------------------

def _policy(value: Any, where: str) -> StagePolicy:
    try:
        return StagePolicy(value)
    except ValueError:
        raise ExecutionConfigError(
            f"{where}: {value!r} is not a stage policy; expected one of "
            + ", ".join(POLICIES)) from None


def _stage_id(value: Any, where: str) -> str:
    if value in CONTROLS:
        raise ExecutionConfigError(
            f"{where}: {value} is a control, not a stage; its timing is "
            "code_cycle.ci.policy or --ci-policy" if value == "ci" else
            f"{where}: {value} is a control, not a stage; merging stays an "
            "explicit human action")
    if value not in PLANNED_STAGES:
        raise ExecutionConfigError(
            f"{where}: unknown stage {value!r}; configurable stages are "
            + ", ".join(PLANNED_STAGES))
    return value


def _choice(value: Any, choices: Iterable[str], where: str, label: str) -> str:
    choices = tuple(choices)
    if value not in choices:
        raise ExecutionConfigError(
            f"{where}: unknown {label} {value!r}; expected one of " + ", ".join(choices))
    return value


def _merge_stage(stages: dict[str, Setting], stage: str, setting: Setting) -> None:
    """Two settings for one stage in one layer must agree."""
    earlier = stages.get(stage)
    if earlier is not None and earlier.value != setting.value:
        raise ExecutionConfigError(
            f"conflicting policies for {stage} in one layer: {earlier.source} sets "
            f"{earlier.value}, {setting.source} sets {setting.value}")
    stages.setdefault(stage, setting)


def repository_settings(config: Mapping[str, Any] | None,
                        label: str = ".code-cycle.yml") -> ExecutionSettings:
    """Validate and read `code_cycle.execution` and the switches it absorbs."""
    section = (config or {}).get("code_cycle") or {}
    if not isinstance(section, Mapping):
        return ExecutionSettings()
    execution = section.get("execution")
    if execution is None:
        execution = {}
    if not isinstance(execution, Mapping):
        raise ExecutionConfigError(
            f"{label}: code_cycle.execution must be a mapping, not {type(execution).__name__}")
    unknown = sorted(str(key) for key in set(execution) - EXECUTION_KEYS)
    if unknown:
        raise ExecutionConfigError(
            f"{label}: unknown key under code_cycle.execution: " + ", ".join(unknown)
            + "; known keys are " + ", ".join(sorted(EXECUTION_KEYS)))
    profile = objective = None
    if "profile" in execution:
        where = f"code_cycle.execution.profile in {label}"
        profile = Setting(_choice(execution["profile"], PROFILES, where, "execution profile"),
                          where, "repository")
    if "objective" in execution:
        where = f"code_cycle.execution.objective in {label}"
        objective = Setting(_choice(execution["objective"], OBJECTIVES, where, "objective"),
                            where, "repository")
    stages: dict[str, Setting] = {}
    declared = execution.get("stages")
    if declared is not None:
        if not isinstance(declared, Mapping):
            raise ExecutionConfigError(
                f"{label}: code_cycle.execution.stages must be a mapping")
        for stage, value in declared.items():
            where = f"code_cycle.execution.stages.{stage} in {label}"
            _stage_id(stage, where)
            stages[stage] = Setting(_policy(value, where).value, where, "repository")
    issue_review = section.get("issue_review")
    if isinstance(issue_review, Mapping) and "mode" in issue_review:
        mode = issue_review["mode"]
        if isinstance(mode, str) and mode in ISSUE_REVIEW_MODES:
            _merge_stage(stages, "issue_review", Setting(
                ISSUE_REVIEW_MODES[mode].value,
                f"code_cycle.issue_review.mode in {label}", "repository"))
    return ExecutionSettings(profile, objective, stages)


def parse_stage_assignment(text: str) -> tuple[str, StagePolicy]:
    """`STAGE=POLICY`, as `--stage` takes it."""
    match = STAGE_ASSIGNMENT.match(text or "")
    if not match:
        raise ExecutionConfigError(f"--stage {text!r}: expected STAGE=POLICY")
    stage, value = match.groups()
    where = f"--stage {text}"
    return _stage_id(stage, where), _policy(value, where)


def invocation_settings(*, execution_profile: str | None = None,
                        objective: str | None = None, local_only: bool = False,
                        stages: Iterable[str] = (),
                        issue_review: str | None = None) -> ExecutionSettings:
    """Validate and read the command-line layer."""
    profile = None
    if execution_profile is not None:
        profile = Setting(_choice(execution_profile, PROFILES, "--execution-profile",
                                  "execution profile"), "--execution-profile", "invocation")
    chosen = None
    if objective is not None:
        chosen = Setting(_choice(objective, OBJECTIVES, "--objective", "objective"),
                         "--objective", "invocation")
    if local_only:
        if chosen is not None and chosen.value != "local_change":
            raise ExecutionConfigError(
                f"--local-only selects objective local_change, but --objective "
                f"selects {chosen.value}; choose one")
        chosen = chosen or Setting("local_change", "--local-only", "invocation")
    parsed: dict[str, Setting] = {}
    for text in stages:
        stage, policy = parse_stage_assignment(text)
        _merge_stage(parsed, stage, Setting(policy.value, f"--stage {text}", "invocation"))
    if issue_review is not None:
        if issue_review not in ISSUE_REVIEW_MODES:
            raise ExecutionConfigError(
                f"--issue-review: unknown mode {issue_review!r}; expected auto or off")
        _merge_stage(parsed, "issue_review", Setting(
            ISSUE_REVIEW_MODES[issue_review].value, f"--issue-review {issue_review}",
            "invocation"))
    return ExecutionSettings(profile, chosen, parsed)


# -- layering ----------------------------------------------------------------

def resolve_execution(repository: ExecutionSettings, invocation: ExecutionSettings, *,
                      ci_policy: Setting | None = None) -> ResolvedExecution:
    """Apply defaults, profile, repository, and invocation, in that order."""
    profile = (invocation.profile or repository.profile
               or Setting(DEFAULT_PROFILE, "built-in default", "default"))
    if (repository.objective is not None and invocation.objective is not None
            and repository.objective.value != invocation.objective.value):
        raise ExecutionConfigError(
            f"conflicting explicit objectives: {repository.objective.source} selects "
            f"{repository.objective.value}, {invocation.objective.source} selects "
            f"{invocation.objective.value}; choose one")
    objective = (invocation.objective or repository.objective
                 or Setting(DEFAULT_OBJECTIVE[profile.value],
                            f"default of profile {profile.value}", "profile"))
    stages: dict[str, StageSetting] = {}
    for stage in PLANNED_STAGES:
        base = PROFILE_POLICIES[profile.value][stage]
        current = StageSetting(base, f"profile {profile.value}", "profile")
        for layer in (repository, invocation):
            setting = layer.stages.get(stage)
            if setting is None:
                continue
            current = StageSetting(
                StagePolicy(setting.value), setting.source, setting.layer,
                current.overridden + (Setting(current.policy.value, current.source,
                                              current.layer),))
        stages[stage] = current
    return ResolvedExecution(
        profile, objective, stages,
        ci_policy or Setting("strict", "built-in default", "default"))


# -- plan construction -------------------------------------------------------

@dataclass
class _Builder:
    resolved: ResolvedExecution
    signals: PlanSignals
    start_from: str
    continue_work: bool
    change_request: str | None
    stages: dict[str, PlannedStage] = field(default_factory=dict)
    decisions: list[PendingDecision] = field(default_factory=list)
    obligations: list[ConditionalObligation] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    effective: dict[str, StageSetting] = field(default_factory=dict)

    @property
    def profile(self) -> str:
        return self.resolved.profile.value

    @property
    def objective(self) -> str:
        return self.resolved.objective.value

    @property
    def publishes(self) -> bool:
        return OBJECTIVE_REQUIREMENTS[self.objective]["publication"] != "none"

    def setting(self, stage: str) -> StageSetting:
        return self.effective.get(stage) or self.resolved.stages[stage]

    def explicit(self, stage: str) -> bool:
        return self.setting(stage).layer in EXPLICIT_LAYERS

    def resumed_past(self, stage: str) -> bool:
        """Whether the start point lies after this stage."""
        order = {"issue_review": 0, "implement": 0, "verify": 0, "review": 1,
                 "resolve": 2, "rereview": 3}
        if stage == "issue_review" and self.continue_work:
            return True
        return START_ORDER.index(self.start_from) > order[stage]

    def resume_provenance(self) -> str:
        if self.continue_work:
            return "continued run (--continue)"
        return f"resumed run (--from {self.start_from} --pr {self.change_request})"

    def conflict(self, stage: str, why: str) -> None:
        self.conflicts.append(f"{stage}={self.setting(stage).policy.value} "
                              f"({self.setting(stage).source}): {why}")

    def raise_to_required(self, stage: str, source: str) -> None:
        """Add an obligation: monotonic, and visible as an override."""
        current = self.setting(stage)
        if current.policy is StagePolicy.REQUIRED:
            return
        self.effective[stage] = StageSetting(
            StagePolicy.REQUIRED, source, "obligation",
            current.overridden + (Setting(current.policy.value, current.source,
                                          current.layer),))

    def check_profile_obligation(self, stage: str) -> None:
        """A non-custom profile's `required` stage cannot be lowered."""
        if self.profile == "custom":
            return
        if (PROFILE_POLICIES[self.profile][stage] is StagePolicy.REQUIRED
                and self.setting(stage).policy is not StagePolicy.REQUIRED):
            self.conflict(stage, f"profile {self.profile} requires {stage}; an override "
                                 "cannot remove an obligation (use profile custom to "
                                 "configure it)")

    def add(self, stage: str, *, resolution: Resolution, selected: bool = False,
            required: bool = False, condition: str, reason: str,
            provenance: str | None = None, depends_on: Iterable[str] = (),
            embedded_in: str | None = None) -> None:
        setting = self.setting(stage)
        self.stages[stage] = PlannedStage(
            stage_id=stage,
            obligation=Obligation.REQUIRED if required else Obligation.OPTIONAL,
            selected=selected,
            state=ExecutionState.PLANNED if selected else ExecutionState.NOT_SELECTED,
            condition=condition, reason=reason,
            provenance=provenance or setting.source,
            depends_on=tuple(item for item in depends_on if item in self.stages and (
                self.stages[item].selected
                or self.stages[item].resolution is Resolution.PENDING)),
            policy=setting.policy, policy_source=setting.source,
            resolution=resolution, embedded_in=embedded_in,
        )

    def not_applicable(self, stage: str, reason: str, provenance: str,
                       embedded_in: str | None = None) -> None:
        self.add(stage, resolution=Resolution.NOT_APPLICABLE, condition="not applicable",
                 reason=reason, provenance=provenance, embedded_in=embedded_in)

    def pending(self, stage: str, awaits: str, required: bool, condition: str,
                reason: str, depends_on: Iterable[str] = (),
                embedded_in: str | None = None) -> None:
        self.add(stage, resolution=Resolution.PENDING, condition=condition,
                 reason=reason, depends_on=depends_on, embedded_in=embedded_in)
        self.decisions.append(PendingDecision(
            stage, awaits, required, condition, self.setting(stage).source))

    def disabled(self, stage: str, reason: str, embedded_in: str | None = None) -> None:
        self.add(stage, resolution=Resolution.DISABLED, condition="disabled",
                 reason=reason, embedded_in=embedded_in)

    def requires_publication(self, stage: str) -> bool:
        """Mark a stage that only exists for a published change request."""
        if self.publishes:
            return False
        if self.explicit(stage) and self.setting(stage).policy in {
                StagePolicy.ENABLED, StagePolicy.REQUIRED}:
            self.conflict(stage, f"objective {self.objective} publishes no change "
                                 f"request, so {stage} cannot run")
        self.not_applicable(stage, f"objective {self.objective} publishes no change request",
                            f"objective {self.objective} ({self.resolved.objective.source})")
        return True

    # -- one method per stage, in cycle order -------------------------------

    def issue_review(self) -> None:
        stage = "issue_review"
        if self.resumed_past(stage):
            self.not_applicable(stage, "a resumed or continued cycle does not review "
                                       "its work item again", self.resume_provenance())
            return
        self.check_profile_obligation(stage)
        policy = self.setting(stage).policy
        condition = "a new cycle reviews its work item before implementing it"
        if policy is StagePolicy.DISABLED:
            self.disabled(stage, f"disabled by {self.setting(stage).source}")
        elif policy is StagePolicy.AUTO and (
                self.signals.difficulty == 1 and not self.signals.security_sensitive):
            self.add(stage, resolution=Resolution.RESOLVED, condition=condition,
                     reason="auto: declared trivial (difficulty 1) and not "
                            "security-sensitive")
        else:
            reason = ("auto: not declared trivial; unknown risk is reviewed"
                      if policy is StagePolicy.AUTO else f"{policy.value} by "
                      f"{self.setting(stage).source}")
            self.add(stage, resolution=Resolution.RESOLVED, selected=True,
                     required=policy is StagePolicy.REQUIRED, condition=condition,
                     reason=reason)

    def implement(self) -> None:
        stage = "implement"
        if self.resumed_past(stage):
            self.not_applicable(stage, "the resumed change request is already implemented",
                                self.resume_provenance())
            return
        self.check_profile_obligation(stage)
        if self.setting(stage).policy is StagePolicy.DISABLED:
            self.conflict(stage, f"objective {self.objective} needs a change, and a new "
                                 "cycle has no other source for it")
            self.disabled(stage, "disabled")
            return
        self.raise_to_required(stage, f"objective {self.objective}")
        self.add(stage, resolution=Resolution.RESOLVED, selected=True, required=True,
                 condition="a new cycle produces the change",
                 reason=("continues the interrupted implementation" if self.continue_work
                         else f"objective {self.objective} needs a change"),
                 depends_on=("issue_review",))

    def verify(self) -> None:
        stage = "verify"
        if self.resumed_past(stage):
            self.not_applicable(stage, "verified by the resumed run's implementation",
                                self.resume_provenance(), embedded_in="implement")
            return
        self.check_profile_obligation(stage)
        policy = self.setting(stage).policy
        condition = "the project's essential verification of the change"
        needed = OBJECTIVE_REQUIREMENTS[self.objective]["verification"]
        if needed:
            if policy is StagePolicy.DISABLED:
                self.conflict(stage, f"objective {self.objective} requires verification")
                self.disabled(stage, "disabled", embedded_in="implement")
                return
            self.raise_to_required(stage, f"objective {self.objective}")
            policy = StagePolicy.REQUIRED
        if policy is StagePolicy.DISABLED:
            self.disabled(stage, f"disabled by {self.setting(stage).source}",
                          embedded_in="implement")
            return
        self.add(stage, resolution=Resolution.RESOLVED, selected=True,
                 required=policy is StagePolicy.REQUIRED, condition=condition,
                 reason="performed by the implementation stage; no separate dispatch",
                 depends_on=("implement",), embedded_in="implement")

    def review(self) -> None:
        stage = "review"
        if self.start_from in {"resolve", "rereview"}:
            self.not_applicable(stage, "the resumed change request was already reviewed",
                                self.resume_provenance())
            return
        if self.requires_publication(stage):
            return
        if self.setting(stage).policy is StagePolicy.DISABLED and self.explicit(stage):
            self.conflict(stage, f"objective {self.objective} requires an independent review")
            self.disabled(stage, "disabled")
            return
        self.check_profile_obligation(stage)
        self.raise_to_required(stage, f"objective {self.objective}")
        self.add(stage, resolution=Resolution.RESOLVED, selected=True, required=True,
                 condition="an implemented change request with an identifiable head",
                 reason=f"objective {self.objective} requires an independent review",
                 depends_on=("implement",))

    def security(self) -> None:
        stage = "security"
        review = self.stages.get("review")
        host = "review" if review is not None and review.selected else "rereview"
        if self.requires_publication(stage):
            return
        self.check_profile_obligation(stage)
        policy = self.setting(stage).policy
        rule = "security policy (code_cycle.security_review, else the built-in rule)"
        condition = "the change touches a trust boundary the security rule names"
        if self.signals.security_sensitive:
            if policy is StagePolicy.DISABLED:
                self.conflict(stage, "the work item is declared security-sensitive "
                                     "(--security-sensitive)")
                self.disabled(stage, "disabled", embedded_in=host)
                return
            self.raise_to_required(stage, "--security-sensitive")
        self.obligations.append(ConditionalObligation(
            "security_policy", stage, condition, rule,
            ObligationState.APPLIES if self.signals.security_sensitive
            else ObligationState.PENDING,
            "declared security-sensitive" if self.signals.security_sensitive else ""))
        if policy is StagePolicy.DISABLED:
            self.disabled(stage, f"disabled by {self.setting(stage).source}; if the "
                                 "security rule applies to the change, the plan is invalid",
                          embedded_in=host)
        elif self.signals.security_sensitive and host == "review":
            self.add(stage, resolution=Resolution.RESOLVED, selected=True, required=True,
                     condition=condition, reason="declared security-sensitive",
                     depends_on=(host,), embedded_in=host)
        else:
            required = self.setting(stage).policy is StagePolicy.REQUIRED
            self.pending(stage, "diff", required, condition,
                         "applicability needs the change; unknown risk is not read "
                         "as no risk", depends_on=(host,), embedded_in=host)

    def resolve(self) -> None:
        stage = "resolve"
        if self.start_from == "rereview":
            self.not_applicable(stage, "the resumed change request starts after its "
                                       "resolution", self.resume_provenance())
            return
        if self.requires_publication(stage):
            return
        self.check_profile_obligation(stage)
        policy = self.setting(stage).policy
        condition = "a review reported findings or failed required checks to fix"
        if self.start_from == "resolve":
            if policy is StagePolicy.DISABLED:
                self.conflict(stage, "--from resolve starts the cycle at this stage")
                self.disabled(stage, "disabled")
                return
            self.add(stage, resolution=Resolution.RESOLVED, selected=True,
                     required=policy is StagePolicy.REQUIRED, condition=condition,
                     reason="the resume start point", provenance=self.resume_provenance())
            return
        if policy is StagePolicy.DISABLED:
            self.disabled(stage, f"disabled by {self.setting(stage).source}; blocking "
                                 "findings stop the cycle for human intervention")
            return
        self.pending(stage, "review_outcome", policy is StagePolicy.REQUIRED, condition,
                     "runs only for concrete findings or failed checks",
                     depends_on=("review",))

    def rereview(self) -> None:
        stage = "rereview"
        if self.requires_publication(stage):
            return
        policy = self.setting(stage).policy
        condition = ("changes after the review invalidate its evidence; open "
                     "findings without fixes keep their blocks and are not reviewed again")
        self.obligations.append(ConditionalObligation(
            "rereview_after_changes", stage,
            "a new approval is requested after changes that invalidate the review",
            f"objective {self.objective}"))
        if self.start_from == "rereview":
            if policy is StagePolicy.DISABLED:
                self.conflict(stage, "--from rereview starts the cycle at this stage")
                self.disabled(stage, "disabled")
                return
            self.add(stage, resolution=Resolution.RESOLVED, selected=True, required=True,
                     condition=condition, reason="the resume start point",
                     provenance=self.resume_provenance())
            return
        if policy is StagePolicy.DISABLED:
            self.disabled(stage, f"disabled by {self.setting(stage).source}; changes after "
                                 "the review then make the plan invalid instead of approved")
            return
        # A head can move without a resolution, so only a selected one is a
        # dependency; the earlier review always is.
        resolve = self.stages.get("resolve")
        self.pending(stage, "post_review_changes", True, condition,
                     "selected only after relevant changes; never repeated on an "
                     "unchanged head",
                     depends_on=("review",) + (("resolve",) if resolve is not None
                                               and resolve.selected else ()))

    def controls(self) -> tuple[PlannedControl, ...]:
        objective = self.resolved.objective
        provenance = f"objective {objective.value} ({objective.source})"
        ci = self.resolved.ci_policy
        if not self.publishes:
            na = f"objective {objective.value} publishes no change request"
            return (
                PlannedControl("ci", ControlRequirement.NOT_REQUIRED,
                               Resolution.NOT_APPLICABLE, "not applicable", na, provenance,
                               timing=ci.value, timing_source=ci.source),
                PlannedControl("merge", ControlRequirement.NOT_REQUIRED,
                               Resolution.NOT_APPLICABLE, "not applicable", na, provenance),
            )
        requirements = OBJECTIVE_REQUIREMENTS[objective.value]
        return (
            PlannedControl(
                "ci", requirements["ci"], Resolution.RESOLVED,
                "every required check of the reviewed SHA passed",
                ("required for the reviewed SHA" if requirements["ci"] is
                 ControlRequirement.REQUIRED else "may run; not required"),
                provenance, timing=ci.value, timing_source=ci.source),
            PlannedControl(
                "merge", requirements["merge"], Resolution.RESOLVED,
                "an explicit human merge of the approved change request",
                ("required, and never automatic" if requirements["merge"] is
                 ControlRequirement.REQUIRED else
                 f"objective {objective.value} stops before merge"),
                provenance),
        )


def build_plan(resolved: ResolvedExecution, *, repository: str, work_item: str,
               signals: PlanSignals | None = None, start_from: str = "implement",
               continue_work: bool = False, change_request: str | None = None,
               plan_id: str | None = None, base_ref: str | None = None,
               provenance: str = "dry_run") -> ExecutionPlan:
    """The plan for one cycle, or an `ExecutionConfigError` naming every conflict."""
    if start_from not in START_ORDER:
        raise ExecutionConfigError(f"unknown start point {start_from!r}")
    builder = _Builder(resolved, signals or PlanSignals(), start_from, continue_work,
                       change_request)
    for stage in PLANNED_STAGES:
        getattr(builder, stage)()
    if builder.conflicts:
        raise ExecutionConfigError(
            "the execution configuration contradicts an obligation:\n  - "
            + "\n  - ".join(builder.conflicts))
    stages = {stage: builder.setting(stage) for stage in PLANNED_STAGES}
    plan = ExecutionPlan(
        plan_id=plan_id or f"{provenance}:{repository}#{work_item}",
        revision=1,
        repository=repository,
        work_item=work_item,
        objective=resolved.objective.value,
        execution_profile=resolved.profile.value,
        effective_configuration={
            "execution_profile": resolved.profile.to_dict(),
            "objective": resolved.objective.to_dict(),
            "stages": {stage: setting.to_dict() for stage, setting in stages.items()},
            "ci_policy": resolved.ci_policy.to_dict(),
            "start": {"from": start_from, "continue": continue_work,
                      "change_request": change_request},
            "signals": {"difficulty": builder.signals.difficulty,
                        "security_sensitive": builder.signals.security_sensitive},
        },
        effective_policy={
            "profile_intent": PROFILE_INTENT[resolved.profile.value],
            "objective_requirements": {
                key: (value.value if isinstance(value, ControlRequirement) else value)
                for key, value in OBJECTIVE_REQUIREMENTS[resolved.objective.value].items()
            },
            "layers": list(LAYERS),
        },
        plan_provenance=provenance,
        stages=tuple(builder.stages[stage] for stage in PLANNED_STAGES),
        base_ref=base_ref,
        decisions=tuple(builder.decisions),
        obligations=tuple(builder.obligations),
        controls=builder.controls(),
    )
    plan.validate()
    return plan


# -- rereview, once the review's outcome is known ---------------------------

@dataclass(frozen=True)
class RereviewDecision:
    applies: bool
    blocks_retained: bool
    reason: str


def rereview_decision(*, changes_after_review: bool, open_findings: bool) -> RereviewDecision:
    """The one rule for every profile, Full included.

    A rereview needs a relevant change after the review. Without one the review
    is not repeated: a clean review stands, and open findings keep blocking
    rather than being reviewed again in search of another verdict.
    """
    if changes_after_review:
        return RereviewDecision(True, False, "changes after the review invalidate its "
                                             "evidence; the current head is reviewed again")
    if open_findings:
        return RereviewDecision(False, True, "open findings without fixes keep their "
                                             "blocks; the same head is not reviewed again")
    return RereviewDecision(False, False, "clean review with no later changes; it is "
                                          "not repeated")


def apply_rereview_decision(plan: ExecutionPlan, decision: RereviewDecision,
                            provenance: str = "cycle observation") -> ExecutionPlan:
    """The next plan revision with the rereview selected or ruled out."""
    if plan.stage("rereview").resolution is Resolution.PENDING:
        return plan.resolve_decision("rereview", decision.applies, decision.reason,
                                     provenance)
    obligation = next((item for item in plan.obligations
                       if item.obligation_id == "rereview_after_changes"), None)
    if obligation is None:
        raise ExecutionConfigError("this plan has no rereview decision to make")
    return plan.settle_obligation("rereview_after_changes", decision.applies,
                                  decision.reason)


# -- the readable form -------------------------------------------------------

def render_text(plan: ExecutionPlan) -> str:
    """The dry-run for a person: what would run, what waits, and why."""
    configuration = plan.effective_configuration
    lines = [
        f"execution plan (contract v{plan.contract_version}, revision {plan.revision}) "
        f"for {plan.repository} work item {plan.work_item}",
        f"  profile:   {plan.execution_profile} "
        f"({configuration['execution_profile']['source']})",
        f"  objective: {plan.objective} ({configuration['objective']['source']})",
        f"  intent:    {plan.effective_policy['profile_intent']}",
        "  dry run: no stage, worktree, dependency preparation, grant or agent was started",
        "",
        "stages:",
    ]
    decisions = {item.subject: item for item in plan.decisions}
    for stage in plan.stages:
        if stage.resolution is Resolution.RESOLVED:
            status = ("required" if stage.obligation is Obligation.REQUIRED else "selected"
                      ) if stage.selected else "omitted"
        elif stage.resolution is Resolution.PENDING:
            decision = decisions[stage.stage_id]
            status = (f"pending{' (required when it applies)' if decision.required else ''}"
                      f" until {decision.awaits}")
        else:
            status = stage.resolution.value.replace("_", " ")
        lines.append(f"  {stage.stage_id:<13} {status}")
        lines.append(f"      policy {stage.policy.value if stage.policy else '-'} "
                     f"from {stage.policy_source or '-'}")
        overridden = configuration["stages"][stage.stage_id]["overridden"]
        for item in overridden:
            lines.append(f"      overrides {item['value']} from {item['source']}")
        lines.append(f"      {stage.reason}")
        if stage.embedded_in:
            lines.append(f"      runs within {stage.embedded_in}")
        if stage.depends_on:
            lines.append(f"      after {', '.join(stage.depends_on)}")
    lines += ["", "controls:"]
    for control in plan.controls:
        timing = (f", timing {control.timing} from {control.timing_source}"
                  if control.timing else "")
        lines.append(f"  {control.control_id:<13} {control.requirement.value.replace('_', ' ')}"
                     f" ({control.resolution.value.replace('_', ' ')}){timing}")
        lines.append(f"      {control.reason}")
    if plan.obligations:
        lines += ["", "conditional obligations:"]
        for obligation in plan.obligations:
            lines.append(f"  {obligation.obligation_id} on {obligation.subject}: "
                         f"{obligation.state.value.replace('_', ' ')}")
            lines.append(f"      when {obligation.condition}; source {obligation.source}")
    return "\n".join(lines)
