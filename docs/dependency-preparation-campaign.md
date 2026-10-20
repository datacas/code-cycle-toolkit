# Native preparation campaign — 2026-10-20

Status: **BLOCKED**. Neither native installation nor dependency verification
completed. The ordinary subprocess fixtures are separate evidence.

Each run below used the same command from the `issue-279-sandbox-dependency-setup`
linked worktree (merge base `bbd3149274f07591f30e1374d3188778e243bad6`):

```console
python3 -m unittest tests.native_dependency_campaign > "$TMPDIR/native-campaign.log" 2>&1
```

Both tests create a fresh npm workspace (`apps/*`) without `node_modules`, pin
`is-number@7.0.0` in a frozen lockfile, and use a new download cache. No install
or lookup is claimed as successful in any run.

## Run 1 — Codex stage, during implementation

Exit **1**, two tests, two failures; fixture HEAD
`f118814ea96742267e54bee8a9c534d715e74668`.

| Executor | Attempt | Observed outcome |
| --- | --- | --- |
| Claude | Build the actual `srt --settings …` preparation wrapper for `npm --version` | Not launched; `srt` is absent. Exit status is `null`, not zero. |
| Codex | `codex sandbox --permission-profile code_cycle_prepare -c … -- npm --version` | Exit 1 before package-manager execution: `app-server socket directory must be a user-owned directory with mode 0700`. |

The dispatch's known masked app-server socket boundary has no authorized
recovery inside a Codex stage. No variant was retried.

## Run 2 — Claude stage, head `aca7b76`

Exit **1**, two tests, two failures; fixture HEAD
`e07e59b0029bc300665a44e40ca9befdc13cb459`.

- Claude: `srt` is still absent; exit status `null`.
- Codex: the sandbox started. `npm --version` exited 0 (`10.9.8`). `npm ci
  --ignore-scripts …` exited **254** with `ENOENT: no such file or directory,
  mkdir '<fixture>/node_modules'`. A write grant on a path that does not exist
  leaves it under the read-only checkout, so the manager cannot create it.

Fixed in the following commit: preparation now creates each package's
`node_modules` (and `.yarn` for Yarn) on the host after the escape checks and
before the install, and an empty pre-created directory never counts as an
install (`tests/test_dependency_setup.py`).

## Run 3 — Claude stage, `aca7b76` plus that fix

Exit **1**, two tests, two failures; fixture HEADs
`189db77a8ea49702d352d4f209cacfea93cd5c27` and, after the final form of the fix,
`3a1a2a53e9716634ec46e34aaeca0ad556fd5631`.

- Claude: unchanged; `srt` is absent.
- Codex: `npm --version` exited 0. `npm ci` exited **240** with `EBUSY: resource
  busy or locked, rmdir '<fixture>/node_modules/.agents'` (the second run hit
  `apps/web/node_modules/.agents`). Codex mounts a
  protected placeholder (`.agents`) inside every writable root; `npm ci` empties
  `node_modules` before installing and cannot remove that mount point.

Each run printed the complete effective sandbox argv, fixture HEAD, stdout/stderr
contents, report paths and exit statuses into `$TMPDIR/native-campaign.log`.
Per-dispatch reports were under `$TMPDIR/native-claude/` and
`$TMPDIR/native-codex/`; these temporary paths do not survive dispatch cleanup.
Normal runtime preparation evidence uses the persistent location documented in
[dependency preparation](dependency-preparation.md).

## Remaining validation

- Codex: decide how a frozen npm install coexists with Codex's protected
  placeholders inside a writable `node_modules`, then rerun.
- Claude: run from a host with `srt` installed.
- Preserve the successful install/verification logs and exact HEAD, and exercise
  the full native Claude and Codex stage permission paths. Pending CI is
  complementary evidence only.
