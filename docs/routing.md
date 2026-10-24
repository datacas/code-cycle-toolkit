# Routing and models

How a stage becomes a concrete executor, model, and effort, and what the router deliberately doesn't do.

Routing applies when the **runtime** drives stages: `run_cycle.py`, and `cc-orchestrator` when it goes through the runtime's recorder. A skill you invoke by hand runs in whatever agent and model you are using.

**On this page:** [Overview](#overview) · [Profiles](#profiles) · [Targets and executors](#targets-and-executors) · [Role → profile rules](#role--profile-rules) · [Availability and fallback](#availability-and-fallback) · [Workspace policy](#workspace-policy) · [Publication boundary](#publication-boundary) · [Strategies](#routing-strategies) · [Jev](#jev-shadow-mode) · [Calibration](#calibration) · [What is recorded](#what-is-recorded) · [Updating models](#model-update-policy)

## Overview

```text
stage role ──► profile ──────────► target ─────────────────────► dispatch
 (implement)   (rule_selector:      (profiles.<name>.primary,     (codex exec / claude -p / orca,
               cheap_coder)          or .fallback if the primary    with the role's workspace
                                     is unavailable/ineligible)     policy)
```

1. **Role → profile.** Two declared rules escalate. Everything else keeps its default profile.
2. **Profile → target.** From `code_cycle.profiles` in `.code-cycle.yml`, layered over the built-in defaults.
3. **Gates.** Executor availability and the role's workspace policy decide whether the primary is eligible, or whether the fallback is used.
4. **Dispatch** runs the target non-interactively and records the result.

## Profiles

A profile names a *kind* of work. It never appears in a skill as a model name.

| Profile | Used for | Default primary | Default fallback |
|---|---|---|---|
| `cheap_coder` | implement, resolve (difficulty 1–2) | `codex:openai/gpt-6-luna high` | `claude:anthropic/claude-sonnet-5 high` |
| `deep_coder` | implement, resolve (difficulty 3) | `codex:openai/gpt-6-luna max` | `claude:anthropic/claude-sonnet-5 high` |
| `reviewer` | review, rereview | `codex:openai/gpt-6-sol high` | — (none, on purpose) |
| `senior_reviewer` | review, rereview of security-sensitive changes | `codex:openai/gpt-6-sol max` | — (none, on purpose) |
| `security` | security audit | `codex:openai/gpt-6-sol high` | `claude:anthropic/claude-opus-5-5 high` ¹ |
| `coordinator` | coordination | `codex:openai/gpt-6-luna medium` | `claude:anthropic/claude-sonnet-5 low` |
| `auxiliary_tool` | verify, run, bootstrap | `codex:openai/gpt-6-luna medium` | — |
| `cheap_tool` | compatibility for direct/custom callers; no built-in role selects it | `claude:anthropic/claude-haiku-4-5-20251001 low` | — |

¹ The security profile retains this configured fallback. Claude runs read-only stages in a disposable clone that the harness watches for changes, as described below.

The source of truth is `DEFAULT_PROFILES` in [`scripts/router.py`](../scripts/router.py). Override only what you want to change, and the rest keep their defaults:

```yaml
code_cycle:
  profiles:
    cheap_coder:
      primary: "codex:openai/gpt-6-luna high"
      fallback: "claude:anthropic/claude-sonnet-5 high"
    deep_coder:
      primary: "claude:anthropic/claude-opus-5-5 high"
```

An unknown profile name, or a `primary`/`fallback` that isn't a target string, stops the run before anything is dispatched.

### Changing profiles

[`cc-profile-config`](skills.md#cc-profile-config) shows the effective profiles and writes this block for you, only after you confirm it. Without an agent, the runtime component does the same:

```bash
python3 ~/.code-cycle/runtime/profile_config.py show                  # effective profiles, config vs default
python3 ~/.code-cycle/runtime/profile_config.py propose --preset balanced-openai-implements
python3 ~/.code-cycle/runtime/profile_config.py write --preset balanced-openai-implements --confirmed
```

| Preset | Implementation (`cheap_coder`, `deep_coder`) | Review (`reviewer`, `senior_reviewer`, `security`) |
|---|---|---|
| `balanced-openai-implements` | Codex `gpt-6-luna high` / `max` | Claude `claude-sonnet-5 high` / `claude-opus-5-5 high` |
| `balanced-anthropic-implements` | Claude `claude-sonnet-5 high` / `claude-opus-5-5 high` | Codex `gpt-6-sol high` / `max` |
| `defaults` | as `DEFAULT_PROFILES` (removes the block) | as `DEFAULT_PROFILES` |
| `single-openai` | Codex | Codex |
| `single-anthropic` | Claude | Claude |

The **balanced** presets keep the vendor that implements from reviewing its own work, fallbacks included, and declare no fallback on either side: availability belongs to the executor, so a same-executor fallback is down whenever its primary is, and a fallback on the other executor would cross the split. A crossing target is refused unless `--allow-cross-split` is passed. `auxiliary_tool` (verify, run, bootstrap) stays on Codex in every preset, because only Codex can confine a disposable workspace.

`--set <profile>.primary=<target>` and `--set <profile>.fallback=<target|none>` adjust any preset or the current profiles. Codex models and efforts are checked against Codex's local models cache (`$CODEX_HOME/models_cache.json`); Claude publishes no local list, so its targets are reported as unchecked. `write` changes only the profiles block, keeps every other key and comment, declares only what differs from the defaults, and refuses a proposal with errors.

## Targets and executors

A target is one string: **`executor:provider/model effort`**.

| Executor | Runs | Can prove | Reports the model it ran? |
|---|---|---|---|
| `codex` | `codex exec -m <model> -c model_reasoning_effort=<effort> --json` | `authenticated` (a credential file exists) | No. It is recorded as `?` / `unreported`. |
| `claude` | `claude -p … --model <model> --effort <effort> --output-format json` | `authenticated` (onboarding completed) | Yes (`modelUsage`) |
| `orca` | an Orca worker for an existing Run and Task | `ready` (the runtime reports its state) | Yes (`launch.effective`) |

- The toolkit passes `model` and `effort` through as written, and the executor's CLI accepts or rejects them. There is no model allowlist for routing.
- Telemetry does keep a closed set of *model names* it will store (defaults plus your declared profiles), so a credential can't be recorded by mistake.
- Every run is non-interactive and uses no bypass flags. A trust dialog, hook review, login prompt, or bypass acknowledgement returns `BLOCKED` naming that capability, so a person can grant it once. The adapter never answers a security prompt itself.

## Role → profile rules

`rule_selector` is the default and only selector. It uses signals declared **before** routing (`--difficulty`, `--verifiability`, `--security-sensitive`):

| Role | Candidates | Rule |
|---|---|---|
| `implement`, `resolve` | `cheap_coder`, `deep_coder` | difficulty 3 → `deep_coder`, otherwise `cheap_coder` |
| `review`, `rereview` | `reviewer`, `senior_reviewer` | security-sensitive → `senior_reviewer`, otherwise `reviewer` |
| `issue_review` | `reviewer`, `senior_reviewer` | security-sensitive or difficulty 3 → `senior_reviewer`, otherwise `reviewer` |
| `security` | `security` | always |
| `coordinate` | `coordinator` | always |
| `verify`, `run`, `bootstrap` | `auxiliary_tool` | always |

One escalation is the runtime's, not a selector's: an issue review whose `READY` it cannot confirm is routed once more with `escalation_selector`, which names `senior_reviewer` for `issue_review` and defers to `rule_selector` for every other role. The escalated dispatch row carries `escalated: true`. The stage is skipped only for a declared trivial, non-sensitive task; see [Issue review before implementation](review-cycle.md#issue-review-before-implementation).

There are no finer rules. The calibration campaigns so far produced one usable finding: an inexpensive implementer never passed review on the first attempt across five real work items. That isn't evidence enough for more rules. A selector can only return a name from the role's candidates. Availability, workspace policy, fallback, and dispatch stay outside it.

Resolve keeps the `cheap_coder` / `deep_coder` profile choice. With `code_cycle.resolve.effort` unset, the runtime pins the primary target to `high`; after a finding survives one claimed fix and remains open, the next resolve uses `max` on the target the previous resolve actually used. An explicit `code_cycle.resolve.effort` pins the primary effort for every round and disables automatic escalation. A configured fallback keeps its declared effort. If the local Codex model catalog confirms the requested effort is unsupported, the runtime uses the fallback without attempting that effort; an unlisted model or missing catalog is unknown and is dispatched. A CLI rejection is classified as `unsupported_effort` only when the effort keyword sits next to the rejection phrase and no auth, transport, capacity or quota marker is present; the fallback keeps its own effort, and the reroute follows the same start-state and reconciliation guard as other failures. The escalation does not change profile selection, and a second survival still stops before another resolution.

## Availability and fallback

- **Probe once per run.** An executor that is only *installed* isn't dispatchable. Codex and Claude can only prove `authenticated`: remaining quota and session validity are invisible until something is spent. Production uses the `attempt` readiness policy to dispatch from `authenticated`, and records that it did (`dispatched_from`).
- **Fallback (production only).** If the primary is unavailable or not eligible for the role's workspace policy, the profile's `fallback` is used, and the decision says so.
- **Sandbox recovery.** An explicit, complete agent-reported restriction can use the same reroute machinery. See [Recovering sandbox restrictions](#recovering-sandbox-restrictions) for its additional conditions.
- **One reroute per stage.** A `capacity`, `transport`, `timeout`, or `quota` failure can use that profile's distinct, available fallback once, sharing the quota retry budget. The previous process group must have exited; asynchronous launches and missing exit proof stop. A `not_started` attempt can reroute directly. A `started` or `unknown` attempt that could write or publish first reconciles the workspace, local HEAD, remote refs, PR state, comments and reviews, and issue comments. Dirty or changed state, a publication, or unavailable evidence stops with a closed reason token. A publication is what the cycle's own code-host account (`gh api user`) created or edited: pull requests (state, head, title, body), comments and reviews. Other accounts' comments and newly opened pull requests, reactions and timestamps do not stop the reroute; an unreadable account is unavailable evidence. A pull request carries no editor identity, so any edit to an existing in-scope pull request stops conservatively, whoever opened it. A review's read-only workspace does not exempt its publication boundary. Other error classes and human friction never reroute. Both attempts and the parent's `error_code` and `start_state` are recorded. The fallback applies only to this stage; later stages keep the original executor availability. Recovery from published evidence remains a separate operation.

Native POSIX dispatches run in their own process group and stop remaining children before returning exit proof, including on timeout. The group first receives `SIGTERM`, so a sandbox can unmount and remove what it set up, then `SIGKILL` after a bounded grace period (`PROCESS_GROUP_GRACE_SECONDS`, 10 seconds); output pipes a stray descendant keeps open are waited on for at most `OUTPUT_DRAIN_SECONDS`, so cleanup never blocks the cycle indefinitely. Windows currently lacks group exit proof and therefore stops rather than automatically rerouting. Reconciliation reads GitHub through `gh`; unsupported publication boundaries fail closed. Snapshots are transient and never enter telemetry.

- **Model mismatch.** If the executor reports running a different model than requested, that is a `contract_violation`, and the cycle doesn't advance on it.
- **Calibration never falls back.** Substituting an arm would answer a different question with the same sample, so it blocks and waits instead.

### Recovering sandbox restrictions

Every runtime stage receives environment recovery guidance. The agent first
diagnoses the failing command and tries recovery through already-authorized
capabilities, including approved command escalation when the host offers it.
It then reruns the original check. A sandbox connection failure is not proof
that the host service is unavailable, and verification is never waived.

If the executor's restriction persists, the stage can return `BLOCKED` with
an `environment_blocker`: `kind` (`sandbox_local_access`,
`sandbox_network_access`, or `sandbox_filesystem_access`), the failing
`command`, observed `error`, a nonempty `attempted` list, and
`requires_human: false`. The delegated verifier's diagnosis is forwarded into
the stage result. Ordinary blockers, missing credentials, approval denials,
and reports containing human questions do not qualify.

For other restrictions, the runtime records `environment_restricted` and keeps
the selected executor. A registered resource requests consent on that executor;
there is no sandbox-driven fallback. Only real executor unavailability
(`capacity`, `transport`, `timeout`, `quota`) uses the availability reroute gate.
Checks that remain impossible are `not_verified`; independent completed work
can continue. An essential blocked operation still stops the cycle. A grant
permits a full guarded new attempt, never replaying published or changed work.

Detection preserves the command and observed error. Claude hook denials and
Codex rollout command/result pairs provide runtime-observed evidence; Codex
reads only the session identified by this invocation, including successful CLI
exits whose functional result is `BLOCKED`. Assistant prose alone is not that
signal. Sandbox-specific diagnostics (such as `SandboxDenied` or a proxy's
domain allowlist denial) qualify; missing binaries, invalid credentials,
ordinary filesystem ownership errors and connection refused alone do not.
Agent-reported structured blockers remain diagnostic hints, never grants.
When a stage hit several denials, the first observed one a registered grant can
lift is the restriction; otherwise the agent's blocker when a grant matches it,
and otherwise the first observed denial. A denied local socket counts as local
access even when its message mentions the network.

A Codex stage cannot run the installed Codex CLI's own sandbox (#268). Codex's
Linux sandbox overmounts its app-server socket directory,
`/tmp/codex-daemon-<uid>`, with a read-only mode-000 tmpfs for the commands it
runs, so a nested `codex sandbox` or `codex exec` fails with "app-server socket
directory must be a user-owned directory with mode 0700". The runtime's
profile and per-dispatch `TMPDIR` do not cause this, and no `TMPDIR`,
`CODEX_HOME` or `--no-daemon` change avoids it; the stage itself still starts
and runs. The guidance tells the stage to record the affected commands as
`not_verified` and continue independent work. This is a known limitation of a Codex sandbox
hosting another Codex sandbox, not a host socket-directory prerequisite. The
runtime records affected checks as `not_verified` with evidence and keeps the
selected executor. Nothing widens the sandbox for it: the Unix socket grant
stays person-only (#239).

Native dispatches set `CODE_CYCLE_STAGE_EXECUTOR` for their child process. A
runtime launched inside a Codex stage uses that marker to classify the nested
Codex limit deterministically and skips native preparation, without inspecting
or repairing any host socket directory. A host-side Codex review is not marked
nested merely because its executor is Codex.

Known missing `srt`, `rg`, `bwrap`, `socat`, the nested socket error and native
isolation startup errors are intercepted before the general reroute path.
They keep the selected executor. Completed main work reports its completed
functional status and `tests.conclusion: not_verified`; independent stages may
continue. A residual `BLOCKED` is **not** promoted merely because a commit or PR
exists: incomplete work and a missing safe static boundary still stop. The
existing conservative closing gate stays unchanged until #290; pending
verification cannot establish `READY_FOR_MANUAL_MERGE`. This does not remove
the general `environment_restricted` reroute (#289), or change the #268 guard
against blindly redispatching a stage that already published.

Interception matches missing executable diagnostics and named sandbox startup
failures. Tool-name substrings in unrelated paths (`target`, `cargo`,
`org.gradle`), or a working tool's permission error on project data, retain
the existing general reroute behavior.

Nested executor behavior depends on the sandbox hosting the runtime, and is not
assumed compatible without a successful functional probe. A probe failure is
recorded as unverified; it does not establish that another executor will work.

| Host sandbox running the runtime | Nested Codex CLI | Nested Claude CLI |
|---|---|---|
| Codex | Known-limited: its app-server socket is masked | Unverified |
| Claude | Unverified | Unverified |
| Other sandboxed host | Unverified | Unverified |
| Unsandboxed host | Unverified until probed | Unverified until probed |

Do not launch `codex sandbox`, `codex exec` or `srt` inside a stage when its
native sandbox cannot support the nested check. Keep checks that cannot run as
`not_verified`; never disable confinement or count missing evidence as a pass.

## Workspace policy

Each role has a fixed write permission. Only an executor that can **enforce** it is eligible:

| Role | Policy | Eligible executors |
|---|---|---|
| `implement`, `resolve` | `workspace_write` | Codex (`-s workspace-write`), Claude (`acceptEdits`), Orca |
| `review`, `rereview`, `issue_review`, `security`, `bootstrap`, `coordinate` | `read_only` | Codex (profile extending `:read-only`, writable scratch directory), Claude (disposable clone, writes detected, not prevented), Orca (explicit review workspace) |
| `verify`, `run` | `disposable` | Codex, confined to a disposable workspace |

Codex records `read_only_mode = enforced` only when its sandbox was used *and* the checkout was verified unchanged afterwards. The sandbox confines the agent's own commands; tools the host configures, such as MCP servers, plugins, and hooks, run outside it. So the harness fingerprints the dispatch checkout before and after every `read_only` stage on every executor, and any change is a `contract_violation`, whatever wrote it. Claude records `read_only_mode = detected`: the harness creates an independent detached clone with its own Git object store at the reviewed HEAD, removes its remotes, and discards local edits. The harness requires a clean implementer checkout and checks its HEAD, working-tree fingerprint, and configured remote refs before and after review; a detected change fails the stage as a contract violation. Claude runs with its full tooling and its ordinary `gh` and `git` access, so it can publish its own comment. With Claude, read-only is watched, not enforced. The harness detects a write it can observe, but it cannot prevent or undo a remote write: a push, a merge, or a change to the change request. A push that is later restored before the stage ends also goes unseen. The maintainer has accepted this risk. Anyone who needs a full guarantee should review with Codex or give the reviewer read-only credentials. Publication access remains role-specific, and a reviewer may publish only the comment its role permits. Details are in [Role workspace policy](role-workspace-policy.md).

## Publication boundary

All native stages receive and clean up an isolated, writable per-dispatch
`TMPDIR` outside the checkout, with the prefix `code-cycle-tmp-` and fixed helper
names (`pr-comment.md`, `report.md`, and `<check>.log`). Orca's asynchronous workers keep their existing temporary-directory
behaviour. See [Role workspace policy](role-workspace-policy.md).

What each stage may publish:

| Stage | May |
|---|---|
| `implement` | comment, create the change request, push its working branch |
| `resolve` | comment, push its working branch |
| `review`, `rereview` | comment |
| security, bootstrap, verification, run | nothing |
| any stage with `--local-only` | nothing |

No stage may merge, force-push, delete remote refs, modify the base branch, close an issue or PR without an explicit instruction, or publish for another stage.

> [!WARNING]
> **On a GitHub cycle, native Claude stages enforce this boundary for direct Bash invocations; nothing else does.** Each Claude stage receives its command table and a hook that guards direct Git and code-host commands, including force-push variants and pushes to the base branch ([Host permissions](host-permissions.md)). Explicit `user_choices` can extend the table; selecting a wrapper, interpreter, arbitrary-code command or integration trusts its nested operations, which the hook does not inspect. Codex stages are confined by their sandbox profile, which filters files and network, not commands. With another code host or issue provider, on Orca workers, and for non-Bash tools such as MCP servers, agents keep their normal access and the rule is behavioural: it is stated in each stage's prompt, and the published comments and telemetry let you audit it. Use a separate credential or repository permissions when you need a hard guarantee.

Before a publishing dispatch, the runtime checks readiness: for a GitHub `origin`, `gh` authentication and repository permission, plus a `git push --dry-run` against a temporary ref, which it doesn't create. A publishing Codex stage keeps its filesystem sandbox and gets network access only to GitHub and Bitbucket hosts, through a permission profile (Codex CLI ≥ 0.138.0). A publishing Claude stage is additionally allowed its stage's `gh` and `git` subcommands on a GitHub cycle, or `gh` and `git` as a whole elsewhere. Orca, and any adapter that hasn't declared how it grants publication access, fails closed for publishing stages. Failures are recorded as `missing_capability=publication_access` before the model starts.

### Sensitive sandbox grants

Some checks need a resource the sandbox blocks on purpose, such as the host Docker daemon. A stage never widens its own sandbox. It reports the restriction as an `environment_blocker`, with `"resource": "docker_host"` for Docker, and a person decides. The registry in `scripts/grants.py` holds each grant, and new resources reuse the same flow.

- **The real scope is what the person is asked about.** Codex on Linux cannot allow the Docker socket alone. Its sandbox blocks every new `AF_UNIX` socket unless all of them are allowed, through the network proxy (`network.dangerously_allow_all_unix_sockets`), so the question says the grant also opens ssh-agent, D-Bus and every other local socket. Docker itself acts as root on the host. A filesystem rule on the socket path would not help: Codex's Linux helper refuses to start with one. Verified with Codex CLI 0.162.1 and `codex sandbox`.
- **Chosen executor.** Request the missing resource on the selected executor. An answer given in advance, a stored answer, or project configuration is loaded before its first dispatch. The grant goes only to an executor with a registered scope: Linux today; macOS and Windows have none. Codex's scope opens every local Unix socket. Claude adds only `docker compose up`, `down`, `ps`, `logs` and `build` to the dispatch's stage table; `docker run`, `exec`, `compose run` and other subcommands remain denied. On a non-GitHub cycle without the command table, Claude's grant changes nothing. Publication domains, review read-only checkouts and separately selected registry domains retain their own policies. The Docker grant supplies no internet access by itself.
- **Three answers.**
  - *This cycle*: lives only in the process, for the authorized executor/platform scope. It is offered only where the executor applies the grant per dispatch. An explicit `--grant` answer applies to each registered executor scope the run uses. A `persistent` or `deny` answer given with `--grant` is stored only for the exact scope fingerprint whose question an earlier run of the same work item recorded in the cycle journal. Every other scope, a scope that changed since its question, and an answer given before any question hold for this run only, as source `--grant (this run)`, with a note in the run's report.
  - *Persistent*: kept per user outside checkouts in `$XDG_CONFIG_HOME/code-cycle/consents.json` (or `CODE_CYCLE_CONSENT_FILE`), per repository and exact executor/platform/scope fingerprint. It reaches retries, resumes and later issues before dispatch. A changed scope is UNKNOWN and needs a new answer. Schema 2 keeps separate fingerprints so executor scopes cannot overwrite each other; schema-1 allows are read without widening and migrate on the next write. Older runtimes cannot read schema 2 and fail closed.
  - *Deny*: stored as DENIED for that exact scope until revoked. No repeat request while it stands. The stage runs without the permission, with a reason and the exact-HEAD CI alternative; local checks needing it remain `not_verified`. The runtime prevents a denial from becoming a reported verification pass. Independent work continues; an essential denied operation returns `BLOCKED` and the cycle stops with `HUMAN_INTERVENTION`. A stage that already ran with the denial is not rerun for it.
- **Asking.** On a terminal, `run_cycle.py` asks once and continues. A run nobody watches, detached or driven by an orchestrator, never widens without an answer given in advance. It stops with `HUMAN_INTERVENTION` and `stop_reason=permission_required`, and the decisions record carries the question in the `questions` shape plus the full scope and risks. Answer by rerunning the stopped stage with `--grant docker_host=cycle|persistent|deny`.
- **Safe reruns.** Every full rerun after an answer (`cycle`, `persistent` or `deny`) passes the same exit and effect guard as an executor fallback, even on the same executor. Publication, remote or workspace changes, any dirty workspace (including one already dirty before the attempt), unavailable evidence, an unproven exit or a human-action requirement stop the rerun. The answer does not authorize repeating a stage that already had effects. `permission_answered` records an allowed rerun; a stopped rerun records the guard's closed reconciliation reason instead. Continuing only the pending operation is a separate capability.
- **Authorization in advance.** `code_cycle.docker.host_socket_access: true` in `.code-cycle.yml` authorizes `docker_host`, in both its Codex and Claude scopes, for every run of the project without asking. The default is `false`.

**Control and revocation.** `ConsentStore.list()` reports current GRANTED and
DENIED decisions with scope and source. `revoke()` removes the stored decision
to UNKNOWN and appends a separate revocation audit record in the same file;
revoking a denial never grants access. The broker rereads the store before
every dispatch, even within a running cycle; operations already started are
not interrupted. No stored GRANTED cache survives between dispatches. Explicit
run answers are consumed once per scope, so revocation does not reapply them.
Configuration is an independent source: revoke it by editing configuration.
The effective listing shows it even after a stored consent is revoked.

```bash
python3 scripts/consent_control.py list --repo owner/repo --executor codex
python3 scripts/consent_control.py revoke --repo owner/repo --executor codex --grant-id docker_host
```

Use `--executor claude` for its scope, `--platform linux` to manage a different
platform, and `--config path/to/.code-cycle.yml` for explicit configuration.
The CLI changes only this user's consent store and prints the resulting
effective decisions. Configuration grants are included only when the configured
repository matches `--repo`. An unreadable store authorizes nothing.

**Authority limits.** The toolkit can reuse explicit `user_choices` and registered
grants in its per-dispatch command tables and native sandbox profiles. It cannot
approve host trust, login or security prompts, create credentials, grant
unsupported platform scopes, repair nested Codex socket isolation, or run code
unconfined. Interactive host rules (`cc-host-permissions`, #267) do not authorize
cycle dispatches. User tool choices and dependency preparation (#279) keep their
existing configuration mechanism; this is the sensitive-grant lifecycle, not a
second approval store. Host-run authorization (#291) is separate and binds the
exact command, repository, commit SHA and isolation context, never a new commit.
No host-run authority is introduced here, and no global scope is inferred from
a repository decision.

When the code host is GitHub and `gh` is authenticated, publishing stages use `gh` for every read and write on the change request. A GitHub connector or MCP tool is used only when `gh` is unavailable. If a GitHub publication attempt returns HTTP 403 or 404 through another tool, the stage retries once with `gh` before reporting `BLOCKED` and names the failed tool. Bitbucket continues to use its configured tooling.

## Routing strategies

```yaml
code_cycle:
  routing:
    strategy: fixed      # fixed (default) | measured
```

| Strategy | Target selection | Cost estimate |
|---|---|---|
| `fixed` | declared primary, then fallback | assumes a correction round always happens (`DEFAULT_FIRST_PASS_RATE = 0.0`) |
| `measured` | **same as `fixed`** | uses this repository's measured first-pass rate once it has ≥ 10 observations. Below that, it keeps the conservative default and records that the rate is unknown. |

**Neither strategy changes which model runs.** `measured` only changes the recorded cycle cost estimate. That estimate uses relative weights per profile (`PROFILE_COST`), not real cost.

## Jev shadow mode

[TypeSafe](https://docs.typesafe.ai/)'s Jev can be asked, for `implement` and `resolve` stages, which of `cheap_coder` or `deep_coder` it would choose. **It is an observer only.** Its answer is recorded next to the rules' choice and never selects a profile, target, fallback, or dispatch.

```yaml
code_cycle:
  routing:
    jev:
      mode: shadow           # disabled (default) | shadow
      model: jev-latest      # or a concrete version such as jev-1.13.0
      timeout_seconds: 3     # > 0, at most 10
```

| | |
|---|---|
| Key | `TYPESAFE_API_KEY` (legacy fallback `JEV_API_KEY`), read at call time, sent only in the `Authorization` header, never stored |
| Sent | one `POST https://api.typesafe.ai/v1/systemone` per eligible stage (not configurable): the role plus pre-routing counts, flags, and closed tokens. No prose, paths, diffs, repository name, work-item ID, or outcome. |
| Disabled | no adapter is built, no key is read, no connection is opened |
| Failure | recorded as a category (`unavailable`, `timeout`, `rate_limited`, `http_error`, `invalid_response`). It never stops or changes the stage. |
| Compatibility | the legacy model value `typesafe-ai/jev` is normalised to `jev-latest` |

`cc-stats` compares Jev's suggestions with the rules' choices and with outcomes. The full protocol is in [Instrumentation → Shadow suggestions](instrumentation.md#shadow-suggestions).

## Calibration

A calibration compares arms on identical input, so it follows stricter rules than production:

- It is entered **only** explicitly: `run_cycle.py --mode calibration`, or `cc-orca-orchestrator … paired_review=true campaign=<id>`. There is no `calibration.enabled` flag, and configuration alone never starts one.
- It never falls back and never reroutes. It needs **proven** readiness, which only Orca can demonstrate, so Codex and Claude arms block under `--mode calibration`.
- `--mode calibration` takes its arms from the ordinary `profiles`.
- **Paired review** (Orca) takes its two reviewers from `calibration.profiles`:

```yaml
code_cycle:
  calibration:
    profiles:
      reviewer_a: { provider: anthropic, model: claude-sonnet-5, effort: high }
      reviewer_b: { provider: openai,    model: gpt-6-sol,       effort: high }
```

The reviewers are isolated (separate worktrees at the same SHA, or strictly sequential), don't publish, and are verified not to have moved the head. Their findings are merged blind before triage. The attribution map lives outside every repository (`scripts/calibration_store.py`) until a human has matched root causes. See [Instrumentation → Experimental: paired review](instrumentation.md#experimental-paired-review).

## What is recorded

Each dispatch row records the role, profile, executor, provider, requested model, resolved model and its `model_resolution` (`matched`, `mismatch_known`, `mismatch_unrecognized`, `unreported`), effort, and whether a fallback was used. It also records the readiness policy and state, any `missing_capability`, the strategy and cost inputs, the pre-routing signals, and the duration. See [Telemetry](telemetry.md).

## Model-update policy

`DEFAULT_PROFILES` are defaults for new installations, not a list of allowed models.

**Configuration only (no toolkit release):**

- switching to another model the same executor and provider already support;
- changing effort to one the executor supports;
- changing `primary` or `fallback` on any profile;
- trying a candidate model.

**May need a toolkit release:**

- a new executor or provider;
- a changed CLI or output format;
- new authentication;
- new workspace or sandbox guarantees;
- a new capability type (`missing_capability` is a closed set);
- an effort level an adapter doesn't understand;
- a change to the dispatch contract.

---

[← Verification](verification.md) · [↑ Documentation index](README.md) · [Configuration →](configuration.md)
