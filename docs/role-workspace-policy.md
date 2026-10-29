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
3. **Default and protected branches.** They did not move, and none was deleted.
   Any move is a breach, even a fast-forward or a legitimate push by someone
   else.

The audited refs are the cycle branch (the change request's head branch when
the runtime knows it, otherwise the checkout's branch), the default branch
(`code_cycle.repository.default_branch` when configured, otherwise the branch
`origin`'s `HEAD` names), and the branches the code host reports as protected.
A remote that has no branch yet has no default branch to audit; that part is
reported `unverified`. They are read once
before the stage, on `origin` only, and the same set is compared after it. A
push to any other branch never fails the check (#197).

The runtime cannot tell who moved a ref during a stage. A breach stops the
cycle with `publication_violation` whoever caused it, and the stop line says
"changed outside the stage?" instead of blaming the stage. `publication_violation`
means that an invariant failed, not that the stage caused it. The audit runs
before the stage's report is read, so a breach wins over a `BLOCKED` or any
other `stage_not_completed`. No later stage is dispatched. The evidence goes to
the cycle report (`CycleReport.publication_audit`) and the cycle journal. It
holds the before and after SHA of each audited ref, the before and after state
of each change request, and for each breach its kind (`change_request_merged`,
`change_request_closed`, `non_fast_forward`, `protected_ref_moved`,
`ref_deleted`) with up to 20 commits added and removed when they are available.

A state that cannot be read is never read as empty. If `origin`'s refs, its
default branch, the protected branches or a change request cannot be read, the
cycle stops with `publication_unverified`. Before the stage, nothing is
dispatched. After it, the stage's report is not accepted.

On GitHub the change requests and protected branches are read through `gh api`.
Other code hosts, Bitbucket included, have no reader until #238. For those, the
cycle branch and the default branch are still audited through Git, and the
change-request state and protected branches are reported `unverified` in the
cycle report, never as passed. A `--local-only` run publishes nothing and is
not audited.

Reconciling a stage that published a verified result but reported `BLOCKED` is
#364. Until it exists, a `BLOCKED` result is never turned into a success, even
when the SHAs match.

---

[← Optional workspace tools](workspace-tools.md) · [↑ Documentation index](README.md) · [Host permissions →](host-permissions.md)
