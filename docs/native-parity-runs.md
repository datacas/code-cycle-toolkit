# Native parity runs

The consent conversions of #456 (rows PA-20 to PA-24 of the
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

## What each side does

| Side | What it runs |
|---|---|
| `toolkit` | For each command and each executor, with the real consent machinery and no mocks: the table refuses it, a consent question is offered, an exact grant allows it, the same command plus an extra flag is still refused, and the subprocess hook allows it. |
| `native claude` | `claude -p --allowedTools Bash` is asked to run the audit's baseline command in the fixture and to report its exit code and output. |
| `native codex` | `codex exec -s workspace-write` does the same, with `sandbox_workspace_write.network_access=true` for PA-21 and PA-22. |

The commands per row are the ones in the audit: `git ls-tree HEAD` and
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
