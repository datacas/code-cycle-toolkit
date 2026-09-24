# Telemetry and `cc-stats`

When the runtime drives a cycle, every stage writes rows to a local SQLite database. `cc-stats` turns those rows into a per-repository report. This page covers what is recorded, what never is, and how to read it.

**On this page:** [When rows are written](#when-rows-are-written) · [Where it lives](#where-it-lives) · [What is recorded](#what-is-recorded) · [What is never recorded](#what-is-never-recorded) · [What it is used for](#what-it-is-used-for) · [Reading it with cc-stats](#reading-it-with-cc-stats) · [Limits](#limits)

## When rows are written

| You ran… | Rows written? |
|---|---|
| `run_cycle.py` | ✅ every stage, always |
| `cc-orchestrator` driving stages through the runtime recorder | ✅ |
| a skill by hand, or the manual cycle | — |
| anything with `--no-runtime` / `npx skills` only | — (`cc-stats` reports that the runtime isn't installed) |

The published PR comment is a separate record that exists either way. See [Review lifecycle](review-cycle.md).

## Where it lives

```text
$CODE_CYCLE_HOME/telemetry.sqlite                         when CODE_CYCLE_HOME is set
${XDG_CONFIG_HOME:-~/.config}/code-cycle-toolkit/telemetry.sqlite   Linux, macOS, WSL
%APPDATA%\code-cycle-toolkit\telemetry.sqlite              Windows
```

The database is outside every repository and never enters Git. Every row carries `repo_id`, the repository selector, and `cc-stats` only reads rows for the repository you are in, so one database can serve many repositories. `run_cycle.py --database <path>` writes to another file.

## What is recorded

References, statuses, counts, and flags. Four kinds of row share a `cycle_id`:

| Row | Written | Holds |
|---|---|---|
| `dispatch` | once per stage attempt, **before** its result is known | role, profile, executor, provider, requested and resolved model, `model_resolution`, effort, outcome (`succeeded`/`blocked`/…), `missing_capability`, fallback used, readiness policy and state, routing strategy and cost inputs, `duration_ms`, `local_only`, and the pre-routing signals below |
| `verdict` | when a stage's structured result is read | status (`APPROVED`, `CHANGES_REQUESTED`, …), findings total/blocking/by severity, `tests_passed` when tests actually ran |
| `cycle` | once, when the run closes | final status, iterations, first-review status, first-pass approved, resolution needed and rounds, final review status, fallback stages, contract violations, tests passed |
| `shadow` | after `implement`/`resolve` when Jev is enabled | the rules' profile, Jev's suggestion, agreement, confidence, probabilities, status, model, duration |

**Pre-routing signals** are what the router could have known before it chose a profile. They are kept separate from outcomes so a future selector can be judged fairly:

| Kind | Signals |
|---|---|
| declared | `difficulty`, `verifiability`, `security_sensitive` |
| read from the diff against `repository.default_branch` | changed file count; has tests; touches dependencies, database, auth, API, migrations, or CI; changed files per language (python, javascript, typescript, go, rust, java, csharp, ruby, php, shell, sql, markdown, other) |
| from the cycle | prior findings (total, blocking, per severity), previous failed attempts, resolution round, `verification_available` |
| estimated | changed lines, test count |

**Unknown is not zero.** A signal or outcome nobody observed is left out, never stored as `0`, `false`, or "failed". An `implement` stage has no diff signals, because no diff exists yet. A cycle without a closing row is *unknown*, not failed.

The field-by-field schema, correlation keys, and schema versions 1–4 are in [Instrumentation → Telemetry](instrumentation.md#telemetry).

## What is never recorded

- prompts, agent replies, review prose, or routing *reasons* (only their count);
- diffs, file paths, file names, or code;
- comments or work-item text;
- credentials. Model names must be in a closed set (the default profiles plus the models your `profiles` declare). References are checked against a selector grammar and rejected when they start with a known credential prefix (`sk-`, `ghp_`, `AKIA`, `xox`, …).

Every field, column or payload key, goes through one typed gate (`telemetry.FIELD_SPECS`). A value that doesn't fit is **refused loudly** rather than silently dropped. An executor-reported model name that isn't in the known set is stored as `null` with `model_resolution: mismatch_unrecognized`.

> [!NOTE]
> No shape rule can tell a model name from a secret. The guarantee is the closed model set plus where references come from (`.code-cycle.yml` and the issue provider, not free text). See [Instrumentation → What the identifier rule does and does not guarantee](instrumentation.md#what-the-identifier-rule-does-and-does-not-guarantee).

**Jev**, when enabled, sends the role and pre-routing counts, flags, and closed tokens to TypeSafe's API. It sends no prose, paths, repository name, or IDs. See [Routing → Jev](routing.md#jev-shadow-mode).

## What it is used for

- **First-pass rate** per repository: how often an implementation passes its first review. It is reported as unknown below 10 observations. The `measured` routing strategy feeds it into the recorded cost estimate.
- **Dispatch blockages** by capability, so an exhausted quota window stays distinct from a trust dialog.
- **Model drift:** the requested model versus the model the executor reported. Executors that report nothing (Codex) are counted as unmeasured, not as agreement.
- **Rules versus Jev:** agreement and outcome comparisons from shadow rows.
- **Cycle outcomes:** how runs ended, how many rounds they took, and whether tests ran.

## Reading it with `cc-stats`

Ask your agent in any language, or use the slash command where your host has one:

```text
Use cc-stats.
Show me the toolkit stats for the last 7 days.
Muéstrame las estadísticas del toolkit de todo el histórico.
/cc-stats
```

Requirements:

- the runtime installed (it looks for `<repo>/.code-cycle/runtime/stats.py`, then `~/.code-cycle/runtime/stats.py`, then `scripts/stats.py` inside the toolkit checkout);
- `code_cycle.repository.selector` in `.code-cycle.yml`. The repository is never guessed from a remote.

The skill runs `stats.py` read-only and replies with the complete report. You can also run it yourself:

```bash
python3 ~/.code-cycle/runtime/stats.py --cwd "$(git rev-parse --show-toplevel)"          # last 30 days
python3 ~/.code-cycle/runtime/stats.py --cwd . --days 7
python3 ~/.code-cycle/runtime/stats.py --cwd . --all-time --format json
```

**The report includes:** tasks and stages, first-pass approval (numerator/denominator), daily activity, stages by role and profile, fallbacks, dispatch blockages, model drift, review verdicts, cycle outcomes, findings by severity, test verification, measured durations, and Jev comparisons. Rates stay *unknown* below 10 tasks. A period is compared with the previous one only when both have 10 or more. Routing cost *estimates* are never presented as real cost.

The report prints aggregates only. It never prints task or cycle IDs, comments, prompts, paths, diffs, or raw rows. If the database is missing or empty for this repository, it says so. It never creates a database or a configuration file.

## Limits

- Telemetry **records**. Nothing reads it back to change behaviour automatically. There is no model selection from statistics, no scoring, and no adaptive learning. The only feedback path is the `measured` strategy's cost estimate.
- The schema will keep changing. Older rows are read as they were written.
- CI results stay in the PR comment. `checks_passed` and `checks_failed` exist as fields, but no stage reports them.

---

[← Providers](provider-contract.md) · [↑ Documentation index](README.md) · [Role workspace policy →](role-workspace-policy.md)
