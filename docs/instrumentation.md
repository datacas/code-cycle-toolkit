# Instrumentation

> [!NOTE]
> This is the design reference: why each rule exists and how it is enforced.
> For how to use these features, start with [Review lifecycle](review-cycle.md),
> [Routing and models](routing.md), and [Telemetry](telemetry.md).

What a Code Cycle run records about itself, and why. This documents what is
implemented today. It is deliberately narrow: it makes runs measurable, and it
chooses nothing.

## The comment is the record

`ORCHESTRATION_RESULT` is off by default in every cycle skill, so no analysis can
assume a JSON file exists. The published change-request comment is therefore the
durable machine-readable record, and everything below survives in it whether or
not the block is ever requested.

That has a second benefit: because the record is the comment, a change request
closed months ago can still be read back.

`scripts/review_contract.py` is the reference parser for that record — the
executable definition of what the skills promise in prose. `scripts/validate-package.py`
checks that the two do not drift apart.

## Three kinds of line

```text
#### [CCR-20260918-001] · senior_reviewer · anthropic/sonnet-5→sonnet-5 · high · schema:1
#### [CCT-20260918-001] · cheap_coder · openai/luna-high→luna-high · high · triaged:0123…4567 · schema:1
#### [REV-004] · medium · resolved · valid · blocks:yes — Short title
```

**Review run line.** Opened by the skill that starts a review. A change request
accumulates one initial review and several rereviews, so each gets its own
`CCR-xxx` and the findings below it belong to that run. A skill that only
republishes the comment preserves existing run lines and mints none.

**Triage run line.** Written by the resolver, once, before it edits anything. Its
`triaged:` token is the commit every disposition in that run was judged against.

**Finding header.** One per published finding, new or previous.

All of it stays language-neutral even when the surrounding prose is not.

## `status` and `disposition`

| | Question | Values |
|---|---|---|
| `status` | What happened to the code? | `open`, `resolved`, `not_applicable` |
| `disposition` | What did the resolver make of the finding? | `valid`, `debatable`, `incorrect`, `obsolete`, `needs_clarification`, `-` |

They are orthogonal, and every combination is meaningful: `resolved` + `valid`
is an accepted finding that was fixed; `not_applicable` + `incorrect` is one the
resolver rejected; `open` + `debatable` is one still under discussion.

`-` means not triaged yet. A reviewer always publishes `-`, because only a
resolver assigns a disposition.

## Trusted finding recovery

`scripts/review_contract.py` recovers findings from provider comments only when
each body is paired with provider-supplied author metadata and the configured
`code_cycle.review.trusted_authors` allow-list. Provider logins compare
case-insensitively; an absent or empty allow-list, missing author metadata, or a
history with contract headings only from untrusted authors blocks recovery.
Ordinary discussion starts an empty record. It reads the complete chronological
history, ignores and records other authors, and skips and records malformed
trusted comments so one bad heading cannot wedge the record. Status and approval
blocking may move; the first severity, title, and disposition remain the
historical record. A later re-score is audit data, but two non-empty titles for
one ID are a collision that blocks recovery, even across comments.

## Dispositions are frozen

The first resolver that triages a finding assigns its disposition. From then on
it is preserved verbatim: never reset to `-`, never recomputed against a later
commit, never replaced because the code has since changed.

The reason is measurement, not implementation. The question a disposition answers
is whether the finding was a real problem *when it was written*. A second opinion
formed against already-corrected code mostly reflects the fix. `status` keeps
moving; `disposition` does not. A later pass that disagrees says so in prose, and
that disagreement is data for a human audit rather than grounds for overwriting
the record.

## Classify everything, then edit

The resolver classifies every finding against one commit and records the frozen
dispositions before changing a line of code.

Interleaving would corrupt the data: fixing finding A turns an equivalent finding
B into `obsolete` for no reason except list order, so the disposition would
measure the sequence rather than the finding.

The triage run line is what makes this checkable instead of merely asserted —
`review_contract.verify_triage_freeze` reports a record whose triage is anchored
to the post-fix commit, whose findings reached the edit step untriaged, or which
mixes two anchors.

## Requested and resolved models

`model_requested` is policy: the closed set accepted by telemetry is the union
of the toolkit defaults and the models in the already-resolved repository
profiles. The caller injects the latter into each `Telemetry` instance; the
store never reads `.code-cycle.yml` itself.

`provider/model_requested→model_resolved` records both sides, including when
they match, and writes `?` for the resolved side when the host does not report
which model ran. The payload also carries the closed `model_resolution` token:
`matched`, `mismatch_known`, `mismatch_unrecognized`, or `unreported`.

`model_resolved` comes from the executor's dispatch receipt, never from the agent.
An agent asked which model it is answers from its own configuration — the very
thing under suspicion when an alias is repointed — and has been observed to
disagree with what was actually launched. When the executor reports nothing, the
value is `?`; the agent does not get to fill that gap.

A constant shape is easier to parse than an optional arrow, and it separates two
different facts: `sonnet-5→sonnet-5` states that nothing drifted, while
`sonnet-5→?` states that we cannot know. Aliases are updated behind the scenes,
so if rates move months from now without anything having changed, this is what
distinguishes a change in the work from a change in the instrument.

An executor-reported model that is not in the permitted set is never written to
the store. The row is retained with `model_resolved: null` and
`model_resolution: mismatch_unrecognized`, so an unexpected observation cannot
make the observation disappear.

## Legacy compatibility

A finding header with four tokens and no disposition predates this contract. It
stays valid and reads as `-`. An open change request is never blocked for having
started before the migration, and republishing such a finding in the five-token
form with `-` loses nothing.

## The optional JSON mirror

When `ORCHESTRATION_RESULT` is enabled it mirrors the same facts:

```json
{
  "review_run_id": "CCR-20260918-001",
  "reviewer": {
    "profile": "senior_reviewer",
    "provider": "anthropic",
    "model_requested": "sonnet-5",
    "model_resolved": "sonnet-5",
    "effort": "high"
  },
  "triage_run_id": "CCT-20260918-001",
  "triaged_sha": "0123456789abcdef0123456789abcdef01234567",
  "finding_outcomes": [
    { "id": "REV-001", "disposition": "valid", "status": "resolved" },
    { "id": "REV-002", "disposition": "incorrect", "status": "not_applicable" }
  ]
}
```

`resolved_findings` and `unresolved_findings` keep their existing shape, so
consumers written against them are unaffected. `finding_outcomes` adds the
dimension they cannot carry. Use `"model_resolved": null` for the `?` case.

The mirror is derived from the comment, never the reverse.

## Experimental: paired review

Off by default, and not part of the permanent contract.

`.code-cycle.yml` can name two experimental reviewer profiles:

```yaml
code_cycle:
  calibration:
    profiles:
      reviewer_a:
        provider: anthropic
        model: sonnet-5
        effort: high
      reviewer_b:
        provider: openai
        model: terra
        effort: high
```

They are candidates for a comparison that has not happened. Neither is the
default reviewer and nothing presumes which is better. This file is the only
place an alias maps to a provider and model, so a candidate can be swapped
without editing a review skill.

`scripts/run_cycle.py` recognises the block and does not read it: its
`--mode calibration` takes its arms from the ordinary profiles, not from these
aliases. Declaring the block therefore never turns an experiment on. There is
deliberately no `calibration.enabled` flag, because it would restate a guarantee
the entry point already gives — a calibration is entered only by the explicit
`--mode calibration` argument or an explicit `paired_review=true` invocation,
never by configuration alone.

`cc-orca-orchestrator` with `paired_review=true` dispatches both over the same
commit, keeps them blind to each other, and merges their findings before triage.
While a campaign runs, the map from opaque `CAL-xxx` candidates back to review
runs lives in `scripts/calibration_store.py`'s store, outside every repository,
written atomically so an interrupted campaign resumes. Once a paired change
request is finished — blind triage, frozen dispositions, human matching of root
causes — the attribution is published into the comment and the store's job is
done.

Isolation is a precondition, not a preference. A pair is dispatched only as
`isolated` — a worktree each at the same head SHA — or `sequential`, the second
reviewer starting after the first has finished and its head SHA has been
confirmed unchanged. Two reviewers concurrently in one worktree is a state the
orchestrator refuses to enter, because a contaminated pair is worse than a
missing one: it enters the sample looking like evidence.

Non-mutation is verified rather than assumed. Nothing can force a worker to leave
the tree untouched, so the head SHA is recorded before each reviewer and
confirmed afterwards; a pair whose head moved is not comparable.

A pair is identified by change request *and* commit. Keying by change request
alone would let a second campaign at a new commit collide with the first: an
existing row is never overwritten, so a later dispatch that crashed would leave
no row at all, hidden behind a row still marked usable from the previous commit.

Findings reach the person matching root causes as one shuffled list of opaque
ids minted by the orchestrator — `F-7K2P` — with no labels, no groups and no
totals per reviewer. Labelled lists leak even when the labels are meaningless,
because the count per label attributes them. The map back to each run stays in
the campaign store until the matching is closed.

A pair counts only when the set of observed reviewer runs matches the expected
set exactly. Checking that *some* observation exists is not enough: a one-sided
review, or an observation from a run nobody dispatched, would otherwise sit in
the sample next to complete pairs and be indistinguishable from them.

A clean pair is not automatically a pair every metric can use. `usable_pairs()`
answers one question — was this collected cleanly — and `capabilities()` answers
a different one: which metrics this pair can actually support, with the reason
when it cannot. A pair whose findings no resolver triaged supports coverage and
overlap but not acceptance, and `pairs_supporting("acceptance")` leaves it out
rather than letting the sample claim information it never produced.

`target_relation` records whose code was under review. A toolkit reviewing itself
cannot separate "this reviewer is better" from "this reviewer wrote that code".
The relation is recorded, not corrected for.

The resolver is part of the instrument too: `record_triage()` stores its profile,
provider, requested and resolved model, effort, and the commit it judged, beside
the reviewers' own runs. It decides whether each reviewer was right, so keeping
it fixed across campaigns is what makes the reviewers comparable at all.

Both facts are stored with the attribution. `usable_pairs()` is the single
definition of a valid sample, and the only thing a later analysis reads to build
one: `pairs()` exists for diagnosis, not as a gate. Keeping one definition in one
place is what stops a future consumer deciding for itself what
`shared_concurrent` or a moved head ought to mean. `excluded_pairs()` reports
every exclusion with its reason, because a campaign that discards half its pairs
is saying something about the dispatch rather than about the reviewers.

A pair is registered when it is dispatched and starts excluded, then earns its
way in. Recording only on success would leave a crashed run invisible rather
than excluded, and a missing row reads as "never attempted" — a different claim
from "attempted and unusable". Storing the verdict matters as much as enforcing
it: an analysis that cannot tell a contaminated pair from a clean one cannot drop
it either.

Matching two findings to the same root cause is a human judgement. It is not
automated here, deliberately: automating it would place an unverified gate inside
the measuring instrument.

## Routing v1

`scripts/router.py` resolves a role to a concrete target. It is deliberately
conservative: two calibration campaigns produced one usable finding about models
— an inexpensive implementer never passed review on the first attempt on five
real work items, at either effort level — and nothing separated the two efforts
beyond that. The router does not pretend to a precision that evidence does not
support.

**Availability gates the choice.** An executor that is merely installed is not
dispatchable: a binary on PATH proves no session, no repository access and no
quota. The router is *told* each executor's state rather than discovering it,
because nothing documented reports remaining quota and inferring it from a failed
dispatch mistakes one exhausted window for evidence about a model.

**Production may fall back, a calibration may not.** Substituting an arm when
quota runs out answers a different question with the same sample, so
`RoutingMode.CALIBRATION` blocks and waits.

**Cost is estimated over the cycle, not the first pass.** Five of five real
implementations needed changes, so the estimate carries at least one resolution
plus its review until a repository measures its own first-pass rate. A profile
that is cheaper to run once is not automatically cheaper to finish with.

Only two rules escalate, both from signals declared before routing: difficulty 3
implementation work goes to `deep_coder`, and a security-sensitive change is
reviewed by `senior_reviewer`. Labelling after the fact would mean measuring the
routing rather than the models.

**Profile selection is a seam, and only a seam.** Those rules live in
`rule_selector`, the default `ProfileSelector`: a callable that receives the
role, the role's allowed profiles (`ROLE_CANDIDATES`) and the `TaskSignals`, and
returns a profile name with its reasons. `route()` refuses any name outside the
candidates, and keeps everything else — workspace policy, the availability
gate, fallback, the calibration block and target resolution — so a selector can
never return a model or skip a gate. Nothing in the cycle passes another
selector yet; the optional Jev shadow (see [Shadow suggestions](#shadow-suggestions))
observes the rules' choice and is deliberately not a selector.

Profiles resolve through `code_cycle.profiles` in `.code-cycle.yml`, the only
place a role maps to a model.

`code_cycle.routing.strategy` names the selection policy: `fixed` or `measured`.
It defaults to `fixed`. Both values currently select the declared primary, then
the declared fallback when availability and workspace policy allow it; the
strategy does not change target selection. Under `measured`, `CycleRecorder`
reads the repository's first-pass rate once when the run starts and passes the
number to `estimate_cost`. A known rate reduces the expected resolution cost;
an unknown rate uses `DEFAULT_FIRST_PASS_RATE` and records `Rate.explain()` on
the decision. Under `fixed`, the recorder does not query that rate.

Stage rows record the strategy, cost breakdown, rate source, known value when
available, and observation threshold. The router receives those values from its
caller and never opens the telemetry database.

## Executor dispatch

`scripts/executors.py` turns a resolved target into a real execution. It adds no
routing rules: the decision arrives already made, and a blocked decision is
never quietly re-routed here, because substituting a target at dispatch time
would make the recorded decision a lie.

The design is built around an asymmetry that is worth stating plainly:

| Executor | Can prove | Why not more |
|---|---|---|
| Orca | `ready` | the runtime reports its own state |
| Codex | `authenticated` | a credential is configured; whether it still works is unknown |
| Claude | `authenticated` | onboarding completed; whether the session is live is unknown |

`authenticated` here means credential material exists, not that the session
behind it is valid: a token can be expired or revoked with no local sign of it.
Nothing documented reports remaining quota or session validity without consuming
something. So a probe reports what it demonstrated and how, and a caller that
still wants to dispatch from `authenticated` asks for the `attempt` readiness
policy. The promotion is then recorded on the result as `dispatched_from`, where
nobody can later mistake a policy for a finding. A calibration dispatch refuses
the promotion outright.

Interactive friction is a missing capability, not a failure to retry. A
folder-trust dialog, a hook-review screen, a bypass acknowledgement or a login
prompt returns `BLOCKED` naming the capability. The adapter never answers a
security prompt blind — that was tried during a campaign, and it produced two
arms running in different environments.

An exhausted window is also `BLOCKED` rather than `FAILED`: retrying costs
nothing and changes nothing, and the distinction keeps one exhausted window from
being read as evidence about a model.

Friction is classified from the failure, never from what the agent wrote. A run
that exits cleanly is never inspected at all, and on a failed run only stderr
and structured error events are read — an implementation that adds rate-limit
handling says "quota" for ordinary reasons, and a non-zero exit does not turn
that sentence into evidence about a window.

A production run uses the `attempt` readiness policy and a calibration uses
`proven`, derived from the mode rather than passed by hand: under `proven` no
native executor could ever be dispatched to, and under `attempt` a calibration
arm would start from a state nobody established.

A dispatch that reports running a model other than the one requested is a
`contract_violation`, not a success. `dispatch(target)` promises that target
ran; a different model answering is that promise broken, and the cycle must not
advance on it. An executor that reports no model at all leaves the question
open, which is a third answer rather than a quiet yes.

Which backend can confirm the model it ran differs, and this was established by
running each one rather than by reading documentation:

| Backend | Reports the model | Where |
|---|---|---|
| Orca | yes | the dispatch receipt's `launch.effective` |
| Claude | yes | as the single key of `modelUsage` — there is no `model` field |
| Codex | no | `exec --json` emits `thread_id`, `type`, `item` and `usage`, and nothing naming a model |

So a Codex dispatch reports `model_resolved: None`, and that is the honest
answer rather than a gap to paper over: the contract check cannot fire for an
executor that never says what it ran. Anything self-reported by the agent in its
own prose is not used — that was observed to be wrong in both arms of a
campaign.

Its exit status is part of the receipt: the CLI exits `0` only for `ready`, and
a failed or uncertain launch exits non-zero while still returning a JSON body
carrying the stage, residual resources and recovery commands. Reading the body
and ignoring the status reports a partial launch as a success, and the same
applies to the status call — readiness is the one thing this backend can prove,
so a command that failed does not get to prove it.

Its dispatch takes an explicit `OrcaDispatchContext`: a coordinator terminal, a
Run and a Task that already exist, plus an optional agent override. A non-writing
review also requires an `OrcaReviewWorkspace` with a path that does not overlap
the implementer's workspace and an explicit `immutable` or `disposable`
isolation mode. The adapter refuses to create any of these, because a
dispatcher that quietly spawns terminals, durable state or review worktrees in
someone's workspace is one nobody can reason about. The Task ID is separate
from the `task` argument every other adapter takes: for Codex and Claude that
argument is the prompt, while Orca's prompt already lives inside the Task and
`--task` wants its identifier. The agent is chosen from the target's provider,
so an Anthropic target launches the Claude agent rather than asking Codex to run
a model it does not have.

### Learning availability by dispatching

A native probe cannot see quota before spending some, so the only moment anyone
learns a window is exhausted is a dispatch that tried. If that stays inside the
result, the production fallback is unreachable in the case it exists for: the
router picked the primary from an optimistic promotion, the dispatch found the
truth, and nothing carried it back.

`DispatchResult.learned_availability` reports that evidence. It does not
re-route — that belongs to the orchestration layer, where a single explicit
second routing is recorded along with the evidence that caused it. Friction a
person has to clear teaches nothing about availability and yields `None`, and a
calibration never re-routes at all.

Claude runs with `-p` and Codex with `exec`, non-interactively and with no
bypass flag. A run that needs elevated permissions to proceed is a run a person
should be looking at.

## Telemetry

`scripts/telemetry.py` records one row per stage in a SQLite database outside
every repository, holding references, statuses and counts — no diffs and no
prose. It never enters Git.

That boundary is enforced rather than asserted, by one typed table covering
**every** field — columns included. `FIELD_SPECS` is the only gate, and there
are no exempt fields.

The single table is the lesson, not a style choice. This boundary was breached
three times, and each fix covered the door that had just been pointed out: first
a docstring with nothing behind it, then payload key names, then payload value
types — while promoted columns still accepted anything, so `status="TOP-SECRET"`
was stored. A column is not safer than a payload key; it is another door.

A count is an integer, an amount a number, a flag a boolean, a token a value
from a closed vocabulary, and an identifier a bounded reference matching a
selector grammar. Containers are refused outright, whatever key they arrive
under.

### What the identifier rule does and does not guarantee

Worth stating exactly, because this boundary was claimed too strongly four
times. `gpt-5.6-luna` and `sk-live-abc123` have the same shape — lowercase
letters, digits, hyphens — so **no grammar separates a model name from a
credential**.

What holds:

- model names are checked against the models this toolkit knows, a closed set,
  which is a real guarantee rather than a shape test. That set is built from the
  router's profiles under either import shape, and when it cannot be built the
  check raises instead of passing the value through: a check that switches
  itself off when it cannot run is not a check;
- repository and work-item references must match the selector grammar and must
  not begin with a published credential prefix — `sk-`, `ghp_`, `AKIA`, `xox`
  and the rest — which rejects the paste that actually happens by accident.

What does not hold: nothing proves an arbitrary caller-supplied reference is not
a secret. That residual is mitigated by where these values come from — a
repository selector out of `.code-cycle.yml` and a work-item id out of the issue
provider, both derived by the toolkit rather than typed into a field — and not
by pretending the validator settles it. Refusing is loud rather than silent: dropping
a field would lose an observation the caller believed it had recorded. Routing
reasons are reduced to a count — the reasons themselves belong in the published
comment.

It exists to replace one guess in particular. `router.DEFAULT_FIRST_PASS_RATE`
is `0.0` because five real implementations out of five needed a correction
round, and every routing cost estimate has carried that number since. Five is
not a measurement; it is the smallest sample that produced a unanimous answer.

`first_pass_rate(repo_id, profile)` measures it per repository, counting each
task's first review **that reached a verdict**, ignoring implementations nobody
reviewed, and filtering by the implementer whose work was judged rather than by
the reviewer. A review row recorded before its verdict arrived — ordinary with
an append-only API — leaves the task uncounted rather than counting as a
failure, which would have poisoned the cost model in the one direction that
matters.
Below ten observations it reports the value as unknown and still returns the
count, so "we do not know yet" and "it is genuinely zero" never look the same.

Two other questions are already answered from the same rows. `dispatch_failures`
counts blocked dispatches by the capability that blocked them, keeping an
exhausted window distinct from a trust dialog — conflating those cost a
campaign. `model_drift` reads `model_resolution` and includes both known and
unrecognised mismatches, while excluding `unreported` rows rather than counting
silence as agreement, because Codex reports none at all. Rows written before
the token existed fall back to the two model columns, so historical drift is
not erased by the new representation.

The schema will change. A schema designed before its questions are known is one
that gets migrated, and that was accepted deliberately: recording now, with
adapters just proven correct against the live CLIs, beats recording later from a
larger unexamined pile. Only fields already queried have columns, a short allowlist of counts and flags
travels in `payload`, and `schema_version` is on every row from the first.

### Pre-routing signals

Schema version 2 adds the signals a stage's router could have known before it
chose a model. They exist so a later selector can be judged on the information
it would actually have had, which is only possible if nothing learned after the
routing leaks into them. All of them live in `payload`, so a version-1 row is
read exactly as it was written; `schema_version` says which signals its writer
could have supplied.

| Kind | Fields | Source |
|---|---|---|
| declared | `difficulty`, `verifiability`, `security_sensitive` | the operator's labels, `router.TaskSignals` |
| observed | `changed_files_count`, `has_tests`, `touches_dependencies`, `touches_database`, `touches_auth`, `touches_api`, `touches_migrations`, `touches_ci`, `changed_<language>_files` | the diff against the default branch, `scripts/stage_signals.py` |
| observed | `prior_findings_total`, `prior_findings_blocking`, `prior_findings_<severity>` | the latest verdict recorded before the stage |
| observed | `previous_failed_attempts`, `resolution_round`, `verification_available` | the cycle's own state, and `--verification` when given |
| estimated | `changed_lines_estimate`, `test_count_estimate` | added plus deleted text lines; added lines that look like a test definition |

`telemetry.PRE_ROUTING_SIGNALS` holds the same classification, so a query can
tell a count read off a diff from a difficulty somebody typed.

The temporal rule is the point. An `implement` stage is routed before any diff
exists, so it carries no change signals at all rather than an empty change.
`prior_findings_*` are the counts of the latest verdict before the stage — the
review's for a resolution, never the one the stage itself goes on to report —
and a verdict that reported no findings leaves them unknown rather than
carrying an older count forward. Per-severity counts are recorded only when
every finding names its severity: a partial breakdown would read as zero for
the severities it missed. `previous_failed_attempts` counts the dispatches in
this cycle that did not succeed before this routing, including an attempt
abandoned for a reroute. `resolution_round` appears only on `resolve` and
`rereview`.

Unknown is not zero. A diff that could not be read — no worktree, no base that
resolves, Git missing — leaves every change signal out; a diff that was read and
was empty records zero files. `--verification` is tri-state: omitted, it is not
recorded.

Only closed categories are stored. Paths are classified in memory and dropped:
languages are a fixed list of scalar counters (`python`, `javascript`,
`typescript`, `go`, `rust`, `java`, `csharp`, `ruby`, `php`, `shell`, `sql`,
`markdown`, and `other`), never an array or a map, and each area is one flag.
Counts are bounded by `FIELD_LIMITS` and refused below zero. No path, diff line
or file name reaches the store. The classifiers are heuristics over paths:
`touches_auth` says a changed path looks like authentication code, matched by
whole word so `processor` is not `sso`, and neither it nor its absence says
anything about what the code does.

### Cycle outcomes

Schema version 3 records what a run went on to produce, on rows of its own. The
pre-routing signals above describe a decision; an outcome written beside them
would leak into any evaluation that reads them, so a `dispatch` row is written
once, before its stage's result is known, and nothing is ever added to it.

Three payload keys tie a run together, listed in `telemetry.CORRELATION_FIELDS`:

| Field | Meaning |
|---|---|
| `cycle_id` | minted once per `CycleRecorder`, so two runs of one work item stay apart; validated before anything is dispatched |
| `stage_seq` | the number of the `stage()` call; a rerouted attempt shares its stage's number, and a verdict carries the number of the latest dispatch of its role |
| `record_kind` | `dispatch`, `verdict` or `cycle` |

Each outcome has one source row, listed in `telemetry.OUTCOME_FIELDS`:

| Source | Fields | Written when |
|---|---|---|
| `verdict` | `status`, `findings_total`, `findings_blocking`, `findings_<severity>`, `tests_passed` | the stage's structured result is read |
| `cycle` | `first_review_status`, `first_pass_approved`, `resolution_needed`, `resolution_rounds` | a first `review` reported `APPROVED` or `CHANGES_REQUESTED` |
| `cycle` | `final_review_status`, `final_approved` | any review or rereview reported one of those |
| `cycle` | `tests_passed` | a verdict recorded `tests_passed`; the latest one wins |
| `cycle` | `fallback_stages`, `contract_violations` | always: every dispatch of the run went through the recorder |
| `cycle` | `status`, `iterations` | always |

Unknown is still not a default. A run that stopped before a review reached a
verdict carries no review outcome at all — not unapproved, not zero rounds —
and a review that reported `BLOCKED` settles nothing. A `tests` value that is
not a boolean `passed` is not a test result, and neither is one that says no
test ran: `tests.ran: false`, or a `BLOCKED` stage reporting `passed: false`
without `ran: true`, records no `tests_passed` at all. A stage that stopped
before touching code has nothing to report as failed.
`Telemetry.cycle_outcome(repo_id, cycle_id)` reassembles a run from these rows and reports `closed: false` for one
that never wrote its closing row, whose outcome is then unknown rather than
failed. `resolution_rounds` counts the `resolve` stages the run attempted.
`checks_passed` and `checks_failed` remain accepted telemetry fields, but no
skill's structured result reports check counts, so they are not a recorded
outcome; CI state stays in the published review comment.

Each dispatch row that reached an executor carries `duration_ms`, the executor
call's wall time from a monotonic clock. An attempt refused before an executor
ran has none, and neither does a dispatch that only started work finishing
elsewhere: its launch time is not the stage's duration.

The routing rules' choice is the `profile` on each `dispatch` row. A later
selector's suggestion can be compared with it, and with the outcome, by
`(cycle_id, stage_seq)`, without that selector being installed while the run is
recorded and without the rows it annotates being rewritten. The Jev shadow
below is the first such suggestion.

Rows from versions 1 and 2 are read as they were written. They carry no
`cycle_id`, so they group by `repo_id` and `task_id` only and belong to no
reassembled cycle.

### Shadow suggestions

Schema version 4 adds `shadow` rows: an optional second opinion about a
profile, recorded beside the rules' choice and never used in its place. The
only one today is [TypeSafe](https://docs.typesafe.ai/), enabled per repository:

```yaml
code_cycle:
  routing:
    jev:
      mode: shadow            # disabled (default) | shadow
      model: jev-latest      # alias (default) or concrete version, e.g. jev-1.13.0
      timeout_seconds: 3      # above 0, at most 10
```

`scripts/jev_shadow.py` is an observer, not a `ProfileSelector`. After an
`implement` or `resolve` stage has been routed, dispatched and recorded,
`CycleRecorder` describes its first routing to Jev and writes the answer on a
row of its own. Nothing the adapter returns reaches `route()`, target
resolution, fallback or dispatch, so a suggestion that disagrees changes no
profile. The first routing is the one asked about because a reroute is
availability, not selection.

**Disabled is absent.** With no block, or `mode: disabled`, no adapter is built,
no key is read and no connection is opened. An unknown key under
`code_cycle.routing` or `code_cycle.routing.jev`, an unknown mode, a model
other than the accepted identifiers and a timeout out of range are refused by
`run_cycle.py` before any stage runs.

The old `typesafe-ai/jev` model setting remains accepted and is normalized to
`jev-latest`. A concrete version such as `jev-1.13.0` may also be configured.
When `jev-latest` reports a concrete version, telemetry records that version
and treats it as a match; new versions matching TypeSafe's `jev-x.y.z` form do
not require a toolkit update.

**What is sent.** One `POST https://api.typesafe.ai/v1/systemone` per
eligible stage, with the model, a fixed `choice` question between `cheap_coder`
and `deep_coder` whose wording is the adapter's own, and a `state` built only
from the fields in `telemetry.PRE_ROUTING_SIGNALS` plus the role. Each value
must be a count, a flag or a closed token (`verifiability`); anything else is
dropped before sending. No prose, path, diff, repository name, work-item id,
cycle id or outcome is sent. The endpoint is not configurable. The key is read
from `TYPESAFE_API_KEY` at the moment of the call, falling back to
`JEV_API_KEY` for compatibility, and placed only in the `Authorization`
header. A `code-cycle-toolkit` User-Agent accompanies the request. The key is
never written to `.code-cycle.yml`, a row, or a log. Redirects are not followed,
so the bearer key cannot be forwarded to another host.

**Failure is a category.** Every outcome is one `jev_status` token:
`suggested`, `unavailable` (no key, connection failure or HTTP 5xx), `timeout`,
`rate_limited` (HTTP 429), `http_error` (another non-200 status) or
`invalid_response` (anything that is not a TypeSafe response whose
`answers.profile` is a typed `choice` of one of the two options, with a
confidence and probabilities between 0 and 1 over those options only). No
response body or exception text is kept. None of them stops or changes the
stage, and the call itself is bounded by `timeout_seconds`.

A `shadow` row carries the correlation keys, `routing_strategy`, `local_only`
and only the fields in `telemetry.SHADOW_FIELDS`:

| Field | Meaning |
|---|---|
| `jev_status` | the category above |
| `jev_rule_profile` | the profile the rules chose for that routing |
| `jev_suggested_profile`, `jev_agreement` | Jev's choice and whether it matched, only when `suggested` |
| `jev_confidence`, `jev_probability_cheap_coder`, `jev_probability_deep_coder` | only when the service reported them |
| `jev_model_requested` | the configured model |
| `jev_model_resolved`, `jev_model_resolution` | the model the service reported, by name only when it is a known identifier; `unreported` when it named none |
| `jev_duration_ms` | how long the call took |

It holds no pre-routing signal and no outcome. The features are on the
`dispatch` row with the same `(cycle_id, stage_seq)`, the outcomes on the
`verdict` and `cycle` rows, and `Telemetry.cycle_outcome()` lists the
suggestions under `shadows`, so rules and Jev can be compared against the
outcome without either being mixed into the features. A shadow row is written
after its stage's own rows and is never read as the implementation by
`first_pass_rate`. Confidence and probabilities are exploratory evidence, not
permission: nothing in the toolkit acts on them.

## What an installation gets

Skills are instructions and can be duplicated harmlessly; the runtime is the
code that routes a stage, dispatches it and writes the row. Both installers
carry it to `.code-cycle/runtime` — under the home directory for a global
installation, under the project root for a project one — one copy per scope
rather than one per host, because three copies would be three answers to the
question of which one a run used. It needs Python 3 and that directory on
`PYTHONPATH`.

The list lives in `scripts/runtime.manifest` and both installers read that one
file, so neither can drift from the other. The installer writes into a directory
somebody else chose, which may be a repository it did not create, so a symlink
on any path it writes to is refused rather than followed, and an existing
`.gitignore` is left exactly as it is — installing is not authority to truncate
a file nobody named. `calibration_store.py` and
`validate-package.py` stay in the checkout: campaign tooling and package tooling,
which no installed skill points anybody at.

This is the correction of a real gap rather than a convenience. From the moment
the router landed until this change, `cc-orchestrator` instructed its reader to
drive every stage through code that `install.sh` never copied — it carried
`skills/` alone, so no installation had a `CycleRecorder` to call — the exact shape of failure this instrument exists to prevent, in the
instrument itself. No test could see it, because every test imports from
`scripts/`, the checkout an installation does not have.

`tests/installed_stage_check.py` is the answer to that. It takes an installed
runtime directory, keeps the repository off `sys.path`, drives a full stage
through a scripted executor and asserts the rows. Both CI jobs run it against a
real installation, and a test asserts the check can still fail by deleting a
module from the installed tree and requiring a non-zero exit. A runtime that
ships a module whose import is not shipped now fails at installation rather than
on the first real call.

## What a stage may touch

An implementation edits the tree it was given; a review reads it. The driver
passes that from the role, and the adapters turn it into their own CLI's terms:
`codex exec -s workspace-write` against `-s read-only`, and Claude's
`--permission-mode acceptEdits` for a writing stage. `danger-full-access` and
the bypass flags are not in any argument list this builds, asserted over the
commands rather than over the source text.

The two CLIs default in opposite directions, which nobody had declared. `codex
exec` is read-only unless told otherwise — four canary runs had an implementer
that could not implement, reporting `BLOCKED` on "the read-only workspace" while
nothing had ever asked for anything else. A dispatched Claude, by contrast,
edited a file with no flag at all. So the flag on a writing Claude stage
declares what is already happening rather than granting something new.

For a **reading** Claude stage, the harness creates an independent detached clone with its own Git object store at the exact reviewed HEAD, outside the implementer's tree, and removes the clone's remotes. A dirty implementer checkout blocks preparation. It records the implementer's HEAD, a fingerprint of `git status --porcelain`, and advertised refs for configured remotes before dispatch, then checks them again afterwards. A detected change makes the stage a `contract_violation`. Writes within Claude's disposable clone produce a warning and are discarded with that clone. The stage prompt prohibits file edits, commits, and pushes and explains why. Claude runs with its full tooling and its ordinary `gh` and `git` access, so it can publish its own comment. With Claude, read-only is watched, not enforced. The harness detects a write it can observe, but it cannot prevent or undo a remote write: a push, a merge, or a change to the change request. A push that is later restored before the stage ends also goes unseen. The maintainer has accepted this risk. Anyone who needs a full guarantee should review with Codex or give the reviewer read-only credentials. Read-only Claude dispatches record `read_only_mode = detected`, and only when the clone was prepared and removed and no change was detected. A clone left on disk fails the stage without a `read_only_mode`.

Codex retains `-s read-only` and records `read_only_mode = enforced`, meaning its sandbox prevents workspace writes. Orca still requires its explicit review-workspace contract. Reviews may use any configured eligible executor; a profile may still choose no fallback. Publication access remains behavioural: the prompt states which operations the role may perform.


## Running a cycle

`run_cycle.py` is the wiring, and only the wiring: probe once, label the work,
then `implement → review → (resolve → rereview)*`, every stage through
`recorder.stage()`. There is no path in it that routes and dispatches by itself,
because that is the hole the recorder closes and a driver is the easiest place
to reopen it.

The `--local-only` mode is the safe rehearsal path: it requires an explicit
linked Git worktree via `--cwd`, runs only the implementation stage, records a
`HUMAN_INTERVENTION` stop because no change request exists to review, carries a
no-publish policy in the implementation prompt, and marks each telemetry row
with `local_only`. That policy is auditable but is not an OS-level network
sandbox; use isolated credentials and remote access controls when remote writes
must be impossible.

It reads a review's verdict from the structured result it explicitly asks the
executor for — a documented opt-in in every review skill — and never from an
exit code or from prose.

**What the agent said is not what the CLI printed.** Every one of these tools
prints a machine envelope and puts the reply inside it, JSON-encoded: Claude one
object whose `result` holds the text, Codex a stream of NDJSON events whose
`agent_message` items hold it. Each adapter unwraps its own format into
`agent_output`, and the driver reads that. Reading the envelope instead finds
the right words with the wrong escapes — a canary run located
`ORCHESTRATION_RESULT` in Claude's output and then failed to parse the block,
because the newlines and quotes were still `\n` and `\"` inside a JSON string.
No fake caught it: the fakes printed the block as plain text. They now write
their CLI's real envelope, and the fixtures are live captures.

**A dispatch that succeeded is not a stage that worked.** The same canary had
Codex exit 0 having changed nothing, because the work item did not exist — and
the cycle reviewed it. Those are two facts about different layers and the store
already keeps them in separate columns: `outcome` says the call returned,
`status` says what the agent reported doing. Both stay true; what changed is
that a stage whose own report is not a completion stops the cycle. The dispatch
row still says `succeeded`, because it did.

**What the agent said about it is shown, and believed by nobody.** A stage that
stops reports a reason in its own block, and that reason is printed beside the
status — never stored, because the store holds references and counts rather than
prose, and never acted on, because it is the agent's claim and not a verified
fact. A canary reported "GitHub authentication is invalid and the GitHub API is
unreachable" while `gh` worked from the same sandbox, the same directory, minutes
later. Showing it is what made that checkable; believing it would have sent
somebody to read a status page. Before it was shown, finding out what an agent
had already explained cost three more dispatches.

It is cleaned before it is shown. An escape is not whitespace, so collapsing
whitespace let `\x1b[31m…` through into a terminal report, where an agent could
recolour, erase or forge the lines around its own. Every C0 control and DEL is
removed, and the executor's own `detail` — stderr from a CLI — goes through the
same function, because it is exactly as untrusted and reaches the same screen.
The bytes are removed rather than the escape sequences recognised: without
`ESC` such a sequence is inert text, and that holds without keeping a grammar in
step with every terminal that might read the output.

Three answers are kept apart where one `None` used to be: a readable report, a
report present but unreadable, and no report at all. A cycle that cannot tell
them apart records "no verdict" for a run that was explicitly blocked. A status
outside what the store can hold is treated as no status rather than carried to
`record_verdict`, where it would raise and end the run without its closing row. When the block is absent the cycle stops and records
that the verdict is unknown. A guess would be indistinguishable from data
forever after, and this instrument exists to be believed later.

Orca is the exception it names out loud: its dispatch returns a started worker
and a `dispatchId`, not a finished stage, so a cycle routed to Orca stops after
the dispatch rather than treating absent output as failure.

That distinction is carried by `completes_work` on the adapter and `asynchronous`
on the result, not by noticing that there was nothing to read — a succeeded
dispatch with no output looks exactly like a finished one, which is how the
first version of this driver reviewed work that was still being written. The
declaration is on the class, and the dispatch wrapper applies it, so an adapter
cannot report finished work it only launched by forgetting a keyword.

The driver loads the configuration and the recorder only receives what was
resolved: `code_cycle.profiles` overlaid on the defaults by `load_profiles`, and
`code_cycle.repository.selector` when no repository was named. `CycleRecorder`
does not look for a file itself — a component that reads configuration on its
own can disagree with its caller about what the configuration says, and both
would be recording rows. An unknown profile name stops the run before anything
is dispatched, because learning that afterwards means paying for a cycle to find
a typo.

The repository and work item are checked the same way and at the same moment,
through `telemetry.validate_reference` — the store's own rule, exported rather
than copied, because two validators agree until one of them is edited. A
reference the store will refuse is one no stage should run under: that stage is
dispatched, paid for, and then its row cannot be written, so the run happens and
leaves no trace of having happened. Configuration shape is checked with it:
`code_cycle`, `code_cycle.repository`, `code_cycle.profiles` and
`code_cycle.routing` must be mappings,
so a valid YAML document with the wrong shape is a stated refusal rather than a
traceback from whichever reader reached it first. `load_profiles` checks the
shapes below those names — every declared profile is a mapping, every `primary`
and `fallback` a target string — because a declared `primary: 3` used to be
carried into a `Profile` as the integer 3, and only something trying to route
with it ever found out.

The probe map is taken once and handed to the recorder. Without it each dispatch
probes again on its own, so an availability could change between two stages with
nothing recording that it had — and the decisions on either side stop being
comparable, which is the one thing this is all for.

`tests/test_installed_cycle.py` is the acceptance criterion: install into a
temporary home, put fake agent binaries on `PATH`, start the entrypoint as a
person would, then open the database and read what the run left. It covers the
healthy cycle, a second round after `CHANGES_REQUESTED`, a review that reports
no verdict, and the case the fallback exists for — Codex reporting an exhausted
window, the recorder rerouting once, and all three facts surviving in SQLite:
the abandoned attempt, the decision to fall back, and what the fallback did.

## Not implemented

Named because they are easy to assume from the contract above: no model
selection from statistics, no scoring, no adaptive learning, no escaped-defect
tracking, and no automatic matching of findings. Telemetry records; nothing
reads it back automatically, and a measured first-pass rate reaches the cost
model only because someone passed it.

What exists now is profile resolution, availability probing and dispatch to
Codex, Claude or Orca. What does not is anything that learns: no rule changes
itself from an outcome, and the router's two escalation rules stay where they
were written until a person moves them. Analysis of what this records is done today by
reading published comments — `gh api` plus `scripts/review_contract.py` — not by
a persistence layer.

---

[← Role workspace policy](role-workspace-policy.md) · [↑ Documentation index](README.md)
