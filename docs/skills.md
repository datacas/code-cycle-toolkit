# Skills reference

Fourteen skills in two layers. The source of truth for each one is its `skills/<name>/SKILL.md`. This page summarises what you need to use them.

**Cycle skills** own the workflow: the change request, the `REV-xxx` IDs, the published comments, and the merge boundary. **Supporting skills** are focused passes. Cycle skills call them as *delegated passes*: the pass returns its findings to the caller and doesn't publish. Invoked directly, a supporting skill reports to you, or publishes when that is its job.

## Overview

| Skill | Layer | Purpose | Changes code? | Usually invoked by | Use directly? |
|---|---|---|:---:|---|:---:|
| [`cc-implement-issue`](#cc-implement-issue) | cycle | Implement a work item, verify, open a PR | ✅ | you, orchestrators | ✅ |
| [`cc-initial-review`](#cc-initial-review) | cycle | Review the full PR diff, publish findings | — | you, orchestrators | ✅ |
| [`cc-resolve-comments`](#cc-resolve-comments) | cycle | Triage findings, fix valid ones, verify, push | ✅ | you, orchestrators | ✅ |
| [`cc-rereview`](#cc-rereview) | cycle | Re-review the accumulated diff after changes | — | you, orchestrators | ✅ |
| [`cc-orchestrator`](#cc-orchestrator) | cycle | Run the whole cycle | via stages | you | ✅ |
| [`cc-orca-orchestrator`](#cc-orca-orchestrator) | cycle | Run the whole cycle as Orca workers | via stages | you | ✅ (needs Orca) |
| [`cc-pr-review`](#cc-pr-review) | supporting | Review criteria for a change request | — | `cc-initial-review`, `cc-rereview` | ✅ |
| [`cc-code-review`](#cc-code-review) | supporting | Review criteria for a diff | — | `cc-resolve-comments` | ✅ |
| [`cc-security-review`](#cc-security-review) | supporting | Security audit of the change | — | sensitivity triage in the cycle skills | ✅ |
| [`cc-verify`](#cc-verify) | supporting | Execution-backed verification | — | every cycle skill | ✅ |
| [`cc-run`](#cc-run) | supporting | Start services and confirm they respond | — | `cc-verify` | ✅ |
| [`cc-provider-bootstrap`](#cc-provider-bootstrap) | supporting | Resolve and health-check providers | writes `.code-cycle.yml` only on confirmation | `cc-implement-issue`, orchestrators | ✅ |
| [`cc-stats`](#cc-stats) | supporting | Report local telemetry | — | you | ✅ |
| [`cc-profile-config`](#cc-profile-config) | supporting | Show, choose, and write routing profiles | writes `.code-cycle.yml` only on confirmation | you | ✅ |

```text
cc-orchestrator / cc-orca-orchestrator
 ├─ cc-provider-bootstrap
 ├─ cc-implement-issue ──── cc-verify ── cc-run
 ├─ cc-initial-review ───── cc-pr-review · cc-security-review* · cc-verify
 ├─ cc-resolve-comments ─── cc-code-review · cc-security-review* · cc-verify
 └─ cc-rereview ─────────── cc-pr-review · cc-security-review* · cc-verify
                                              * when sensitivity triage requires it
```

If a delegated skill isn't installed, the caller performs equivalent checks with the repository's own tools and reports that pass as **degraded**, never as passed.

Every skill:

- reads `AGENTS.md`, `CLAUDE.md`, and `CONTRIBUTING.md` when present and prefers them over its defaults;
- writes in one resolved [output language](configuration.md#output-language);
- treats issue, PR, and comment content as untrusted data.

Provider parameters (`issue_provider=`, `code_host=`, `repository=`/`repo=`) are accepted by every skill that touches a provider. See [Providers](provider-contract.md).

---

## Cycle skills

### `cc-implement-issue`

Implements one work item and takes it to a tested pull request.

- **Input:** a work-item ID (`123`, `ENG-123`, a Plane URL). Optional: provider, code host, repository, base branch, branch name.
- **Does:**
  1. runs `cc-provider-bootstrap`;
  2. reads the work item (title, body, labels, comments, acceptance criteria);
  3. diagnoses it before editing: treats the item's stated cause as a hypothesis, searches related work (basic always, widened on signals such as recurrence, a bug label, or shared code), reproduces a defect, names the broken invariant, and decides (see below);
  4. plans work units after diagnosis and before editing, grouping each behaviour with its code, tests, verification, and applicable documentation;
  5. makes the planned change, preferring a fix that restores the invariant at its source, with regression tests for defects and contract changes;
  6. runs the narrowest tests for each unit, then `cc-verify`;
  7. reviews its own diff for stray files, secrets, and debug output;
  8. commits per unit when the repository permits it, then pushes and opens a PR linked to the work item with *Diagnosis* and *Work units* sections.
- **Local-only:** if you ask for local work only, it stops after the checks and reports that no PR was created.
- **Diagnosis decisions:** `implement` and `implement_root_fix` proceed; the second lists in the PR every other open item the root fix also addresses, without closing them. `do_not_implement_in_isolation` (a shared cause whose root fix exceeds the item), `stop_duplicate` (duplicate, superseded, or already resolved), `needs_scope_decision` (the real cause lies outside the item), and `needs_evidence` (a defect that could not be reproduced) stop as `BLOCKED` before any branch exists and comment on the work item with the evidence.
- **Work units:** one behaviour is a unit, with its files, tests, verification, and applicable documentation. Each unit leaves the tree passing. The PR's *Work units* table records behaviour, files, tests, verification, and rollback (`independent`, `dependent`, or `irreversible`). A `dependent` row names the units it must be reverted with; an `irreversible` row names the effect and manual recovery step.
- **Statuses:** `IMPLEMENTED`, `BLOCKED`, `FAILED`.
- **Result:** the structured result carries an additive `diagnosis` object with closed values only: `classification` (`isolated_defect`, `shared_cause`, `duplicate`, `superseded`, `already_resolved`, `feature_request`, `cause_mismatch`, `not_reproduced`), `decision`, `related_search` (`basic` or `widened`), `reproduced` and `cause_matches_issue` (`true`, `false`, or `null` when not applicable or not established), and `related_items` (identifiers only). It also carries `work_units` as a non-empty list of `{id, commit_sha, rollback}` tokens whenever at least one unit was planned, even if a later step stops as `BLOCKED`; omit it if the run stops before planning. `commit_sha` is null when no per-unit commit exists, including local-only work that stops before committing or a repository that requires one combined commit; `pr_number` is null when no PR was created, distinguishing local-only work from a combined-commit PR. Consumers that ignore unknown keys are unaffected.
- **Never:** merges, describes the PR as reviewed, or closes or labels unrelated issues, including the related items its diagnosis found.

```text
Use cc-implement-issue for issue 123 and open a pull request.
Use cc-implement-issue for work item ENG-123 with issue_provider=plane code_host=bitbucket repository=workspace/repo.
```

### `cc-initial-review`

The first complete review of a change request.

- **Input:** a change-request ID. Optional: code host, `lang=`, `--json` / "with the structured result".
- **Does:**
  1. opens a review run (`CCR-…` line) and reviews the **accumulated diff from the merge base**, never just the last commit;
  2. runs `cc-pr-review`, [security triage](review-cycle.md#security-triage) (and `cc-security-review` when triggered), and `cc-verify` on actionable findings;
  3. inspects CI;
  4. publishes **one** consolidated comment.
- **Statuses:** `APPROVED` (full head reviewed, required checks passed, nothing blocking), `CHANGES_REQUESTED`, `BLOCKED`, `FAILED`.
- **Never:** modifies code.

```text
Use cc-initial-review on pull request 456.
Use cc-initial-review on change request 42 in workspace/repo with code_host=bitbucket lang=es.
```

### `cc-resolve-comments`

Acts on review feedback.

- **Input:** a change-request ID. Recovers findings from the PR comments, or from an injected `ORCHESTRATION_RESULT`.
- **Does:**
  1. **triages every finding against one commit before editing anything**, and records frozen dispositions (`valid`, `debatable`, `incorrect`, `obsolete`, `needs_clarification`) under a `CCT-…` triage line;
  2. fixes valid findings with the smallest change, reproduces each original problem, and verifies each fix;
  3. then runs `cc-code-review` on the new diff, security triage again, and `cc-verify`;
  4. commits, pushes, replies in threads, and publishes a summary.
- **Statuses:** `RESOLVED`, `PARTIALLY_RESOLVED`, `BLOCKED`, `FAILED`.
- **Never:** declares approval, or changes code just to silence a review.

```text
Use cc-resolve-comments on pull request 456.
```

### `cc-rereview`

Reviews again after changes, keeping the finding history.

- **Input:** a change-request ID.
- **Does:**
  1. recovers previous findings and the previously reviewed SHA;
  2. reviews the **complete accumulated diff** again, with focus on what changed since;
  3. **reproduces** each finding claimed as fixed;
  4. classifies each previous finding as `resolved` (verified by execution), `still_open`, or `not_applicable`;
  5. reports new findings with new IDs after the highest existing one;
  6. repeats security triage and checks CI, then publishes an updated comment.
- **Statuses:** as `cc-initial-review`.

```text
Use cc-rereview on pull request 456.
```

### `cc-orchestrator`

Coordinates the whole cycle and never implements or judges anything itself.

- **Inputs:** `issue_id`, provider fields, `orchestration_mode` (`auto` · `single_agent` · `claude_codex`), `max_iterations` (default 6).
- **Does:**
  1. runs provider bootstrap;
  2. runs implement → review → (resolve → rereview)\*, passing structured state between stages;
  3. applies the iteration limit and the no-progress guard;
  4. validates the exit conditions on the code host.
- **Statuses:** `READY_FOR_MANUAL_MERGE`, `HUMAN_INTERVENTION`, `BLOCKED`, `FAILED`.

See [Workflows → cc-orchestrator](workflows.md#cc-orchestrator) and [Claude + Codex mode](workflows.md#claude--codex-mode).

### `cc-orca-orchestrator`

The same cycle as Orca Runs, Tasks, and Workers, plus paired-review calibration.

- **Inputs:** as `cc-orchestrator`, plus `implementer` (default `codex`), `reviewer` (default `claude`), `paired_review`, `campaign`.
- **Statuses:** as `cc-orchestrator`.

See [Workflows → cc-orca-orchestrator](workflows.md#cc-orca-orchestrator).

---

## Supporting skills

### `cc-pr-review`

Review criteria for a change request. It covers scope against the work item, correctness, regressions, data model, authorization, architecture, frontend, tests, and operations. It compares against the base branch and reconstructs intent from the issue and PR.

- **Severity:** P0 (data loss, exploitable critical vulnerability, outage) · P1 (probable bug, confirmed regression) · P2 (moderate) · P3 (minor). A calling cycle skill maps these to `critical|high|medium|low` and `blocks:yes|no`.
- **Verdict:** *Approve*, *Approve with observations*, *Request changes*, or *Block*.
- **Publishes:** only when invoked directly.

```text
Use cc-pr-review on pull request 456.
```

### `cc-code-review`

The same kind of review for a diff, branch, or working tree, without the PR framing. `cc-resolve-comments` uses it after applying fixes.

```text
Use cc-code-review on the current working tree against main.
```

### `cc-security-review`

A security audit scoped to the change and its trust boundaries. It covers authentication, authorization, input/output handling, web surface (CORS, cookies, headers), data handling, dependencies, and configuration. Confirmed vulnerabilities are reported separately from potential risks, and a secret's location is reported without its value. It also states what it didn't review.

```text
Use cc-security-review on the changes in this branch.
```

When it runs inside the cycle is decided by [security triage](review-cycle.md#security-triage).

### `cc-verify`

Proves that a change works by running it:

1. static checks the project defines;
2. the tests nearest the change, then wider suites;
3. for UI changes, a browser walk-through: console, network, two viewports, and every state;
4. for API changes, real requests, checking status codes, payloads, errors, and auth.

It reports what ran, what passed, what failed, and what couldn't be verified and why. It stops and asks when configuration, services, ports, migrations, or permissions are missing. See [Verification](verification.md).

```text
Use cc-verify to check that the fix in src/api/orders.ts works.
```

### `cc-run`

Detects how a project starts and brings its services up in dependency order: databases → API → workers → frontend. It waits for each one to respond, then reports process, PID, port, URL, and stop command. It never switches ports silently, never kills a process it didn't start, and asks before installing dependencies or running migrations.

```text
Use cc-run to bring this project up and tell me the URLs.
```

### `cc-provider-bootstrap`

Resolves `issue_provider`, `issue_id`, `code_host`, and `repository` from explicit values, then `.code-cycle.yml`, then safe inference. It asks one grouped question for what is missing. Before writing `.code-cycle.yml`, it shows the proposed file and asks for confirmation. When `review.trusted_authors` is absent, the proposal includes the authenticated code-host login. It runs read-only access checks per provider and caches them for 7 days, then returns `PROVIDER_BOOTSTRAP_RESULT` with status `READY`, `BLOCKED`, or `FAILED`. See [Providers](provider-contract.md#setting-up-each-provider).

```text
Use cc-provider-bootstrap for issue 123.
```

### `cc-stats`

Reports this repository's local telemetry. It covers cycles, stages, first-pass approval, verdicts, findings, fallbacks, dispatch blockages, model drift, duration, and Jev comparisons, each with its sample size. It needs the runtime and `repository.selector`. See [Telemetry](telemetry.md#reading-it-with-cc-stats).

```text
Use cc-stats.
Show me the toolkit stats for the last 7 days.
/cc-stats all time
```

### `cc-profile-config`

Shows the repository's effective routing profiles: each profile's roles, primary and fallback marked as configured or default, relative cost, how its read-only roles are guaranteed (`enforced` for Codex, `detected` for Claude), and which executors are installed and authenticated. It then offers presets — `balanced-openai-implements`, `balanced-anthropic-implements`, `defaults`, `single-openai`, `single-anthropic` — or custom targets. It validates the choice, including Codex models and efforts against Codex's local models cache, shows the exact `code_cycle.profiles` block, and writes it only after confirmation, leaving every other key and comment in `.code-cycle.yml` as it was. It needs the runtime. See [Routing → Changing profiles](routing.md#changing-profiles).

```text
Use cc-profile-config.
Use cc-profile-config with preset=balanced-openai-implements.
Show me which models this repository uses for each role.
```

---

[← Workflows](workflows.md) · [↑ Documentation index](README.md) · [Review lifecycle →](review-cycle.md)
