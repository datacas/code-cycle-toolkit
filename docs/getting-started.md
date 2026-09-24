# Getting started

From nothing to a first reviewed pull request. No knowledge of the architecture is assumed.

**On this page:** [Requirements](#requirements) · [Skills and runtime](#skills-and-runtime) · [Install](#install) · [Authenticate providers](#authenticate-providers) · [Configure](#configure-optional) · [First run](#first-run) · [What you get](#what-you-get) · [Where it stops](#where-it-stops) · [Troubleshooting](#troubleshooting)

## Requirements

| Requirement | Needed for | Notes |
|---|---|---|
| An agent host | everything | Claude Code, Codex, or OpenCode. The host loads the skills. |
| Git | everything | Branches, diffs, commits. |
| Issue-provider tooling, authenticated | reading work items | `gh` for GitHub Issues, or a configured connector, CLI, or MCP server for Plane or Jira |
| Code-host tooling, authenticated | pull requests, comments, checks | `gh` for GitHub, or a configured connector, CLI, or API tooling for Bitbucket |
| Your project's own dependencies | verification | The toolkit runs your tests. It doesn't install your stack. |
| Python 3 | the runtime, `run_cycle.py`, `cc-stats` | Standard library only. |
| PyYAML (`pip install pyyaml`) | reading `.code-cycle.yml` from the runtime | Only when a `.code-cycle.yml` exists. The runtime then stops if PyYAML is missing rather than ignoring the file. |
| Node.js / `npx` | `npx skills` installation only | Not needed with the bundled installers. |
| Codex CLI and/or Claude Code CLI | `run_cycle.py` dispatching | Codex CLI 0.138.0 or later for stages that publish. |
| Orca | `cc-orca-orchestrator` only | Installed and authenticated separately. |
| `codex-plugin-cc` + Codex | Claude + Codex mode only | Installed separately inside Claude Code. |

The toolkit contains no credentials. Keep them in the provider CLI, connector, or agent host, never in a skill file or `.code-cycle.yml`.

## Skills and runtime

An installation has two parts:

| Part | What it is | Installed by | Without it |
|---|---|---|---|
| **Skills** | Markdown instructions in `skills/` that the agent host loads | `npx skills`, `install.sh`, `install.ps1` | Nothing works. |
| **Runtime** | Python modules for routing, dispatch, telemetry, and stats (listed in `scripts/runtime.manifest`) | `install.sh` / `install.ps1` only | Skills still run. Nothing is recorded, `cc-stats` has no data, and `run_cycle.py` isn't available. |

`npx skills` installs skills only. To get both, use the one-command installer (Option A), or the installer from a clone.

## Install

### Option A: one command (skills + runtime)

Downloads a release and runs the bundled installer for you. No clone needed. It always passes `--force`, so running it again updates an existing installation.

```bash
# latest release, every host, for your user
curl -fsSL https://raw.githubusercontent.com/datacas/code-cycle-toolkit/main/scripts/get.sh | sh

# a specific version, or other installer options
curl -fsSL https://raw.githubusercontent.com/datacas/code-cycle-toolkit/main/scripts/get.sh | sh -s -- --version v0.3.0
curl -fsSL https://raw.githubusercontent.com/datacas/code-cycle-toolkit/main/scripts/get.sh | sh -s -- --agent claude --scope project --project-dir .
```

```powershell
# latest release, every host, for your user
irm https://raw.githubusercontent.com/datacas/code-cycle-toolkit/main/scripts/get.ps1 | iex

# with parameters
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/datacas/code-cycle-toolkit/main/scripts/get.ps1))) -Version v0.3.0 -Agent claude
```

`--version` / `-Version` accepts `latest` (the default), `main`, or a tag such as `v0.3.0`. `CODE_CYCLE_VERSION` sets the same default. It needs `curl` and `tar` on Unix. As with any piped installer, you can download [`scripts/get.sh`](../scripts/get.sh) and read it first.

### Option B: installer from a clone (skills + runtime)

Clone once:

```bash
git clone https://github.com/datacas/code-cycle-toolkit.git
cd code-cycle-toolkit
```

**macOS, Linux, WSL, Git Bash:**

```bash
# every host, for your user
bash scripts/install.sh --agent all --scope global

# one host only
bash scripts/install.sh --agent claude --scope global

# into one repository instead of your user profile
bash scripts/install.sh --agent all --scope project --project-dir /path/to/repository
```

**Windows PowerShell** (`pwsh` works too):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Agent all -Scope global
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Agent all -Scope project -ProjectDir C:\path\to\repository
```

**`PYTHONPATH` is optional.** `run_cycle.py` and `cc-stats` run the runtime's scripts by path and find its modules without it. Add the runtime to `PYTHONPATH` only if other Python code (for example an agent composing stages with `CycleRecorder`) has to `import` its modules:

```bash
export PYTHONPATH="$HOME/.code-cycle/runtime:${PYTHONPATH:-}"
```

Where files land:

| Target | Global | Project |
|---|---|---|
| Claude Code | `~/.claude/skills/` | `<repo>/.claude/skills/` |
| Codex | `~/.agents/skills/` and `~/.codex/skills/` | `<repo>/.agents/skills/` |
| OpenCode | `~/.config/opencode/skills/` | `<repo>/.opencode/skills/` |
| Runtime (one copy for all hosts) | `~/.code-cycle/runtime/` | `<repo>/.code-cycle/runtime/` |

Installer behaviour:

- An existing skill is never overwritten unless you pass `--force` (`-Force`).
- `--no-runtime` (`-NoRuntime`) installs skills only.
- The installer writes `.code-cycle/.gitignore` containing `*` when none exists. In a project install, this keeps the runtime from being committed by accident. An existing file is left alone, even with `--force`.
- A symlink on any path the installer writes to is refused, not followed.
- WSL and native Windows are separate environments. Run the matching installer in each one you use.

All installer flags are listed in [Configuration → Command-line flags](configuration.md#command-line-flags).

### Option C: `npx skills` (skills only, no clone)

```bash
# interactive: choose skills, agents, and scope
npx skills add datacas/code-cycle-toolkit

# everything, current project
npx skills add datacas/code-cycle-toolkit --all --copy

# everything, for your user
npx skills add datacas/code-cycle-toolkit --all --global --copy

# selected skills for selected agents
npx skills add datacas/code-cycle-toolkit \
  --skill cc-initial-review cc-rereview cc-pr-review cc-security-review cc-verify \
  --agent claude-code codex --copy
```

`--copy` copies files instead of linking them, which avoids symlink problems on Windows. Use `npx skills list` (or `--global`) to see what is installed.

> [!IMPORTANT]
> A cycle skill delegates to `cc-pr-review`, `cc-code-review`, `cc-security-review`, and `cc-verify`. Install them alongside it. Without them the cycle still runs, but it reports those passes as **degraded**, not passed.

### Option D: load the checkout directly

- **Claude Code:** `claude --plugin-dir .` from the toolkit clone loads the bundled `.claude-plugin/plugin.json`.
- **Codex:** plugin-aware environments can use `.codex-plugin/plugin.json`.
- **OpenCode:** `opencode.jsonc` points at `./skills`, so OpenCode started inside the clone discovers them.

## Authenticate providers

The toolkit uses tooling you already authenticated and never stores credentials.

| Provider | Role | What must work |
|---|---|---|
| GitHub | issue provider and/or code host | `gh auth status`, and `gh repo view owner/repo` for the target repository |
| Bitbucket | code host | Your Bitbucket connector, CLI, or API tooling can read the repository and its pull requests. GitHub access doesn't count. |
| Plane | issue provider | A configured Plane MCP server or connector can read the workspace/project and the work item |
| Jira | issue provider | Your Jira connector, CLI, or API tooling can read the project and the work item |

`cc-provider-bootstrap` runs these checks read-only at the start of every cycle and caches a successful result for 7 days (see [Providers](provider-contract.md#setting-up-each-provider)). If a check fails, the cycle stops with `BLOCKED` and names what is missing.

## Configure (optional)

Nothing is required. When values are missing, `cc-provider-bootstrap` infers what is unambiguous from the `origin` remote. It asks you one grouped question for the rest, then shows you the proposed `.code-cycle.yml` and writes it only if you confirm. If you decline, the values are used for this run only.

A typical starting file:

```yaml
code_cycle:
  issue_provider: github        # github | plane | jira
  code_host: github             # github | bitbucket
  repository:
    selector: owner/repository
    default_branch: main
  review:
    trusted_authors:            # logins whose comments may advance findings
      - your-github-login
```

> [!TIP]
> Keep `review.trusted_authors` in the file. When it is missing, the bootstrap proposes the login your code-host tooling is authenticated as (on GitHub, `gh api user --jq .login`), since the stages publish with that identity. Add any other reviewer or bot whose comments should count. A later review round can only recover the findings from earlier rounds when their comments were published by a trusted author. Without it, recovery stops with `BLOCKED`. See [Review lifecycle → Trusted authors](review-cycle.md#trusted-authors).

Every other key (models, routing, security rules, orchestration mode) is covered in [Configuration](configuration.md).

## First run

Open your agent in the target repository. Invoking skills in natural language works in every host. Slash commands such as `/cc-orchestrator` are host-specific.

**The whole cycle:**

```text
Use cc-orchestrator for issue 123. Stop at READY_FOR_MANUAL_MERGE.
```

**One stage at a time:**

```text
Use cc-implement-issue for issue 123 and open a pull request.
Use cc-initial-review on pull request 456.
Use cc-resolve-comments on pull request 456.
Use cc-rereview on pull request 456.
```

**Other providers:**

```text
Use cc-orchestrator for work item ENG-123 with issue_provider=jira code_host=bitbucket repository=workspace/repo.
```

**From the shell, recorded in telemetry** (requires the runtime and the Codex and/or Claude CLIs):

```bash
python3 ~/.code-cycle/runtime/run_cycle.py --task 123 --difficulty 2 --verifiability auto
```

What each of these does, step by step, is in [Workflows](workflows.md).

## What you get

- **A branch and a pull request** linked to the work item (`Closes #123` for GitHub Issues when the repository uses it; the native key or URL for Plane and Jira).
- **Review comments** on the pull request: one consolidated comment per review round. Each opens with a run line (`[CCR-…]`) and lists findings with stable headers such as `#### [REV-001] · high · open · - · blocks:yes — Title`. See [Review lifecycle](review-cycle.md).
- **A resolution comment** per fix round: what was fixed, what was rejected and why, what was verified, and what couldn't be.
- **A final status** from the orchestrator: `READY_FOR_MANUAL_MERGE`, `HUMAN_INTERVENTION`, `BLOCKED`, or `FAILED`.
- **Telemetry rows**, when the runtime drives the stages. Read them with `cc-stats`. See [Telemetry](telemetry.md).

## Where it stops

| Stop | Why | What to do |
|---|---|---|
| `READY_FOR_MANUAL_MERGE` | The reviewed head is current, required checks passed, no blocking finding is open | Review and merge yourself |
| `HUMAN_INTERVENTION` | `max_iterations` (default 6) reached, or a round made no progress (same head, same open findings) | Read the latest review and decide |
| `BLOCKED` | Access, information, or an external condition is missing (for example an unauthenticated provider, an ambiguous repository, or a verification stop condition) | Fix what the summary names, then rerun |
| `FAILED` | An unexpected technical failure | Read the reported cause |

It never merges, never force-pushes, never deletes remote refs, and never closes a work item without an explicit instruction.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `BLOCKED` before any branch is created | The provider pair or repository is ambiguous, or a provider check failed. Pass `issue_provider=`, `code_host=`, `repository=` explicitly. |
| Rereview says it can't recover findings | `review.trusted_authors` is missing or doesn't include the account that published the review. |
| A review pass is reported as degraded | The supporting skill it delegates to isn't installed. |
| `cc-stats` or `cc-orchestrator` says the runtime isn't installed | You installed with `npx skills` or `--no-runtime`. Run the one-command installer. |
| `run_cycle.py` stops with `publication_access` | `gh` isn't authenticated, the account can't push, Codex CLI is older than 0.138.0, or the working tree isn't on a named branch. |
| `run_cycle.py` refuses a key in `.code-cycle.yml` | Unknown keys are refused by name to catch typos. See [Configuration](configuration.md). |

---

[← Documentation index](README.md) · [↑ Project README](../README.md) · [Workflows →](workflows.md)
