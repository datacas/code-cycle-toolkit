"""The closed command table of each native stage, and the hook that enforces it.

Until #142 a dispatched stage kept its executor's normal `gh` and `git` access,
and the publication policy in its prompt was the only boundary. This module
replaces that, for Claude on a GitHub cycle, with two barriers:

- `--allowedTools` patterns derived from the stage's table, so the executor's
  own permission layer starts from the table rather than from `Bash(gh:*)`;
- a `PreToolUse` hook that the runtime adds to every Claude dispatch. It parses
  each Bash command, its arguments, flags and refspecs, and denies anything the
  stage's table does not contain. Claude Code 2.1.302 lets a hook's `deny`
  prevail over another hook's `allow`, so a permissive global rule or the RTK
  rewrite hook cannot reopen what this one closes.

The parser is fail-closed. It accepts one simple command: words, single and
double quotes, `$TMPDIR` and one output redirection into the dispatch's own
temporary directory. Chaining, pipes, substitutions, other expansions, globs and
anything it cannot read are refused. Allowing a command it does not understand
would make the table a suggestion.

Codex gets no table: in Codex 0.163.0 permission profiles do not filter
commands and execpolicy rules load only from configuration layers. Its stages
are confined by their sandbox profile and run with `--ignore-rules` instead.

The table applies only when both the code host and the issue provider are
GitHub. Other providers' tooling is not in it, and the hook would deny it, so
those cycles keep the earlier behaviour and their dispatch report says so.

Run as a hook: `python3 stage_permissions.py hook <policy.json>`, reading the
hook event on stdin. The runtime copies this file and the policy into a
directory outside the agent's workspace and temporary directory.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

WRITING_ROLES = frozenset({"implement", "resolve"})
REVIEW_ROLES = frozenset({"review", "rereview"})
ISSUE_REVIEW_ROLE = "issue_review"

#: Read-only shell tools every stage may use, and nothing else from the shell.
READ_SHELL = frozenset({"ls", "cat", "head", "tail", "wc", "grep", "find", "jq"})
#: `find` primaries that run a command, delete, or write a file.
FIND_FORBIDDEN = frozenset({
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fprint", "-fprint0", "-fprintf", "-fls",
})

GIT_COMMON_READ = frozenset({"status", "diff", "log", "rev-parse", "show"})
GIT_REVIEW_READ = frozenset({"merge-base", "ls-files", "grep", "blame"})
#: Stages that search the repository with `GIT_REVIEW_READ`. The hook and the
#: `--allowedTools` patterns both read this set, so the two barriers agree.
GIT_SEARCH_ROLES = WRITING_ROLES | REVIEW_ROLES | {ISSUE_REVIEW_ROLE}
GIT_WRITE = frozenset({"add", "commit", "push", "fetch", "checkout", "switch", "pull",
                       "rm", "mv"})
#: Options that make Git run another program, on any subcommand. Git also
#: accepts any unambiguous prefix of a long option, so a prefix is refused too.
GIT_EXECUTING_OPTIONS = ("--upload-pack", "--receive-pack", "--exec", "--exec-path",
                         "--open-files-in-pager")

DOCKER_COMPOSE_SUBCOMMANDS = frozenset({"up", "down", "ps", "logs", "build"})
DOCKER_COMPOSE_GLOBAL = {"-f": True, "--file": True, "-p": True, "--project-name": True,
                         "--profile": True, "--env-file": True,
                         "--project-directory": True, "--ansi": True, "--progress": True}

#: Interpreters and launchers whose bare form, or inline-code option, would turn
#: a verification prefix into arbitrary execution.
LAUNCHERS = frozenset({
    "python", "python3", "py", "node", "deno", "bun", "ruby", "perl", "php",
    "bash", "sh", "zsh", "dash", "fish", "pwsh", "powershell", "cmd",
    "npx", "pnpx", "bunx", "uv", "uvx", "poetry", "pipx", "env", "xargs",
    "sudo", "doas", "eval", "exec", "nohup", "time", "watch", "make", "npm",
    "pnpm", "yarn", "cargo", "go", "dotnet", "java", "gradle", "mvn",
})
#: Launcher subcommands that run whatever follows them (`npm exec`, `uv run`).
RUNNER_SUBCOMMANDS = frozenset({"run", "exec", "x", "dlx"})
SHELL_LAUNCHERS = frozenset({"bash", "sh", "zsh", "dash", "fish", "pwsh", "powershell",
                             "cmd", "env", "xargs", "sudo", "doas", "eval", "exec",
                             "nohup", "time", "watch"})

THREAD_QUERY_FIELDS = frozenset({"first", "after"})
THREAD_MUTATIONS = {
    "addPullRequestReviewThreadReply": {"pullRequestReviewThreadId", "body"},
    "resolveReviewThread": {"threadId"},
}

#: In every denial reason, so the runtime can tell this hook's denials apart.
HOOK_DENIAL_MARKER = "stage policy denied this command"

DENY_ADVICE = ("If this stage needs that command, do not work around the policy: "
               "stop BLOCKED and name the exact command so a person can grant it once.")
EDIT_DENY_ADVICE = ("No grant lifts this rule: finish without the edit, or stop BLOCKED "
                    "and say which file the stage needed to change.")


class PolicyError(ValueError):
    """A command, or a configured command, the policy refuses."""


@dataclass(frozen=True)
class StagePolicy:
    """What one dispatched stage may run through Bash, and in which context.

    The repository, change request and work item scope the publishing `gh`
    commands; the base branch is the push target no stage may write. `tmpdir`
    and `rtk` are filled in at dispatch time, when they are known.
    """

    role: str
    repo: str | None = None
    change_request_id: str | None = None
    work_item_id: str | None = None
    base_branch: str | None = None
    test_command: str | None = None
    verify_prefixes: tuple[str, ...] = ()
    code_host: str | None = "github"
    issue_provider: str | None = "github"
    docker: bool = False
    rtk: bool = False
    tmpdir: str | None = None
    fetch_origin: bool = True

    @property
    def active(self) -> bool:
        return self.code_host == "github" and self.issue_provider == "github"

    @property
    def inactive_reason(self) -> str | None:
        if self.active:
            return None
        return ("the per-stage command table covers GitHub only "
                f"(code_host={self.code_host or 'unknown'}, "
                f"issue_provider={self.issue_provider or 'unknown'}); "
                "this dispatch keeps the executor's normal tooling")

    @property
    def pr_number(self) -> str | None:
        return _number(self.change_request_id, ("/pull/", "/pull-requests/", "#"))

    @property
    def issue_number(self) -> str | None:
        return _number(self.work_item_id, ("/issues/", "#"))

    @property
    def verification_commands(self) -> tuple[str, ...]:
        """The configured test and verify commands this role receives."""
        if self.role not in WRITING_ROLES | REVIEW_ROLES:
            return ()
        commands = (self.test_command,) if self.test_command else ()
        return commands + tuple(self.verify_prefixes)

    @property
    def requires_confined_verification(self) -> bool:
        """A reviewer that may run tests must do so in a confined clone."""
        return self.role in REVIEW_ROLES and bool(self.verification_commands)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "StagePolicy":
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("role"), str):
            raise PolicyError("not a stage policy")
        known = {name for name in cls.__dataclass_fields__}
        unknown = sorted(set(data) - known)
        if unknown:
            raise PolicyError(f"unknown stage policy keys: {', '.join(unknown)}")
        data["verify_prefixes"] = tuple(data.get("verify_prefixes") or ())
        return cls(**data)

    def at_dispatch(self, *, tmpdir: str | None, rtk: bool, fetch_origin: bool,
                    docker: bool) -> "StagePolicy":
        return replace(self, tmpdir=tmpdir, rtk=rtk, fetch_origin=fetch_origin,
                       docker=docker)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    #: The denial would be lifted by the `docker_host` grant.
    needs_docker: bool = False


@dataclass
class ParsedCommand:
    words: list[str] = field(default_factory=list)
    #: `(fd, operator, target)`; `target` is None for `2>&1`.
    redirects: list[tuple[str, str, str | None]] = field(default_factory=list)


def _number(reference: str | None, markers: tuple[str, ...]) -> str | None:
    if not reference:
        return None
    reference = str(reference).strip()
    if reference.isdecimal():
        return reference
    for marker in markers:
        if marker in reference:
            tail = reference.rsplit(marker, 1)[-1]
            if tail.isdecimal():
                return tail
    return None


# --- parsing -----------------------------------------------------------------

_OPERATORS = set(";&|<>()`")
_GLOB = set("*?[]{}")


def parse_command(command: str, tmpdir: str | None) -> ParsedCommand:
    """Split one simple shell command into literal words, or refuse it.

    Only `$TMPDIR` and `${TMPDIR}` expand, to the dispatch's directory; every
    other expansion, glob, operator and control character is refused, so each
    word the validator sees is exactly the word the shell will pass.
    """
    if not isinstance(command, str) or not command.strip():
        raise PolicyError("empty command")
    if re.search(r"[\x00-\x08\x0a-\x1f\x7f]", command):
        raise PolicyError("control characters and newlines are not allowed")
    parsed = ParsedCommand()
    word: list[str] = []
    started = quoted = False
    pending: tuple[str, str] | None = None
    i, n = 0, len(command)

    def expand(position: int) -> tuple[str, int]:
        for spelling in ("${TMPDIR}", "$TMPDIR"):
            if command.startswith(spelling, position):
                end = position + len(spelling)
                if spelling == "$TMPDIR" and end < n and (command[end].isalnum()
                                                         or command[end] == "_"):
                    break
                if not tmpdir:
                    raise PolicyError("$TMPDIR is not a runtime temporary directory here")
                return tmpdir, end
        raise PolicyError("only $TMPDIR may be expanded")

    def finish() -> None:
        nonlocal word, started, quoted, pending
        if not started:
            return
        text = "".join(word)
        if pending is not None:
            parsed.redirects.append((pending[0], pending[1], text))
            pending = None
        else:
            parsed.words.append(text)
        word, started, quoted = [], False, False

    while i < n:
        char = command[i]
        if char in " \t":
            finish()
            i += 1
        elif char == "'":
            end = command.find("'", i + 1)
            if end < 0:
                raise PolicyError("unterminated quote")
            word.append(command[i + 1:end])
            started = quoted = True
            i = end + 1
        elif char == '"':
            i += 1
            started = quoted = True
            while True:
                if i >= n:
                    raise PolicyError("unterminated quote")
                inner = command[i]
                if inner == '"':
                    i += 1
                    break
                if inner == "\\" and i + 1 < n and command[i + 1] in '$`"\\':
                    word.append(command[i + 1])
                    i += 2
                elif inner == "$":
                    value, i = expand(i)
                    word.append(value)
                elif inner == "`":
                    raise PolicyError("command substitution is not allowed")
                else:
                    word.append(inner)
                    i += 1
        elif char == "\\":
            if i + 1 >= n:
                raise PolicyError("trailing backslash")
            word.append(command[i + 1])
            started = quoted = True
            i += 2
        elif char == "$":
            value, i = expand(i)
            word.append(value)
            started = True
        elif char == ">":
            fd = "1"
            if started and not quoted and "".join(word) == "2":
                fd, word, started = "2", [], False
            finish()
            if pending is not None:
                raise PolicyError("a redirection needs a target")
            operator = ">>" if command.startswith(">>", i) else ">"
            i += len(operator)
            if command.startswith("&", i):
                if fd == "2" and operator == ">" and command.startswith("&1", i) and (
                        i + 2 == n or command[i + 2] in " \t"):
                    parsed.redirects.append(("2", ">&", None))
                    i += 2
                    continue
                raise PolicyError("only 2>&1 may duplicate a descriptor")
            pending = (fd, operator)
        elif char in _OPERATORS:
            raise PolicyError(f"shell operator {char!r} is not allowed; run one simple command")
        elif char in _GLOB:
            raise PolicyError(f"unquoted {char!r} would expand; quote it")
        elif char in "~#" and not started:
            raise PolicyError(f"unquoted {char!r} at the start of a word is not allowed")
        else:
            word.append(char)
            started = True
            i += 1
    finish()
    if pending is not None:
        raise PolicyError("a redirection needs a target")
    if not parsed.words:
        raise PolicyError("no command")
    return parsed


#: The files a non-writing stage writes in its `$TMPDIR` (#272): the comment,
#: report and thread-reply bodies its skills publish, and check logs named
#: after the check. Claude keeps its own session state in the same directory
#: (materialised settings, `tasks/` output, `bash-edit-diff/`), so the grant
#: names these top-level files rather than the whole directory.
TMPDIR_HELPER_FILES = ("pr-comment.md", "report.md", "reply.md")
TMPDIR_LOG_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\.log")


def is_tmpdir_helper(path: str, tmpdir: str | None) -> bool:
    """Whether path names a stage helper in TMPDIR or Claude's own subdir.

    Both the path as written and the file it resolves to must have a helper
    name and remain in TMPDIR. The nested exception is limited to Claude's
    per-user claude-<uid> directory directly below TMPDIR.
    """
    if not inside_tmpdir(path, tmpdir):
        return False
    root = Path(tmpdir).resolve(strict=True)
    literal = Path(os.path.normpath(path))
    resolved = Path(path).resolve(strict=False)

    def owned_parent(parent: Path) -> bool:
        return (parent == root or
                (parent.parent == root and re.fullmatch(r"claude-\d+", parent.name) is not None))

    def helper_in_tmpdir(name: str, parent: Path) -> bool:
        is_helper = name in TMPDIR_HELPER_FILES or TMPDIR_LOG_FILE.fullmatch(name)
        return bool(is_helper and owned_parent(parent))

    return (helper_in_tmpdir(literal.name, literal.parent.resolve())
            and helper_in_tmpdir(resolved.name, resolved.parent))


def inside_tmpdir(path: str, tmpdir: str | None) -> bool:
    """Whether `path` resolves strictly inside the dispatch's temporary directory.

    Symlinks and `..` are resolved first, so neither can lead out of it, and a
    sibling `code-cycle-tmp-*` directory is outside it like any other path.
    """
    if not tmpdir or not path or not os.path.isabs(path):
        return False
    try:
        root = Path(tmpdir).resolve(strict=True)
        target = Path(path).resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return target != root and root in target.parents


# --- the table ---------------------------------------------------------------

def check(command: str, policy: StagePolicy, *, cwd: str | None = None,
          run=subprocess.run) -> Decision:
    """Decide whether this stage may run `command`. Never raises."""
    try:
        parsed = parse_command(command, policy.tmpdir)
        words = list(parsed.words)
        if words[0] == "rtk":
            if not policy.rtk:
                raise PolicyError("rtk is not installed for this dispatch")
            words = words[2:] if words[1:2] == ["proxy"] else words[1:]
            if not words:
                raise PolicyError("rtk needs a command")
        for _, _, target in parsed.redirects:
            if target is not None and not inside_tmpdir(target, policy.tmpdir):
                raise PolicyError("output may be redirected only into $TMPDIR")
        _check_words(words, parsed, policy, cwd=cwd, run=run)
    except _DockerDenied as denied:
        return Decision(False, str(denied), needs_docker=True)
    except PolicyError as error:
        return Decision(False, str(error))
    except Exception as error:  # fail closed on anything unexpected
        return Decision(False, f"the command could not be validated ({type(error).__name__})")
    return Decision(True, "allowed by the stage command table")


#: Claude's file-editing tools and the input key that names their target.
FILE_EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
                   "NotebookEdit": "notebook_path"}


def _git_config_paths() -> tuple[Path, ...]:
    home = Path.home()
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return home / ".gitconfig", Path(xdg) / "git"


def check_file_edit(tool_name: str, tool_input, policy: StagePolicy, *,
                    cwd: str | None = None) -> Decision:
    """Decide whether this stage may edit a file with a non-Bash tool. Never raises.

    A non-writing stage edits only inside its own `$TMPDIR`, where its skills
    write the comment body they publish with `--body-file` (#272). No stage
    edits Git's configuration, a `.git` directory or a `.git` file:
    `core.fsmonitor`, `diff.external` and the like would make an allowed
    `git status` or `git diff` run a program.
    """
    try:
        path = (tool_input or {}).get(FILE_EDIT_TOOLS[tool_name])
        if not isinstance(path, str) or not path.strip():
            raise PolicyError("the edit names no file")
        if policy.role not in WRITING_ROLES and not is_tmpdir_helper(path, policy.tmpdir):
            raise PolicyError(f"a {policy.role} stage edits only its comment, report, reply "
                              "and check-log files in $TMPDIR")
        if not os.path.isabs(path):
            if not cwd:
                raise PolicyError("a relative path needs the working directory")
            path = os.path.join(cwd, path)
        literal = Path(os.path.normpath(path))
        resolved = Path(path).resolve(strict=False)
        for candidate in (literal, resolved):
            # Windows also reaches `.git` as `.git.`, `.git ` and `.git::$INDEX_ALLOCATION`.
            if any(part.casefold().split(":", 1)[0].rstrip(" .") == ".git"
                   for part in candidate.parts):
                raise PolicyError("no stage edits a .git directory or file")
            for config in _git_config_paths():
                if candidate == config or config in candidate.parents:
                    raise PolicyError("no stage edits Git's global configuration")
    except PolicyError as error:
        return Decision(False, str(error))
    except Exception as error:  # fail closed on anything unexpected
        return Decision(False, f"the edit could not be validated ({type(error).__name__})")
    return Decision(True, "allowed by the stage edit rule")


class _DockerDenied(PolicyError):
    pass


def _check_words(words: list[str], parsed: ParsedCommand, policy: StagePolicy, *,
                 cwd: str | None, run) -> None:
    for configured in policy.verification_commands:
        expected = configured.split()
        exact = configured == policy.test_command
        if words == expected or (not exact and words[:len(expected)] == expected):
            return
    program = words[0]
    if program in READ_SHELL:
        if parsed.redirects:
            raise PolicyError(f"{program} may not redirect its output")
        if program == "find":
            for word in words[1:]:
                if word in FIND_FORBIDDEN:
                    raise PolicyError(f"find {word} runs, deletes or writes; it is not allowed")
        return
    if program == "git":
        return _check_git(words[1:], policy, cwd=cwd, run=run)
    if program == "gh":
        return _check_gh(words[1:], policy, cwd=cwd, run=run)
    if program == "docker":
        return _check_docker(words[1:], policy)
    raise PolicyError(f"{program!r} is not in the {policy.role} stage's command table")


def _check_git(args: list[str], policy: StagePolicy, *, cwd: str | None, run) -> None:
    while args and args[0] == "--no-pager":
        args = args[1:]
    if not args or args[0].startswith("-"):
        raise PolicyError("git global options other than --no-pager are not allowed")
    subcommand, rest = args[0], args[1:]
    allowed = set(GIT_COMMON_READ)
    if policy.role in GIT_SEARCH_ROLES:
        allowed |= GIT_REVIEW_READ
    if policy.role in WRITING_ROLES:
        allowed |= GIT_WRITE
    if subcommand == "branch":
        if rest != ["--show-current"]:
            raise PolicyError("git branch is allowed only as git branch --show-current")
        return
    if subcommand == "worktree" and policy.role in WRITING_ROLES:
        # The implementer inspects linked worktrees; it never creates one.
        if rest not in (["list"], ["list", "--porcelain"]):
            raise PolicyError("git worktree is allowed only as git worktree list")
        return
    if policy.role in REVIEW_ROLES and subcommand == "fetch":
        if not policy.fetch_origin:
            raise PolicyError("this review workspace has no origin remote to fetch")
        if not rest or rest[0] != "origin" or any(word.startswith("-") for word in rest):
            raise PolicyError("a review may only run git fetch origin [<ref>...]")
        return
    if subcommand not in allowed:
        raise PolicyError(f"git {subcommand} is not in the {policy.role} stage's command table")
    for word in rest:
        if (any(_names_git_option(word, option) for option in GIT_EXECUTING_OPTIONS)
                or word.startswith("-O")
                # `git grep -O` runs a pager; short options bundle, as in `-iO<cmd>`.
                or (subcommand == "grep" and word.startswith("-")
                    and not word.startswith("--") and "O" in word)):
            raise PolicyError(f"git option {word} runs another program")
    for index, word in enumerate(rest):
        if not _names_git_option(word, "--output"):
            continue
        if "=" in word:
            target = word.split("=", 1)[1]
        else:
            target = rest[index + 1] if index + 1 < len(rest) else ""
        if not inside_tmpdir(target, policy.tmpdir):
            raise PolicyError("git --output may write only into $TMPDIR")
    if subcommand == "push":
        _check_push(rest, policy, cwd=cwd, run=run)
    elif subcommand in {"fetch", "pull"}:
        remotes = [word for word in rest if not word.startswith("-")]
        if remotes and remotes[0] != "origin":
            raise PolicyError(f"git {subcommand} may use only the origin remote")


def _names_git_option(word: str, option: str) -> bool:
    """Whether `word` is the long `option`, in full or as a prefix Git expands to it."""
    name = word.split("=", 1)[0]
    return name.startswith("--") and len(name) > 2 and option.startswith(name)


def _check_push(args: list[str], policy: StagePolicy, *, cwd: str | None, run) -> None:
    positional: list[str] = []
    index = 0
    while index < len(args):
        word = args[index]
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            if name in {"--force", "--force-with-lease", "--force-if-includes", "--mirror",
                        "--delete", "--prune", "--all", "--branches", "--repo"}:
                raise PolicyError(f"git push {name} is never allowed")
            if name not in {"--set-upstream", "--no-verify", "--verify", "--dry-run",
                            "--porcelain", "--quiet", "--verbose", "--progress",
                            "--push-option", "--atomic", "--follow-tags", "--tags"}:
                raise PolicyError(f"git push option {name} is not in the table")
            if name == "--push-option" and "=" not in word:
                index += 1
        elif word.startswith("-") and word != "-":
            letters = word[1:]
            if any(letter in letters for letter in "fd"):
                raise PolicyError(f"git push {word} forces or deletes; it is never allowed")
            if any(letter not in "unqvo" for letter in letters):
                raise PolicyError(f"git push option {word} is not in the table")
            if letters.endswith("o"):
                index += 1
        else:
            positional.append(word)
        index += 1
    if not positional or positional[0] != "origin":
        raise PolicyError("git push names its remote, origin, and the branch it pushes")
    refspecs = positional[1:]
    if not refspecs:
        # Without a refspec, `push.default` or `remote.origin.push` decides the
        # destination, which could be the base branch the command never names.
        raise PolicyError("git push needs an explicit branch, such as git push origin HEAD")
    # The usual default names stay protected even when the base is another.
    protected = {"main", "master"} | ({policy.base_branch} if policy.base_branch else set())
    for refspec in refspecs:
        if refspec.startswith("+"):
            raise PolicyError("a +refspec forces the push; it is never allowed")
        source, _, destination = refspec.partition(":")
        if not source:
            raise PolicyError("an empty source deletes the remote ref; it is never allowed")
        destination = destination or source
        if destination in {"HEAD", "@"}:
            destination = _current_branch(cwd, run)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", destination) or ".." in destination:
            raise PolicyError(f"git push destination {destination!r} is not a plain branch name")
        # Git matches a short destination against the remote's refs, so
        # `heads/main` reaches refs/heads/main as surely as `main` does.
        names = {destination, destination.removeprefix("refs/heads/"),
                 destination.removeprefix("heads/")}
        if names & protected:
            raise PolicyError(f"pushing to the base branch {destination} is never allowed")


def _current_branch(cwd: str | None, run) -> str:
    try:
        completed = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd,
                        capture_output=True, text=True, timeout=10,
                        stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        raise PolicyError(f"could not read the current branch ({type(error).__name__})") from error
    branch = (completed.stdout or "").strip()
    if completed.returncode or not branch or branch == "HEAD":
        raise PolicyError("could not read the current branch to check the push target")
    return branch


# gh ---------------------------------------------------------------------------

_READ_OUTPUT = {"--json": True, "--jq": True, "-q": True, "--template": True, "-t": True}
_BODY = {"--body": True, "-b": True, "--body-file": True, "-F": True}
_LIST = {**_READ_OUTPUT, "--search": True, "-S": True, "--state": True, "-s": True,
         "--label": True, "-l": True, "--limit": True, "-L": True, "--author": True,
         "-A": True, "--assignee": True, "-a": True, "--head": True, "-H": True,
         "--base": True, "-B": True}
_SEARCH = {**_READ_OUTPUT, "--state": True, "--limit": True, "-L": True, "--label": True,
           "--match": True, "--sort": True, "--order": True, "--author": True,
           "--closed": True, "--created": True, "--updated": True, "--include-prs": False,
           "--owner": True}


@dataclass(frozen=True)
class _GhSpec:
    roles: frozenset[str]
    flags: dict
    max_positional: int | None = 1
    #: `pr`, `issue` or None: what the positional names and must match.
    scope: str | None = None
    required: tuple[str, ...] = ()


_ALL = frozenset({"implement", "resolve", "review", "rereview", ISSUE_REVIEW_ROLE,
                  "security", "bootstrap", "coordinate"})
_WRITERS = WRITING_ROLES
_READERS_OF_PRS = WRITING_ROLES | REVIEW_ROLES

GH_TABLE: dict[tuple[str, str], _GhSpec] = {
    ("auth", "status"): _GhSpec(_ALL, {"-a": False, "--active": False}, 0),
    ("repo", "view"): _GhSpec(_ALL, {**_READ_OUTPUT, "--branch": True, "-b": True}, 1),
    ("issue", "view"): _GhSpec(_ALL, {**_READ_OUTPUT, "--comments": False, "-c": False}, 1),
    ("pr", "view"): _GhSpec(_ALL, {**_READ_OUTPUT, "--comments": False, "-c": False}, 1),
    ("pr", "checks"): _GhSpec(_ALL, {**_READ_OUTPUT, "--watch": False, "--fail-fast": False,
                                     "--interval": True, "-i": True, "--required": False}, 1),
    ("issue", "list"): _GhSpec(frozenset({ISSUE_REVIEW_ROLE}), _LIST, 0),
    ("pr", "list"): _GhSpec(frozenset({ISSUE_REVIEW_ROLE}) | _WRITERS, _LIST, 0),
    ("search", "issues"): _GhSpec(frozenset({ISSUE_REVIEW_ROLE}), _SEARCH, None),
    ("search", "prs"): _GhSpec(frozenset({ISSUE_REVIEW_ROLE}), _SEARCH, None),
    ("pr", "diff"): _GhSpec(_READERS_OF_PRS, {"--name-only": False, "--patch": False,
                                              "--color": True, "--exclude": True,
                                              "-e": True}, 1),
    ("run", "view"): _GhSpec(_WRITERS, {**_READ_OUTPUT, "--log": False, "--log-failed": False,
                                        "--job": True, "-j": True, "--verbose": False,
                                        "-v": False, "--exit-status": False,
                                        "--attempt": True, "-a": True}, 1),
    ("run", "list"): _GhSpec(_WRITERS, {**_READ_OUTPUT, "--branch": True, "-b": True,
                                        "--limit": True, "-L": True, "--commit": True,
                                        "-c": True, "--workflow": True, "-w": True,
                                        "--status": True, "-s": True, "--event": True,
                                        "-e": True}, 0),
    ("pr", "comment"): _GhSpec(_READERS_OF_PRS, {**_BODY, "--edit-last": False,
                                                 "--create-if-none": False}, 1, "pr"),
    ("pr", "review"): _GhSpec(REVIEW_ROLES, {**_BODY, "--comment": False, "-c": False}, 1,
                              "pr", ("--comment", "-c")),
    ("pr", "create"): _GhSpec(frozenset({"implement"}), {
        **_BODY, "--title": True, "-t": True, "--base": True, "-B": True, "--head": True,
        "-H": True, "--draft": False, "-d": False, "--fill": False, "-f": False,
        "--fill-first": False}, 0),
    ("pr", "edit"): _GhSpec(frozenset({"implement"}), {**_BODY, "--title": True, "-t": True},
                            1, "pr"),
    ("issue", "comment"): _GhSpec(_WRITERS, {**_BODY, "--edit-last": False}, 1, "issue"),
}

#: The only `gh api` call outside resolve, used by `cc-provider-bootstrap`.
GH_API_USER = ["api", "user", "--jq", ".login"]


def _same_repo(value: str, repo: str | None) -> bool:
    return bool(repo) and value.lower().removesuffix(".git") == repo.lower()


def _check_gh(args: list[str], policy: StagePolicy, *, cwd: str | None, run) -> None:
    if args == GH_API_USER:
        return
    # Global repository selection may appear anywhere and must be this cycle's.
    remaining: list[str] = []
    index = 0
    while index < len(args):
        word = args[index]
        if word in {"-R", "--repo"} or word.startswith("--repo="):
            value = word.split("=", 1)[1] if "=" in word else (
                args[index + 1] if index + 1 < len(args) else "")
            if not _same_repo(value, policy.repo):
                raise PolicyError("gh may operate only on this cycle's repository")
            index += 1 if "=" in word else 2
            continue
        if word == "--hostname" or word.startswith("--hostname="):
            raise PolicyError("gh --hostname is not allowed")
        remaining.append(word)
        index += 1
    if remaining[:2] == ["api", "graphql"]:
        if policy.role != "resolve":
            raise PolicyError("gh api graphql is available only to the resolve stage")
        return _check_thread_call(remaining[2:], policy, cwd=cwd, run=run)
    if remaining[:1] == ["api"]:
        raise PolicyError("gh api is not allowed except the documented calls")
    key = tuple(remaining[:2])
    spec = GH_TABLE.get(key)  # type: ignore[arg-type]
    if spec is None or policy.role not in spec.roles:
        raise PolicyError(f"gh {' '.join(remaining[:2])} is not in the {policy.role} "
                          "stage's command table")
    positional: list[str] = []
    seen: set[str] = set()
    index = 2
    while index < len(remaining):
        word = remaining[index]
        if word.startswith("-") and word != "-":
            name, has_value, value = word.partition("=")
            if name not in spec.flags:
                raise PolicyError(f"gh {' '.join(key)} option {name} is not in the table")
            seen.add(name)
            if spec.flags[name]:
                if not has_value:
                    index += 1
                    value = remaining[index] if index < len(remaining) else ""
                if name in {"--body-file", "-F"} and not inside_tmpdir(value, policy.tmpdir):
                    raise PolicyError("a body file must be inside $TMPDIR")
            elif has_value:
                raise PolicyError(f"gh option {name} takes no value")
        else:
            positional.append(word)
        index += 1
    if spec.max_positional is not None and len(positional) > spec.max_positional:
        raise PolicyError(f"gh {' '.join(key)} takes at most {spec.max_positional} argument(s)")
    if spec.required and not seen.intersection(spec.required):
        raise PolicyError(f"gh {' '.join(key)} requires {spec.required[0]}")
    if key == ("repo", "view") and positional and not _same_repo(positional[0], policy.repo):
        raise PolicyError("gh repo view may read only this cycle's repository")
    if key in {("search", "issues"), ("search", "prs")} and "--owner" in seen:
        raise PolicyError("gh search may not widen to another owner")
    if spec.scope and positional and not _reference_in_repo(positional[0], policy.repo):
        raise PolicyError(f"gh {' '.join(key)} may target only this cycle's repository")
    if spec.scope == "pr":
        expected = policy.pr_number
        if expected is None:
            # Before the change request exists (an implementation opens it),
            # only the current branch's own one may be named, by naming none.
            if positional:
                raise PolicyError(f"gh {' '.join(key)} may target only the current branch's "
                                  "change request until this cycle has one")
        elif not positional or _number(positional[0], ("/pull/", "#")) != expected:
            raise PolicyError(f"gh {' '.join(key)} may target only change request {expected}")
    if spec.scope == "issue":
        expected = policy.issue_number
        if (expected is None or not positional
                or _number(positional[0], ("/issues/", "#")) != expected):
            raise PolicyError("gh issue comment may target only this cycle's work item")


def _reference_in_repo(reference: str, repo: str | None) -> bool:
    """A number or `#n` is this repository's; a URL must name it."""
    if "/" not in reference:
        return True
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/(?:pull|issues)/\d+/?", reference)
    return bool(match) and _same_repo(match.group(1), repo)


def _check_docker(args: list[str], policy: StagePolicy) -> None:
    if args[:1] != ["compose"]:
        raise PolicyError("only docker compose up/down/ps/logs/build is ever allowed")
    rest = args[1:]
    index = 0
    while index < len(rest) and rest[index].startswith("-"):
        name, has_value, _ = rest[index].partition("=")
        if name not in DOCKER_COMPOSE_GLOBAL:
            raise PolicyError(f"docker compose option {name} is not in the table")
        index += 1 if has_value or not DOCKER_COMPOSE_GLOBAL[name] else 2
    subcommand = rest[index] if index < len(rest) else ""
    if subcommand not in DOCKER_COMPOSE_SUBCOMMANDS:
        raise PolicyError(f"docker compose {subcommand or '(none)'} is never allowed; "
                          "only up, down, ps, logs and build are")
    if not policy.docker:
        raise _DockerDenied("docker compose needs the docker_host grant, which this "
                            "dispatch does not have")


# --- review-thread GraphQL ------------------------------------------------------

_GQL_TOKEN = re.compile(
    r'(?P<space>[\s,]+)|(?P<string>"(?:[^"\\\n]|\\.)*")|(?P<number>-?\d+)'
    r'|(?P<name>[_A-Za-z][_0-9A-Za-z]*)|(?P<punct>[{}()\[\]:!=$])|(?P<other>.)'
)


@dataclass
class _Field:
    name: str
    alias: str | None
    args: dict
    selection: list | None


class _GraphQL:
    """Just enough of GraphQL to prove a document is one documented operation."""

    def __init__(self, document: str) -> None:
        if '"""' in document:
            raise PolicyError("GraphQL block strings are not allowed")
        self.tokens: list[tuple[str, str]] = []
        for match in _GQL_TOKEN.finditer(document):
            kind = match.lastgroup
            if kind == "space":
                continue
            if kind == "other":
                raise PolicyError(f"unexpected GraphQL character {match.group()!r}")
            self.tokens.append((kind, match.group()))
        self.position = 0

    def peek(self) -> str | None:
        return self.tokens[self.position][1] if self.position < len(self.tokens) else None

    def take(self, expected: str | None = None, kind: str | None = None) -> str:
        if self.position >= len(self.tokens):
            raise PolicyError("incomplete GraphQL document")
        token_kind, value = self.tokens[self.position]
        if (expected is not None and value != expected) or (kind and token_kind != kind):
            raise PolicyError(f"unexpected GraphQL token {value!r}")
        self.position += 1
        return value

    def document(self) -> tuple[str, set[str], list[_Field]]:
        operation = self.take(kind="name")
        if operation not in {"query", "mutation"}:
            raise PolicyError("the document must be one query or mutation")
        if self.peek() not in {"(", "{"}:
            self.take(kind="name")
        variables: set[str] = set()
        if self.peek() == "(":
            self.take("(")
            while self.peek() != ")":
                self.take("$")
                variables.add(self.take(kind="name"))
                self.take(":")
                self.type_reference()
                if self.peek() == "=":
                    self.take("=")
                    self.value()
            self.take(")")
        selection = self.selection()
        if self.position != len(self.tokens):
            raise PolicyError("only one GraphQL operation per call is allowed")
        return operation, variables, selection

    def type_reference(self) -> None:
        if self.peek() == "[":
            self.take("[")
            self.type_reference()
            self.take("]")
        else:
            self.take(kind="name")
        if self.peek() == "!":
            self.take("!")

    def selection(self) -> list[_Field]:
        self.take("{")
        fields = []
        while self.peek() != "}":
            name = self.take(kind="name")
            alias = None
            if self.peek() == ":":
                self.take(":")
                alias, name = name, self.take(kind="name")
            args = {}
            if self.peek() == "(":
                self.take("(")
                while self.peek() != ")":
                    key = self.take(kind="name")
                    self.take(":")
                    args[key] = self.value()
                self.take(")")
            nested = self.selection() if self.peek() == "{" else None
            fields.append(_Field(name, alias, args, nested))
        self.take("}")
        if not fields:
            raise PolicyError("empty GraphQL selection")
        return fields

    def value(self):
        token = self.peek()
        if token == "$":
            self.take("$")
            return ("var", self.take(kind="name"))
        if token == "[":
            self.take("[")
            items = []
            while self.peek() != "]":
                items.append(self.value())
            self.take("]")
            return ("list", items)
        if token == "{":
            self.take("{")
            items = {}
            while self.peek() != "}":
                key = self.take(kind="name")
                self.take(":")
                items[key] = self.value()
            self.take("}")
            return ("object", items)
        kind, text = self.tokens[self.position] if token is not None else ("", "")
        self.take()
        if kind == "string":
            return ("string", json.loads(text))
        if kind in {"number", "name"}:
            return (kind, text)
        raise PolicyError(f"unexpected GraphQL value {text!r}")


def _variable(value, what: str) -> str:
    if not (isinstance(value, tuple) and value[0] == "var"):
        raise PolicyError(f"{what} must be passed as a GraphQL variable")
    return value[1]


def _check_thread_call(args: list[str], policy: StagePolicy, *, cwd: str | None,
                       run) -> None:
    """One of the three documented review-thread calls, on this cycle's PR."""
    fields: dict[str, tuple[str, str]] = {}
    query = None
    paginate = False
    index = 0
    while index < len(args):
        word = args[index]
        name, has_value, value = word.partition("=")
        if name in {"--paginate", "--slurp", "--silent"} and not has_value:
            paginate |= name == "--paginate"
        elif name in {"--jq", "-q", "--template", "-t"}:
            if not has_value:
                index += 1
        elif name in {"-f", "--raw-field", "-F", "--field"}:
            if not has_value:
                index += 1
                value = args[index] if index < len(args) else ""
            key, separator, content = value.partition("=")
            if not separator or not key:
                raise PolicyError("a GraphQL field must be key=value")
            if key == "query":
                if query is not None:
                    raise PolicyError("only one GraphQL query per call is allowed")
                query = content
            else:
                if key in fields:
                    raise PolicyError(f"GraphQL variable {key} was given twice")
                fields[key] = ("F" if name in {"-F", "--field"} else "f", content)
        else:
            raise PolicyError(f"gh api graphql option {name} is not in the table")
        index += 1
    if query is None:
        raise PolicyError("gh api graphql needs a query field")
    operation, declared, selection = _GraphQL(query).document()
    if len(selection) != 1 or selection[0].alias:
        raise PolicyError("a review-thread call has exactly one top-level field")
    root = selection[0]
    if operation == "query":
        used = _check_thread_query(root, fields, policy, paginate)
    else:
        used = _check_thread_mutation(root, fields, policy, cwd=cwd, run=run)
    provided = set(fields)
    if provided - used:
        raise PolicyError("unexpected GraphQL variables: " + ", ".join(sorted(provided - used)))
    missing = (used | declared) - provided - ({"endCursor"} if paginate else set())
    if missing:
        raise PolicyError("missing GraphQL variables: " + ", ".join(sorted(missing)))


def _field_value(fields: dict, name: str) -> str:
    if name not in fields:
        raise PolicyError(f"GraphQL variable {name} is not provided")
    kind, value = fields[name]
    if value.startswith("@"):
        raise PolicyError(f"GraphQL variable {name} may not be read from a file")
    return value


def _check_thread_query(root: _Field, fields: dict, policy: StagePolicy,
                        paginate: bool) -> set[str]:
    if root.name != "repository" or set(root.args) != {"owner", "name"}:
        raise PolicyError("the thread query reads repository(owner:, name:) only")
    owner = _field_value(fields, _variable(root.args["owner"], "owner"))
    name = _field_value(fields, _variable(root.args["name"], "name"))
    if not _same_repo(f"{owner}/{name}", policy.repo):
        raise PolicyError("the thread query may read only this cycle's repository")
    children = root.selection or []
    if len(children) != 1 or children[0].name != "pullRequest" or children[0].alias:
        raise PolicyError("the thread query reads one pullRequest only")
    pull = children[0]
    if set(pull.args) != {"number"}:
        raise PolicyError("the thread query selects pullRequest(number:) only")
    number_variable = _variable(pull.args["number"], "number")
    if policy.pr_number is None or _field_value(fields, number_variable) != policy.pr_number:
        raise PolicyError("the thread query may read only this cycle's change request")
    threads = pull.selection or []
    if (len(threads) != 1 or threads[0].name != "reviewThreads" or threads[0].alias
            or not threads[0].selection or set(threads[0].args) - THREAD_QUERY_FIELDS):
        raise PolicyError("the thread query reads reviewThreads(first:, after:) only")
    used = {_variable(root.args["owner"], "owner"), _variable(root.args["name"], "name"),
            number_variable}
    for key, value in threads[0].args.items():
        if isinstance(value, tuple) and value[0] == "var":
            used.add(value[1])
        elif key == "after" or value[0] != "number":
            raise PolicyError("reviewThreads paging must use a number or a variable")
    return used - ({"endCursor"} if paginate else set())


def _check_thread_mutation(root: _Field, fields: dict, policy: StagePolicy, *,
                           cwd: str | None, run) -> set[str]:
    expected = THREAD_MUTATIONS.get(root.name)
    if expected is None:
        raise PolicyError("only addPullRequestReviewThreadReply and resolveReviewThread "
                          "may be called")
    if set(root.args) != {"input"} or root.args["input"][0] != "object":
        raise PolicyError(f"{root.name} takes one input object")
    payload = root.args["input"][1]
    if set(payload) != expected:
        raise PolicyError(f"{root.name} input must be exactly {', '.join(sorted(expected))}")
    used = set()
    thread_key = "pullRequestReviewThreadId" if "pullRequestReviewThreadId" in payload else "threadId"
    thread_variable = _variable(payload[thread_key], thread_key)
    used.add(thread_variable)
    if "body" in payload:
        body_variable = _variable(payload["body"], "body")
        used.add(body_variable)
        if body_variable in fields:
            # `-F body=@path` reads a file; `-f` passes the text as it is.
            kind, value = fields[body_variable]
            if (kind == "F" and value.startswith("@")
                    and not inside_tmpdir(value[1:], policy.tmpdir)):
                raise PolicyError("a reply body file must be inside $TMPDIR")
    thread = _field_value(fields, thread_variable)
    if not re.fullmatch(r"[A-Za-z0-9_=-]{1,128}", thread):
        raise PolicyError("not a review-thread node ID")
    owner = THREAD_OWNER(thread, cwd=cwd, run=run)
    if (owner is None or not _same_repo(owner[0], policy.repo)
            or policy.pr_number is None or owner[1] != policy.pr_number):
        raise PolicyError("the review thread does not belong to this cycle's change request")
    return used


THREAD_OWNER_QUERY = (
    "query($id: ID!) { node(id: $id) { ... on PullRequestReviewThread { "
    "pullRequest { number repository { nameWithOwner } } } } }"
)


def lookup_thread_owner(thread_id: str, *, cwd: str | None = None,
                        run=subprocess.run) -> tuple[str, str] | None:
    """The repository and pull request a review thread belongs to, or None."""
    try:
        completed = run(
            ["gh", "api", "graphql", "-f", f"query={THREAD_OWNER_QUERY}", "-F",
             f"id={thread_id}"],
            cwd=cwd, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
        )
        if completed.returncode:
            return None
        pull = json.loads(completed.stdout)["data"]["node"]["pullRequest"]
        return pull["repository"]["nameWithOwner"], str(pull["number"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


#: Indirection for tests; the hook always performs the real lookup.
THREAD_OWNER = lookup_thread_owner


# --- rendering for Claude --------------------------------------------------------

def allowed_tool_patterns(policy: StagePolicy) -> list[str]:
    """`--allowedTools` patterns for the stage's table: the first barrier.

    The patterns are prefixes; the hook validates the arguments behind them.
    """
    prefixes = [f"git {sub}" for sub in sorted(GIT_COMMON_READ)]
    prefixes.append("git branch --show-current")
    if policy.role in GIT_SEARCH_ROLES:
        prefixes += [f"git {sub}" for sub in sorted(GIT_REVIEW_READ)]
    if policy.role in WRITING_ROLES:
        prefixes += [f"git {sub}" for sub in sorted(GIT_WRITE)]
        prefixes.append("git worktree list")
    elif policy.role in REVIEW_ROLES and policy.fetch_origin:
        prefixes.append("git fetch origin")
    prefixes += [f"gh {group} {sub}" for (group, sub), spec in sorted(GH_TABLE.items())
                 if policy.role in spec.roles]
    prefixes.append("gh api user --jq .login")
    if policy.role == "resolve":
        prefixes.append("gh api graphql")
    prefixes += sorted(READ_SHELL)
    if policy.docker:
        prefixes += [f"docker compose {sub}" for sub in sorted(DOCKER_COMPOSE_SUBCOMMANDS)]
    patterns = []
    for prefix in prefixes:
        patterns.append(f"Bash({prefix}:*)")
        if policy.rtk:
            patterns += [f"Bash(rtk {prefix}:*)", f"Bash(rtk proxy {prefix}:*)"]
    for command in policy.verification_commands:
        exact = command == policy.test_command
        patterns.append(f"Bash({command})" if exact else f"Bash({command}:*)")
    if policy.tmpdir:
        # Native helpers may live in an executor-owned subdirectory (#280).
        # The hook validates names and resolved paths, including symlinks.
        for tmpdir in (policy.tmpdir, str(Path(policy.tmpdir).resolve(strict=False))):
            root = _absolute_rule_path(tmpdir)
            patterns += [f"Edit({root}/{name})" for name in TMPDIR_HELPER_FILES]
            patterns += [f"Edit({root}/claude-*/{name})" for name in TMPDIR_HELPER_FILES]
            patterns += [f"Edit({root}/*.log)", f"Edit({root}/claude-*/*.log)"]
    return list(dict.fromkeys(patterns))


def _absolute_rule_path(path: str) -> str:
    """`path` as a Claude path rule names an absolute path: `//` from the root.

    Claude matches Windows paths in POSIX form, `C:\\x` as `/c/x`.
    """
    target = Path(path)
    if target.drive:
        rest = "/".join(target.parts[1:])
        return f"//{target.drive.rstrip(':').lower()}/{rest}".rstrip("/")
    return "/" + target.as_posix().rstrip("/")


def hook_settings(hook_command: str) -> dict:
    """The `--settings` hook block that makes the hook part of the dispatch."""
    return {"hooks": {"PreToolUse": [{
        "matcher": "|".join(["Bash", *FILE_EDIT_TOOLS]),
        "hooks": [{"type": "command", "command": hook_command, "timeout": 60}],
    }]}}


def prompt_note(policy: StagePolicy) -> str:
    """What the agent is told about the table it runs under."""
    if not policy.active:
        return ""
    verification = (
        "The configured verification commands are " + ", ".join(
            f"`{command}`" for command in policy.verification_commands) + "."
        if policy.verification_commands else
        "No test or verify command is configured for this repository, so no "
        "verification command is available.")
    return (
        f"Command policy for this {policy.role} stage: a runtime hook validates "
        "every Bash command against this stage's closed table. Run one simple "
        "command per call, without chaining, pipes, substitutions or redirections "
        "other than into $TMPDIR. " + verification + " When a command you need is "
        "denied, do not try a variant or another tool to get around it: stop "
        "BLOCKED and name the exact denied command, so a person can authorize it "
        "and resume the cycle."
    )


# --- configuration -----------------------------------------------------------

def validate_verify_prefix(command: str) -> None:
    """Refuse a `verify` entry that would be arbitrary execution."""
    if not isinstance(command, str) or not command.strip():
        raise PolicyError("a verify command must be a non-empty string")
    if re.search(r"\*|;|&&|\|\||\||`|\$|[<>]|[&\r\n]|\\", command):
        raise PolicyError("a verify command is one exact prefix; wildcards, variables "
                          "and shell operators are not allowed")
    words = command.split()
    program = os.path.basename(words[0]).lower()
    program = re.sub(r"(\.exe|[0-9.]+)$", "", program) or program
    if program in SHELL_LAUNCHERS:
        raise PolicyError(f"{words[0]} runs arbitrary commands; name the script or tool itself")
    if program in LAUNCHERS:
        if len(words) < 2:
            raise PolicyError(f"{words[0]} alone would run anything; add the script or module")
        second = words[1]
        if second == "-m" or second in RUNNER_SUBCOMMANDS:
            if len(words) < 3 or words[2].startswith("-"):
                raise PolicyError(f"{words[0]} {second} needs the module or script it runs")
        elif second.startswith("-"):
            raise PolicyError(f"{words[0]} {second} is not a specific script or module; "
                              "inline code is never a verification prefix")


# --- Claude configuration checks ------------------------------------------------

def claude_config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def managed_settings_paths() -> tuple[Path, ...]:
    if sys.platform == "darwin":
        return (Path("/Library/Application Support/ClaudeCode/managed-settings.json"),)
    if os.name == "nt":
        return (Path(os.environ.get("ProgramFiles", "C:/Program Files"))
                / "ClaudeCode" / "managed-settings.json",)
    return (Path("/etc/claude-code/managed-settings.json"),)


def _read_settings(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise PolicyError(f"{path} could not be read ({type(error).__name__})") from error
    return payload if isinstance(payload, dict) else {}


def hook_override(config_dir: Path | None = None,
                  managed: tuple[Path, ...] | None = None) -> str | None:
    """Why a dispatch-supplied hook would not run, or None."""
    config_dir = config_dir or claude_config_dir()
    for path in managed if managed is not None else managed_settings_paths():
        settings = _read_settings(path)
        if settings.get("disableAllHooks") or settings.get("allowManagedHooksOnly"):
            return f"managed settings at {path} disable hooks a dispatch supplies"
    if _read_settings(config_dir / "settings.json").get("disableAllHooks"):
        return f"{config_dir / 'settings.json'} sets disableAllHooks"
    return None


def tool_check_mods(config_dir: Path | None = None) -> list[str]:
    """Enabled user plugins whose hooks module handles `tool.check`.

    Claude Code's permission documentation says such a mod can approve a call a
    `PreToolUse` hook blocked, unless the hook comes from managed settings. The
    CLI has no flag that turns mods off while keeping the rest of the user's
    configuration (`--safe-mode` and `--bare` also drop hooks such as RTK's),
    so a dispatch with one loaded is refused.
    """
    config_dir = config_dir or claude_config_dir()
    settings = _read_settings(config_dir / "settings.json")
    enabled = {name for name, on in (settings.get("enabledPlugins") or {}).items() if on}
    if not enabled:
        return []
    installed = _read_settings(config_dir / "plugins" / "installed_plugins.json")
    found = []
    for name in sorted(enabled):
        for entry in (installed.get("plugins") or {}).get(name) or []:
            path = entry.get("installPath") if isinstance(entry, dict) else None
            if path and Path(path).is_dir() and _handles_tool_check(Path(path)):
                found.append(name)
                break
    return found


def _handles_tool_check(root: Path) -> bool:
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = [name for name in subdirectories
                             if name not in {"node_modules", ".git"}]
        for name in files:
            if Path(name).suffix not in {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}:
                continue
            try:
                text = (Path(directory) / name).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "tool.check" in text:
                return True
    return False


#: User-level sandbox settings that would widen a confined review, because
#: Claude merges arrays across settings sources.
SANDBOX_WIDENING_KEYS = (
    ("filesystem", "allowWrite"), ("filesystem", "disabled"), ("excludedCommands",),
    ("allowUnsandboxedCommands",), ("network", "allowUnixSockets"),
    ("network", "allowAllUnixSockets"), ("enableWeakerNestedSandbox",),
)


def sandbox_override(path: Path | None = None) -> str | None:
    """A setting in `path` (the user's settings by default) that would widen
    the review sandbox, or None."""
    path = path or claude_config_dir() / "settings.json"
    sandbox = _read_settings(path).get("sandbox") or {}
    if not isinstance(sandbox, dict):
        return None
    for keys in SANDBOX_WIDENING_KEYS:
        value = sandbox
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        if value:
            return f"{path} sets sandbox.{'.'.join(keys)}, which would widen the review sandbox"
    return None


#: Claude's own tools the stage table already accounts for: Bash and the edit
#: tools pass through the hook, a subagent's calls pass through it too, and
#: the rest only read.
TABLE_COVERED_TOOLS = frozenset({"Bash", *FILE_EDIT_TOOLS, "Task", "Agent", "Read", "Grep",
                                 "Glob", "LS", "NotebookRead", "WebSearch", "WebFetch",
                                 "TodoWrite"})

#: Words in a tool's name that suggest it runs code or writes files. A name is
#: all a configuration says about a tool, so this is a hint, not a verdict.
CODE_RUNNING_WORDS = frozenset({"bash", "batch", "command", "create", "delete", "edit", "eval",
                                "exec", "execute", "merge", "push", "remove", "run", "script",
                                "shell", "terminal", "update", "upload", "write"})


def preapproved_outside_table(cwd: str | None = None, config_dir: Path | None = None,
                              managed: tuple[Path, ...] | None = None) -> list[str]:
    """What the loaded configuration pre-approves that the stage table does not govern.

    The table and the hook govern the stage's Bash commands and file edits.
    Any other tool the user's `permissions.allow` names, such as an MCP tool,
    runs under the user's configuration. So does a managed `defaultMode` of
    `auto` or `bypassPermissions`; a user or project one does not apply,
    because a policy-bound dispatch passes its own `--permission-mode`. This
    only informs: it never refuses a dispatch (#142).
    """
    config_dir = config_dir or claude_config_dir()
    managed = managed if managed is not None else managed_settings_paths()
    layers = [*managed, config_dir / "settings.json"]
    if cwd:
        layers += [Path(cwd) / ".claude" / name for name in ("settings.json", "settings.local.json")]
    found: list[str] = []
    for path in layers:
        try:
            permissions = _read_settings(path).get("permissions")
        except PolicyError:
            continue
        if not isinstance(permissions, dict):
            continue
        allow = permissions.get("allow")
        for rule in allow if isinstance(allow, list) else []:
            tool = rule.split("(", 1)[0].strip() if isinstance(rule, str) else ""
            if tool and tool not in TABLE_COVERED_TOOLS and tool not in found:
                found.append(tool)
        mode = permissions.get("defaultMode")
        if path in managed and mode in {"auto", "bypassPermissions"}:
            entry = f"managed permissions.defaultMode {mode}"
            if entry not in found:
                found.append(entry)
    return found


def unnamed_tools(tool: str) -> str | None:
    """What an MCP rule approves beyond one named tool, or None when it names one.

    Claude Code reads `mcp__<server>` and `mcp__<server>__*` as approving every
    tool of that server, so the rule itself names none of them (REV-008).
    """
    if not tool.startswith("mcp__"):
        return None
    server, _, name = tool[len("mcp__"):].partition("__")
    if "*" not in server and name in {"", "*"}:
        return "every tool of that server"
    return "every tool that pattern matches" if "*" in tool else None


def may_run_code(tool: str) -> bool:
    """Whether a pre-approved tool's name suggests it runs code or writes files.

    A rule that approves tools it does not name cannot be told apart from one
    that approves a code-running tool, so it counts as one.
    """
    if tool.startswith("managed permissions.defaultMode") or unnamed_tools(tool):
        return True
    words = set(re.split(r"[^a-z]+", tool.rsplit("__", 1)[-1].lower()))
    return bool(words & CODE_RUNNING_WORDS)


#: The longest pre-approved-tools notice the stage report prints.
NOTICE_LIMIT = 600


def _listed(names: list[str], room: int) -> str:
    """`names` in order within `room` characters, the rest as `and N more`."""
    shown: list[str] = []
    for index, name in enumerate(names):
        rest = len(names) - index - 1
        if len(", ".join([*shown, name]) + (f", and {rest} more" if rest else "")) > room:
            break
        shown.append(name)
    hidden = len(names) - len(shown)
    if not shown:
        return f"{hidden} tool" + ("s" if hidden > 1 else "")
    return ", ".join(shown) + (f", and {hidden} more" if hidden else "")


def preapproved_notice(tools, limit: int = NOTICE_LIMIT) -> str | None:
    """One line for the stage report naming what the table does not govern.

    The tools that can run code come first, and a list too long for `limit`
    ends in `and N more`, so the cap never cuts off their flag (REV-009).
    """
    tools = [tool for tool in tools or () if isinstance(tool, str) and tool.strip()]
    if not tools:
        return None
    running = [f"{tool} ({unnamed_tools(tool)})" if unnamed_tools(tool) else tool
               for tool in tools if may_run_code(tool)]
    quiet = [tool for tool in tools if not may_run_code(tool)]
    head = "outside the stage table, pre-approved by your configuration: "
    flag = " can run code or write files, worth a thought when the cycle runs unattended"
    tail = " (the table governs this cycle's Bash commands and file edits)"
    if not running:
        return head + _listed(quiet, limit - len(head) - len(tail)) + tail
    fixed = len(head) + len(flag) + len(tail) + (len("; also ") if quiet else 0)
    reserve = len(_listed(quiet, 0)) if quiet else 0
    notice = head + _listed(running, limit - fixed - reserve) + flag
    if quiet:
        notice += "; also " + _listed(quiet, limit - len(notice) - len("; also ") - len(tail))
    return notice + tail


#: The code hosts a sandboxed reviewer's `gh` must reach to read the change
#: request and publish its comment; the same hosts Codex's review profile allows.
REVIEW_SANDBOX_DOMAINS = ("github.com", "api.github.com", "bitbucket.org", "api.bitbucket.org")


def review_sandbox_settings(writable: tuple[str, ...]) -> dict:
    """Claude's Bash sandbox for a reviewer that verifies in its clone.

    A sandboxed command has no network route except the allowed domains, and a
    `claude -p` reviewer has nobody to answer a prompt for any other host.
    """
    return {"sandbox": {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "filesystem": {"allowWrite": list(writable)},
        "network": {"allowedDomains": list(REVIEW_SANDBOX_DOMAINS)},
    }}


#: What Claude prints when `failIfUnavailable` stops it before any tool runs.
SANDBOX_UNAVAILABLE_MARKERS = ("sandbox required but unavailable",
                               "refusing to start without a working sandbox")


# --- the hook ------------------------------------------------------------------

def _hook_output(decision: str, reason: str) -> str:
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }})


def hook_main(arguments: list[str], stdin=None, stdout=None) -> int:
    """Answer one `PreToolUse` event; deny on any doubt.

    An allowed command gets no decision at all rather than `allow`: an `allow`
    would skip the permission layer, and `--allowedTools` and the user's own
    deny rules are meant to stay in force as the first barrier. The hook only
    ever narrows.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        policy = StagePolicy.from_json(Path(arguments[0]).read_text(encoding="utf-8"))
        event = json.loads(stdin.read())
        tool = event.get("tool_name")
        if tool in FILE_EDIT_TOOLS:
            decision = check_file_edit(tool, event.get("tool_input"), policy,
                                       cwd=event.get("cwd"))
        elif tool != "Bash":
            return 0
        else:
            command = (event.get("tool_input") or {}).get("command")
            decision = check(command, policy, cwd=event.get("cwd"))
    except Exception as error:
        print(_hook_output("deny", f"code-cycle {HOOK_DENIAL_MARKER}: the stage policy "
                                   f"could not run ({type(error).__name__}). {DENY_ADVICE}"),
              file=stdout)
        return 0
    if not decision.allowed:
        advice = EDIT_DENY_ADVICE if tool in FILE_EDIT_TOOLS else DENY_ADVICE
        print(_hook_output("deny", f"code-cycle {policy.role} {HOOK_DENIAL_MARKER}: "
                                   f"{decision.reason}. {advice}"), file=stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the hook tests
    if sys.argv[1:2] == ["hook"] and len(sys.argv) == 3:
        sys.exit(hook_main(sys.argv[2:]))
    print("usage: stage_permissions.py hook <policy.json>", file=sys.stderr)
    sys.exit(2)
