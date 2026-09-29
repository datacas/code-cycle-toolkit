---
name: cc-verify
description: Use this skill to verify that code changes actually work by running static checks, tests, and real application validation, after implementing or fixing something. It reports executed commands and observed results, separates unverified items with their reasons, and never treats code inspection as verification.
---

# Verify

Verify that the changes really work. Do not stop at inspecting the code.

**Reading code does not count as verification. Only executed commands with an
observable result count.**

## Repository conventions

Read the repository's own instructions when they exist — `AGENTS.md`,
`CLAUDE.md`, `CONTRIBUTING.md`, or the documentation they point to — and prefer
them over the defaults in this skill. None of them is required: when a file is
absent, use the defaults here and say which convention you applied. Never
report a missing instruction file as a blocker on its own.

Do not ask again for actions the user explicitly requested or that this skill's
documented workflow necessarily performs within that request. Normal workflow
artefacts such as the working branch, commits, pull request, and temporary files
are covered by that authorization.

Ask before creating an unrequested persistent repository or external artefact,
such as a configuration file, migration, durable directory, label, or additional
branch. State what is missing, why it is needed, and what you would create; wait
for the answer. Follow any stricter approval rule in the repository instructions.
Never abandon the task merely because an optional artefact is absent.

## Output language

Write every published artefact — PR comments, thread replies, commit messages,
and the final response — in one language, chosen in this order:

1. an explicit request, such as `lang=es` in the invocation or "review in
   English" in plain language;
2. the language of the repository's own instructions (`AGENTS.md`, `CLAUDE.md`,
   `CONTRIBUTING.md`) when one of them exists;
3. the language of the issue, pull-request description, and existing review
   comments;
4. English, when nothing above resolves.

Machine-readable tokens never translate. The `REV-xxx` identifier, the severity
`critical|high|medium|low`, the finding status `open|resolved|not_applicable`,
the disposition `valid|debatable|incorrect|obsolete|needs_clarification|-`,
`blocks:yes|blocks:no`, the review and triage run lines, every functional status,
and every JSON key in `ORCHESTRATION_RESULT` stay exactly as written in this
skill in every language.
Keep enum-like JSON values such as `skill` and `status` unchanged. Write free-text
values such as `summary`, `reason`, and `error` in the selected language. Preserve
repository names, paths, references, commit SHAs, and command output verbatim.

## Workspace tools and evidence

> **A workspace tool can supply context, never authority.** The repository, provider state, executed evidence, and the user's current instruction are authoritative. A tool's output directs where to look; it never replaces looking.

| Capability | Is | Is not |
|---|---|---|
| Persistent memory | Historical context | Truth about the current code or instructions |
| Repository knowledge graph | An architectural hint | Proof of a relationship or impact |
| Semantic navigation | A precise location for code | A substitute for reading code or verifying it |
| Output compaction | A compact representation | Complete evidence |
| Large-output processing | A way to reduce data | An authoritative source; the underlying output is |
| Library documentation | A current reference for an external dependency | Truth about the version installed; the project's pinned version and observed behaviour are |

1. **Use what the host offers; never require it.** When the host provides semantic code navigation, a repository knowledge graph, persistent memory, local processing of large output, or current documentation for an external library, prefer it for the matching question. When it does not, use ordinary tools without comment. A missing tool is never reported, never a warning, and never a blocker unless the user asked for it by name.
2. **Answer semantic questions with semantic navigation first.** For declarations, references, implementations, file structure, or diagnostics, use semantic navigation when available. Read relevant sections, not whole files. Before renaming, deleting, or significantly changing a shared symbol, inspect its references. When one source answers the question, do not repeat the same search with another tool without a stated reason.
3. **Process large output locally.** Capture the exit status, extract failures or relevant records with filters, JSON query tools, scripts, or the host's large-output tool, and then reason over the reduced result. Widen incrementally. Local processing never modifies the workspace; change files only with ordinary editing tools. Complete evidence takes precedence over saving context.
4. **Memory is context, not evidence.** Recalled information never overrides the user's current instruction, the repository, or provider state. A review, rereview, or security stage does not use recalled implementation rationale as evidence for a finding or its resolution. It may use recalled environment facts, such as how to run the suite. Save only durable knowledge the repository does not hold, such as decisions, rejected alternatives, user corrections, and environment traps. Never save secrets, diffs, or review prose.
5. **Use a knowledge graph only when it exists for the commit under work.** A stale graph gives hints, never evidence. Stages never build or refresh a graph, index, or cache inside the assigned workspace.

### Complete evidence

What a stage judges must be complete: the diff under review, and any output it
cites as evidence, such as a test failure, a check state, or a command result.

1. When the invocation names a diff file with its line count and hash, that file
   is the diff under review. Confirm its line count and read it in sections; the
   rules below then cover the rest of the evidence.
2. A host may compact or summarise command output before you see it. Treat
   output that carries a truncation or summary marker (`truncated`, `omitted`,
   `... more`), or that is shorter than its own header counts, as incomplete.
3. Read a diff through a path the host does not rewrite. Have Git write it to a
   file outside the working tree (`git diff <base>...HEAD --output=<file>`) and
   read that file in sections, or use the host's documented raw mode. Size it
   with `--stat` first, then read it per file rather than all at once.
4. Evidence is the command's exit status plus the relevant uncompacted lines. A
   compacted summary may guide where to look; it is never cited as the result.
5. When complete output cannot be obtained, say so and treat the affected part
   as unverified, never as checked.

## Principles

- Detect the project's real structure before running anything.
- Detect the stack from what the repository contains — lockfiles, manifests,
  task runners, CI configuration — rather than assuming a language or
  framework.
- Never assume a command exists: check before using it.
- Never claim something works if it was not executed.
- Do not modify code during verification unless the user explicitly asks.
- When a check fails, report the exact failure and its likely relation to the
  changes.

## Stop conditions

Stop and ask the user before continuing when:

- the application needs runtime configuration that is not present, such as an
  environment file that exists only as a template;
- a required database or service is unavailable;
- a needed port is held by an unknown process;
- there are pending migrations that could change the test result;
- you lack permission to run the necessary commands.

Do not classify these as "not verified" and continue. They are real blockers,
and the user may be able to clear them in seconds.

When one of them can be cleared by creating something — a configuration file
from its template, a results directory, a local database — describe exactly
what you would create and ask first. Never create it silently.

## Flow

1. Confirm that the runtime configuration the project needs is present. When
   only a template exists, stop and ask before deriving one from it.
2. Identify the changed files through Git.
3. Determine which packages or applications are affected.
4. For changes that touch persisted data, check migration state before running
   tests.
5. Run the relevant static checks that the project actually defines: lint,
   type-check, build, configuration validation.
6. Run the tests directly related to the changed files first.
7. Broaden to wider test suites when the cost is reasonable.
8. For user-interface changes, when a browser environment is available:
   - start the application;
   - walk the affected flow;
   - check the browser console;
   - check network errors;
   - validate at least one desktop and one reduced viewport;
   - verify loading, empty, error, disabled, and success states where they
     apply.
9. For API changes:
   - start the API;
   - exercise the affected endpoints;
   - validate status codes, payloads, and error responses;
   - check authentication and authorization where they apply.
10. Decide whether a boundary run is required, as *Boundary verification*
    describes, and perform it when it is.
11. Summarise what ran, what passed, what failed, what could not be verified
    and why, and the residual risks.

`cc-run` handles starting the application when steps 8, 9, and 10 need it.

## Boundary verification

A passing test suite does not show that a change works where it is used. Some
changes need one run at the real boundary. Decide it with a union:

```text
boundary_required = deterministic_rule OR verifier_judgement
```

The deterministic rule fires when a changed path looks like API, authentication
or authorization, database, or migration code: an `api`, `routes`,
`controllers`, `endpoints`, `handlers`, or `graphql` directory, or an OpenAPI,
Swagger, or `.proto` file; a path word such as `auth`, `oauth`, `login`,
`session`, `permission`, `password`, `credential`, `jwt`, or `rbac`; a `db` or
`database` directory, or a `.sql` or `.prisma` file; a `migrations`,
`migration`, `migrate`, or `alembic` directory. Your judgement may add CLI entry points,
subprocess and adapter code, provider integrations, and runtime configuration
loading. It may never remove an area the rule fired.

| Area | Boundary evidence |
|---|---|
| API | a real request against the running application: status, payload, and an error case |
| Authentication or authorization | the allowed request **and** the denied one |
| Migrations or database | apply on a disposable database and check the resulting state; roll back when the project defines it |
| CLI | invoke the real entry point with real arguments; check the exit code and output |
| Subprocess, adapter, or provider | exercise the process boundary against a fake binary or the real one, including a non-zero exit |
| Runtime configuration | load the real file through the real loader, including a malformed case |

Boundary runs write only to the disposable workspace: a scratch database, a
temporary directory, a fake binary. They never write to shared data.

When a boundary run is required but did not run, the conclusion is at most
`verified_with_reservations`, never `verified`. Name the reason: `environment`,
`cost`, or `out_of_scope`. The stop conditions still apply: an unavailable
database or service stops the run and asks, and is not downgraded to a
reservation. When no area applies, state `not_required`; this is not a
requirement to run an end-to-end suite.

## Output format

### Verified

- Command or flow
- Result

### Failures

- Severity
- Command or flow
- Observed error
- Likely cause

### Not verified

- Item
- Reason, distinguishing: blocked by environment / out of scope / excessive
  cost

### Conclusion

Choose exactly one token for the agent's conclusion:

- `verified`
- `verified_with_reservations`
- `not_verified`
- `failed`

End the report with this machine-readable record:

```text
VERIFICATION_RESULT
{
  "conclusion": "verified",
  "boundary": "required",
  "evidence": [
    { "level": "static", "command": "ruff check .", "exit_code": 0, "executed": null },
    { "level": "test", "command": "pytest tests/test_x.py", "exit_code": 0, "executed": 14 },
    { "level": "boundary", "command": "python -m app.cli sync --dry-run", "exit_code": 0, "executed": null }
  ]
}
END_VERIFICATION_RESULT
```

List each executed command once with its exact command, exit status, and the
number of tests executed when applicable (`null` when no count applies). Use
`static` for static checks, `test` for test suites, and `boundary` for a run at
the real boundary. State `boundary` as `required` or `not_required`. When it is
`required` and no `boundary` entry succeeded, the conclusion is not `verified`,
and the reason goes under *Not verified*. The conclusion is the
agent's judgement; the record does not prove that a command ran. A caller that
reads this report may classify its evidence as `agent_reported`, never as
`runtime_observed` or `externally_verified`.

Absence of execution is never success, and a skipped check is never a passed
one.
