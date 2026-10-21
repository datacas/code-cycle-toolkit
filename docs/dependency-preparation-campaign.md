# Native preparation campaign — 2026-10-20

Status: **PASSED**. In run 5, both native executors completed the cold install
and the dependency lookup inside their own sandbox: Claude through `srt` and
Codex through `codex sandbox`. The ordinary subprocess fixtures are separate
evidence.

Each run below used the same command from the `issue-279-sandbox-dependency-setup`
linked worktree (merge base `bbd3149274f07591f30e1374d3188778e243bad6`):

```console
python3 -m unittest tests.native_dependency_campaign > "$TMPDIR/native-campaign.log" 2>&1
```

Runs 4 and 5 printed to the console instead, because the redirect was not
available in those stages.

Both tests create a fresh npm workspace (`apps/*`) without `node_modules`, pin
`is-number@7.0.0` in a frozen lockfile, and use a new download cache. Runs 1–3
claim no successful install or lookup.

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
install (`tests/test_dependency_setup.py`). Run 4 superseded the pre-creation:
the checkout is no longer writable at all.

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

## Codex placeholder: options examined (owner decision, 2026-10-20)

The owner asked first for a Codex permission option that keeps the `.agents`
placeholder out of writable output roots, and only then the scratch-copy
install. Installed CLI: `@openai/codex` with the `codex-linux-x64` vendor binary
under `~/.nvm/versions/node/v22.23.2`.

| Option | Outcome |
| --- | --- |
| A configuration key that disables or renames the protected names | None found. In the installed binary, `.agents` and `.codex` occur only as a constant beside the permission-profile code. The filesystem entry schema has `access`, `missing_path_behavior`, `glob_scan_max_depth` and `entries`, and no key for protected children. |
| `missing_path_behavior` on the output entry | Controls what happens when an entry's own path is missing. Protected children are a separate matter. |
| An explicit `write` entry on `<output>/.agents` | Every Codex write grant is a bind mount. This one would leave another mount point inside the tree that `npm ci` empties, so it cannot fix `EBUSY`. |
| Granting the lock root instead of `node_modules` | Moves the placeholder out of `node_modules`, but makes the checkout writable and needs a `read` rule per tracked file, the argv limit fixed in REV-002. Rejected. |

The resolve stage could not consult the official Codex documentation: the
Context7 and web-fetch tools were denied there, and so was a direct `codex`
command. The table rests on the installed binary, which is authoritative for
the version in use. No option existed, so preparation now uses the fallback:
the install runs in a writable scratch copy of the lock root's tracked files,
and the host moves the finished outputs into the checkout.

## Run 4 — Claude stage, scratch-copy install

Exit **1**, two tests, one failure.

- Codex (fixture HEAD `bd8dca8a0b41c4d3a61f8ba8d1715fe96b302f7f`): **passed**.
  - `codex sandbox --permission-profile code_cycle_prepare … -- npm --version`
    exited 0 (`10.9.8`).
  - `npm ci --ignore-scripts --no-audit --no-fund --cache <new cache>` exited 0,
    with `added 2 packages`. The filesystem rules granted `write` only to the
    scratch directory and the cache. The staged `package-lock.json` and both
    `package.json` files were `read`.
  - Preparation status was `prepared`.
  - The verification command
    `codex sandbox … -- node -e "if (!require('is-number')(42)) process.exit(1)"`
    ran in the checkout without network and exited 0.
  - The lockfile bytes and HEAD were unchanged.
- Claude (fixture HEAD `db25a84e6dfc4a9f3442d45f21048c536e22abec`): not run.
  - `srt` is installed (`@anthropic-ai/sandbox-runtime` 0.0.81).
  - `srt --settings <scratch>/sandbox.json "npm --version"` exited 1 with
    `Error: Sandbox dependencies not available: ripgrep (rg) not found`.

## Run 5 — Claude stage, after `rg` was installed

The owner installed ripgrep 15.2.0 at `~/.local/bin/rg`, which is on the `PATH`.
After that, `srt` started.

**First attempt, on `def7bdb`.** Exit **1**, two tests, one failure.

- Claude (fixture HEAD `5b04ef9cab8aec0d663126295397da9d230cf5b4`):
  `srt --settings <scratch>/sandbox.json "npm --version"` exited **127** with
  `/usr/bin/bash: line 1: npm --version: command not found`.
  - `srt` shell-quotes each positional argument itself (`quote(commandArgs)`
    in its `dist/cli.js`). The command that preparation had already joined
    therefore ran as one word.
  - Passing the arguments separately would not be enough on its own, because
    `srt` registers its own `--version`.
  - Fix: preparation now passes `srt --settings <file> -- <argv…>`. The
    commander parser in `srt` drops the `--` and keeps every later argument as
    an operand. A regression assertion in
    `test_both_sandbox_wrappers_disable_general_network_and_protect_lock` pins
    this argv.
- Codex (fixture HEAD `f513246dee0748483d0fbf8420703b36d09df35b`): passed again.

**Second attempt, with the fix.** Exit **0**, two tests, both passed.

- Claude (fixture HEAD `9586150219c4896f3b5dad3a720bb0200b904fc3`): **passed**.
  - `srt --settings <scratch>/sandbox.json -- npm --version` exited 0
    (`10.9.8`).
  - `srt --settings <scratch>/sandbox.json -- npm ci --ignore-scripts --no-audit
    --no-fund --cache <new cache>` exited 0, with `added 2 packages`. The
    settings allowed writes only to the scratch directory and the cache, and
    denied writes to the staged lockfile and manifests.
  - Preparation status was `prepared`.
  - The verification command
    `srt --settings <fixture>/verification-settings.json -- node -e "if (!require('is-number')(42)) process.exit(1)"`
    ran in the checkout with no allowed domains and exited 0.
  - The lockfile bytes and HEAD were unchanged.
- Codex (fixture HEAD `ea0c809582a8ec5ee1d0ebbfccddd87324930a13`): **passed**.
  - `npm --version` exited 0 (`10.9.8`).
  - `npm ci …` exited 0, with `added 2 packages`.
  - Preparation status was `prepared`.
  - The no-network `node -e` lookup exited 0.
  - The lockfile and HEAD were unchanged.

## Run 6 — Claude `-p` implement stage, contained lifecycle build (2026-10-21)

Toolkit: PR #302, the lifecycle-build unit on top of `54d514c`. The run used a
cold cache under the dispatch TMPDIR. Command:
`python3 -m unittest -v tests.native_dependency_campaign`. Exit **0**, four
tests, all passed.

- `test_claude` (fixture HEAD `816df64480a1f625f34c792237d2e391203ef0e6`) and
  `test_codex` (fixture HEAD `0ebf2a667c142a1d86a45994196e254fcca448d8`):
  passed, as in run 5. No lifecycle build ran, since nothing needed one.
- `test_claude_lifecycle_build` (fixture HEAD
  `6af1fd8d1e2c9cad10d276cc5c8243b8cf59b91b`): **passed**.
  - `npm ci --ignore-scripts …` exited 0. Preparation reported
    `needs_build: ["package.json"]` for the root `postinstall`.
  - `srt --settings <scratch>/sandbox.json -- npm ci --no-audit --no-fund
    --cache <scratch>/build-cache` exited 0, with writes allowed only to
    `<scratch>/build`, `<scratch>/build-home` and `<scratch>/build-cache`.
  - The script's write to the checkout path failed inside the sandbox
    (`checkout write denied: EROFS`).
  - `lifecycle_build` was `built` and `generated: ["generated"]`.
    `generated/ok.txt` reached the checkout, containing `built`.
  - The escape file was absent, `git status --porcelain` was empty, and the
    no-network `node -e` lookup exited 0. The lockfile and HEAD were unchanged.
- `test_codex_lifecycle_build` (fixture HEAD
  `4cafcaab5dcbb0918cc5793754e355168fa33dcc`): **passed**, with the same
  results.
  - The `codex sandbox` profile granted `write` only to the build copy, its
    `HOME` and the cache copy, and `read` to the staged lockfile and manifests.
  - The checkout write again failed with `EROFS`.

This run does not reproduce failures A and B from #279 on the installed
runtime. That validation remains pending with the #291 host runner.

## Not covered by this campaign

- pnpm and Yarn under a native sandbox. Only npm ran.
- A native `binding.gyp` compile or a dependency's own install script under a
  native sandbox. The lifecycle fixture covers a project `postinstall`. The unit
  fixtures cover dependency build outputs and failure fallback.
- A complete dispatched stage, end to end. The campaign drives `prepare` and
  the dependency lookup through each real sandbox. The stage wiring is covered
  by the fixture tests in `tests/test_stage_dispatch.py`. Pending CI is
  complementary evidence only.
