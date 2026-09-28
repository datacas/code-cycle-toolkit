# Verification

The toolkit counts something as verified only after it has run it. This page explains what that means for each stage, when a stage stops instead of continuing, and how checks are reported.

## The rules

1. **Execution is the evidence.** A fix read in the diff is *present*, not *verified*. `cc-verify` reports what ran, what passed, what failed, and what couldn't be verified, and why.
2. **Unverified stays unverified.** It is reported with its concrete reason and carried into the verdict and residual risk. "Environment unavailable" with no cause isn't an acceptable reason.
3. **Nothing is invented.** Commands come from what the project defines (lockfiles, manifests, task runners, CI). If a command doesn't exist, it isn't run.
4. **Verification doesn't edit code** unless you ask.
5. **A pass that couldn't run isn't a passed check.** A missing delegated skill is reported as a degraded pass.
6. **Compacted output isn't evidence.** A stage judges the whole diff and cites uncompacted output. When it can't obtain either, the affected part is unverified. See [Complete evidence](#complete-evidence).

## Complete evidence

A host can change what an agent sees of a command's output. A command-rewriting
hook may compact `git diff`, test runs, or other output before the agent reads
it, and the only trace is a marker in the middle of the text. A review that
approves a shortened diff has not read the change it approved.

Measured on this repository with RTK 0.48, whose Claude Code hook turns
`git diff` into `rtk git diff` transparently (`git diff b9dbc54 91ba1dd`, the
PR #74 merge):

| Command | Lines |
|---|---|
| `git diff` through the hook | 495, with markers such as `... (100 additions truncated)` |
| raw (`rtk proxy git diff`) | 764 |
| `git diff --output=<file>` through the hook | 764 (the file is written by Git, unfiltered) |

In the same test, `gh` comment reads and exit codes were intact. The rule has two
layers:

- **The runtime supplies the diff.** When `run_cycle` dispatches `review`,
  `resolve`, or `rereview`, it writes `git diff <base>...HEAD` itself, through
  `subprocess` and `--output`, which no host hook rewrites. The base is the first
  of `change_bases_of` that resolves, the same resolution the change signals
  use. The file goes in a fresh directory under `evidence/`, beside the
  telemetry database (`CODE_CYCLE_HOME`, or the user configuration directory).
  That is outside the workspace and Claude's disposable review clone. It is
  also outside the system temporary directory, which Codex's `workspace-write`
  sandbox may write. If `CODE_CYCLE_HOME` puts it inside the workspace, `/tmp`,
  or `$TMPDIR`, no file is written and the prompt says so. The prompt names the file, the base, merge base and head
  SHAs, the line count, and the SHA-256. The file is removed when the stage
  ends. When no base resolves, no file is written and the prompt says so; the
  stage never receives a partial file.

  How far the evidence is protected depends on the executor:

  | Executor | Reads the file | Can the stage change it? |
  |---|---|---|
  | Codex, any stage | Its sandbox reads the whole file system | No; the operating system sandbox writes only its workspace and temporary directories |
  | Claude, non-writing stage | Granted the directory with `--add-dir` | Not prevented. Claude has no sandbox here, and an added directory is editable |
  | Claude, writing stage | Not granted; falls back to the skill's rules | Not granted, but not sandboxed either |

  For every executor the file is read-only, and after the stage the runtime
  checks its SHA-256 again. A changed or missing file stops the cycle as
  `stage_not_completed` before the stage's verdict is read. What this can't
  catch is a Claude stage that changes the file and restores it before it
  exits. For a review stage that matters, because the cycle acts on its
  verdict. No runtime can prove which bytes an agent read. For Codex the
  sandbox makes this case impossible. For Claude it is an accepted limit:
  Claude reviews remain trusted, with their capabilities unchanged and no
  secondary confirmation required. They are not runtime-verifiable against an
  edit to the evidence that is restored before the stage exits.
- **The skills state the rule.** Every skill that reads a diff or cites output
  carries the shared `## Complete evidence` section, for manual runs and for
  evidence other than the diff. A truncation or summary marker, or output
  shorter than its own header counts, makes the output incomplete. Diffs are
  read from a file Git wrote, or through the host's raw mode. When complete
  output can't be obtained, the affected part is unverified, never checked.

This section decides whether output is complete. [Evidence levels](#evidence-levels)
decides who produced it; the two don't overlap.

## Evidence levels

Every recorded test outcome has one `tests_basis` token. The functional
conclusion is separate: it describes the agent's judgement, while the basis
says who produced the evidence that judgement rests on.

| `tests_basis` | Meaning | Example | Source |
|---|---|---|---|
| `claimed` | No executed command is attached | `"tests": { "passed": true }` | The agent |
| `agent_reported` | The result includes a command, exit status, and count, as reported by the agent | `pytest tests/test_x.py` → exit 0, 14 run | The agent; auditable in the comment, not proof |
| `runtime_observed` | The runtime observed the fact itself | Executor exit code, head SHA, or a command the runtime ran | The runtime |
| `externally_verified` | A system the agent does not control owns the fact | Checks on the pushed head SHA read by the runtime | The code host |

Evidence entries use `level: static` or `level: test` to describe the command;
these are command categories, not `tests_basis` values. `cc-verify` emits the
entries and one conclusion token: `verified`, `verified_with_reservations`,
`not_verified`, or `failed`. The conclusion is the agent's judgement and does
not raise its evidence level. Missing or contradictory evidence never becomes
a verified test outcome. Older telemetry rows without `tests_basis` are read
as `claimed`.

## Boundary verification

Some changes need one run where the code is actually used, not only a green
suite. `cc-verify` decides it with the security gate's shape:

```text
boundary_required = deterministic_rule OR verifier_judgement
```

The rule fires on changed paths classified as `api`, `auth`, `database`, or
`migrations`, the same classes as the `touches_*` change signals
(`stage_signals.boundary_rule_areas`). The verifier may add CLI entry points,
subprocess and adapter code, provider integrations, and runtime configuration;
it cannot remove what the rule fired. `cc-verify` lists what counts as a
boundary run for each area, and boundary runs write only to the disposable
workspace.

A required boundary run appears as an evidence entry with `level: boundary`:

```json
{
  "conclusion": "verified",
  "boundary": "required",
  "evidence": [
    { "level": "test", "command": "python3 -m unittest tests.test_config", "exit_code": 0, "executed": 9 },
    { "level": "boundary", "command": "python3 -m app.cli sync --config config.yml --dry-run", "exit_code": 0, "executed": null }
  ]
}
```

A non-zero boundary command fails the test outcome like any other command.
An expected failure, such as the malformed-configuration case, is recorded as
a command that checks the failure and exits 0 when it occurs. When the run is
required and no successful `boundary` entry exists, the runtime records
`boundary_verified: false` and caps a `verified` conclusion at
`verified_with_reservations`; the reason (`environment`, `cost`, or
`out_of_scope`) is listed under *Not verified*, where reviewers already read
unverified items and residual risk. When the driver observed the change, the
runtime applies the path rule itself, so a verifier that states
`not_required` for a migration still gets `boundary_verified: false`.
A stage that completed code work but reported no test outcome also records
`boundary_verified: false` when a run was required. Without a test report, a
review, a blocked stage, or a `no_code_change` resolution records nothing.
`boundary: not_required` records nothing new. The runtime never runs a
boundary check.

## What each stage verifies

| Stage | Verifies |
|---|---|
| `cc-implement-issue` | The narrowest related tests first, then the repository's required verification through `cc-verify` |
| `cc-initial-review` | Reproduces actionable findings when the environment allows it. Findings are marked as observed at runtime or found by code inspection. |
| `cc-resolve-comments` | Reproduces each original finding, applies the fix, and confirms it no longer reproduces. Then `cc-verify` over the fixes and related regressions. |
| `cc-rereview` | Reproduces every finding claimed as fixed. Only those it could verify become `resolved`. |

Depth follows the change:

- **Authorization:** both the allowed and the denied case.
- **API contracts:** status, payload, relevant headers, and error responses.
- **Frontend:** the affected flow in a browser, with console and network checks, desktop and reduced viewports, and loading, empty, error, disabled, and success states.
- **Persisted data:** migration state is checked before tests run.

## Stop conditions

`cc-verify` and `cc-run` **stop and ask** instead of marking something "not verified" and continuing when:

- runtime configuration is missing (for example, only an `.env.example` exists);
- a required database or service is unavailable;
- a needed port is held by an unknown process;
- migrations are pending that could change the result;
- permission to run a needed command is missing.

When one of these could be cleared by creating something (a config from its template, a local database, a results directory), the skill describes exactly what it would create and asks first.

Before invoking `cc-verify`, the cycle skills **preflight** these conditions without changing state. If preflight fails, verification isn't attempted, and the reason is recorded. In a resolution thread that reads, for example:

```text
Fix applied. Verification pending: environment unavailable (PostgreSQL on :5432 not running).
```

## Where verification runs

Stages that only verify or start services write their artefacts to a **disposable workspace**, never to the source tree. Reviews are read-only. See [Role workspace policy](role-workspace-policy.md).

`cc-run` starts services in dependency order and reports each one's process, PID, port, URL, and stop command. It never picks another port silently and never kills a process it didn't start.

## CI reporting

Every review reports the checks the repository actually defines, by their real names, and every state that isn't a pass: pending, failed, cancelled, skipped, and **missing**. A check the repository normally runs that didn't run on this PR is a finding, not a pass. When a repository policy skips a suite (for example, end-to-end tests on draft PRs), the review states it. `skipped` is never reported as `passed`.

For GitHub the reviewers use:

```bash
gh pr view <n> --json isDraft,headRefName,baseRefName,labels,statusCheckRollup
gh pr checks <n>          # output kept even on non-zero exit: it is evidence
```

### Stages that push wait for their head

A local pass isn't the change request's result. `cc-implement-issue` after it opens the PR, and `cc-resolve-comments` after it pushes a fix, wait for the checks **of the head SHA they pushed**, never an earlier run's, while any is queued or running. The wait is bounded by the repository's own timeout, or about 15 minutes when it states none. Their shared section *Checks of the pushed head* holds the rule.

| Checks of the pushed head | Result |
|---|---|
| every required check passed | the local result stands |
| failed within the stage's scope (its own code or tests) | the stage fixes it, pushes and waits again, at most two rounds unless the repository sets its own limit; these rounds aren't cycle iterations |
| failed outside the stage's scope (infrastructure, an unrelated flaky suite) | `PARTIALLY_RESOLVED` for a resolution, `BLOCKED` for an implementation, naming the check and why |
| still pending at the timeout | the local status stands, and every pending check is named; none is reported as passed |

Before pushing, the stage tests the way CI will when that's cheap: no reliance on the global Git identity, `~/.gitconfig`, cached credentials, or anything else only the developer's machine has. The summary states which environment assumptions its local run shares with CI and which it doesn't.

The published summary lists the final head's checks by name and state with the SHA they ran on. The structured result carries them as `checks`: `head_sha`, the counts `passed`, `failed` (failed, cancelled, or expected but missing) and `pending`, and the named `items`.

## In telemetry

When the runtime drives the cycle, each `tests_passed` value is stored beside
its `tests_basis`; the telemetry gate rejects a row that has one without the
other. A boolean `passed` without evidence is `claimed`. A well-formed evidence
list with at least one successful test entry and no failing command is
`agent_reported`. Missing or contradictory evidence fails closed. A stage that
reports `ran: false` records no test outcome. A justified no-code resolution
uses `"tests": { "ran": false, "reason": "no_code_change" }`, never
`passed: true`.

`cc-stats` groups outcomes by basis. It labels `claimed` as claimed and
`agent_reported` as reported; it uses “verified” for test outcomes only when
their basis is `runtime_observed` or `externally_verified`. Agent conclusion
tokens are shown separately from those evidence levels. It also shows the
boundary verification rate among the cycles that required a boundary run.

A verdict row also records `checks_passed`, `checks_failed` and `checks_pending` when the stage reported all three counts for its own head. Counts for a different head, or an incomplete set, record nothing. `cc-stats` sets each implementation and resolution head against its CI, so it can show how often a `RESOLVED` head was actually green. The check names stay in the PR comment. See [Telemetry](telemetry.md).

---

[← Review lifecycle](review-cycle.md) · [↑ Documentation index](README.md) · [Routing and models →](routing.md)
