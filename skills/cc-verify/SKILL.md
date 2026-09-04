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
`blocks:yes|blocks:no`, every functional status, and every JSON key in
`ORCHESTRATION_RESULT` stay exactly as written in this skill in every language.
Keep enum-like JSON values such as `skill` and `status` unchanged. Write free-text
values such as `summary`, `reason`, and `error` in the selected language. Preserve
repository names, paths, references, commit SHAs, and command output verbatim.

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
10. Summarise what ran, what passed, what failed, what could not be verified
    and why, and the residual risks.

`cc-run` handles starting the application when steps 8 and 9 need it.

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

State exactly one of:

- Verified
- Verified with reservations
- Not verified
- Failed

Absence of execution is never success, and a skipped check is never a passed
one.
