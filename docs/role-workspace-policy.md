# Role workspace policy

Every dispatched role has an explicit workspace contract. The contract is
enforced by `scripts/cycle.py` and `scripts/executors.py`; a caller cannot
upgrade a role's permission through dispatch arguments.

| Role | Policy | Repository mutation | Generated artifacts |
| --- | --- | --- | --- |
| `implement`, `resolve` | `workspace_write` | Allowed in the assigned workspace | Allowed |
| `review`, `rereview`, `issue_review`, `security`, `bootstrap`, `coordinate` | `read_only` | Forbidden | Forbidden in the assigned workspace |
| `verify`, `run` | `disposable` | Forbidden in the source workspace | Allowed only in the disposable workspace |

`issue_review` reuses the `read_only` contract and has no publication
permission: its prompt states that it publishes nothing, and it may not
comment on, label, or edit the work item it judges. Like every `read_only`
stage, its checkout is fingerprinted before and after the dispatch.

`verify` and `run` must receive an `executors.DisposableWorkspace` and use its
`path` as `cwd`. The contract rejects a missing workspace, a mismatched `cwd`,
or a path that overlaps the source workspace. The caller creates and cleans up
that workspace; the dispatch layer does not silently create one in the
repository. The default auxiliary profile uses Codex because its sandbox can
confine writes to the disposable cwd; an adapter without that boundary is not
eligible for `disposable`, even when its process cwd is isolated.

An adapter is eligible only when the harness can establish the requested policy. Codex runs a read-only stage with a permission profile extending `:read-only`, adding only its per-dispatch scratch directory as writable. That sandbox confines the commands the agent runs, not the processes the host launches for it: an MCP server, a plugin, or a hook writes outside it, and a scripted MCP server was observed appending to a tracked file under `codex exec -s read-only`. So every `read_only` dispatch, on every executor, is also checked after the fact. The harness fingerprints the checkout the stage was given (its dispatch `cwd`) before and after: HEAD, the branch ref, `git status --porcelain --untracked-files=all`, the staged blob IDs, and a digest of each modified or untracked file, so a second edit to an already dirty file is seen. File contents are digested, never stored; ignored files are left out. The checkout does not have to be clean; it only has to be unchanged. A difference is a `contract_violation` and the cycle stops, whatever wrote it. A checkout that cannot be fingerprinted before the stage blocks it (`read_only_verification`); one that cannot be fingerprinted after it is a violation. `read_only_mode = enforced` therefore means sandboxed *and* verified unchanged; a failed check drops the token. A host tool that writes into the checkout on startup, such as project metadata, fails the stage unless the repository ignores that path. Only a stage that succeeded and finished its work can be verified. A stage that only starts work elsewhere, such as an Orca worker, has not finished when its dispatch returns, and one that ends any other way, such as a timeout that can leave a worker or its children running, may still be writing. A change observed at that point is still a violation, but an unchanged checkout proves nothing. The result is therefore marked `read_only_verification = pending` and records no after-fingerprint, and whoever knows the work has stopped calls `verify_read_only_completion(result, cwd)` to compare again. `run_cycle.py` never accepts such a stage: it stops because the result finishes elsewhere. A synchronous stage that passes is marked `read_only_verification = verified`. A blocked pre-check is recorded with `missing_capability = read_only_verification`.

Claude cannot enforce read-only inside its process, so the harness runs it in an independent detached clone with its own Git object store at the exact reviewed HEAD, outside the implementer's tree, with the clone's remotes removed. A dirty implementer checkout blocks preparation. Before and after the stage, the harness compares the implementer's HEAD, `git status --porcelain`, and every configured remote's advertised refs; a detected change is a `contract_violation`. Edits inside the disposable clone produce a warning and are discarded. If the clone cannot be removed afterwards, the stage fails. Claude records `read_only_mode = detected` only when those checks pass. The prompt prohibits edits, commits, and pushes and explains why. On a GitHub cycle (#142) this is no longer only a behavioural rule: the reviewer receives its stage's closed command table and a validation hook, so it can read, run the configured verification inside the clone under Claude's Bash sandbox, and publish its own comment, and nothing else through Bash — no push, commit, merge or other `gh` write, directly or through RTK. It runs with an explicit `--permission-mode default`, and the hook denies it Claude's file-editing tools, so the user's `defaultMode` cannot let it rewrite `.git/config` for an allowed `git status` to run. Non-Bash tools the user's configuration pre-approves, such as MCP tools, stay outside that table; the stage report names them. See [Host permissions](host-permissions.md). Elsewhere — another code host or issue provider — Claude keeps its full tooling and its ordinary `gh` and `git` access, read-only is watched, not enforced, and the harness can detect a write it observes but cannot prevent or undo a remote write: a push, a merge, or a change to the change request. A push that is later restored before the stage ends also goes unseen. The maintainer has accepted that risk for those cycles. Anyone who needs a full guarantee there should review with Codex or give the reviewer read-only credentials.

Codex reviewers on a GitHub cycle also run in a disposable clone, with `--ignore-rules`, under a profile extending `:read-only` whose only writable paths are the clone and the dispatch's `TMPDIR`, so they can verify findings without touching the source checkout. A review that may verify is dispatched only after the installed Codex sandbox has been shown to confine those writes; otherwise it stops `BLOCKED`.

All native Codex and Claude dispatches receive a unique writable directory
outside the checkout as `TMPDIR`. It lasts for that dispatch only and is removed
after success, failure, timeout, or cancellation. The parent process environment
is unchanged. Its name starts with `code-cycle-tmp-` under the selected system
temporary parent; concurrent dispatches never share a directory. Use fixed
helper names: `$TMPDIR/pr-comment.md`, `$TMPDIR/report.md`, and
`$TMPDIR/<check>.log`, replacing `<check>` with the check's fixed name.
Review stages may also read and write the fix delta as `$TMPDIR/delta.diff`;
Claude may place it in its per-user `$TMPDIR/claude-<uid>/delta.diff`
subdirectory. These exact names are allowed, while other diff files and the
rest of the temporary directory remain unavailable.
Codex read-only stages use a named permission profile extending `:read-only`
with only this directory writable. Writing stages add the directory to their
workspace-write roots or existing named profile, preserving Git protections,
publication access, and any explicitly granted socket access. Codex also
forwards `TMPDIR` to shell commands. This uses the same
[permission-profile mechanism](https://developers.openai.com/codex/permissions)
as scoped publication and requires a compatible Codex CLI. Checkout verification
still runs for read-only stages and a write there is still a violation. Orca workers keep their
existing behaviour: worker-start has no per-worker environment setting and
returns before completion, so the runtime cannot safely own or clean up a
per-dispatch temporary directory for them.

Native Codex and Claude stages receive `GH_TELEMETRY=false` and
`GH_NO_UPDATE_NOTIFIER=1` when their parent environment leaves those variables
unset. These defaults prevent GitHub CLI telemetry and its version update check
from making network requests that can interrupt a sandboxed stage. An explicit
parent value takes precedence. Codex receives both values in the process
environment and through `shell_environment_policy.set` for commands run by its
agent; Claude receives them in its process environment. The cycle runtime also
sets these defaults before making its own GitHub CLI calls. Orca workers remain
excluded because worker-start has no per-worker environment setting; see #354.

Claude remains ineligible for `disposable` roles because it cannot confine writes to their workspace. Orca follows its explicit review-workspace contract and requires its own orchestration context where applicable.

This deliberately separates useful generated output from unauthorized source
tree changes: verification reports, caches, build output, and runtime state
belong in the disposable workspace and must not be used to justify granting a
write-capable role access to the implementer's tree.

See also [Routing and models → Workspace policy](routing.md#workspace-policy) for how this constrains which executor each profile may use.

## GitHub upload from a writing stage

Native `implement` and `resolve` stages run the installed `publication.py`
helper's `preflight` command before editing, through their own sandbox. The
runtime supplies the exact command with the authorized repository and base
branch. A host metadata check, host dry run, or working GitHub HTTP API does
not establish that the stage can upload Git commits. Completion without the
stage preflight evidence is blocked.

Preflight may run on the base branch or detached HEAD before a working branch
exists. It only dry-runs a fixed `cc-cycle-preflight` destination; the actual
upload still requires a named non-base working branch. Active GitHub Claude
writers require the Bash sandbox for this path even without dependency
preparation; see [sandbox prerequisites](getting-started.md#requirements) and
[static-only recovery](getting-started.md#troubleshooting).

The helper uploads only the current non-base working branch to the authorized
GitHub `origin`. It retains a failed original push (including SSH transport
errors), then retries automatically using HTTPS with `gh auth git-credential`.
The URL, empty credential-helper list, replacement helper and temporary remote
are supplied for that Git command only. No global Git settings, remotes or
stored credentials change, and tokens are never included in command arguments
or evidence. Multiple push URLs, an unexpected repository, and URL rewriting
to another destination are refused. Missing network or authentication remains
a recoverable publication failure; the runtime never pushes from the host.

A successful upload requires both an exit-zero push and a remote branch ref
equal to the expected commit. Before a success comment or receipt, the stage
also reads the PR using `gh` and confirms that head. The runtime independently
compares the reported SHA, local HEAD and current PR head before recording a
successful functional verdict. A failure/progress comment must state that the
push failed and cannot count as a successful resolution. The invariant audit
for merges, force pushes and deleted refs is described in
[Publication audit](#publication-audit) below.
The completion probe normalizes supported GitHub PR references (a number,
`#number`, `owner/repo#number`, or a GitHub `/pull/number` URL) to the numeric
identifier before checking the remote head.

`$TMPDIR/publication.log` contains the preflight, original transport result and
fallback result. The runtime reads it before removing the scratch directory
and retains bounded, redacted command results in the cycle journal. Retry the
same upload after a failure; it creates no new commit. On resume, reuse an
existing success publication rather than duplicating it. Bitbucket and Orca
transport recovery are not verified by this GitHub native path.

## Publication audit

The cycle never merges, force-pushes or deletes refs. A stage's command table
(#142) narrows what it can run, but code that its verification executes and
non-Bash tools that its host pre-approves can still reach the code host. So
`run_cycle.py` checks the result after every publishing stage (`implement`,
`review`, `resolve`, `rereview`), on every executor, whatever status the stage
reported (`scripts/publication_audit.py`):

1. **Change requests.** Every change request of the cycle that was open before
   the stage is still open, and any change request the stage created is open.
   A merge or a close is a breach. The audit reads the change request that the
   stage was given, or for `implement` the one it reports, and every change
   request whose head is the cycle branch.
2. **Cycle branch.** Its remote head did not move, or moved fast-forward from
   the head recorded before the stage. A non-fast-forward move or a deletion
   is a breach. `implement` may create the branch when it was absent. A commit
   the checkout does not have is fetched into `FETCH_HEAD` only, so the audit
   leaves no ref behind.
3. **Default and protected branches.** Deletions, non-fast-forward moves and
   moves adding commits reachable from the cycle heads remain breaches. A
   fast-forward can instead be classified as an external advance, as below.

The audited refs are the cycle branch (the change request's head branch when
the runtime knows it, otherwise the checkout's branch), the default branch
(`code_cycle.repository.default_branch` when configured, otherwise the branch
`origin`'s `HEAD` names), and the branches the code host reports as protected.
A remote that has no branch yet has no default branch to audit; that part is
reported `unverified`. They are read once
before the stage, on `origin` only, and the same set is compared after it. A
push to any other branch never fails the check (#197).

The runtime cannot identify an actor from Git topology alone. A breach stops the
cycle with `publication_violation`, and the stop line says
"changed outside the stage?" instead of blaming the stage. `publication_violation`
means that an invariant failed, not that the stage caused it. The audit runs
before the stage's report is read, so a breach wins over a `BLOCKED`, a
`contract_violation` (in every role, model drift included) or any other
`stage_not_completed`, and an unreadable audit stops as
`publication_unverified` whatever the stage reported. No later stage is
dispatched. The evidence goes to the cycle report
(`CycleReport.publication_audit`) and the cycle journal. It holds the before
and after SHA of each audited ref, the before and after state of each change
request, and for each breach its kind (`change_request_merged`,
`change_request_closed`, `non_fast_forward`, `protected_ref_moved`,
`ref_deleted`, `review_target_changed`) with up to 20 commits added and removed
when they are available.

A state that cannot be read is never read as empty. If `origin`'s refs, its
default branch, the protected branches or a change request cannot be read, the
cycle stops with `publication_unverified`. Before the stage, nothing is
dispatched. After it, the stage's report is not accepted.

On GitHub the change requests and protected branches are read through `gh api`.
Other code hosts, Bitbucket included, have no reader until #238. For those, the
cycle branch and the default branch are still audited through Git, and the
change-request state and protected branches are reported `unverified` in the
cycle report, never as passed. A `--local-only` run publishes nothing and is
not audited; its read-only stages still guard their refs, as described below.

### External advances and read-only guards (#421)

An effectively active preventive command table (#142) is required before an
origin default/protected-ref advance can be classified as external. The table
blocks protected-ref pushes, merges and equivalent publication paths. Merely
requesting a table does not establish that it ran: the executor must report it
active. Without this evidence (including unrestricted Claude and Orca
dispatches), every movement remains a `publication_violation`. This conservative
fallback retains false positives for legitimate concurrent merges.

With the table active, the audit requires a fast-forward, full reachability
showing that none of the added commits is reachable from the before/after
cycle heads or reviewed checkout HEAD, and the cycle's change request read as
open. Such a movement is `external_change` and does not fail the stage. The
20-commit evidence list never limits the reachability test. If those conditions
cannot be established, the movement is `suspected_external_change`: the cycle
stops under that distinct reason, without recording an agent breach. A
non-fast-forward, deletion or addition of a cycle-head commit is still a
`publication_violation` even with an active table.

For `issue_review`, before a cycle branch or change request exists, reachability
uses the checkout HEAD captured at stage start; only the open-change-request
condition is skipped. The same exception applies to a stage without a change
request. A missing host reader when a cycle request could exist provides no
proof of its absence and yields a suspected change.

Read-only stages use the same protected-ref classifier on `origin`. Their
cycle branch and the head of a change request that stays open are guarded:
any movement of the cycle branch from its own value before the stage, its
creation or its deletion, and any change of the change request's head ref or
SHA, is `review_target_changed`. A remote branch that already differed from the
checkout before the stage (a local branch ahead of a stale `origin`) did not
change during it and is not a target change. A merge or a close of a change
request stays `change_request_merged` or `change_request_closed`, a
`publication_violation`, in read-only stages too: merging the reviewed change
request also moves a protected ref, even when a squash or rebase merge adds no
cycle-head commit to it. HEAD, index and working-tree mutations in the source
checkout retain `contract_violation`; this primary code takes precedence over a
changed review target or a suspected advance, never over a confirmed breach,
which stops as `publication_violation`. Neither code by itself attributes
causality. For `issue_review`, the unchanged reviewed checkout HEAD is the
target and no change-request guard applies.

Other branches, tags and every ref on other configured remotes are observed.
With an active command table they are recorded as external without failing the
read-only stage. Without one (an unrestricted Claude or Orca dispatch), the
stage could have pushed them itself, so they are recorded as
`suspected_external_change` and the stage fails under that reason, as a
protected-ref advance would fail without a table. Disposable clone edits remain
discarded and warned about as described above.

A read-only stage dispatched without a cycle auditor (`--local-only`, or a
direct dispatch) still audits `origin`, without a code-host reader. Such a run
publishes nothing, so no change request can exist: as for `issue_review`, the
open-change-request condition is skipped and a classified base advance is
`external_change`. When the checkout has no `origin` remote, no ref can be
audited, so any movement observed on any remote fails the stage as
`suspected_external_change` instead of being recorded as external.

Publication audit evidence and read-only artifacts carry `external_changes`:
remote name, ref, before/after SHA, bounded `commits_added`, and `class`.
Classified protected advances also carry reachability heads and a reason. No
remote URL is recorded. The cycle journal records each movement once. Schema
28 keeps suspected advances and changed review targets distinct from breaches
in [instrumentation](instrumentation.md).

Residual risk: a stage that merges an unrelated PR through a bypass path can
look external. A read-only stage with an active table that writes an observed
branch or tag through a bypass path is also only recorded. The preventive
command table remains essential; this
heuristic is not certain actor attribution. #421 records the advance only:
existing CI-gate conflict and head checks still decide whether to continue.
Updating the cycle branch and revalidating against a new base belongs to #426;
this classifier does not waive an existing requirement to refresh verification.

### A publishing stage that reported `BLOCKED` after a verified publication (#364)

#261 verifies that a successful stage broke no publication invariant. The
inverse also happens: an `implement` or `resolve` stage publishes a correct
commit and change request, meets refused auxiliary commands (a `pwd`, a
heredoc, a `grep --include=*.py`, a `gh pr edit` the guard refuses with the pull
request already open) and reports `BLOCKED`. The cycle used to stop with
`stage_not_completed` as if nothing had been published.

`reconcile_result` (#409) now decides this, in the same place that already
decides whether a refusal is essential; no second mechanism exists. It acts only
on `implement` and `resolve`: any other role still has its refusals recorded and
classified, but a `BLOCKED` result of it is never completed from them. The
bounded re-read of `review` and `rereview` publications is #446. A refused
command that chains several non-read-only operations (`git push && gh pr edit`)
is classified `unknown` or `write`, never by its first part.

**Every denial is recorded and classified by the runtime.** That includes
operations that can never be granted: a heredoc, a pipeline, the `gh pr edit`
guard. Each record carries the command, the stage, the reason, the rule that
refused (`command_table`, `credential_guard`, `docker_host`, `edit_rule`,
`foreground_policy`, `verification_timeout`, `user_denial`,
`validation_error` or `executor_permission_layer`) and an operation
identifier. Recording a refusal never authorizes the operation, and no guard is
weakened.

| Class | Meaning | Reconciles? |
|---|---|---|
| `auxiliary_denial` | not needed to complete the stage or a mandatory check: read-only inspection, a scratch file outside the checkout, a helper whose result is already published and verified | yes |
| `redundant_operation` | its effect is already published and verified, including the metadata it meant to change | yes |
| `essential_permission` | an operation still pending that the stage needs, a mandatory check, or one the stage reported as essential | no |
| `environment_blocker` | a required resource is unreachable | no |
| `unknown` | not enough evidence; neither assumed essential nor auxiliary | no |

The classification does not depend on the agent's `permission_effects`. The
agent can make a denial more serious (`essential: true` is honoured) and never
less (`essential: false` is not). A `redundant_operation` is judged on what the
command meant to change, not on a matching SHA: `git push` and `gh pr create`
need the remote head, the local HEAD and the PR head to be one commit, and
`gh pr edit` also needs the title, body (compared by digest, taken by the hook
while the body file still exists), base, labels or milestone it set to be what
the pull request already has. A flag the runtime does not know leaves the
intent `unknown`.

**A `BLOCKED` stage is completed only when all of this holds:**

1. the block declares a `completed_status` valid for its role (`IMPLEMENTED`
   for `implement`, `RESOLVED` for `resolve`). Since #464 a block that declares
   none is judged on its structured result instead: `implement` needs a full
   `head_sha`, a change request, non-empty `work_units` and `tests.ran: true`;
   `resolve` needs a full `head_sha` and `finding_outcomes`. The runtime then
   derives the status and records `inferred: true`; prose never counts and a
   pull request existing is not completion;
2. every denial of the stage is `auxiliary_denial` or `redundant_operation`.
   A configured check the person declined is the one exception: it keeps its
   `essential_permission` class in the record, but it is recorded `unverified`
   and never stops the stage or keeps it blocked; the cycle ends
   `VERIFICATION_INCOMPLETE` naming it
   ([#398](verification.md#declared-docker-checks-and-declined-checks));
3. the remote head of the cycle branch, the local HEAD and the PR head are one
   commit, the pull request is open with the cycle branch as its head, the
   worktree is clean, and the head and change request the block reports are
   those observed;
4. the result does not say its publication is pending, report an environment
   blocker or an essential permission, or carry a failed check;
5. the #261 audit found no breach. The audit runs before the report is read, so
   a `BLOCKED` stage that also broke an invariant stops with
   `publication_violation` and is never reconciled.

Otherwise the stage stays `BLOCKED` and stops as before; a grantable essential
or `unknown` refusal is still asked about (`permission_required`). Reconciling
changes the stage's status, nothing else: a pending mandatory verification stays
`unverified`, the check linked to a refused command never keeps a pass, and
`verification_unverified` still stops the cycle later. The cycle continues from
the verified head and a publication is never retried. The state is read with
`reroute.observe_publication`; when it cannot be read nothing is reconciled.

The decision and its evidence are recorded in the [cycle journal](instrumentation.md#reconciling-a-blocked-publishing-stage-364).

---

[← Optional workspace tools](workspace-tools.md) · [↑ Documentation index](README.md) · [Host permissions →](host-permissions.md)
