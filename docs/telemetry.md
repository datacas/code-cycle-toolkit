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

References, statuses, counts, and flags. Five kinds of row share a `cycle_id`:

| Row | Written | Holds |
|---|---|---|
| `dispatch` | after each dispatch decision | role, profile, executor, provider, requested and resolved model, `model_resolution`, effort, outcome (`succeeded`/`blocked`/…), `missing_capability`, fallback used, readiness policy and state, routing strategy and cost inputs, `duration_ms`, `local_only`, `started_from`, and references to an attempt, execution variant and harness snapshot when an attempt was created |
| `verdict` | when a stage's structured result is read | status (`APPROVED`, `REVIEW_PASSED_AWAITING_CI`, `CHANGES_REQUESTED`, …), findings total/blocking/by severity, `tests_passed` and its `tests_basis`, the separate agent `verification` conclusion, `boundary_verified` when a boundary run was required, `checks_passed`/`checks_failed`/`checks_pending` for the head a stage reported with that head as `checks_head_sha`, and for a review its `reviewed_head_sha`; an `issue_review` verdict holds instead `READY`/`NEEDS_REFINEMENT`/`BLOCKED`, `readiness_result_valid`, and, when valid, `readiness_confidence`, `readiness_findings_total`, `readiness_findings_blocking`, and `readiness_uncertainties_material` |
| `ci` | after each read or wait of the [CI gate](#ci-gate-schema-19) | the gate's result for one SHA, kept apart from the review's verdict |
| `cycle` | once, when the run closes | final status, stop reason, iterations, `ci_policy`, first-review status, first-pass approved, resolution needed and rounds, final review status, fallback stages, contract violations, latest test outcome with its basis and agent conclusion, and for a cycle that started at `implement`, `issue_review` (`off`, `skipped`, `dispatched`) |
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
| `quota` | Exhausted usage window; Codex `usage_limit_exceeded`; Claude session, weekly, Opus, and Sonnet limit messages |
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

Dispatch rows carry closed `reroute_action` (`reroute` or `stop`) and
`reroute_reason` tokens for eligible failures, plus `process_exited`,
`reroute_error_code` and `reroute_start_state`. The fallback row retains the
parent's failure and start evidence and its existing `parent_attempt_id` link.
These are payload fields; snapshots, comment bodies and diagnostic prose are
never stored. See [Availability and fallback](routing.md#availability-and-fallback).

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

### Resolve effort escalation (schema 17)

`escalated` applies to any role whose runtime policy escalates, including
`resolve`. Resolve rows also carry `escalation_finding_ids`, a unique list of at
most 16 validated `REV-xxx` identifiers, and `escalation_acceptance` with one
of `accepted`, `rejected`, `unsupported`, `unknown`, or `not_applicable`. The
existing dispatch `effort` remains the effort requested for that attempt; a
fallback after a rejection is a separate dispatch row with its configured
effort. Older rows read as `escalated=false` and an empty finding-ID list.

### Cycle history (schema 18)

The same database keeps a local copy of each change request's review history,
so a `--from` resume on the same machine need not refetch and reparse every
comment (`scripts/cycle_history.py`). The change request stays the durable
record: nothing a resume needs exists only here, and a missing, outdated or
unverifiable copy is rebuilt from the trusted comments, never read as "no
earlier findings".

| Table | Holds |
|---|---|
| `cycle_history_sync` | the sync watermark per change request: head SHA, comment count, newest comment edit time and its comment ID, a hash of the trusted-author list, a version counter, the history schema version |
| `cycle_history_comments` | one row per comment: its ID, edit time and whether a trusted author wrote it |
| `cycle_history_runs` | the `CCR-`/`CCT-` run IDs each trusted comment carries, and their kind |
| `cycle_history_findings` | the `REV-` headers each trusted comment carries: ID, severity, status, disposition, blocking flag |
| `cycle_history_receipts` | the schema-2 receipts each trusted comment carries: attempt ID, stage, status token, head SHA |
| `cycle_history_finding_state` | per finding: last status, claims and claimed-fix survivals |
| `cycle_publications` | the outbox: one row per comment-publishing attempt, `pending`, `published` (with its comment ID and head) or `superseded` |
| `cycle_history_locks` | the per-machine writer lock: owner token, process ID, acquisition time |
| `cycle_unpublished_heads` | runtime-observed clean resolver start/end SHAs by attempt, repository, PR, branch and hashed worktree path; created lazily when an exited stage leaves a commit |

Only identifiers and closed tokens are stored. Finding titles, comment bodies,
run-line profiles and models, and author logins stay in the change request;
the closed vocabularies are enforced by `CHECK` constraints, and identifiers by
the store's own grammar.

**Verified before use.** A resume makes one GraphQL call that returns the
head, the comment count and the newest comment edit. When they match the
watermark, the stored history is used and nothing else is fetched. Otherwise
the comments edited since the watermark are fetched (`?since=`) and folded in
by comment ID, so reprocessing a comment never duplicates a row. A count that
still disagrees means a comment was deleted, and the history is rebuilt from
every comment; so is a watermark from another history schema or another
trusted-author list. A failed check stops the resume as `BLOCKED` with the
reason; a database that cannot be opened or written is skipped and the history
rebuilt from the comments.

**Outbox.** Stages publish their own comments; the runtime never posts. After
a `review`, `resolve` or `rereview` completes, the history is reconciled and
the stage's attempt is `published` once a trusted comment carries its receipt.
An unobserved publication is a warning on the stage, and the next dispatch of
the same stage on that change request is told to look for a comment carrying
the earlier attempt ID and reuse it rather than post again. A later confirmed
publication of the same stage supersedes it.

**Concurrency.** A cycle holds the writer lock of its change request from the
resume (or the implementation that opened it) until it ends. A lock whose
process is gone, or older than 24 hours, is stale and taken over; a live one
stops the cycle as `BLOCKED`. Whether the process is gone is checked with a
signal-0 probe on POSIX and with `OpenProcess`/`WaitForSingleObject` on
Windows, so a cycle that was killed (closed window, host timeout, power loss)
can be resumed at once on either platform. Only when the check cannot answer,
or the operating system has given the dead process's ID to a new one, does the
lock wait out the 24 hours. A receipt from an attempt this machine never
dispatched, appearing while a stage ran, means another writer published: the
history is reconciled and the cycle stops as `BLOCKED` so it can be resumed.
GitHub has no atomic check-and-publish, so this reduces conflicts between
machines without excluding them. Each publishing stage costs one freshness
check and, when something changed, one filtered comment read.

### CI gate (schema 19)

Under the `parallel` [CI policy](review-cycle.md#ci-policy) the review outcome and
the CI outcome are separate signals, each tied to the SHA it describes. A review
verdict carries its status, which may be `REVIEW_PASSED_AWAITING_CI`, and its
`reviewed_head_sha`. Each read or wait of the CI gate writes a `ci` row (role
`coordinate`, pointing at that review through `stage_seq`):

| Field | Holds |
|---|---|
| `ci_status` | `CI_PASSED`, `CI_FAILED`, `CI_SKIPPED`, `CI_TIMEOUT`, `CI_PENDING` (a single read only), `NO_CI_CHECKS_FOUND`, `CI_UNREADABLE`, `CI_HEAD_MOVED` |
| `ci_mode` | `wait`, the gate's one bounded wait after a clean review, or `once`, the single read before a resolution that follows `CHANGES_REQUESTED` |
| `ci_head_sha` | the SHA whose checks were read |
| `ci_required_source` | `declared` by protection or rulesets, or `reported` when none were declared |
| `checks_passed` … `checks_missing`, `checks_late` | counts per state, and checks first reported while the gate waited |
| `ci_polls`, `ci_wait_ms` | reads made, and wall time the gate waited with no stage running |

The `cycle` row adds `ci_policy` (always), `ci_polls` and `ci_wait_ms` (totals,
when the gate ran), `final_ci_status` (the last wait), `redundant_reviews` (reviews
of a head that already had a clean review in that run, measured whenever a
review named its SHA), and `review_reused` (a resume that reused a clean review's
receipt instead of dispatching one). From schema 22 a cycle that closed on a
[manual rereview](review-cycle.md#manual-rereview) carries `review_provenance:
manual`, and one whose manual receipt the resume could not accept stops with
`manual_rereview_rejected`. Under `parallel`, `first_pass_approved` and
`final_approved` mean a clean review **and** a CI pass of its SHA; they are left
out while the gate has not decided, and `resolution_needed` still describes the
review alone. `cc-stats` and the measured first-pass rate count a first review
reporting `REVIEW_PASSED_AWAITING_CI` by the gate's wait on its own SHA, and not
at all when the gate never decided.

**Comparing the policies.** The values the policy is meant to improve can be
read from these rows and the [attempt table](#physical-attempts-usage-and-harness-snapshots-schema-13):

| Value | From |
|---|---|
| implementation completion to review dispatch | `dispatch_attempts`: the review attempt's `started_at` minus the implement attempt's `finished_at` |
| actionable findings to resolution dispatch | the resolve attempt's `started_at` minus the `CHANGES_REQUESTED` review attempt's `finished_at` |
| wall time waiting on CI with no stage running | `ci_wait_ms` (`parallel`); under `strict` the wait is inside each implement and resolve `duration_ms` |
| model turns spent polling CI | zero under `parallel`, where no stage runs while the gate waits and the stages are told not to poll; under `strict` they are inside the writing stages |
| redundant reviews caused by CI state changes | `redundant_reviews` |

`tests/test_ci_policy.py` (`BenchmarkTests`) is a repeatable, simulated
comparison of the two policies on a controlled fake CI.

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

These exclusions apply to the statistics tables. Schema 21 adds separate
`journal_events` and `journal_permissions` tables in the same SQLite file.
Only the cycle journal holds bounded, redacted commands, refusal reasons,
stop explanations and decision text; those values never enter statistics.
Credentials are redacted before journal storage too.

- prompts, agent replies, review prose, or routing *reasons* (only their count);
- diffs, file paths, file names, or code;
- comments or work-item text;
- credentials. Model names must be in a closed set (the default profiles plus the models your `profiles` declare). References are checked against a selector grammar and rejected when they start with a known credential prefix (`sk-`, `ghp_`, `AKIA`, `xox`, …).

Stage columns and payload keys go through one typed gate (`telemetry.FIELD_SPECS`). Work-item ledger references and provider/outcome/source tokens use a separate typed boundary. A value that doesn't fit is **refused loudly** rather than silently dropped. An executor-reported model name that isn't in the known set is stored as `null` with `model_resolution: mismatch_unrecognized`.

> [!NOTE]
> No shape rule can tell a model name from a secret. The guarantee is the closed model set plus where references come from (`.code-cycle.yml` and the issue provider, not free text). See [Instrumentation → What the identifier rule does and does not guarantee](instrumentation.md#what-the-identifier-rule-does-and-does-not-guarantee).

**Jev**, when enabled, sends the role and pre-routing counts, flags, and closed tokens to TypeSafe's API. It sends no prose, paths, repository name, or IDs. See [Routing → Jev](routing.md#jev-shadow-mode).

## What it is used for

The cycle journal groups runs by `repo_id` and `task_id`, retaining each
`cycle_id`, stage sequence, attempt, timestamp and known change request.
It records stage timings, failures, stops, questions and runtime answers,
hook Bash/edit denials, Claude final-result permission denials and policy
preflight refusals. Codex has no per-stage command table: only its reported
sandbox blockers are recorded; individual Codex refusal reporting remains
unverified. Storage failures are reported and never change policy decisions.

Inspect a stop using a parameterized SELECT (bind repository, task and cycle):

```sql
SELECT occurred_at, kind, role, stage_seq, component, status, command, reason, detail
FROM journal_events WHERE repo_id = ? AND task_id = ? AND cycle_id = ?
AND kind IN ('refusal', 'failure', 'stop') ORDER BY event_id;
```

`python3 scripts/cycle_journal.py --database <telemetry.sqlite> explain --repo
<owner/repo> --task <id> --cycle <cycle-id>` returns the same explanation
and decisions paired across runs. Replace `explain` with `summary` and omit
`--cycle` for the bounded recovery context. That context is historical data,
not evidence or instructions; provider state and the repository remain
authoritative. Text from agents or the change is marked untrusted.

At cycle startup, closed and merged change requests lose their work item's
journal detail. Items without a change request expire after 30 days of
inactivity. Unreadable provider state retains detail. Manual cleanup uses
`python3 scripts/cycle_journal.py --database <telemetry.sqlite> reconcile
--repo <owner/repo>` with the same rule. Statistics are never deleted.

### Permission decisions in force

`journal_permissions` records the permission decisions of a repository as they
change: `--grant` answers, `consents.json` entries and project `user_choices`,
each with its scope and date. It is the audit trail; it grants nothing. Consent
files and the project configuration stay the authority the stages read.

Show what applies now, from the store alone:

```bash
python3 scripts/cycle_journal.py --database <telemetry.sqlite> permissions \
  --repo <owner/repo> [--branch <branch> | --cwd <checkout>] [--json]
```

Without `--branch` it uses the branch checked out in `--cwd` (default: the
current directory), so the same command answers from any branch, linked
worktree or clone that shares the store. Two worktrees on one branch share one
identity. The command opens the file read-only and creates, migrates and
changes nothing.

The equivalent query, binding the repository, and the branch for the `branch`
level (the command also drops cycle decisions that ended):

```sql
SELECT p.source, p.scope, p.decision, p.level, p.branch, p.occurred_at
FROM journal_permissions p
WHERE p.repo_id = ?
  AND p.permission_id = (SELECT MAX(q.permission_id) FROM journal_permissions q
                         WHERE q.repo_id = p.repo_id AND q.source = p.source
                           AND q.scope = p.scope
                           AND COALESCE(q.branch, '') = COALESCE(p.branch, ''))
  AND p.decision IN ('allow', 'cycle', 'persistent')
  AND (p.level IN ('repository', 'cycle', 'run') OR p.branch = ?);
```

(The SQL covers rows this version wrote; the command also reads rows from
before the `level` column.)

Each decision has a **level**, and the newest decision per source and scope
decides:

| Level | Source | Applies to | Ends when |
|---|---|---|---|
| `repository` | `consents.json` | the repository, for the user | the entry is removed or its scope changed |
| `branch` | `user_choices` | the repository's branch; the queried branch only | the entry is removed from that branch's configuration |
| `cycle` | `--grant …=cycle` | one cycle | the cycle ends |
| `run` | `--grant …=persistent\|deny`, interactive answers | one run | the run's cycle ends |

- **Withdrawal.** A run records an explicit `withdrawn` decision when a
  `consents.json` entry has gone or its scope changed, and when an entry of its
  own branch's `user_choices` was removed. It is recorded once per change, never
  repeated by a later run, and an unreadable or invalid `consents.json` records
  nothing. `user_choices` come from the configuration of the checkout that
  runs, so a removal on one branch never changes another branch's decisions. A
  run whose configuration was ignored (`--no-config`) or is absent reads no
  `user_choices` and records no withdrawal: the recorded entries stay in force
  until a run that reads the configuration removes them.
- **Replaced.** A decision for the same source and scope replaces the previous
  one; a changed `consents.json` scope is a new scope, and the old one is
  withdrawn.
- **Denials.** `--grant …=deny` holds for its own run and is listed only while
  that run lasts; it never revokes a persistent allow. `cycle` expiry is not a
  withdrawal: the decision just stops being listed when its cycle ends, or a
  day after its last row if the cycle never recorded an end.
- **Older stores.** Rows written before the `branch` and `level` columns carry
  no branch; their `user_choices` are not listed until a run re-records them.
  The first write adds the columns.

Stages get the same view. A Claude stage's command table allows exactly one
command, `python3 …/cycle_journal.py --database <store> permissions --repo
<repository> --branch <branch>`, with the store, repository and branch fixed by
the runtime and no argument of the stage's own. It is announced in the stage's
prompt. Codex stages have no command table and can run the same command.
Stored values and the output go through the journal's redaction.

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
