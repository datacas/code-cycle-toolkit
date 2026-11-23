# Native parity runs

The consent conversions of #455 and #456 (rows PA-19 to PA-24 of the
[capability parity audit](capability-parity-audit.md)) are tested in CI through
the shared hook, with fixture `gh` and `docker` executables. That does not prove
the native executors behave the same, so this procedure runs the audit's
baseline commands with the real `claude -p` and `codex exec` and compares them
with the toolkit's decision. It is a manual procedure: the frozen checks
`native-claude-parity` and `native-codex-parity` cannot run it inside a cycle,
because a Codex stage cannot start `claude -p` (no network) or a nested
`codex exec`.

Run it by hand, outside any cycle stage, with your own credentials. Repeat it
when the consent code (`scripts/stage_permissions.py`, `scripts/grants.py`,
`scripts/command_permissions.py`) or a row of the audit changes.

## Requirements

For PA-19 alone, only `claude`, `codex`, `git`, Bash, and the user's normal
native configuration are needed. No GitHub fixture or Docker access is used.

- `claude`, `codex`, `gh` (authenticated), `git`, and Docker with `alpine:3`
  available.
- A **private, disposable** GitHub repository for PA-22. The run creates one
  issue per executor there.
- A repository PA-21 can read (the default is this one).

## Steps

```text
python3 docs/native-parity/run.py setup
python3 docs/native-parity/run.py --fixture-repo <owner/fixture> toolkit
python3 docs/native-parity/run.py --fixture-repo <owner/fixture> native claude
python3 docs/native-parity/run.py --fixture-repo <owner/fixture> native codex
python3 docs/native-parity/run.py cleanup
```

Then close the issues PA-22 created in the fixture repository. `--workdir` sets
where the fixtures and the `*.json` results go (default: a temporary
directory), `--rows PA-20,PA-23` limits the rows, `--read-repo` changes what
PA-21 reads and `--claude-model` the model `claude -p` uses.

For #455, select only PA-19 (outside the cycle stage):

```text
python3 docs/native-parity/run.py setup
python3 docs/native-parity/run.py --rows PA-19 toolkit
python3 docs/native-parity/run.py --rows PA-19 native claude
python3 docs/native-parity/run.py --rows PA-19 native codex
```

Run the direct commands in the same `fx` directory and with `TMPDIR` set to
its absolute `tmp` subdirectory, as the PA-19 native runner requests. Compare
`git log --oneline | head -5` output with the native report. The second command
prints `PA19_TARGET=<expanded path>` before running `cat <<EOF` into that
quoted path, with body `PA-19 fixture` and closing `EOF`. A native executor's
Bash tool may override `TMPDIR`: use the path printed by that execution,
never infer it from the runner's environment. Record an override as an
environment difference and use the same effective `TMPDIR` for the direct
comparison. Remove the file at the reported path before each side (repeat
after learning an overridden path if necessary), then check that each run
creates exactly `PA-19 fixture` followed by a newline at that path. Record
the command's exit code as well as the marker and file contents: a marker
alone does not prove a successful write. A missing marker, refusal, nonzero
exit or inaccessible file needs investigation and remains `not_verified`
until explained; do not infer parity or divergence from a missing file at
`fx/tmp/pa19.txt`. Record the environment, commands, outputs, file contents,
and any actual permission or infrastructure limit.
Keep #455 open until both native comparisons are recorded; `not_verified`
does not demonstrate parity. PA-19 needs no Docker cleanup.

## What each side does

| Side | What it runs |
|---|---|
| `toolkit` | For each command and each executor, with the real consent machinery and no mocks: the table refuses it, a consent question is offered, an exact grant allows it, the same command plus an extra flag is still refused, and the subprocess hook allows it. |
| `native claude` | `claude -p --allowedTools Bash` is asked to run the audit's baseline command in the fixture and to report its exit code and output. |
| `native codex` | `codex exec -s workspace-write` does the same, with `sandbox_workspace_write.network_access=true` for PA-21 and PA-22. |

The commands per row are the ones in the audit: a pipe to `head` and a heredoc
into `$TMPDIR` (PA-19), `git ls-tree HEAD` and
`git stash list` (PA-20), `gh issue list` (PA-21), `gh issue create` (PA-22),
`git -c core.pager=cat log -1` and `GIT_PAGER=cat git log -1` (PA-23), and
`docker compose run --rm fixture true` (PA-24).

## Reading the result

A row passes when the native run executed its command (compare the output with
the same command run directly, and check the side effect for PA-22 and PA-24)
and every `toolkit` field is `true` except `table_allowed`, which is `false`
because the table refuses these commands. A difference between a native run and
the toolkit is a finding: file it as its own issue with the evidence. A real
limit of the agent, the operating system, the provider or the infrastructure is
reported as such, not as a pass.

Limits of this comparison:

- It compares the native run with the hook's decision on the same command. It
  does not dispatch a cycle stage through the runtime.
- The native agent reports its own result, and may not report an exit status.
  Compare outputs and side effects, not a captured exit code.
- The user's normal hooks and configuration apply to both executors.

## Recorded runs

- 2026-11-21, #516: PA-20 to PA-24, Claude and Codex, no divergence from the
  direct baselines.
- #455, PA-19: native Claude and Codex comparisons **pending**. The named
  tests in `tests/test_compound_parity.py` compare direct Bash execution with
  execution after the real shared hook accepts each executor's grant metadata;
  they do not launch either native executor or establish native parity.
