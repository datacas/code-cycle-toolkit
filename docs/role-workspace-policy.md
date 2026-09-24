# Role workspace policy

Every dispatched role has an explicit workspace contract. The contract is
enforced by `scripts/cycle.py` and `scripts/executors.py`; a caller cannot
upgrade a role's permission through dispatch arguments.

| Role | Policy | Repository mutation | Generated artifacts |
| --- | --- | --- | --- |
| `implement`, `resolve` | `workspace_write` | Allowed in the assigned workspace | Allowed |
| `review`, `rereview`, `security`, `bootstrap`, `coordinate` | `read_only` | Forbidden | Forbidden in the assigned workspace |
| `verify`, `run` | `disposable` | Forbidden in the source workspace | Allowed only in the disposable workspace |

`verify` and `run` must receive an `executors.DisposableWorkspace` and use its
`path` as `cwd`. The contract rejects a missing workspace, a mismatched `cwd`,
or a path that overlaps the source workspace. The caller creates and cleans up
that workspace; the dispatch layer does not silently create one in the
repository. The default auxiliary profile uses Codex because its sandbox can
confine writes to the disposable cwd; an adapter without that boundary is not
eligible for `disposable`, even when its process cwd is isolated.

An adapter is eligible only when the harness can establish the requested policy. Codex enforces read-only execution directly with its sandbox and records `read_only_mode = enforced`.

Claude cannot enforce read-only inside its process, so the harness runs it in a detached worktree at the exact reviewed HEAD, outside the implementer's tree. Before and after the stage, the harness compares the implementer's HEAD and `git status --porcelain`; a change is a `contract_violation`. Edits inside the disposable review worktree produce a warning and are discarded. Claude records `read_only_mode = isolated_verified`, and its reviewer cannot write to the branch. The prompt also prohibits edits, commits, and pushes, while CLI tool restrictions provide defence in depth. Publication access remains behavioural and follows the role's existing policy.

Claude remains ineligible for `disposable` roles because it cannot confine writes to their workspace. Orca follows its explicit review-workspace contract and requires its own orchestration context where applicable.

This deliberately separates useful generated output from unauthorized source
tree changes: verification reports, caches, build output, and runtime state
belong in the disposable workspace and must not be used to justify granting a
write-capable role access to the implementer's tree.

See also [Routing and models → Workspace policy](routing.md#workspace-policy) for how this constrains which executor each profile may use.

---

[← Telemetry](telemetry.md) · [↑ Documentation index](README.md) · [Instrumentation internals →](instrumentation.md)
