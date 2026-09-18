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

## Not implemented

Named because they are easy to assume from the contract above: no router, no
model selection from statistics, no SQLite, no scoring, no adaptive learning, no
escaped-defect tracking, no general profile system, no executor abstraction, and
no automatic matching of findings. Analysis of what this records is done today by
reading published comments — `gh api` plus `scripts/review_contract.py` — not by
a persistence layer.
