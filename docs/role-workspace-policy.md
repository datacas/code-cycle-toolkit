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

An adapter is eligible only when the harness can establish the requested policy. Codex runs a read-only stage with `-s read-only`. That sandbox confines the commands the agent runs, not the processes the host launches for it: an MCP server, a plugin, or a hook writes outside it, and a scripted MCP server was observed appending to a tracked file under `codex exec -s read-only`. So every `read_only` dispatch, on every executor, is also checked after the fact. The harness fingerprints the checkout the stage was given (its dispatch `cwd`) before and after: HEAD, the branch ref, `git status --porcelain --untracked-files=all`, the staged blob IDs, and a digest of each modified or untracked file, so a second edit to an already dirty file is seen. File contents are digested, never stored; ignored files are left out. The checkout does not have to be clean; it only has to be unchanged. A difference is a `contract_violation` and the cycle stops, whatever wrote it. A checkout that cannot be fingerprinted before the stage blocks it (`read_only_verification`); one that cannot be fingerprinted after it is a violation. `read_only_mode = enforced` therefore means sandboxed *and* verified unchanged; a failed check drops the token. A host tool that writes into the checkout on startup, such as project metadata, fails the stage unless the repository ignores that path. Only a stage that succeeded and finished its work can be verified. A stage that only starts work elsewhere, such as an Orca worker, has not finished when its dispatch returns, and one that ends any other way, such as a timeout that can leave a worker or its children running, may still be writing. A change observed at that point is still a violation, but an unchanged checkout proves nothing. The result is therefore marked `read_only_verification = pending` and records no after-fingerprint, and whoever knows the work has stopped calls `verify_read_only_completion(result, cwd)` to compare again. `run_cycle.py` never accepts such a stage: it stops because the result finishes elsewhere. A synchronous stage that passes is marked `read_only_verification = verified`. A blocked pre-check is recorded with `missing_capability = read_only_verification`.

Claude cannot enforce read-only inside its process, so the harness runs it in an independent detached clone with its own Git object store at the exact reviewed HEAD, outside the implementer's tree, with the clone's remotes removed. A dirty implementer checkout blocks preparation. Before and after the stage, the harness compares the implementer's HEAD, `git status --porcelain`, and every configured remote's advertised refs; a detected change is a `contract_violation`. Edits inside the disposable clone produce a warning and are discarded. If the clone cannot be removed afterwards, the stage fails. Claude records `read_only_mode = detected` only when those checks pass. The prompt prohibits edits, commits, and pushes and explains why. Claude runs with its full tooling and its ordinary `gh` and `git` access, so it can publish its own comment. With Claude, read-only is watched, not enforced. The harness detects a write it can observe, but it cannot prevent or undo a remote write: a push, a merge, or a change to the change request. A push that is later restored before the stage ends also goes unseen. The maintainer has accepted this risk. Anyone who needs a full guarantee should review with Codex or give the reviewer read-only credentials.

Claude remains ineligible for `disposable` roles because it cannot confine writes to their workspace. Orca follows its explicit review-workspace contract and requires its own orchestration context where applicable.

This deliberately separates useful generated output from unauthorized source
tree changes: verification reports, caches, build output, and runtime state
belong in the disposable workspace and must not be used to justify granting a
write-capable role access to the implementer's tree.

See also [Routing and models → Workspace policy](routing.md#workspace-policy) for how this constrains which executor each profile may use.

---

[← Optional workspace tools](workspace-tools.md) · [↑ Documentation index](README.md) · [Instrumentation internals →](instrumentation.md)
