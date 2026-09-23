# Code Cycle Toolkit

Code Cycle Toolkit is a portable collection of Agent Skills for taking a work item from GitHub Issues, Plane, or Jira through implementation, code-review, targeted resolution, and rereview on GitHub or Bitbucket.

It works with Codex, Claude Code, and OpenCode on Linux, macOS, Windows, WSL, Git Bash, and similar environments. The reusable instructions live in `skills/`; the host-specific manifests and installers are only compatibility layers.

`cc` means **Code Cycle**.

This project is licensed under the MIT License. See [LICENSE](LICENSE).

## What it does

- Implements one work item from the configured issue provider and takes it to a tested pull request.
- Performs a complete initial pull-request review.
- Tracks review findings with stable `REV-xxx` identifiers.
- Resolves valid review comments with targeted changes and evidence-backed verification.
- Rereviews the accumulated pull request after changes.
- Coordinates the complete issue-to-merge-readiness cycle.
- In Claude Code, can optionally keep implementation and comment resolution in
  Claude while delegating initial review and rereview to Codex.
- Publishes consolidated pull-request comments when the workflow requires them.
- Preserves a manual merge boundary: the toolkit never merges a pull request.
- Documents and enforces role workspace contracts in
  [docs/role-workspace-policy.md](docs/role-workspace-policy.md), including
  isolated handling for verification and runtime artifacts.

## What it does not do

- It is not a GitHub, Bitbucket, Plane, or Jira replacement, CI service, deployment system, or issue tracker.
- It does not provide provider credentials, API clients, or bypass repository permissions.
- It does not bundle, install, authenticate, or update `codex-plugin-cc`.
- It does not invent missing tests, project policies, or verification commands.
- It does not silently treat unavailable tools, skipped checks, or unverified behavior as success.
- It does not require the target repository to adopt any file, label, or convention.
- The generic orchestrator does not assume that the host has workers, subagents, worktrees, or a particular delegation API.
- `cc-orca-orchestrator` is not generic: it requires the Orca supervised worker API.

## Skills

Thirteen skills in two layers. The **cycle skills** own the provider-neutral workflow: the change request, finding identifiers, published comments, and merge boundary. The **supporting skills** provide provider bootstrap, focused review, security, verification, runtime, and telemetry capabilities. Cycle skills delegate to supporting skills; delegated review and verification passes never publish on their own.

### Cycle skills

| Skill | Use it for | Changes product code? |
|---|---|---:|
| `cc-implement-issue` | Implement an issue, verify it, and open a pull request. | Yes |
| `cc-initial-review` | Review the full pull-request diff and publish findings. | No |
| `cc-resolve-comments` | Triage review feedback and implement valid fixes. | Yes |
| `cc-rereview` | Rereview the accumulated pull request after changes. | No |
| `cc-orchestrator` | Coordinate the complete cycle with native host delegation or a sequential fallback. | Only through delegated stages |
| `cc-orca-orchestrator` | Coordinate the cycle through Orca Runs, Tasks, and Workers. | Only through delegated stages |

### Supporting skills

| Skill | Use it for | Invoked by |
|---|---|---|
| `cc-pr-review` | Full pull-request review criteria: scope, correctness, regressions, architecture, tests, operations. | `cc-initial-review`, `cc-rereview`, or directly |
| `cc-code-review` | Diff-focused review of a change set, without the pull-request framing. | `cc-resolve-comments`, or directly |
| `cc-security-review` | Security audit scoped to the change and its trust boundaries. | Sensitivity triage, or directly |
| `cc-verify` | Execution-backed verification: static checks, tests, real application validation. | Any cycle skill, or directly |
| `cc-run` | Detect how the project starts, bring services up in order, confirm they respond. | `cc-verify`, or directly |
| `cc-provider-bootstrap` | Resolve provider configuration and validate cached provider access at cycle startup. | `cc-implement-issue`, `cc-orchestrator`, or `cc-orca-orchestrator` |
| `cc-stats` | Summarize local, repository-scoped telemetry with sample-aware rates and compact trends. | Natural language or `/cc-stats` |

Each supporting skill is also useful on its own — `cc-code-review` on a working tree, `cc-verify` after a fix, `cc-run` to bring an unfamiliar project up, and `cc-stats` for a telemetry report.

Review skills do not approve or merge code. The orchestrators stop at a validated `READY_FOR_MANUAL_MERGE` state.

## Working assumptions

The toolkit brings its own defaults and adapts to the target repository rather than requiring it to adapt.

**Repository conventions are optional.** Every skill reads `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, and the documentation they point to *when they exist*, and prefers them over its own defaults. When a repository defines none, the skill uses its built-in defaults and says which convention it applied. A missing instruction file is never reported as a blocker.

**Authorization follows the requested workflow.** A skill does not ask again before creating normal artefacts that the user requested or that its documented workflow necessarily produces, such as a working branch, commits, a pull request, or temporary files. It asks before creating an unrequested persistent repository or external artefact, such as a configuration file, migration, durable directory, label, or additional branch.

**Labels and CI gates are matched by meaning.** Sensitivity triage recognises labels such as `type:security` or `area:auth` as common spellings, not as a required taxonomy, and CI reporting names whatever checks the repository actually defines. When the repository states its own triage triggers, those replace the defaults.

**The security audit runs on a union**, `deterministic_rule OR reviewer_requests_security`. A rule in `security_review.always_when` of `.code-cycle.yml` matches changed paths, filenames and labels; a review may add an audit and may never remove one the rule activated. Skipping it therefore requires both halves to fail at once, and the rule costs nothing to run. A repository that declares no rule keeps the defaults, so a missing configuration is never the unsafe case. `scripts/security_gate.py` is the reference implementation.

**Stacks are detected, not assumed.** Package managers come from lockfiles, test commands from what the project defines, frameworks from what the repository contains.

## Output language

Skills write published text — pull-request comments, thread replies, commit messages, and their final response — in one language, resolved in this order:

1. an explicit request: `lang=es` in the invocation, or plain language such as "review in English";
2. the language of the repository's own instructions, when it has any;
3. the language of the issue, the pull-request description, and existing review comments;
4. English, when nothing above resolves.

So a Spanish repository gets Spanish reviews without configuration, and an explicit `lang=` always wins.

Machine-readable tokens never translate, in any language: the `REV-xxx` identifier, the severities `critical|high|medium|low`, the finding statuses `open|resolved|not_applicable`, the dispositions `valid|debatable|incorrect|obsolete|needs_clarification|-`, `blocks:yes|blocks:no`, the review and triage run lines, functional statuses, JSON keys, and enum-like values inside `ORCHESTRATION_RESULT`. Free-text values such as `summary`, `reason`, and `error` use the selected language. That keeps the structured result parseable without forcing human-readable prose into English.

## Requirements

The host must provide a way to load Agent Skills. The workflows also normally require:

- Git;
- Python 3 for the runtime and the scripts in `scripts/`;
- PyYAML, only to read `.code-cycle.yml`; a repository with no configuration file does not need it;
- authenticated tooling for the selected issue provider;
- authenticated tooling for the selected code host;
- access to the target repository and its change requests;
- the target repository's own test and verification dependencies.

For `cc-orca-orchestrator`, install and authenticate Orca separately. The package does not include Orca.

The optional Claude-to-Codex orchestration mode requires the external
`codex-plugin-cc` plugin and a working local Codex installation. Neither is a
dependency of the ordinary single-agent workflow.

The toolkit contains no credentials, tokens, private repository configuration, or customer data. Keep credentials in the provider's CLI, connector, or selected agent host, never in a skill file or `.code-cycle.yml`.

## Provider model

Issues and repositories are deliberately configured independently. The
supported combinations include:

| Issue provider | Code host |
|---|---|
| GitHub Issues | GitHub |
| GitHub Issues | Bitbucket |
| Plane | GitHub |
| Plane | Bitbucket |
| Jira | GitHub |
| Jira | Bitbucket |

Use explicit provider context when invoking a skill:

```text
Use cc-implement-issue for work item ENG-123 with issue_provider=plane,
code_host=bitbucket, repository=workspace/repository.
```

The skills also read an optional repository-local `.code-cycle.yml` containing
non-secret provider and repository defaults. Explicit invocation values win;
the code host may be inferred from an unambiguous `origin` remote. If the
issue provider or repository remains ambiguous, the skill stops before making
remote changes. See [docs/provider-contract.md](docs/provider-contract.md) for
the full contract, capability rules, identifier mapping, and migration details.

## Configuration reference

`.code-cycle.yml` is optional. Every key lives under `code_cycle`, holds no
secrets, and is never edited automatically. `run_cycle.py` refuses any key not
listed here, by name and before a stage is dispatched (see
[The runtime](#the-runtime)); the skills read the keys that belong to them.

| Key | Consumer | What it declares |
|---|---|---|
| `issue_provider` | `cc-provider-bootstrap` | `github`, `plane`, or `jira`. |
| `code_host` | `cc-provider-bootstrap` | `github` or `bitbucket`. |
| `issue.project`, `issue.selector` | `cc-provider-bootstrap` | The project scope and work item a provider check reads. |
| `repository.selector` | `cc-provider-bootstrap`, `run_cycle.py` | The repository on the code host; `run_cycle.py` uses it when `--repo` is not given. |
| `repository.default_branch` | `cc-provider-bootstrap` | The base branch for scoped reads. |
| `verification.cache_ttl`, `verification.recheck_on_failure` | `cc-provider-bootstrap` | How long a provider health check stays fresh (default `7d`). `recheck_on_failure` appears in the documented shape, but no component reads it: a live provider failure always forces a recheck. |
| `profiles.<name>.primary`, `profiles.<name>.fallback` | `run_cycle.py` via `router.load_profiles()`; `cc-orchestrator` | Where each of the eight profiles resolves. Only the profiles you declare change; the rest keep `DEFAULT_PROFILES` in `scripts/router.py`. An unknown profile name is refused. The models declared here are added to telemetry's accepted model set for that repository. |
| `routing.strategy` | `run_cycle.py` via `router.load_routing_strategy()` | `fixed` (default) or `measured`. Neither changes which target is selected; `measured` only feeds the repository's first-pass rate into the recorded cost estimate. |
| `routing.jev.mode`, `routing.jev.model`, `routing.jev.timeout_seconds` | `run_cycle.py` via `jev_shadow.load_jev_config()` | `disabled` (default) or `shadow`. In `shadow`, `implement` and `resolve` stages ask [TypeSafe](https://docs.typesafe.ai/) which of `cheap_coder` or `deep_coder` it would pick and record the answer beside the rules' choice; the suggestion never selects a profile, a target or a dispatch. `model` defaults to `jev-latest` and accepts it or a concrete version such as `jev-1.13.0`; the legacy `typesafe-ai/jev` value is normalized to `jev-latest`. `timeout_seconds` defaults to `3` (at most `10`). The key comes from `TYPESAFE_API_KEY`, with `JEV_API_KEY` as a compatibility fallback; without either, the stage records `unavailable`. See [docs/instrumentation.md](docs/instrumentation.md#shadow-suggestions). |
| `review.trusted_authors` | `cc-initial-review`, `cc-rereview`, `cc-resolve-comments` | The provider logins whose comments may advance recovered findings. An absent or empty list blocks recovery. |
| `security_review.always_when.paths`, `.files`, `.labels` | `cc-initial-review`, `cc-rereview`, `cc-resolve-comments`; implemented as a library in `scripts/security_gate.py` | When the security audit always runs. A declared list replaces its default; an absent block keeps the defaults, so configuration cannot switch the gate off. |
| `orchestration.mode` | `cc-orchestrator` | `auto` (default), `single_agent`, or `claude_codex`. |
| `calibration.profiles.<alias>` | `cc-orca-orchestrator` paired review | The `provider`, `model` and `effort` of each reviewer candidate, such as `reviewer_a` and `reviewer_b`. It never changes the routing mode; see [The runtime](#the-runtime). |

A profile target is one string, `executor:provider/model effort`:

```yaml
code_cycle:
  profiles:
    cheap_coder:
      primary: "codex:openai/gpt-6-luna high"
      fallback: "claude:anthropic/claude-sonnet-5 high"
```

The executors with an adapter today are `codex`, `claude` and `orca`. The toolkit
passes `model` and `effort` to the executor's CLI as written; it does not keep
its own list of permitted models or efforts, so the executor is what accepts or
rejects them. `calibration.profiles` uses separate `provider`, `model` and
`effort` fields rather than a target string, as in
[`cc-provider-bootstrap`](skills/cc-provider-bootstrap/SKILL.md).

## Model-update policy

**`DEFAULT_PROFILES` are the defaults for a new installation, not a restriction
on which models a repository may declare.**

A change is configuration, and needs no new release of this toolkit, when it:

- switches to another model already supported by the same executor and provider;
- changes effort, where the executor already supports that effort;
- changes `primary` or `fallback` on any profile;
- tries a candidate model through configuration.

A change may require a new release when it introduces:

- a new executor;
- a new provider;
- a changed CLI or output format;
- new authentication;
- new workspace or sandbox guarantees;
- a new capability type — `missing_capability` is a closed token set in
  `telemetry.FIELD_SPECS`, so a genuinely new capability is a code change by
  construction;
- an effort level the adapter does not yet understand;
- a change to the dispatch contract.

## Install from GitHub

For the recommended `npx skills` method, no clone is needed. Run it from the repository where you want to install the skills.

### Recommended installation: `npx skills`

If you have Node.js and `npx`, this is the simplest installation method. It works from the target repository and lets you choose the skills, agents, and scope.

```bash
# Interactive: choose the skills, agents, and project/global scope
npx skills add datacas/code-cycle-toolkit
```

When no `--global` flag is provided, the default scope is the current project. In an interactive terminal, `skills` can ask which skills and agents to use. To install the complete toolkit without prompts:

```bash
# All thirteen skills, all agents supported by the CLI, project scope
npx skills add datacas/code-cycle-toolkit --all --copy

# All thirteen skills, all supported agents, global scope
npx skills add datacas/code-cycle-toolkit --all --global --copy
```

`--all` means all skills and all agents known by the `skills` CLI. `--copy` copies the files instead of creating links, which is convenient on Windows and when the project should remain self-contained.

To install only selected skills for one or more agents:

```bash
# One skill for one agent
npx skills add datacas/code-cycle-toolkit \
  --skill cc-rereview \
  --agent codex \
  --copy

# Several skills for several agents
npx skills add datacas/code-cycle-toolkit \
  --skill cc-rereview cc-initial-review cc-pr-review \
  --agent claude-code codex \
  --copy
```

A cycle skill delegates to the supporting review and verification skills, so install `cc-pr-review`, `cc-code-review`, `cc-security-review`, and `cc-verify` alongside it. Without them the cycle skills still run, but they report the affected pass as degraded rather than passed.

Omit `--global` for project scope, or add it for global installation. Use `npx skills list` to inspect installed project skills and `npx skills list --global` for global skills.

Ask an agent for a telemetry summary in natural language, or invoke `cc-stats` directly (for example, `/cc-stats` on hosts that support skill commands). The default report covers the last 30 days. Ask for a different period such as the last 7 days, or for all recorded history. The report reads the local SQLite store in read-only mode and uses `code_cycle.repository.selector` from `.code-cycle.yml` to scope results. It shows measured values and sample sizes; unavailable metrics remain unknown.

The included Bash and PowerShell installers remain available when explicit destinations or only the three native host layouts in this repository are required. They copy files and do not use symlinks, so they also work on Windows without developer-mode or administrator privileges. Clone the toolkit before using them:

```bash
git clone https://github.com/datacas/code-cycle-toolkit.git
cd code-cycle-toolkit
```

### Direct installers: global installation

Global skills are available to the selected user across repositories.

#### macOS, Linux, WSL, or Git Bash

```bash
# One host
bash scripts/install.sh --agent claude --scope global
bash scripts/install.sh --agent codex --scope global
bash scripts/install.sh --agent opencode --scope global

# All supported hosts in this environment
bash scripts/install.sh --agent all --scope global
```

#### Windows PowerShell

```powershell
# One host
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Agent claude -Scope global
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Agent codex -Scope global
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Agent opencode -Scope global

# All supported hosts in this Windows profile
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Agent all -Scope global
```

PowerShell 7 can use `pwsh` instead of `powershell`.

Global destinations are:

| Target | Destination |
|---|---|
| Claude Code | `~/.claude/skills/` |
| Codex compatibility path | `~/.agents/skills/` |
| Codex traditional path | `~/.codex/skills/` |
| OpenCode | `~/.config/opencode/skills/` |
| Runtime, every host | `~/.code-cycle/runtime/` |

The Codex installation writes both Codex locations for compatibility. OpenCode also discovers `.agents/skills`.

### Repository-level installation

Repository-level skills are available only when the agent works in that repository. Run the installer from the toolkit clone and point it to the target repository:

#### macOS, Linux, WSL, or Git Bash

```bash
# All hosts in one repository
bash scripts/install.sh \
  --agent all \
  --scope project \
  --project-dir /path/to/target-repository

# Only Claude Code in the current repository
bash scripts/install.sh --agent claude --scope project --project-dir .
```

#### Windows PowerShell

```powershell
# All hosts in one repository
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 `
  -Agent all `
  -Scope project `
  -ProjectDir C:\path\to\target-repository

# Only OpenCode in the current repository
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 `
  -Agent opencode `
  -Scope project `
  -ProjectDir .
```

Repository destinations are:

| Target | Destination |
|---|---|
| Claude Code | `<repo>/.claude/skills/` |
| Codex | `<repo>/.agents/skills/` |
| OpenCode | `<repo>/.opencode/skills/` |
| Runtime, every host | `<repo>/.code-cycle/runtime/` |

These copied files can be committed when the team wants the skills to travel with the repository. Otherwise, use global installation.

The installer refuses to overwrite an existing skill. Add `--force` on Bash or `-Force` on PowerShell only when replacing an intentional previous installation:

```bash
bash scripts/install.sh --agent all --scope global --force
```

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Agent all -Scope global -Force
```

### The runtime

Skills are instructions; the runtime is the code that routes a stage, dispatches
it and records the row. It is installed once per scope rather than once per
host — three copies would be three answers to the question of which one a run
used — and it needs Python 3 plus its directory on `PYTHONPATH`:

```bash
export PYTHONPATH="$HOME/.code-cycle/runtime:${PYTHONPATH:-}"
```

```powershell
$env:PYTHONPATH = "$HOME\.code-cycle\runtime;$env:PYTHONPATH"
```

`scripts/runtime.manifest` lists what it contains, and both installers read that
one file, so neither can drift from the other. When `.code-cycle/.gitignore`
does not exist, the installer writes one containing `*`, so installed code
cannot be committed to the target repository by accident; an existing file is
left exactly as it is, including under `--force`, which asks to replace this
toolkit's files and not the target project's. A symlink on any path the
installer writes to is refused rather than followed: installing into a
repository is not authority to modify a file elsewhere on the disk.

Running one work item through a recorded cycle is `run_cycle.py`, in that same
directory:

```bash
python3 ~/.code-cycle/runtime/run_cycle.py \
  --repo owner/name --task API-7 --difficulty 2 --verifiability auto
```

`cc-stats` uses the installed runtime's `stats.py` component. It accepts
`--days N` for a different lookback window and `--all-time` for all available
history; `--format json` returns the same aggregate report as structured data.
Pass `--cwd <repository root>` to select the repository whose configured
telemetry should be reported.
The component uses the same configured repository identity and telemetry
database location as the cycle runtime, opens the database read-only, and
returns aggregates only. It never prints task identifiers, comments, prompts,
paths, diffs, or raw telemetry rows.

Each stage row also records the signals its router could have known before
choosing a model: the declared difficulty, verifiability and security flag; for
stages routed after the implementation, counts and area flags read off the diff
against `code_cycle.repository.default_branch`; the findings and failed
attempts reported earlier in the cycle; and, when `--verification available` or
`--verification unavailable` is given, whether the change can be verified
automatically. An unknown signal is left out, never recorded as zero. See
[Pre-routing signals](docs/instrumentation.md#pre-routing-signals).

What the run went on to produce is recorded on separate rows, never on the
dispatch rows that hold those signals: each verdict row carries the status,
findings by severity and a reported test result, and the closing row carries
first-pass approval, whether resolution was needed and how many rounds it took,
the first and final review verdicts, fallbacks and contract violations. Every
row of one run shares a `cycle_id`, and `stage_seq` ties a verdict to the
dispatch it reports on. An outcome nobody reported is absent, not approved,
passing or zero. See [Cycle outcomes](docs/instrumentation.md#cycle-outcomes).

For a rehearsal that must stay in a disposable linked Git worktree, pass both
`--cwd /path/to/worktree` and `--local-only`. The driver refuses to enable this
mode for the live repository, runs implementation only, records the resulting
human-intervention stop, adds the no-publish boundary to the implementation
prompt, and records `local_only` in each telemetry row. It is an orchestration
policy, not an operating-system sandbox: block network credentials and remote
Git access separately when a hard no-publish guarantee is required.

Publishing stages are explicit in the role contract. `implement` may comment,
create the change request, and push its working branch. `resolve` may comment
and push its working branch. `review` and `rereview` may only comment.
Security, bootstrap, verification, and runtime stages publish nothing, and
`--local-only` removes publication from every stage. Every stage's prompt states
its allowed operations and the prohibitions that apply to all stages: never
merge, force-push, delete remote refs, modify the base branch, close an issue or
change request without an explicit instruction, or publish for another stage.

**This publication boundary is behavioural, not technical.** Codex and Claude
keep their normal GitHub access: `gh`, `git`, their skills, MCP servers, and the
user's credentials. The toolkit does not stop an operation the role contract
does not list. It tells the agent the rule, and the published comments and
telemetry let a person check that the rule was followed. Use a separate
credential or repository permissions when you need a hard guarantee. Local
file access is different: each role's `WorkspacePolicy` is enforced where the
adapter can enforce it (see [the role workspace policy](docs/role-workspace-policy.md)).

Before a publishing dispatch, the runtime checks that publication is possible.
For a GitHub `origin` it checks `gh` authentication and the account's
repository permission. It then runs `git push --dry-run` against a temporary
preflight ref, which it does not create. A failure is recorded as
`missing_capability=publication_access` before the model CLI starts. This is a
readiness check, not a permission check per operation.

A publishing Codex stage keeps the stage's filesystem sandbox (read-only for
review, workspace write for implementation) and gets network access to GitHub
and Bitbucket through a CLI permission profile. That needs Codex CLI 0.138.0 or
newer; older versions record `publication_access` and do not dispatch. A
publishing Claude stage receives `gh` and `git` alongside its existing file
permission. Orca and new adapters fail closed until they declare how
publication access is granted.

Reading `.code-cycle.yml` needs PyYAML, the runtime's one optional dependency
(`pip install pyyaml`). Nothing else in the toolkit needs it: a repository with
no configuration file runs on the standard library alone, and when a file is
present but the parser is not, the run stops and says so rather than quietly
using the defaults.

It reads `.code-cycle.yml` from the working directory: `code_cycle.profiles`
overlays the built-in profiles, and `code_cycle.repository.selector` supplies
the repository when `--repo` is not given. A configuration that exists and
cannot be read stops the run instead of falling back to the defaults, because a
row recorded under the defaults while a file says otherwise describes a policy
nobody chose. `--no-config` asks for the defaults deliberately.

Every key under `code_cycle` must be one the toolkit knows, and an unknown one
stops the run by name before anything is dispatched — so a typo such as
`profles` is refused rather than silently running the built-in profiles. The
driver itself reads `repository`, `profiles` and `routing`. It recognises, and
deliberately does not interpret, the keys that belong elsewhere:
`issue_provider`, `code_host`, `issue` and `verification` (provider bootstrap),
`review` (the review skills), `security_review` (`scripts/security_gate.py`),
`orchestration` (`cc-orchestrator`) and `calibration` (`cc-orca-orchestrator`'s
paired review).

There is no `calibration.enabled` flag, and that is a decision rather than an
omission. A production run cannot enter a calibration by accident: the only way
in is the explicit `--mode calibration` argument, a calibration never
substitutes an arm, and a calibration dispatch demands proven readiness. A
`calibration` block in `.code-cycle.yml` never changes the mode; it only names
the reviewer candidates a paired review dispatches, and a test asserts that a
production run under it stays in production.

`code_cycle.routing.strategy` accepts `fixed` or `measured` and defaults to
`fixed`. Both currently follow the declared primary and fallback targets;
`measured` also uses the repository's first-pass rate for its recorded cost
estimate. If the sample is too small, it keeps the conservative default and
records that the rate is unknown. Neither strategy changes target selection.
Each stage row records the active strategy and cost inputs.

`code_cycle.routing.jev` is off unless `mode: shadow` is declared. In shadow
mode, `implement` and `resolve` stages also record Jev's suggested profile on a
separate `shadow` row; the rules still choose every profile, and a missing
`TYPESAFE_API_KEY` (or legacy `JEV_API_KEY`), a timeout or an error only
records its category. Any other key under `code_cycle.routing` is refused.

It probes the executors once, labels the work before routing anything, and runs
`implement → review → (resolve → rereview)*` with every stage going through the
recorder, so no dispatch can happen without leaving a row. It reads each
review's verdict from the structured result it asks the executor to emit, and
stops when that block is absent rather than guessing from an exit code. It
decides nothing else: no cost model, no learning, no rule that changes itself.

Pass `--no-runtime` on Bash or `-NoRuntime` on PowerShell to install the skills
alone. An installation without the runtime records nothing: `first_pass_rate()`
stays unmeasured, and the orchestrator is told to say so rather than report a
number no run produced.

### WSL and native Windows are separate environments

Running the Bash installer in WSL writes to the WSL user's home and installs skills for agents running inside WSL. Running the PowerShell installer writes to the Windows user profile and installs skills for native Windows agents. If both environments are used, run the appropriate installer once in each environment.

## Native host usage

The portable payload is always the `skills/` directory.

### Claude Code

The repository includes a Claude plugin manifest. To test it directly from the toolkit directory:

```bash
claude --plugin-dir .
```

After loading it, invoke a skill explicitly with a command such as `/cc-rereview`, or describe the task in natural language and let Claude select the skill.

#### Optional Claude + Codex orchestration

`cc-orchestrator` can use the external
[`codex-plugin-cc`](https://github.com/openai/codex-plugin-cc) integration to
divide the cycle by responsibility:

```text
Claude: provider bootstrap and implementation
Codex: initial review
Claude: resolve comments
Codex: rereview
Claude: final validation and manual-merge handoff
```

Code Cycle Toolkit does not include or install that plugin. Install and prepare
it separately in Claude Code, following its official instructions:

```text
/plugin marketplace add openai/codex-plugin-cc
/plugin install codex@openai-codex
/reload-plugins
/codex:setup
```

Install Code Cycle Toolkit for both Claude Code and Codex so delegated Codex
sessions can load the same review skills:

```bash
npx skills add datacas/code-cycle-toolkit --all --copy
```

Select the mode explicitly:

```text
Use cc-orchestrator for issue 123 with orchestration_mode=claude_codex.
```

Or save the non-secret project preference after confirmation:

```yaml
code_cycle:
  orchestration:
    mode: claude_codex
```

Supported values are `auto`, `single_agent`, and `claude_codex`. In Claude Code,
the first applicable run briefly explains the optional integration. If the
plugin is ready but the project has no saved preference, the orchestrator asks
once whether to use the mixed mode and offers to update `.code-cycle.yml`. The
notice acknowledgement is stored outside the repository only with permission.
The plugin's absence never blocks `auto`; an explicitly configured
`claude_codex` mode does block before implementation when Codex cannot be used.

Codex performs the complete `cc-initial-review` and `cc-rereview` stages and may
publish their required review comments, but it must not modify product code or
the working tree. Claude validates the returned structured result and confirms
that the branch is unchanged before continuing. See the
[adapter contract](skills/cc-orchestrator/references/codex-plugin-cc.md) for the
full discovery, handoff, and failure rules.

### Codex

Use the included installer for global or repository-level installation. Codex can also install individual skill directories from a public GitHub repository when only one workflow is wanted. The `.codex-plugin/plugin.json` manifest is included for Codex plugin-aware environments.

Examples:

```text
Use cc-implement-issue for GitHub issue 123 in owner/repository and open the pull request.
Use cc-implement-issue for Plane work item ENG-123 with issue_provider=plane, code_host=bitbucket, repository=workspace/repository.
Use cc-initial-review on change request 456 in owner/repository.
```

### OpenCode

The repository includes `opencode.jsonc` pointing at `./skills`. When OpenCode starts in the toolkit repository, it can discover the skills directly. For another repository, use the repository-level installer or configure the toolkit's `skills/` directory in that project's OpenCode configuration.

Examples:

```text
Use cc-resolve-comments to address the review feedback on pull request 456.
Run cc-orchestrator for issue 123 and stop after the pull request is ready for manual merge.
```

Natural-language invocation is the portable way to use the toolkit. Slash-command syntax is host-specific.

## Usage examples

### Implement an issue

```text
Use cc-implement-issue to implement issue 123 in owner/repository with issue_provider=github and code_host=github. Follow the repository instructions, run the relevant tests, and open a pull request. Do not merge it.
```

### Perform an initial review

```text
Use cc-initial-review on change request 456 in owner/repository with code_host=bitbucket. Review the accumulated diff, validate findings with execution where possible, publish one consolidated comment, and return the structured result.
```

### Resolve review comments

```text
Use cc-resolve-comments on change request 456 in owner/repository. Resolve valid findings, preserve the existing REV identifiers, run the related checks, and publish the result.
```

### Rereview a changed pull request

```text
Use cc-rereview on change request 456 in owner/repository after the latest fixes. Verify previous findings, inspect the complete accumulated diff, check CI, and publish the updated review.
```

### Run a single pass

```text
Use cc-code-review on the current working tree against main.
Use cc-security-review on the changes in this branch.
Use cc-verify to check that the fix in src/example.ext actually works.
Use cc-run to bring this project up and tell me the URLs.
```

### Run the generic complete cycle

```text
Use cc-orchestrator for issue 123 in owner/repository with max_iterations=6. Implement the issue, review the pull request, resolve valid findings, rereview it, and stop at READY_FOR_MANUAL_MERGE. Never merge.
```

The generic orchestrator uses native workers or subagents only when the host exposes a known mechanism. Otherwise it runs the stages sequentially in the current session.

To require the optional Claude-to-Codex split when running in Claude Code:

```text
Use cc-orchestrator for issue 123 with orchestration_mode=claude_codex. Claude implements and resolves comments; Codex reviews and rereviews. Stop at READY_FOR_MANUAL_MERGE.
```

### Run the Orca-supervised cycle

```text
Use cc-orca-orchestrator for issue 123 in owner/repository with implementer=codex reviewer=claude max_iterations=6.
```

Use this only in an environment with Orca. It creates and manages Orca Runs, Tasks, workers, result files, and the shared pull-request branch. It asks before creating the results directory, and it never merges.

### Force a language

```text
Use cc-initial-review on pull request 456 with lang=en.
```

## Result and review conventions

- Review findings use stable IDs such as `REV-001`.
- Every published finding carries the header `#### [REV-004] · medium · resolved · valid · blocks:yes — Short title`, whose five leading tokens stay in English in every language. This is what makes a published comment recoverable by the next run.
- The fourth token is the disposition: what the first resolver made of the finding, frozen from then on. A reviewer publishes `-`, meaning not triaged yet. A header with four tokens and no disposition predates this contract, reads as `-`, and is never a reason to block an open change request.
- Each published review opens with a run line such as `#### [CCR-20260918-001] · senior_reviewer · anthropic/sonnet-5→sonnet-5 · high · schema:1`, and a resolver adds a `[CCT-xxx]` line carrying the commit every finding was judged against. See [docs/instrumentation.md](docs/instrumentation.md).
- `ORCHESTRATION_RESULT` is opt-in: it is emitted only when requested, when a delegated host contract requires it, or when the status is `BLOCKED`/`FAILED` and no comment could serve as the record.
- Both orchestrators return the same result envelope, so one consumer parses either.
- The configured code host remains authoritative for change-request state, comments, threads, commits, and CI; the configured issue provider remains authoritative for work-item state.
- A missing or unverified check is reported as such; it is never silently promoted to success.
- `READY_FOR_MANUAL_MERGE` means a human still owns the merge decision.

## Validation

Run the package validator before publishing or creating a release:

```bash
python3 scripts/validate-package.py
python3 -m unittest discover -s tests
```

On Windows, use:

```powershell
py -3 .\scripts\validate-package.py
```

The validator checks all thirteen skills, portable frontmatter, names and description limits, that the sections duplicated across skills have not drifted apart, known fixed-language output mistakes and literal-output directives, manifest name and version agreement, JSONC parsing, README coverage, and possible private data. Its negative-path tests exercise these failure modes:

```bash
python3 -m unittest discover -s tests -v
```

The repository also supports native manifest validation:

```bash
claude plugin validate .
```

Codex plugin-aware environments can use the `.codex-plugin/plugin.json` manifest; the included package validator verifies its JSON shape.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the skill-authoring rules, including how the deliberately duplicated sections are kept in sync. Security reports go through [SECURITY.md](SECURITY.md). Released changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## Release checklist

- Confirm that no repository-specific names, local paths, credentials, or customer data are present.
- Run the package validator and `claude plugin validate .`.
- Confirm both plugin manifests carry the version being released.
- Add the release to `CHANGELOG.md`.
- Test Bash installation on Linux, macOS, and WSL.
- Test PowerShell installation on Windows.
- Test global and repository-level installation for each host.
- Test one manual skill and one complete cycle in each supported host.
- Publish versioned Git tags and release archives.
