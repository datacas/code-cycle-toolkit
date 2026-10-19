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
- **One reroute per stage.** A `capacity`, `transport`, `timeout`, or `quota` failure can use that profile's distinct, available fallback once, sharing the quota retry budget. The previous process group must have exited; asynchronous launches and missing exit proof stop. A `not_started` attempt can reroute directly. A `started` or `unknown` attempt that could write or publish first reconciles the workspace, local HEAD, remote refs, PR state, comments and reviews, and issue comments. Dirty or changed state, a publication, or unavailable evidence stops with a closed reason token. A publication is what the cycle's own code-host account (`gh api user`) created or edited: its pull requests (state, head, title, body), comments and reviews. Other accounts' comments and pull requests, reactions and timestamps do not stop the reroute; an unreadable account is unavailable evidence. A pull request's title and body carry no editor, so any edit to the cycle's own pull request stops conservatively. A review's read-only workspace does not exempt its publication boundary. Other error classes and human friction never reroute. Both attempts and the parent's `error_code` and `start_state` are recorded. The fallback applies only to this stage; later stages keep the original executor availability. Recovery from published evidence remains a separate operation.

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

The runtime records the attempt as `environment_restricted` and can use the
profile's configured fallback on a different executor. It passes the diagnosis
to that executor and retains the same workspace and publication contracts.
This shares the existing one-reroute budget and requires process exit proof
and unchanged, clean effect snapshots for stages that can write or publish.
It never rewrites project configuration, grants additional permissions,
disables the sandbox, substitutes an unconfigured model, or repeats the same
sandbox with a different model. Missing fallback, unknown or changed effects,
calibration mode, or a second failure still stops with a recorded reason.

A Codex stage cannot run the installed Codex CLI's own sandbox (#268). Codex's
Linux sandbox overmounts its app-server socket directory,
`/tmp/codex-daemon-<uid>`, with a read-only mode-000 tmpfs for the commands it
runs, so a nested `codex sandbox` or `codex exec` fails with "app-server socket
directory must be a user-owned directory with mode 0700". The runtime's
profile and per-dispatch `TMPDIR` do not cause this, and no `TMPDIR`,
`CODEX_HOME` or `--no-daemon` change avoids it; the stage itself still starts
and runs. The guidance tells the stage to report it once as
`sandbox_filesystem_access`. The runtime's own pre-dispatch Codex probes run
where the runtime runs, outside the stage's sandbox, and are unaffected unless
the runtime itself was started inside a Codex sandbox. Nothing widens the
sandbox for it: the Unix socket grant stays person-only (#239), and a check
that needs the installed Codex sandbox runs on the host or on an executor that
is not itself inside Codex.

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
> **On a GitHub cycle, native Claude stages enforce this boundary for Bash; nothing else does.** Each Claude stage receives only its closed command table and a hook that denies every other Bash command, including every force-push variant and pushes to the base branch ([Host permissions](host-permissions.md)). Codex stages are confined by their sandbox profile, which filters files and network, not commands. With another code host or issue provider, on Orca workers, and for non-Bash tools such as MCP servers, agents keep their normal access and the rule is behavioural: it is stated in each stage's prompt, and the published comments and telemetry let you audit it. Use a separate credential or repository permissions when you need a hard guarantee.

Before a publishing dispatch, the runtime checks readiness: for a GitHub `origin`, `gh` authentication and repository permission, plus a `git push --dry-run` against a temporary ref, which it doesn't create. A publishing Codex stage keeps its filesystem sandbox and gets network access only to GitHub and Bitbucket hosts, through a permission profile (Codex CLI ≥ 0.138.0). A publishing Claude stage is additionally allowed its stage's `gh` and `git` subcommands on a GitHub cycle, or `gh` and `git` as a whole elsewhere. Orca, and any adapter that hasn't declared how it grants publication access, fails closed for publishing stages. Failures are recorded as `missing_capability=publication_access` before the model starts.

### Sensitive sandbox grants

Some checks need a resource the sandbox blocks on purpose, such as the host Docker daemon. A stage never widens its own sandbox. It reports the restriction as an `environment_blocker`, with `"resource": "docker_host"` for Docker, and a person decides. The registry in `scripts/grants.py` holds each grant, and new resources reuse the same flow.

- **The real scope is what the person is asked about.** Codex on Linux cannot allow the Docker socket alone. Its sandbox blocks every new `AF_UNIX` socket unless all of them are allowed, through the network proxy (`network.dangerously_allow_all_unix_sockets`), so the question says the grant also opens ssh-agent, D-Bus and every other local socket. Docker itself acts as root on the host. A filesystem rule on the socket path would not help: Codex's Linux helper refuses to start with one. Verified with Codex CLI 0.162.1 and `codex sandbox`.
- **Least privilege.** A configured fallback executor that can recover the stage without a wider sandbox is tried first, as for any sandbox restriction; a person is asked only when none can. When the fallback hits the same restriction, the question goes back to the primary, which reruns with the answer; an answer given in advance, by `--grant`, a stored answer, or the project setting, applies the same way. The grant goes only to an executor with a scope for it, and only on platforms where its reach was verified: Linux today; macOS and Windows have no scope, so nothing is widened there. Codex's scope opens every local Unix socket. Claude's scope (#142) has its own text: it adds only `docker compose up`, `down`, `ps`, `logs` and `build` to the stage's command table for that dispatch, while `docker run`, `docker exec`, `docker compose run` and every other subcommand stay denied by the stage hook. A `docker compose` the hook denied in a Claude stage that then reports `BLOCKED` is the `docker_host` blocker that starts this flow. Without the command table, on a non-GitHub cycle, Claude's grant changes nothing. Publishing stages keep their GitHub and Bitbucket allowlist and review stages keep their read-only checkout. A non-publishing writer moves to a `:workspace` profile whose proxy allows no domain, so the grant gives it no internet access.
- **Three answers.**
  - *This cycle*: the grant lives only in the running process and applies to the cycle's remaining Codex dispatches. It is never written down, so another session never sees it. It is offered only where the executor applies the grant per dispatch.
  - *Persistent*: kept per user, outside every checkout, in `$XDG_CONFIG_HOME/code-cycle/consents.json` (or `CODE_CYCLE_CONSENT_FILE`), for this repository and this exact scope. When the scope changes, the stored answer no longer applies.
  - *Deny*: holds for this run only. The stage reruns without the grant, told to finish every check that does not need it and to report the rest as unverified.
- **Asking.** On a terminal, `run_cycle.py` asks once and continues. A run nobody watches, detached or driven by an orchestrator, never widens without an answer given in advance. It stops with `HUMAN_INTERVENTION` and `stop_reason=permission_required`, and the decisions record carries the question in the `questions` shape plus the full scope and risks. Answer by rerunning the stopped stage with `--grant docker_host=cycle|persistent|deny`.
- **Authorization in advance.** `code_cycle.docker.host_socket_access: true` in `.code-cycle.yml` authorizes `docker_host`, in both its Codex and Claude scopes, for every run of the project without asking. The default is `false`.

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
