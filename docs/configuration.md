# Configuration

Every setting the toolkit reads, in one place.

**On this page:** [Precedence](#precedence) · [Full example](#full-example) · [`.code-cycle.yml` reference](#code-cycleyml-reference) · [Providers and repository](#providers-and-repository) · [Provider health cache](#provider-health-cache) · [Profiles and routing](#profiles-and-routing) · [Review](#review) · [Security review rule](#security-review-rule) · [Orchestration](#orchestration) · [Calibration](#calibration) · [Invocation parameters](#invocation-parameters) · [Output language](#output-language) · [Environment variables](#environment-variables) · [Command-line flags](#command-line-flags) · [Local files](#local-files)

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

  # ── Provider health cache ─────────────────────────────────
  verification:
    cache_ttl: 7d

  # ── Review ────────────────────────────────────────────────
  review:
    trusted_authors: [your-login]   # empty/absent → finding recovery BLOCKS

  security_review:                  # absent → built-in defaults; declared → replaces them
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

All keys live under `code_cycle`. `run_cycle.py` **refuses any key not in this table**, by name and before dispatching anything, so a typo such as `profles` can't silently run on defaults. It reads `repository`, `profiles`, and `routing` itself. It recognises the other keys and leaves them to the component that owns them.

| Key | Type / values | Default | Read by |
|---|---|---|---|
| `issue_provider` | `github` · `plane` · `jira` | inferred only as GitHub, from a numeric ID on a GitHub host | `cc-provider-bootstrap` |
| `code_host` | `github` · `bitbucket` | inferred from `origin` | `cc-provider-bootstrap` |
| `issue.project` | string | — | `cc-provider-bootstrap` |
| `issue.selector` | string | — | `cc-provider-bootstrap` |
| `repository.selector` | `owner/name` or `workspace/repo` | inferred from `origin` (skills); **required** by `cc-stats` and by `run_cycle.py` without `--repo` | bootstrap, `run_cycle.py`, `cc-stats` |
| `repository.default_branch` | branch name | remote default (`origin/HEAD`) | bootstrap, `run_cycle.py` diff signals |
| `verification.cache_ttl` | duration, e.g. `7d` | `7d` | `cc-provider-bootstrap` |
| `verification.recheck_on_failure` | boolean | — | **not read**: a live provider failure always forces a recheck |
| `profiles.<name>.primary` | target string | `DEFAULT_PROFILES` | `run_cycle.py`, `cc-orchestrator` |
| `profiles.<name>.fallback` | target string | `DEFAULT_PROFILES` | `run_cycle.py`, `cc-orchestrator` |
| `routing.strategy` | `fixed` · `measured` | `fixed` | `run_cycle.py` |
| `routing.jev.mode` | `disabled` · `shadow` | `disabled` | `run_cycle.py` |
| `routing.jev.model` | `jev-latest` · `jev-x.y.z` (`typesafe-ai/jev` → `jev-latest`) | `jev-latest` | `run_cycle.py` |
| `routing.jev.timeout_seconds` | number, > 0 and ≤ 10 | `3` | `run_cycle.py` |
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

### Provider health cache

`cc-provider-bootstrap` caches a successful read-only check per provider and scope for `verification.cache_ttl` (default 7 days). A cache entry is reused only when provider, scope, capabilities, and configuration still match. A live failure invalidates the affected entry and forces a recheck. The cache lives outside the repository (see [Local files](#local-files)) and stores no tokens or response bodies.

### Profiles and routing

Target format: `executor:provider/model effort`, where executor is `codex`, `claude`, or `orca`. Profile names: `cheap_coder`, `deep_coder`, `reviewer`, `senior_reviewer`, `security`, `coordinator`, `auxiliary_tool`, `cheap_tool`. Models you declare here are also added to telemetry's accepted model set for this repository.

Defaults, role rules, fallback behaviour, strategies, and Jev are explained in [Routing and models](routing.md).

### Review

`review.trusted_authors` controls which comments can create or advance recovered findings. When it is absent, `cc-provider-bootstrap` adds the authenticated code-host login to the configuration it proposes, and writes it only on confirmation. An existing list is never changed. See [Review lifecycle → Trusted authors](review-cycle.md#trusted-authors).

### Security review rule

`security_review.always_when` names paths, files, and labels that **always** trigger `cc-security-review`, whatever a reviewer concludes. A reviewer can add an audit but never remove one the rule fired.

Built-in defaults, used when the block is absent:

| Kind | Defaults |
|---|---|
| paths | `auth/**`, `middleware/**`, `migrations/**`, `routes/**` (at the root and at any depth), `**/Policies/**`, `**/policies/**` |
| files | `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `composer.lock`, `requirements.txt`, `poetry.lock`, `Gemfile.lock`, `go.sum`, `Cargo.lock`, `Dockerfile`, `docker-compose*.yml`, `*.env.example` |
| label terms | security, auth, authz, authentication, authorization, permission, privacy, data, secret, credential, dependencies |

A declared list **replaces** the matching defaults. Don't write an empty block to mean "no rule": absence is what keeps the gate on.

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
| `CODEX_HOME` | Codex probe | Where the Codex credential (`auth.json`) is looked for; default `~/.codex` |
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

`--task` (required) · `--repo` · `--difficulty 1|2|3` (default 2) · `--verifiability auto|partial|human` (default auto) · `--security-sensitive` · `--verification available|unavailable` · `--mode production|calibration` · `--max-iterations` (default 3) · `--cwd` · `--local-only` · `--timeout` · `--verbose` · `--progress-interval` (default 60 seconds) · `--database` · `--config` · `--no-config`. Each is explained in [Workflows → Runtime driver](workflows.md#runtime-driver-run_cyclepy).

### `stats.py` (used by `cc-stats`)

| Flag | Default | Meaning |
|---|---|---|
| `--cwd <repo root>` | current directory | Repository whose `.code-cycle.yml` selects the telemetry |
| `--days N` | `30` | Look-back window |
| `--all-time` | off | Whole history |
| `--format markdown\|json` | `markdown` | Output format |

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

---

[← Routing and models](routing.md) · [↑ Documentation index](README.md) · [Providers →](provider-contract.md)
