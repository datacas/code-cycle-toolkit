# Telemetry and `cc-stats`

When the runtime drives a cycle, every stage writes rows to a local SQLite database. `cc-stats` turns those rows into a per-repository report. This page covers what is recorded, what never is, and how to read it.

**On this page:** [When rows are written](#when-rows-are-written) · [Where it lives](#where-it-lives) · [What is recorded](#what-is-recorded) · [Work-item outcomes](#work-item-outcomes) · [What is never recorded](#what-is-never-recorded) · [What it is used for](#what-it-is-used-for) · [Reading it with cc-stats](#reading-it-with-cc-stats) · [Limits](#limits)

## When rows are written

| You ran… | Rows written? |
|---|---|
| `run_cycle.py` | ✅ every stage, always |
| `cc-orchestrator` driving stages through the runtime recorder | ✅ |
| a skill by hand, or the manual cycle | — |
| anything with `--no-runtime` / `npx skills` only | — (`cc-stats` reports that the runtime isn't installed) |

The published PR comment is a separate record that exists either way. See [Review lifecycle](review-cycle.md).

Manual review and triage run lines use profile `manual` and remain visible in
that comment record. They have no telemetry rows and are not dispatch stages;
count them separately only in comment-based analysis.

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
| `dispatch` | after each dispatch decision | role, profile, executor, provider, requested and resolved model, `model_resolution`, effort, outcome (`succeeded`/`blocked`/…), `missing_capability`, fallback used, readiness policy and state, routing strategy and cost inputs, `duration_ms`, `local_only`, `started_from`, and references to an attempt, execution variant and harness snapshot when an attempt was created |
| `verdict` | when a stage's structured result is read | status (`APPROVED`, `CHANGES_REQUESTED`, …), findings total/blocking/by severity, `tests_passed` and its `tests_basis`, the separate agent `verification` conclusion, `boundary_verified` when a boundary run was required, and `checks_passed`/`checks_failed`/`checks_pending` for the head a stage pushed; an `issue_review` verdict holds instead `READY`/`NEEDS_REFINEMENT`/`BLOCKED`, `readiness_result_valid`, and, when valid, `readiness_confidence`, `readiness_findings_total`, `readiness_findings_blocking`, and `readiness_uncertainties_material` |
| `cycle` | once, when the run closes | final status, stop reason, iterations, first-review status, first-pass approved, resolution needed and rounds, final review status, fallback stages, contract violations, latest test outcome with its basis and agent conclusion, and for a cycle that started at `implement`, `issue_review` (`off`, `skipped`, `dispatched`) |
| `shadow` | after `implement`/`resolve` when Jev is enabled | the rules' profile, Jev's suggestion, agreement, confidence, probabilities, status, model, duration |

**Pre-routing signals** are what the router could have known before it chose a profile. They are kept separate from outcomes so a future selector can be judged fairly:

| Kind | Signals |
|---|---|
| declared | `difficulty`, `verifiability`, `security_sensitive` |
| read from the diff against `repository.default_branch` | changed file count; has tests; touches dependencies, database, auth, API, migrations, or CI; changed files per language (python, javascript, typescript, go, rust, java, csharp, ruby, php, shell, sql, markdown, other) |
| from the cycle | prior findings (total, blocking, per severity), previous failed attempts, resolution round, repeated findings, `verification_available`, `escalated` (an issue review routed to its stronger profile) |
| estimated | changed lines, test count |

**Unknown is not zero.** A signal or outcome nobody observed is left out, never stored as `0`, `false`, or "failed". An `implement` stage has no diff signals, because no diff exists yet. A cycle without a closing row is *unknown*, not failed.

An implementation `forecast` is a pre-edit claim made after implement routing, recorded only on the implement verdict as `forecast_*` outcomes and never used as pre-routing input.

Issue-review findings, uncertainties, evidence references, and proposed edits are printed for the operator and never stored: only their counts and closed tokens are. Rows from before schema 11 have no issue-review stage and read as they were written.

A stage the host stopped before it finished is recorded with the outcome `interrupted`, and its cycle with the stop reason `interrupted` (schema 12); a run killed without a catchable signal still leaves no closing row and reads as unknown. A cycle that continued an interrupted implementation with `--continue` carries `continued` on its cycle row.

### Physical attempts, usage, and harness snapshots (schema 13)

Each executor invocation has a `dispatch_attempt_id`, even when multiple
invocations share one logical `(cycle_id, stage_seq)` because of a retry or
fallback. `dispatch_attempts` records the work item, cycle, stage, parent
attempt, execution variant, lifecycle state, outcome, safe error code,
duration, and any Orca `dispatchId`. An Orca launch remains `launched` until a
later partial update confirms a terminal state. Updates are idempotent by
`update_id`; omitted fields leave the prior value intact. A contradictory
terminal update creates a reconciliation conflict. A correction requires a
closed correction reason and preserves the prior update.

Token observations are stored by reported category and source, including cache
and reasoning categories when the executor reports them. Missing categories
have no observation; a reported zero remains a measured zero. Aggregation keeps
categories separate so overlapping provider fields are never added into an
invented total. Retries and fallbacks aggregate through their distinct attempt
IDs without counting the same observation ID twice.

Cost measures carry exactly one basis: `actual_billed`,
`api_equivalent_estimated`, `subscription_consumption`, or `unknown`. Native
CLI `costUSD` reports have `unknown` basis and keep the reported amount apart
from an attributable billed amount. API-equivalent estimates require an explicit
pricing snapshot ID and date; subscription consumption keeps its own unit.
Totals are grouped by basis, component, and unit. An actual billed measure
takes precedence over an API estimate with the same attribution key, and no
combined monetary total across bases or currencies is produced.

Each attempt references an execution variant (executor, provider, requested
and resolved model when known, and effort) and a content-free harness snapshot.
The snapshot records release and commit, CLI version, effective profile and
routing policy, runtime manifest, relevant prompt templates, and the stage
skill/configuration hashes. It contains no prompt or issue text, credentials,
or local paths. Identical normalized components share a fingerprint.

Historical stage rows remain queryable. They have no new attempt or usage rows;
the new fields are unmeasured and must be read as unknown, never as zero.

The field-by-field schema, correlation keys, and schema history are in [Instrumentation → Telemetry](instrumentation.md#telemetry).

### Dispatch failure evidence (schema 15)

`dispatch_attempts.error_code` describes the cause of a non-succeeded attempt,
including `BLOCKED`. It is distinct from executor availability and
`missing_capability`, whose vocabulary is unchanged.

| `error_code` | Meaning |
|---|---|
| `capacity` | Temporarily overloaded model or server; Codex `server_overloaded` |
| `quota` | Exhausted usage window; Codex `usage_limit_exceeded` |
| `auth` | Missing or rejected credentials, including a reported 401 |
| `transport` | Network/API connection failure or stream disconnect |
| `timeout` | Dispatch exceeded its time limit; work may still be alive |
| `unreadable_result` | Output exists but contains no parseable structured stage result |
| `unavailable` | Executor unavailable or waiting on an interactive prompt |
| `contract_violation` | Wrong model ran or the read-only contract could not be established |
| `interrupted` | Operator signal stopped the cycle |
| `precondition` | Runtime/workspace condition failed before launch |
| `executor_error` | Executor reported an error not mapped to a specific class |
| `no_error_report` | No stage result and no executor error report |

Structured executor signals take precedence over stderr and exit status.
Codex reads terminal errors from JSON events, including `task_complete` and
`codex_error_info`. On a failed or incomplete run, when the stream lacks that error, it reads the session log
under `CODEX_HOME/sessions` identified by this invocation's `thread.started`
ID, verifying the log's session ID. It never selects a different run's newest
log. Claude reads its stream-json error/result envelope. Assistant prose is
not an executor error. The Codex stdin banner is ignored in stderr fallback.
An unmapped structured code stays `executor_error` even when its message
contains a familiar error phrase. `unclassified` is not used.

`dispatch_attempts.start_state` uses the typed gate `attempt_start_state`:

| State | Evidence |
|---|---|
| `started` | Codex session (`thread.started`), tool/command/file-change or agent-message event; Claude session init/session ID or tool-use event; observed Git workspace change; Orca dispatch receipt ID |
| `not_started` | Explicit executor `started: false`/`start_state: not_started`, a pre-launch capability refusal, or failure to spawn the process |
| `unknown` | No positive evidence either way, such as timeout with no stream, a non-streamed run or asynchronous launch with no receipt ID |

Any activity overrides negative evidence. Process launch, lifecycle `running`,
and a timer alone do not prove agent work started. `unknown` may refine to
`started` or `not_started`, and `not_started` may refine to `started`.
`started` never reverts, including during corrections; other transitions
produce conflicts. Recovery must reconcile `unknown` as though it started
before rerouting (#173).

| `missing_capability` | Attempt `error_code` | Start evidence |
|---|---|---|
| `operating_quota` | `quota` | Per-executor evidence |
| `operating_availability` | `unavailable` | Per-executor evidence |
| `authenticated_session`, `bypass_acknowledgement`, `folder_trust`, `hook_trust`, `trusted_directory` | `unavailable` | Per-executor evidence |
| `orchestration_context`, `review_workspace_isolation`, `review_workspace_mismatch`, `review_workspace_conflict`, `provider_agent_mapping` | `precondition` | `not_started` for checks before launch |
| `publication_access`, `proven_readiness`, `read_only_enforcement`, `disposable_workspace`, `workspace_policy` | `precondition` | `not_started` for checks before launch; no attempt row when refused by the outer gate |
| `read_only_verification` | `contract_violation` | `not_started` when the fingerprint precheck refuses launch; a violation detected after execution is `started` |

The mapping is explicit and tested; new capabilities cannot fall through to a
default class.

Workspace fingerprints used only to infer start activity hash at most 128 dirty
or untracked files and 8 MiB in total. Exceeding either budget supplies no
workspace start evidence; stream evidence still applies, otherwise the state
remains `unknown`. Positive stream evidence skips the second fingerprint.
Read-only contract verification has no such budget and remains exhaustive.

**Migration:** a schema-14 store is detected by the absence of `start_state`,
since there is no database-level version marker. Initialization adds the
column with `unknown` for old rows and rewrites only
`dispatch_attempts.error_code`: `operating_quota` → `quota`,
`operating_availability` → `unavailable`, and `dispatch_failed`,
`executor_error`, `unknown` → `executor_error`. Other values remain unchanged.
The append-only `dispatch_attempt_updates` log, including `patch_json` and
`patch_hash`, is untouched; its readers accept historical tokens. New writes
reject the removed values. The migration is transactional and idempotent and
does not backfill from session logs.

**Diagnostics:** observed excerpts and raw executor error codes live only in
the stop report and live status snapshot, bounded and redacted before display.
They never enter telemetry. The [What is never recorded](#what-is-never-recorded)
boundary remains unchanged.

### Stage recoveries (schema 16)

`stage_recoveries` is an append-only table keyed by `attempt_id` and linked to
`dispatch_attempts`. A row is written when a stage that omitted its structured
result is recovered from attempt-attributed comment evidence (see [Review
lifecycle → Missing structured result recovery](review-cycle.md#missing-structured-result-recovery)).
Each row stores only references: PR ID, full head SHA, provider comment ID,
creation time, schema version, and the closed state
`recovered_pending_verification`. Repository, work item, cycle and stage are
recovered through the attempt foreign key. No comment body, execution excerpt,
finding narrative or receipt JSON enters this table. SQL triggers reject
updates and deletions. Opening a schema-15 database creates the table without
rewriting existing attempts or their update logs; the recovered attempt keeps
its failed outcome. The later verifier has its own normal dispatch and verdict
records, so recovery never masquerades as a successful stage verdict.

### Work-item outcomes (schema 14)

Work-item disposition events are stored in a separate append-only table. Each
transition records the provider, repository or project, work-item ID, linked
cycle and PR IDs, outcome, source (`provider_query` or `explicit`), and both
the provider's `observed_at` and the local `recorded_at`, and per-PR merged
references. No work-item text,
comments, or PR descriptions are copied. Repeated snapshots with an unchanged
disposition and associations, and snapshots older than the latest observed
transition, are ignored.
Implementation verdicts keep only a numeric PR identifier from the reported
change-request reference, so reconciliation can include toolkit-created PRs.
The event qualifies that identifier with the cycled code repository when it
differs from the work-item repository. PR links returned by GitHub retain their
own repository too; event references use `owner/repo#number` when a PR is in a
different repository from the work item. Older unqualified event references
are interpreted in the work-item repository.

Outcomes are `resolved`, `closed_unresolved`, `pr_merged`, `reopened`,
`reverted`, and `unknown`. GitHub's `COMPLETED` close reason maps to resolved;
`NOT_PLANNED` and `DUPLICATE` map to closed unresolved. An open issue is marked
reopened after a previously observed closed state. A merge, green check, review
verdict, or ready-for-merge result does not imply issue resolution. Plane's
completed and cancelled groups and Jira's Done category plus a configured
resolution mapping are normalized by the provider mapping layer; missing or
unmapped Jira resolutions stay unknown. When a work item is resolved, the
linked resolving PRs are snapshotted at that transition; a later PR merge is
tracked per PR without changing that resolving snapshot. A `reverted` event
must identify a previously merged resolving PR; an unrelated PR cannot revoke
the resolution. A later-linked PR remains non-resolving unless a person
explicitly associates it with `--resolving-pr-id`.

Cycle rows retain the configured issue provider and repository/project
with the task ID. GitHub defaults to the repository being cycled; Plane and
Jira use `code_cycle.issue.project`. Pass `--work-item-repository` when a
work item belongs to a different repository or project.
Reconciliation selects identity-bearing cycles for the requested provider and
repository. A legacy cycle without stored identity is attached only when no
known cycle puts that task ID in a different scope; if the known cycles span
multiple scopes, the legacy cycle stays unassociated rather than being guessed.
Statistics apply the same rule: a legacy cycle counts under the only scope known
for its task ID, whether a scoped cycle or a recorded disposition revealed it,
and otherwise stays one unknown item. A bare PR number from a GitHub work item
counts as the same PR as its qualified `owner/repo#number` form.

Run reconciliation on demand; there is no background polling. The current
standalone query adapter uses the authenticated `gh` CLI and visits work items
with recorded cycles. A failed query appends a timestamped safe failure category
to `work_item_reconciliation_failures`, creates no disposition transition, and
leaves the prior disposition intact. To record a person-supplied observation or
explicit PR association, use the entry command:

```sh
python3 scripts/work_item_outcomes.py reconcile \
  --repo-id datacas/code-cycle-toolkit --repository datacas/code-cycle-toolkit
python3 scripts/work_item_outcomes.py record \
  --repo-id datacas/code-cycle-toolkit --provider github \
  --repository datacas/code-cycle-toolkit --work-item-id 120 --outcome resolved
```

## What is never recorded

- prompts, agent replies, review prose, or routing *reasons* (only their count);
- diffs, file paths, file names, or code;
- comments or work-item text;
- credentials. Model names must be in a closed set (the default profiles plus the models your `profiles` declare). References are checked against a selector grammar and rejected when they start with a known credential prefix (`sk-`, `ghp_`, `AKIA`, `xox`, …).

Stage columns and payload keys go through one typed gate (`telemetry.FIELD_SPECS`). Work-item ledger references and provider/outcome/source tokens use a separate typed boundary. A value that doesn't fit is **refused loudly** rather than silently dropped. An executor-reported model name that isn't in the known set is stored as `null` with `model_resolution: mismatch_unrecognized`.

> [!NOTE]
> No shape rule can tell a model name from a secret. The guarantee is the closed model set plus where references come from (`.code-cycle.yml` and the issue provider, not free text). See [Instrumentation → What the identifier rule does and does not guarantee](instrumentation.md#what-the-identifier-rule-does-and-does-not-guarantee).

**Jev**, when enabled, sends the role and pre-routing counts, flags, and closed tokens to TypeSafe's API. It sends no prose, paths, repository name, or IDs. See [Routing → Jev](routing.md#jev-shadow-mode).

## What it is used for

- **First-pass rate** per repository: how often an implementation passes its first review. It is reported as unknown below 10 observations. The `measured` routing strategy feeds it into the recorded cost estimate.
- **Dispatch blockages** by capability, so an exhausted quota window stays distinct from a trust dialog.
- **Model drift:** the requested model versus the model the executor reported. Executors that report nothing (Codex) are counted as unmeasured, not as agreement.
- **Rules versus Jev:** agreement and outcome comparisons from shadow rows.
- **Cycle outcomes:** how runs ended and why they stopped, how many rounds they took, how many had a finding that survived a claimed fix, and whether tests ran. Cycles recorded before schema 7 show an unknown stop reason.
- **Work-item outcomes:** one current disposition per unique provider work item across all cycles in the period. Unobserved outcomes stay unknown; merged PRs are counted separately from resolution. The report contains aggregates only, not provider IDs.
- **Test evidence:** per-cycle outcomes grouped by `claimed`, `agent_reported`, `runtime_observed`, and `externally_verified`, plus the agent's conclusion tokens as a separate breakdown. Historical rows without a basis count as `claimed`.
- **Boundary verification:** how many cycles whose change required a boundary run had evidence of one. Cycles that did not require one are left out.
- **Forecast accuracy:** predicted flags and size estimates compared with the first review dispatch in the same cycle; rates and error bands remain unknown below the minimum sample.

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

**The report includes:** tasks and stages, first-pass approval (numerator/denominator), daily activity, stages by role and profile, fallbacks, dispatch blockages, model drift, review verdicts, cycle outcomes, findings by severity, test verification, CI on stage heads, measured durations, and Jev comparisons. Rates stay *unknown* below 10 tasks. A period is compared with the previous one only when both have 10 or more. Routing cost *estimates* are never presented as real cost.

The report prints aggregates only. It never prints task or cycle IDs, comments, prompts, paths, diffs, or raw rows. If the database is missing or empty for this repository, it says so. It never creates a database or a configuration file.

## Limits

- Telemetry **records**. Nothing reads it back to change behaviour automatically. There is no model selection from statistics, no scoring, and no adaptive learning. The only feedback path is the `measured` strategy's cost estimate.
- The schema will keep changing. Older rows are read as they were written.
- CI check names stay in the PR comment. Telemetry keeps only the counts for the head an implementation or resolution finished on.

---

[← Providers](provider-contract.md) · [↑ Documentation index](README.md) · [Optional workspace tools →](workspace-tools.md)
