# Shared execution plans in supervised Orca cycles

Orca owns Runs, Tasks, workers, deliveries, cancellation and terminal release.
The toolkit owns policy: `resolve_execution` and `build_plan` resolve every
route. `plan_service.py` shares canonical receipts, persistence, HEAD
invalidation, security obligations and rereview reconciliation with the native
driver. There is no second coordinator.

Resolve configuration and invocation overrides with the installed runtime.
Save the resulting JSON outside the checkout. These examples use placeholders
for the coordinator's real cycle identifier, current HEAD and authorized commands.

```bash
python3 ~/.code-cycle/runtime/run_cycle.py --repo owner/repo --task 330 --execution-profile prototype --dry-run --json
python3 ~/.code-cycle/runtime/run_cycle.py --repo owner/repo --task 330 --execution-profile standard --dry-run --json
python3 ~/.code-cycle/runtime/run_cycle.py --repo owner/repo --task 330 --execution-profile full --dry-run --json
python3 ~/.code-cycle/runtime/run_cycle.py --repo owner/repo --task 330 --execution-profile hotfix --dry-run --json
python3 ~/.code-cycle/runtime/run_cycle.py --repo owner/repo --task 330 --execution-profile custom --stage security=required --dry-run --json
python3 ~/.code-cycle/runtime/orca_plan.py initialize --database /tmp/orca-plan.sqlite --repo owner/repo --task 330 --cycle existing-cycle --head CURRENT_SHA --plan /tmp/plan.json
python3 ~/.code-cycle/runtime/orca_plan.py spec --database /tmp/orca-plan.sqlite --repo owner/repo --task 330 --cycle existing-cycle --head CURRENT_SHA --stage implement
```

`initialize` refuses to replace an existing plan. `reconcile` restores the
latest scoped revision with `CycleHistoryStore`, rebinds command fingerprints
only from current `--command` arguments, and persists a new revision. History
grants no execution permission. Missing bindings block recovery. Reconcile
HEAD before `spec`: exporting an unpersisted HEAD is refused. Writes append
revision history, preserving canonical plan ID, provenance, digest format and
obligations. The database is coordinator telemetry, not an application database.

Before creating its existing Task, the coordinator embeds the entire `spec`
response: effective profile, plan, stage, receipt, HEAD, security/verification
requirements, workspace policy and publication permissions. It supplies the
existing Run, Task and coordinator identities to `OrcaDispatchContext`. The
adapter's `task_spec()` exposes the same data and launch artifacts retain it.
Since `worker-start` consumes a pre-existing Task, the coordinator must install
this spec first; an artifact alone cannot prove the actual Task contents.
The managed adapter requires a coordinator-supplied `task_spec_reader` that
reads that existing Task and returns its bound spec. It compares the complete
spec before launching. Missing or mismatched bindings block before worker-start.
For a native driver launch the plan is finalized and persisted during stage
selection, after the coordinator created its Task. Supply an explicit
`task_spec_installer(run_id, task_id, spec)` capability alongside the reader to
install that final spec in the **existing** Task when readback differs. The
installer must be supplied by the existing coordinator; it does not create Tasks
or define a new Orca CLI command. The adapter checks workspace and provider
preconditions first, passes a detached copy of the final spec to the installer,
and reads the Task again. Installation acknowledgement alone never authorizes
worker-start: the independent reader must return the complete matching spec.
Without an installer, a preinstalled matching spec still works; a stale one
blocks. An installer without a reader also blocks.
The stock CLI has no verified Task-spec reader binding and therefore blocks
planned Orca launches until that integration is supplied; API fixtures exercise
the successful path without inventing a command in the installed Orca CLI.

The native driver accepts `--orca-coordinator`, `--orca-run` and `--orca-task`
together; `--task` remains the provider work item. Stages routed to Orca receive
the effective plan and stage in their context. The driver records the launch
dispatch ID and receipt in its journal and stops at `IN_PROGRESS` with
`result_pending` launch evidence. A launch
receipt or `worker_done` alone is never a functional result. Review still needs
an explicit isolated `OrcaReviewWorkspace`, and publication permissions retain
their existing gates. The native CLI does not create an Orca review workspace;
an API caller or coordinator supplies its contract, otherwise review blocks.

`--orca-action resume` and `--orca-action cancel` reconcile the persisted plan,
HEAD and outstanding checks before doing anything else. The stock route then
returns `BLOCKED` with `orca_capability_unsupported`: no authoritative-outcome
or cancellation CLI binding has been verified. It launches no replacement
worker. `orca_plan.reconcile_outcome` is the boundary for a capability supplied
by the existing coordinator, not a remote protocol. A file or worker self-report
cannot supply authority. Cancellation acknowledgement remains `RESULT_PENDING`.

Recovered outcomes require schema 2, expected Run/Task/repository/stage, plan ID,
revision, digest, applicable HEAD, terminal status and valid evidence. Partial,
stale, launch-only and mismatched responses block. This boundary only recovers
an outcome: existing functional-result, finding, verification and CI validators
must still judge its payload before advancing. Unsupported interfaces block
instead of inventing polling or cancellation commands.

| Mode | Default terminal obligation | Rereview |
|---|---|---|
| Prototype | `local_change`; native driver stops `HUMAN_INTERVENTION` with `local_only`, without approval; publishing or approval adds guarantees | Conditional when approval is requested |
| Standard | `READY_FOR_MANUAL_MERGE` after review, verification, applicable security and CI | Conditional |
| Full | Same merge gates, with every applicable declared control | Conditional; never an identical repeat |
| Hotfix | Same merge gates plus test-first reproduction and focused regression | Conditional |
| Custom | Standard plus overrides; required obligations cannot be withdrawn | Conditional |

All modes retain failures and partial evidence, stop `BLOCKED` on missing
capabilities and keep merge manual. Transport `RESULT_PENDING` is never a
functional terminal outcome. An explicit objective, including `verified_change`,
is resolved through the shared plan and determines its own obligations.

After review, helper action `rereview` accepts `--changes-after-review` only for
observed relevant changes or invalidated review evidence. Without that flag,
`--open-findings` retains blocks without another identical review. Full uses
the same rule. HEAD changes invalidate current verification and security
completion; old check results keep their original SHA. Other stages and
required obligations remain. Action `security` adds the control when the shared
security gate requires it and cannot remove it.

## Required live evidence

Required check `orca-live` stays **unverified** until a host campaign passes on
the exact commit SHA. The implementer sandbox does not run Orca or weaken
isolation. Orca **1.4.209** was reported at planning time, not observed by this
implementation.

The host verifier records actual version, environment, exact commands, commit
SHA, Run/Task/worker IDs, plan receipt, actual Task contents, launch receipt and
authoritative functional outcome. It exercises launch, resume, partial/stale
results, cancellation and terminal release; checks supported interfaces against
the installed version; and records limitations. Missing authoritative-outcome
or cancellation interfaces are unsupported capabilities, never fabricated
success. Fixtures complement this campaign and cannot satisfy `orca-live` or
mark the issue complete.
