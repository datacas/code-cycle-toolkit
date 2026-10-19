# Host permissions

[← Documentation index](README.md)

What each dispatched stage may run, how each host enforces it, and the opt-in
global set for interactive sessions. Until #142 a dispatched stage kept its
executor's ordinary `gh` and `git` access and its prompt was the only boundary;
this page describes what replaced that, and what it still does not cover.

## Per stage, not global

The runtime applies permissions to each stage it dispatches natively
(`claude -p`, `codex exec`). Nothing is written to a global file as a side
effect, and nothing is written into a worktree.

| Host | What a stage receives | What it confines |
|---|---|---|
| Claude | `--allowedTools` built from the stage's closed table, and a `PreToolUse` validation hook added to the dispatch | every Bash command, its arguments, flags and refspecs |
| Codex | the stage's sandbox profile, and `--ignore-rules` so `~/.codex/rules/default.rules` is not inherited | files and network, **not** commands |

The table and the hook apply only when both `code_host` and `issue_provider`
are GitHub. Other code hosts and issue providers use tooling the table does not
list, so they keep the earlier behaviour, and each dispatch records
`command_policy: inactive: …` saying so. Orca workers keep their current
behaviour too (`inactive: orca workers …`); a review that must verify inside a
confined clone is never routed to Orca.

## The stage table

| Stage | Reads | Writes and publication |
|---|---|---|
| every stage | `git status`, `diff`, `log`, `rev-parse`, `show`, `branch --show-current`; `gh issue view`, `pr view`, `pr checks`, `auth status`, `repo view`; `gh api user --jq .login`; `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `find`, `jq` | — |
| `issue_review` | `gh issue list`, `gh pr list`, `gh search issues\|prs`; `git merge-base`, `ls-files`, `grep`, `blame` | none |
| `review`, `rereview` | `gh pr diff`, `git merge-base`, `ls-files`, `grep`, `blame`; `git fetch origin` only when the workspace has an `origin` remote (Claude's review clones have none) | `gh pr comment` and `gh pr review --comment` on the cycle's change request |
| `implement` | as review, plus `git worktree list`, `gh pr list`, `gh run view\|list` | `git add`, `commit`, `push`, `fetch`, `checkout`, `switch`, `pull`, `rm`, `mv`; `gh pr create`, `gh pr edit` and `gh pr comment` on the branch's change request; `gh issue comment` on the work item |
| `resolve` | as implement | as implement without `gh pr create`/`edit`; and exactly three `gh api graphql` calls for review threads |

Plus, for implementation, resolution, review and rereview only, the repository's
`allowed_commands.test` (exact) and `allowed_commands.verify` (prefixes). Review
and rereview run them **only inside their disposable clone**. Without either key
no verification command is granted; a stage that needs one stops `BLOCKED` and
names the command. See [Configuration → Allowed commands](configuration.md#allowed-commands).

The hook validates arguments, not only the program, and it is fail-closed:

- one simple command per call; chaining (`&&`, `;`, `|`), substitutions, other
  variable expansions, globs and control characters are refused;
- `$TMPDIR` is the only expansion, and only when it is the dispatch's own
  runtime directory (`<parent>/code-cycle-tmp-*`). A path given to
  `--body-file`, `git … --output` or an output redirection (`>`, `>>`, `2>`)
  must resolve, through symlinks and `..`, strictly inside it: not `/tmp`,
  not another `code-cycle-tmp-*`, not the checkout;
- `git push` must name `origin` and an explicit branch. Every force variant is
  refused (`--force`, `--force-with-lease`, `--force-if-includes`, `-f` in any
  combined flag, a `+refspec`, flags after the refspec), as are `--mirror`,
  `--all`, `--delete`, `--prune` and an empty source. The base branch, `main`
  and `master` are never a destination, whether spelled `main`,
  `refs/heads/main`, `heads/main` or `HEAD` while on it;
- Git options that run another program (`-c`, `-C`, `--upload-pack`,
  `--receive-pack`, `--exec-path`, `grep -O`) are refused, including the
  abbreviations Git expands (`--open-files=…`, `--upload-pa=…`) and `O` in a
  bundled `git grep` flag (`-iO…`);
- `gh` is limited to this cycle's repository (`-R`/`--repo`, URLs), change
  request and work item; `--hostname` is refused; `gh api` is refused except
  `gh api user --jq .login` and resolve's three thread calls;
- `find -exec`, `-execdir`, `-ok`, `-okdir`, `-delete` and `-fprint*` are
  refused, and the read-only shell tools may not redirect output;
- `docker compose up|down|ps|logs|build` only with the `docker_host` grant;
  `docker run`, `docker exec`, `docker compose run` and every other subcommand
  are always refused.

`rtk …` and `rtk proxy …` forms are accepted only when RTK is installed, and
are validated as the command they wrap.

### Review threads

`gh pr` cannot read, answer or resolve review threads, so resolve alone may run
these three calls, one operation per call, with every value as a variable:
the `repository.pullRequest.reviewThreads` query on the cycle's change request,
`addPullRequestReviewThreadReply`, and `resolveReviewThread`. For a mutation
the hook looks the thread up and refuses one that belongs to another change
request. The exact forms are in `cc-resolve-comments`.

## How the hook stays in force

- The hook file and its policy are written to a directory outside the agent's
  workspace and `TMPDIR`, read-only, and removed when the dispatch ends; the
  runtime passes it with `--settings`, so the agent cannot edit it.
- The hook only ever denies. An allowed command gets no decision, so
  `--allowedTools` stays the first barrier and the user's own deny rules still
  apply; a `deny` from this hook is not overridden by another hook's `allow`.
- Before each Claude dispatch the runtime refuses to start, `BLOCKED`, when the
  loaded configuration could defeat the hook: `disableAllHooks`, managed
  `allowManagedHooksOnly`, or an enabled plugin whose hooks handle
  `tool.check`. Claude Code's documentation says such a mod can approve a call a
  `PreToolUse` hook blocked, and the CLI has no flag that disables mods while
  keeping the user's other hooks (`--safe-mode` and `--bare` drop RTK's too),
  so the stage names the mod instead of running with it.
- The user's global configuration otherwise still loads, so hooks such as the
  RTK rewrite keep working.
- The hook also sees the file-editing tools (`Edit`, `Write`, `MultiEdit`,
  `NotebookEdit`). A non-writing stage edits only its own helper files in or
  below `$TMPDIR`: `pr-comment.md`, `report.md`, `reply.md` and `<check>.log`.
  Its `--allowedTools` names those helpers directly and under Claude's
  `claude-<uid>` subdirectory, which the executor may provide. The hook checks
  both names and resolved paths, and refuses a helper that resolves outside
  that subdirectory or the dispatch's `$TMPDIR`.
  The hook also allows the exact `printenv TMPDIR` query when a dispatch has
  created its temporary directory, so the agent can learn the path without
  exposing other environment variables.
  No stage
  edits a `.git` directory or file, `~/.gitconfig` or
  `$XDG_CONFIG_HOME/git`: a `core.fsmonitor` or `diff.external` setting would
  make an allowed `git status` or `git diff` run a program. Symlinks and `..`
  are resolved before the check.
- A non-writing stage runs with an explicit `--permission-mode default`, so the
  user's `permissions.defaultMode` (`auto`, `acceptEdits`) cannot approve an
  edit for it. Writing stages run with `acceptEdits`.

## What the table covers, and what it does not

The table and the hook protect the cycle's own Bash commands and file edits:
no force-push, merge, branch or ref deletion, push to the base branch, or
reviewer write beyond its comment, in any spelling the hook knows.

They do not cover the other tools the user's configuration pre-approves, such
as MCP tools, plugin tools and other non-Bash tools in `permissions.allow`.
Those run under the user's own configuration, as they do in an interactive
session. The cycle cannot know in advance which tools a user installs, so it
keeps no allow list of them. Before each Claude stage on a GitHub cycle it reads
the configuration the dispatch loads (managed, user and project settings) and
names those tools once in the stage report:

```text
outside the stage table, pre-approved by your configuration: mcp__…__ctx_execute can run code or write files, worth a thought when the cycle runs unattended; also mcp__…__query-docs (the table governs this cycle's Bash commands and file edits)
```

The stage keeps running; the notice is never a stop or a question. A tool is
flagged only when its name suggests it runs code or writes files (`execute`,
`run`, `shell`, `write`, …). That is a hint from the name, not an analysis of
the tool. Flagged tools come first. The notice is at most 600 characters: a
list that does not fit ends in `and N more` rather than being cut, so the flag
is never lost. A read-only tool, such as a documentation lookup or a symbol
search, is listed without a flag. A server-wide approval, `mcp__<server>` or
`mcp__<server>__*`, approves every tool of that server without naming any, so
it is always flagged, as `mcp__<server> (every tool of that server)`; so is
any other MCP rule with a `*`. A managed `permissions.defaultMode` of `auto` or
`bypassPermissions` is named as well. A user or project `defaultMode` is not,
because the dispatch passes its own `--permission-mode`.

That is reasonable in supervised use: the tools are the ones the user chose
for their own sessions, and someone reads the stage report. Reconsider when a
pre-approved tool can run code or write files and the cycle runs unattended,
for example by disabling that server or plugin for cycle runs. Any agent that
runs code from the change under review can be steered by that code. The table
is not a sandbox against hostile code, and nothing here makes it one.

## Reviewers verify in a confined clone

Review and rereview never get commit, push or a change-request write beyond
their comment. When they may verify, they do it only in a disposable clone:

- **Claude** runs in its detached clone with Claude Code's Bash sandbox passed
  by `--settings`: `failIfUnavailable`, no unsandboxed fallback, and
  `filesystem.allowWrite` limited to the clone and the dispatch's `TMPDIR`. Its
  `network.allowedDomains` holds only the code hosts (`github.com`,
  `api.github.com`, `bitbucket.org`, `api.bitbucket.org`), the same hosts the
  Codex review profile allows, so `gh` can read the change request and publish
  the comment. That route is open to any sandboxed command, including the code
  under review that a verification runs, and `gh`'s credentials are readable:
  "publishes only its comment" holds for the commands the agent types, not for
  what its test runner executes. A user or project setting that would widen it (`allowWrite`, `excludedCommands`,
  `allowUnsandboxedCommands`, Unix sockets, `enableWeakerNestedSandbox`) stops
  the stage `BLOCKED`, because Claude merges such arrays across sources. A
  sandbox that cannot start fails the stage as an environment restriction.
  On Linux and WSL2 Claude Code's sandbox needs `bubblewrap` and `socat`
  (`sudo apt install bubblewrap socat` on Debian and Ubuntu, `sudo dnf install
  bubblewrap socat` on Fedora); macOS uses its built-in Seatbelt and needs
  nothing extra. Binaries outside `PATH` are set with the managed settings
  `sandbox.bwrapPath` and `sandbox.socatPath`. `/sandbox` inside Claude Code
  shows what is missing. This applies only to a Claude review or rereview that
  may verify, in a repository with `allowed_commands` configured; other stages
  do not start the sandbox.
- **Codex** reviewers now get a disposable clone too, under a profile extending
  `:read-only` whose only writable paths are the clone and `TMPDIR`. Before the
  first such review the runtime proves it with the installed CLI
  (`codex sandbox`): the clone and `TMPDIR` must be writable, and a sibling
  `code-cycle-tmp-*`, the clone's parent through `..`, and a symlink out of the
  clone must not. If that cannot be shown, the review stops `BLOCKED`.

The clone and its `TMPDIR` are removed when the review ends. The review still
publishes its comment with the user's `gh` credentials; no further secret is
passed to it.

## When a command is denied

The stage report lists each denied command (`blocked command: …`), read from
Claude's `permission_denials` and from the hook's own denial in the stream, and
the cycle's stop line names the first: `blocked command in the implement stage:
\`npm test\``. Grant it once in `allowed_commands`, then resume with
`--continue`. A denied `docker compose …` in a stage that then reports
`BLOCKED` becomes the `docker_host` environment blocker and enters the
[grant flow](routing.md) (`stop_reason=permission_required`,
`--grant docker_host=…`). The Claude scope of that grant opens only the five
compose subcommands; `docker.host_socket_access: true` pre-authorizes both
scopes. Docker access is in practice root on the host.

Codex rejections are not reported this way yet: how `codex exec` reports a
command its sandbox or execpolicy refused has not been verified.

## Limitations

- **Codex has no per-stage command table.** Its isolation is files and network
  only; it is not a security guarantee equivalent to Claude's.
- **`--ignore-rules` drops the user's `forbidden` rules** in
  `~/.codex/rules` for Codex stages, along with their allows.
- **Writing stages run with the user's credentials.** Implementation and
  resolution keep using `gh` as the user, within their table.
- **Non-Bash tools the user pre-approved** (MCP and plugin tools) stay governed
  by the user's configuration, not by the table or the hook. One that runs code
  can do anything the user can, `git push` and `.git/config` included. The
  stage report names them; see
  [What the table covers](#what-the-table-covers-and-what-it-does-not).
- **Orca, Bitbucket, Plane and Jira** cycles keep the earlier behaviour until
  their own tables exist (#265 for Orca).
- Not yet verified: whether a Codex execpolicy `allow` rule makes `codex exec`
  run a command outside its sandbox.

## Global opt-in

`cc-host-permissions` (runtime component `host_permissions.py`) manages an
**optional** global set in `~/.claude/settings.json` and
`~/.codex/rules/default.rules`, for interactive sessions only. A cycle never
needs it and never writes it.

- It allows only reading and Git queries. No verification, write, commit,
  push or pull request: those are granted per stage and per project. It is not
  the union of the stage tables.
- A prefix rule allows every option after the prefix, and the global set has
  no hook to check them. So `git diff`, `git log` and `git show` (whose
  `--output` writes any file) and `git grep` (whose `-O` runs a program) are
  left out; they still prompt. The allowed queries can read whatever the user
  can, and `gh` uses the user's credentials.
- Applying again withdraws an entry an earlier version added and this one no
  longer includes.
- It denies force-pushes, merges and the deletion of branches or remote refs.
  A prefix rule cannot see every spelling (`-uf`, a `+refspec`), so these
  denies narrow interactive sessions without the hook's guarantee.
- It preserves every hook, setting and rule already there, backs up each file
  before changing it, and adds only missing entries, so applying twice changes
  nothing.
- Claude's settings carry no markers, so the entries it created are recorded in
  `~/.config/code-cycle-toolkit/host-permissions.json`; in Codex's rules the
  block sits between marker comments and its hash is recorded.
- Reverting removes only those entries and keeps later user changes. An entry
  or block edited by hand is never overwritten or removed: it is reported as a
  conflict (exit status 3).

Check Codex's rules by hand with the installed CLI:

```bash
codex execpolicy check --rules ~/.codex/rules/default.rules -- git status
codex execpolicy check --rules ~/.codex/rules/default.rules -- git push --force origin x
codex execpolicy check --rules ~/.codex/rules/default.rules -- git push origin x
```

They print `allow`, `forbidden`, and no matched rule. Claude has no equivalent
command; `host_permissions.py validate` checks only that the settings still
parse and the managed entries are well formed.

---

[← Role workspace policy](role-workspace-policy.md) · [↑ Documentation index](README.md) · [Instrumentation internals →](instrumentation.md)
