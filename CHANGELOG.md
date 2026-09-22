# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Trusted full-history finding recovery: `code_cycle.review.trusted_authors`
  defines a case-insensitive provider-login allow-list, invalid trust anchors
  block only when untrusted contract state could be lost, ordinary discussion
  starts an empty record, malformed/untrusted comments are recorded without
  wedging recovery, and cross-comment ID reuse blocks safely.
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
- `Campaign.set_protocol()`, `select_samples()` and `selection_log()`: the
  eligible universe, the exclusions and the selection rule are frozen before any
  sample is drawn, and every candidate's decision is recorded — not only the
  chosen ones — so why a change request entered the sample stays checkable.
  Selection ranks by a campaign-scoped hash of the identity, which cannot follow
  from how interesting a change looks. The protocol refuses to be redefined.
- `Campaign.write_pair_manifest()`: a self-contained record of a finished pair,
  written only after it closes, so an experiment does not depend forever on one
  global store file. It carries the revealed attribution, the resolver, the
  triage mode, and the root-cause groups with `shared_valid` and
  `disposition_agreement` per group.
- `triage_mode` on a triage: `blind_pure` when the resolver only classifies,
  `resolution` when it also implements the fix. Separate populations; their
  acceptance rates are never averaged together.
- `purpose` on a pair: `mechanism_validation` or `calibration`. A pair that
  proves the mechanism works is not automatically a sample for choosing a model,
  and `pairs_supporting()` filters by it so the exclusion does not depend on
  whoever writes the analysis remembering.
- `Campaign.capabilities()` and `pairs_supporting()`: which metrics a given pair
  can support, separately from whether it was collected cleanly. A pair nobody
  triaged feeds coverage and overlap but not acceptance.
- `Campaign.record_triage()`: the resolver's identity and the commit it judged,
  recorded beside the reviewers'. The resolver decides whether each reviewer was
  right, so it belongs in the instrument's record.
- `target_relation` on a pair: whether the reviewers were judging their own
  toolkit or an external project.
- `Campaign.mint_presentation_ids()` and `reveal_presentation()`: opaque
  per-finding identifiers for blind root-cause matching, so neither the labels
  nor the count per reviewer can attribute a finding before the matching closes.
- `docs/instrumentation.md`.
- A deterministic security gate: `security_required = deterministic_rule OR
  reviewer_requests_security`. A rule in `security_review.always_when` of
  `.code-cycle.yml` matches changed paths, filenames and labels, and
  `scripts/security_gate.py` is its reference implementation. A repository that
  declares no rule keeps the defaults, so a missing configuration cannot be the
  case that silently disables the audit.

- `scripts/router.py`: router v1. Executor availability gates the choice before
  any model is considered; production may use a profile's fallback and a
  calibration may not; cost is estimated over the whole cycle because every real
  implementation measured so far needed a correction round. Two escalation rules
  only, both from signals declared before routing. It decides and does not
  dispatch: turning a resolved target into a running worker on an arbitrary host
  is integration work that does not exist yet.
- `code_cycle.profiles` in `.code-cycle.yml`: the single place a role resolves to
  an executor, provider, model and effort.
- `Adapter.completes_work` and `DispatchResult.asynchronous`: a backend that
  starts work and returns a handle to it says so on the class, and the dispatch
  wrapper applies that to every result, so a started Orca worker can never be
  read as a finished stage. `dispatch()` now rebuilds its result with
  `dataclasses.replace`, so a new field cannot be dropped on the way out.
- The repository's configuration reaches routing: `run_cycle.py` loads
  `.code-cycle.yml`, overlays `code_cycle.profiles` through `load_profiles`, and
  passes the resolved profiles to `CycleRecorder`, which routes with them.
  `code_cycle.repository.selector` supplies the repository when `--repo` is
  absent. Until this, every run used the built-in profiles whatever the
  repository declared. An unreadable configuration or an unknown profile name
  stops the run before any dispatch; `--no-config` asks for the defaults.
  Reading configuration needs PyYAML, the runtime's one optional dependency,
  imported only when there is a file to parse. The repository and work item are
  validated during planning through `telemetry.validate_reference`, the store's
  own rule exported rather than copied, so a reference the store would refuse
  never reaches an executor prompt; and `code_cycle`, `code_cycle.repository`
  and `code_cycle.profiles` must be mappings, so valid YAML with the wrong shape
  is refused rather than raising from whichever reader reached it first.
  `load_profiles` refuses a declared profile that is not a mapping and a
  `primary` or `fallback` that is not a target string, naming the profile: one
  used to raise `TypeError` past every handler, and the other was carried into a
  `Profile` as whatever it was.
- Adapters hand over `agent_output`: the reply lifted out of their own CLI's
  envelope — Claude's `result` field, Codex's `agent_message` items — so the
  driver never learns either format. A canary against the real CLIs found the
  block located and then unparseable, because reading the envelope as the reply
  keeps `\n` and `\"` as escapes. Fixtures are live captures, and the fake
  agents now write their CLI's envelope rather than plain text. That text is
  kept whole: the bounded tail in `artifacts` is for a person reading afterwards,
  and bounding what the caller parses deletes the result of any reply that keeps
  talking past its own block.
- A stage's structured result distinguishes readable, present-but-unreadable and
  absent, instead of collapsing all three into `None`, and a status outside the
  store's vocabulary is treated as no status rather than raising on the way in.
- A stopped cycle prints the reason the agent gave in its own structured block,
  from `error`, `summary`, `blocking_reason` or `reason`. It is displayed only:
  never stored, since telemetry holds references and counts, and never acted on,
  since it is the agent's claim rather than a verified fact — a canary claimed
  GitHub was unreachable while `gh` worked from the same sandbox minutes later.
  Control characters are stripped and the text is cut to 500 characters before
  it is printed, and an executor's own `detail` goes through the same cleaning:
  both are untrusted text on its way to a terminal.
- A dispatch that returned is no longer read as a stage that worked: a role whose
  own report is not a completion stops the cycle, with the dispatch row still
  recording `succeeded` and the reported status recorded beside it. The canary
  had Codex exit 0 having changed nothing and the cycle review it.
- A stage is dispatched with the permission its role needs: `implement` and
  `resolve` may write, everything else reads. `codex exec` gets `-s
  workspace-write` or `-s read-only`, Claude gets `--permission-mode acceptEdits`
  when writing. Until this, `codex exec` defaulted to read-only and nothing asked
  otherwise, so a dispatched implementer could never implement. No argument list
  built here asks for `danger-full-access` or a bypass flag. Review profiles use
  Codex, which enforces its read-only sandbox; any configured adapter that cannot
  enforce non-mutation fails closed before it is started.
- `scripts/run_cycle.py`, the production wiring: probe once, label the work,
  then `implement → review → (resolve → rereview)*` with every stage through
  `CycleRecorder.stage()`. It decides nothing — no cost model, no learning — and
  reads each review's verdict from the structured result it asks the executor to
  emit rather than inferring one from an exit code, stopping when that block is
  absent. Until it existed the recorder had no caller outside its tests, so the
  guarantee that a dispatch cannot skip its row applied to nothing.
- `CycleRecorder` takes the probe map and passes it to every dispatch. Without
  it each dispatch probed again on its own, so an availability could change
  between two stages with nothing recording that it had.
- `tests/test_installed_cycle.py` runs the entrypoint from an installed layout
  with fake agent binaries on `PATH` and reads the rows back out of SQLite,
  including the abandoned attempt and the fallback decision after an exhausted
  window.
- Both installers carry the runtime, not the skills alone: `.code-cycle/runtime`
  under the home directory or the project root, one copy per scope rather than
  one per host. `scripts/runtime.manifest` is the single list both read, so they
  cannot drift; the installer writes `.code-cycle/.gitignore` containing `*` when
  none exists, so installed code cannot be committed by accident, and leaves an
  existing one alone; a symlink on any path it writes to is refused rather than
  followed, in both installers; and `--no-runtime` / `-NoRuntime` installs the
  skills alone. Until this existed,
  `cc-orchestrator` told its reader to drive every stage through `CycleRecorder`
  while no installation had one.
- `tests/installed_stage_check.py` runs a full stage against an installed
  runtime with the repository kept off `sys.path`, and asserts the rows it left.
  Every other test imports from `scripts/` — the checkout an installation does
  not have — so none of them could see a runtime that was never installed. The
  suite also refuses a module in `scripts/` that is neither shipped nor declared
  as tooling, and a shipped module whose own imports are not shipped.
- `scripts/cycle.py`: `CycleRecorder` routes, dispatches and records a stage as
  one operation, so no run that goes through it can dispatch without leaving a
  row. It owns the single production reroute — an exhausted window only — and
  records both decisions, including the abandoned attempt: a fallback whose
  first attempt left no trace makes fallbacks look free. Friction a person must
  clear stops where it is instead, unrerouted and with availability untouched,
  so nothing spends another provider's window on a waiting login screen.
  `router` and `executors` stay unaware of the store, asserted by test.
- `router` routes `resolve` and `rereview`, which the cycle uses and which the
  role table had never covered.
- `scripts/telemetry.py`: one row per stage in a SQLite database outside every
  repository. `first_pass_rate()` measures per repository what the router
  currently assumes, reporting unknown below ten observations rather than
  hardening a handful of runs into a routing constant; `dispatch_failures()`
  counts blocked dispatches by capability; `model_drift()` lists executors that
  ran something other than what was requested. The no-prose, no-credentials
  boundary is enforced by one typed table covering every field, columns
  included: counts are integers, amounts numbers, flags booleans, tokens a
  closed vocabulary, identifiers bounded references matching a selector grammar,
  and containers are refused outright. Model names are checked against the
  models the toolkit knows — built under either import shape, and raising rather
  than passing values through when it cannot be built — and published credential
  prefixes are rejected in any reference; no grammar can separate a model name from a credential, so the
  closed set is the guarantee and the residual is documented rather than
  claimed away.
- `scripts/executors.py`: generic executor dispatch. `probe()` reports what each
  executor could be shown to be and on what evidence, `dispatch()` runs a
  resolved target non-interactively through Codex, Claude or Orca. Only Orca can
  prove readiness; the native adapters stop at `authenticated`, because
  remaining quota is not observable without spending it. Dispatching from that
  state requires the `attempt` readiness policy and is recorded as such. A
  calibration dispatch refuses it. Interactive friction and exhausted windows
  return `BLOCKED` naming the missing capability rather than being retried or
  answered blind. Friction is classified from a failed exit, never from a
  successful run's own output. A dispatch that reports a different model than
  the one requested is a `contract_violation` rather than a success.
- Claude's resolved model is read from the single key of `modelUsage`, which is
  where it actually appears; there is no `model` field, so it had been lost on
  every Claude dispatch and the contract check never fired. Codex reports no
  model at all, which stays `None`.
- Codex refusing to run outside a trusted git directory is `BLOCKED` with
  `trusted_directory` rather than a plain failure, so the missing capability is
  named instead of guessed at.
- The Orca adapter checks the process exit status alongside the JSON body, in
  both dispatch and probe. The CLI exits `0` only for `ready`, so a failed
  launch that still returns a valid-looking receipt is a failure, and its stage
  and residual resources are kept for recovery.
- `OrcaDispatchContext`: the coordinator terminal, Run and Task an Orca
  dispatch consumes and never creates, with the Task ID kept separate from the
  prompt every other adapter takes. The Orca agent follows the target's
  provider.
- Orca review dispatches now require an explicit `OrcaReviewWorkspace` whose
  immutable or disposable path does not overlap the implementer's workspace;
  missing, mismatched and contradictory workspace contracts fail closed.
- `DispatchResult.learned_availability`: the evidence a failed dispatch produced
  about its executor, so the orchestrator can re-route once when a window turns
  out to be exhausted — the only moment that is knowable for a native executor.
- `ReadinessPolicy.for_mode()`: production attempts, a calibration demands
  proof, neither depending on a caller remembering to pass a policy.
- `cc-orchestrator` routes and dispatches per stage: probe once, label the work,
  route each role, dispatch the resolved target, validate the result. An
  explicit execution mode still fixes the executors and replaces the routing
  step, and a run says which of the two paths it took.

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
- Sensitivity triage in `cc-initial-review`, `cc-rereview` and
  `cc-resolve-comments` evaluates that rule first and adds the model's judgement
  on top. A review may add a security audit and may never remove one the rule
  activated: the coordinator was the cheapest component in the cycle deciding
  whether its most expensive check ran, on a semantic judgement nothing verified.
  Skipping now requires the rule and the reviewer to fail at the same time.

### Fixed

- A pair is now identified by change request and commit, not change request
  alone. A second campaign on the same change request at a new commit used to
  collide with the first, so a crashed dispatch left no row at all behind a row
  still marked usable.
- `model_resolved` comes from the executor's dispatch receipt. Workers were
  asked to report their own model and got it wrong in both arms of the first
  campaign, which is the drift the field exists to detect.
- `record_pair()` accepted a pair as usable when only one reviewer had been
  observed, or when the single observation named a run that was never
  dispatched. It now requires the observed set to match the expected review runs
  exactly, and records which run is missing or unexpected. Found by the first
  real paired campaign: both calibration reviewers reported the one-sided case
  independently, and the unattributable-observation case surfaced while verifying
  their reports.

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
