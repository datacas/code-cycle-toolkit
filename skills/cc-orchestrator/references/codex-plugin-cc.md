# Claude-to-Codex Orchestration Adapter

Read this reference only when `cc-orchestrator` is running in Claude Code and
the optional `codex-plugin-cc` integration may be used. The plugin is an
external host capability. Code Cycle Toolkit does not ship, install, update,
authenticate, or wrap its implementation.

Official project: <https://github.com/openai/codex-plugin-cc>

## Mode resolution

The supported execution modes are:

- `single_agent`: Claude executes every stage;
- `claude_codex`: Claude implements and resolves; Codex performs initial review
  and rereview;
- `auto`: discover a usable adapter and a saved project preference, otherwise
  continue with `single_agent`.

An explicit `orchestration_mode` wins over `.code-cycle.yml`. Project
configuration may contain:

```yaml
code_cycle:
  orchestration:
    mode: claude_codex
```

Preserve unrelated configuration keys. Ask before creating or updating
`.code-cycle.yml`, show the proposed non-secret change, and use the user's
answer only for the current run when they decline persistence.

## Capability discovery

Select `claude_codex` only when all of these are true:

1. the current host identifies itself as Claude Code;
2. the host declares a task-capable `codex-plugin-cc` command, agent, or tool
   that it can invoke and from which it can recover a durable result;
3. Codex is installed and authenticated according to the plugin's own readiness
   check;
4. the delegated Codex session can load the Code Cycle review skills; and
5. repository and provider access required by the review stage is available.

When Claude Code's `Agent` tool advertises the plugin subagent type
`codex:codex-rescue`, invoke that subagent with the complete review-stage task.
The plugin's public `/codex:rescue` command is the equivalent user-facing route;
a command that the coordinator cannot invoke and receive a result from is not
an autonomous orchestration capability. Native `/codex:review` output alone is
not a replacement for `cc-initial-review` or `cc-rereview`: the cycle also needs
prior finding state, provider context, publication, and a strict structured
result.

Use only interfaces the host declares. Do not inspect plugin cache directories,
invoke private scripts, guess a versioned path, or run a delegated task merely
to test whether delegation exists. If setup or login is missing, direct the
user to the plugin's `/codex:setup`; do not install packages or start login on
their behalf.

## First-run notice and preference

Only Claude Code needs this notice. Skip it when the user supplied an explicit
mode or the project already has a saved mode.

When no acknowledgement exists, briefly explain that Code Cycle can optionally
delegate initial review and rereview to Codex through `codex-plugin-cc`, and
that the ordinary single-agent cycle remains available. Do not turn the notice
into an installation wizard or block the active run.

Record an acknowledgement at notice schema version `1` only in a non-secret
host-local Code Cycle state store, outside the repository. Use the same user
configuration base as provider-health state and the platform-equivalent
application-data directory on Windows. Before creating a new persistent file
or directory, ask for confirmation as required by the repository-conventions
policy. Store only the notice name, schema version, and acknowledgement time;
never store project content, provider responses, credentials, or transcripts.
If persistence is declined or unavailable, continue normally and accept that a
future session may show the notice again.

If the plugin is usable but the project has no saved mode, ask once whether to
use `claude_codex` for this project. Offer to persist the answer in
`.code-cycle.yml`. A declined mixed mode resolves the current run to
`single_agent`; it is not a provider or workflow failure. When the notice,
mode choice, and persistence permission are all unresolved, group them into one
concise question rather than interrupting startup several times.

## Stage ownership

In `claude_codex` mode the ownership is fixed for the complete run:

| Stage | Executor |
|---|---|
| provider bootstrap | Claude/current coordinator |
| implement issue | Claude |
| initial review | Codex |
| resolve comments | Claude |
| rereview | Codex |
| final validation and handoff | Claude/current coordinator |

The coordinator owns sequencing, iteration limits, state validation, and the
manual merge boundary. Codex owns the substantive review stage it receives,
including its one consolidated change-request comment. Claude must not redo the
same review and substitute its own judgement after a valid Codex result.

## Delegated review contract

For each Codex review task, provide:

- the exact skill to run: `cc-initial-review` or `cc-rereview`;
- the complete `PROVIDER_BOOTSTRAP_RESULT`;
- repository, issue provider, code host, work-item ID, and change-request ID;
- expected base, branch, and current head SHA;
- the complete previous `ORCHESTRATION_RESULT` for rereview;
- current open `REV-xxx` identifiers and the selected output language;
- an explicit request for the strict `ORCHESTRATION_RESULT` block; and
- the rule that product code, commits, branches, and the working tree must not
  change, while the normal review comment is authorised for publication.

The review stage is source-read-only but not entirely side-effect-free: its
consolidated provider comment is a required external write. Make that boundary
explicit in the delegated task so the plugin grants only the capability needed
for publication while Codex remains forbidden from editing or committing the
repository.

Capture the pre-dispatch head SHA and working-tree snapshot. After the task,
validate all of the following before accepting the result:

- the result delimiters and JSON are present and parseable;
- `skill`, functional `status`, provider identifiers, repository, and
  change-request identifier match the dispatched stage;
- `reviewed_head_sha` equals the expected and current change-request head;
- `comment_url` exists when the review stage published its required comment;
- every reported finding uses a stable `REV-xxx` identifier; and
- local `HEAD` and the complete working-tree snapshot are unchanged.

The configured code host and published change-request comment remain
authoritative. If the transport output is incomplete, attempt the existing
single recovery path from that comment. Do not infer success from free-form
Codex prose.

If Codex changed local files or `HEAD`, stop with `FAILED`, report the exact
unexpected delta, and leave it untouched for the user to inspect. Never reset,
discard, commit, or push those changes as part of review recovery.

## Failure and fallback

- In `auto`, an unavailable adapter before implementation resolves to
  `single_agent` with a brief note.
- In explicit or saved `claude_codex`, an unavailable or unauthenticated adapter
  is `BLOCKED` before implementation.
- Once implementation starts in `claude_codex`, a plugin failure stops the run;
  never switch reviewers silently in the middle of the cycle.
- Missing or malformed structured output after comment recovery is `FAILED`.
- A provider, permission, or connectivity failure is `BLOCKED` and invalidates
  the affected provider-health entry under the provider bootstrap contract.
- Background delegation is allowed only when the host provides a durable
  completion event and result retrieval. Otherwise wait for the review result
  in the foreground.

The user may resume with an explicit `single_agent` mode after a stopped mixed
run. Preserve the current change-request state and existing `REV-xxx` IDs.
