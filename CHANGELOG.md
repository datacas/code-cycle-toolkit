# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Finding headers carry a disposition token: `#### [REV-004] · medium · resolved · valid · blocks:yes — Short title`.
  It records what the first resolver made of the finding, independently of what
  happened to the code.
- A review run line opens every published review, identifying the run, profile,
  requested and resolved model, effort, and schema version.
- A triage run line records the commit every finding was judged against, so a
  frozen triage is verifiable rather than merely asserted.
- `scripts/review_contract.py`, the reference parser for the published record,
  and `scripts/calibration_store.py`, the out-of-repository attribution store for
  an experimental paired review.
- Optional `calibration.profiles` in `.code-cycle.yml`, and `paired_review` in
  `cc-orca-orchestrator`, for dispatching two reviewers over one commit. Off by
  default. Reviewer isolation is a precondition: a pair is dispatched only with a
  worktree each or strictly sequentially, and the isolation mode and observed head
  SHAs are recorded so a contaminated pair can be excluded later.
- `docs/instrumentation.md`.

### Changed

- `cc-resolve-comments` classifies every finding against one commit and records
  the dispositions before it edits any code, instead of triaging and fixing one
  finding at a time.
- A disposition is assigned once and preserved verbatim afterwards; `cc-rereview`
  republishes it unchanged.
- `ORCHESTRATION_RESULT` mirrors the run identity and `finding_outcomes` when it
  is enabled. `resolved_findings` and `unresolved_findings` are unchanged.
- The package validator checks the documented contract against its parser.
- The validator now catches a Windows `Zone.Identifier` sidecar written with a
  colon, the form that actually reaches WSL trees; the previous check only
  matched a dot and let it through.

### Compatibility

- Finding headers with four tokens and no disposition stay valid and read as
  not triaged. No open change request needs migrating.

## [0.2.0] - 2026-09-10

### Added

- An optional `cc-orchestrator` adapter contract for alternating Claude and
  Codex: Claude implements and resolves findings while Codex performs initial
  review and rereview through an externally installed `codex-plugin-cc`.
- First-run discovery and preference rules for the optional Claude-to-Codex
  mode, including a non-secret acknowledgement and project configuration.
- A provider contract that separates issue providers (GitHub Issues, Plane, and
  Jira) from code hosts (GitHub and Bitbucket), including explicit provider
  resolution, capability boundaries, native identifiers, and safe work-item
  linking.
- A `cc-provider-bootstrap` skill that fills missing provider configuration,
  validates read-only access at startup, and caches non-secret health metadata
  for later runs.
- Five supporting skills now ship with the toolkit instead of being assumed to
  exist on the host: `cc-pr-review`, `cc-code-review`, `cc-security-review`,
  `cc-verify`, and `cc-run`. Each runs standalone; review and verification
  skills can also run as delegated passes without publishing their own comment.
- A shared *Output language* rule: published text follows an explicit `lang=`
  argument, then the repository's own instructions, then the issue and pull
  request, then English. Machine-readable tokens and enum-like values stay in
  English while free-text structured-result values follow the selected language.
- A shared *Repository conventions* rule: repository instruction files are
  optional and preferred when present, requested workflow artefacts need no
  duplicate confirmation, and unrequested persistent artefacts do.
- `CONTRIBUTING.md`, `SECURITY.md`, and this changelog.

### Changed

- `cc-orchestrator` now resolves `auto`, `single_agent`, or `claude_codex`
  execution before implementation, validates Codex stage results and branch
  immutability, and never changes reviewers silently after a mixed run starts.
- Cycle and review skills now use provider-neutral work-item and change-request
  terminology. GitHub-specific commands are documented as adapter examples,
  not universal requirements.
- Structured results now carry `issue_provider`, `issue_id`, `code_host`,
  `change_request_id`, and `change_request_url`; `issue_number` and `pr_number`
  remain compatibility aliases for existing GitHub-oriented consumers.
- `cc-implement-issue` and both orchestrators now run the provider bootstrap
  before implementation and pass its validated context through the cycle.
- Sensitivity triage no longer requires an `AGENTS.md` with a specific trigger
  list, and matches labels by meaning rather than by an exact taxonomy.
- CI reporting names the checks a repository actually defines instead of a
  fixed set of gate names.
- The three review-cycle skills no longer name a specific orchestration CLI on
  their manual path; they follow whatever worker contract is injected.
- `cc-orca-orchestrator` asks before creating its run-scoped results directory.
- `cc-orchestrator` returns the same result envelope as `cc-orca-orchestrator`.
- The package validator now checks twelve skills, verifies that the deliberately
  duplicated sections have not drifted, rejects known fixed-language output and
  literal-output directives, requires both manifests to agree on version,
  parses `opencode.jsonc` as JSONC, and requires every skill to appear in the
  README.
- CI exercises the Bash installer for real, lints it with shellcheck, tests the
  overwrite and missing-directory paths on both platforms, and checks discovery
  of all skills through the pinned `skills` CLI.

### Fixed

- Hardcoded Spanish sentences that every skill was required to publish verbatim,
  regardless of the target repository's language.
- `cc-resolve-comments` delegated to a `project-code-review` hook that no other
  skill named and that the README never documented.
- The Bash installer scopes its helper variables and rejects a nonexistent
  `--project-dir` with a clear message instead of a shell error.
- CI validated `opencode.jsonc` with a strict JSON parser, which would have
  failed on the first comment added to a file whose extension exists to allow
  them.

## [0.1.0]

- Initial six cycle skills, cross-agent manifests, installers, and validator.
