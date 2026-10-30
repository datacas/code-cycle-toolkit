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

## ExecutionPlan version 3

`ExecutionPlan` serializes the plan contract without copying the existing
findings or code-host checks. Its
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
combinations, and unknown, omitted (including a selected stage that depends on
a disabled or not-applicable one), or cyclic dependencies. Catalog tests
ensure runtime-owned control targets resolve to existing functions. A future
incompatible contract increments `CONTRACT_VERSION`; readers must not silently
interpret an unknown version.

Version 2 (#328) adds what version 1 could not hold:

- **Stage policy and resolution.** Each stage records its effective `policy`
  (`disabled`, `auto`, `enabled`, `required`), the `policy_source` that set it,
  and a `resolution`: `resolved` (selected or omitted by a decision already
  made), `pending` (waiting for a signal), `disabled` (by policy), or
  `not_applicable` (the objective or a resume start point excludes it).
  `embedded_in` names the stage that performs it when it is no separate
  dispatch, such as `verify` within `implement`.
- **Pending decisions** (`decisions`), separate from the stages: the subject,
  the signal it awaits (`diff`, `review_outcome`, `post_review_changes`),
  whether the subject becomes required when it applies, the condition, and its
  provenance. Every `pending` stage or control has exactly one.
- **Conditional obligations** (`obligations`): a requirement that holds only
  when its condition applies, with its source and state (`pending`, `applies`,
  `not_applicable`). No stage policy removes one: an obligation that applies to
  a disabled or omitted stage makes the plan invalid.
- **Controls** (`controls`): `ci` (implemented by the `ci_gate` control, with
  its `strict` or `parallel` timing and source) and `merge`, each with a
  requirement (`required`, `optional`, `not_required`) and resolution. A merge
  is never automatic; a plan that says otherwise is rejected.

`check_executable(stage_id)` refuses a stage the plan has not selected. A
pending decision, and above all a pending required one, prevents execution: the
cycle first settles it with `resolve_decision()`, which returns the next plan
revision, and `settle_obligation()` does the same for an obligation. The
rereview decision is one rule for every profile
(`execution_profiles.rereview_decision`): it applies only after changes that
invalidate the review's evidence, and open findings without fixes keep their
blocks rather than being reviewed again.

**Version negotiation.** `from_dict()` reads versions 1, 2 and 3 and rejects every
other value, including `"1"` and `true`; a version 1 payload is upgraded, never
reinterpreted, and may not carry version 2 fields. `to_dict(contract_version=1)`
writes a lossless projection for a version 1 reader: it refuses while any
decision or obligation is pending, naming each, and carries settled
obligations, controls, and stage policies under
`effective_policy.contract_v2` instead of dropping them. On upgrade, this
envelope is validated and its typed fields restored. A version 1 payload
read and projected back is unchanged.

Version 3 (#290) adds `verification`, a `VerificationPlan` from
`scripts/verification_plan.py`. It belongs to the enclosing plan's `plan_id`
and `revision`; it has no parallel identity or store. Older plans upgrade with
`verification: null`. Versions 1 and 2 may still be written when verification
is absent; projecting a plan that carries verification to either version is
rejected instead of dropping obligations or history.

The verification contract contains:

- `criteria`: provider, content SHA-256, evaluation status, ordered criterion
  texts and a reason. Runtime parsing assigns `AC-1`, `AC-2`, etc. within this
  owning revision. Changed content requires an explicit revision; identifiers
  from a previous revision must not be silently assigned new meanings.
- `checks`: stable ID, exact command, executor, profile, execution context,
  required flag, criterion IDs, command level, provenance and activation reason.
  The configured test command is `configured-test` and required. Every check
  linked to a criterion is required. An agent can add checks and promote an
  obligation; it cannot remove or demote an existing one.
- `results`: append-only check ID, verified HEAD, `pass|fail|unverified`,
  evidence, cause and attempts. Unexecuted results carry an explicit cause;
  a null HEAD is permitted only for an unverified result whose target is unknown.
  Executed evidence requires integer exit codes and nonnegative counts or null.

`extend_verification()` returns a new owning revision and preserves result
history. `VerificationPlan.extend()` freezes command, executor, profile,
context and provenance, allows additions/promotions, and retains omitted
entries. `record()` appends results; `current()` invalidates an old HEAD without
deleting history. `cap()` never raises the reported conclusion: required fail
caps at `failed`; required unverified, unavailable criteria or an unlinked
criterion cap at `not_verified`. Code-host CI never substitutes for these
results in #290.

The native driver consumes the plan before dispatch. It resolves conditions
from the observed diff and review outcome, records revisions after stage results
and HEAD changes, and refuses a disabled stage when a mandatory obligation
applies. Agent reports can add checks or request security; they cannot withdraw
requirements. Security remains embedded in review and verification in code work.

Schema 25 stores append-only revisions in `cycle_execution_plans` in the history
database. The version-1 metadata projection removes prompts, criterion prose,
reasoning, commands and output. Commands become SHA-256 bindings; criteria become
stable `AC-n` identities. Check histories retain HEAD, results, closed cause
tokens and evidence fingerprints. Canonical JSON uses sorted keys, compact
separators and ASCII escaping; its SHA-256 is `digest`. Every stage receives
`plan_id`, `revision` and `digest` for its schema-2 receipt. A receipt supplements
the existing attempt, head and `REV-*` contracts; it grants no execution rights.

On resume the runtime reconciles the current configuration, source criteria,
remote receipt and local revisions. Required checks are retained across HEAD
changes; results from another SHA cannot satisfy them. Changed criteria add new
identities rather than reusing old coverage. Command hashes can be rebound only
to currently authorized exact commands. Without recoverable local history the
runtime replans with `partial` provenance and accepts no prior check as passed.
This is partial recovery, not a fully verified continuation. Analysis such as
#127 can read this metadata without reading prompts.

The work-item journal may retain redacted command proposals, separately from
the metadata-only history projection. A resume can use a proposal to recover a
binding only after the current configured verification policy authorizes it
and its hash matches the frozen check. Journal text never grants permission.
If redaction or cleanup removed the command, the check remains unsatisfied until
an authorized binding is supplied; it is never dropped.

The contract reserves identity, profile, revision, provenance, effective
configuration, policy, and stage activation/omission reasons for later analysis
such as #127. `scripts/execution_profiles.py` builds the plan from the
execution profile, objective, and stage policies described in
[Configuration → Execution profiles](configuration.md#execution-profiles);
`run_cycle.py --dry-run [--json]` prints it. Routing, metrics, and plugin loading
stay out of this contract. #330
can consume the same contract in supervised Orca cycles.
