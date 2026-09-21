# Instrumentation

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

`provider/model_requested→model_resolved` always writes both sides, including
when they match, and writes `?` for the resolved side when the host does not
report which model ran.

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

Profiles resolve through `code_cycle.profiles` in `.code-cycle.yml`, the only
place a role maps to a model.

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

Orca is the only backend that reports the model it actually launched, in its
receipt's `launch.effective`. Elsewhere the value is either self-reported by the
agent — observed to be wrong in both arms of a campaign — or unavailable.

Its dispatch takes an explicit `OrcaDispatchContext`: a coordinator terminal, a
Run and a Task that already exist, plus an optional agent override. The adapter
refuses to create any of them, because a dispatcher that quietly spawns
terminals and durable state in someone's workspace is one nobody can reason
about. The Task ID is separate from the `task` argument every other adapter
takes: for Codex and Claude that argument is the prompt, while Orca's prompt
already lives inside the Task and `--task` wants its identifier. The agent is
chosen from the target's provider, so an Anthropic target launches the Claude
agent rather than asking Codex to run a model it does not have.

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

## Not implemented

Named because they are easy to assume from the contract above: no model
selection from statistics, no SQLite, no scoring, no adaptive learning, no
escaped-defect tracking, and no automatic matching of findings.

What exists now is profile resolution, availability probing and dispatch to
Codex, Claude or Orca. What does not is anything that learns: no rule changes
itself from an outcome, and the router's two escalation rules stay where they
were written until a person moves them. Analysis of what this records is done today by
reading published comments — `gh api` plus `scripts/review_contract.py` — not by
a persistence layer.
