---
name: cc-orchestrator
description: Use this skill to run the complete Code Cycle from a GitHub issue through implementation, pull-request review, targeted resolution, and rereview, using the host's available delegation mechanism or a sequential fallback without merging.
---

# Code Cycle Orchestrator

Coordinate the complete lifecycle of one issue. This skill owns sequencing,
state handoff, iteration limits, and exit conditions. It does not own review
criteria, implementation decisions, or security judgements; those belong to the
delegated skills.

## Responsibilities

Do:

- resolve the issue and repository before starting work;
- run `cc-implement-issue`, `cc-initial-review`, `cc-resolve-comments`, and
  `cc-rereview` in the defined order;
- pass the current structured state to the next stage;
- preserve stable `REV-xxx` finding IDs across review iterations;
- enforce the iteration limit and no-progress guard;
- validate the final state against GitHub before reporting it.

Do not:

- implement code or make review judgements yourself;
- reclassify, dismiss, or resolve a finding;
- invent host commands or assume a worker API exists;
- merge the pull request or ask another agent to merge it.

## Inputs

Accept:

| Input | Default | Meaning |
|---|---|---|
| `issue_number` | required | Issue to implement and review. |
| `repo` | resolved | GitHub repository selector. |
| `max_iterations` | `6` | Maximum resolve+rereview cycles. |
| `merge` | `manual` | Fixed; automatic merge is not supported. |

Reject an iteration limit below `1`. Treat an unknown or unavailable
repository as `BLOCKED` before starting implementation.

## Host-neutral execution

First determine which capabilities the current host actually provides:

- delegated workers or subagents;
- durable task completion messages;
- isolated worktrees or terminals;
- a way to pass structured state between stages.

Use those capabilities only when they are available and their contract is
known. Do not translate this skill into guessed commands for a particular
agent. When delegation is unavailable, load and execute each delegated skill
sequentially in the current session, preserving the same state and branch.

All stages that touch the same pull request must use the same branch or an
explicitly coordinated worktree. Never let parallel workers edit the same
branch concurrently.

## Workflow

1. Resolve `issue_number` and `repo`. If either is ambiguous, stop with
   `BLOCKED` before creating work.
2. Run `cc-implement-issue` and request its structured result. Require the PR
   number, branch, and current head SHA before continuing.
3. Run `cc-initial-review` on that PR and request its structured result.
4. If the review returns `APPROVED`, validate the final exit conditions. If it
   returns `CHANGES_REQUESTED`, begin an iteration with
   `cc-resolve-comments`. Stop on `BLOCKED` or `FAILED`.
5. For each iteration, pass the previous structured result to
   `cc-resolve-comments`, then pass its result to `cc-rereview`.
6. Continue only when the statuses and external conditions allow it. Record
   the current head SHA and open finding IDs after every iteration.
7. Stop with `HUMAN_INTERVENTION` when the iteration limit is reached or when
   the same head SHA and open finding set repeat without progress.
8. Before reporting readiness, confirm that the reviewed SHA equals the current
   PR head, required checks have passed, no blocking finding is open, and the
   PR remains unmerged.

## Stage statuses

Branch only on the delegated skill's structured result:

- `cc-implement-issue`: `IMPLEMENTED`, `BLOCKED`, `FAILED`;
- `cc-initial-review` and `cc-rereview`: `APPROVED`, `CHANGES_REQUESTED`,
  `BLOCKED`, `FAILED`;
- `cc-resolve-comments`: `RESOLVED`, `PARTIALLY_RESOLVED`, `BLOCKED`,
  `FAILED`.

Never infer a functional status from free-form terminal text, a worker subject,
or a host-specific outcome field. If no valid structured result can be
recovered, treat the stage as `FAILED`.

`PARTIALLY_RESOLVED` may proceed to rereview only when its result says
`blocking: false` and does not require external intervention. Otherwise stop
with `HUMAN_INTERVENTION` and list the unresolved findings.

## State handoff

Pass the complete previous `ORCHESTRATION_RESULT` to every resolution and
rereview stage. The stage must preserve existing finding IDs and SHAs, validate
them against GitHub, and return its own result. The published PR comment and
GitHub state remain authoritative when structured state conflicts with them.

Keep one run-scoped copy of each result outside the repository when the host
supports result files. Never stage result files, credentials, or transcripts
in the pull-request branch.

## Final result

Return a short human-readable summary and, when requested by the caller or
host, one strict JSON block:

```text
ORCHESTRATION_RESULT
{
  "skill": "cc-orchestrator",
  "status": "READY_FOR_MANUAL_MERGE",
  "issue_number": 123,
  "pr_number": 456,
  "head_sha": "89abcdef0123456789abcdef0123456789abcdef",
  "reviewed_head_sha": "89abcdef0123456789abcdef0123456789abcdef",
  "iterations": 2,
  "max_iterations": 6,
  "blocking_findings": [],
  "merge": "manual",
  "blocking": false
}
END_ORCHESTRATION_RESULT
```

Use `READY_FOR_MANUAL_MERGE` only after all exit conditions hold. Use
`HUMAN_INTERVENTION` for a budget or decision boundary, `BLOCKED` for an
external condition, and `FAILED` for an unexpected technical failure.
