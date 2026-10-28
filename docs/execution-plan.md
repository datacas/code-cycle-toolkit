# Stage registry and execution-plan contract

The runtime keeps stage descriptions in `scripts/execution_plan.py`. A stage
names its role, responsibility, output contract, workspace policy, publication
permissions, skill or function, and accepted completion statuses. Runtime-owned
controls such as CI, security selection, and worktree validation have separate
control entries; a control is not a role or an executor.

The registry is static. Unknown stage and control identifiers raise
`ContractError`, so a caller can reject a typo before dispatch. The compatibility
views `cycle.ROLE_CONTRACTS`, `run_cycle.SKILL_FOR_ROLE`, and
`run_cycle.COMPLETES` are derived from the catalog and retain their existing
keys and values. In particular, `SKILL_FOR_ROLE` remains the five-role adapter
used by existing callers; catalog entries for other skills do not expand it.

## ExecutionPlan version 1

`ExecutionPlan` serializes the plan contract without copying the existing
findings, verification checks, code-host checks, or evidence contracts. Its
identity includes `plan_id`, `revision`, repository, and work-item ID. It also
stores the objective, execution profile, effective configuration and policy,
plan provenance, review metadata, base/head references, and a metadata extension
point.

Each planned stage has its catalog ID, obligation (`required` or `optional`),
selection, execution state, selection condition, reason, provenance,
selected executor (`codex`, `claude`, or `orca`), dependencies, and optional
functional conclusion. The executor is a separate field from the stage and the
role; the profile is plan-level routing context. These are separate fields:
obligation says whether it must occur, selection records the plan decision,
state records execution progress, and conclusion records what the stage
reported. An omitted stage must say why it was omitted. A selected required
stage cannot be omitted. Conclusions may be recorded only after completion.

Use `to_dict()` to validate and serialize, and `ExecutionPlan.from_dict()` to
validate input before a consumer uses it. The loader reports malformed types
through `ContractError` and rejects unsupported contract versions, unknown or
repeated stages, missing required stages, invalid selection/execution
combinations, and unknown, omitted, or cyclic dependencies. Catalog tests
ensure runtime-owned control targets resolve to existing functions. A future
incompatible contract increments `CONTRACT_VERSION`; readers must not silently
interpret an unknown version.

The contract reserves identity, profile, revision, provenance, effective
configuration, policy, and stage activation/omission reasons for later analysis
such as #127. It does not implement execution modes, routing, metrics, or plugin
loading. #330 can consume the same contract in supervised Orca cycles.
