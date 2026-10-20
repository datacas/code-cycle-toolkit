# Native preparation campaign — 2026-10-20

Status: **BLOCKED**. Neither native installation nor dependency verification
completed. The ordinary subprocess fixtures are separate evidence.

Executed from the `issue-279-sandbox-dependency-setup` linked worktree, based on
`bbd3149274f07591f30e1374d3188778e243bad6`, with this change applied:

```console
rtk proxy python3 -m unittest tests.native_dependency_campaign > "$TMPDIR/native-campaign.log" 2>&1
```

Result: exit **1**, two tests, two failures. Both tests created a fresh npm
workspace without `node_modules`, pinned `is-number@7.0.0` in a frozen lockfile,
and used fixture HEAD `f118814ea96742267e54bee8a9c534d715e74668`. No install or
lookup is claimed as successful at that HEAD.

| Executor | Attempt | Observed outcome |
| --- | --- | --- |
| Claude | Build the actual `srt --settings …` preparation wrapper for `npm --version` | Not launched; `srt` is absent. Exit status is `null`, not zero. |
| Codex | `codex sandbox --permission-profile code_cycle_prepare -c … -- npm --version` | Exit 1 before package-manager execution. |

Codex stderr ended with:

```text
error building bubblewrap command: app-server socket directory must be a user-owned directory with mode 0700
```

It also warned that PATH helper aliases could not be created under the isolated
temporary HOME. The dispatch's known masked app-server socket boundary has no
authorized recovery inside this Codex stage. No TMPDIR, CODEX_HOME, no-daemon or
alternate sandbox variant was retried after this failure.

The campaign printed the complete effective sandbox argv, exact fixture HEAD,
stdout/stderr log contents, report paths and exit statuses into
`$TMPDIR/native-campaign.log`. Per-dispatch preparation reports are under
`$TMPDIR/native-claude/` and `$TMPDIR/native-codex/`; these temporary paths are not
claimed to survive dispatch cleanup. Normal runtime preparation evidence uses
the persistent location documented in [dependency preparation](dependency-preparation.md).

Remaining validation: run the same campaign from an authorized host executor
with functioning native sandboxes and `srt` available, preserve the successful
install/verification logs and exact HEAD, and exercise the full native Claude
and Codex stage permission paths. Pending CI is complementary evidence only.
