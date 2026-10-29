# Configuration

Every setting the toolkit reads, in one place.

**On this page:** [Precedence](#precedence) · [Full example](#full-example) · [`.code-cycle.yml` reference](#code-cycleyml-reference) · [Providers and repository](#providers-and-repository) · [Provider health cache](#provider-health-cache) · [Profiles and routing](#profiles-and-routing) · [Execution profiles](#execution-profiles) · [CI policy](#ci-policy) · [Review](#review) · [Security review rule](#security-review-rule) · [Orchestration](#orchestration) · [Calibration](#calibration) · [Invocation parameters](#invocation-parameters) · [Output language](#output-language) · [Environment variables](#environment-variables) · [Command-line flags](#command-line-flags) · [Local files](#local-files)

## Precedence

```text
explicit invocation value   (issue_provider=plane, max_iterations=4, lang=es, --repo …)
        ▼ overrides
.code-cycle.yml             (optional, non-secret, in the repository root)
        ▼ overrides
safe inference              (an unambiguous github.com / bitbucket.org origin remote)
        ▼ otherwise
built-in defaults, or BLOCKED when a provider or repository stays ambiguous
```

`run_cycle.py`, `profile_config.py`, and `stats.py` look for the file in `--cwd`; from a linked worktree that has none, they fall back to the main worktree root's file (found through the Git common directory). A file in the worktree wins. Each component reports the selected configuration path on stderr; `run_cycle.py` reports when it uses built-in defaults, and the profile and stats commands keep their machine-readable output on stdout. `--config <path>` and `--no-config` keep their meaning for `run_cycle.py`.

`.code-cycle.yml` is optional and holds **no secrets**. Skills never edit it silently. `cc-provider-bootstrap` and `cc-orchestrator` show the proposed change and ask first.

## Full example

Every key the toolkit reads, with its default or a typical value. Declare only what you need.

```yaml
code_cycle:
  # ── Providers ─────────────────────────────────────────────
  issue_provider: github            # github | plane | jira
  code_host: github                 # github | bitbucket
  issue:
    project: ENG                    # project/workspace scope for provider checks
    selector: ENG-123               # a work item the health check reads
  repository:
    selector: owner/repository      # required by run_cycle.py (unless --repo) and cc-stats
    default_branch: main            # base for scoped reads and diff signals
  worktree_dir: .worktree           # persistent worker worktrees; relative to the repository root

  # ── Provider health cache ─────────────────────────────────
  verification:
    cache_ttl: 7d

  # ── Review ────────────────────────────────────────────────
  review:
    trusted_authors: [your-login]   # empty/absent → finding recovery BLOCKS

  security_review:                  # absent → defaults; declared → replaces defaults, keeps minimum
    always_when:
      paths: ["auth/**", "**/migrations/**"]
      files: ["package-lock.json", "Dockerfile", "*.env.example"]
      labels: ["security", "auth", "data"]

  # ── Routing (runtime) ─────────────────────────────────────
  profiles:                         # overlay on DEFAULT_PROFILES
    cheap_coder:
      primary: "codex:openai/gpt-6-luna high"
      fallback: "claude:anthropic/claude-sonnet-5 high"
  routing:
    strategy: fixed                 # fixed | measured
    jev:
      mode: disabled                # disabled | shadow
      model: jev-latest
      timeout_seconds: 3
  resolve:
    # effort: high                  # low | medium | high | max; explicit value disables automatic escalation
  allowed_commands:
    test: python3 -m unittest discover -s tests  # one exact command
    verify:                         # exact prefixes a stage may extend with arguments
      - python3 scripts/validate-package.py
      - python3 -m unittest
  ci:
    policy: strict                  # strict | parallel (GitHub only); --ci-policy overrides
    timeout: 900                    # seconds the parallel CI gate waits for one SHA
  docker:
    host_socket_access: false       # true grants docker_host without asking (see routing.md)
  execution:                        # how deep the cycle goes; not model routing
    profile: standard               # prototype | standard | full | hotfix | custom
    # objective: merge_ready        # local_change | verified_change | merge_ready
    # stages:
    #   security: required          # disabled | auto | enabled | required

  # ── Orchestration ─────────────────────────────────────────
  orchestration:
    mode: auto                      # auto | single_agent | claude_codex

  # ── Calibration (experimental, paired review only) ────────
  calibration:
    profiles:
      reviewer_a: { provider: anthropic, model: claude-sonnet-5, effort: high }
      reviewer_b: { provider: openai, model: gpt-6-sol, effort: high }
```

## `.code-cycle.yml` reference

All keys live under `code_cycle`. `run_cycle.py` **refuses any key not in this table**, by name and before dispatching anything, so a typo such as `profles` can't silently run on defaults. It reads `repository`, `profiles`, `routing`, `issue_review`, `ci`, `docker`, and `execution` itself. It recognises the other keys and leaves them to the component that owns them.

| Key | Type / values | Default | Read by |
|---|---|---|---|
| `issue_provider` | `github` · `plane` · `jira` | inferred only as GitHub, from a numeric ID on a GitHub host | `cc-provider-bootstrap` |
| `code_host` | `github` · `bitbucket` | inferred from `origin` | `cc-provider-bootstrap` |
| `issue.project` | string | — | `cc-provider-bootstrap` |
| `issue.selector` | string | — | `cc-provider-bootstrap` |
| `repository.selector` | `owner/name` or `workspace/repo` | inferred from `origin` (skills); **required** by `cc-stats` and by `run_cycle.py` without `--repo` | bootstrap, `run_cycle.py`, `cc-stats` |
| `repository.default_branch` | branch name | remote default (`origin/HEAD`) | bootstrap, `run_cycle.py` diff signals |
| `worktree_dir` | non-empty relative path within the repository | `.worktree` | orchestrator skills and `run_cycle.py` stage prompts |
| `verification.cache_ttl` | duration, e.g. `7d` | `7d` | `cc-provider-bootstrap` |
| `verification.recheck_on_failure` | boolean | — | **not read**: a live provider failure always forces a recheck |
| `profiles.<name>.primary` | target string | `DEFAULT_PROFILES` | `run_cycle.py`, `cc-orchestrator` |
| `profiles.<name>.fallback` | target string | `DEFAULT_PROFILES` | `run_cycle.py`, `cc-orchestrator` |
| `routing.strategy` | `fixed` · `measured` | `fixed` | `run_cycle.py` |
| `routing.jev.mode` | `disabled` · `shadow` | `disabled` | `run_cycle.py` |
| `routing.jev.model` | `jev-latest` · `jev-x.y.z` (`typesafe-ai/jev` → `jev-latest`) | `jev-latest` | `run_cycle.py` |
| `routing.jev.timeout_seconds` | number, > 0 and ≤ 10 | `3` | `run_cycle.py` |
| `resolve.effort` | `low` · `medium` · `high` · `max` | primary resolve uses `high`; an explicit value disables automatic escalation | `run_cycle.py` |
| `allowed_commands.test` | one exact command string; wildcards and shell operators are refused | unset: no stage may run tests | `run_cycle.py`; the command table of implementation and resolution, and of review and rereview inside their disposable clone ([host permissions](host-permissions.md)) |
| `allowed_commands.verify` | non-empty list of exact command prefixes; a stage may append arguments. Wildcards, variables, shell operators, a bare interpreter or shell, inline code (`python3 -c`, `node -e`) and launchers without a target (`npx`, `npm run`) are refused | unset: no verification command | same as `test`. A runtime older than #142 rejects this key as unknown |
| `ci.policy` | `strict` · `parallel` | `strict` | `run_cycle.py`, both orchestrators |
| `ci.timeout` | whole seconds, 1 to 86400 | `900` | `run_cycle.py` and the orchestrators' `ci_gate.py` call, under `parallel` |
| `docker.host_socket_access` | boolean | `false`: a stage that needs Docker asks first. `true` authorizes the `docker_host` grant in advance for both of its scopes. On Linux both native sandboxes open every reachable Unix socket; Claude's command hook still limits Docker to the listed Compose commands. A runtime `docker version` probe checks reachability inside the selected sandbox before Docker-dependent checks run | `run_cycle.py` (sensitive grants) |
| `issue_review.mode` | `auto` · `off` | `auto` | `run_cycle.py`, both orchestrators |
| `execution.profile` | `prototype` · `standard` · `full` · `hotfix` · `custom` | `standard` | `run_cycle.py`; see [Execution profiles](#execution-profiles) |
| `execution.objective` | `local_change` · `verified_change` · `merge_ready` | the profile's | `run_cycle.py` |
| `execution.stages.<stage_id>` | `disabled` · `auto` · `enabled` · `required` | the profile's | `run_cycle.py` |
| `review.trusted_authors` | list of provider logins | empty (recovery blocks); `cc-provider-bootstrap` proposes the authenticated login when absent | `cc-initial-review`, `cc-rereview`, `cc-resolve-comments` |
| `security_review.always_when.paths` | glob list | built-in defaults | review and resolution skills (`scripts/security_gate.py`) |
| `security_review.always_when.files` | filename glob list | built-in defaults | same |
| `security_review.always_when.labels` | label terms, matched by meaning | built-in defaults | same |
| `orchestration.mode` | `auto` · `single_agent` · `claude_codex` | `auto` | `cc-orchestrator` |
| `calibration.profiles.<alias>.provider` / `.model` / `.effort` | strings | — | `cc-orca-orchestrator` with `paired_review=true` |

A configuration file that exists but can't be read (bad YAML, PyYAML missing, wrong shape) **stops** `run_cycle.py`. It never falls back to the defaults. Use `--no-config` to run on the defaults deliberately.

### Providers and repository

`issue_provider` and `code_host` are independent, even when both are GitHub. Plane and Jira are never guessed from an identifier's shape. When either value stays ambiguous, the cycle stops with `BLOCKED` before creating a branch, commit, comment, or PR. See [Providers](provider-contract.md).

`repository.default_branch` is also the base the runtime diffs against to record [pre-routing signals](telemetry.md#what-is-recorded). Without it, the remote's default branch is used.

### Persistent worker worktrees

`worktree_dir` names the repository-relative base directory for persistent
worker worktrees created or requested by the toolkit. It defaults to
`.worktree`; a stage prompt receives its resolved absolute path. The default
directory is ignored by Git. If you override it, add the selected directory to
the repository's `.gitignore` too. The setting does not relocate disposable
review clones, which remain outside the repository. Orca-managed worktrees use
Orca's managed location because its current CLI does not expose a caller-chosen
base directory; see [Orca workflows](workflows.md#cc-orca-orchestrator).
Implementation workflows use a task-specific linked worktree by default.
`workspace=current` is a per-invocation opt-out for the current checkout; it is
not persisted in repository configuration. An active linked worktree is reused
only when its branch contains the exact, delimited work-item identifier or its existing change request links the same work item.

### Provider health cache

`cc-provider-bootstrap` caches a successful read-only check per provider and scope for `verification.cache_ttl` (default 7 days). A cache entry is reused only when provider, scope, capabilities, and configuration still match. A live failure invalidates the affected entry and forces a recheck. The cache lives outside the repository (see [Local files](#local-files)) and stores no tokens or response bodies.

### Profiles and routing

Target format: `executor:provider/model effort`, where executor is `codex`, `claude`, or `orca`. Profile names: `cheap_coder`, `deep_coder`, `reviewer`, `senior_reviewer`, `security`, `coordinator`, `auxiliary_tool`, `cheap_tool`. Models you declare here are also added to telemetry's accepted model set for this repository.

Defaults, role rules, fallback behaviour, strategies, and Jev are explained in [Routing and models](routing.md). To see the effective profiles or write this block with a preset, use [`cc-profile-config`](skills.md#cc-profile-config).

### Execution profiles

An execution profile states how deep a cycle goes; an objective states the outcome it must reach; a stage policy adjusts one stage. They are separate from each other and from the model routing [profiles](#profiles-and-routing), which they never read or change. `run_cycle.py --dry-run [--json]` prints the resulting [execution plan](execution-plan.md) without running anything.

```yaml
code_cycle:
  execution:
    profile: full                   # prototype | standard | full | hotfix | custom
    objective: merge_ready          # local_change | verified_change | merge_ready
    stages:
      security: required            # disabled | auto | enabled | required
```

**Profiles.** `standard` is the current cycle and the default: with no new configuration, the plan records `standard` and the current stage order, policies, and side effects. `prototype` is fast local output. `full` applies every applicable declared control at greater depth, without repeating an unchanged review. `hotfix` reproduces the defect, makes the minimal change, and runs a focused regression, with no exception to security or review. `custom` starts from `standard` and applies explicit stage policies.

| Stage | `prototype` | `standard` | `full` | `hotfix` | `custom` |
|---|---|---|---|---|---|
| `issue_review` | auto | auto | required | auto | auto, configurable |
| `implement` | required | required | required | required | required, configurable |
| `verify` (within `implement`) | required | required | required | required | required, configurable |
| `review` | disabled | required | required | required | required, configurable |
| `security` (within the reviews) | auto | auto | required when it applies | auto | auto, configurable |
| `resolve` | auto | auto | auto | auto | auto, configurable |
| `rereview` | auto | auto | auto | auto | auto, configurable |

A row is not a dispatch: `verify` runs inside the implementation and `security` inside the reviews, as each plan entry's `embedded_in` records. `resolve` runs only for concrete findings or failed checks. `rereview` runs only after changes that invalidate the review's evidence, in every profile including `full`: a clean review without later changes is not repeated, and open findings without fixes keep their blocks instead of being reviewed again.

**Stage policies.**

| Policy | Meaning |
|---|---|
| `disabled` | Do not request the stage. When that contradicts an obligation of the profile or objective, or a stage that depends on it, the plan is refused with the conflict. A conditional obligation, such as the security rule, stays in the plan; if it later applies, the plan is invalid rather than the obligation void. |
| `auto` | Decide by deterministic rules on the available signals. Unknown risk is never read as no risk: a condition that needs the change stays `pending`. |
| `enabled` | Schedule the stage when it applies. Its unavailability is recorded; it creates no obligation of success. |
| `required` | When the stage applies, require its result and evidence. A missing tool or an inconclusive result does not satisfy it. |

**Objectives.** `prototype` defaults to `local_change`; every other profile to `merge_ready`.

| Objective | Verification | Review | CI | Merge | Publication |
|---|---|---|---|---|---|
| `local_change` | per stage policy | not required | not required | not required | none |
| `verified_change` | required | required | may run, not required | not required | change request |
| `merge_ready` | required | required | required for the reviewed SHA | never automatic; stays an explicit human merge | change request |

An objective adds obligations: publishing a `prototype` with `--objective merge_ready` makes its review required, and the plan records the objective as that policy's source. Conflicting explicit objectives in the repository and invocation are refused, including `--local-only` against a configured publication objective; agreeing selections are accepted.

**Precedence.** Settings resolve in layers: compatible defaults, profile, repository (`.code-cycle.yml`), invocation. A later layer replaces an earlier one, and the plan lists each effective value with its source and the values it replaced. Two different values in one layer, such as `--issue-review off` with `--stage issue_review=auto`, or `--local-only` with `--objective merge_ready`, are refused rather than one silently winning. Obligations are monotonic: an override may add one but never remove one. Lowering a `required` stage of `prototype`, `standard`, `full`, or `hotfix` is a conflict (use `custom`), and no profile may lower what the objective or a declared `--security-sensitive` requires. Every conflict is reported, with its source, before any effect.

| Key or flag | Layer | Stage or control | Policy it sets |
|---|---|---|---|
| `code_cycle.issue_review.mode` (`auto`\|`off`) | repository | `issue_review` | `auto` \| `disabled` |
| `--issue-review auto\|off` | invocation | `issue_review` | `auto` \| `disabled` |
| `code_cycle.ci.policy` (`strict`\|`parallel`) | repository | `ci` control | timing of the gate, not whether it runs |
| `--ci-policy strict\|parallel` | invocation | `ci` control | timing of the gate, not whether it runs |
| `--local-only` | invocation | objective | selects `local_change` |
| `code_cycle.execution.objective` / `--objective` | repository / invocation | objective | selects the objective |
| `code_cycle.execution.profile` / `--execution-profile` | repository / invocation | profile | selects the base profile |
| `code_cycle.execution.stages.<stage_id>` / `--stage STAGE=POLICY` | repository / invocation | one stage | `disabled`\|`auto`\|`enabled`\|`required` |
| `--from` / `--continue` | invocation | resume start point | not a policy: the stages before it are `not_applicable`, with the resumed run as provenance |

The configurable stages are `issue_review`, `implement`, `verify`, `review`, `security`, `resolve`, and `rereview`. `ci` and `merge` are controls, not stages: `--stage ci=…` is refused, and nothing makes a merge automatic. An unknown key, stage, profile, objective, or policy stops the run by name.

**Before plan-driven execution (#329).** The driver follows the plan only from #329 on. Until then, a run without `--dry-run` refuses, before any effect, any execution configuration whose behaviour differs from `standard`, naming the setting and its source. No new configuration, or an explicit configuration that resolves to `standard` (including `custom` without a difference, `issue_review` off, and `local_change` through the local-only run), runs the current flow; `local_change` from `--objective` or the file applies the local-only run's own requirements (`--cwd` linked worktree, `--workspace current`). A requested profile is never silently replaced by `standard`. `--dry-run` shows the plan for every profile and override.

### Issue review

`issue_review.mode: auto` (the default) makes a new `run_cycle.py`, `cc-orchestrator`, or `cc-orca-orchestrator` cycle review its work item with `cc-issue-review` before implementing it, except a task declared trivial and not security-sensitive. `off` keeps the original flow. `--issue-review`, or `issue_review=auto|off` in an orchestrator invocation, overrides it for one run. Only a confirmed `READY` reaches implementation. See [Review lifecycle → Issue review](review-cycle.md#issue-review-before-implementation).

### CI policy

`ci.policy` decides how a cycle treats the code host's checks. `strict`, the default, is the original serial flow: the implementation and each resolution wait for their pushed head's checks, and a review approves only once they passed. `parallel` starts the review while the checks run, sends a review that requests changes to resolution at once, and approves a clean review only after the runtime's one CI gate saw every required check of that same SHA pass. Parallel mode never disables CI; it removes the time spent waiting for it before any useful work can start.

```yaml
code_cycle:
  ci:
    policy: parallel
    timeout: 1200
```

`--ci-policy strict|parallel` overrides the file for one `run_cycle.py` run, and `ci_policy=strict|parallel` for one orchestrator invocation; `--ci-timeout` overrides `ci.timeout`. `parallel` reads GitHub checks and rulesets, so it is refused for another `code_host`, which keeps `strict`. See [Review lifecycle → CI policy](review-cycle.md#ci-policy).

### Review

`review.trusted_authors` controls which comments can create or advance recovered findings. When it is absent, `cc-provider-bootstrap` adds the authenticated code-host login to the configuration it proposes, and writes it only on confirmation. An existing list is never changed. See [Review lifecycle → Trusted authors](review-cycle.md#trusted-authors).

### Allowed commands

On a GitHub cycle (`code_host` and `issue_provider` both GitHub), every native Claude stage runs under a closed command table and a hook that denies everything outside it ([Host permissions](host-permissions.md)). `allowed_commands.test` and `allowed_commands.verify` are the only verification commands any stage receives: implementation and resolution in their worktree, review and rereview only inside their disposable clone. Without them no verification command is granted and no global permission is inherited in its place. A stage that needs one stops `BLOCKED`, and the cycle's stop line names the exact denied command. Add it here, then resume with `--continue` (or `--from`); the progress made so far is kept.

**Upgrading.** A cycle that used to run tests through a stage's inherited permissions now needs `allowed_commands.test` or `allowed_commands.verify`. A runtime older than this change refuses `verify` as an unknown key, so update the runtime before adding it.

### Security review rule

`security_review.always_when` names paths, files, and labels that **always** trigger `cc-security-review`, whatever a reviewer concludes. A reviewer can add an audit but never remove one the rule fired.

Built-in defaults, used when the block is absent:

| Kind | Defaults |
|---|---|
| paths | `auth/**`, `middleware/**`, `migrations/**`, `routes/**` (at the root and at any depth), `**/Policies/**`, `**/policies/**` |
| files | `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `composer.lock`, `requirements.txt`, `poetry.lock`, `Gemfile.lock`, `go.sum`, `Cargo.lock`, `Dockerfile`, `docker-compose*.yml`, `*.env.example` |
| label terms | security, auth, authz, authentication, authorization, permission, privacy, data, secret, credential, dependencies |

A declared `paths`, `files`, or `labels` list replaces the corresponding default list. The mandatory minimum below is always added to declared path and file lists; it cannot be disabled or excluded. Other defaults remain replaceable. An absent or empty configuration keeps the built-in defaults and the minimum.

The mandatory minimum is the shared baseline applied by `scripts/security_gate.py` and every review skill. Skills are installed without `docs/`, so `cc-initial-review`, `cc-rereview`, `cc-resolve-comments` and `cc-provider-bootstrap` each carry the same table in a *Mandatory security minimum* section; `validate-package.py` keeps those copies identical and a test keeps them equal to the script constants:

| Group | Paths and files |
|---|---|
| CI and automation | `.github/workflows/**`, `.github/actions/**`, `.gitlab-ci.yml`, `bitbucket-pipelines.yml` |
| Secrets and keys | `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*` |
| Agent and toolkit configuration | `.code-cycle.yml`, `AGENTS.md`, `CLAUDE.md`, `.claude/**`, `.codex/**`, `.agents/**`, `opencode.json*` |

When a mandatory path or file also matches a declared project rule, the audit runs once and reports that changed path once. Labels stay project-configurable. This gate determines whether the audit runs; severity and blocking policy are separate.

### Orchestration

`orchestration.mode` sets `cc-orchestrator`'s default mode. An explicit `orchestration_mode=` in the prompt wins. `claude_codex` requires Claude Code with `codex-plugin-cc`, and it blocks before implementation when Codex can't be used. See [Workflows → Claude + Codex](workflows.md#claude--codex-mode).

### Calibration

`calibration.profiles` names the two reviewer candidates for a paired review. Declaring them **never** starts a calibration and never changes the routing mode. See [Routing → Calibration](routing.md#calibration).

## Invocation parameters

Pass these in the prompt, for example `Use cc-orchestrator for issue 123 with max_iterations=4 lang=es`.

| Parameter | Skills | Default | Meaning |
|---|---|---|---|
| `issue_id` / `issue_number` | implement, orchestrators, bootstrap | required | Work item. `issue_number` is the GitHub alias. |
| `issue_provider` | all provider-aware skills | resolved | `github` · `plane` · `jira` |
| `code_host` | all provider-aware skills | resolved | `github` · `bitbucket` |
| `repository` / `repo` | all provider-aware skills | resolved | Repository selector |
| `lang` | every skill that publishes | resolved | Output language, e.g. `lang=en` |
| `orchestration_mode` | `cc-orchestrator` | `auto` | `auto` · `single_agent` · `claude_codex` |
| `max_iterations` | both orchestrators | `6` | Resolve + rereview rounds, ≥ 1 |
| `ci_policy` | both orchestrators | `ci.policy`, else `strict` | `strict` · `parallel`; see [CI policy](review-cycle.md#ci-policy) |
| `implementer` | `cc-orca-orchestrator` | `codex` | Agent that implements and resolves |
| `reviewer` | `cc-orca-orchestrator` | `claude` | Agent that reviews and rereviews |
| `paired_review` | `cc-orca-orchestrator` | `false` | Two blind reviewers (calibration) |
| `campaign` | `cc-orca-orchestrator` | — | Required with `paired_review=true` |
| `--json`, `--orchestration-result`, or "with the structured result" | review and resolution skills | off | Emit `ORCHESTRATION_RESULT` |
| `force=true` | `cc-provider-bootstrap` | — | Ignore the health cache and recheck |

## Output language

Published text (PR comments, thread replies, commit messages, the final response) uses one language, chosen in this order:

1. an explicit request: `lang=es`, or "review in English";
2. the language of the repository's own instructions (`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`);
3. the language of the issue, the PR description, and existing review comments;
4. English.

Machine-readable tokens never translate: `REV-xxx`, severities, statuses, dispositions, `blocks:yes|no`, run lines, functional statuses, and JSON keys. Free-text JSON values such as `summary` follow the selected language. `cc-run` publishes no provider artefact, and its step 3 uses the language of the project documentation instead of the issue or PR. Command names, service names, ports, and paths are never translated.

## Environment variables

| Variable | Used by | Effect |
|---|---|---|
| `PYTHONPATH` | code that imports runtime modules | Optional. Add `~/.code-cycle/runtime` only when other Python code must `import` them. `run_cycle.py` and `stats.py` don't need it. |
| `CODE_CYCLE_HOME` | telemetry, calibration store | Overrides the directory holding `telemetry.sqlite` and `calibration/` |
| `XDG_CONFIG_HOME` | telemetry, calibration store (Unix) | Base for `code-cycle-toolkit/` when `CODE_CYCLE_HOME` is unset |
| `APPDATA` | same (Windows) | Base for `code-cycle-toolkit\` when `CODE_CYCLE_HOME` is unset |
| `CODEX_HOME` | Codex probe, `host_permissions.py` | Where the Codex credential (`auth.json`) and `rules/default.rules` are looked for; default `~/.codex` |
| `CLAUDE_CONFIG_DIR` | Claude stage preflight, `host_permissions.py` | Where Claude's `settings.json` and plugins are read; default `~/.claude` |
| `GH_TELEMETRY` | native stages and runtime GitHub CLI calls | Defaults to `false` when unset, disabling GitHub CLI telemetry. An explicit parent value takes precedence. |
| `GH_NO_UPDATE_NOTIFIER` | native stages and runtime GitHub CLI calls | Defaults to `1` when unset, disabling GitHub CLI update checks. An explicit parent value takes precedence. |
| `TYPESAFE_API_KEY` | Jev shadow | API key, read at call time and never stored. Without it, Jev records `unavailable`. |
| `JEV_API_KEY` | Jev shadow | Legacy fallback for `TYPESAFE_API_KEY` |

## Command-line flags

### Installers

| `install.sh` | `install.ps1` | Default | Meaning |
|---|---|---|---|
| `--agent all\|claude\|codex\|opencode` | `-Agent` | `all` | Target host |
| `--scope global\|project` | `-Scope` | `global` | User-wide or one repository |
| `--project-dir <path>` | `-ProjectDir` | current directory | Repository for project scope |
| `--force` | `-Force` | off | Replace existing installed skills |
| `--no-runtime` | `-NoRuntime` | off | Skills only |

`get.sh` and `get.ps1` take `--version` / `-Version` (`latest`, `main`, or `vX.Y.Z`; also `CODE_CYCLE_VERSION`), pass the options above to the installer, and always add `--force`.

### `run_cycle.py`

`--task` (required) · `--repo` · `--difficulty 1|2|3` (default 2) · `--verifiability auto|partial|human` (default auto) · `--security-sensitive` · `--verification available|unavailable` · `--mode production|calibration` · `--issue-review auto|off` (default `issue_review.mode`, else auto) · `--from implement|review|resolve|rereview` (default implement) · `--pr` (required by any `--from` but implement) · `--continue` · `--max-iterations` (default 3) · `--ci-policy strict|parallel` (default `ci.policy`, else strict) · `--ci-timeout` (default `ci.timeout`, else 900 seconds) · `--cwd` · `--workspace task|current` (default task) · `--local-only` (requires `--workspace current`) · `--timeout` · `--detach` · `--verbose` · `--progress-interval` (default 60 seconds) · `--database` · `--config` · `--no-config` · `--execution-profile prototype|standard|full|hotfix|custom` (default `execution.profile`, else standard) · `--objective local_change|verified_change|merge_ready` (default `execution.objective`, else the profile's) · `--stage STAGE=POLICY` (repeatable) · `--dry-run` · `--json` (requires `--dry-run`). Each is explained in [Workflows → Runtime driver](workflows.md#runtime-driver-run_cyclepy).

### `ci_gate.py` (used by both orchestrators under `parallel`)

`--repo owner/name` · `--pr <number>` · `--sha <full head SHA>` (all required) · `--timeout` (default 900 seconds) · `--poll-interval` (default 30 seconds) · `--once` (read the checks once, never wait). It prints the gate's JSON result and exits `0` only on `CI_PASSED`. It only reads the code host. See [Review lifecycle → CI policy](review-cycle.md#ci-policy).

### `stats.py` (used by `cc-stats`)

| Flag | Default | Meaning |
|---|---|---|
| `--cwd <repo root>` | current directory | Repository whose `.code-cycle.yml` selects the telemetry |
| `--days N` | `30` | Look-back window |
| `--all-time` | off | Whole history |
| `--format markdown\|json` | `markdown` | Output format |

### `profile_config.py` (used by `cc-profile-config`)

`show | propose | write | presets` · `--cwd <repo root>` · `--config <path>` · `--preset <name>` · `--set <profile>.primary|fallback=<target|none>` (repeatable) · `--allow-cross-split` · `--confirmed` (required by `write`) · `--no-probe` · `--format markdown|json`. An explicit `--config` path may name a file that does not exist yet: `show` and `propose` use built-in defaults, while a confirmed `write` creates the file. From a linked worktree, writes target the repository's current or main worktree configuration; symlinked configuration files are followed while validating and writing. See [Routing → Changing profiles](routing.md#changing-profiles).

### `host_permissions.py` (used by `cc-host-permissions`)

`plan | apply | revert | validate` · `--host claude|codex|both` (default both) · `--yes` (required by `apply` and `revert`). Exit status `3` reports a conflict with a hand-edited entry, `2` a file it could not read. See [Host permissions → Global opt-in](host-permissions.md#global-opt-in).

### `validate-package.py`

`python3 scripts/validate-package.py` takes no flags. See [CONTRIBUTING](../CONTRIBUTING.md).

## Local files

Nothing below is written into your repository, except a project-scope installation.

| Path (Unix) | Contents |
|---|---|
| `~/.config/code-cycle-toolkit/telemetry.sqlite` | Telemetry database. See [Telemetry](telemetry.md). |
| `~/.config/code-cycle-toolkit/provider-health/` | Provider health cache |
| `~/.config/code-cycle-toolkit/calibration/` | Paired-review campaign store |
| `~/.code-cycle/runtime/` | Global runtime installation |
| `<repo>/.code-cycle/runtime/` + `.code-cycle/.gitignore` | Project runtime installation |

On Windows, the `~/.config/code-cycle-toolkit` directory is `%APPDATA%\code-cycle-toolkit`.

## Project tool choices and dependency setup

See [dependency preparation](dependency-preparation.md) for automatic frozen
Node installs, contained lifecycle-build recovery, persistent setup evidence,
and the `code_cycle.user_choices` configuration shared by dispatched stages.

---

[← Routing and models](routing.md) · [↑ Documentation index](README.md) · [Providers →](provider-contract.md)
