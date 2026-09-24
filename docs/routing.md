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

¹ Kept in the registry for compatibility only. The security stage is read-only and Claude can't enforce read-only, so this fallback is never selected.

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
| `security` | `security` | always |
| `coordinate` | `coordinator` | always |
| `verify`, `run`, `bootstrap` | `auxiliary_tool` | always |

There are no finer rules. The calibration campaigns so far produced one usable finding: an inexpensive implementer never passed review on the first attempt across five real work items. That isn't evidence enough for more rules. A selector can only return a name from the role's candidates. Availability, workspace policy, fallback, and dispatch stay outside it.

## Availability and fallback

- **Probe once per run.** An executor that is only *installed* isn't dispatchable. Codex and Claude can only prove `authenticated`: remaining quota and session validity are invisible until something is spent. Production uses the `attempt` readiness policy to dispatch from `authenticated`, and records that it did (`dispatched_from`).
- **Fallback (production only).** If the primary is unavailable or not eligible for the role's workspace policy, the profile's `fallback` is used, and the decision says so.
- **One reroute.** If a dispatch discovers an exhausted quota window, availability is updated and the stage is routed **once** more. Both attempts are recorded. Friction that needs a person (trust dialog, login) never triggers a reroute.
- **Model mismatch.** If the executor reports running a different model than requested, that is a `contract_violation`, and the cycle doesn't advance on it.
- **Calibration never falls back.** Substituting an arm would answer a different question with the same sample, so it blocks and waits instead.

## Workspace policy

Each role has a fixed write permission. Only an executor that can **enforce** it is eligible:

| Role | Policy | Eligible executors |
|---|---|---|
| `implement`, `resolve` | `workspace_write` | Codex (`-s workspace-write`), Claude (`acceptEdits`), Orca |
| `review`, `rereview`, `security`, `bootstrap`, `coordinate` | `read_only` | Codex (`-s read-only`); Orca only with an explicit isolated review workspace |
| `verify`, `run` | `disposable` | Codex, confined to a disposable workspace |

Claude can't enforce read-only, so it is never used for review roles. That is why review profiles have **no fallback**: when Codex is unavailable, a review blocks instead of running without a proven non-mutating boundary. Details are in [Role workspace policy](role-workspace-policy.md).

## Publication boundary

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
> **This boundary is behavioural, not technical.** Agents keep their normal `gh`, `git`, MCP, and credential access. The rule is stated in each stage's prompt, and the published comments and telemetry let you audit it. Use a separate credential or repository permissions when you need a hard guarantee.

Before a publishing dispatch, the runtime checks readiness: for a GitHub `origin`, `gh` authentication and repository permission, plus a `git push --dry-run` against a temporary ref, which it doesn't create. A publishing Codex stage keeps its filesystem sandbox and gets network access only to GitHub and Bitbucket hosts, through a permission profile (Codex CLI ≥ 0.138.0). A publishing Claude stage is additionally allowed `gh` and `git`. Orca, and any adapter that hasn't declared how it grants publication access, fails closed for publishing stages. Failures are recorded as `missing_capability=publication_access` before the model starts.

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
