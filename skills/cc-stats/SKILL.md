---
name: cc-stats
description: Summarize local Code Cycle telemetry for the configured repository, with clear trends and honest sample sizes.
---

# Code Cycle Stats

Use this skill when someone asks for Code Cycle telemetry, cycle performance,
review outcomes, routing or Jev comparisons, or invokes `/cc-stats`.

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

## Run the report

1. Use the current repository's `.code-cycle.yml` and read
   `code_cycle.repository.selector`. Do not guess the repository from a Git
   remote or use telemetry from another repository.
2. Locate the installed runtime component `stats.py`. Check the project runtime
   at `<repository>/.code-cycle/runtime/stats.py`, then the user's global
   runtime at `<user home>/.code-cycle/runtime/stats.py`. In a toolkit checkout,
   `scripts/stats.py` is the same component.
3. Run the component with Python 3 and the repository root as the working
   directory. It reads the default telemetry database itself and prints a
   Markdown report:

   ```sh
   python3 /resolved/path/to/stats.py
   ```

   Substitute the actual component path you located. Use `python` or `py -3`
   on Windows if needed. For machine-readable aggregates, add `--format json`.

4. Use the default last 30 days unless the person asks for a different window.
   Pass `--days N` for a requested number of days, or `--all-time` for the full
   recorded history. For example, “show me the last 7 days” maps to
   `--days 7`.
5. Present the returned summary directly. Do not recalculate rates, rank a
   profile with insufficient evidence, or turn unknown and not-applicable data
   into zeroes or failures.

## What the report means

The report includes task and stage counts, first-pass approvals with their
numerator and denominator, daily activity, role and profile counts, fallback
stages, dispatch blockages, model drift, verdicts, finding severity, reported
test verification, measured duration and cost, and Jev shadow comparisons when those records exist. It
compares the selected period with the previous period only when each has at
least the telemetry module's minimum sample of 10 tasks. Rates and suggested
profile outcomes include their sample counts and stay unknown below that
threshold. Confidence buckets show their numeric ranges.

The component opens the local SQLite database read-only, scopes every query to
the configured repository selector, and emits aggregates only. Never inspect
or repeat task identifiers, cycle identifiers, comments, prompts, paths, diffs,
outputs, secrets, or raw telemetry rows. Duration and cost are shown only when
measured; routing cost estimates are not presented as actual cost.

If the repository identity is not configured, explain that
`code_cycle.repository.selector` in `.code-cycle.yml` is required. If the
database is absent or has no rows for this repository, report that no telemetry
is available yet. Do not create a database or configuration file.
