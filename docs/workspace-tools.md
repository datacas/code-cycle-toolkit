# Optional workspace tools

Some host tools make the cycle faster or more precise. This page lists the ones
the toolkit is written to work with and how it uses them. It also covers how
they interact with dispatched stages and what to watch for.

**On this page:** [Status](#status) · [Capabilities](#capabilities) · [How the toolkit uses them](#how-the-toolkit-uses-them) · [Dispatched stages](#dispatched-stages) · [Privacy](#privacy) · [Known pitfalls](#known-pitfalls)

## Status

> [!IMPORTANT]
> **These tools are recommended, external, and optional.** The toolkit never
> installs them. The installers (`scripts/install.sh`, `scripts/install.ps1`,
> `scripts/get.sh`, `scripts/get.ps1`) install the skills and the runtime only,
> `npx skills` installs the skills only, and no plugin manifest depends on any
> of these tools. Their absence is never an error, a warning, or a
> blocker.

Install and configure each one through its own project, for the host you use.
This page links them and doesn't pin versions or endorse any tool's own
configuration details.

## Capabilities

| Capability | Recommended | Role in the cycle | Fallback |
|---|---|---|---|
| Semantic code navigation | [Serena](https://github.com/oraios/serena) | Symbols, references, implementations, diagnostics, safe edits | Text search and reading |
| Persistent memory | [AgentMemory](https://github.com/rohitg00/agentmemory) | Decisions, traps, and corrections across sessions | The repository and change-request comments |
| Repository knowledge graph | [Graphify](https://github.com/Graphify-Labs/graphify) | System-level impact and relationships, when a graph exists for the commit | Navigation and reading |
| Command-output compaction | [RTK](https://github.com/rtk-ai/rtk) | Smaller CLI output | Plain output |
| Large-output processing | [context-mode](https://github.com/mksglu/context-mode) | Filtering and aggregating logs, JSON, and CI output locally | Shell filters (`rg`, `jq`, `tail`) |
| Library documentation | [Context7](https://github.com/upstash/context7) | Current, version-specific documentation for external libraries, frameworks, SDKs, and CLIs | The project's installed version, its lock file, and the vendor's official documentation |

Each tool supplies context, never authority. The repository, provider state,
executed evidence, and the user's current instruction decide. A recalled note,
a graph edge, or a library document shows where to look; it never replaces
looking. When a library document disagrees with the project, the installed
version and its observed behaviour are authoritative.

## How the toolkit uses them

The toolkit reaches these tools through the host, never through the runtime:

- The skills' shared `## Workspace tools and evidence` section states
  capability preferences, such as semantic navigation for symbol questions,
  local processing for large output, and current documentation for an external
  dependency. It names no product. The validator rejects a product name in that
  section.
- Each tool's own host integration, such as MCP server instructions or hooks,
  routes the matching request to it.
- There is no detection and no configuration key. Nothing in `.code-cycle.yml`
  turns a tool on or off, and `cc-provider-bootstrap` doesn't check for any of
  them.

A repository can add its own routing rules in `AGENTS.md` or `CLAUDE.md`. The
skills read those files and prefer them over their defaults.

## Dispatched stages

Stages started by `run_cycle.py` run a non-interactive host session
(`codex exec`, `claude -p`, or Orca). This changes a few things:

- **Availability follows the host.** A tool is available in a dispatched stage
  only if it is configured for that host and permitted by the host's own
  settings. The toolkit grants no tool permissions, and a tool that needs an
  interactive approval isn't available.
- **Isolated review clones are elsewhere.** A read-only Claude stage runs in a
  disposable clone outside the project directory
  ([Role workspace policy](role-workspace-policy.md)). Project-scoped tool
  state, such as a semantic index, a knowledge graph, or project memory keyed
  by path, may not apply there.
- **Evidence stays complete under compaction.** The runtime hands each review,
  resolve, and rereview stage the complete diff as a file Git wrote itself. The
  skills treat compacted or truncated output as incomplete
  ([Verification → Complete evidence](verification.md#complete-evidence)).
- **Read-only is checked by its result.** For Claude, the harness compares the
  implementer checkout before and after the stage and fails it on a change,
  whatever made the write. A tool launched by the host, such as an MCP server
  with editing functions, isn't confined by a sandbox flag meant for the
  agent's own commands. Extending the after-check to every read-only dispatch
  is tracked in
  [#91](https://github.com/datacas/code-cycle-toolkit/issues/91).

## Privacy

Host memory and output tools may capture dispatched sessions, including
prompts, diffs, and code, through their own hooks. That capture is the host's
configuration. It falls outside the toolkit's telemetry guarantees
([Telemetry → What is never recorded](telemetry.md#what-is-never-recorded)).
Review what each tool stores, and where, before using it on a private
repository. The skills never ask a memory tool to save secrets, diffs, or
review prose.

## Known pitfalls

- **Compacted diffs.** A command-rewriting hook can shorten `git diff` with
  only a marker to show it. The measurement is in
  [Verification → Complete evidence](verification.md#complete-evidence).
- **Memory scoping.** Memory keyed to the wrong project, or shared across
  projects, surfaces decisions that don't apply. Scope memory per project.
- **Stale graphs.** A knowledge graph built for an older commit gives hints,
  never evidence. Stages never build or refresh one inside their workspace.
- **Version drift in library documentation.** Documentation for the latest
  release may describe APIs the project's installed version lacks. Query the
  version the project pins.
- **Tool directories.** Tools create directories such as `.serena/` or
  `graphify-out/`. Ignore them in your global gitignore
  (`git config --global core.excludesFile`), not in each repository's, and not
  in the toolkit's.

---

[← Telemetry](telemetry.md) · [↑ Documentation index](README.md) · [Role workspace policy →](role-workspace-policy.md)
