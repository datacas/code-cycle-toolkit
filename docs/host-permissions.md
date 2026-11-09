# Host permissions

[← Documentation index](README.md)

What each dispatched stage may run, how each host enforces it, and the opt-in
global set for interactive sessions. Until #142 a dispatched stage kept its
executor's ordinary `gh` and `git` access and its prompt was the only boundary;
this page describes what replaced that, and what it still does not cover.

## Agent capability parity

This rule is mandatory for every new implementation and every review (#450).
`AGENTS.md` states it too.

The toolkit orchestrates; it is not a second permission authority. Any operation
the underlying agent can run directly, in the same environment and with the same
credentials and permissions, must also run through the toolkit.

- The toolkit may explain a risk and ask for consent. Once the user authorizes
  the operation, the toolkit adds no further veto.
- A toolkit-only policy never turns an operation the agent can run into
  `BLOCKED`, `HUMAN_INTERVENTION` or `not_verified`. It never skips or
  downgrades a check that can run because the table does not recognize its
  command.
- Permissions, decisions, commands and outcomes are recorded. Recording never
  changes what the agent can do.
- A real limit of the agent, the operating system, the provider or the
  infrastructure is reported, and the toolkit looks for an alternative. User
  consent does not remove a real limit or an authorization boundary the user
  defined.
- The same rule applies to Claude, Codex and any future agent, according to
  what each one can really do.

Every restriction on this page is either a **real limit**, a **role contract**
(what a stage is for, such as a review that does not edit the code it judges)
or a **toolkit veto** still to be converted to disclosure, consent and a
record. The [capability parity audit](capability-parity-audit.md) classifies
each one. It links the issue that converts each veto and names the
direct-agent baseline its parity test uses. A change that adds a toolkit-only
veto is a review finding.

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

The table is the default. Explicit `code_cycle.user_choices` extend executable,
command and integration access without another confirmation; unknown names are
not a reason to block. Direct Git, code-host publication and Docker invocations
still pass their guards. Selecting a wrapper, interpreter, arbitrary-code command
or MCP integration trusts its nested operations; the command hook cannot enforce
publication boundaries inside it. For authorized source reads, an unavailable preferred search tool can
fall back to `git grep` or `grep`. See [project choices and preparation](dependency-preparation.md).

| Stage | Reads | Writes and publication |
|---|---|---|
| every stage | `git status`, `diff`, `log`, `rev-parse`, `show`, `branch --show-current`; `gh issue view`, `pr view`, `pr checks`, `auth status`, `repo view`; `gh api user --jq .login`; `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `find`, `jq` | — |
| `issue_review` | `gh issue list`, `gh pr list`, `gh search issues\|prs`; `git merge-base`, `ls-files`, `grep`, `blame` | none |
| `review`, `rereview` | `gh pr diff`, `git merge-base`, `ls-files`, `grep`, `blame`; `git fetch origin` only when the workspace has an `origin` remote (Claude's review clones have none) | `gh pr comment` and `gh pr review --comment` on the cycle's change request |
| `implement` | as review, plus `git worktree list`, `gh pr list`, `gh run view\|list` | `git add`, `commit`, `push`, `fetch`, `checkout`, `switch`, `pull`, `rm`, `mv`; `gh pr create`, `gh pr edit` and `gh pr comment` on the branch's change request; `gh issue comment` on the work item |
| `resolve` | as implement; literal `gh api repos/<configured-owner>/<configured-repo>/check-runs/<numeric-id>/annotations` with only `--jq` and `--paginate` | as implement without `gh pr create`/`edit`; and exactly three `gh api graphql` calls for review threads |

In resolve, `gh run view` requires a numeric run ID. Every `--job`/`-j`
must name a job belonging to that run. Reading `--log`, `--log-failed` or
check-run annotations requires an API lookup proving the run's `head_sha`
matches the change request's current head. Missing or unverifiable metadata
denies the read. Annotations use GET only: method overrides, fields, input
files, placeholder paths and a leading slash are refused. Implement retains
its existing run-view behavior. Output is untrusted data from the change.

Plus, for implementation, resolution, review and rereview only, the repository's
`allowed_commands.test` (exact) and `allowed_commands.verify` (prefixes). Review
and rereview run them **only inside their disposable clone**. Without either key
no verification command is preauthorized; an operation outside the table asks
for consent. See [Configuration → Allowed commands](configuration.md#allowed-commands).

The hook validates arguments, not only the program, and it is fail-closed:

- simple literal commands use the table. Chaining (`&&`, `;`, `|`), heredocs,
  substitutions, other expansions and globs use shell-operation consent,
  with every visible segment checked against the concrete guards (#439);
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
- `sed`, `date` and `ps` are read-only tools only in the forms below (#464);
  any other form is the exact consent question of the rest of this page.
  `sed` must start with `-n`, `-E` or `-e` (or `-ne`, `-nE`, `-En`), may add
  `-r`, `-s`, `-z` and further `-e`, needs a file operand inside the workspace
  and a script made only of line, `$` and `/regex/` addresses with
  `p d q Q = l P n N` and `s` without the `w` and `e` flags. `-i`,
  `--in-place`, `-f`, `--file`, the `w W e r R` commands and a script that
  sed's POSIX and GNU bracket parsing read differently are refused. `date`
  prints a time: `+FORMAT`, `-u`, `-I`, `-R`, `--rfc-3339`, `-d`/`--date`;
  `-s`, `--set`, `-f`, `-r` and a positional time are refused. `ps` takes
  `-e`, `-A`, `-a`, `--no-headers`, `-o`/`--format` fields that name no
  command line (`pid`, `ppid`, `etime`, `pcpu`, `comm`, …) and `-p`; `args`,
  `command`, `cmd`, `-f` and BSD forms such as `ps aux` are refused, because
  another process's arguments can carry a credential. The hook and the
  `--allowedTools` patterns read the same sets: `sed` appears only as the
  prefixes `sed -n`, `sed -E`, `sed -e`, `sed -ne`, `sed -nE` and `sed -En`,
  and `date` and `ps` as their program prefix with the hook reading the
  arguments, as for `find` and `grep`;
- every Docker command needs the `docker_host` grant first, also with an
  environment prefix; with it, `docker compose up|down|ps|logs|build` is in the
  table and `docker compose run`, `docker run`, `docker exec` and every other
  form is an exact operation-consent question
  ([Declared Docker checks](verification.md#declared-docker-checks-and-declined-checks));
- a command with `NAME=value` prefixes is never a table entry; consent can
  cover it, and the Git, GitHub and credential guards still apply to the
  program after the prefixes. `GIT_*`, `GH_*`, `GITHUB_*`, `HOME`, `XDG_*`,
  `PATH`, `LD_*`, `DYLD_*`, `EDITOR`, `VISUAL`, `PAGER`, `LESS*`, `BROWSER`,
  `SSH_ASKPASS*` and `SSH_*COMMAND` prefixes on `git` or `gh`, in any letter
  case, are refused: they redirect the program, or choose a program it starts,
  where the guards cannot see it. A command whose program the runtime cannot
  identify after an append-style `NAME+=value` prefix or nested `rtk` retains
  its guard. Ordinary assignments before or after one `rtk` wrapper are
  recognized.

### Reusable command consent (#439)

An exact grant for a configured check also covers ordinary `NAME=value`
prefixes, one leading `rtk` (optionally `proxy`), and output redirects into the
current `$TMPDIR`, in combination. The underlying configured argv must stay
the same: a verify prefix may still have its configured extra arguments, but
a grant does not cover different arguments. Other scripts, the underlying
runner and names in `_GUARD_DEFEATING_ENV` or `_RESOLVER_UNSAFE_ENV` (including
`DOCKER_CONFIG`) are different operations. A variant using the same underlying
runner names the exact configured command in its consent question; unrelated
commands do not. Run checks as written, without prefixes or
redirects, to avoid unnecessary questions.

For a regex, shell-source or configured-check grant the generated Claude
`--allowedTools` rule uses `Bash(*)` because its prefix grammar cannot express
these families. The mandatory hook still validates every concrete command;
the broad host allow never bypasses a guard or authorizes another operation.
User-defined host deny rules and external isolation remain in force.

All path operands under the current dispatch's `$TMPDIR` become placeholders,
including relative paths resolved against the command's working directory.
Membership uses real paths after resolving symlinks and `..`. Another
dispatch's directory stays literal. A grant created in an earlier dispatch
therefore matches the corresponding path in a later one; reusing an old
absolute path does not gain this normalization. Inline code stays opaque.

An agent can include `command_pattern_proposals` in its stage result. Each
proposal has `command` (the exact observed refused command), `pattern` (a
regular expression), `proposer`, `justification`, and non-empty `positive`
and `negative` arrays of normalized example commands. This is a proposal,
never authorization. The consent record displays the complete regex, examples,
justification, role, risks and executor scope. The user chooses this cycle,
this repository, forever, or deny. A global grant never applies to review or
rereview. The user can edit a pattern through the prompt callback answer:
`{"answer":"persistent","pattern":"...","positive":["..."],"negative":["..."]}`.
An edited pattern must pass the same validation and cover the refused normalized
operation. Invalid edits and unavailable scopes (including `forever` for review)
show the reason and ask again without saving a grant. EOF leaves the original
request pending. Widening requires a new approval. The terminal prompt accepts
this JSON answer too.

Matching is `re.fullmatch` against program and arguments joined with single
spaces; temporary operands are `<TMPDIR>/relative/path`. Redirects append
their fd, operator and normalized destination. Compound shell source preserves
its exact quoting and delimiters, so a grant for a simple segment never
authorizes a pipeline or another segment. A pattern can intentionally cover
interpreters and compound source. To authorize compound source, the proposal or
edited answer must include `"compound":true`; this opt-in is stored in the
grant's operation and the question explicitly says that it authorizes every
segment of matching shell source. Existing pattern entries without this field
cover simple commands only. There is no program denylist for patterns.
The question warns that shell and interpreter internals require external
isolation. Each visible segment, including command and process substitutions,
retains the Git, publication, credential and redirect guards. A matching
pattern grants nothing for a protected command, including a forbidden push,
a credential-store read or an output redirect outside `$TMPDIR`. Static
credential guards account for `cd` options and directory-stack changes from
`pushd`/`popd` (including `-n`), also when run through `builtin` or
`command`, and `env -C`/`--chdir` in any spelling `env` accepts (combined
short options such as `-vC` or `-uNAME -C`, abbreviations such as `--c=`, and
a `-C` or `--c…` anywhere inside an `env -S` value, which counts as
unresolved), also when `env` runs behind another program word such as
`command`, `nice`, `nohup` or `timeout 5`. Tilde prefixes resolve as bash
reads them: `~` and `~/…` name the home directory, `~user/…` that user's home
directory, and `~+/…` the segment's working directory.
They compare glob and brace operands, the
value of a `name=value` word and an attached short-option value such as
`-C~/.ssh` to the protected stores without reading them. Globs match one path
component at a time, as bash expands them: `*` never crosses a `/`, a bracket
expression matches one character, and a leading `*` or `?` does not match a
dotfile unless the source mentions `dotglob` or `GLOBIGNORE`, or the inherited
`BASHOPTS` or `GLOBIGNORE` enables it. A `..` after a glob, inside braces, or a
brace alternative holding `/` or `~` may lead to any directory. Glob
characters in the working directory itself are literal. Since a directory
change may fail, be conditional or run in a subshell, analysis retains earlier
possible working directories when checking relative paths. A guard that fails
unexpectedly refuses.

A directory change the hook cannot resolve is a `cd`/`pushd`/`env -C` target
with a variable, substitution or glob, a `pushd`/`popd` without a known stack,
any relative target while `CDPATH` is set in the environment or mentioned in
the source, a bare `cd`, `~` or home target when the source names `HOME`
other than as an expansion (`HOME=`, `read HOME`, `printf -v HOME`) or uses a
builtin that may assign a quoted or computed name (`declare`, `export`, `read`,
`printf`, …), `cd -` when it names `OLDPWD`, a `~-`, `~N`, `~+N` or `~-N`
prefix (the previous directory or a directory-stack entry) on any operand or
target, `~+` when the source may assign `PWD` in the same way as `HOME`,
and anything after `eval`, `source`, `.`,
`trap` or `alias` (also through `builtin` or `command`). The unknown directory
may be any directory, so after one a relative operand (or `name=value` value,
or redirect target) receives no grant when it may spell the rest of a store's
path from a parent (such as `auth.json`, `.codex/*`, `.ssh/x` or `*`), when it
names a store's parent below the home directory (`.codex`, `.claude`,
`.config`), when its last part looks like a key file at any depth (`id_*`,
`*_rsa`, `*_ed25519`, `*_key`, `*.pem`, `*.key`, `known_hosts`,
`authorized_keys`, `credentials`, or a glob with at least two literal
characters that may match one, such as `*rsa`), when it is a bare wildcard,
brace, `config` or `hosts.yml` that stands alone or below a part that may name
a store directory (`*`, `config`, `.s?h/*`) or directly below a leading
`cache` (`cache/*`, but not `build/cache/*`), or when it holds a token cache
(`sso/cache`, `cli/cache`). What follows a `..` may start anywhere. Use an
explicit absolute path so the guard can inspect it. Every other operand, such
as `tests/cli/test_main.py` or `build/*`, gets the ordinary consent question,
which warns that the directory cannot be resolved; the refusal request carries
`"unresolved_directory": true`. This name check is a static best effort that
judges each word by its spelling alone. An operand that is itself a variable or
substitution, a directory-wide operand such as `.`, or a store subdirectory
named on its own (`sso`) gets the question rather than a refusal, and external
isolation still denies the stores. A subcommand word that is also a store file
name, such as `config` in `git config`, a glob such as `build/*/*`, or an
unusual glob that may match a key name (`lib/*ts`) is still refused; use an
explicit absolute directory instead of the unresolved one.

Validation rejects an empty, invalid or empty-matching regex, nested
repetitions, repeated alternatives, backreferences and lookarounds. Patterns
are limited to 1024 characters and commands to 4096. Positive examples must
match and negative examples must not match before saving. Matching runs in a
separate process with a 500 ms budget to bound regex backtracking. A failed
match grants nothing; ordinary exact-command consent remains available.

Pattern grants use `ConsentStore` schema 2 alongside existing exact grants;
old schema 1 and 2 entries stay readable without a user migration. The store
records proposer, justification, examples, user approval and observed completed
commands. List and revoke them with the same `consent_control.py` selectors
used for exact grants. Cycle, repository and global records retain their own
revocation history.

Hook refusal messages carry `[layer=toolkit_hook]`. The journal records the
layer separately from the rule, including Claude permission-layer and Codex
sandbox refusals. An unlabelled refusal comes from the host permission layer;
the prompt note explains this split. A toolkit grant does not grant host or OS
access. Those real limits remain visible in verification results.

The hook is registered with `"onFailure": "block"` (Claude Code 2.1.295 or
later). Without it a `PreToolUse` hook that fails or reaches its 60-second
timeout lets the call through the normal permission flow; with it, a failed or
cancelled hook refuses the call.

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
- The `review`, `rereview` and `resolve` command tables allow the installed
  `validate-triage-comment.py` only for `pr-comment.md` directly in the
  dispatch's `$TMPDIR` or its `claude-<uid>` child. The hook checks the script,
  argument count, helper name and resolved path; other scripts and paths stay
  denied. The runtime resolves the validator and the permission view's script
  once, from the installed runtime, and writes that directory into `policy.json`
  as `runtime_dir`; the hook runs a copy of its module in a temporary directory
  and never derives these paths from its own location. Review and rereview validate the staged comment before publishing.
  The hook also allows the exact `printenv TMPDIR` query when a dispatch has
  created its temporary directory, so the agent can learn the path without
  exposing other environment variables.
  It also allows exactly one read-only query of the permission decisions in
  force, with the store, repository and branch fixed by the runtime and no
  argument of the stage's own; it grants nothing
  ([Telemetry → Permission decisions in force](telemetry.md#permission-decisions-in-force)).
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
  `network.allowedDomains` holds the code hosts (`github.com`,
  `api.github.com`, `bitbucket.org`, `api.bitbucket.org`) plus explicitly selected
  registry domains, the same hosts the
  Codex review profile allows, so `gh` can read the change request and publish
  the comment. That route is open to any sandboxed command, including the code
  under review that a verification runs, and `gh`'s credentials are readable:
  "publishes only its comment" holds for the commands the agent types, not for
  what its test runner executes. A user or project setting that would widen it (`allowWrite`, `excludedCommands`,
  `allowUnsandboxedCommands`, Unix sockets, `enableWeakerNestedSandbox`) stops
  the stage `BLOCKED`, because Claude merges such arrays across sources. A
  sandbox that cannot start is recorded as unavailable. If a failed Bash tool
  result consists of bubblewrap's `Can't find source path` startup error alone
  (text quoted inside the output of a command that ran does not count), the
  runtime journals the affected path as a `sandbox_startup_failure` refusal of
  component `claude_sandbox`, with the missing protected source and Claude
  Code's lack of a supported runtime-owned mount as its cause, shows a stage
  warning, and forces the stage checks to `not_verified` even
  if the agent reports success. On Linux/WSL2 the
  runtime first probes the required isolation. If unavailable, it retains the
  disposable clone and command/edit/publication guards for a static review;
  verification, arbitrary-code tool choices and Docker commands are denied,
  and affected checks are `not_verified`, never passed. No repository code runs
  unconfined. Writing stages whose registry choices or active GitHub publication
  policy require this sandbox may continue edits, but static-only mode denies
  publication as well as verification. It supplies no publication helper and
  denies `git push`; completion cannot claim a confirmed sandbox preflight.
  Retain an unpublished commit and resume the same stage and worktree after
  restoring isolation. A writer that cannot publish cannot complete its stage.
  If even the existing
  static boundary cannot be preserved, the stage remains `BLOCKED`.
  On Linux and WSL2 Claude Code's sandbox needs `bubblewrap` and `socat`
  (`sudo apt install bubblewrap socat` on Debian and Ubuntu, `sudo dnf install
  bubblewrap socat` on Fedora); macOS uses its built-in Seatbelt and needs
  nothing extra. Binaries outside `PATH` are set with the managed settings
  `sandbox.bwrapPath` and `sandbox.socatPath`. `/sandbox` inside Claude Code
  shows what is missing. This applies to Claude reviews or rereviews that may
  verify with configured `allowed_commands`, stages with registry choices, and
  every active GitHub `implement`/`resolve` publication policy, even without
  dependency preparation. Writers also receive their scoped Git write directories.
  Their Bash tests run in that same sandbox: network is limited to code-host
  and selected registry domains, with no Unix-socket or local-binding allowance
  for Docker or localhost services. Use the documented environment-recovery
  process for checks that cannot run; do not execute them outside confinement.
- **Codex** reviewers now get a disposable clone too, under a profile extending
  `:read-only` whose only writable paths are the clone and `TMPDIR`. Before the
  first such review the runtime proves it with the installed CLI
  (`codex sandbox`): the clone and `TMPDIR` must be writable, and a sibling
  `code-cycle-tmp-*`, the clone's parent through `..`, and a symlink out of the
  clone must not. If that cannot be shown, the review stops `BLOCKED`.

The clone and its `TMPDIR` are removed when the review ends. The review still
publishes its comment with the user's `gh` credentials; no further secret is
passed to it.

Native writing/review stages prepare Node dependencies in a separate restricted
sandbox before the agent starts. Preparation has registry-only networking,
an install with lifecycle scripts disabled, a separate contained lifecycle build
only when one is required, persistent evidence and bounded retries; it never
inherits another revision's `node_modules`. Non-publishing Codex stages receive
no default code-host network route, because readable credentials would permit
writes as well as reads. Codex issue review, by the owner's #279 decision,
receives `github.com` and `api.github.com`; #294 denies its stored gh
credentials unless the project explicitly opts out (see
[dependency preparation](dependency-preparation.md#explicit-user-choices)); its
command table still denies publication. Explicitly selected registry domains do
not add code-host domains.

Every Codex stage and preparation sandbox denies credential-store reads:
`~/.ssh`, `~/.aws`, `~/.codex/auth.json`, `~/.claude/.credentials.json`, and
`~/.claude.json`. Only publishing stages retain `~/.config/gh` access by
default. Codex uses `none` filesystem rules; Claude and preparation use
`denyRead`. Claude adds these denials where its Bash sandbox already applies:
an active GitHub policy with confined review verification, registry domains,
publication commands or a Docker grant. Inactive policies retain their normal
tooling; adding credential denials does not enable a sandbox for them. The same
condition selects sandbox configuration checks and the Linux functional probe.
Unavailable isolation selects static-only recovery with verification disabled;
this does not establish credential confinement. Existing paths resolve to their
real targets before rules are
emitted; missing paths are omitted. An explicit per-project
`code_cycle.user_choices.credential_reads` list selects exceptions and is
reported in dispatch and preparation evidence. See
[credential-read exceptions](dependency-preparation.md#credential-read-exceptions).
Issue review keeps the `~/.config/gh` denial: the runtime reads a GitHub work
item with its own credentials and passes the stage a read-only
[issue snapshot](provider-contract.md#runtime-issue-snapshot), so no stage gains
credential access to read it. For an issue-review stop that still names a
denied GitHub credential-store read, use the
[troubleshooting guidance](review-cycle.md#github-credential-store-denied-during-issue-review).

## When a command is denied

Operations outside the table, reads outside the worktree and forbidden edits
enter the existing `ConsentStore` flow. The runtime asks one question at a time
with the exact options **this cycle / this repository / forever / deny**.
Unknown and risky operations are questions too. The record includes the full
command or edit target, stage role, reason, exact matching pattern, risk, pros,
cons and alternative. Until answered, an essential request is a
`permission_required` stop, never an agent's terminal `BLOCKED` solely for
lacking an answer.

An operation ID is `command_<sha256>`, computed from its structured pattern and
stage role; `review` and `rereview` share one role key, and a stored `rereview`
decision still matches. Exact grants use the configured-check variant rule
above, or otherwise exact matching except for verified per-dispatch
paths: TMPDIR operands of every program, the values of environment prefixes
that lie in TMPDIR or the checkout, and Docker arguments that do. Membership is
checked after resolving symlinks and `..`. A Docker operation also carries its
directory relative to the checkout, a Compose one the digest and warnings of its
effective configuration, and a configured check its check identity
([Declared Docker checks](verification.md#declared-docker-checks-and-declined-checks)).
Exact grants preserve interpreter code, Git destinations, sensitive paths,
flags and actions. Separately approved regex grants match the normalized
command family described above and retain the same concrete guards.
An allow applies to matching operations in later dispatches and retries of the
selected scope. **This cycle** is keyed by cycle ID and can be reloaded for that
ID; **this repository** persists across issues and resumed runs; **forever**
persists globally for writing and issue-review roles. Different stage roles
require their own decisions. An answer can be supplied on resume as
`--grant command_<sha256>=cycle|persistent|forever|deny`.
A global grant never covers `review` or `rereview`, because those stages judge
untrusted changes in any repository: their questions offer only **this cycle /
this repository / deny**, a `forever` answer for them is refused, and a stored
global entry for those roles is ignored. A writing-stage grant never authorizes
a review either; a reviewer must be shown and explicitly authorize its own
role's pattern.

**Deny** is stored too. The stage skips the operation and reports affected
checks as `not_verified`, never passed. Its structured `permission_effects`
entries give `command`, `essential` and `check_ids`. Only an essential denied
operation stops the cycle with `HUMAN_INTERVENTION` and the named decision. A
configured check is never essential: an unanswered one is still asked, and a
denied one is recorded `unverified` while the cycle continues to
`VERIFICATION_INCOMPLETE`.
An accessory inspection does not stop completed work; a stage reporting
`BLOCKED` can supply `completed_status` and completion references, which still
pass the normal publication/effect guards. Agent prose is not completion
evidence. For `implement` and `resolve` the runtime classifies every refusal
itself and completes the stage only when the remote head, the local HEAD and
the PR head agree: see
[a publishing stage that reported `BLOCKED`](role-workspace-policy.md#a-publishing-stage-that-reported-blocked-after-a-verified-publication-364).

### Detached and unattended runs

<a id="detached-and-unattended-runs"></a>

A run with no human channel, `--detach` or no terminal on stdin and stderr, has
nobody to answer a question. The policy applies to every hook command-table
refusal in such a run, not only `--detach` (#464):

1. **A saved grant applies first.** An answer stored by #406 (`--grant`, this
   cycle, this repository, forever) is read before anything else.
2. **Otherwise the refusal is a denial that grants nothing.** The runtime
   records it for every refused command with its command, stage, role, rule,
   reason and runtime class (`denial_classification`), and as a pending
   decision (`decision_asked`, component `unattended_denial`, status
   `unanswered`). The decisions record of the cycle lists it afterwards, and
   `--grant command_<sha256>=…` answers it on the next run.
3. **An incidental denial continues.** A stage whose work is done, whose
   publication is verified and whose refusals were only inspection or
   redundant keeps its status. The checks it could not run stay `not_verified`.
4. **An essential denial stops the cycle** with `HUMAN_INTERVENTION`
   (`stop_reason=permission_required`), naming the command, the pending
   decision and the `--grant` that answers it. `docker_host` and
   configured-check consent keep `permission_required`; they are persisted as
   pending and the cycle never waits for them.
5. **Sandbox limits are unchanged.** A missing socket, `bubblewrap` or network
   is a real limit of the host, reported as before; this policy does not grant
   it.

An attended run asks as before, and records no `unattended_denial`.

**The cause of a `BLOCKED` stage.** A refused command is not assumed to be why
a stage ended. The stop names the cause the runtime verified, apart from the
refusals: the missing completion evidence, a failed publication check, or the
essential refused operation with its command, rule and class. The incidental
refusals of that stage are listed as warnings. A publishing stage that reports
`BLOCKED` without a `completed_status` is judged on its structured result: a
full head, the change request, planned work units and tests that ran for
`implement`, or finding outcomes for `resolve`. With that evidence and a
verified publication the runtime derives `IMPLEMENTED` or `RESOLVED` itself and
records `inferred: true`; without it the stage stays `BLOCKED`. A result that
carries `questions` is waiting for an answer from the user, so it is never
inferred done and never writes a resume point.

**Resume without `--from` and `--pr`.** When a publishing stage's result proves
its work done, the runtime writes a *resume point* to the cycle journal
(`resume_point`, with the change request, the full head, the branch and the
next stage), whether the stage completed or ended `BLOCKED` with no refused
operation it needed. Running the same work item again reads it: `implement`
resumes at `review` and `resolve` at `rereview`. It is used only when the
earlier run recorded its stop, no later stage began, the persisted state is
consistent, and the change request is open at the recorded head. Any mismatch
stops the run and names it. The resume is idempotent: it is recorded
(`auto_resume`, with the block reason, the recovery stage and the checks made),
a second rerun beside a run still in progress is refused, and once a stage of
the resumed run has begun nothing is left to resume. A pull request existing is
not completion. `--no-auto-resume` starts the work item over; `--from` and
`--pr` resume by hand as before.

List or revoke decisions with `consent_control.py list|revoke --repo owner/repo
--executor claude`; revoke also needs `--grant-id command_<sha256>`, with
`--global` to select a global record. The next dispatch reloads decisions so
revocation takes effect from the next stage. The consent store stays outside
the checkout; revocation retains an audit record.
Cycle entries can be selected with `--cycle-id <id> --task-id <work-item>`.
Both selectors are required together. Revoking a decision absent from the
selected scope exits with an error naming that scope; it does not revoke an
entry from another scope or report a silent success.
Resuming a work item reloads its command-consent cycle lineage; a second work
item starts a separate lineage and does not receive cycle-only decisions.

Force-push, direct pushes to base/protected branches, deletion of Git refs and
direct reads naming a protected credential store cannot be granted: publication
integrity and credential confidentiality require these boundaries. Under
[agent capability parity](#agent-capability-parity) the audit classifies these
never-grant boundaries, Git configuration edits and the refusal of `forever`
for review roles as toolkit vetoes. They are to become consent with a risk
warning and a record (rows PA-25 to PA-31 of the
[audit](capability-parity-audit.md#sensitive-operations)); until that change
ships, the behaviour below still applies. The hook
recognizes a store named as a complete argument or edit target. It does not
inspect recursive ancestor reads (`grep -r token ~`), attached option paths
(`grep -f~/.ssh/id_ed25519 ...`, `--file=...`) or opaque program internals.
Credential confidentiality for these forms requires the external sandbox's
deny-read rules; operation consent never widens those rules. Direct command consent
also retains argument validation, redirect and workspace/publication guards;
Git configuration edits remain guarded because they can turn later reads into
code execution. Foreground and verification-timeout safeguards remain in force.
Exact consent permits `git ls-remote origin ...` and a GET of
`gh api repos/<cycle-repo>/pulls/<current-pr> [--jq <filter>]`. Git execution
options, another remote, API method overrides, input fields and other PRs
remain guarded. Other Git/GitHub argument or publication guard failures are
not offered as grantable questions: an answer could not override those guards.
Read-tool validation distinguishes file operands from grep patterns, jq
filters and option data. The runtime's exact supplied evidence file is readable
outside the workspace without consent; its directory and siblings gain no access.
An interpreter (`python3 -c`, `bash -c`, `node -e`, etc.) can be granted for its
concrete command. The question warns that the hook **cannot inspect or enforce
restrictions inside that code**. Never-grant operations inside an authorized
interpreter need external isolation: sandbox rules, filesystem permissions,
restricted credentials and code-host protections. A command grant never widens
that isolation.

The stage report lists each refused command, read from Claude's
`permission_denials` and the hook's stream. A denied `docker compose …` in a stage that then reports
`BLOCKED` becomes the `docker_host` environment blocker and enters the
[grant flow](routing.md) (`stop_reason=permission_required`,
`--grant docker_host=…`). On Linux the Claude grant opens every reachable Unix
socket for that dispatch, while the command table permits only the five listed
Compose subcommands. The runtime probes `docker version` inside the sandbox;
failure leaves Docker-dependent checks `not_verified` and independent work
continues. `docker.host_socket_access: true` pre-authorizes both scopes. Docker
access is in practice root on the host.

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
