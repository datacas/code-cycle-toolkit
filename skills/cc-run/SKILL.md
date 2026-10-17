---
name: cc-run
description: Use this skill to detect how a project's applications are meant to start, bring up the required services in the right order, and confirm they are actually responding. It reports each service with its process, port, URL, and stop command, and never silently changes ports or kills processes it does not own.
---

# Run

Start the project safely and reproducibly.

## Repository conventions

Read the repository's own instructions when they exist — `AGENTS.md`,
`CLAUDE.md`, `CONTRIBUTING.md`, or the documentation they point to — and prefer
them over the defaults in this skill. None of them is required: when a file is
absent, use the defaults here and say which convention you applied. Never
report a missing instruction file as a blocker on its own.

Do not ask again for actions the user explicitly requested or that this skill's
documented workflow necessarily performs within that request. Normal workflow
artefacts such as the working branch, commits, pull request, and temporary files
are covered by that authorization only when this skill's documented workflow
performs them. This shared rule does not expand a skill's capabilities.

Ask before creating an unrequested persistent repository or external artefact,
such as a configuration file, migration, durable directory, label, or additional
branch. State what is missing, why it is needed, and what you would create; wait
for the answer. Follow any stricter approval rule in the repository instructions.
Never abandon the task merely because an optional artefact is absent.

Always ask before deleting or overwriting something the cycle did not create;
changing the working structure (no branch, another branch, or the base branch);
changing scope, including whether work belongs in another issue or this one;
merging or force-pushing; publishing; adding dependencies; or changing public
contracts, data, or security. Here, publishing means releases, tags, package
publication, or comments outside the cycle's change request. This list grants
no authority: each skill's publication boundary still applies, and read-only
stages still cannot publish unless their own workflow explicitly permits it.
An explicit instruction already authorizing the action settles the question;
do not ask again.

For native dispatches, write temporary helpers to the supplied per-dispatch
`TMPDIR`: use `$TMPDIR/pr-comment.md` for comment bodies,
`$TMPDIR/report.md` for reports, and `$TMPDIR/<check>.log` for check logs
(replace `<check>` with the check's fixed name). Keep these names stable rather
than adding IDs or random suffixes. The runtime isolates concurrent dispatches
and removes their temporary directories at completion. Orca workers retain
their existing temporary-directory behavior; native TMPDIR handling does not
apply to them.

## Output language

Write every published artefact — status reports, notes, and the final response
— in one language, chosen in this order:

1. an explicit request, such as `lang=es` in the invocation or "report in
   English" in plain language;
2. the language of the repository's own instructions (`AGENTS.md`, `CLAUDE.md`,
   `CONTRIBUTING.md`) when one of them exists;
3. the language of the surrounding project documentation;
4. English, when nothing above resolves.

Command names, service names, ports, and paths are never translated.

## Rules

- Detect the package manager from lockfiles rather than assuming one.
- When dependencies are not installed, say so and ask before installing them.
- Never use an alternative port without reporting it.
- Never kill a process you did not start.
- Never use a forced kill except as a last resort, and say so when you do.
- Keep the relevant logs visible.
- Identify clearly which process corresponds to which service.

## Flow

1. Identify the project root.
2. Confirm that the runtime configuration the project needs is present. When
   only a template exists, stop, tell the user, and ask before deriving a
   working configuration from it. Do not start the project without valid
   configuration.
3. Detect the applications and services present: frontend, API, workers and
   queues, databases, auxiliary services.
4. Locate the official start command for each service.
5. Check the environment variables each one requires.
6. Check whether the needed ports are already taken. When one is, report it —
   do not silently choose another.
7. For projects with a database, check migration state before starting the
   API. When migrations are pending, report it and ask whether to run them.
8. Start the services in dependency order:
   - databases and auxiliary services first;
   - then the API;
   - then workers and queues;
   - then the frontend.
9. Wait until each service responds before starting the next one.
10. Check:
    - the health endpoint or root route;
    - startup errors in the logs;
    - browser console errors where that applies.
11. Report every service: process, PID, port, URL, and stop command.

## Multi-application layouts

When a project splits into separate applications, for example:

```text
project/
├── api/
└── frontend/
```

run each command from its own directory. Do not assume the parts share a
package manager, a lockfile, or an environment file. Detect each one
separately.

## Output format

### Services

| Service | Status | URL/Port | PID | Stop command |
|---|---|---|---|---|

### Problems

### How to stop everything

Give the exact commands to stop every service you started, in reverse
dependency order.
