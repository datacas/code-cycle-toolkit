"""Static stage metadata and the versioned contract for a cycle execution plan.

This module describes work; it does not route or execute it.  A stage's role,
responsibility, output contract, and permissions live together here so callers
can keep compatibility views without maintaining independent policy maps.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

from executors import WorkspacePolicy
from issue_review import ROLE as ISSUE_REVIEW_ROLE, SKILL as ISSUE_REVIEW_SKILL


CONTRACT_VERSION = 2

#: Versions `ExecutionPlan.from_dict` reads and `to_dict` can write. Version 1
#: is read by upgrading it and written only as a lossless projection.
SUPPORTED_CONTRACT_VERSIONS = (1, 2)


class ContractError(ValueError):
    """The plan or its static stage metadata is inconsistent or unsupported."""


@dataclass(frozen=True)
class RoleContract:
    """Workspace and publication permissions for one agent role."""

    workspace_policy: WorkspacePolicy
    publishes: bool = False
    publication_permissions: tuple[str, ...] = ()


class StageKind(str, Enum):
    DISPATCH = "dispatch"
    CONTROL = "control"


class Obligation(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class ExecutionState(str, Enum):
    NOT_SELECTED = "not_selected"
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"


class ExecutorKind(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"
    ORCA = "orca"


class StagePolicy(str, Enum):
    """What a profile, repository, or invocation asks of one stage."""

    DISABLED = "disabled"
    AUTO = "auto"
    ENABLED = "enabled"
    REQUIRED = "required"


class Resolution(str, Enum):
    """Whether the plan has decided a stage or control, and how."""

    PENDING = "pending"
    RESOLVED = "resolved"
    DISABLED = "disabled"
    NOT_APPLICABLE = "not_applicable"


class ObligationState(str, Enum):
    PENDING = "pending"
    APPLIES = "applies"
    NOT_APPLICABLE = "not_applicable"


class ControlRequirement(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    NOT_REQUIRED = "not_required"


#: The signals a pending decision can wait for. Each is observed by the cycle
#: after a stage ran; none can be known when the plan is first built.
AWAITED_SIGNALS = frozenset({"diff", "review_outcome", "post_review_changes"})


@dataclass(frozen=True)
class StageDefinition:
    """Static contract for one dispatch role in the current cycle."""

    stage_id: str
    role: str
    responsibility: str
    output_contract: str
    workspace_policy: WorkspacePolicy
    publishes: bool = False
    publication_permissions: tuple[str, ...] = ()
    skill: str | None = None
    function: str | None = None
    completes_with: frozenset[str] = frozenset()
    kind: StageKind = StageKind.DISPATCH


@dataclass(frozen=True)
class ControlDefinition:
    """A runtime-owned gate, distinct from an agent role or executor."""

    control_id: str
    responsibility: str
    output_contract: str
    function: str


# This catalog is the source for the role contracts and driver adapters below.
# Output contract names point at existing result contracts; they do not copy
# their findings, checks, or evidence payloads.
STAGES: tuple[StageDefinition, ...] = (
    StageDefinition(
        ISSUE_REVIEW_ROLE, ISSUE_REVIEW_ROLE, "Assess work-item readiness",
        "ISSUE_REVIEW_RESULT", WorkspacePolicy.READ_ONLY,
        skill=ISSUE_REVIEW_SKILL, function="issue_review.assess",
        completes_with=frozenset({"READY"}),
    ),
    StageDefinition(
        "bootstrap", "bootstrap", "Resolve provider and repository context",
        "PROVIDER_BOOTSTRAP_RESULT", WorkspacePolicy.READ_ONLY,
        skill="cc-provider-bootstrap",
    ),
    StageDefinition(
        "implement", "implement", "Implement the work item",
        "ORCHESTRATION_RESULT", WorkspacePolicy.WORKSPACE_WRITE, True,
        ("comment", "create_pr", "push_branch"), "cc-implement-issue",
        completes_with=frozenset({"IMPLEMENTED"}),
    ),
    StageDefinition(
        "verify", "verify", "Verify a change", "VERIFICATION_RESULT",
        WorkspacePolicy.DISPOSABLE, skill="cc-verify",
    ),
    StageDefinition(
        "review", "review", "Review a change request",
        "ORCHESTRATION_RESULT", WorkspacePolicy.READ_ONLY, True,
        ("comment",), "cc-initial-review",
        completes_with=frozenset({"APPROVED", "CHANGES_REQUESTED"}),
    ),
    StageDefinition(
        "resolve", "resolve", "Resolve valid review findings",
        "ORCHESTRATION_RESULT", WorkspacePolicy.WORKSPACE_WRITE, True,
        ("comment", "push_branch"), "cc-resolve-comments",
        completes_with=frozenset({"RESOLVED", "PARTIALLY_RESOLVED"}),
    ),
    StageDefinition(
        "rereview", "rereview", "Verify fixes and review the current change",
        "ORCHESTRATION_RESULT", WorkspacePolicy.READ_ONLY, True,
        ("comment",), "cc-rereview",
        completes_with=frozenset({"APPROVED", "CHANGES_REQUESTED"}),
    ),
    StageDefinition(
        "security", "security", "Audit changed trust boundaries",
        "SECURITY_REVIEW_RESULT", WorkspacePolicy.READ_ONLY,
        skill="cc-security-review",
    ),
    StageDefinition(
        "run", "run", "Start and inspect project services", "RUN_RESULT",
        WorkspacePolicy.DISPOSABLE, skill="cc-run",
    ),
    StageDefinition(
        "coordinate", "coordinate", "Coordinate cycle stages",
        "ORCHESTRATION_RESULT", WorkspacePolicy.READ_ONLY,
        function="run_cycle.main",
    ),
)

CONTROLS: tuple[ControlDefinition, ...] = (
    ControlDefinition("ci_gate", "Wait for checks of the reviewed head", "CI_GATE_RESULT", "ci_gate.wait_for_checks"),
    ControlDefinition("security_gate", "Select whether a security review is required", "SECURITY_GATE_RESULT", "security_gate.evaluate"),
    ControlDefinition("worktree", "Select and validate the implementation workspace", "WORKTREE_RESULT", "run_cycle.select_workspace"),
)

#: The controls a plan records as outcomes of the cycle, and the runtime
#: control that implements each. Merge has none: it is never automatic.
PLAN_CONTROLS: Mapping[str, str | None] = {"ci": "ci_gate", "merge": None}
CI_TIMINGS = frozenset({"strict", "parallel"})


def _index_unique(items: tuple[Any, ...], key: str, label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        value = getattr(item, key)
        if value in result:
            raise ContractError(f"duplicate {label}: {value}")
        result[value] = item
    return result


STAGES_BY_ID = _index_unique(STAGES, "stage_id", "stage")
STAGES_BY_ROLE = _index_unique(STAGES, "role", "role")
CONTROLS_BY_ID = _index_unique(CONTROLS, "control_id", "control")

for _stage in STAGES:
    if not _stage.output_contract or not (_stage.skill or _stage.function):
        raise ContractError(f"stage {_stage.stage_id} requires an output contract and responsibility")

if set(STAGES_BY_ID) & set(CONTROLS_BY_ID):
    raise ContractError("stage and control identifiers must be distinct")

for _control, _implementation in PLAN_CONTROLS.items():
    if _control in STAGES_BY_ID or (
            _implementation is not None and _implementation not in CONTROLS_BY_ID):
        raise ContractError(f"plan control {_control} is inconsistent with the catalog")


def stage_definition(stage_id: str) -> StageDefinition:
    """Return known stage metadata, rejecting unknown IDs before dispatch."""
    try:
        return STAGES_BY_ID[stage_id]
    except KeyError as exc:
        raise ContractError(f"unknown stage: {stage_id}") from exc


def control_definition(control_id: str) -> ControlDefinition:
    """Return known control metadata, rejecting unknown controls."""
    try:
        return CONTROLS_BY_ID[control_id]
    except KeyError as exc:
        raise ContractError(f"unknown control: {control_id}") from exc


def role_contracts() -> dict[str, RoleContract]:
    """Build the legacy cycle.RoleContract map from the static catalog."""
    return {
        definition.role: RoleContract(
            definition.workspace_policy,
            publishes=definition.publishes,
            publication_permissions=definition.publication_permissions,
        )
        for definition in STAGES
    }


def skills_for_roles() -> dict[str, str]:
    """Return the historical role-to-skill compatibility view."""
    legacy_roles = {"issue_review", "implement", "review", "resolve", "rereview"}
    return {
        item.role: item.skill
        for item in STAGES
        if item.role in legacy_roles and item.skill is not None
    }


def completion_statuses() -> dict[str, frozenset[str]]:
    """Return role completion adapters for the driver."""
    return {item.role: item.completes_with for item in STAGES if item.completes_with}


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class PlannedStage:
    """One selected or omitted stage and why it has that disposition."""

    stage_id: str
    obligation: Obligation
    selected: bool
    state: ExecutionState
    condition: str
    reason: str
    provenance: str
    executor: ExecutorKind | None = None
    depends_on: tuple[str, ...] = ()
    conclusion: str | None = None
    # Version 2. `policy` is None only for a plan upgraded from version 1.
    policy: StagePolicy | None = None
    policy_source: str | None = None
    resolution: Resolution = Resolution.RESOLVED
    embedded_in: str | None = None

    def v2_fields(self) -> dict[str, Any]:
        return {
            "policy": self.policy.value if self.policy is not None else None,
            "policy_source": self.policy_source,
            "resolution": self.resolution.value,
            "embedded_in": self.embedded_in,
        }

    def has_v2_fields(self) -> bool:
        return (self.policy is not None or self.policy_source is not None
                or self.resolution is not Resolution.RESOLVED
                or self.embedded_in is not None)

    def to_dict(self, contract_version: int = CONTRACT_VERSION) -> dict[str, Any]:
        payload = {
            "stage_id": self.stage_id,
            "obligation": self.obligation.value,
            "selected": self.selected,
            "state": self.state.value,
            "condition": self.condition,
            "reason": self.reason,
            "provenance": self.provenance,
            "executor": self.executor.value if self.executor is not None else None,
            "depends_on": list(self.depends_on),
            "conclusion": self.conclusion,
        }
        if contract_version >= 2:
            payload.update(self.v2_fields())
        return payload


@dataclass(frozen=True)
class PendingDecision:
    """A selection the plan cannot make until the cycle observes a signal."""

    subject: str
    awaits: str
    required: bool
    condition: str
    provenance: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject, "awaits": self.awaits, "required": self.required,
            "condition": self.condition, "provenance": self.provenance,
        }


@dataclass(frozen=True)
class ConditionalObligation:
    """A requirement that holds only when its condition applies.

    No stage policy removes one: a disabled stage whose obligation applies
    makes the plan invalid rather than the obligation void.
    """

    obligation_id: str
    subject: str
    condition: str
    source: str
    state: ObligationState = ObligationState.PENDING
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id, "subject": self.subject,
            "condition": self.condition, "source": self.source,
            "state": self.state.value, "reason": self.reason,
        }


@dataclass(frozen=True)
class PlannedControl:
    """A gate on the cycle's outcome, kept apart from the dispatched stages."""

    control_id: str
    requirement: ControlRequirement
    resolution: Resolution
    condition: str
    reason: str
    provenance: str
    timing: str | None = None
    timing_source: str | None = None
    automatic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id, "requirement": self.requirement.value,
            "resolution": self.resolution.value, "condition": self.condition,
            "reason": self.reason, "provenance": self.provenance,
            "timing": self.timing, "timing_source": self.timing_source,
            "automatic": self.automatic,
        }


#: Where a version-1 projection keeps what version 1 has no field for, so a
#: control or a stage policy is carried explicitly instead of dropped.
V1_PROJECTION_KEY = "contract_v2"

_V1_STAGE_KEYS = frozenset({
    "stage_id", "obligation", "selected", "state", "condition", "reason",
    "provenance", "executor", "depends_on", "conclusion",
})
_V2_STAGE_KEYS = _V1_STAGE_KEYS | {"policy", "policy_source", "resolution", "embedded_in"}
_V2_PLAN_KEYS = frozenset({"decisions", "obligations", "controls"})


@dataclass(frozen=True)
class ExecutionPlan:
    """Versioned, serializable plan metadata for a single cycle."""

    plan_id: str
    revision: int
    repository: str
    work_item: str
    objective: str
    execution_profile: str
    effective_configuration: Mapping[str, Any]
    effective_policy: Mapping[str, Any]
    plan_provenance: str
    stages: tuple[PlannedStage, ...]
    contract_version: int = CONTRACT_VERSION
    base_ref: str | None = None
    head_sha: str | None = None
    review: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    decisions: tuple[PendingDecision, ...] = ()
    obligations: tuple[ConditionalObligation, ...] = ()
    controls: tuple[PlannedControl, ...] = ()

    def validate(self) -> None:
        if type(self.contract_version) is not int:
            raise ContractError("contract version must be an integer")
        if self.contract_version != CONTRACT_VERSION:
            raise ContractError(f"unsupported execution-plan contract version: {self.contract_version}")
        if type(self.revision) is not int or self.revision < 1:
            raise ContractError("plan revision must be a positive integer")
        if any(not isinstance(value, str) for value in (
            self.plan_id, self.repository, self.work_item, self.objective,
            self.execution_profile, self.plan_provenance,
        )):
            raise ContractError("plan identity, execution profile, and provenance must be strings")
        if (not self.plan_id.strip() or not self.repository.strip()
                or not self.work_item.strip() or not self.objective.strip()):
            raise ContractError(
                "plan identity requires plan_id, repository, work_item, and objective"
            )
        if not self.execution_profile.strip() or not self.plan_provenance.strip():
            raise ContractError("execution profile and plan provenance are required")
        if not all(isinstance(value, Mapping) for value in (
            self.effective_configuration, self.effective_policy, self.review, self.metadata,
        )):
            raise ContractError("configuration, policy, review, and metadata must be objects")
        if any(value is not None and not isinstance(value, str) for value in (
            self.base_ref, self.head_sha,
        )):
            raise ContractError("base_ref and head_sha must be strings or null")
        for name in ("stages", "decisions", "obligations", "controls"):
            if not isinstance(getattr(self, name), (tuple, list)):
                raise ContractError(f"{name} must be a sequence")
        if any(not isinstance(stage, PlannedStage) for stage in self.stages):
            raise ContractError("stages must contain planned-stage objects")
        seen: set[str] = set()
        if any(not isinstance(stage.stage_id, str) for stage in self.stages):
            raise ContractError("stage ID must be a string")
        stage_ids = {stage.stage_id for stage in self.stages}
        stages_by_id = {stage.stage_id: stage for stage in self.stages}
        for stage in self.stages:
            definition = stage_definition(stage.stage_id)
            if stage.stage_id in seen:
                raise ContractError(f"duplicate planned stage: {stage.stage_id}")
            seen.add(stage.stage_id)
            if not isinstance(stage.obligation, Obligation):
                raise ContractError(f"stage {stage.stage_id} has an invalid obligation")
            if type(stage.selected) is not bool:
                raise ContractError(f"stage {stage.stage_id} selected must be a boolean")
            if not isinstance(stage.state, ExecutionState):
                raise ContractError(f"stage {stage.stage_id} has an invalid execution state")
            if any(not isinstance(value, str) for value in (
                stage.condition, stage.reason, stage.provenance,
            )):
                raise ContractError(f"stage {stage.stage_id} condition, reason, and provenance must be strings")
            if (not stage.condition.strip() or not stage.reason.strip()
                    or not stage.provenance.strip()):
                raise ContractError(
                    f"stage {stage.stage_id} requires condition, reason, and provenance"
                )
            if stage.conclusion is not None and not isinstance(stage.conclusion, str):
                raise ContractError(f"stage {stage.stage_id} conclusion must be a string")
            if stage.executor is not None and not isinstance(stage.executor, ExecutorKind):
                raise ContractError(f"stage {stage.stage_id} has an unknown executor")
            if stage.selected and stage.state is ExecutionState.NOT_SELECTED:
                raise ContractError(f"selected stage {stage.stage_id} cannot be not_selected")
            if not stage.selected and stage.state is not ExecutionState.NOT_SELECTED:
                raise ContractError(f"omitted stage {stage.stage_id} must be not_selected")
            if stage.obligation is Obligation.REQUIRED and not stage.selected:
                raise ContractError(f"required stage {stage.stage_id} cannot be omitted")
            if stage.conclusion is not None and stage.state is not ExecutionState.COMPLETED:
                raise ContractError(f"stage {stage.stage_id} cannot have a conclusion before completion")
            self._validate_stage_v2(stage, stage_ids)
            if not isinstance(stage.depends_on, (tuple, list)) or any(
                not isinstance(dependency, str) for dependency in stage.depends_on
            ):
                raise ContractError(f"stage {stage.stage_id} dependencies must be stage IDs")
            if len(set(stage.depends_on)) != len(stage.depends_on):
                raise ContractError(f"stage {stage.stage_id} has duplicate dependencies")
            for dependency in stage.depends_on:
                stage_definition(dependency)
                if dependency == stage.stage_id or dependency not in stage_ids:
                    raise ContractError(
                        f"stage {stage.stage_id} has invalid dependency {dependency}"
                    )
                if stage.selected and not stages_by_id[dependency].selected:
                    raise ContractError(
                        f"selected stage {stage.stage_id} depends on omitted stage {dependency}"
                    )
                if (stage.resolution is Resolution.PENDING
                        and not stages_by_id[dependency].selected
                        and stages_by_id[dependency].resolution is not Resolution.PENDING):
                    raise ContractError(
                        f"pending stage {stage.stage_id} depends on omitted stage {dependency}"
                    )
            if definition.kind is not StageKind.DISPATCH:
                raise ContractError(f"planned stage {stage.stage_id} is not dispatchable")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(stage_id: str) -> None:
            if stage_id in visiting:
                raise ContractError(f"cyclic stage dependency involving {stage_id}")
            if stage_id in visited:
                return
            visiting.add(stage_id)
            for dependency in stages_by_id[stage_id].depends_on:
                visit(dependency)
            visiting.remove(stage_id)
            visited.add(stage_id)

        for stage_id in stage_ids:
            visit(stage_id)

        controls_by_id = self._validate_controls()
        self._validate_decisions(stages_by_id, controls_by_id)
        self._validate_obligations(stages_by_id, controls_by_id)

    @staticmethod
    def _validate_stage_v2(stage: PlannedStage, stage_ids: set[str]) -> None:
        name = stage.stage_id
        if stage.policy is not None and not isinstance(stage.policy, StagePolicy):
            raise ContractError(f"stage {name} has an invalid policy")
        if stage.policy_source is not None and not _text(stage.policy_source):
            raise ContractError(f"stage {name} policy source must be a non-empty string")
        if stage.policy_source is not None and stage.policy is None:
            raise ContractError(f"stage {name} records a policy source without a policy")
        if not isinstance(stage.resolution, Resolution):
            raise ContractError(f"stage {name} has an invalid resolution")
        if stage.resolution is not Resolution.RESOLVED and (
                stage.selected or stage.state is not ExecutionState.NOT_SELECTED):
            raise ContractError(
                f"stage {name} is {stage.resolution.value} and cannot be selected")
        if (stage.resolution is Resolution.DISABLED) != (stage.policy is StagePolicy.DISABLED
                                                         and stage.resolution is not Resolution.NOT_APPLICABLE):
            raise ContractError(
                f"stage {name}: only a disabled policy resolves as disabled")
        if stage.embedded_in is not None:
            if (not isinstance(stage.embedded_in, str) or stage.embedded_in == name
                    or stage.embedded_in not in stage_ids):
                raise ContractError(f"stage {name} has an invalid host stage")

    def _validate_controls(self) -> dict[str, PlannedControl]:
        result: dict[str, PlannedControl] = {}
        for control in self.controls:
            if not isinstance(control, PlannedControl):
                raise ContractError("controls must contain planned-control objects")
            if control.control_id not in PLAN_CONTROLS:
                raise ContractError(f"unknown plan control: {control.control_id}")
            if control.control_id in result:
                raise ContractError(f"duplicate plan control: {control.control_id}")
            result[control.control_id] = control
            if not isinstance(control.requirement, ControlRequirement):
                raise ContractError(f"control {control.control_id} has an invalid requirement")
            if not isinstance(control.resolution, Resolution):
                raise ContractError(f"control {control.control_id} has an invalid resolution")
            if control.resolution is Resolution.DISABLED:
                raise ContractError(f"control {control.control_id} cannot be disabled")
            if not all(_text(value) for value in (
                    control.condition, control.reason, control.provenance)):
                raise ContractError(
                    f"control {control.control_id} requires condition, reason, and provenance")
            if (control.resolution is Resolution.NOT_APPLICABLE
                    and control.requirement is not ControlRequirement.NOT_REQUIRED):
                raise ContractError(
                    f"control {control.control_id} is not applicable and cannot be required")
            if type(control.automatic) is not bool:
                raise ContractError(f"control {control.control_id} automatic must be a boolean")
            if control.control_id == "merge" and control.automatic:
                raise ContractError("the merge control is never automatic")
            if control.timing is not None and (
                    control.control_id != "ci" or control.timing not in CI_TIMINGS):
                raise ContractError(f"control {control.control_id} has an invalid timing")
            if control.timing_source is not None and (
                    control.timing is None or not _text(control.timing_source)):
                raise ContractError(f"control {control.control_id} has an invalid timing source")
        return result

    def _validate_decisions(self, stages: dict[str, PlannedStage],
                            controls: dict[str, PlannedControl]) -> None:
        decided: set[str] = set()
        for decision in self.decisions:
            if not isinstance(decision, PendingDecision):
                raise ContractError("decisions must contain pending-decision objects")
            subject = stages.get(decision.subject) or controls.get(decision.subject)
            if subject is None:
                raise ContractError(f"pending decision for unknown subject: {decision.subject}")
            if subject.resolution is not Resolution.PENDING:
                raise ContractError(
                    f"pending decision for {decision.subject}, which is not pending")
            if decision.subject in decided:
                raise ContractError(f"duplicate pending decision: {decision.subject}")
            decided.add(decision.subject)
            if decision.awaits not in AWAITED_SIGNALS:
                raise ContractError(
                    f"pending decision for {decision.subject} awaits an unknown signal")
            if type(decision.required) is not bool:
                raise ContractError(f"pending decision for {decision.subject} required must be a boolean")
            if not _text(decision.condition) or not _text(decision.provenance):
                raise ContractError(
                    f"pending decision for {decision.subject} requires condition and provenance")
        for subject_id, subject in {**stages, **controls}.items():
            if subject.resolution is Resolution.PENDING and subject_id not in decided:
                raise ContractError(f"{subject_id} is pending without a pending decision")

    def _validate_obligations(self, stages: dict[str, PlannedStage],
                              controls: dict[str, PlannedControl]) -> None:
        seen: set[str] = set()
        for obligation in self.obligations:
            if not isinstance(obligation, ConditionalObligation):
                raise ContractError("obligations must contain conditional-obligation objects")
            if not _text(obligation.obligation_id) or obligation.obligation_id in seen:
                raise ContractError(f"invalid or duplicate obligation: {obligation.obligation_id}")
            seen.add(obligation.obligation_id)
            if not isinstance(obligation.state, ObligationState):
                raise ContractError(f"obligation {obligation.obligation_id} has an invalid state")
            if not _text(obligation.condition) or not _text(obligation.source):
                raise ContractError(
                    f"obligation {obligation.obligation_id} requires condition and source")
            if not isinstance(obligation.reason, str):
                raise ContractError(f"obligation {obligation.obligation_id} reason must be a string")
            subject = stages.get(obligation.subject) or controls.get(obligation.subject)
            if subject is None:
                raise ContractError(
                    f"obligation {obligation.obligation_id} names unknown subject {obligation.subject}")
            if obligation.state is ObligationState.APPLIES and isinstance(subject, PlannedStage):
                if subject.resolution is Resolution.NOT_APPLICABLE:
                    raise ContractError(
                        f"stage {subject.stage_id} is not applicable but obligation "
                        f"{obligation.obligation_id} applies ({obligation.source})")
                if subject.resolution is Resolution.DISABLED:
                    raise ContractError(
                        f"stage {subject.stage_id} is disabled but obligation "
                        f"{obligation.obligation_id} applies ({obligation.source}); "
                        "a stage policy cannot remove an obligation")
                if subject.resolution is Resolution.RESOLVED and not subject.selected:
                    raise ContractError(
                        f"stage {subject.stage_id} is omitted but obligation "
                        f"{obligation.obligation_id} applies ({obligation.source})")
            if obligation.state is ObligationState.APPLIES and isinstance(subject, PlannedControl):
                if (subject.resolution is Resolution.NOT_APPLICABLE
                        or subject.requirement is not ControlRequirement.REQUIRED):
                    raise ContractError(
                        f"control {subject.control_id} must be required when obligation "
                        f"{obligation.obligation_id} applies ({obligation.source})")

    # -- queries ------------------------------------------------------------

    def stage(self, stage_id: str) -> PlannedStage:
        for item in self.stages:
            if item.stage_id == stage_id:
                return item
        raise ContractError(f"stage {stage_id} is not in the plan")

    def control(self, control_id: str) -> PlannedControl:
        for item in self.controls:
            if item.control_id == control_id:
                return item
        raise ContractError(f"control {control_id} is not in the plan")

    def pending_required_decisions(self) -> tuple[PendingDecision, ...]:
        return tuple(item for item in self.decisions if item.required)

    def check_executable(self, stage_id: str) -> None:
        """Refuse to run a stage the plan has not selected.

        A pending decision, required or not, must be resolved by a new plan
        revision first: running it would invent the outcome of a condition the
        cycle has not observed, and skipping a required one would drop it.
        """
        self.validate()
        stage = self.stage(stage_id)
        if stage.resolution is Resolution.PENDING:
            decision = next(item for item in self.decisions if item.subject == stage_id)
            kind = "a pending required decision" if decision.required else "pending"
            raise ContractError(
                f"stage {stage_id} cannot run: its selection is {kind} "
                f"awaiting {decision.awaits}")
        if not stage.selected:
            raise ContractError(
                f"stage {stage_id} cannot run: it is {stage.resolution.value}, not selected")

    def resolve_decision(self, subject: str, applies: bool, reason: str,
                         provenance: str) -> ExecutionPlan:
        """Return the next revision with one pending decision settled.

        Obligations still pending on the same subject settle with it, so an
        applicable obligation makes the stage required.
        """
        decision = next((item for item in self.decisions if item.subject == subject), None)
        if decision is None:
            raise ContractError(f"no pending decision for {subject}")
        if type(applies) is not bool or not _text(reason) or not _text(provenance):
            raise ContractError("a resolved decision requires applies, reason, and provenance")
        state = ObligationState.APPLIES if applies else ObligationState.NOT_APPLICABLE
        obligations = tuple(
            replace(item, state=state, reason=reason)
            if item.subject == subject and item.state is ObligationState.PENDING else item
            for item in self.obligations
        )
        bound = any(item.subject == subject and item.state is ObligationState.APPLIES
                    for item in obligations)
        required = applies and (decision.required or bound)
        stages = tuple(
            replace(
                item, resolution=Resolution.RESOLVED, selected=applies,
                state=ExecutionState.PLANNED if applies else ExecutionState.NOT_SELECTED,
                obligation=Obligation.REQUIRED if required else Obligation.OPTIONAL,
                reason=reason, provenance=provenance,
            ) if item.stage_id == subject else item
            for item in self.stages
        )
        controls = tuple(
            replace(
                item, resolution=Resolution.RESOLVED,
                requirement=(ControlRequirement.REQUIRED if required
                             else item.requirement if applies
                             else ControlRequirement.NOT_REQUIRED),
                reason=reason, provenance=provenance,
            ) if item.control_id == subject else item
            for item in self.controls
        )
        plan = replace(
            self, revision=self.revision + 1, stages=stages, controls=controls,
            obligations=obligations,
            decisions=tuple(item for item in self.decisions if item.subject != subject),
        )
        plan.validate()
        return plan

    def settle_obligation(self, obligation_id: str, applies: bool, reason: str) -> ExecutionPlan:
        """Return the next revision with one obligation applied or ruled out.

        An obligation that applies to a disabled stage makes the plan invalid:
        that is the conflict the `disabled` policy has to explain.
        """
        if not any(item.obligation_id == obligation_id for item in self.obligations):
            raise ContractError(f"no obligation {obligation_id}")
        if type(applies) is not bool or not _text(reason):
            raise ContractError("a settled obligation requires applies and reason")
        state = ObligationState.APPLIES if applies else ObligationState.NOT_APPLICABLE
        plan = replace(self, revision=self.revision + 1, obligations=tuple(
            replace(item, state=state, reason=reason)
            if item.obligation_id == obligation_id else item
            for item in self.obligations
        ))
        plan.validate()
        return plan

    # -- serialization ------------------------------------------------------

    def to_dict(self, contract_version: int = CONTRACT_VERSION) -> dict[str, Any]:
        """Validate and serialize, at the current version or a lossless older one."""
        self.validate()
        if type(contract_version) is not int:
            raise ContractError(f"unsupported execution-plan contract version: {contract_version!r}")
        if contract_version == CONTRACT_VERSION:
            return {
                **self._common_dict(dict(self.effective_policy), contract_version),
                "decisions": [item.to_dict() for item in self.decisions],
                "obligations": [item.to_dict() for item in self.obligations],
                "controls": [item.to_dict() for item in self.controls],
            }
        if contract_version == 1:
            return self._project_v1()
        raise ContractError(f"unsupported execution-plan contract version: {contract_version}")

    def _common_dict(self, effective_policy: dict[str, Any],
                     contract_version: int) -> dict[str, Any]:
        return {
            "contract_version": contract_version,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "repository": self.repository,
            "work_item": self.work_item,
            "objective": self.objective,
            "execution_profile": self.execution_profile,
            "effective_configuration": dict(self.effective_configuration),
            "effective_policy": effective_policy,
            "plan_provenance": self.plan_provenance,
            "base_ref": self.base_ref,
            "head_sha": self.head_sha,
            "review": dict(self.review),
            "stages": [stage.to_dict(contract_version) for stage in self.stages],
            "metadata": dict(self.metadata),
        }

    def _project_v1(self) -> dict[str, Any]:
        """Version 1 for an older reader, refusing what it cannot carry.

        Version 1 has no field for a pending decision or a pending obligation,
        and a reader would take the stage as omitted. So those refuse the
        projection; settled obligations, controls, and stage policies travel
        under `effective_policy.contract_v2` rather than disappearing.
        """
        pending = [f"decision for {item.subject}" for item in self.decisions]
        pending += [f"obligation {item.obligation_id}" for item in self.obligations
                    if item.state is ObligationState.PENDING]
        if pending:
            raise ContractError(
                "execution-plan contract version 1 cannot represent the pending "
                + ", ".join(pending) + "; negotiate version 2")
        carried: dict[str, Any] = {}
        stages = {item.stage_id: item.v2_fields() for item in self.stages if item.has_v2_fields()}
        if stages:
            carried["stages"] = stages
        if self.controls:
            carried["controls"] = [item.to_dict() for item in self.controls]
        if self.obligations:
            carried["obligations"] = [item.to_dict() for item in self.obligations]
        policy = dict(self.effective_policy)
        if carried:
            if V1_PROJECTION_KEY in policy:
                raise ContractError(
                    f"effective_policy already holds {V1_PROJECTION_KEY}; "
                    "the version 1 projection would overwrite it")
            policy[V1_PROJECTION_KEY] = carried
        return self._common_dict(policy, 1)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExecutionPlan:
        try:
            if not isinstance(payload, Mapping):
                raise ContractError("execution plan must be an object")
            version = payload["contract_version"]
            if type(version) is not int or version not in SUPPORTED_CONTRACT_VERSIONS:
                raise ContractError(f"unsupported execution-plan contract version: {version!r}")
            if version == 1 and _V2_PLAN_KEYS & set(payload):
                raise ContractError("a version 1 plan cannot carry version 2 fields")
            if version == 1:
                policy = payload.get("effective_policy")
                if isinstance(policy, Mapping) and V1_PROJECTION_KEY in policy:
                    carried = policy[V1_PROJECTION_KEY]
                    if not isinstance(carried, Mapping) or set(carried) - {
                            "stages", "controls", "obligations"}:
                        raise ContractError("invalid version 1 projection envelope")
                    stage_fields = carried.get("stages", {})
                    if not isinstance(stage_fields, Mapping):
                        raise ContractError("projected stage fields must be an object")
                    raw = payload.get("stages")
                    if not isinstance(raw, (list, tuple)) or any(
                            not isinstance(item, Mapping) for item in raw):
                        raise ContractError("stages must be an array of objects")
                    if set(stage_fields) - {item.get("stage_id") for item in raw}:
                        raise ContractError("projection names an unknown stage")
                    upgraded_stages = []
                    for item in raw:
                        if set(item) - _V1_STAGE_KEYS:
                            raise ContractError("version 1 stage has fields it does not define")
                        fields = stage_fields.get(item.get("stage_id"), {})
                        if not isinstance(fields, Mapping) or set(fields) - (
                                _V2_STAGE_KEYS - _V1_STAGE_KEYS):
                            raise ContractError("invalid projected stage fields")
                        upgraded_stages.append({**item, **fields})
                    upgraded = {
                        **payload, "contract_version": CONTRACT_VERSION,
                        "effective_policy": {key: value for key, value in policy.items()
                                             if key != V1_PROJECTION_KEY},
                        "stages": upgraded_stages,
                        "controls": carried.get("controls", []),
                        "obligations": carried.get("obligations", []),
                    }
                    restored = cls.from_dict(upgraded)
                    # A projection is only valid for settled plans; validate
                    # that it could actually have been written as version 1.
                    restored._project_v1()
                    return restored
            raw_stages = payload["stages"]
            if not isinstance(raw_stages, (tuple, list)):
                raise ContractError("stages must be an array")

            def parse_stage(item: Mapping[str, Any]) -> PlannedStage:
                allowed = _V1_STAGE_KEYS if version == 1 else _V2_STAGE_KEYS
                if set(item) - allowed:
                    raise ContractError(
                        f"stage has fields this version does not define: "
                        + ", ".join(sorted(set(item) - allowed)))
                dependencies = item.get("depends_on", ())
                if not isinstance(dependencies, (tuple, list)):
                    raise ContractError("stage dependencies must be an array")
                return PlannedStage(
                    stage_id=item["stage_id"],
                    obligation=Obligation(item["obligation"]),
                    selected=item["selected"],
                    state=ExecutionState(item["state"]),
                    condition=item["condition"],
                    reason=item["reason"],
                    provenance=item["provenance"],
                    executor=(ExecutorKind(item["executor"])
                              if item.get("executor") is not None else None),
                    depends_on=tuple(dependencies),
                    conclusion=item.get("conclusion"),
                    policy=(StagePolicy(item["policy"])
                            if item.get("policy") is not None else None),
                    policy_source=item.get("policy_source"),
                    resolution=Resolution(item.get("resolution", Resolution.RESOLVED.value)),
                    embedded_in=item.get("embedded_in"),
                )

            def entries(name: str) -> list[Mapping[str, Any]]:
                raw = payload.get(name, [])
                if not isinstance(raw, (tuple, list)) or any(
                        not isinstance(item, Mapping) for item in raw):
                    raise ContractError(f"{name} must be an array of objects")
                return list(raw)

            parsed_stages = []
            for item in raw_stages:
                if not isinstance(item, Mapping):
                    raise ContractError("each stage must be an object")
                parsed_stages.append(parse_stage(item))
            decisions = tuple(PendingDecision(
                subject=item["subject"], awaits=item["awaits"], required=item["required"],
                condition=item["condition"], provenance=item["provenance"],
            ) for item in entries("decisions"))
            obligations = tuple(ConditionalObligation(
                obligation_id=item["obligation_id"], subject=item["subject"],
                condition=item["condition"], source=item["source"],
                state=ObligationState(item["state"]), reason=item.get("reason", ""),
            ) for item in entries("obligations"))
            controls = tuple(PlannedControl(
                control_id=item["control_id"],
                requirement=ControlRequirement(item["requirement"]),
                resolution=Resolution(item["resolution"]),
                condition=item["condition"], reason=item["reason"],
                provenance=item["provenance"], timing=item.get("timing"),
                timing_source=item.get("timing_source"),
                automatic=item.get("automatic", False),
            ) for item in entries("controls"))
            plan = cls(
                plan_id=payload["plan_id"],
                revision=payload["revision"],
                repository=payload["repository"],
                work_item=payload["work_item"],
                objective=payload["objective"],
                execution_profile=payload["execution_profile"],
                effective_configuration=payload["effective_configuration"],
                effective_policy=payload["effective_policy"],
                plan_provenance=payload["plan_provenance"],
                stages=tuple(parsed_stages),
                contract_version=CONTRACT_VERSION,
                base_ref=payload.get("base_ref"),
                head_sha=payload.get("head_sha"),
                review=payload.get("review", {}),
                metadata=payload.get("metadata", {}),
                decisions=decisions,
                obligations=obligations,
                controls=controls,
            )
            plan.validate()
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            if isinstance(exc, ContractError):
                raise
            raise ContractError(f"invalid execution plan: {exc}") from exc
        return plan
