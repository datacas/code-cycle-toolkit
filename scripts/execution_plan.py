"""Static stage metadata and the versioned contract for a cycle execution plan.

This module describes work; it does not route or execute it.  A stage's role,
responsibility, output contract, and permissions live together here so callers
can keep compatibility views without maintaining independent policy maps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from executors import WorkspacePolicy
from issue_review import ROLE as ISSUE_REVIEW_ROLE, SKILL as ISSUE_REVIEW_SKILL


CONTRACT_VERSION = 1


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

    def to_dict(self) -> dict[str, Any]:
        return {
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
        if not isinstance(self.stages, (tuple, list)):
            raise ContractError("stages must be a sequence")
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

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "contract_version": self.contract_version,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "repository": self.repository,
            "work_item": self.work_item,
            "objective": self.objective,
            "execution_profile": self.execution_profile,
            "effective_configuration": dict(self.effective_configuration),
            "effective_policy": dict(self.effective_policy),
            "plan_provenance": self.plan_provenance,
            "base_ref": self.base_ref,
            "head_sha": self.head_sha,
            "review": dict(self.review),
            "stages": [stage.to_dict() for stage in self.stages],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExecutionPlan:
        try:
            if not isinstance(payload, Mapping):
                raise ContractError("execution plan must be an object")
            raw_stages = payload["stages"]
            if not isinstance(raw_stages, (tuple, list)):
                raise ContractError("stages must be an array")
            def parse_stage(item: Mapping[str, Any]) -> PlannedStage:
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
                )

            parsed_stages = []
            for item in raw_stages:
                if not isinstance(item, Mapping):
                    raise ContractError("each stage must be an object")
                parsed_stages.append(parse_stage(item))
            stages = tuple(parsed_stages)
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
                stages=stages,
                contract_version=payload["contract_version"],
                base_ref=payload.get("base_ref"),
                head_sha=payload.get("head_sha"),
                review=payload.get("review", {}),
                metadata=payload.get("metadata", {}),
            )
            plan.validate()
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            if isinstance(exc, ContractError):
                raise
            raise ContractError(f"invalid execution plan: {exc}") from exc
        return plan

