# Code Cycle Toolkit

Code Cycle Toolkit is a portable collection of Agent Skills for taking a GitHub issue through implementation, pull-request review, targeted resolution, and rereview.

It is designed to work with Codex, Claude Code, and OpenCode on Linux, macOS, Windows, WSL, Git Bash, and similar environments. The reusable instructions live in `skills/`; the host-specific manifests and installers are only compatibility layers.

`cc` means **Code Cycle**.

This project is licensed under the MIT License. See [LICENSE](LICENSE).

## What it does

- Implements one GitHub issue and takes it to a tested pull request.
- Performs a complete initial pull-request review.
- Tracks review findings with stable `REV-xxx` identifiers.
- Resolves valid review comments with targeted changes and evidence-backed verification.
- Rereviews the accumulated pull request after changes.
- Coordinates the complete issue-to-merge-readiness cycle.
- Publishes consolidated pull-request comments when the workflow requires them.
- Preserves a manual merge boundary: the toolkit never merges a pull request.

## What it does not do

- It is not a GitHub replacement, CI service, deployment system, or issue tracker.
- It does not provide GitHub credentials or bypass repository permissions.
- It does not invent missing tests, project policies, or verification commands.
- It does not silently treat unavailable tools, skipped checks, or unverified behavior as success.
- The generic orchestrator does not assume that the host has workers, subagents, worktrees, or a particular delegation API.
- `cc-orca-orchestrator` is not generic: it requires the Orca supervised worker API.

## Skills

| Skill | Use it for | Changes product code? |
|---|---|---:|
| `cc-implement-issue` | Implement an issue, verify it, and open a pull request. | Yes |
| `cc-initial-review` | Review the full pull-request diff and publish findings. | No |
| `cc-resolve-comments` | Triage review feedback and implement valid fixes. | Yes |
| `cc-rereview` | Rereview the accumulated pull request after changes. | No |
| `cc-orchestrator` | Coordinate the complete cycle with native host delegation or a sequential fallback. | Only through delegated stages |
| `cc-orca-orchestrator` | Coordinate the cycle through Orca Runs, Tasks, and Workers. | Only through delegated stages |

Review skills do not approve or merge code. The orchestrators stop at a validated `READY_FOR_MANUAL_MERGE` state.

## Requirements

The host must provide a way to load Agent Skills. The workflows also normally require:

- Git;
- GitHub CLI (`gh`) authenticated for the target repository;
- access to the target repository and its pull requests;
- the target repository's trusted instructions, such as `AGENTS.md`, `CLAUDE.md`, or contribution documentation;
- the target repository's own test and verification dependencies.

For `cc-orca-orchestrator`, install and authenticate Orca separately. The package does not include Orca.

The toolkit contains no credentials, tokens, private repository configuration, or customer data. Keep credentials in GitHub CLI and the selected agent host, never in a skill file.

## Install from GitHub

Clone or download the repository first:

```bash
git clone https://github.com/datacas/code-cycle-toolkit.git
cd code-cycle-toolkit
```

The included installers copy the same six skills into the selected host directories. They do not use symlinks, so they also work on Windows without developer-mode or administrator privileges.

### Global installation

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

These copied files can be committed when the team wants the skills to travel with the repository. Otherwise, use global installation.

The installer refuses to overwrite an existing skill. Add `--force` on Bash or `-Force` on PowerShell only when replacing an intentional previous installation:

```bash
bash scripts/install.sh --agent all --scope global --force
```

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Agent all -Scope global -Force
```

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

### Codex

Use the included installer for global or repository-level installation. Codex can also install individual skill directories from a public GitHub repository when only one workflow is wanted. The `.codex-plugin/plugin.json` manifest is included for Codex plugin-aware environments.

Examples:

```text
Use cc-implement-issue for GitHub issue 123 in owner/repository and open the pull request.
Use cc-initial-review on pull request 456 in owner/repository.
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
Use cc-implement-issue to implement issue 123 in owner/repository. Follow the repository instructions, run the relevant tests, and open a pull request. Do not merge it.
```

### Perform an initial review

```text
Use cc-initial-review on pull request 456 in owner/repository. Review the accumulated diff, validate findings with execution where possible, publish one consolidated comment, and return the structured result.
```

### Resolve review comments

```text
Use cc-resolve-comments on pull request 456 in owner/repository. Resolve valid findings, preserve the existing REV identifiers, run the related checks, and publish the result.
```

### Rereview a changed pull request

```text
Use cc-rereview on pull request 456 in owner/repository after the latest fixes. Verify previous findings, inspect the complete accumulated diff, check CI, and publish the updated review.
```

### Run the generic complete cycle

```text
Use cc-orchestrator for issue 123 in owner/repository with max_iterations=6. Implement the issue, review the pull request, resolve valid findings, rereview it, and stop at READY_FOR_MANUAL_MERGE. Never merge.
```

The generic orchestrator uses native workers or subagents only when the host exposes a known mechanism. Otherwise it runs the stages sequentially in the current session.

### Run the Orca-supervised cycle

```text
Use cc-orca-orchestrator for issue 123 in owner/repository with implementer=codex reviewer=claude max_iterations=6.
```

Use this only in an environment with Orca. It creates and manages Orca Runs, Tasks, workers, result files, and the shared pull-request branch. It never merges.

## Result and review conventions

- Review findings use stable IDs such as `REV-001`.
- `ORCHESTRATION_RESULT` is emitted only when requested or required by a delegated host contract.
- GitHub remains authoritative for pull-request state, comments, threads, commits, and CI.
- A missing or unverified check is reported as such; it is never silently promoted to success.
- `READY_FOR_MANUAL_MERGE` means a human still owns the merge decision.

## Validation

Run the package validator before publishing or creating a release:

```bash
python3 scripts/validate-package.py
```

On Windows, use:

```powershell
py -3 .\scripts\validate-package.py
```

The validator checks all six skills, portable frontmatter, names, both plugin manifests, OpenCode configuration, legacy names, obvious private-data patterns, and metadata sidecars.

The repository also supports native manifest validation:

```bash
claude plugin validate .
```

Codex plugin-aware environments can use the `.codex-plugin/plugin.json` manifest; the included package validator verifies its JSON shape.

## Public-release checklist

- Replace `<owner>` examples only in release documentation when the public repository URL is known.
- Confirm that no repository-specific names, local paths, credentials, or customer data are present.
- Choose and add an open-source license before publishing.
- Run the package validator and native Claude validation.
- Test Bash installation on Linux, macOS, and WSL.
- Test PowerShell installation on Windows.
- Test global and repository-level installation for each host.
- Test one manual skill and one complete cycle in each supported host.
- Publish versioned Git tags and release archives.
