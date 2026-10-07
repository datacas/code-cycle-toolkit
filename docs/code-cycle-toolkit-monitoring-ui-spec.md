# Code Cycle Toolkit — Monitoring & Adaptive UI Specification

**Status:** Draft specification for implementation planning and issue generation  
**Target repository:** `datacas/code-cycle-toolkit`  
**Primary purpose:** Define a monitoring/runtime presentation subsystem that allows an implementation agent to decompose the work into GitHub issues without requiring additional product decisions for the core architecture.

---

## 1. Objective

Create a monitoring and presentation subsystem for Code Cycle Toolkit that exposes the state of cycles, tasks, agents, worktrees, findings, tests, Git/PR state, receipts, tools, model/session telemetry, and optional Orca orchestration through multiple user interfaces.

The same underlying runtime state must be usable from:

- Herdr
- Claude Code
- Codex
- standalone CLI/TUI
- future integrations

The monitoring system must work in all of these combinations:

- with Herdr or without Herdr;
- with Claude Code or Codex;
- with Orca or without Orca;
- with one agent or several agents;
- with Code Cycle orchestration or only the lower-level toolkit/runtime facilities.

The implementation must avoid coupling monitoring logic to any specific frontend.

### Host-level integration and automatic discovery

Integrations are installed and detected at the host level, never enabled by
listing repositories in Code Cycle configuration. Once the Herdr integration
is installed in the scope supported by Herdr, it discovers every open
workspace, repository, worktree, agent and session exposed by Herdr. Opening a
new repository in Herdr must not require a Code Cycle setup step.

The host supplies its live workspace/session inventory. Code Cycle correlates
that context with local Git/worktree state and any registered Code Cycle run.
An open workspace remains visible when no Code Cycle cycle is active; the
monitor shows available host, repository, branch and Git state, then enriches
it with task, cycle, stage, findings, receipts, tests and Orca data when those
sources are present. Unknown test or session fields remain unavailable.

Official integrations should ship with the Code Cycle Toolkit release and be
installed once per host at the host-supported global or user scope. A separate
package/repository is justified only by a concrete host packaging constraint.
No repo allowlist, per-repository enablement, or manual project registration is
part of this design.

---

## 2. Product concept

The system has a shared core and host-level adapters, all shipped from the
Code Cycle Toolkit repository where host packaging permits:

```text
Herdr workspaces ── Herdr plugin ──────┐
Claude session/cwd ─ Claude Mod ───────┤
Codex session/cwd ── Codex adapter ────┼── Monitor Core ── snapshot/events ── UI/CLI
Code Cycle runtime ────────────────────┤
Git / checks / Orca ──────────────────┘
```

The central ownership rule is:

> Code Cycle owns cycle state. Hosts own their workspace/session inventory. The monitor correlates both; frontends render or control the resulting view.

No frontend may become the source of truth for cycle state.

The Herdr integration receives Herdr's open workspace/session inventory and
matches it to Code Cycle runtime state when present. Claude and Codex adapters
provide their current host/session context in the same way. No repository is
registered in Code Cycle to make discovery work.

---

## 3. Design principles

### 3.1 Single normalized state model

All frontends must consume the same normalized runtime state and event model.

The representation may be backed by SQLite, files, IPC, sockets, or another local transport, but the public schema must remain stable and frontend-neutral.
The model also includes host-discovered workspaces that have no matching Code
Cycle runtime; every field must identify its provider/source and observation
time so host facts and Code Cycle facts remain distinguishable.

### 3.2 Adaptive presentation

The amount of information shown depends on the host environment.

Recommended presentation levels:

| Level | Name | Purpose |
|---|---|---|
| 0 | Status | One-line or compact current state |
| 1 | Agent | Session/model-specific information |
| 2 | Cycle | Full state of one task/cycle |
| 3 | Control Center | Global view across cycles, agents and worktrees |

Default mapping:

| Environment | Default view |
|---|---|
| Claude inside Herdr | Level 1 |
| Codex inside Herdr | Level 1 |
| Claude standalone with active Code Cycle | Level 2 |
| Claude standalone without active Code Cycle | Level 1 with available session/repository state |
| Codex standalone with active Code Cycle | Level 2 where host capabilities permit |
| Codex standalone without active Code Cycle | Level 1 with available session/repository state |
| Herdr | Level 3 |
| `cc status` | Level 0 |
| `cc watch` / optional standalone TUI | Level 2 |

### 3.3 No model-turn polling

Monitoring must not consume model turns merely to discover current state.

State must be obtained from:

- runtime events;
- process state;
- filesystem state;
- Git state;
- existing Code Cycle storage;
- optional host APIs;
- optional local polling performed by the monitoring runtime.

Polling local files, processes, Git, SQLite, or local APIs is acceptable.

Polling Claude/Codex through prompts is not acceptable.

### 3.4 Orca is optional

Monitoring is a Code Cycle capability, not an Orca-only capability.

Orca enriches the monitoring model with orchestration data when present.

Without Orca, monitoring must still expose all available base telemetry.

### 3.5 Frontends degrade gracefully

Missing telemetry must not break a view.

Example:

```text
tokens: unavailable
cost: unavailable
context: unavailable
```

A renderer must omit or mark unavailable data rather than fail.

---

## 4. Supported deployment modes

The implementation must support at least the following scenarios.

### 4.1 Herdr + Orca + Claude/Codex

Herdr provides the global Level 3 control center for all open workspaces,
repositories, worktrees, agents and sessions it exposes. Discovery is automatic
and does not depend on a repository allowlist.

Claude/Codex provide Level 1 session-specific telemetry in their own host
surface.

For workspaces without an active Code Cycle run, show available host, Git and
repository state. When a run is detected, enrich the workspace with Code Cycle
task, cycle, stage, findings, receipts and Orca state. Herdr owns no Code Cycle
state; it supplies workspace/session context and consumes the monitor projection.

### 4.2 Herdr + Claude/Codex without Orca

Herdr still discovers every open workspace and shows available:

- sessions;
- agents;
- tasks;
- worktrees;
- Git state;
- tests;
- findings when available;
- runtime events.

Orca-specific sections are omitted.

### 4.3 Claude standalone + Orca

Claude Code is installed once at host scope and automatically associates its
session and current working directory with available repository/worktree state.
When an Orca-backed cycle is active, it can provide an expanded Level 2 panel.

This mode should be suitable for launching workflows such as `cc-orchestrator` directly from Claude Code without Herdr.

### 4.4 Claude standalone without Orca

Claude Code shows Level 1 session/repository state by default, even when no
Code Cycle run exists. If a run matches its host session or working directory,
the panel may expand to Level 2.

### 4.5 Codex standalone

Codex should expose the same information model as Claude, subject to the UI extension capabilities available in Codex.

The implementation must not assume that Codex supports the same UI primitives as Claude Code.

If necessary, Codex may use a reduced renderer such as:

- status line;
- side panel if supported;
- command output;
- TUI;
- local browser panel.

The Codex adapter is installed once at host scope. It discovers the current
session and working directory automatically. A native embedded panel is not a
prerequisite; use the supported external renderer when Codex does not expose
one.

### 4.6 CLI only

The monitoring core must remain usable without Herdr, Claude UI extensions, or Codex UI extensions.

Minimum commands:

```bash
cc status
cc watch
cc monitor snapshot --json
cc monitor events --jsonl
cc monitor task <task-id> --json
```

Exact command naming may follow repository conventions.
These are target commands, not current entry points. The repository currently
ships `cycle_status.py` for cycle status and `stats.py` for aggregate reports;
the first CLI issue should either add compatible `cc` aliases or document the
installed script interface before naming commands as acceptance criteria.

---

## 5. Runtime state model

The monitoring subsystem must expose a normalized snapshot.

Illustrative shape:

```json
{
  "schemaVersion": 1,
  "monitorId": "monitor-...",
  "updatedAt": "...",
  "hosts": [],
  "summary": {},
  "workspaces": [],
  "runtimes": [],
  "cycles": [],
  "agents": [],
  "tasks": [],
  "worktrees": [],
  "findings": [],
  "tests": [],
  "git": {},
  "github": {},
  "receipts": [],
  "tools": [],
  "events": []
}
```

This is illustrative, not a required storage format.

The implementation issue that defines the schema must finalize field names and versioning.

---

## 6. Runtime identity and relationships

The state model must be able to relate at least:

```text
monitor
 ├─ host
 │   └─ workspace (may exist without a Code Cycle run)
 │       ├─ repository/worktree
 │       ├─ agent/session
 │       └─ matching runtime/cycle when present
 └─ Code Cycle runtime (optional)
     └─ task
         └─ cycle
             ├─ stage
             ├─ finding
             └─ receipt
```

Identifiers should remain stable for the lifetime of each entity.

Suggested identifiers:

- `runtimeId`
- `monitorId`
- `hostId`
- `hostWorkspaceId`
- `taskId`
- `cycleId`
- `stageId`
- `agentId`
- `sessionId`
- `worktreeId`
- `findingId`
- `receiptId`

---

## 7. Base telemetry

The base monitoring layer should expose the following where available.

### 7.1 Task

- task/issue identifier;
- title;
- state;
- creation time;
- start time;
- completion time;
- active cycle;
- assigned agent/session;
- repository;
- branch;
- worktree.

### 7.2 Agent/session

- agent type: Claude, Codex, other;
- model;
- session ID;
- current state;
- active task;
- active cycle;
- active stage;
- elapsed time;
- current action if known;
- context usage if exposed by host;
- token usage if exposed by host;
- cost if exposed by host;
- compactions if exposed by host;
- permission wait state;
- last activity timestamp.

Agent states should include at least:

```text
starting
running
waiting
blocked
idle
completed
failed
unknown
```

### 7.3 Git/worktree

- repository;
- branch;
- worktree path or logical identifier;
- clean/dirty state;
- modified file count;
- staged file count;
- untracked file count;
- commits created during task;
- HEAD SHA;
- upstream state if relevant.

### 7.4 Findings

MVP findings view:

- count by severity and state;
- a PR link when one is known.

Do not add local findings persistence solely to feed the UI. Full finding
details (title, prose, file/symbol, timestamps and reopen history) may be shown
only when an explicit provider supplies them, such as a read-only PR review
provider. Without that provider, keep the summary and link; do not imply that
details are available from SQLite.

An explicit detail provider may expose:

- finding ID;
- severity;
- state;
- title/summary;
- source stage;
- file/symbol when known;
- created timestamp;
- resolved timestamp;
- reopen count.

Minimum states:

```text
open
in_progress
resolved
reopened
dismissed
not_applicable
```

### 7.5 Tests/checks

- check name;
- source;
- state;
- passed count;
- failed count;
- skipped count;
- duration;
- last run timestamp;
- command when applicable.

Minimum states:

```text
pending
running
passed
failed
skipped
unknown
```

### 7.6 Receipts

Expose existing Code Cycle receipt information, including where applicable:

- result/exit status;
- stage;
- `failedStage`;
- `residualResources`;
- timestamps;
- duration;
- task/cycle relationship;
- relevant artifact references.

The monitor must consume existing receipt semantics rather than define conflicting ones.

### 7.7 Tools/integrations

When detectable:

- Serena;
- Context7;
- MCP servers;
- GitHub;
- other agent tools.

Per tool:

```text
available
ready
busy
error
unavailable
unknown
```

---

## 8. Orca telemetry

When Orca is active, add orchestration-specific state.

This may include:

- cycle/stage graph;
- current coordinator;
- workers;
- worker state;
- fan-out;
- dependencies;
- retry counts;
- stage transitions;
- blocked stages;
- failed stages;
- next expected stage;
- residual resources;
- receipts;
- spawned tasks/subtasks.

Monitoring must not require Orca to duplicate state already available in the base layer.

---

## 9. Normalized event stream

The runtime must emit or derive normalized events.

Example:

```json
{
  "schemaVersion": 1,
  "eventId": "evt-...",
  "timestamp": "...",
  "type": "finding.resolved",
  "source": "code_cycle",
  "hostId": "herdr",
  "hostWorkspaceId": "workspace-...",
  "runtimeId": "runtime-...",
  "taskId": "TASK-151",
  "cycleId": "cycle-...",
  "agentId": "claude-1",
  "payload": {
    "findingId": "REV-004"
  }
}
```

### 9.1 Initial event taxonomy

The first schema should consider at least:

```text
runtime.started
runtime.stopped

host.connected
host.disconnected
workspace.opened
workspace.updated
workspace.closed

task.started
task.updated
task.completed
task.failed
task.blocked

cycle.started
cycle.completed
cycle.failed

stage.started
stage.completed
stage.failed
stage.blocked
stage.retrying

agent.started
agent.state_changed
agent.completed
agent.failed

finding.created
finding.updated
finding.resolved
finding.reopened

test.started
test.completed
test.failed

git.changed
commit.created
worktree.created
worktree.changed
worktree.removed

receipt.created

tool.state_changed

permission.requested
permission.resolved
```

Exact names should be finalized in the event-schema issue.

### 9.2 Event requirements

Events must be:

- timestamped;
- attributable to a monitor and source/provider;
- attributable to a host/workspace where relevant;
- attributable to a runtime, task/cycle/agent when a Code Cycle run exists;
- serializable;
- versioned;
- replayable where practical;
- safe to consume by several frontends.

---

## 10. Storage and transport

The implementation must separate:

1. state schema;
2. persistence;
3. transport;
4. renderer.

The logical source layout belongs in the Code Cycle Toolkit repository:

```text
code-cycle-toolkit
├── core/monitor/       # schema, discovery, projections, event store
├── integrations/
│   ├── herdr/          # host plugin
│   ├── claude/         # Claude Code Mod
│   └── codex/          # Codex adapter
└── cli/                # status, watch, integration install/status
```

These are logical boundaries; physical paths should follow the repository's
Python/runtime packaging conventions. All bundled integrations and schemas
should be versioned and released together unless a host's verified packaging
constraints require a separate artifact.

The architecture must allow a frontend to obtain:

- current snapshot;
- event stream;
- single task/cycle details.

The implementation agent should evaluate existing repository storage before introducing a new database.

Preferred direction:

- reuse existing SQLite/state infrastructure if appropriate;
- avoid creating a second source of truth;
- optionally maintain a derived monitoring projection.

Potential interfaces:

```text
snapshot()
getTask(taskId)
getCycle(cycleId)
subscribe()
listEvents(since)
```

Transport may initially be local-only.

Examples:

- direct library API;
- Unix socket;
- named pipe;
- localhost HTTP/SSE;
- WebSocket;
- filesystem/JSONL;
- SQLite read model.

The issue-generation agent must create a decision issue only if repository constraints make the transport choice genuinely ambiguous.

---

## 11. Host detection

The toolkit should detect the current host and the installed host integrations
where practical. Detection and integration installation are host-level; they
must not inspect a configured list of repositories.

Possible capabilities:

```json
{
  "herdr": true,
  "claude": true,
  "codex": false,
  "orca": true
}
```

`detectHost()` should report capabilities such as the current host, whether its
integration is installed and active, and whether Orca is available. The host
adapter supplies its open workspace/session inventory. The runtime correlates
that inventory with Git root/worktree state and Code Cycle runs; it must not
depend on process-name matching alone.

Detection must be capability-based where possible.

Do not rely exclusively on fragile process-name matching.

Environment variables or explicit registration from the host are acceptable.
Use host workspace/session identifiers when available. Paths and Git metadata
are correlation inputs, not an allowlist or a requirement for manual
registration.

Recommended behavior:

```text
Herdr present:
    global renderer -> Herdr Level 3
    agent renderer  -> Level 1

Herdr absent + cycle active:
    agent renderer  -> Level 2

No active Code Cycle run:
    agent renderer  -> Level 1 with available session/repository/Git state
```

`cc-orchestrator` and other runtime entry points should use the same host
detection. If a compatible host is detected but its Code Cycle integration is
missing, print a non-blocking recommendation with the appropriate install
command. Monitoring must continue through the CLI/runtime without the plugin.

Example:

```text
Code Cycle detected Herdr.

Enhanced monitoring is available through the Code Cycle Toolkit Herdr integration.
Run:
  cc integrations install herdr
```

The message must not stop or fail the current run. The exact command and
installation scope follow the host's supported extension mechanism.

Target integration commands (names may follow repository conventions):

```bash
cc integrations status
cc integrations install herdr
cc integrations install claude
cc integrations install codex
```

`cc integrations status` should report at least detected, installed and active
separately for each supported host. For example:

```text
Integration       Detected     Installed     Active
Herdr             yes          yes           yes
Claude Code       yes          yes           yes
Codex             no           yes           no
Orca              yes          built-in      yes
```

For Orca, `built-in` describes the Code Cycle adapter shipped with the toolkit;
Orca itself remains an optional external orchestration system.

The installer must use the host's supported global or user-level mechanism,
report detected/installed/active status, and be idempotent. It must not add
per-repository configuration or silently write into the current project.

---

## 12. Herdr integration

Herdr is the primary global visualization and control surface.

### 12.1 Herdr role

Herdr should provide:

- visualization;
- navigation;
- drill-down;
- optional control actions.

Herdr must not become the orchestration engine.

The Herdr plugin is installed once in Herdr's supported global/user scope. It
reads the workspaces, repositories, worktrees, agents and sessions Herdr
already knows about and submits that inventory to the Code Cycle Monitor. Every
workspace is visible without project registration, whether or not a Code Cycle
run exists. Basic Git/repository/session fields come from Herdr and local Git
inspection; Code Cycle and Orca fields are added only when detected.

“Plugin” names the desired user experience, not an assumed Herdr API. Before
implementation, verify Herdr's supported extension/install mechanism. If it has
no formal plugin API, use a supported adapter, sidecar or hook with the same
host-level installation and automatic workspace discovery behavior. Do not
invent or depend on an undocumented extension interface.

### 12.2 Main dashboard

Target Level 3 view across all host workspaces, including repositories without
an active Code Cycle run:

```text
┌ CODE CYCLE ─────────────────────────────────────────────────────────────┐
│ agents · cycles · worktrees · PRs · blocked · runtime metrics         │
└────────────────────────────────────────────────────────────────────────┘

┌ ACTIVE CYCLES ────────────────────┐ ┌ AGENTS ──────────────────────────┐
│ TASK-151   implement   4/7        │ │ Claude   TASK-151   running      │
│ TASK-154   rereview    2 findings │ │ Codex    TASK-154   reviewing    │
│ TASK-147   blocked                │ │ Claude   idle                     │
└───────────────────────────────────┘ └───────────────────────────────────┘

┌ OPEN WORKSPACES ────────────────────────────────────────────────────────┐
│ repo A · worktree A1 · Claude · clean · cycle 91                       │
│ repo B · main · Codex · dirty 3 · no active Code Cycle                  │
│ repo C · worktree C2 · Claude · branch fix/42 · no active Code Cycle    │
└────────────────────────────────────────────────────────────────────────┘

┌ SELECTED TASK / CYCLE ─────────────────────────────────────────────────┐
│ review ✓ → implement ● → verify ○ → rereview ○ → finalize ○          │
│ current finding / action / next expected action                        │
└────────────────────────────────────────────────────────────────────────┘

┌ FINDINGS ─────────────────────────┐ ┌ TESTS / CI ──────────────────────┐
│ severity/state summary            │ │ local tests / lint / remote CI  │
└───────────────────────────────────┘ └───────────────────────────────────┘

┌ GIT / WORKTREES ───────────────────────────────────────────────────────┐
│ branch · worktree · dirty state · commits · PR                        │
└────────────────────────────────────────────────────────────────────────┘

┌ EVENT STREAM ──────────────────────────────────────────────────────────┐
│ recent normalized events                                              │
└────────────────────────────────────────────────────────────────────────┘
```

### 12.3 Visual goals

The Herdr view should be visually dense but readable.

Desired characteristics:

- dashboard/card layout;
- clear semantic status indicators;
- progress bars where useful;
- compact timelines;
- severity counts;
- active/blocked emphasis;
- keyboard navigation;
- fast refresh;
- no large text logs as the primary interface;
- drill-down instead of always-visible detail.

Optional enhancements:

- sparklines;
- token/context trend;
- duration trend;
- stage timing;
- agent activity;
- event rate.

### 12.4 Navigation

At minimum:

```text
tasks
cycles
agents
findings
receipts
git/worktrees
tests/CI
events
```

Selecting an entity should allow drill-down.

### 12.5 Control plane

Herdr may evolve from monitor to control plane.

Candidate actions:

```text
open agent
open worktree
open PR
pause cycle
resume cycle
retry stage
request rereview
show receipt
focus task
```

Control actions must call Code Cycle runtime/orchestration APIs.

They must not implement workflow rules inside Herdr.

Destructive or workflow-changing actions should require explicit user intent.

---

## 13. Claude Code integration

Use Claude Code Mods or the current supported extension mechanism.

Install the mod once at Claude Code host scope. It obtains the current session
and working directory from Claude Code and asks the local monitor for matching
state. It must not require a repo list or repository-level install. If the
session has no matching Code Cycle run, show available session/repository/Git
state only.

When Herdr is present, the mod uses Level 1 and leaves the global workspace
view to Herdr. Without Herdr, it can expand a matching active run to Level 2.

### 13.1 Claude embedded in Herdr

Default Level 1 panel.

Show Claude-specific information plus immediate Code Cycle context.

Example:

```text
┌ Code Cycle ───────────────────────┐
│ TASK-151 · implement             │
│ REV-004 · MED                    │
│                                  │
│ Context       37%                │
│ Model         ...                │
│ Tools         Serena ✓           │
│ Worktree      clean              │
│ Tests         228 ✓              │
│ Permission    none               │
│                                  │
│ next → update tests              │
└──────────────────────────────────┘
```

Avoid duplicating the whole Herdr control center.

### 13.2 Claude standalone

If a Code Cycle cycle is active, expand to Level 2.

Example:

```text
┌ CODE CYCLE ───────────────────────────────┐
│ TASK-151                                 │
│ Worktree reuse rule                     │
│                                          │
│ review       ✓                           │
│ implement    ● 4/7                       │
│ verify       ○                           │
│ rereview     ○                           │
│ finalize     ○                           │
├──────────────────────────────────────────┤
│ AGENTS                                   │
├──────────────────────────────────────────┤
│ FINDINGS                                 │
├──────────────────────────────────────────┤
│ GIT                                      │
├──────────────────────────────────────────┤
│ TESTS                                    │
├──────────────────────────────────────────┤
│ CLAUDE SESSION                           │
└──────────────────────────────────────────┘
```

This must be sufficient to operate `cc-orchestrator` from Claude without Herdr.

### 13.3 Claude-specific telemetry

Where exposed by Claude Code:

- model;
- session ID;
- context usage;
- token usage;
- cache metrics;
- compactions;
- current/last tool call;
- permission requests;
- current turn/activity;
- active subprocess/command state.

Do not fabricate telemetry not exposed by Claude Code.

### 13.4 Claude event integration

Where the mod API permits, subscribe to useful host events and map them into normalized Code Cycle monitoring events.

The mod should remain a thin integration layer.

It must not become the main persistence layer.

Pin the Mod to supported Claude Code versions. A version incompatibility or
load failure must leave the runtime and CLI usable and direct the user to
`cc watch` or the supported fallback surface.

---

## 14. Codex integration

Codex must use the same underlying monitoring model.

Install the adapter once at Codex host scope. It discovers the current session
and working directory automatically, with no per-repository registration.

The implementation must first determine which UI extension primitives are officially available in the target Codex version.

Do not assume feature parity with Claude Code Mods.

Do not create an implementation issue that assumes an embedded sidebar or
panel. Verify the current official API first. If no native renderer is
supported, the adapter plus `cc watch`/TUI or another documented external
surface is a complete integration deliverable.

Priority order:

1. native Codex extension/panel API if available;
2. native status/UI hooks if available;
3. integrated TUI/pane;
4. `cc watch` as fallback.

Codex should expose:

- agent/session state;
- active task/cycle;
- worktree;
- Git state;
- tests;
- findings;
- current stage;
- optional context/token telemetry when exposed.

When Herdr is present, prefer Level 1.

When Herdr is absent and a cycle is active, prefer Level 2.

---

## 15. Standalone CLI/TUI

Provide a renderer independent of Herdr, Claude, and Codex.

### 15.1 `cc status`

Compact Level 0 output:

```text
CC │ TASK-151 │ implement │ 2 findings │ tests ✓ │ git clean
```

### 15.2 `cc watch`

Interactive or continuously refreshed Level 2 view.

Minimum data:

- active task;
- stage;
- findings;
- agents;
- tests;
- worktree/Git;
- recent events.

### 15.3 Machine-readable interfaces

Provide stable machine-readable access:

```bash
cc monitor snapshot --json
cc monitor events --jsonl
cc monitor task TASK-151 --json
```

This interface is important for Herdr and third-party renderers even if they later use a more efficient transport.

---

## 16. Configuration

Recommended configuration model:

```toml
[monitor]
enabled = true
auto_detect_host = true

[monitor.orca]
show_workers = true
show_receipts = true
show_event_stream = true
```

Exact location and naming should follow Code Cycle Toolkit conventions.
This is host/user-level configuration. Do not add a project-to-host mapping,
repository allowlist, or per-repository integration enablement.

Configuration should permit:

- enable/disable monitoring;
- host override;
- view level override;
- event retention;
- telemetry opt-outs;
- refresh settings;
- optional metrics visibility.
- installed integration detection and status;
- non-blocking installation recommendations for detected hosts.

---

## 17. Performance requirements

Monitoring must have low runtime overhead.

Initial targets:

- state update visible to a local UI within approximately 500 ms when event-driven;
- local polling interval configurable;
- no model calls caused solely by dashboard refresh;
- no blocking writes on the critical agent path when avoidable;
- bounded event retention;
- UI capable of rendering hundreds of events without continuously reprocessing full history.

If these targets conflict with existing repository architecture, the implementation agent may adjust them with justification.

---

## 18. Reliability requirements

The monitoring subsystem must tolerate:

- frontend disconnect/reconnect;
- Herdr restart;
- Claude/Codex restart;
- stale runtime data;
- incomplete events;
- crashed cycles;
- orphan worktrees;
- missing receipts;
- unknown agent state;
- partial provider failure.

A monitoring failure must not normally stop a Code Cycle workflow.

Monitoring should be observational by default.

Control-plane failures should be isolated from runtime execution.

---

## 19. Security requirements

UI integrations may execute in privileged developer environments.

Requirements:

- minimize privileges;
- do not expose secrets in snapshots/events;
- redact tokens/API keys/environment secrets;
- do not persist full prompts by default;
- do not persist arbitrary terminal output by default;
- sanitize paths or command arguments when they may contain secrets;
- keep Claude/Codex extensions small and auditable;
- do not open network listeners externally by default;
- bind local transports to localhost/IPC unless explicitly configured otherwise.

Apply these rules separately to analytical telemetry and operational status
files. The current SQLite telemetry policy excludes prompts, prose, file paths,
file names and code. The existing per-cycle status JSON is a different surface:
it includes workspace paths and short activity text, and can include a pending
question. Reusing it requires an explicit field allowlist, retention policy and
redaction rules; do not assume the SQLite telemetry guarantees already cover
that file.

---

## 20. Compatibility and versioning

Both snapshot and event schemas must include a version.

Renderers must:

- reject incompatible major versions clearly;
- tolerate additive fields;
- ignore unknown optional fields.

The monitoring subsystem must not force all frontends to update atomically for minor additive schema changes.

---

## 21. Testing strategy

The implementation should include tests at several levels.

### 21.1 Schema tests

Validate:

- required identifiers;
- event serialization;
- snapshot serialization;
- version compatibility;
- unknown/additive fields.

### 21.2 Projection/state tests

Given events:

```text
task.started
stage.started
finding.created
finding.resolved
stage.completed
```

the resulting snapshot should be deterministic.

### 21.3 Provider tests

Mock or fixture:

- Git;
- Orca;
- Claude host events;
- Codex host events;
- GitHub/CI where applicable.

### 21.4 Renderer tests

Test that:

- missing fields do not crash;
- embedded vs standalone mode renders correctly;
- Herdr view updates from runtime events;
- a Herdr workspace without an active Code Cycle run remains visible;
- opening another host workspace requires no Code Cycle configuration change;
- a matching Code Cycle run enriches the existing workspace automatically;
- unsupported host metrics stay unavailable without breaking the renderer;
- Claude Mod fallback and the verified Codex surface remain usable.
- status output remains stable.

### 21.5 Integration tests

At minimum:

```text
Claude standalone + Code Cycle
Claude + Herdr
Herdr with multiple workspaces, including one without Code Cycle
Code Cycle without Orca
Code Cycle with Orca
frontend disconnect/reconnect
runtime crash with stale state
Claude Mod unavailable/version mismatch falls back to CLI/watch
```

Add Codex combinations once the available Codex UI mechanism is confirmed.

---

## 22. Observability of the monitor itself

The monitor should expose basic diagnostics:

- provider connected/disconnected;
- last provider update;
- dropped/invalid event count;
- renderer connection count;
- schema version;
- storage size/event count;
- last persistence error.

Do not mix monitor-internal errors with cycle findings.

---

## 23. Non-goals for the first implementation

Unless repository analysis shows they are trivial, the first milestone does not need:

- remote multi-machine orchestration dashboard;
- cloud-hosted monitoring;
- mobile UI;
- historical analytics platform;
- long-term cost analytics;
- full log aggregation;
- replacing Herdr;
- replacing Claude/Codex terminals;
- workflow logic implemented inside the UI.

---

## 24. Recommended implementation phases

The issue-generation agent should decompose work roughly along these boundaries, but may refine them after repository inspection.

### Phase 1 — Core monitoring contracts

- define entities and relationships;
- define snapshot schema;
- define provider interface;
- define renderer/consumer interface;
- define snapshot schema versioning;
- define host workspace/session inventory and automatic correlation inputs;
- define a stable way to distinguish host-observed state from Code Cycle state.

### Phase 2 — Runtime projection

- build monitoring projection from current Code Cycle state;
- expose existing finding counts, severity/state summaries and PR links;
- integrate receipts;
- integrate task/stage state;
- integrate Git/worktree;
- expose the versioned snapshot;
- expose available repository/worktree state when no Code Cycle run is active.

Do not persist finding prose or add a findings table for the dashboard. A later
detail view may read findings from an explicit PR/provider adapter.

### Phase 3 — Forward-only event stream

- define event IDs, source provenance, ordering/cursor, deduplication and retention;
- emit events from the point this capability is implemented;
- use existing structured events where available;
- never reconstruct granular historical events from SQLite aggregates or replaceable snapshots.

### Phase 4 — CLI and machine-readable interfaces

- `cc status`;
- `cc watch`;
- JSON snapshot;
- JSONL event stream.
- `cc integrations status` and host-level integration installation commands.

This phase proves that the monitoring core is frontend-independent before
building dashboard presentation.

### Phase 5 — Providers

- host workspace/session inventory adapter;
- Git and available checks/CI providers;
- explicit finding-detail provider only if required beyond MVP summaries;
- Orca provider for coordinator/workers/stage graph/retries/receipts/residual resources.

### Phase 6 — Claude Code integration

- verify the supported Mod mechanism and pin compatible Claude Code versions;
- bundle the Mod with the toolkit release and install it once at host scope;
- host/session/cwd detection and automatic matching;
- Level 1 embedded mode;
- Level 2 standalone mode;
- Claude-specific telemetry;
- runtime subscription;
- fallback to CLI/watch when the Mod is unavailable or fails to load.

### Phase 7 — Herdr integration

- verify Herdr's real extension mechanism before choosing the implementation form;
- bundle the plugin with the toolkit release and install it once at the scope Herdr supports;
- if Herdr has no formal plugin API, use its supported adapter/sidecar/hook mechanism to preserve the same host-level UX;
- discover all Herdr workspaces/repositories/worktrees/agents/sessions automatically;
- show base host/Git state even without an active Code Cycle run;
- enrich matching contexts with Code Cycle and optional Orca state;
- runtime connection;
- global dashboard;
- tasks/cycles;
- agents;
- findings;
- tests/CI;
- worktrees/Git;
- receipts;
- event stream;
- drill-down.

### Phase 8 — Codex integration

After verifying the current supported Codex capabilities:

- bundle the adapter with the toolkit release and install it once at host scope;
- discover session and working directory automatically;
- use a native renderer only if the documented host surface supports it;
- otherwise deliver the adapter with `cc watch`/TUI or the best supported external surface;
- never make a sidebar or embedded panel an acceptance prerequisite without verified support.

### Phase 9 — Herdr control plane

After monitoring is stable:

- open/focus agent;
- open worktree;
- open PR;
- pause/resume;
- retry;
- rereview;
- receipt navigation.

Control actions are outside the observational MVP and depend on the resolved
host permission and human-control contracts.

---

## 25. Issue generation requirements

This specification is intended to be consumed by an agent that creates implementation issues.

The agent must inspect the current repository before creating issues.

It must not blindly create one issue per section.

### 25.1 Required behavior of the issue-generation agent

The agent must:

1. inspect existing architecture and relevant modules;
2. identify reusable state/storage/event infrastructure;
3. identify current Orca interfaces;
4. identify current receipt/findings/task models;
5. identify CLI conventions;
6. identify Herdr integration boundaries;
7. package official integrations in this repository/release unless a confirmed host constraint requires a separate artifact or repository;
8. detect existing issues that already cover part of the scope;
9. create a dependency-aware implementation plan;
10. create implementation-ready issues.

### 25.2 Each issue should contain

At minimum:

- category: `core`, `provider`, `renderer`, or `integration installer`;
- title;
- problem statement;
- scope;
- explicit non-scope;
- proposed implementation boundary;
- affected modules/packages;
- dependencies;
- acceptance criteria;
- required tests;
- compatibility/migration considerations;
- documentation requirements.

Each issue should have one primary category. Split work that combines a core
contract, provider, renderer and/or installer unless one category is only a
small inseparable part of the same independently reviewable deliverable.

### 25.3 Issue size

Prefer issues that can be implemented and reviewed independently.

Avoid:

- one giant “build monitoring” issue;
- tiny mechanical issues with no independently reviewable value.

A typical issue should represent one coherent contract, provider, projection, renderer, or integration.

### 25.4 Dependency graph

The issue-generation agent must state issue dependencies.

Example only:

```text
MON-001 schemas
   │
   ├── MON-002 runtime projection
   │      ├── MON-003 CLI
   │      ├── MON-004 Orca provider
   │      ├── MON-005 Claude renderer
   │      └── MON-006 Herdr renderer
   │
   └── MON-007 compatibility/version tests
```

The actual graph must be derived from repository inspection.

### 25.5 Decisions vs implementation issues

Do not create “decision” issues for choices that can be made safely from repository conventions.

Create a decision/blocking issue only when:

- two viable choices have materially different compatibility consequences;
- a public contract cannot be established without maintainer input;
- an external host API is genuinely uncertain;
- the choice changes project scope.

### 25.6 Avoid speculative issues

Do not create implementation issues for unsupported host features.

Examples:

- if Codex does not expose a side-panel API, do not create an issue assuming one exists;
- instead create an integration issue using the best actually supported surface.

### 25.7 Repository baseline and issue reconciliation

This baseline was checked against the repository and GitHub issues on
2026-10-05. Recheck issue states and host APIs when creating the implementation
issues.

| Area | Current repository state | Planning consequence |
|---|---|---|
| Live cycle status | `scripts/cycle_status.py` writes one atomic JSON snapshot per cycle. It lists snapshots and supports follow, one-line output and timestamps. `scripts/run_cycle.py` updates it during a run; the module is in `scripts/runtime.manifest`. | Extend this source for current cycle state. `cc watch` would add a renderer; it must not create a second status writer. |
| Stage telemetry | `scripts/telemetry.py` stores local SQLite stage rows and correlated attempt, usage, cost and harness records. The current schema is version 13, including the foundation from closed issue [#119](https://github.com/datacas/code-cycle-toolkit/issues/119). It is analytical history, not a live event log. | Build a read-only projection over this store where its fields fit. Do not change historical unknowns into zeroes or successes. |
| Progress and workspace context | Closed issues [#71](https://github.com/datacas/code-cycle-toolkit/issues/71), [#131](https://github.com/datacas/code-cycle-toolkit/issues/131), [#137](https://github.com/datacas/code-cycle-toolkit/issues/137) and [#130](https://github.com/datacas/code-cycle-toolkit/issues/130) cover status snapshots, adaptive progress, observed workspace context and detached/interrupted runs. | Treat these as implemented foundations and verify exact fields in code; do not file duplicate progress issues. Preserve the distinction between a stale snapshot and a confirmed failure. |
| Stage report | Closed issue [#112](https://github.com/datacas/code-cycle-toolkit/issues/112) adds stage outcomes, finding counts and warnings to the runtime report. | Reuse the existing report semantics and formatting. The dashboard's finding detail still needs a separate source. |
| Aggregate analytics | Closed issue [#42](https://github.com/datacas/code-cycle-toolkit/issues/42) defines `cc-stats` as a local, read-only aggregate report and excludes a dashboard/server from that feature. | This does not prohibit a separate monitor, but the monitor must not turn `cc-stats` into a server or expose its per-task data as aggregate analytics. |
| Findings | The runtime validates finding results and records counts/statuses. Full review finding content is carried in the review/PR contract, not as a general local findings table. | MVP is counts by severity/state plus a PR link. Full detail requires an explicit provider; do not add persistence only for the UI. |
| Receipts and Orca | Executor/Orca dispatch receipts exist and are correlated with dispatch attempts; there is no evidence of a general-purpose receipt collection matching every field in this spec. | Consume the existing receipt contract through the executor/attempt provider. Do not introduce a competing receipt schema without a demonstrated gap. |
| Tasks and work-item outcomes | Telemetry correlates `repo_id`, `task_id` and `cycle_id`. Open issue [#120](https://github.com/datacas/code-cycle-toolkit/issues/120) adds verified post-cycle work-item outcomes. | A task snapshot may identify the work item, but must keep cycle completion distinct from PR merge or issue resolution and should depend on #120 only for those later outcomes. |
| Checks and behavioral verification | Open issues [#145](https://github.com/datacas/code-cycle-toolkit/issues/145)–[#147](https://github.com/datacas/code-cycle-toolkit/issues/147) and [#154](https://github.com/datacas/code-cycle-toolkit/issues/154)–[#159](https://github.com/datacas/code-cycle-toolkit/issues/159) cover CI classification, visual evidence, requirement fit and behavioral evidence. | Reuse their result contracts if the monitor displays them. Do not duplicate test execution, review loops or evidence retention in the monitor. |
| Human control and host permissions | Open issues [#141](https://github.com/datacas/code-cycle-toolkit/issues/141) and [#142](https://github.com/datacas/code-cycle-toolkit/issues/142) address orchestration questions and host permissions. | Keep pause/resume/retry outside the observational MVP. Any later control plane must use the resolved permission contract and runtime APIs. |
| Tool detection | The runtime does not have a general tool-capability registry. Closed issue [#90](https://github.com/datacas/code-cycle-toolkit/issues/90) explicitly keeps host workspace-tool detection outside runtime behavior. | Do not require Serena/Context7/MCP detection in the core. Show only explicitly registered capabilities or omit them. |
| Host integrations | This repository contains no Herdr renderer or Claude/Codex UI extension. Requirement: one host-level installation with automatic workspace discovery. | Bundle official integrations in this repository/release by default. Use a companion repository only when a confirmed host packaging constraint requires it. Never introduce a repo allowlist or manual project registration. |
| Normalized events | There is no versioned cross-provider event stream or general snapshot API. The status JSON is replaceable current state; SQLite rows do not contain enough detail to reconstruct every event. | Start the stream forward-only when the capability ships. Define event IDs, ordering/cursors, deduplication, retention and source provenance. Never fabricate granular history from aggregate rows or snapshots. |

No open issue found in this repository directly covers the normalized monitoring
contract, event stream, or host dashboards. Those are net-new roadmap items; the
issues above provide reusable contracts and dependencies, not hidden coverage
for the dashboard initiative.

The current division of responsibility is therefore: per-cycle JSON for live
operational status, SQLite for bounded stage/attempt history, and external host
or provider data for session, PR and global orchestration details. The proposed
monitoring projection is a consumer of those sources. Any new persistent store
must state which gap it fills and how it avoids becoming a second source of
truth.

Current host feasibility was checked against official documentation. Claude
Code has custom Mods render surfaces, but its function-hooks API is early
access and may change; pin the supported host version and keep a fallback.
Codex has an app-server protocol for external integrations, but this audit did
not find an official embedded custom-panel API. Plan `cc watch` or another
external renderer as the supported fallback, and re-verify before filing host
integration issues. References: [Claude Code Mods README](https://github.com/anthropics/claude-code/blob/main/mods/README.md),
[Codex app-server test client](https://github.com/openai/codex/blob/main/codex-rs/app-server-test-client/README.md).

### 25.8 Net-new issue candidates

These are candidate boundaries, not pre-created issues. The issue-generation
agent must still inspect module ownership and open issues before filing them.

| Candidate | Scope | Depends on / excludes |
|---|---|---|
| Monitoring snapshot contract and projection | Define the minimal versioned snapshot, source/provenance and staleness fields, then project current cycle status and available SQLite records. Keep findings as counts unless a separate provider supplies detail. | Reuses `cycle_status.py` and `telemetry.py`; depends on no new database or frontend. |
| Forward-only normalized event stream | Define event identity, ordering/cursor, deduplication, retention and partial-provider behavior; emit new events at runtime and document which historic events cannot be reconstructed. | Depends on the snapshot/entity contract. Does not replay invented per-tool or finding events from aggregate rows. |
| CLI and machine-readable access | Add or document `cc status`, `cc watch` and JSON snapshot/event/task commands over the core contracts. Keep `cycle_status.py --follow` compatible. | Depends on snapshot; event command depends on the event stream. No Herdr UI or workflow controls. |
| External data adapters | Add only the provider adapters that repository inspection confirms belong here and have stable interfaces, such as GitHub check summaries or Orca dispatch enrichment. | Must consume existing provider/receipt contracts and open issues. Does not execute checks or own orchestration. |
| Host integration detection and installation | Detect each host, report detected/installed/active status, and install or recommend the matching integration once at the supported host scope. | Part of the bundled CLI/integration packages. Installation must be idempotent, non-blocking for runtime work and must not write to the current repository. |
| Host renderer roadmap | Ship official Herdr, Claude and Codex integrations alongside the toolkit when each host's packaging mechanism permits. This row groups the roadmap only; it is not one implementation issue. | After the core contract is stable, create separate renderer/integration issues per host. Verify each real host API first. Use a separately packaged artifact only for a verified technical constraint; never require per-repo registration. |

Do not file a separate issue for a generic plugin/provider framework unless the
first concrete adapter demonstrates that the existing boundaries cannot serve
it. Keep the control plane in a later initiative phase and resolve #141/#142
before implementing pause, resume or retry actions.

### 25.9 Guardrails for issue drafting

Before creating issues, the planning agent must preserve these scope decisions:

1. **Findings MVP:** show counts by severity/state and a PR link. Read full
   details only through an explicit provider. Do not add local findings storage
   solely for the UI.
2. **Events:** the stream is forward-only from the release that introduces it.
   Fix event ID, ordering/cursor, deduplication, retention and provenance.
   Never synthesize historical granular events from SQLite or status snapshots.
3. **Herdr:** verify its real supported extension and installation mechanisms
   before choosing the adapter form. A plugin is the desired experience, not
   an assumed API; use a supported sidecar/adapter/hook if needed. Host-level
   installation and automatic discovery of all open workspaces are required.
4. **Claude Code:** treat the Mod as an official, version-pinned integration.
   Include a supported fallback when the Mod is incompatible or fails to load.
5. **Codex:** verify current official capabilities before issue creation. Do
   not assume a native sidebar or panel; an adapter plus `cc watch`/TUI or
   another documented surface is a valid deliverable.
6. **Renderers:** the renderer roadmap row is not one issue. Once the core
   contract is stable, create separate implementation issues for Herdr, Claude
   Code and Codex, each with its own API/version risks and acceptance criteria.
7. **Sequence:** snapshot/projection → forward-only events → CLI and
   machine-readable access → providers → per-host renderers. Do not start with
   dashboard styling; renderers demonstrate consumption of the core contract.
8. **Sources of truth:** `cycle_status.py`, SQLite telemetry, executor receipts
   and host APIs retain their respective authority. Any projection, cache or
   event store must explain the specific gap it fills, retention and rebuild
   behavior. Do not duplicate source state without that rationale.
9. **No-cycle workspaces:** keep host-discovered repo/worktree/agent/Git state
   visible without a Code Cycle run and enrich it automatically when a matching
   run appears. This is a core requirement, not a later enhancement.
10. **Control plane:** pause, resume, retry and rereview remain outside the
    observational MVP and depend on the applicable host permission and human
    control contracts.
11. **Optional host telemetry:** preserve `should`, `may` and `where available`
    as optional behavior. Do not create dependencies just to obtain metrics a
    host does not expose; mark them unavailable and degrade gracefully.
12. **Issue boundaries:** identify every issue as `core`, `provider`,
    `renderer` or `integration installer`. Split an issue that mixes these
    responsibilities unless the combined scope is independently reviewable.

---

## 26. Acceptance criteria for the complete feature

The monitoring initiative is functionally complete when all of the following are true:

Treat these as initiative-level outcomes, not as acceptance criteria for one
issue or one repository milestone. The core milestone is the versioned
snapshot, current-cycle projection and CLI renderer; Herdr, Claude and Codex
renderers are separate integrations with their own host/version support.

1. Code Cycle exposes a versioned normalized runtime snapshot.
2. Code Cycle exposes a versioned normalized event stream.
3. Monitoring works without Orca.
4. Orca enriches, rather than replaces, the base monitoring model.
5. `cc status` works without any graphical host.
6. A standalone continuously updating view is available.
7. Claude can show task/session information without Herdr.
8. Claude can expand to cycle-level information when used standalone.
9. When Claude is hosted in Herdr, Claude defaults to local/session information while Herdr shows the global view.
10. Herdr can show all active cycles, agents, finding summaries, tests, worktrees, receipts and recent events available from their respective providers; full finding details appear only when an explicit provider supplies them.
11. The design does not require model calls for UI refresh.
12. Frontends reconnect without corrupting cycle state.
13. Missing telemetry degrades gracefully.
14. Sensitive values are not exposed by default.
15. The MVP does not add local findings persistence solely for rendering.
16. Codex has the best supported equivalent integration for its actual extension capabilities.
17. All renderers consume the same underlying state model rather than maintaining independent workflow state.
18. Installing each host integration once discovers every workspace the host exposes, with no repository allowlist or project registration.
19. A workspace remains visible with available host/repository/Git state when no Code Cycle run is active; matching Code Cycle state enriches it automatically.
20. `cc integrations status` distinguishes host detection, integration installation and active connection; missing integrations produce non-blocking install guidance.
21. Official host integrations ship with the Code Cycle Toolkit release wherever the host's packaging mechanism permits, and each renderer supports the schema versions declared by that release.

---

## 27. UX target

The desired UX is:

> At any moment, the user can understand what Code Cycle is doing, what each agent is doing, what has failed or is blocked, what comes next, and where to intervene, without reading raw logs or asking the model for status.

Herdr should be the most powerful global visualization.

Claude/Codex should provide context-appropriate local visualization.

Standalone operation must remain first-class.

---

## 28. Example adaptive behavior

### Herdr + Claude + Orca

```text
Herdr:
    Level 3
    all cycles
    all agents
    worktrees
    findings
    receipts
    tests/CI
    Orca graph/events

Claude:
    Level 1
    own session
    current task
    context/model/tools
    local worktree
    current finding
```

### Claude + Orca, no Herdr

```text
Claude:
    Level 2
    complete active cycle
    agents/workers
    stages
    findings
    tests
    Git
    receipts
    own session telemetry
```

### Claude only

```text
Claude:
    Level 1
    own session
    Git/worktree
    active Code Cycle task if present
    tests/findings when available
```

### Herdr + Codex, no Orca

```text
Herdr:
    Level 3
    tasks
    agents
    worktrees
    tests
    findings
    Git

Codex:
    Level 1
    own session/task state
```

---

## 29. Future extensions

The architecture should permit future renderers without changing the core model:

- web dashboard;
- VS Code extension;
- JetBrains plugin;
- tmux integration;
- desktop notifications;
- remote read-only dashboard;
- historical cycle analytics.

These are not required for the initial implementation.

---

## 30. Instruction for the planning/issue-generation agent

Use this specification as the product and architecture target.

Before proposing or creating issues:

1. inspect the repository;
2. map each requirement to existing code;
3. identify what already exists;
4. identify the minimum new abstractions required;
5. verify current Claude Code Mod capabilities from authoritative documentation if implementation depends on them;
6. verify current Codex UI/extension capabilities from authoritative documentation if implementation depends on them;
7. prefer repository conventions over introducing parallel infrastructure;
8. produce a dependency-aware issue plan;
9. flag only genuine product decisions that cannot be inferred safely;
10. create issues only after the architecture-to-code mapping is complete.

The issue set should be sufficient for another agent to implement the monitoring initiative incrementally without needing to reinterpret this specification.
