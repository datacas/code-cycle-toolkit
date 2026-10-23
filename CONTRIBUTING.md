# Contributing

Thanks for improving Code Cycle Toolkit. The rules below exist because the
skills are copied into other people's repositories, where nothing about this
one is available to them.

## Run the validator

```bash
python3 scripts/validate-package.py
python3 -m unittest discover -s tests -v
```

It must pass before a pull request is opened. CI runs it on Linux and Windows,
plus a real installation of every skill into every host layout. On Windows, use
`py -3 .\scripts\validate-package.py`.

The validator checks every skill's frontmatter, name, and description limits.
It checks that the duplicated sections are identical, and it catches
fixed-language output mistakes and literal-output directives. It checks that the
manifest names and versions agree, that the JSONC parses, that every skill
appears in `README.md` and the README links the Claude-to-Codex adapter
contract, and that no private data is present. Native manifest validation is
also available: `claude plugin validate .`.

Documentation lives in `docs/`, indexed by [docs/README.md](docs/README.md).
Each concept has one main page. `README.md` summarises and links rather than
repeating. When a change alters behaviour, update the page that owns that topic.

## Non-code changes take a fast path

`Validate package` has two required checks, `Skills, manifests, and Bash
installer` and `PowerShell installer`, and a pull request cannot merge while
either is missing. So neither is ever skipped by a path filter. Instead each job,
and each of the four Windows test shards, classifies the change first with
`scripts/fast_path.py`, comparing a pull request with its merge base and a push
with `before..after`. A change that touches only the allowlist below reports both
checks green after a scan and a few mapped tests; anything else runs everything
as before.

**The allowlist** is exactly these paths:

- `.code-cycle.yml`;
- Markdown at the repository root, except `README.md`, which the validator reads
  (`AGENTS.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`);
- everything under `docs/`;
- everything under `.github/ISSUE_TEMPLATE/`.

Every other path runs in full, including `skills/`, `scripts/`, `tests/`,
`README.md`, `opencode.jsonc`, the installers and every other workflow. These
always run in full even if a later edit widened the list: both plugin manifests,
`release.yml`, `validate.yml`, `scripts/fast_path.py` and
`scripts/fast-path-tests.json`. A change to a `## ` heading of `CHANGELOG.md`
also runs in full, because `release.yml` cuts a release from those headings.

**The fast path** runs the privacy and metadata-sidecar scan
(`python3 scripts/validate-package.py --scan-only`, the same scan the full
validator runs) and the tests mapped to the changed files. The Windows shards
skip their other tests but still finish successfully. The job summary says
whether a job was `SKIPPED_NON_CODE` or `TESTS_PASSED`, which validations ran and
which were skipped, and which paths caused a full run. Runner queueing is outside
this repository's control; the fast path covers the jobs' own work.

**Classification fails closed.** An error, an empty or unreadable diff, an event
other than a pull request or a push, a diff that cannot be computed, a test
mapping that cannot be read, or a failing dependency guard all run the full
validation. So does a classification step that does not run at all, because
every other step runs unless the classification reports `fast`.

**Tests that read an allowlisted file.** A few tests read `.code-cycle.yml`,
`docs/telemetry.md`, `docs/configuration.md` or `docs/spikes/**`. They are listed
in `scripts/fast-path-tests.json`, and the fast path runs the ones mapped to the
changed files. A static guard, `tests/test_fast_path.py`, parses every test
module and fails when one builds a path from the repository root that is or may
be allowlisted and is not registered. When you add such a test, register it
under the group that covers the path, as a module, a class, or one test method,
in the same pull request. A path the guard cannot see, such as one assembled from
a string, is not detected, so write the path with `/` or `joinpath` from the
root. If a dependency cannot be registered simply, remove the path from the
allowlist instead.

## Skill-authoring rules

**Portability first.** A skill may not require the target repository to have a
particular file, label, CI gate, framework, or directory. Read `AGENTS.md`,
`CLAUDE.md`, and `CONTRIBUTING.md` when they exist, prefer them over the
skill's defaults, and fall back to the defaults with an explicit note when they
do not. A missing instruction file is never a blocker on its own.

**Respect the workflow's authorization boundary.** Do not ask again before
creating normal artefacts explicitly requested by the user or necessarily
produced by this skill's documented workflow, such as its branch, commits, pull request,
or temporary files. Ask before creating an unrequested persistent repository or
external artefact, such as a configuration file, migration, durable directory,
label, or additional branch.

The shared conventions do not grant additional capabilities. Keep the
skill-specific *Already authorized actions* lists limited to implementation,
resolution, verification, and the two coordinators, with actions appropriate to
each role. Preserve the explicit Docker grant requirement. Temporary helper
instructions use `$TMPDIR/pr-comment.md`, `$TMPDIR/report.md`, and
`$TMPDIR/<check>.log`; the native runtime owns isolation and cleanup.

**No hardcoded output language.** Published text follows the target repository,
resolved by the shared *Output language* section. Do not require human-readable
text to use one fixed-language sentence. Examples may use English when they are
clearly examples and the instruction requires natural wording in the selected
language. The opt-in trigger list may quote phrases a user could type.

**Machine-readable tokens stay in English.** `REV-xxx`, the severities, finding
statuses, the dispositions `valid|debatable|incorrect|obsolete|needs_clarification|-`,
`blocks:yes|blocks:no`, the review and triage run lines, functional statuses, JSON keys, and enum-like
values are part of the parsing contract. Free-text JSON values such as
`summary`, `reason`, and `error` follow the selected output language.

**Passes do not publish when delegated.** `cc-pr-review`, `cc-code-review`,
`cc-security-review`, and `cc-verify` return findings to the calling cycle skill,
which publishes one consolidated comment. Only a directly invoked pass may
publish its own.

**Never merge.** No skill merges a pull request or asks another agent to.

## The duplicated sections

Skills install as independent directories, so a shared file would not travel
with them. Several sections are therefore duplicated verbatim:

| Section | Present in |
|---|---|
| `## Repository conventions` | every skill |
| `## Output language` | every skill except `cc-run`, which publishes no GitHub artefact and states its own variant |
| `### The ORCHESTRATION_RESULT block is opt-in` | `cc-initial-review`, `cc-rereview`, `cc-resolve-comments` |
| `### Where the block goes` | the same three |
| `### The PR comment is the machine-readable record` | the same three |
| `## Checks of the pushed head` | `cc-implement-issue`, `cc-resolve-comments` |
| `## Workspace tools and evidence` | `cc-code-review`, `cc-implement-issue`, `cc-initial-review`, `cc-issue-review`, `cc-pr-review`, `cc-rereview`, `cc-resolve-comments`, `cc-security-review`, `cc-verify` (full rules); both orchestrators (rules 1 and 4) |
| `## Issue review before implementation` | both orchestrators |
| `## Asking the user` | every cycle skill and `cc-provider-bootstrap`; its `questions` example must pass `stop_questions.questions_errors` |
| `### Complete evidence` | `cc-code-review`, `cc-implement-issue`, `cc-initial-review`, `cc-issue-review`, `cc-pr-review`, `cc-rereview`, `cc-resolve-comments`, `cc-security-review`, `cc-verify` |

Editing one copy means updating every copy in its listed group. The validator
compares each group's copies byte for byte and rejects product names from the
workspace-tools section, so these workflow contracts cannot drift silently.

## Adding a skill

1. Create `skills/<name>/SKILL.md` with `name` and `description` frontmatter.
   The name must match the directory and be kebab-case.
2. Add the two shared sections verbatim.
3. Register the name in `scripts/validate-package.py`, under `CYCLE_SKILLS` or
   `SUPPORT_SKILLS`.
4. Add it to `EXPECTED_SKILLS` in `.github/workflows/validate.yml`.
5. Document it in the README skill tables — the validator checks that too.

## Commits and pull requests

Keep a change focused on one concern. Describe what changed in the skills'
behaviour, not only which files moved: these files are instructions, so a
wording change can be a behaviour change.

### Commit messages and pull request titles

Use Conventional Commits for commit subjects and pull request titles. Pull
requests are squash-merged, so the pull request title becomes the commit subject
on `main`.

Write the subject as `<type>(<optional scope>): <description>`. Use one of these
types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`,
`ci`, or `chore`. Keep the whole subject to 72 characters or fewer. Write the
description in imperative English, without a final period or ellipsis. For
example: `feat(install): add one-command installers for skills and runtime`.

Add a body only when it provides useful context. For an incompatible change,
include a `BREAKING CHANGE:` note in the body.

This convention applies to pull request titles as well as commit subjects.
Toolkit skills must follow the target repository's commit-message convention
and output language; this repository's rule does not change their portability.

## Release checklist

- Confirm that no repository-specific names, local paths, credentials, or customer data are present.
- Run the package validator and `claude plugin validate .`.
- Confirm both plugin manifests carry the version being released.
- Add the release to `CHANGELOG.md`.
- Test Bash installation on Linux, macOS, and WSL.
- Test PowerShell installation on Windows.
- Test global and repository-level installation for each host.
- Test one manual skill and one complete cycle in each supported host.
- Merge. `.github/workflows/release.yml` runs after `Validate package` passes on
  `main`. When the manifest version has no tag yet, it creates `vX.Y.Z` on that
  commit and publishes a GitHub release whose notes are the version's
  `CHANGELOG.md` section. A merge that leaves the version unchanged releases
  nothing. It can also be started by hand from the Actions tab.
