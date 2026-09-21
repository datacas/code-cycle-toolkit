---
name: cc-orchestrator
description: Use this skill to run the complete Code Cycle from a GitHub, Plane, or Jira work item through implementation, pull-request review, targeted resolution, and rereview on GitHub or Bitbucket, using known host delegation such as optional Claude-to-Codex review handoffs or a sequential fallback without merging.
---

# Code Cycle Orchestrator

Coordinate the complete lifecycle of one work item. This skill owns sequencing,
state handoff, iteration limits, and exit conditions. It does not own review
criteria, implementation decisions, or security judgements; those belong to the
delegated skills.

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

## Provider context

Resolve `issue_provider=github|plane|jira` and `code_host=github|bitbucket`
independently before dispatching any stage. Accept explicit `issue_id`,
`repository`, and provider values first, then the repository's optional
`.code-cycle.yml`, then an unambiguous code-host `origin`. A GitHub issue
number may be inferred only for a GitHub code host; never guess Jira or Plane
identifiers. If the pair is incomplete or ambiguous, stop with `BLOCKED`
before starting implementation. Pass the resolved provider context unchanged
to every delegated stage. See `docs/provider-contract.md` for the canonical
fields and capability rules.

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

## Responsibilities

Do:

- resolve the issue provider, work item, code host, and repository before
  starting work;
- run `cc-provider-bootstrap`, then `cc-implement-issue`, `cc-initial-review`,
  `cc-resolve-comments`, and
  `cc-rereview` in the defined order, letting each of them delegate to the
  review passes it owns — `cc-pr-review`, `cc-code-review`,
  `cc-security-review`, `cc-verify` — rather than invoking those passes here;
- pass the current structured state to the next stage;
- resolve one execution mode before implementation and keep the selected stage
  executors stable for the run;
- preserve stable `REV-xxx` finding IDs across review iterations;
- enforce the iteration limit and no-progress guard;
- validate the final state against the configured code host and work-item
  provider before reporting it.

Do not:

- implement code or make review judgements yourself;
- reclassify, dismiss, or resolve a finding;
- invent host commands or assume a worker API exists;
- install, authenticate, update, or vendor an optional delegation plugin;
- merge the pull request or ask another agent to merge it.

## Inputs

Accept:

| Input | Default | Meaning |
|---|---|---|
| `issue_id` | required | Work-item identifier; `issue_number` is a GitHub compatibility alias. |
| `issue_provider` | resolved | `github`, `plane`, or `jira`. |
| `code_host` | resolved | `github` or `bitbucket`. |
| `repo` | resolved | Repository selector on the configured code host. |
| `orchestration_mode` | `auto` | `auto`, `single_agent`, or `claude_codex`. |
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

## Execution modes

Resolve `orchestration_mode` from an explicit invocation value, then
`code_cycle.orchestration.mode` in `.code-cycle.yml`, then `auto`. Reject any
other value.

- `single_agent`: execute every stage sequentially in the current agent.
- `claude_codex`: when the current host is Claude Code and exposes a known,
  task-capable `codex-plugin-cc` interface, keep implementation and resolution
  in Claude and delegate initial review and rereview to Codex.
- `auto`: use a known host adapter only after its capabilities and the user's
  saved preference resolve it; otherwise use `single_agent`.

Do not identify a plugin from guessed installation directories, process names,
or the mere presence of a `codex` binary. Use only capabilities the current
host declares and can invoke with a durable result. The plugin remains an
optional external dependency and its absence never blocks `auto` mode.

When running in Claude Code and the optional adapter may apply, read
[references/codex-plugin-cc.md](references/codex-plugin-cc.md) before selecting
the mode or dispatching a review. That reference defines discovery, the
first-run notice, stage ownership, result validation, and failure behavior. Do
not read it for Codex, OpenCode, Orca, or an explicit `single_agent` run.

## Routing

Name a profile, never a model. `cheap_coder`, `deep_coder`, `reviewer`,
`senior_reviewer`, `security`, `coordinator`, `cheap_tool` resolve through
`code_cycle.profiles` in `.code-cycle.yml`; `scripts/router.py` is the reference
implementation of how.

`scripts/executors.py` turns a resolved target into a real execution:

```text
probe()    -> what each executor could be shown to be, and on what evidence
route()    -> which target the role resolves to
dispatch() -> that target actually running, or BLOCKED naming what is missing
```

Probe before routing, and pass what the probe found. The executors differ in
what they can prove: Orca reports its runtime state, so `ready` is provable;
Codex and Claude expose a version and a credential, which proves `authenticated`
and no more, because remaining quota is not observable without spending it. A
caller that accepts dispatching from `authenticated` asks for it through the
`attempt` readiness policy, and the result records that it dispatched from an
unproven state. Never treat that promotion as a probe finding.

A calibration dispatch requires demonstrated readiness. An arm that ran from an
unproven state would put a sample whose executor state nobody established next
to samples where it was.

Interactive friction blocks. A folder-trust dialog, a hook-review screen, a
bypass acknowledgement or a login prompt makes the run unattainable without a
person, and the adapter returns `BLOCKED` naming the capability rather than
answering a security prompt blind. Report the named capability so the user can
grant it once instead of guessing.

Compare the model that ran against the model requested. When the executor
reports it, keep both; when it does not, record that it was unreported rather
than assuming a match — silence is a third answer, not a quiet yes.

Say in the result which profile and target were chosen, whether a fallback was
used and why, and which readiness state the dispatch actually started from.

Resolve executor availability **before** choosing anything. An executor that is
merely installed is not dispatchable: a binary on PATH proves no session, no
repository access and no quota. One whose quota is exhausted is not a candidate
whatever its profile would score. Report what you observed rather than inferring
it — a failed dispatch is one exhausted window, not evidence about a model.

When the primary executor is unavailable, a production run may use the profile's
fallback and must say it did. A calibration run may not: substituting an arm
answers a different question with the same sample, so it stops with `BLOCKED`
and waits.

Label `difficulty` and `verifiability` before routing, never after. Choosing a
model from a judgement and then measuring results by model measures the routing
rather than the models.

Two rules escalate, both from declared signals: difficulty 3 implementation work
goes to `deep_coder`, and a security-sensitive change is reviewed by
`senior_reviewer`. There is no evidence for finer rules yet, and inventing them
would make this look calibrated when it is not.

When weighing cost, weigh the cycle and not the first pass. Every real
implementation measured so far needed a correction round, so the estimate
carries at least one resolution plus its review until a repository measures its
own first-pass rate.

## Workflow

1. Run `cc-provider-bootstrap` with the explicit inputs and request its
   `PROVIDER_BOOTSTRAP_RESULT`. If it returns `BLOCKED` or `FAILED`, stop before
   creating work; otherwise pass its resolved context unchanged to every stage.
2. Probe every executor once and keep the availability map for the whole run.
   Record what each probe demonstrated and on what evidence, and record the
   readiness policy in force. Do not re-probe silently between stages: an
   availability that changes without being recorded makes every later decision
   unexplainable.
3. Label `difficulty` and `verifiability` for the work item, and whether the
   change is security-sensitive, **before** routing anything. Labelling
   afterwards would mean the routing was chosen and then justified.
4. For each stage, route its role against those signals and that availability
   map, then dispatch the resolved target:

   ```text
   probe -> availability map
         -> route(stage role, signals)
         -> dispatch(decision)
         -> validate the dispatch result
         -> next stage
   ```

   Record the profile, the target, whether a fallback was used and why, and
   which readiness state the dispatch started from. A stage whose dispatch is
   `BLOCKED` stops the run with the named capability; it is not retried and not
   quietly re-routed. A dispatch that reports running a different model than the
   one requested is a broken contract, not a success: stop rather than letting
   the cycle continue on work nobody asked that model to do.

   An execution mode named in the invocation, such as `single_agent` or
   `claude_codex`, fixes the executors for every stage and replaces the routing
   step. Say which of the two paths the run took; never mix them within one run.
5. Run `cc-implement-issue` on the target routed for `implement`, and request
   its structured result. Require the change-request ID, branch, and current
   head SHA before continuing.
6. Run `cc-initial-review` on that change request, on the target routed for
   `review`, and request its structured result.
7. If the review returns `APPROVED`, validate the final exit conditions. If it
   returns `CHANGES_REQUESTED`, begin an iteration with
   `cc-resolve-comments`. Stop on `BLOCKED` or `FAILED`.
8. For each iteration, pass the previous structured result to
   `cc-resolve-comments`, then pass its result to `cc-rereview`, each on the
   target routed for its role.
9. Continue only when the statuses and external conditions allow it. Record
   the current head SHA and open finding IDs after every iteration.
10. Stop with `HUMAN_INTERVENTION` when the iteration limit is reached or when
    the same head SHA and open finding set repeat without progress.
11. Before reporting readiness, confirm that the reviewed SHA equals the current
   change-request head, required checks have passed, no blocking finding is
   open, and the change request remains unmerged.

## Stage statuses

Branch only on the delegated skill's structured result:

- `cc-implement-issue`: `IMPLEMENTED`, `BLOCKED`, `FAILED`;
- `cc-provider-bootstrap`: `READY`, `BLOCKED`, `FAILED`;
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

Pass the provider bootstrap result and the complete previous
`ORCHESTRATION_RESULT` to every stage. Each stage must preserve existing finding
IDs and SHAs, validate them against the configured code host, and return its own
result. The published
change-request comment and provider state remain authoritative when structured
state conflicts with them.

Keep one run-scoped copy of each result outside the repository when the host
supports result files. Ask the user before creating the directory that holds
them: name the exact path and say why it must live outside the working tree.
Never stage result files, credentials, or transcripts in the pull-request
branch.

## Final result

Return a short human-readable summary and, when requested by the caller or
host, one strict JSON block:

```text
ORCHESTRATION_RESULT
{
  "skill": "cc-orchestrator",
  "status": "READY_FOR_MANUAL_MERGE",
  "issue_provider": "github",
  "issue_id": "123",
  "issue_number": 123,
  "code_host": "github",
  "pr_number": 456,
  "change_request_id": "456",
  "change_request_url": "https://github.com/owner/repository/pull/456",
  "repo": "owner/repository",
  "head_sha": "89abcdef0123456789abcdef0123456789abcdef",
  "reviewed_head_sha": "89abcdef0123456789abcdef0123456789abcdef",
  "iterations": 2,
  "max_iterations": 6,
  "blocking_findings": [],
  "merge": "manual",
  "summary": "Work item implemented, reviewed, corrected, and reapproved on the current HEAD. Merge is manual.",
  "blocking": false
}
END_ORCHESTRATION_RESULT
```

This is the same envelope `cc-orca-orchestrator` returns, minus the fields that
only a supervised run has, so one consumer parses both. New consumers should
use `issue_id`, `change_request_id`, `issue_provider`, and `code_host`; the
numeric `issue_number` and `pr_number` fields are compatibility aliases.

Use `READY_FOR_MANUAL_MERGE` only after all exit conditions hold. Use
`HUMAN_INTERVENTION` for a budget or decision boundary, `BLOCKED` for an
external condition, and `FAILED` for an unexpected technical failure. For every
status other than `READY_FOR_MANUAL_MERGE`, state in the summary what is
missing and who has to act. Report `pr_number: null` when no PR was created,
`reviewed_head_sha: null` when no review completed, and the real `iterations`
count even when the run stopped early.
