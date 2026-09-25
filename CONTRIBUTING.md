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

## Skill-authoring rules

**Portability first.** A skill may not require the target repository to have a
particular file, label, CI gate, framework, or directory. Read `AGENTS.md`,
`CLAUDE.md`, and `CONTRIBUTING.md` when they exist, prefer them over the
skill's defaults, and fall back to the defaults with an explicit note when they
do not. A missing instruction file is never a blocker on its own.

**Respect the workflow's authorization boundary.** Do not ask again before
creating normal artefacts explicitly requested by the user or necessarily
produced by the documented workflow, such as its branch, commits, pull request,
or temporary files. Ask before creating an unrequested persistent repository or
external artefact, such as a configuration file, migration, durable directory,
label, or additional branch.

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

Editing one copy means editing all of them. The validator compares the copies
byte for byte and fails on drift, which is the whole point: these sections
define a state-recovery contract that breaks silently when the copies disagree.

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
