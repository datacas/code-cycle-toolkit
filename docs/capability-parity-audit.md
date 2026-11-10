# Agent capability parity audit

[← Documentation index](README.md)

Audit of every restriction the toolkit itself adds to a dispatched stage, done
for #450 on 2026-11-06 against `d8d34f5`, and re-read against `2f99512`
after #452 (Docker consent) and #453 (`attempts` normalization). The rule it applies is
[Agent capability parity](host-permissions.md#agent-capability-parity): any
operation the underlying agent can run directly, in the same environment and
with the same credentials, must also run through the toolkit. A risky one may
need a warning and the user's consent first. After that the toolkit adds no
veto of its own.

Each restriction gets one of three outcomes:

- **real limit**: the agent, the operating system, the provider or the
  infrastructure refuses the operation. It is kept and reported, and the
  toolkit looks for an alternative. User consent does not remove it.
- **role contract**: what a stage is for, such as a review that does not edit
  the code it judges, or a boundary the user defined. It is kept, and it is
  written down as a contract, not as a permission.
- **toolkit veto**: the agent could run the operation directly, but the
  toolkit refuses it, stops the stage, or downgrades its check. It becomes
  disclosure, consent when the operation is risky, and a record.

This document classifies the restrictions; it does not change any. The
conversion of each veto is delivered by the issue or change it links. The
vetoes no earlier issue tracked link a child issue of #450, #455 to #459: see
[child issues](#child-issues).

## Inventory

`stage_permissions` means `scripts/stage_permissions.py`. Function names are
given instead of line numbers so the references stay valid.

| ID | Restriction | Source | Role it serves | Incident | Outcome | Tracked by |
|---|---|---|---|---|---|---|
| PA-01 | Bash commands of a `claude -p` stage must run in the foreground | `stage_permissions.check_bash_tool_input`, rule `foreground_policy` | The stage's structured result is read when the turn ends, so its commands must have finished | #276: a dispatched Claude stage that ran a command in the background returned before it finished | real limit | — |
| PA-02 | `codex sandbox` / `codex exec` cannot start inside a Codex stage | environment recovery policy; host-by-nested-executor table in `docs/routing.md` | Nested verification inside a Codex stage | Isolation audit 2026-10-20; #268 | real limit | — |
| PA-03 | Claude's Bash sandbox needs `bubblewrap` and `socat` on Linux and WSL2, and can fail to start (`Can't find source path`) | `executors.py` sandbox probe and `sandbox_startup_failure` journal entry | Confining verification, registry access and publication | #345, #348 | real limit | — |
| PA-04 | Claude is not eligible for `disposable` roles (`verify`, `run`) | `docs/role-workspace-policy.md`; workspace contract in `scripts/cycle.py` and `scripts/executors.py` | Writes of those roles must stay inside a disposable workspace | Role workspace policy design | real limit | — |
| PA-05 | Code-host refusals: branch protection, rulesets, HTTP 403/404, missing scopes | GitHub and Bitbucket | The repository owner's own authorization | #284 upload recovery | real limit | — |
| PA-06 | Missing quota or authenticated session of an executor | `executors.py` `missing_capability` `operating_quota`, `authenticated_session` | Dispatching a stage at all | #389 | real limit | — |
| PA-07 | Review and rereview do not commit, push, edit the tree or write to the change request beyond their own comment or review | `GH_TABLE` and `GIT_WRITE` roles in `stage_permissions`; `check_file_edit`; disposable clone in `executors.py` | An independent judgement of an unchanged head | #142 | role contract | — |
| PA-08 | `issue_review` publishes nothing | `GH_TABLE` (no write entry for the role); `docs/role-workspace-policy.md` | A readiness judgement before any work exists | #142, #279 | role contract | — |
| PA-09 | A `read_only` checkout must be unchanged after the stage, or the stage is a `contract_violation` | `executors.py` `read_only_verification` fingerprint | Detects writes the sandbox cannot prevent (MCP servers, hooks) | A scripted MCP server appended to a tracked file under `codex exec -s read-only` | role contract | — |
| PA-10 | The cycle never merges or closes a change request; the publication audit stops on a merge, close, non-fast-forward or protected-ref move | `scripts/publication_audit.py` | The owner merges by hand; cycle integrity | #261, #197, #421 | role contract | — |
| PA-11 | Each stage writes only to the cycle's own change request and work item, and only the artefacts its role owns (implement opens the PR, review posts the review) | `_check_gh` scope checks and `GH_TABLE` roles in `stage_permissions` | Every artefact can be attributed to one stage attempt and receipt | #142 | role contract | — |
| PA-12 | The required controls of an execution plan and the frozen checks of a cycle cannot be removed or demoted by an agent | `scripts/execution_plan.py`, `scripts/verification_plan.py` | Controls chosen by the user or the plan, not by the agent under review | #327, #328, #393 | role contract | — |
| PA-13 | A check without executed evidence is never reported as passed; `verification_unverified` stops once with a question the user can answer with `--verification-answer KEY=accept` | `scripts/verification_plan.py`, `scripts/run_cycle.py` | Evidence integrity | #414, #425 | role contract | — |
| PA-14 | A consent answer covers only the stage role it was shown for; a writing-stage grant never authorizes a review | `scripts/grants.py` | The user sees and decides each role's request | #406 | role contract | — |
| PA-15 | Implementation runs in a task-specific linked worktree unless the run says `workspace=current` | `cc-implement-issue` *Implementation workspace*; `scripts/cycle_workspace.py` | Concurrent cycles never share a checkout | Worktree policy design; no single incident | role contract | — |
| PA-16 | A dirty implementer checkout blocks the preparation of a review clone | `executors.py` review clone preparation | A review judges a committed head, not uncommitted edits | #142 | role contract | — |
| PA-17 | A success receipt needs the remote head, the local HEAD and the PR head to be one commit | `scripts/publication.py`, `scripts/reroute.py` `observe_publication` | A publication is claimed only after it is observed | #284 | role contract | — |
| PA-18 | A program outside the stage table, or a read outside the stage workspace, is refused. Since #464 the read-only forms of `sed`, `date` and `ps` are in the table (PA-40) | `stage_permissions._check_words`, rule `command_table` | Closed default table | #406: refused `sed`, `cp`, `rm`, `sha256sum`, `pwd` stopped cycles | toolkit veto | #406 (converted by #409), #439, #441 |
| PA-19 | #439 converts compound shell syntax to interpreter consent; visible segments and substitutions retain concrete guards. A regex needs explicit compound approval; simple-command grants never extend to other segments. Native parity remains pending, so the audit stays open. Redirects outside `$TMPDIR` retain their concrete guard | `stage_permissions.parse_command`, `protected_reason` | Per-segment analysis and whole-operation consent | #450 (a pipe to `head`, a heredoc), #364, #187 | toolkit veto | #439 absorbs #455; #455 stays open until verified, #431 |
| PA-43 | Reads outside the stage workspace request exact or validated pattern consent; trailing slash, flags and another read tool can share an approved directory matcher. Reads under the current Claude session's own tool-results directory are allowed like `$TMPDIR` reads, and an operand there is a placeholder; a grant naming a dispatch- or session-unique path is offered for this cycle or as a pattern only | `stage_permissions.check`, `ConsentStore` | Role-scoped disclosure and consent | #439 | role contract | #439 |
| PA-20 | Git subcommands and forms outside the table are refused and not offered as questions: `ls-tree`, `stash`, `worktree add`, `branch` other than `--show-current`, `merge` in implement, other `pull` and `fetch` forms, global options other than `--no-pager` | `stage_permissions._check_git`, re-run by `protected_reason` | Closed Git table | #431 | toolkit veto | #431, #456 |
| PA-21 | `gh` reads beyond the table or the cycle's artefacts are refused and not offered as questions: `gh issue list` and `gh search` outside `issue_review`, `gh api` GET of other endpoints, `gh pr edit <n>` before the cycle knows its PR number | `stage_permissions._check_gh` and `GH_TABLE`, re-run by `protected_reason` | Closed `gh` table | #440; this audit's own implement stage was refused `gh issue list` on 2026-11-06 | toolkit veto | #440, #456 |
| PA-22 | `gh` writes outside the cycle's artefacts are refused and not offered as questions: `gh issue create`, comments on related issues or other change requests | `GH_TABLE` roles and `_check_gh` scope | Stage publication boundary | #450: the implement stage could not create the child issues this issue asks for | toolkit veto | #456 |
| PA-23 | Git options that run another program or write a file are refused and not offered as questions: `-c`, `-C`, `--upload-pack`, `--receive-pack`, `--exec-path`, `git grep -O`, `--output` outside `$TMPDIR`; so are `NAME=value` prefixes on `git` or `gh` that choose a program it starts or redirect it (`GIT_*`, `GH_*`, `GITHUB_*`, `HOME`, `XDG_*`, `PATH`, `LD_*`, `PAGER`, `EDITOR`, `SSH_*COMMAND` and similar) | `stage_permissions._check_git`, `GIT_EXECUTING_OPTIONS`; `protected_reason` with `_GUARD_DEFEATING_ENV` | Stops an allowed command from running a program the hook cannot see | #142, #452 | toolkit veto | #456 |
| PA-24 | With the `docker_host` grant, a `docker compose` command outside the table whose effective configuration the hook cannot resolve (`docker compose config` fails or exceeds the hook's 20-second budget) is refused and not offered as a question. Since #452 every other form outside `docker compose up\|down\|ps\|logs\|build`, including `docker compose run`, `docker run` and `docker exec`, is an exact consent question | `stage_permissions._check_docker`, `operation` and `_resolve_within_budget` | Docker access is root on the host; the consent names the configuration's host access | #398, #452 | toolkit veto | #398 (converted by #452), #456 |
| PA-25 | Every force-push variant is refused and cannot be granted | `stage_permissions._check_push` | Publication integrity | #142 | toolkit veto | #457 |
| PA-26 | A push to the base branch, `main` or `master` is refused and cannot be granted | `stage_permissions._check_push` | Publication integrity | #142 | toolkit veto | #457 |
| PA-27 | Ref deletion is refused and cannot be granted: `push --delete`, an empty-source refspec, `--prune`, `--mirror`, `--all`, `branch -d/-D`, `tag -d`, `update-ref -d` | `stage_permissions._check_push`, `protected_reason` | Publication integrity | #142 | toolkit veto | #457 |
| PA-28 | Reads of a protected credential store cannot be granted (`~/.ssh`, `~/.aws`, `~/.codex/auth.json`, `~/.claude/.credentials.json`, `~/.claude.json`, and `~/.config/gh` outside publishing stages); Claude's sandbox and Codex's profile deny them too. #439 covers static glob/brace operands matched one component at a time and possible directories after cd or stack operations, including `builtin`/`command` and `env -C` forms (also behind any program other than `echo`/`printf`/`grep`/`rg`), `~user`/`~+` tilde prefixes, and words and path/glob settings decoded from `$'…'`/`$"…"` quoting and line continuations. `nocaseglob` from decoded words or inherited `BASHOPTS` compares globs without case; extglob components conservatively match any name. An assigning builtin may set any path/glob setting, and computed `shopt`/`set` options may enable either glob mode; unrelated settings can therefore retain the guard for credential-like operands. After a directory change it cannot resolve (variables, `CDPATH`, a reassigned `HOME`/`OLDPWD`/`PWD`, `~-`/`~N` prefixes, `eval`/`source`/`trap`), only relative operands that may name a store keep this guard, and the rest, including `.`, are asked with a warning; a subcommand word spelled like a store file (`git config`) is still refused there | `stage_permissions.credential_reason`, rule `credential_guard`; `credential_read_denials`; Codex `none` filesystem rules; since #397, issue review reads a GitHub work item from the runtime's [issue snapshot](provider-contract.md#runtime-issue-snapshot) instead of the `gh` store, which stays denied | Credential confidentiality | #294, #397, #428, #439 | toolkit veto | #457 |
| PA-29 | Edits to `.git`, `~/.gitconfig` or `$XDG_CONFIG_HOME/git` cannot be granted | `stage_permissions.check_file_edit`, `protected_reason` | `core.fsmonitor` or `diff.external` would make an allowed `git status` run a program | #142 | toolkit veto | #457 |
| PA-30 | A `forever` grant is refused for `review` and `rereview`, and a stored global entry for them is ignored | `grants.NO_GLOBAL_ROLES` | Reviews judge untrusted changes in any repository | #406 | toolkit veto | #457 |
| PA-31 | When Claude's Bash sandbox cannot start, static-only mode denies verification, Docker, arbitrary-code tool choices and publication, and the checks become `not_verified` | `executors.py` (`static_only=True`); `stage_permissions._check_words` and `_check_git` | No repository code runs unconfined; no host-side push | #345, #348 | toolkit veto | #291 (checks), #457 (publication) |
| PA-32 | A Claude dispatch stops `BLOCKED` when the loaded configuration could defeat the hook (`disableAllHooks`, managed `allowManagedHooksOnly`, a `tool.check` mod) or widens the sandbox; a Codex review stops `BLOCKED` when its sandbox cannot be shown to confine writes | `executors.py` `policy_preflight`; `stage_permissions.sandbox_override`, `tool_check_mods`; Codex review sandbox proof | The command table must stay in force | #142 | toolkit veto | #458 |
| PA-33 | A configured verification command needs a Bash timeout of at least 600 s, and the toolkit sets `BASH_MAX_TIMEOUT_MS` to the same 600 s, so a longer check cannot finish | `stage_permissions.CLAUDE_VERIFICATION_TIMEOUT_MS`; `executors.py` dispatch environment | Avoids a check timing out early | #416: `basedpyright` needs 12 min 25 s | toolkit veto | #377 |
| PA-34 | A stage report that differs only in representation is rejected: plan receipt keys `plan_revision` / `plan_digest` instead of `revision` / `digest`, `evidence` entries in another shape, unknown keys in a closed result contract. `checks[].attempts` as an integer or a string is no longer rejected: #453 normalizes it | `scripts/review_contract.py` (`PLAN_RECEIPT_KEYS`), `scripts/issue_review.py` (`RESULT_KEYS`), `scripts/verification_plan.py` (`normalize_attempts`) | Machine-readable results | #433, #412, #435 | toolkit veto | #433 (converted by #453 for `attempts`), #459 |
| PA-35 | A grant for a configured verification command does not cover its variants (an environment prefix, `rtk`, a redirection into `$TMPDIR`), so the stage asks again | `grants.py` exact matching; `stage_permissions.operation` | Exact consent | #439 | toolkit veto | #439 |
| PA-36 | A publishing stage that reported `BLOCKED` after a verified publication stopped the cycle as `stage_not_completed`. Since #464 a stage that reports `BLOCKED` without `completed_status` is judged on its structured result (`inferred_completion`), and a stop that remains names the real cause apart from the incidental refusals | `scripts/command_permissions.py` `reconcile_result`, `inferred_completion`; `scripts/cycle_dispatch.py` `blocked_cause` | Completion evidence | #364, #446, #397 | toolkit veto | #364 (converted by #449 and #464) |
| PA-37 | A review or rereview publication the runtime did not observe was treated as missing and retried without confirming the remote state. Since #446 it is re-read three times with backoff before it is called absent, and a persistent read failure stops the cycle as `BLOCKED` with `publication_unverified`, which is a real limit of the provider and never triggers another publication | `scripts/cycle_publication.py` (`settle_publication`), `scripts/cycle_history.py` (`REREAD_BACKOFF`) | Completion evidence | #446 | toolkit veto | #446 (converted by #446) |
| PA-38 | Docker access needs the `docker_host` grant | `stage_permissions._check_docker` (`_DockerDenied`); `--grant docker_host=…` | Docker access is root on the host | #345, #398 | toolkit veto | #289 (converted to a cycle or persistent grant) |
| PA-39 | An issue review is not dispatched when the runtime's snapshot of its GitHub work item is not complete, and a `READY` that leaves an unreadable referenced item of the snapshot unnamed is not confirmed | `cycle_dispatch.review_issue`; `issue_review.assess` (`unreadable_links`); `scripts/issue_snapshot.py` | A readiness judgement on complete, disclosed input | #397 | role contract | — |
| PA-40 | The forms of `sed`, `date` and `ps` that write, run a program or show another process's command line are refused: `sed -i`, `--in-place`, `-f`, `--file`, the `w`, `W`, `e`, `r`, `R` commands and the `s` flags `w` and `e`, a `sed` that does not start with `-n`, `-E` or `-e`, `date -s`, `--set`, `-f`, `-r`, a positional time, `ps` fields `args`, `command` and `cmd`, and BSD-style `ps aux`. They are an exact consent question like any other table refusal (PA-18) | `stage_permissions.check_sed`, `check_date`, `check_ps`; `command_permissions._word_kind` | `sed`, `date` and `ps` inspect without a consent question; another process's arguments can carry a credential | #446: five `sed -n`, a `ps` and a `date` refused in one stage; #397 | toolkit veto | #464 |
| PA-41 | In a run with no human channel (`--detach`, no terminal) a refusal that no saved grant covers is denied and grants nothing; it is recorded as a pending decision with its command, stage, role, rule and reason, and an essential one stops the cycle as `permission_required` naming the command | `cycle._journal_attempt` (`unattended_denial`); `cycle_dispatch.unfinished` | Nobody can answer a question, and a silent grant would widen a sandbox | #446, #397: detached stages waited for a person with no recorded policy | real limit | — |
| PA-42 | A rerun resumes a finished `implement` or `resolve` stage at `review` or `rereview` only when the recorded resume point is consistent and the change request is open at the recorded head; otherwise it stops and names the mismatch | `scripts/cycle_resume.py` (`auto_resume`); `cycle_cli.auto_resume_of` | A review of a head that moved would judge other code; the explicit `--from` and `--pr` stay available | #446 `implement`, #397 `implement`, #446 `resolve` ended `BLOCKED` after publishing | role contract | — |

## Kept restrictions

Why each real limit and role contract stays.

| ID | Why it stays |
|---|---|
| PA-01 | `claude -p` ends when the model returns its final message, and a background command's output never reaches it. The same agent run directly in `-p` mode has the same limit. The toolkit adds nothing here; it only names the limit before the command runs. |
| PA-02 | Codex hides its own app-server socket directory from the commands it runs, so no `TMPDIR`, `CODEX_HOME` or `--no-daemon` change helps. Checks that need it are `not_verified`, and `docs/routing.md` names which executor pairs work. |
| PA-03 | The operating system lacks a component, or Claude Code cannot mount a protected path. The toolkit reports it and names the package to install. What it then denies is PA-31, a veto. |
| PA-04 | Claude cannot confine a whole process's writes to one directory, so it cannot keep a `disposable` role out of the source tree. |
| PA-05 | The code host enforces the owner's protection rules and the credential's scopes. A cycle reports the refusal and never tries to get around it. |
| PA-06 | An executor without quota or a session cannot run. The runtime reports it and offers the configured fallback. |
| PA-07 | A review stage is defined by judging a head it did not change. Write access would make it an implementer, so this is a role contract. It still runs, in its clone, every check it is allowed to verify. |
| PA-08 | The issue review is a gate before any work exists. Publishing would make it act on the work item it is judging. |
| PA-09 | This check does not stop the stage from running anything. It finds after the fact that a read-only contract was broken, whatever wrote the file. |
| PA-10 | The owner merges by hand and keeps the manual-merge evidence. The audit runs no command of its own; it records what changed on the host. A force-push the user authorized under #457 must be recorded as authorized, not reported as a breach. |
| PA-11 | Receipts and finding outcomes are attributed to one attempt on one change request. Writes outside the cycle are PA-22, a veto. |
| PA-12 | These are user-defined boundaries. The agent whose work is being checked cannot loosen its own checks. |
| PA-13 | Calling an unexecuted check a pass would be a false claim. The user can accept an unverified check explicitly, so this rule never vetoes the user. |
| PA-14 | The user approves the pattern for the role that asked for it. Applying it to another role would authorize something the user never saw. |
| PA-15 | A shared checkout let one cycle's dirty tree reach another. `workspace=current` is the user's per-run choice. |
| PA-16 | A review of uncommitted edits would not describe the head the change request publishes. |
| PA-17 | A success receipt needs to match what is actually published. This rule refuses no operation. |
| PA-39 | A `READY` judged on a partial work item would be a claim about input nobody read. The stop names the missing data and the provider's real failure; no command of the stage is refused, its credential denials are unchanged, and `--issue-review off` remains the user's choice. |
| PA-41 | With no one attached, a question has no answerer, which is the same limit an agent run directly in `-p` mode meets when its permission layer denies a command. The toolkit grants nothing implicitly, records the denial and the question for later, and never waits indefinitely. A saved grant (#406) applies first, and `docker_host` and configured-check consent keep `permission_required`. |
| PA-42 | The resume point is evidence the runtime wrote after reading the stage's structured result, not a permission. Using it on a moved head would review or fix code the earlier stage never produced, so a mismatch stops and is named. `--no-auto-resume` starts the work item over and `--from <stage> --pr <n>` resumes by hand, so the user keeps every option. |
| PA-43 | A stage discloses reads outside its assigned workspace and the user can authorize an exact command or reusable pattern. This is a role contract with consent, and a stored answer removes the toolkit question without widening host or OS permissions. |

## Vetoes: conversion and parity baseline

Each veto gets one parity test, delivered with its conversion. The baseline is
the same operation run directly by the agent in the same worktree, with the
user's own configuration and credentials:

- **Claude**: `claude -p` started in the stage's worktree with the user's
  settings and permission rules, without the stage's `--settings` hook and
  `--allowedTools` list;
- **Codex**: `codex exec` started in the stage's worktree with the user's
  `~/.codex` configuration and the sandbox mode of the stage's role
  (`workspace-write` for `implement` and `resolve`, `read-only` for the
  others), without the stage's permission profile additions.

The test passes when the operation reaches the same outcome through the toolkit
as at the baseline once the user has consented. Anything the baseline itself
refuses is a real limit and stays. Probes that touch credentials or remote refs
use a fixture `HOME` and a throwaway local bare remote, never real stores or
the real repository.

| ID | Conversion | Claude baseline | Codex baseline |
|---|---|---|---|
| PA-18 | Done in #409: the request is offered as **this cycle / this repository / forever / deny**, recorded, and skipped with `not_verified` checks when denied | `claude -p` runs `sha256sum README.md` in the worktree | `codex exec -s workspace-write` runs `sha256sum README.md`; a Codex stage has no command table, so the test pins that it stays equal |
| PA-19 | #439: consent for whole shell source, optionally a user-approved validated pattern; guards analyse each segment and substitutions. #455 stays open pending native verification | `claude -p` runs `git log --oneline \| head -5` and a heredoc `cat <<EOF` into `$TMPDIR` | `codex exec -s workspace-write` runs the same two commands |
| PA-20 | Offer table-membership refusals as exact grants; keep argument guards as warnings shown in the question | `claude -p` runs `git ls-tree HEAD` and `git stash list` | `codex exec -s workspace-write` runs the same |
| PA-21 | Offer reads beyond the table and the cycle as exact grants, labelled read-only | `claude -p` runs `gh issue list --repo <cycle repo> --state open` | `codex exec -s workspace-write` with the network route a publishing stage has runs the same |
| PA-22 | Offer writes outside the cycle as exact grants, with the target artefact named and a risk warning | `claude -p` runs `gh issue create --repo <fixture repo> --title t --body b` | `codex exec -s workspace-write` runs the same against the fixture repository |
| PA-23 | Offer as an exact grant with the interpreter warning: the option or the prefix can run any program | `claude -p` runs `git -c core.pager=cat log -1` and `GIT_PAGER=cat git log -1` | `codex exec -s workspace-write` runs the same |
| PA-24 | Done in #452 for `docker compose run`, `docker run`, `docker exec` and every other form: an exact consent question that warns that Docker is root on the host and lists the configuration's host access. #456 offers a Compose command whose configuration cannot be resolved as an exact grant, warning that its host access is unknown | `claude -p` with the user's Docker access runs `docker compose run --rm <service> true` | `codex exec -s workspace-write` with socket access runs the same |
| PA-25 | Ask with an explicit risk warning (rewrites published history), record the grant, and have the publication audit (PA-10) report the authorized force-push as recorded | `claude -p` runs `git push --force-with-lease origin <branch>` against a throwaway bare remote | `codex exec -s workspace-write` runs the same against the throwaway remote |
| PA-26 | Ask with an explicit risk warning (changes the base the owner merges into), and record the decision | `claude -p` runs `git push origin HEAD:main` against a throwaway bare remote | `codex exec -s workspace-write` runs the same |
| PA-27 | Ask with an explicit risk warning (deletes a ref others may use), and record the decision | `claude -p` runs `git push origin --delete <branch>` and `git branch -D <branch>` against a throwaway bare remote | `codex exec -s workspace-write` runs the same |
| PA-28 | Ask per store and scope, with a warning that a credential becomes readable by the stage and by code it runs; widen the sandbox's deny-read rule only for the store the user approved; record the decision. #397 removed the issue-review need for the `gh` store: the runtime reads the work item and passes a snapshot | `claude -p` runs `cat $HOME/.ssh/config` with a fixture `HOME` | `codex exec -s workspace-write` runs the same with the fixture `HOME` |
| PA-29 | Ask with a warning that a Git setting can make later allowed commands run a program, and record the decision | `claude -p` edits `.git/info/exclude` in a fixture repository | `codex exec -s workspace-write` makes the same edit |
| PA-30 | Offer `forever` for review roles too, with a warning that the pattern will then run on untrusted changes in every repository | Claude's own global `permissions.allow` in `~/.claude/settings.json` lets `claude -p` run the pattern in every repository | Codex's own `~/.codex/rules/default.rules` `allow` lets `codex exec` run the pattern in every repository |
| PA-31 | #291 runs authorized checks on an isolated host runner; #457 asks before publishing without a confirmed sandbox | `claude -p` runs the configured test and `git push origin <branch>` on a host without `bubblewrap` | `codex exec -s workspace-write` runs the same; Codex does not use Claude's sandbox, so the test pins equality |
| PA-32 | Disclose the reduced guarantee (which hook or sandbox setting is defeated), ask once per scope, record it, and run | `claude -p` runs a stage command with the user's `disableAllHooks` or `tool.check` mod loaded | `codex exec -s read-only` runs a review whose clone confinement cannot be proven |
| PA-33 | #377 derives the timeouts from a per-project and per-check configuration instead of the 600 s constant | `claude -p` with `BASH_MAX_TIMEOUT_MS` set by the user runs a check that takes more than 10 min | `codex exec -s workspace-write` runs the same check under its own command limit |
| PA-34 | Done in #453 for `checks[].attempts`: an integer or a string is normalized, and the original is kept as `attempts_normalized`. #459 applies the same rule to plan receipt aliases, `evidence` shapes and unknown keys, keeping the original value next to the normalized one | Not an agent operation: the baseline is the result `claude -p` produced, which the user reads as is | Same: the result `codex exec` produced |
| PA-35 | #439 binds a grant to the configured check, so its harmless variants reuse it | `claude -p` runs `rtk pnpm run test:unit` after the user allowed `pnpm run test:unit` | `codex exec -s workspace-write` runs the same variant |
| PA-36 | Done in #449: the runtime reconciles a stage whose publication it verified. Done in #464 for a stage that reports `BLOCKED` with no `completed_status`: the structured result and the verified publication decide, and the stop names its real cause | `claude -p` pushes and opens the PR, and the published state is the outcome | `codex exec -s workspace-write` does the same |
| PA-37 | #446 re-reads the change request a bounded number of times before retrying | `claude -p` posts the review comment, which then exists on the change request | `codex exec` posts the same comment |
| PA-38 | Done in #289: `--grant docker_host=cycle\|persistent` or `docker.host_socket_access: true` | `claude -p` with the user's Docker access runs `docker compose ps` | `codex exec -s workspace-write` with socket access runs `docker compose ps` |
| PA-40 | Done in #464: `sed -n`, `sed -E` and `sed -e` read-only scripts, `date` without a clock change and `ps` with fields that show no command line are in the table of every stage; the other forms stay the PA-18 consent question | `claude -p` runs `sed -n 1,5p fixture.txt`, `date +%s` and `ps -eo pid,comm`, then `sed -i s/a/b/ fixture.txt` and `ps -eo pid,args` | `codex exec -s workspace-write` runs the same commands; a Codex stage has no command table, so the test pins that it stays equal |

## Sensitive operations

| Operation | Row | Classification | Rationale |
|---|---|---|---|
| Force-push | PA-25 | toolkit veto | Toolkit-defined; the agent can run it directly. Owner decision 2026-11-06: consent with a clear risk warning, a record, and no further veto after consent. |
| Pushes to the base branch | PA-26 | toolkit veto | Same decision. Branch protection on the code host is a real limit (PA-05) and stays. |
| Ref deletion | PA-27 | toolkit veto | Same decision. |
| Credential reads | PA-28 | toolkit veto | Same decision. Consent widens access only to the store the user approved, and only for the chosen scope. |
| Git config edits | PA-29 | toolkit veto | Same decision. The warning names the risk: a later allowed command can run a program. |
| Publication guards: writes only to the cycle's own artefacts, by the stage that owns them | PA-11 | role contract | Cycle integrity: receipts and finding outcomes are attributed to one attempt. |
| Publication guards: no merge or close, audited after each stage | PA-10 | role contract | Cycle integrity: the owner merges by hand. The audit is a record, not a command refusal. |
| Publication guards: success claimed only after the publication is observed | PA-17 | role contract | Evidence integrity; no operation is refused. |
| Publication guards: writes outside the cycle's artefacts | PA-22 | toolkit veto | The agent can create an issue or comment elsewhere directly; that becomes a consented and recorded operation. |
| Publication guards: no publication without a confirmed sandbox | PA-31 | toolkit veto | The missing sandbox is a real limit (PA-03); refusing to publish without it is toolkit policy, so it becomes consent. |
| Forever-grants for review | PA-30 | toolkit veto | The agent's own global allow rules already reach every repository, so the toolkit cannot refuse what the user can configure directly. The question warns about untrusted changes. Scoping a grant to its role (PA-14) stays a role contract. |

## Reviews

A change that adds a restriction of the toolkit's own, without a real limit or
a role contract behind it, is a review finding. `AGENTS.md` states this, and
`cc-pr-review` applies the review rules the repository's instructions state.
Classify a new restriction with the three outcomes above. A new veto needs a
row in this audit, an issue that converts it, and a parity baseline. Without
them, the review reports it.

<a id="child-issues-to-create"></a>

## Child issues

These are the child issues of #450 for the vetoes no earlier issue tracked.
They were drafted here as `CI-1` to `CI-5`. The implement stage of #450 could
not create them: `gh issue create` is outside its publication policy, and its
command table refuses it without asking (PA-22). The owner created them on
2026-11-06 with the milestone and priority given here, and they are in the
milestone table of [ROADMAP_V2](ROADMAP_V2.md).

| ID | Draft | Title | Milestone | Priority | Rows | Scope |
|---|---|---|---|---|---|---|
| #455 | CI-1 | Compound shell consent absorbed by #439; stays open until integrated verification | M1 — Safe, inspectable execution | P1 | PA-19 | #439 owns shared simple/compound consent, segment guards and validated pattern grants. Record the interpreter warning and decision; verify both native executor baselines before closing #455. |
| #456 | CI-2 | Offer consent for Git, gh and Docker table refusals | M1 — Safe, inspectable execution | P1 | PA-20, PA-21, PA-22, PA-23, PA-24 | Stop `protected_reason` from turning table membership and argument guards into refusals that are never asked about. Ask with the guard's risk as the warning: executing options, writes outside the cycle, Docker as root. Keep each grant exact and recorded. Add one parity test per row. #431 and #440 stay the narrower fixes they track. #452 already asks about Docker forms outside the table, so the Docker part left is a Compose command whose configuration cannot be resolved (PA-24). |
| #457 | CI-3 | Convert the never-grant boundaries to consent with a risk warning and a record | M1 — Safe, inspectable execution | P1 | PA-25, PA-26, PA-27, PA-28, PA-29, PA-30, PA-31 | Ask about force-pushes, base pushes, ref deletion, credential reads, Git config edits, `forever` grants for review roles, and publication without a confirmed sandbox. After consent, run with no further veto. The publication audit reports an authorized operation as recorded, not as a breach. Code-host protection stays a real limit. Add one parity test per row. |
| #458 | CI-4 | Run with a disclosed reduced guarantee instead of stopping when the configuration defeats the hook | M1 — Safe, inspectable execution | P2 | PA-32 | Name the setting that defeats the hook or the sandbox. Ask once per scope, record the decision, and dispatch. A Codex review whose clone confinement cannot be proven gets the same question rather than `BLOCKED`. |
| #459 | CI-5 | Normalize report and receipt representations instead of rejecting them | M1 — Safe, inspectable execution | P1 | PA-34 | Extend #433's rule to plan receipt aliases (`plan_revision`, `plan_digest`), `evidence` entries and unknown keys in closed result contracts. Keep the original value next to the normalized one. Reject only shapes that cannot be normalized, naming the field. |

---

[← Host permissions](host-permissions.md) · [↑ Documentation index](README.md)
