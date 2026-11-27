"""The closed command table of each native stage, and the hook that enforces it.

Until #142 a dispatched stage kept its executor's normal `gh` and `git` access,
and the publication policy in its prompt was the only boundary. This module
replaces that, for Claude on a GitHub cycle, with two barriers:

- `--allowedTools` patterns derived from the stage's table, so the executor's
  own permission layer starts from the table rather than from `Bash(gh:*)`.
  #524 has two documented dispatch-only exceptions: `git *` and
  `python3 -I -c:*` admit all Git subcommands and arbitrary inline Python.
  Their operation restrictions rely on the immutable hook, not this barrier;
- a `PreToolUse` hook that the runtime adds to every Claude dispatch. It parses
  each Bash command, its arguments, flags and refspecs, and denies anything the
  stage's table does not contain. Claude Code 2.1.302 lets a hook's `deny`
  prevail over another hook's `allow`, so a permissive global rule or the RTK
  rewrite hook cannot reopen what this one closes.

Literal commands and simple read pipelines use the default table. Other
shell syntax is analysed without evaluation and requests whole-operation interpreter consent. Concrete segment
guards remain in force; pattern grants lift only table membership. Opaque
interpreter internals always depend on external isolation.

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

import contextlib
import ast
import hashlib
import fnmatch
import itertools
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

WRITING_ROLES = frozenset({"implement", "resolve"})
REVIEW_ROLES = frozenset({"review", "rereview"})
ISSUE_REVIEW_ROLE = "issue_review"

CREDENTIAL_STORES = ("~/.ssh", "~/.aws", "~/.codex/auth.json",
                     "~/.claude/.credentials.json", "~/.claude.json")
GH_CREDENTIAL_STORE = "~/.config/gh"


def credential_read_denials(*, publishes: bool = False,
                           credential_reads: tuple[str, ...] = (),
                           home: Path | None = None) -> tuple[str, ...]:
    """Existing canonical targets only: bwrap cannot mask a symlink (#294).

    Inspect metadata, never credential contents. An absent target needs no
    mount, and must not be created by the sandbox as a persistent placeholder.
    """
    unknown = set(credential_reads) - {*CREDENTIAL_STORES, GH_CREDENTIAL_STORE}
    if unknown:
        raise PolicyError("credential_reads must name known credential stores")
    root = home if home is not None else Path.home()
    stores = (*CREDENTIAL_STORES, *((GH_CREDENTIAL_STORE,) if not publishes else ()))
    return tuple(dict.fromkeys(
        str(target.resolve(strict=True)) for store in stores
        if store not in credential_reads
        for target in (root / store[2:],) if target.exists()))

#: Read-only shell tools every stage may use, and nothing else from the shell.
READ_SHELL = frozenset({"ls", "cat", "head", "tail", "wc", "grep", "find", "jq", "sha256sum",
                        "sed", "date", "ps"})
#: `sed` is the one read tool whose first barrier is narrower than the program:
#: the hook accepts a `sed` that starts with one of these options, and the
#: `--allowedTools` patterns name exactly these prefixes (#464).
SED_FIRST_OPTIONS = ("-n", "-E", "-e", "-ne", "-nE", "-En")
_SED_FLAGS = frozenset({"-n", "--quiet", "--silent", "-E", "-r", "--regexp-extended",
                        "-s", "--separate", "-z", "--null-data"})
_SED_COMMANDS = "pdqQ=lPnN"
_SED_S_FLAGS = frozenset("gpiImM0123456789")
#: `ps` output fields that name no command line, environment or file. `args`,
#: `command` and `cmd` are not here: another process's arguments can carry a
#: credential, so those forms stay in the consent flow (#464).
PS_SAFE_FIELDS = frozenset({
    "pid", "ppid", "pgid", "sid", "tid", "tty", "stat", "state", "s", "etime", "etimes",
    "time", "cputime", "cputimes", "pcpu", "%cpu", "pmem", "%mem", "rss", "vsz", "sz",
    "nlwp", "thcount", "comm", "ucmd", "lstart", "start", "start_time", "nice", "ni",
    "pri", "user", "uid"})
_PS_SELECT_FLAGS = frozenset({"-e", "-A", "-a", "--no-headers", "--no-header"})
#: `find` primaries that run a command, delete, or write a file.
FIND_FORBIDDEN = frozenset({
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fprint", "-fprint0", "-fprintf", "-fls",
})

GIT_COMMON_READ = frozenset({"status", "diff", "log", "rev-parse", "show"})
GIT_REVIEW_READ = frozenset({"merge-base", "ls-files", "grep", "blame"})
#: Stages that search the repository with `GIT_REVIEW_READ`. The hook and the
#: `--allowedTools` patterns both read this set, so the two barriers agree.
GIT_SEARCH_ROLES = WRITING_ROLES | REVIEW_ROLES | {ISSUE_REVIEW_ROLE, "verify", "security", "bootstrap", "coordinate"}
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
#: The fixed Bash timeout of a dispatched Claude `-p` stage, in milliseconds
#: (10 minutes). Claude Code reads it from the `env` of the dispatch settings.
CLAUDE_VERIFICATION_TIMEOUT_MS = 600_000

DENY_ADVICE = ("Do not bypass this refusal. The runtime asks this cycle / this repository / "
               "forever / deny for a grantable operation. Record the exact operation in "
               "permission_effects with essential and check_ids. Skip accessory operations; "
               "affected checks stay not_verified. An unanswered request is permission_required.")
EDIT_DENY_ADVICE = DENY_ADVICE
UNRESOLVED_COMPOSE_ADVICE = (
    "This check cannot be identified in this checkout, so no permission can be asked for it and "
    "nothing here is waiting for an answer. Do not retry variants of it. Record it not_verified "
    "with this cause, say what would fix it (update the branch with the base branch if the Compose "
    "file is missing, create the missing env file from its template) and continue the stage's "
    "other work.")


class PolicyError(ValueError):
    """A command, or a configured command, the policy refuses."""


class ConsentRequired(PolicyError):
    """An operation that needs exact consent; it is not a guard consent cannot lift."""


class ComposeResolutionTimeout(PolicyError):
    """The inspection budget expired, rather than configuration being absent."""


def _runtime_directory() -> str:
    """The directory this module is installed in, in POSIX form."""
    return Path(__file__).resolve().parent.as_posix()


@dataclass(frozen=True)
class StagePolicy:
    """What one dispatched stage may run through Bash, and in which context.

    The repository, change request and work item scope the publishing `gh`
    commands; changes to the base branch need exact recorded consent. `tmpdir`
    and `rtk` are filled in at dispatch time, when they are known.
    """

    role: str
    repo: str | None = None
    change_request_id: str | None = None
    work_item_id: str | None = None
    base_branch: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    test_command: str | None = None
    verify_prefixes: tuple[str, ...] = ()
    code_host: str | None = "github"
    issue_provider: str | None = "github"
    docker: bool = False
    rtk: bool = False
    tmpdir: str | None = None
    fetch_origin: bool = True
    # Explicit project choices are authorization, not suggestions to the table.
    tools: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    integrations: tuple[str, ...] = ()
    registry_domains: tuple[str, ...] = ()
    sandbox_writable: tuple[str, ...] = ()
    sandbox_grant_role: str = ""
    credential_reads: tuple[str, ...] = ()
    credential_publishing: bool = False
    # Host-observed unavailable isolation: retain read/publication/edit guards,
    # but never allow project code execution without the required sandbox.
    static_only: bool = False
    # Where the hook records its own denials (#306): it runs as a separate
    # process that receives only this policy. None of these widens anything.
    journal_path: str | None = None
    cycle_id: str | None = None
    attempt_id: str | None = None
    stage_seq: int | None = None
    #: The branch of the run's checkout, which `permissions_command` queries.
    checkout_branch: str | None = None
    #: The installed runtime's directory, fixed when the policy is built in the
    #: runtime process (#319). The hook runs a copy of this module in a
    #: temporary directory, so it must read the directory from `policy.json`
    #: instead of deriving it from its own `__file__`.
    runtime_dir: str = field(default_factory=lambda: _runtime_directory())
    command_permissions: tuple[dict[str, Any], ...] = ()
    #: Exact read-only evidence files supplied by the runtime, outside the checkout.
    evidence_paths: tuple[str, ...] = ()
    #: The current Claude session's own tool-results directory (#439 AC-13).
    #: The hook sets it from the event Claude Code sends; the runtime never does.
    tool_results: str | None = None

    @property
    def triage_validator_path(self) -> str:
        """The staged-comment validator installed with the runtime, in shell-safe form."""
        return (Path(self.runtime_dir) / "validate-triage-comment.py").as_posix()

    @property
    def permissions_command(self) -> tuple[str, ...]:
        """The one read-only command that lists the permissions in force (#311).

        The runtime fixes the database, repository and branch; a stage appends
        nothing, so it cannot read another repository's or branch's decisions.
        Empty when the dispatch has no journal or no branch to query.
        """
        if not (self.journal_path and self.repo and self.checkout_branch):
            return ()
        script = (Path(self.runtime_dir) / "cycle_journal.py").as_posix()
        return ("python3", script, "--database", self.journal_path, "permissions",
                "--repo", self.repo, "--branch", self.checkout_branch)

    @property
    def publication_commands(self) -> tuple[tuple[str, ...], ...]:
        """Only the installed transport helper, with this dispatch's fixed scope."""
        if (self.static_only or self.role not in WRITING_ROLES or self.code_host != "github"
                or not self.repo or not _SAFE_REPOSITORY.fullmatch(self.repo)
                or not self.base_branch):
            return ()
        script = (Path(self.runtime_dir) / "publication.py").as_posix()
        return tuple(("python3", script, action, "--repo", self.repo,
                      "--base", self.base_branch) for action in ("preflight", "push"))

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
        if self.static_only or self.role not in WRITING_ROLES | REVIEW_ROLES | {"verify"}:
            return ()
        commands = (self.test_command,) if self.test_command else ()
        return commands + tuple(self.verify_prefixes)

    @property
    def requires_confined_verification(self) -> bool:
        """A reviewer that may run tests must do so in a confined clone."""
        return self.role in REVIEW_ROLES and bool(self.verification_commands)

    @property
    def uses_bash_sandbox(self) -> bool:
        """Keep Claude sandbox emission and its preflight on the same scope."""
        return self.active and not self.static_only and bool(
            self.requires_confined_verification or self.registry_domains or self.sandbox_writable
            or self.publication_commands or self.docker)

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
        for key in ("tools", "commands", "integrations", "registry_domains", "sandbox_writable", "credential_reads", "command_permissions", "evidence_paths"):
            data[key] = tuple(data.get(key) or ())
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
    #: The rule that refused, for the denial record (#364). Empty when allowed.
    rule: str = ""


@dataclass
class ParsedCommand:
    words: list[str] = field(default_factory=list)
    #: `(fd, operator, target)`; `target` is None for `2>&1`.
    redirects: list[tuple[str, str, str | None]] = field(default_factory=list)
    shell_source: str | None = None
    segments: list["ParsedCommand"] = field(default_factory=list)


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


def _parse_simple(command: str, tmpdir: str | None) -> ParsedCommand:
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
    windows_path = False
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
        nonlocal word, started, quoted, pending, windows_path
        if not started:
            return
        text = "".join(word)
        if pending is not None:
            parsed.redirects.append((pending[0], pending[1], text))
            pending = None
        else:
            parsed.words.append(text)
        word, started, quoted, windows_path = [], False, False, False

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
                if (inner == "\\" and os.name == "nt"
                        and (windows_path or re.fullmatch(r"[A-Za-z]:", "".join(word)))):
                    windows_path = True
                    word.append(inner)
                    i += 1
                elif (inner == "\\" and i + 1 < n
                        and command[i + 1] in '$`"\\'):
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
            if (os.name == "nt"
                    and (windows_path or re.fullmatch(r"[A-Za-z]:", "".join(word)))):
                # Keep separators in drive-rooted Windows paths so the path
                # guard sees C:\\Temp\\outside rather than C:Tempoutside.
                windows_path = True
                word.append(char)
                started = True
                i += 1
            elif i + 1 >= n:
                raise PolicyError("trailing backslash")
            else:
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


def parse_command(command: str, tmpdir: str | None) -> ParsedCommand:
    """Literal commands use the table; shell syntax uses explicit consent.

    Shell text stays exact, including quotes and delimiters. Analysis never
    evaluates an expansion or executes a command. Each visible segment and
    nested substitution is retained for the concrete-command guards.
    """
    try:
        parsed = _parse_simple(command, tmpdir)
        # These reserved words execute the following command even without an
        # operator or expansion. They must take the same segment path as their
        # compound forms, rather than hiding the program from its guards.
        start = len(command) - len(command.lstrip())
        if not any(_shell_keyword(command, start, prefix) for prefix in ("coproc", "time", "!")):
            return parsed
    except PolicyError as error:
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise
        if str(error) in {"unterminated quote", "trailing backslash", "a redirection needs a target", "no command"}:
            raise
    segments = _shell_segments(command, tmpdir)
    return ParsedCommand(shell_source=command, segments=segments)


#: ANSI-C escapes with a fixed meaning, as bash decodes them in `$'…'`.
_ANSI_C_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r",
                   "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _ansi_c(body: str) -> str:
    """The text of a `$'…'` body, decoded exactly as bash decodes it.

    Octal takes 1-3 digits, `\\x` 1-2, `\\u` 1-4 and `\\U` 1-8 hex digits;
    `\\cX` is a control character; an unknown escape keeps its backslash; the
    result ends at the first NUL, as the C string bash builds does.
    """
    out: list[str] = []
    i = 0
    while i < len(body):
        if body[i] != "\\" or i + 1 == len(body):
            out.append(body[i])
            i += 1
            continue
        code = body[i + 1]
        digits = None
        if code in _ANSI_C_ESCAPES:
            out.append(_ANSI_C_ESCAPES[code])
            i += 2
        elif code in "01234567":
            digits = re.match(r"[0-7]{1,3}", body[i + 1:])
            assert digits is not None
            out.append(chr(int(digits[0], 8) & 0xFF))
            i += 1 + len(digits[0])
        elif code in "xuU" and (digits := re.match(r"[0-9A-Fa-f]{1,%d}" % {"x": 2, "u": 4, "U": 8}[code],
                                                    body[i + 2:])):
            out.append(chr(min(int(digits[0], 16), 0x10FFFF)))
            i += 2 + len(digits[0])
        elif code == "c" and i + 2 < len(body):
            control = body[i + 2]
            # `\c\\` names control-backslash and consumes both backslashes.
            width = 4 if control == "\\" and body[i + 3:i + 4] == "\\" else 3
            out.append("\x7f" if control == "?" else chr(ord(control.upper()) & 0x1F))
            i += width
        else:
            out.append("\\" + code)
            i += 2
    return "".join(out).split("\0", 1)[0]


def _splice_dollar(source: str, start: int) -> str:
    """Remove line continuations inside the `$` expansion at `start`.

    Bash removes backslash-newline before it reads `$'`, `$"`, `$(`, `${…}`
    or a name, so a continuation cannot split them from their `$`.
    """
    position = start + 1
    braced = False
    while position < len(source):
        if source.startswith("\\\n", position):
            source = source[:position] + source[position + 2:]
            continue
        char = source[position]
        if position == start + 1 and char in "'\"(":
            break
        if position == start + 1 and char == "{":
            braced = True
        elif braced:
            if char == "}":
                break
        elif not (char.isalnum() or char == "_"):
            break
        position += 1
    return source


#: Reserved words after which the next word is again in command position.
#: Bash permits non-POSIX function names: any unquoted shell word without $.
_FUNCTION_NAME = r"[^ \t\n;&|()<>$'\"\\]+"
_COMMAND_PREFIX_WORD = re.compile(
    r"(?<![^ \t\n;&|()])(?:then|do|else|elif|if|while|until|!|\{|"
    r"time(?:[ \t]+-p)?(?:[ \t]+--)?|coproc(?:[ \t]+" + _FUNCTION_NAME + r")?|"
    r"function[ \t]+" + _FUNCTION_NAME + r")$")
_FUNCTION_HEADER = re.compile(r"(?<![^ \t\n;&|()])(?:function[ \t]+)?" + _FUNCTION_NAME + r"[ \t]*\([ \t]*\)$")
# Bash permits `do` (or an arithmetic for's `{`) immediately after a loop
# header, without a separator. Only use this after finding that body opener.
_LOOP_BODY_HEADER = re.compile(
    r"(?<![^ \t\n;&|()])(?:for|select)[ \t]+[A-Za-z_][A-Za-z0-9_]*$")
_ARITHMETIC_FOR_HEADER = re.compile(r"(?<![^ \t\n;&|()])for[ \t]*\(\(")


def _loop_body_header(source: str, depth: int) -> int | None:
    """Start of a loop header ending here, without consuming earlier data."""
    if header := _LOOP_BODY_HEADER.search(source):
        return header.start()
    if not source.endswith("))"):
        return None
    for header in reversed(list(_ARITHMETIC_FOR_HEADER.finditer(source))):
        # Reuse arithmetic expansion's quote/nesting rules, without evaluation.
        # A regex alone can span an earlier extglob or another loop header.
        arithmetic = "$" + source[header.end() - 2:]
        try:
            end = _shell_expansion_end(arithmetic, 0, depth + 1, [])
        except PolicyError as error:
            if str(error) != "unterminated shell expansion":
                raise  # preserve the existing analysis resource limits
            continue  # a header-looking word inside quoted data
        if end == len(arithmetic) - 1:
            return header.start()
    return None


def _shell_keyword(source: str, i: int, word: str) -> int:
    """Width of an unquoted keyword, including Bash line continuations.

    Keep offsets into the original source: globally removing continuations
    would also change quoted data and the substitution slices we reanalyse.
    """
    end = i
    for letter in word:
        while source.startswith("\\\n", end):
            end += 2
        if source[end:end + 1] != letter:
            return 0
        end += 1
    while source.startswith("\\\n", end):
        end += 2
    return end - i if end == len(source) or source[end] in " \t\n;&|()<>" else 0


def _command_position(source: str, start: int, i: int, pattern_close: int, depth: int = 0) -> bool:
    """Whether a word at `i` is where bash recognises a reserved word.

    `start` is the first character of the command list and `pattern_close`
    the `)` that last ended a `case` pattern. Function and loop headers also
    start compound bodies; a substitution's `)` leaves an argument position.
    """
    # Preserve original offsets while removing continuations. Escaped
    # backslashes are pairs, so an even run leaves a real newline separator.
    # The mapping also retains a pattern ')' after stripping `if`, `time`,
    # etc. from the suffix, including when continuations follow that ')'.
    characters: list[str] = []
    offsets: list[int] = []
    position = start
    while position < i:
        if source.startswith("\\\n", position):
            position += 2
            continue
        width = 2 if source[position] == "\\" and position + 1 < i else 1
        characters.extend(source[position:position + width])
        offsets.extend(range(position, position + width))
        position += width
    before = "".join(characters).rstrip(" \t")
    while True:
        if not before:
            return True
        last, rest = before[-1], before[:-1]
        if (len(rest) - len(rest.rstrip("\\"))) % 2:
            return False  # an escaped separator or a line continuation
        if last in ";&|\n":
            return True
        if last == ")":
            if offsets[len(before) - 1] == pattern_close:
                return True
            header = _FUNCTION_HEADER.search(before)
            if header is None:
                return False
            # `echo @()` is an extglob argument, not a function declaration.
            # The header must itself start where a command can start.
            before = before[:header.start()].rstrip(" \t")
            continue
        if last == "(":
            return not rest or rest[-1] not in "@+*?$<>"
        keyword = _COMMAND_PREFIX_WORD.search(before)
        if keyword is None:
            return False
        before = before[:keyword.start()].rstrip(" \t")
        if keyword.group() in {"do", "{"}:
            header = _loop_body_header(before, depth)
            if header is not None:
                # Check the header's own command position on the next pass:
                # `echo for x do case` still contains only argument words.
                before = before[:header].rstrip(" \t")


def _shell_expansion_end(source: str, start: int, depth: int, substitutions: list[str]) -> int:
    """Find a nested expansion's delimiter without counting its parentheses
    against an enclosing extglob (REV-013). Never evaluate the expansion.
    """
    if depth > 32:
        raise PolicyError("shell analysis resource limit; use a script with interpreter consent")
    def marker_at(position: int) -> int:
        marker = position + 1
        if source[position] == "$":
            while source.startswith("\\\n", marker):
                marker += 2
        return marker

    marker = marker_at(start)
    opening = source[marker]
    closing = {"{": "}", "[": "]", "(": ")"}[opening]
    nesting, i = 1, marker + 1
    quote: str | None = None
    shell = opening == "(" and not (source[start] == "$" and source[marker + 1:marker + 2] == "(")
    heredocs: list[tuple[str, bool]] = []
    word_active = False
    # Open `case` clauses (REV-014): [state, nesting, pattern_started], where
    # state is "head" (before `in`), "pattern" or "body". A pattern list ends
    # with an unmatched `)` that does not close the substitution.
    cases: list[list[Any]] = []
    pattern_close = -1
    while i < len(source):
        char = source[i]
        if char == "\\" and quote != "'":
            if source[i + 1:i + 2] != "\n":
                word_active = True
            i += 2
            continue
        if shell and quote is None and cases and cases[-1][0] == "pattern" and not cases[-1][2] \
                and char not in " \t\n#":
            # The first character of a pattern: `esac` ends the clause list
            # and an opening `(` is optional syntax, not a nested group.
            if keyword_width := _shell_keyword(source, i, "esac"):
                cases.pop()
                word_active = True
                i += keyword_width
                continue
            cases[-1][2] = True
            if char == "(":
                i += 1
                continue
        nested_marker = marker_at(i) if char in "$<>" else i
        if quote is None and char == "$" and source[nested_marker:nested_marker + 1] == "'":
            # ANSI-C quotes permit escaped single quotes. Treat the whole
            # string as data, including braces and expansion-looking text.
            i = nested_marker + 1
            word_active = True
            while i < len(source) and source[i] != "'":
                i += 2 if source[i] == "\\" else 1
            if i >= len(source):
                break
            i += 1
            continue
        if char in "\"'" and (quote is None or quote == char):
            word_active = True
            quote = None if quote == char else char
            i += 1
            continue
        if quote != "'":
            if (char == "$" and source[nested_marker:nested_marker + 1] in {"{", "[", "("}
                    or char in "<>" and source[nested_marker:nested_marker + 1] == "("):
                nested: list[str] = []
                word_active = True
                end = _shell_expansion_end(source, i, depth + 1, nested)
                if (source[nested_marker] == "("
                        and not (char == "$" and source[nested_marker + 1:nested_marker + 2] == "(")):
                    # Keep the original quoting. Heredoc-style quote escaping
                    # changes which ')' ends a command substitution.
                    substitutions.append(source[nested_marker + 1:end])
                else:
                    substitutions.extend(nested)
                i = end + 1
                continue
            if char == "`":
                word_active = True
                body_start = i + 1
                i += 1
                while i < len(source) and source[i] != "`":
                    i += 2 if source[i] == "\\" else 1
                if i >= len(source):
                    break
                substitutions.append(source[body_start:i])
                i += 1
                continue
            if quote is None:
                if shell and char == "#" and not word_active:
                    newline = source.find("\n", i)
                    if newline < 0:
                        break
                    i = newline
                    continue
                if shell and source.startswith("<<", i) and not source.startswith("<<<", i):
                    match = re.match(r"<<(-)?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2", source[i:])
                    if match:
                        heredocs.append((match[3], bool(match[1])))
                        word_active = True
                        i += match.end()
                        continue
                if shell and char == "\n" and heredocs:
                    i += 1
                    for delimiter, strip_tabs in heredocs:
                        closing_line = re.search(r"(?m)^" + (r"\t*" if strip_tabs else "")
                                                 + re.escape(delimiter) + r"(?:\n|$)", source[i:])
                        if closing_line is None:
                            raise PolicyError("unterminated shell heredoc")
                        i += closing_line.end()
                    heredocs.clear()
                    word_active = False
                    continue
                if shell and cases and cases[-1][1] == nesting:
                    clause = cases[-1]
                    if clause[0] == "pattern" and char == ")":
                        clause[0], word_active, pattern_close = "body", False, i
                        i += 1
                        continue
                    terminator = re.match(r";;&|;;|;&", source[i:]) if clause[0] == "body" else None
                    if terminator:
                        clause[0], clause[2], word_active = "pattern", False, False
                        i += terminator.end()
                        continue
                    if (not word_active and clause[0] == "head"
                            and (keyword_width := _shell_keyword(source, i, "in"))):
                        clause[0], clause[2], word_active = "pattern", False, True
                        i += keyword_width
                        continue
                    if (not word_active and clause[0] == "body" and (keyword_width := _shell_keyword(source, i, "esac"))
                            and _command_position(source, marker + 1, i, pattern_close, depth)):
                        cases.pop()
                        word_active = True
                        i += keyword_width
                        continue
                if (shell and not word_active and (keyword_width := _shell_keyword(source, i, "case"))
                        and not (cases and cases[-1][0] != "body")
                        and _command_position(source, marker + 1, i, pattern_close, depth)):
                    cases.append(["head", nesting, False])
                    word_active = True
                    i += keyword_width
                    continue
                # A parameter expansion ends at the first unquoted }, except
                # inside another expansion. A literal { does not nest it.
                if char == opening and opening != "{":
                    nesting += 1
                elif char == closing:
                    nesting -= 1
                    if nesting == 0:
                        if opening == "{" and source[marker + 1:marker + 2] in {" ", "\t", "\n", "|"}:
                            substitutions[:] = [source[marker + 1:i].lstrip("|")]
                        return i
                if shell:
                    word_active = char not in " \t\n;|&()<>"
        i += 1
    raise PolicyError("unterminated shell expansion")


def _peel_command_headers(words: list[str], before_subshell: bool = False,
                          quoted: list[bool] | None = None) -> list[str]:
    """The words of a segment once its leading reserved-word headers are gone.

    Peel headers together, repeatedly: e.g. `time coproc`, `function f {`, or
    `coproc NAME {` (REV-015). Only a leading header changes command position;
    the same words in printf/echo arguments stay data. `before_subshell` says
    that a `(` ends the segment, as in `coproc NAME ( … )`. `quoted[i]` says
    that `words[i]` had quoting: Bash recognises reserved words only when
    unquoted, so `coproc find "if" src` is the simple command `find` (REV-016).
    Peeling a quoted *leading* word stays conservative, because it only
    exposes more words to the guards.
    """
    flags = list(quoted) if quoted is not None and len(quoted) == len(words) else [False] * len(words)
    while words:
        before = len(words)
        words = _peel_one_header(words, before_subshell, flags)
        if len(words) == before:
            break
        flags = flags[before - len(words):]
    return words


def _peel_one_header(words: list[str], before_subshell: bool, quoted: list[bool]) -> list[str]:
    if words:
        if words[0] in {"if", "then", "elif", "else", "while", "until", "do", "!", "{", "}"}:
            return words[1:]
        if words[0] == "time":
            words = words[1:]
            if words[:1] == ["-p"]:
                words = words[1:]
            if words[:1] == ["--"]:
                words = words[1:]
            return words
        if words[0] == "function" and len(words) > 1:
            return words[2:]
        if words[0] in {"for", "select"} and words[2:3] == ["{"]:
            return words[3:]
        if words[0] == "coproc":
            # A name is optional only before a compound command. For a simple
            # command `coproc git push`, git is the executable, and so is find
            # in `coproc find "if" src` because a quoted word is never reserved.
            named = ((len(words) > 2 and not quoted[2]
                      and words[2] in {"{", "[[", "if", "while", "until", "for", "select", "case"})
                     or (before_subshell and len(words) == 2))
            return words[2:] if named else words[1:]
    return words


def _shell_segments(source: str, tmpdir: str | None, depth: int = 0) -> list[ParsedCommand]:
    if depth > 32 or len(source) > 65536:
        raise PolicyError("shell analysis resource limit; use a script with interpreter consent")
    result: list[ParsedCommand] = []
    word: list[str] = []
    words: list[str] = []
    # Whether each word of `words` had quoting (REV-016). Bash decides
    # reserved words and comments before quote removal, so `"if"` is not `if`,
    # `""` is an empty argument and `""#` is not a comment.
    quoted_words: list[bool] = []
    word_quoted = False
    redirects: list[tuple[str, str, str | None]] = []
    pending: str | None = None
    quote: str | None = None
    extglob_depth = 0
    i = 0
    segment_start = 0

    def finish_word():
        nonlocal word, pending, word_quoted
        if not word and not word_quoted:
            return
        value = "".join(word)
        was_quoted = word_quoted
        word, word_quoted = [], False
        if pending:
            redirects.append(("1", pending, value))
            pending = None
        else:
            words.append(value)
            quoted_words.append(was_quoted)

    def finish_segment(before_subshell: bool = False):
        nonlocal words, quoted_words, redirects, segment_start
        finish_word()
        words = _peel_command_headers(words, before_subshell, quoted_words)
        if words or redirects:
            result.append(ParsedCommand(words=words, redirects=redirects))
        words, quoted_words, redirects = [], [], []
        segment_start = i + 1

    while i < len(source):
        char = source[i]
        if char == "$" and quote != "'":
            source = _splice_dollar(source, i)
        if char == "\\" and quote != "'" and i + 1 < len(source):
            # In double quotes Bash retains a backslash before ordinary
            # characters (e.g. grep's \|, \. and \b regex escapes).
            if quote == '"' and source[i + 1] not in '$`"\\\n':
                word.append(char)
                i += 1
                continue
            # A backslash-newline is a line continuation: bash removes both.
            if source[i + 1] != "\n":
                word.append(source[i + 1])
                word_quoted = True
            i += 2
            continue
        if char == "$" and quote is None and source.startswith("$'", i):
            # ANSI-C quoting: decode the escapes bash decodes, so the guards
            # see the path the program receives.
            end = i + 2
            while end < len(source) and source[end] != "'":
                end += 2 if source[end] == "\\" else 1
            word.append(_ansi_c(source[i + 2:min(end, len(source))]))
            word_quoted = True
            i = end + 1
            continue
        if char == "$" and quote is None and source.startswith('$"', i):
            # Locale quoting `$"…"` reads as a double-quoted string.
            i += 1
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
            word_quoted = True
            i += 1
            continue
        if char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
            word_quoted = True
            i += 1
            continue
        if quote == "'":
            word.append(char)
            i += 1
            continue
        if (quote is None and not word and not extglob_depth
                and (keyword_width := _shell_keyword(source, i, "do"))
                and _loop_body_header(source[segment_start:i].rstrip(" \t"), depth) is not None
                and _command_position(source, segment_start, i + keyword_width, -1, depth)):
            # `for x do CMD` and `select x do CMD` have no operator between
            # the header and the executable body. Do not fold CMD into `for`.
            finish_segment()
            i += keyword_width
            segment_start = i
            continue
        if extglob_depth and source.startswith(("${", "$["), i):
            substitutions: list[str] = []
            end = _shell_expansion_end(source, i, depth + 1, substitutions)
            for body in substitutions:
                result.extend(_shell_segments(body, tmpdir, depth + 1))
            word.append(source[i:end + 1])
            i = end + 1
            continue
        # Substitutions inside double quotes execute too. Preserve their text
        # in the outer word and analyse the body separately for direct guards.
        prefix = next((p for p in ("$(", "<(", ">(") if source.startswith(p, i)), None)
        if prefix or char == "`":
            start = i
            if char == "`":
                end = i + 1
                while end < len(source) and (source[end] != "`" or source[end - 1] == "\\"):
                    end += 1
                body = source[i + 1:end]
            else:
                end = _shell_expansion_end(source, i, depth + 1, [])
                body = source[i + 2:end]
            result.extend(_shell_segments(body, tmpdir, depth + 1))
            word.append(source[start:min(end + 1, len(source))])
            i = end + 1
            continue
        if char == "$":
            home_spelling = next((p for p in ("${HOME}", "$HOME") if source.startswith(p, i)
                                  and (p.endswith("}") or i + len(p) == len(source)
                                       or not (source[i + len(p)].isalnum() or source[i + len(p)] == "_"))), None)
            if home_spelling:
                word.append(str(Path.home()))
                i += len(home_spelling)
                continue
            spelling = next((p for p in ("${TMPDIR}", "$TMPDIR") if source.startswith(p, i)
                             and (p.endswith("}") or i + len(p) == len(source)
                                  or not (source[i + len(p)].isalnum() or source[i + len(p)] == "_"))), None)
            if spelling and tmpdir:
                word.append(tmpdir)
                i += len(spelling)
                continue
        if (quote is None and char in "@+!*?" and source[i + 1:i + 2] == "("
                and not (char == "!" and not word and not _peel_command_headers(
                    [prefix for prefix in words if not _ENV_ASSIGNMENT.match(prefix)]))):
            # An extglob is one operand, even with spaces or shell-looking
            # alternatives. Substitutions above still get their own guards.
            # At command position `!(...)` may instead be negation followed
            # by a subshell; retain that executable interpretation too.
            word.extend((char, "("))
            extglob_depth += 1
            i += 2
            continue
        if quote is None and extglob_depth:
            word.append(char)
            extglob_depth += (1 if char == "(" else -1 if char == ")" else 0)
        elif quote is None and char in " \t":
            finish_word()
        elif quote is None and char == "#" and not word and not word_quoted:
            end = source.find("\n", i)
            i = len(source) if end < 0 else end
            continue
        elif quote is None and source.startswith("<<", i):
            # Heredoc contents are data, except substitutions of an unquoted
            # delimiter. The original text still requires shell consent.
            finish_word()
            match = re.match(r"<<(-)?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2", source[i:])
            if match:
                body_start = source.find("\n", i + match.end())
                if body_start >= 0:
                    closing = re.search(r"(?m)^\t*" + re.escape(match[3]) + r"\s*$", source[body_start + 1:])
                    if closing:
                        body_end = body_start + 1 + closing.start()
                        result.extend(_shell_segments(source[i + match.end():body_start], tmpdir, depth + 1))
                        if not match[2]:
                            # Scan only expansions: other heredoc text is not code.
                            result.extend(_heredoc_substitutions(source[body_start + 1:body_end], tmpdir, depth))
                        i = body_start + 1 + closing.end()
                        continue
            pending = "<<"
            i += 2
            continue
        elif quote is None and char in "<>":
            # A quoted `"2">x` is the argument 2 and a stdout redirection.
            numbered = bool(word) and not word_quoted and "".join(word).isdecimal()
            descriptor = "".join(word) if numbered else "1"
            if numbered:
                word = []
            finish_word()
            duplication = re.match(r"([<>])&([0-9]+|-)", source[i:])
            if duplication:
                redirects.append((descriptor, duplication[1] + "&", None))
                i += duplication.end()
                continue
            pending = char
            if i + 1 < len(source) and source[i + 1] == char:
                pending += char
                i += 1
        elif quote is None and char in "|&;\n()":
            finish_segment(before_subshell=char == "(")
        elif char == "~" and not word and not word_quoted and quote is None:
            # `""~` does not start with an unquoted `~`: bash keeps it literal.
            # Only a bare `~` names $HOME. `~user`, `~+`, `~-` and `~N` keep
            # their prefix so the guards resolve what bash reads (REV-009).
            # Braces expand first (`~{,}/x` is `~/x`): expanding `~` keeps
            # the brace part, which may then name any home entry.
            bare = i + 1 == len(source) or source[i + 1] in "/ \t|&;\n()<>{"
            word.append(os.path.expanduser("~") if bare else "~")
        else:
            word.append(char)
        i += 1
    if extglob_depth:
        raise PolicyError("unterminated extglob")
    finish_segment()
    return result


def _heredoc_substitutions(body: str, tmpdir: str | None, depth: int) -> list[ParsedCommand]:
    # Treat the body as double-quoted data, which shares heredoc expansion
    # semantics for command substitutions; quotes in the body are literal.
    escaped = body.replace('"', '\\"')
    return _shell_segments('"' + escaped + '"', tmpdir, depth + 1)[:-1]


#: The files a non-writing stage writes in its `$TMPDIR` (#272): the comment,
#: report and thread-reply bodies its skills publish, and check logs named
#: after the check. Claude keeps its own session state in the same directory
#: (materialised settings, `tasks/` output, `bash-edit-diff/`), so the grant
#: names these top-level files rather than the whole directory.
TMPDIR_HELPER_FILES = ("pr-comment.md", "report.md", "reply.md", "delta.diff")
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


#: A Claude Code session identifier as it appears in its transcript name.
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
#: How a stored operation spells an operand under the session's tool-results
#: directory. Claude Code names each saved output with a fresh random
#: identifier, so the whole operand is the placeholder (#439 AC-13).
TOOL_RESULT_OUTPUT = "<OUTPUT>"
TOOL_RESULTS_OPERAND = "<TOOL_RESULTS>/" + TOOL_RESULT_OUTPUT


def session_tool_results(event: Any) -> str | None:
    """The current session's own tool-results directory, from Claude Code's event.

    Claude Code saves a large tool output under
    `<projects>/<project-dir>/<session_id>/tool-results/`, beside the session
    transcript `<projects>/<project-dir>/<session_id>.jsonl`. Both values come
    from the host's hook event, never from the agent; any other shape names
    no directory, and another session's directory is never this one.
    """
    if not isinstance(event, dict):
        return None
    session, transcript = event.get("session_id"), event.get("transcript_path")
    if not (isinstance(session, str) and isinstance(transcript, str)
            and SESSION_ID.fullmatch(session) and os.path.isabs(transcript)):
        return None
    path = Path(transcript)
    if path.name != session + ".jsonl" or not is_session_tool_results(path.parent / session / "tool-results"):
        return None
    return str(path.parent / session / "tool-results")


def is_session_tool_results(path: Any) -> bool:
    """Whether `path` has the shape `…/projects/<project-dir>/<session>/tool-results`."""
    if not isinstance(path, (str, Path)) or not os.path.isabs(path):
        return False
    path = Path(path)
    return (path.name == "tool-results" and ".." not in path.parts and len(path.parts) > 4
            and SESSION_ID.fullmatch(path.parent.name) is not None
            and path.parent.parent.parent.name == "projects")


def inside_tool_results(path: str, policy: Any) -> bool:
    """Whether `path` really lies in the current session's tool-results directory.

    The same membership rule as `$TMPDIR`: symlinks and `..` are resolved
    first, so neither leads into it from elsewhere or out of it. The directory
    itself must be a real directory, not a symlink to another one.
    """
    results = getattr(policy, "tool_results", None)
    if not results:
        return False
    try:
        directory = Path(results)
        if directory.is_symlink() or not directory.resolve(strict=True).is_dir():
            return False
    except (OSError, RuntimeError):
        return False
    return inside_tmpdir(os.path.expanduser(path) if path.startswith("~/") else path, results)


#: Directories created once per dispatch or per session: a review clone, a
#: dispatch's `$TMPDIR`, and a Claude session's tool-results directory. A
#: literal path under one of them can never occur again (#439 AC-14). One
#: path component at a time, with no leading repetition: linear in the text.
_UNIQUE_COMPONENT = r"[^\s\"'<>|&;()/:,=\[\]{}]+"
_DISPATCH_UNIQUE_PATH = re.compile(
    r"(?:^|(?<=[/\s\"'=:,\[{]))(?:code-cycle-(?:tmp|review)-[A-Za-z0-9_]+"
    r"|\.claude/projects/" + _UNIQUE_COMPONENT + "/" + _UNIQUE_COMPONENT + "/tool-results)"
    r"(?=$|[/\s\"'<>|&;:,\]})=])")


def dispatch_unique_path(op: Any) -> str | None:
    """A literal path in an exact operation that no later dispatch can repeat.

    The current `$TMPDIR` and the current session's tool-results directory are
    placeholders already; what remains under such a directory belongs to one
    dispatch or one session, so a repository or forever grant for it would
    never match again. A configured check ignores its environment prefixes
    when it matches, so they do not count here. Pattern grants never apply.
    """
    if not isinstance(op, dict) or op.get("version") != 1:
        return None
    pattern = op.get("pattern")
    if not isinstance(pattern, dict):
        return None
    if op.get("tool") != "Bash":
        target = pattern.get("target")
        texts = [target] if isinstance(target, str) else []
    else:
        words = pattern.get("words")
        if op.get("check") and isinstance(words, list):
            pattern = {**pattern, "words": [word for word in words if not (
                isinstance(word, dict) and "env" in word
                or isinstance(word, str) and _ENV_ASSIGNMENT.match(word))]}
        try:
            texts = [normalized_command({**op, "pattern": pattern})]
        except (KeyError, TypeError, AttributeError):
            return None
    for text in texts:
        found = _DISPATCH_UNIQUE_PATH.search(text.replace("\\", "/"))
        if found:
            return found.group(0)
    return None


# --- the table ---------------------------------------------------------------

def git_remote_identity(url: str, cwd: str | None) -> str:
    """Bind local remotes to their resolved location, never a relative spelling."""
    if os.path.isabs(url) or ":" not in url:
        url = str((Path(cwd or ".") / os.path.expanduser(url)).resolve())
    return hashlib.sha256(url.encode()).hexdigest()


#: Options an ordinary working-branch push may carry without consent.
_WORKING_BRANCH_PUSH_OPTIONS = frozenset({
    "--set-upstream", "--quiet", "--verbose", "--progress", "--porcelain",
    "--no-verify", "--verify", "--dry-run", "--atomic"})


def sensitive_operation(tool: str, data: dict[str, Any], policy: Any,
                        cwd: str | None, *, resolve: bool = True, run=None,
                        _parsed: ParsedCommand | None = None,
                        _working_branch: bool = True) -> dict[str, Any] | None:
    """Resolve the scope shown to the person; never execute the requested operation.

    Read-only Git inspection is bounded. A failed inspection is a real limit,
    never an invented SHA or an authorization for an unknown destination.
    """
    run = run or subprocess.run
    paths: list[str] = []
    words: list[str] = []
    selection: list[str] = []
    if tool == "Bash":
        parsed = _parsed or parse_command(data.get("command", ""), policy.tmpdir)
        if parsed.shell_source is not None:
            parts = []
            credential_stores = []
            raw_guard = _compound_analysis(tool, data, parsed, policy, cwd, _credential_only=True)[0]
            candidates = (_guarded_stores(policy) if raw_guard in {
                "protected credential reads cannot be granted", _UNRESOLVED_CREDENTIAL} else [])
            for store in candidates:
                probe = replace(policy, credential_reads=tuple(
                    item for item in (*CREDENTIAL_STORES, GH_CREDENTIAL_STORE) if item != store),
                    credential_publishing=False)
                reason = _compound_analysis(tool, data, parsed, probe, cwd, _credential_only=True)[0]
                if reason in {"protected credential reads cannot be granted", _UNRESOLVED_CREDENTIAL}:
                    credential_stores.append(store)
            if credential_stores:
                parts.append({"kind": "credential", "stores": credential_stores,
                              "role_bound": policy.role not in WRITING_ROLES,
                              "cycle_only": policy.role not in WRITING_ROLES,
                              "warning": "WARNING: credential stores become readable by this stage and code it runs: "
                                         + ", ".join(credential_stores)})
            directory = cwd
            for segment in parsed.segments:
                if not segment.words:
                    continue
                if segment.words[0] == "cd" and len(segment.words) == 2:
                    directory = str(Path(directory or ".") / os.path.expanduser(segment.words[1]))
                    continue
                segment_policy = replace(policy, credential_reads=tuple(dict.fromkeys(
                    (*policy.credential_reads, *credential_stores))))
                segment_detail = sensitive_operation("Bash", {"command": shlex.join(segment.words)}, segment_policy, directory,
                                                     resolve=resolve, _parsed=segment,
                                                     # Earlier segments may cd or export
                                                     # configuration; a compound already
                                                     # needs consent, so keep the ref binding.
                                                     _working_branch=False)
                if segment_detail:
                    parts.append(segment_detail)
            if parts:
                return {"kind": "compound_sensitive", "parts": parts,
                        "cycle_only": any(part.get("cycle_only") for part in parts),
                        "role_bound": any(part.get("role_bound") for part in parts),
                        "stores": list(dict.fromkeys(store for part in parts for store in part.get("stores", []))),
                        "movements": [move for part in parts for move in part.get("movements", [])],
                        "publication_unconfined": any(part.get("publication_unconfined")
                            or part.get("kind") == "unconfined_publication" for part in parts),
                        "warning": " ".join(part["warning"] for part in parts)}
            return None
        environment, words, cwd = _runner_command_parts(list(parsed.words), cwd)
        paths = list(parsed.words[1:]) + [target for _, _, target in parsed.redirects if target]
    elif tool in FILE_EDIT_TOOLS:
        paths = [data.get(FILE_EDIT_TOOLS[tool], "")]
    else:
        return None
    stores = []
    raw_guard = (protected_reason(tool, data, policy, cwd, _parsed=parsed, _credential_only=True)
                 if tool == "Bash" else credential_reason(paths, policy, cwd))
    candidates = (_guarded_stores(policy) if raw_guard in {
        "protected credential reads cannot be granted", _UNRESOLVED_CREDENTIAL} else [])
    for store in candidates:
        probe = replace(policy, credential_reads=tuple(
            item for item in (*CREDENTIAL_STORES, GH_CREDENTIAL_STORE) if item != store),
            credential_publishing=False)
        reason = (protected_reason(tool, data, probe, cwd, _parsed=parsed, _credential_only=True)
                  if tool == "Bash" else credential_reason(paths, probe, cwd))
        if reason in {"protected credential reads cannot be granted", _UNRESOLVED_CREDENTIAL}:
            stores.append(store)
    if stores:
        restricted = policy.role not in WRITING_ROLES
        detail: dict[str, Any] = {"kind": "credential", "stores": stores,
                "cycle_only": restricted, "role_bound": restricted,
                "warning": "WARNING: credential stores become readable by this stage and code it runs: "
                           + ", ".join(stores)}
        other = sensitive_operation(tool, data, replace(policy, credential_reads=tuple(dict.fromkeys(
            (*policy.credential_reads, *stores)))), cwd, resolve=resolve,
            _parsed=parsed if tool == "Bash" else None)
        if other:
            detail.update(parts=[other], movements=other.get("movements", []),
                          cycle_only=restricted or other.get("cycle_only", False),
                          warning=detail["warning"] + " " + other["warning"])
        return detail
    if tool != "Bash":
        path = Path(cwd or ".") / os.path.expanduser(paths[0])
        candidates = (path.absolute(), path.resolve())
        if any(any(part.casefold().split(":", 1)[0].rstrip(" .") == ".git" for part in candidate.parts)
               or any(candidate == root or root in candidate.parents
               for root in _git_config_paths()) for candidate in candidates):
            return {"kind": "git_config", "warning":
                    "WARNING: Git configuration can execute programs on later allowed commands; "
                    "including host-side inspection outside the stage sandbox. The sandbox permits writes to the whole "
                    "approved file, not just the exact edit. Linked worktrees share the common Git directory "
                    "and the main repository configuration."}
        return None
    if not words:
        return None
    program = _program(words[0])
    if program == "git":
        args, selection = _git_global_options(words[1:])
        selection.extend("--" + {"GIT_DIR": "git-dir", "GIT_WORK_TREE": "work-tree"}[name] + "=" + value
                         for assignment in environment for name, _, value in [assignment.partition("=")]
                         if name in {"GIT_DIR", "GIT_WORK_TREE"})
        # Only literal destination/ref settings may enter host-side read-only
        # inspection. Never forward aliases, pagers, transport executables or
        # credential-helper settings from the requested command.
        settings = []
        for index, word in enumerate(words[1:]):
            if word == "-c" and index + 2 < len(words):
                settings.append(words[index + 2])
            elif word.startswith("-c") and not word.startswith("--") and len(word) > 2:
                settings.append(word[2:])
        assignments = {name: value for item in environment for name, _, value in [item.partition("=")]}
        for name, key in assignments.items():
            if name.startswith("GIT_CONFIG_KEY_"):
                value = assignments.get(name.replace("_KEY_", "_VALUE_"))
                if value is not None:
                    settings.append(key + "=" + value)
        for setting in settings:
            key = setting.partition("=")[0]
            if re.fullmatch(r"remote\.[A-Za-z0-9._/-]+\.(?:url|pushurl|push)|branch\.[A-Za-z0-9._/-]+\.(?:remote|pushRemote|merge)|remote\.pushDefault|push\.default|url\..+\.(?:insteadOf|pushInsteadOf)", key, re.IGNORECASE):
                selection.extend(("-c", setting))
        if not args:
            return None
        subcommand, rest = args[0], args[1:]
        if subcommand == "push":
            options = {"--force", "--force-with-lease", "--force-if-includes", "--mirror",
                       "--delete", "--prune", "--all", "--branches", "--repo", "--push-option",
                       "--receive-pack", "--exec", "--set-upstream", "--no-verify", "--verify",
                       "--dry-run", "--porcelain", "--quiet", "--verbose", "--progress",
                       "--atomic", "--follow-tags", "--tags"}
            normalized = []
            for word in rest:
                name, equals, value = word.partition("=")
                matches = [option for option in options if name.startswith("--") and option.startswith(name)]
                normalized.append((matches[0] + ("=" + value if equals else "")) if len(matches) == 1 else word)
            rest = normalized
        elif subcommand in {"branch", "tag", "update-ref"}:
            rest = ["--delete" + word[len(word.partition("=")[0]):]
                    if word.startswith("--") and "--delete".startswith(word.partition("=")[0]) else word
                    for word in rest]
        if subcommand == "config" and rest and rest[0] not in {"--get", "--get-all", "--get-regexp", "--list", "-l"}:
            if "--global" in rest:
                targets = [str(path) for path in _git_config_paths()]
            elif any(word.startswith("--file=") for word in rest):
                value = next(word.partition("=")[2] for word in rest if word.startswith("--file="))
                targets = [str((Path(cwd or ".") / os.path.expanduser(value)).resolve())]
            elif "--file" in rest or "-f" in rest:
                index = rest.index("--file") if "--file" in rest else rest.index("-f")
                if index + 1 >= len(rest):
                    raise PolicyError("Git config file selection needs a path")
                targets = [str((Path(cwd or ".") / os.path.expanduser(rest[index + 1])).resolve())]
            else:
                directory_option = "--git-dir" if "--worktree" in rest else "--git-common-dir"
                done = subprocess.run(["git", "--no-pager", *selection, "rev-parse", directory_option],
                                      cwd=cwd, capture_output=True, text=True, timeout=10)
                if done.returncode:
                    raise PolicyError("Git configuration target could not be resolved")
                filename = "config.worktree" if "--worktree" in rest else "config"
                targets = [str((Path(cwd or ".") / done.stdout.strip() / filename).resolve())]
            return {"kind": "git_config", "targets": targets, "warning":
                    "WARNING: Git configuration can execute programs on later allowed commands; "
                    "including host-side inspection outside the stage sandbox. The sandbox permits writes to the whole "
                    "approved file, not just the exact edit. Linked worktrees share the common Git directory "
                    "and the main repository configuration."}
        deletion = subcommand in {"branch", "tag", "update-ref"} and any(
            word == "--delete" or word.startswith("--delete=") or
            word.startswith("-") and not word.startswith("--") and any(c in word[1:] for c in "dD")
            for word in rest)
        force = subcommand == "push" and any(
            word.startswith(("--force", "+")) or word.startswith("-") and not word.startswith("--")
            and any(c in word[1:] for c in "fd") for word in rest)
        broad = subcommand == "push" and (len([word for word in rest if not word.startswith("-")]) <= 1
            or any(word.startswith(":") for word in rest) or any(word.split("=", 1)[0] in {
            "--delete", "--prune", "--mirror", "--all", "--branches", "--repo", "--tags", "--follow-tags"} for word in rest)
            )
        base = subcommand == "push" and any(
            word.split(":")[-1].removeprefix("refs/heads/").removeprefix("heads/")
            in {"main", "master", policy.base_branch} for word in rest if not word.startswith("-"))
        positional = [word for word in rest if not word.startswith("-")]
        # The working-branch push of implement and resolve keeps its
        # consent-free path (#456): no global option, no Git environment, so
        # nothing selects another repository or configuration. _check_push
        # reads HEAD of the cycle worktree as before and confirms that a named
        # source is a local branch, never a tag or another ref.
        # A runner (env -C, timeout, nice) or any variable prefix (GIT_*,
        # HOME, XDG_CONFIG_HOME) can select another directory or configuration.
        working_branch_push = (
            _working_branch and subcommand == "push" and not environment
            and list(words) == list(parsed.words) and len(args) == len(words) - 1
            and all(word in _WORKING_BRANCH_PUSH_OPTIONS or re.fullmatch(r"-[uqvn]+", word)
                    for word in rest if word.startswith("-"))
            and len(positional) >= 2 and positional[0] == "origin" and "tag" not in positional[1:]
            and all(":" not in spec and not spec.startswith("+") for spec in positional[1:]))
        # Otherwise an unqualified HEAD destination depends on the selected
        # repository. Do not inspect even HEAD until host-side inspection was
        # authorized.
        broad = broad or subcommand == "push" and not working_branch_push and any(
            word.rsplit(":", 1)[-1] in {"HEAD", "@"} for word in rest)
        # Git can infer the tag namespace from an unqualified source. Only
        # explicit branch sources or HEAD with an explicit destination are
        # known without reading the selected repository.
        broad = broad or subcommand == "push" and not working_branch_push and any(
            ":" not in spec or spec.partition(":")[0] not in {"HEAD", "@"}
            and not spec.startswith("refs/heads/") for spec in positional[1:])
        exceptional_push = False
        if subcommand == "push" and not (force or broad or base):
            try:
                _check_push(rest, policy, cwd=cwd, run=run)
            except PolicyError:
                exceptional_push = True
        if deletion or force or broad or base or exceptional_push:
            if not resolve or policy.role not in WRITING_ROLES:
                return {"kind": "git_refs", "cycle_only": True,
                        "publication_unconfined": bool(policy.static_only and subcommand == "push"),
                        "warning": "WARNING: Git ref changes need recorded exact consent bound to the remote, ref and old/new SHAs."
                                   + (" Publication runs without a confirmed sandbox." if policy.static_only and subcommand == "push" else "")}
            # The hook and broker both run outside the executor's sandbox.
            # A selected repository can configure executable transports, so
            # authorize inspection separately BEFORE running any selected Git.
            identity = operation(tool, data, policy, cwd, _include_sensitive=False)
            directory = str(Path(cwd or ".").resolve())
            inspection = {"kind": "git_inspection", "cycle_only": True, "role_bound": True,
                          "directory": _relative_value(directory, policy, None) or directory,
                          "command_sha256": hashlib.sha256(json.dumps(identity["pattern"], sort_keys=True).encode()).hexdigest(),
                          "warning": "WARNING: binding inspection runs host-side Git outside the stage sandbox; "
                                     "selected repository configuration may execute programs and contact endpoints. "
                                     "This authorizes inspection only; the ref operation needs separate consent."}

            def includes_inspection(detail: dict[str, Any]) -> bool:
                return detail == inspection or any(includes_inspection(part) for part in detail.get("parts", []))

            if not any(entry.get("decision") == "allow"
                       and entry.get("operation", {}).get("role") == policy.role
                       and includes_inspection(entry.get("operation", {}).get("sensitive", {}))
                       for entry in policy.command_permissions):
                return inspection
            def inspect(*arguments: str, optional: bool = False,
                        env: dict[str, str] | None = None) -> str:
                try:
                    done = subprocess.run(["git", "--no-pager", *selection,
                                           *arguments], cwd=cwd, env=env,
                                          capture_output=True, text=True, timeout=30,
                                          stdin=subprocess.DEVNULL)
                except (OSError, subprocess.SubprocessError) as error:
                    raise PolicyError(f"Git consent inspection unavailable: {type(error).__name__}") from error
                if done.returncode:
                    if optional and done.returncode == 1:
                        return ""
                    raise PolicyError(f"Git consent inspection failed: git {arguments[0]} exited {done.returncode}")
                return done.stdout.strip()
            local = dict(line.split(" ", 1)[::-1] for line in inspect(
                "for-each-ref", "--format=%(objectname) %(refname)").splitlines())
            if deletion:
                return {"kind": "git_refs", "cycle_only": True, "local_refs": local,
                        "directory": str(Path(cwd or ".").resolve()), "warning":
                        "WARNING: deletes local Git refs others may use. Exact consent is for this cycle only."}
            # Resolve push destinations and old/new heads before asking. Remote
            # URLs are fingerprinted, never copied into the consent journal.
            operands = []
            skip = False
            selected_remote = None
            for index, word in enumerate(rest):
                if skip:
                    skip = False
                    continue
                if (word in {"--push-option", "-o", "--receive-pack", "--exec", "--repo"}
                        or word.startswith("-") and not word.startswith("--") and word.endswith("o")):
                    if word == "--repo" and index + 1 < len(rest):
                        selected_remote = rest[index + 1]
                    skip = True
                elif word.startswith("--repo="):
                    selected_remote = word.partition("=")[2]
                elif not word.startswith("-"):
                    operands.append(word)
            current = inspect("symbolic-ref", "HEAD", optional=True)
            branch = current.removeprefix("refs/heads/")
            # Git sets up nonbare commands at the worktree root. Bare
            # commands retain the caller directory after literal -C options;
            # --git-dir alone does not move that directory.
            git_cwd = Path(cwd or ".").resolve()
            index = 0
            while index < len(selection):
                if selection[index] == "-C":
                    git_cwd = (git_cwd / selection[index + 1]).resolve()
                index += 2 if selection[index] in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"} else 1
            if inspect("rev-parse", "--is-bare-repository") == "false":
                git_cwd = Path(inspect("rev-parse", "--show-toplevel"))
            # A positional repository takes precedence over --repo, exactly
            # as Git's push option parser does.
            remote = operands[0] if operands else (selected_remote or
                inspect("config", "--get", f"branch.{branch}.pushRemote", optional=True) or
                inspect("config", "--get", "remote.pushDefault", optional=True) or
                inspect("config", "--get", f"branch.{branch}.remote", optional=True) or "origin")
            names = inspect("remote").splitlines()
            if remote in names:
                urls = inspect("remote", "get-url", "--push", "--all", remote).splitlines()
            else:
                # remote get-url requires a locally configured remote. Resolve
                # literal operands in disposable config containing only the
                # effective URL rules; never edit the selected repository or
                # inherit an unrelated remote's pushurl/conditional includes.
                rules = inspect("config", "-z", "--get-regexp",
                                r"^url\..*\.(insteadof|pushinsteadof)$", optional=True)
                rewrites = [tuple(item.split("\n", 1)) for item in rules.split("\0") if item]
                env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
                env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
                try:
                    with tempfile.TemporaryDirectory(prefix="code-cycle-url-") as scratch:
                        directory = Path(scratch)
                        (directory / "objects").mkdir()
                        (directory / "refs").mkdir()
                        (directory / "HEAD").write_text("ref: refs/heads/main\n")
                        (directory / "config").write_text("[core]\n\tbare = true\n")
                        base = ["git", "--no-pager", "--git-dir=" + scratch]
                        for key, value in [("remote.inspection.url", remote), *rewrites]:
                            done = subprocess.run([*base, "config", "--local", "--add", key, value],
                                cwd=cwd, env=env, capture_output=True, text=True, timeout=30,
                                stdin=subprocess.DEVNULL)
                            if done.returncode:
                                raise PolicyError(f"Git consent URL configuration failed: exit {done.returncode}")
                        done = subprocess.run([*base, "remote", "get-url", "--push", "--all", "inspection"],
                            cwd=cwd, env=env, capture_output=True, text=True, timeout=30,
                            stdin=subprocess.DEVNULL)
                        if done.returncode:
                            raise PolicyError(f"Git consent URL resolution failed: exit {done.returncode}")
                        urls = done.stdout.strip().splitlines()
                except (OSError, subprocess.SubprocessError) as error:
                    raise PolicyError(f"Git consent URL resolution unavailable: {type(error).__name__}") from error
            # The separate inspection consent above covers selected endpoints
            # and repository configuration; inspection never authorizes a push.
            movements = []
            for push_url in urls:
                remote_id = git_remote_identity(push_url, str(git_cwd))
                # The URL is already rewritten. A unique full-prefix mapping
                # pins ls-remote to that endpoint without applying the original
                # rules again, retaining the selected transport/auth settings.
                marker = "code-cycle-inspect:" + os.urandom(16).hex()
                env = dict(os.environ)
                slot = int(env.get("GIT_CONFIG_COUNT", "0"))
                env.update(GIT_CONFIG_COUNT=str(slot + 1))
                env[f"GIT_CONFIG_KEY_{slot}"] = f"url.{push_url}.insteadOf"
                env[f"GIT_CONFIG_VALUE_{slot}"] = marker
                old_refs = dict(line.split("\t", 1)[::-1] for line in inspect(
                    "ls-remote", "--refs", marker, env=env).splitlines())
                specs = operands[1:]
                deleting = "--delete" in rest or any(w.startswith("-") and not w.startswith("--") and "d" in w[1:] for w in rest)
                if "--mirror" in rest:
                    specs = [f"{ref}:{ref}" for ref in local] + [f":{ref}" for ref in old_refs if ref not in local]
                elif any(word in rest for word in ("--all", "--branches")):
                    specs = [f"{ref}:{ref}" for ref in local if ref.startswith("refs/heads/")]
                elif not specs and "--tags" not in rest:
                    specs = inspect("config", "--get-all", f"remote.{remote}.push", optional=True).splitlines()
                    if not specs:
                        default = inspect("config", "--get", "push.default", optional=True) or "simple"
                        if default == "matching":
                            specs = [f"{ref}:{ref}" for ref in local if ref.startswith("refs/heads/") and ref in old_refs]
                        elif default == "nothing":
                            specs = []  # Git itself rejects this configured mode.
                        else:
                            upstream = inspect("config", "--get", f"branch.{branch}.merge", optional=True)
                            upstream_remote = inspect("config", "--get", f"branch.{branch}.remote", optional=True)
                            target = upstream if upstream and default in {"upstream", "tracking", "simple"} and upstream_remote == remote else current
                            specs = [f"{current}:{target}"]
                # `git push <remote> tag <name>` is a refspec shorthand.
                named_specs = []
                index = 0
                while index < len(specs):
                    if specs[index] == "tag" and index + 1 < len(specs):
                        ref = "refs/tags/" + specs[index + 1]
                        named_specs.append(f"{ref}:{ref}")
                        index += 2
                    else:
                        named_specs.append(specs[index])
                        index += 1
                expanded_specs = []
                for spec in named_specs:
                    if spec.lstrip("+") == ":":
                        expanded_specs.extend(f"{ref}:{ref}" for ref in local
                                              if ref.startswith("refs/heads/") and ref in old_refs)
                        continue
                    source, colon, target = spec.lstrip("+").partition(":")
                    if "*" in source:
                        target = target or source
                        prefix, _, suffix = source.partition("*")
                        for ref in local:
                            if ref.startswith(prefix) and ref.endswith(suffix):
                                middle = ref[len(prefix):len(ref) - len(suffix) if suffix else None]
                                expanded_specs.append(f"{ref}:{target.replace('*', middle)}")
                        if "--prune" in rest:
                            tp, _, ts = target.partition("*")
                            for ref in old_refs:
                                if ref.startswith(tp) and ref.endswith(ts):
                                    middle = ref[len(tp):len(ref) - len(ts) if ts else None]
                                    if source.replace("*", middle) not in local:
                                        expanded_specs.append(f":{ref}")
                    else:
                        expanded_specs.append(spec)
                specs = expanded_specs
                if "--tags" in rest:
                    specs += [f"{ref}:{ref}" for ref in local if ref.startswith("refs/tags/")]
                elif "--follow-tags" in rest:
                    for source in [spec.lstrip("+").partition(":")[0] for spec in specs]:
                        if not source:
                            continue
                        for line in inspect("for-each-ref", "--format=%(objecttype) %(refname)",
                                            "--merged=" + source, "refs/tags").splitlines():
                            object_type, ref = line.split(" ", 1)
                            if object_type == "tag" and ref not in old_refs:
                                specs.append(f"{ref}:{ref}")
                specs = list(dict.fromkeys(specs))
                for spec in specs:
                    source, colon, target = spec.lstrip("+").partition(":")
                    if deleting:
                        target, source = source, ""
                    target = target or source
                    if target in {"HEAD", "@"}:
                        target = inspect("symbolic-ref", "HEAD")
                    if not target.startswith("refs/"):
                        if target.startswith(("heads/", "tags/")):
                            target = "refs/" + target
                        else:
                            existing = [ref for ref in ("refs/heads/" + target, "refs/tags/" + target) if ref in old_refs]
                            source_ref = inspect("rev-parse", "--symbolic-full-name", source) if source else ""
                            target = (existing[0] if len(existing) == 1 else
                                      ("refs/tags/" if source_ref.startswith("refs/tags/") else "refs/heads/") + target)
                    new = inspect("rev-parse", "--verify", source) if source else None
                    movements.append({"remote": remote, "remote_identity": remote_id,
                                      "ref": target, "before": old_refs.get(target), "after": new})
            return {"kind": "git_refs", "cycle_only": True, "movements": movements,
                    "publication_unconfined": bool(policy.static_only),
                    "warning": "WARNING: publishes refs, may rewrite history, change a base branch or delete refs. "
                               "Tags, other refs and other remotes are outside publication-audit coverage. "
                               "Consent binds the remote, ref and old/new SHAs for this cycle only."
                               + (" Publication runs without a confirmed sandbox." if policy.static_only else "")}
    if policy.static_only and (program == "git" and words[1:2] == ["push"] or program == "gh"
            and any(word in {"comment", "create", "edit", "review"} for word in words[1:])
            or program == "python3" and tuple(words) in replace(policy, static_only=False).publication_commands):
        return {"kind": "unconfined_publication", "warning":
                "WARNING: publication runs without a confirmed sandbox; configured verification remains separate (#291)."}
    return None


def consented_credential_reads(policy: Any) -> tuple[str, ...]:
    """Only approved stores enter the executor's per-dispatch sandbox settings."""
    return tuple(dict.fromkeys(store for entry in policy.command_permissions
        if entry.get("decision") == "allow"
        and (policy.role in WRITING_ROLES or entry.get("operation", {}).get("role") == policy.role
             and entry.get("operation", {}).get("sensitive", {}).get("cycle_only") is True)
        and (not entry.get("operation", {}).get("sensitive", {}).get("role_bound")
             or entry["operation"].get("role") == policy.role)
        for store in entry.get("operation", {}).get("sensitive", {}).get("stores", [])
        if store in (*CREDENTIAL_STORES, GH_CREDENTIAL_STORE)))


def consented_git_edits(policy: Any) -> tuple[str, ...]:
    if policy.role not in WRITING_ROLES:
        return ()
    return tuple(dict.fromkeys(target
        for entry in policy.command_permissions if entry.get("decision") == "allow"
        and entry.get("operation", {}).get("sensitive", {}).get("kind") == "git_config"
        for target in ([entry["operation"]["pattern"]["target"]]
                       if isinstance(entry.get("operation", {}).get("pattern", {}).get("target"), str)
                       else entry["operation"]["sensitive"].get("targets", []))))


def _read_paths_allowed(operands: list[str], policy: StagePolicy, cwd: str | None) -> bool:
    """Share the ordinary-read boundary with isolated Python inspection."""
    root = Path(cwd or ".").resolve()
    evidence = {Path(path).resolve() for path in policy.evidence_paths}
    for operand in operands:
        path = (root / os.path.expanduser(operand)).resolve()
        if (path != root and root not in path.parents and path not in evidence
                and not inside_tmpdir(str(path), policy.tmpdir)
                and not inside_tool_results(str(path), policy)):
            return False
    return True


def _inspection_python(words: list[str], policy: StagePolicy, cwd: str | None) -> bool:
    """Recognize data inspection, never execute inline code in the validator.

    Only isolated stdlib JSON/argv reads and pure expressions are automatic.
    Other interpreter programs retain operation consent and external isolation.
    """
    if words[:3] != ["python3", "-I", "-c"] or len(words) < 4:
        return False
    try:
        tree = ast.parse(words[3])
    except (SyntaxError, ValueError):
        return False
    nodes = tuple(ast.walk(tree))
    allowed = (ast.Module, ast.Import, ast.alias, ast.Assign, ast.Expr, ast.Call,
               ast.Name, ast.Load, ast.Store, ast.Constant, ast.Attribute,
               ast.Subscript, ast.List, ast.Tuple, ast.Dict, ast.IfExp,
               ast.BoolOp, ast.Or, ast.And, ast.Compare, ast.Eq, ast.NotEq,
               ast.Is, ast.IsNot, ast.UnaryOp, ast.Not)
    builtins = {"print", "list", "tuple", "dict", "str", "type", "isinstance", "len", "open"}
    read_paths = list(words[4:])
    for node in nodes:
        if not isinstance(node, allowed):
            return False
        if isinstance(node, ast.Import) and any(
                alias.name not in {"json", "sys"} or alias.asname for alias in node.names):
            return False
        if isinstance(node, ast.Assign) and any(
                not isinstance(target, ast.Name) or target.id in builtins | {"json", "sys"}
                for target in node.targets):
            return False
        if isinstance(node, ast.Attribute):
            module_attr = (isinstance(node.value, ast.Name) and
                           (node.value.id, node.attr) in {("sys", "argv"), ("json", "load"),
                               ("json", "loads"), ("json", "dumps")})
            data_attr = (node.attr in {"get", "keys", "values", "items"}
                         and isinstance(node.value, ast.Name)
                         and node.value.id not in builtins | {"json", "sys"})
            read_attr = (node.attr == "read" and isinstance(node.value, ast.Call)
                         and isinstance(node.value.func, ast.Name) and node.value.func.id == "open")
            if not (module_attr or data_attr or read_attr):
                return False
        if isinstance(node, ast.Call):
            if node.keywords or not (isinstance(node.func, ast.Attribute) or
                    isinstance(node.func, ast.Name) and node.func.id in builtins):
                return False
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                if not 1 <= len(node.args) <= 2:
                    return False
                if len(node.args) == 2 and not (
                        isinstance(node.args[1], ast.Constant) and node.args[1].value in {"r", "rt"}):
                    return False
                path = node.args[0]
                if not (isinstance(path, ast.Constant) and isinstance(path.value, str) or
                        isinstance(path, ast.Subscript) and isinstance(path.value, ast.Attribute)
                        and isinstance(path.value.value, ast.Name) and path.value.value.id == "sys"
                        and path.value.attr == "argv" and isinstance(path.slice, ast.Constant)
                        and isinstance(path.slice.value, int) and 0 < path.slice.value <= len(words) - 4):
                    return False
                if isinstance(path, ast.Constant) and isinstance(path.value, str):
                    read_paths.append(path.value)
    paths = [node.value for node in nodes if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    return (credential_reason([*paths, *words[4:]], policy, cwd) is None
            and _read_paths_allowed(read_paths, policy, cwd))


def _inspection_shell(command: str, parsed: ParsedCommand, policy: StagePolicy,
                      cwd: str | None, run) -> bool:
    """Only simple read pipelines skip operation consent (owner AC-1, REV-007)."""
    if not _simple_read_source(command, pipelines=True):
        return False
    groups = [group.split() for group in command.split("|")]
    if groups != [segment.words for segment in parsed.segments]:
        return False
    for segment in parsed.segments:
        if segment.redirects:
            return False
        words = segment.words
        if words[:1] == ["rtk"] and policy.rtk:
            words = words[2:] if words[1:2] == ["proxy"] else words[1:]
        # A path such as ./git runs a file, not the read program it is named after.
        if not words or "/" in words[0] or not _ordinary_read_words(words):
            return False
        if credential_reason(words[1:], policy, cwd):
            return False
        _check_words(words, ParsedCommand(words=words), policy, cwd=cwd, run=run)
    return True


def _simple_read_source(command: str, *, pipelines: bool = False) -> bool:
    """Recognize words whose argv is unchanged by Bash, without evaluating it."""
    groups = command.split("|") if pipelines else [command]
    # An inherited SHELLOPTS=keyword (`set -k`) turns any NAME=value or
    # NAME+=value word into an environment assignment and drops it from argv,
    # so such words are not simple even after the command name (REV-008).
    return all(group.strip() and re.fullmatch(r"[A-Za-z0-9_./:@%+,=^ \t-]+", group)
               and not any(re.match(r"[A-Za-z_][A-Za-z0-9_]*\+?=", word) for word in group.split())
               for group in groups)


def _explicit_command_authorization(words: list[str], policy: StagePolicy) -> bool:
    if not policy.static_only and (words[0] in policy.tools or any(
            words[:len(expected)] == expected for item in policy.commands
            if (expected := _parse_simple(item, None).words))):
        return True
    return any(words == expected if item == policy.test_command else words[:len(expected)] == expected
               for item in policy.verification_commands if (expected := item.split()))


def _ordinary_read_words(words: list[str]) -> bool:
    program = _program(words[0])
    if program in READ_SHELL | {"printenv", "sha256sum"}:
        return True
    args = list(words[1:])
    if program == "git":
        while args and args[0] in {"-C", "--no-pager", "-P"}:
            option = args.pop(0)
            if option == "-C" and args:
                args.pop(0)
        return bool(args and (args[0] in GIT_COMMON_READ | GIT_REVIEW_READ
                              or args[:2] in (["branch", "--show-current"], ["worktree", "list"])))
    if program == "gh":
        # Match _check_gh: global repository selectors can appear anywhere.
        remaining = []
        index = 0
        while index < len(args):
            word = args[index]
            if word in {"-R", "--repo"}:
                index += 2
            elif word.startswith("--repo="):
                index += 1
            else:
                remaining.append(word)
                index += 1
        args = remaining
        if args[:2] == ["api", "graphql"]:
            for word in args[2:]:
                if word.startswith(("--raw-field=", "--field=", "-f=", "-F=")):
                    word = word.partition("=")[2]
                if word.startswith("query="):
                    return _GraphQL(word.partition("=")[2]).document()[0] == "query"
            return False
        if args[:1] == ["api"]:
            return True
        return len(args) >= 2 and (args[0] == "search"
                                  or args[1] in {"view", "list", "diff", "checks", "status"})
    return False


def check(command: str, policy: StagePolicy, *, cwd: str | None = None,
          run=subprocess.run) -> Decision:
    """Decide whether this stage may run `command`. Never raises."""
    try:
        sensitive = sensitive_operation("Bash", {"command": command}, policy, cwd,
                                        resolve=False, run=run)
        if sensitive:
            guard = protected_reason("Bash", {"command": command}, policy, cwd)
            if guard is not None:
                return Decision(False, guard, rule="sensitive_guard")
            # Legacy and regex grants cannot authorize these operations. Avoid
            # remote inspection until an exact sensitive decision could match.
            eligible = any(entry.get("operation", {}).get("sensitive")
                           for entry in policy.command_permissions)
            answer = matching_answer("Bash", {"command": command}, policy, cwd) if eligible else None
            if answer == "allow":
                return Decision(True, "allowed by recorded sensitive operation consent")
            return Decision(False, sensitive["warning"], rule="sensitive_consent")
        if matching_answer("Bash", {"command": command}, policy, cwd) == "deny":
            return Decision(False, "the user denied this exact operation; skip it, checks not_verified",
                            rule="user_denial")
        parsed = parse_command(command, policy.tmpdir)
        if parsed.shell_source is not None:
            if _inspection_shell(command, parsed, policy, cwd, run):
                return Decision(True, "allowed read-only shell inspection")
            guard = protected_reason("Bash", {"command": command}, policy, cwd)
            if guard:
                return Decision(False, guard, rule="compound_guard")
            for segment in parsed.segments:
                _, segment_words = command_parts(segment.words)
                if segment_words and _program(segment_words[0]) == "docker" and not policy.docker:
                    raise _DockerDenied("docker needs the docker_host grant")
            raise PolicyError("shell syntax needs operation consent; interpreter internals "
                              "depend on external isolation; each segment retains its guards")
        words = list(parsed.words)
        if words[0] == "rtk":
            if not policy.rtk:
                raise PolicyError("rtk is not installed for this dispatch")
            words = words[2:] if words[1:2] == ["proxy"] else words[1:]
            if not words:
                raise PolicyError("rtk needs a command")
        for _, _, target in parsed.redirects:
            if target is not None and not inside_tmpdir(str(Path(cwd or ".") / target), policy.tmpdir):
                raise PolicyError("output may be redirected only into $TMPDIR")
        protected = credential_reason(words[1:], policy, cwd)
        if protected:
            return Decision(False, protected, rule="credential_guard")
        _check_words(words, parsed, policy, cwd=cwd, run=run)
        if (_ordinary_read_words(words) and not _simple_read_source(command)
                and not _explicit_command_authorization(words, policy)):
            raise ConsentRequired("complex read arguments need operation consent")
    except _DockerDenied as denied:
        return Decision(False, str(denied), needs_docker=True, rule="docker_host")
    except PolicyError as error:
        if (not policy.static_only
                and matching_answer("Bash", {"command": command}, policy, cwd) == "allow"
                and protected_reason("Bash", {"command": command}, policy, cwd) is None):
            return Decision(True, "allowed by exact operation consent")
        unresolved = _unresolved_compose(command, policy, cwd)
        if unresolved:
            return Decision(False, unresolved, rule="compose_unresolved")
        reason = str(error)
        patterns = [entry.get("operation", {}).get("pattern", {}).get("regex")
                    for entry in policy.command_permissions if entry.get("operation", {}).get("version") == 2]
        if patterns:
            try:
                normalized = normalized_command(operation("Bash", {"command": command}, policy, cwd))
                reason += f"; existing patterns {patterns!r} did not authorize full normalized command {normalized!r}"
            except (ValueError, OSError, TypeError):
                pass
        return Decision(False, reason, rule="command_table")
    except Exception as error:  # fail closed on anything unexpected
        return Decision(False, f"the command could not be validated ({type(error).__name__})",
                        rule="validation_error")
    return Decision(True, "allowed by the stage command table")


def _unresolved_compose(command: str, policy: StagePolicy, cwd: str | None) -> str | None:
    """Why a `docker compose` command cannot be identified, or None when it can.

    The table refuses `compose run`, and the answer is an exact consent question.
    That question needs the effective configuration; when `docker compose config`
    fails (a Compose file or `env_file` missing from this checkout) the command has
    no identity, so no answer could ever be stored. The cause is the answer, not the table.
    """
    if not policy.docker or policy.static_only:
        return None
    try:
        words = list(parse_command(command, policy.tmpdir).words)
        _, command_words = command_parts(words)
        if _program(command_words[0]) != "docker" or command_words[1:2] != ["compose"]:
            return None
        with compose_resolution_cache():
            operation("Bash", {"command": command}, policy, cwd)
    except PolicyError as error:
        return str(error)
    except Exception:
        return None
    return None


def _is_verification_command(command, policy: StagePolicy) -> bool:
    """Whether a Bash tool call reliably identifies a configured check."""
    if not isinstance(command, str):
        return False
    try:
        words = list(parse_command(command, policy.tmpdir).words)
        if words[:1] == ["rtk"]:
            words = words[2:] if words[1:2] == ["proxy"] else words[1:]
        if not words:
            return False
        if policy.test_command and words == list(parse_command(policy.test_command, None).words):
            return True
        for prefix in policy.verify_prefixes:
            prefix_words = list(parse_command(prefix, None).words)
            if words[:len(prefix_words)] == prefix_words:
                return True
        return False
    except (PolicyError, IndexError):
        return False


def check_bash_tool_input(tool_input: dict, policy: StagePolicy, *,
                          cwd: str | None = None) -> Decision:
    """Apply Claude -p foreground and verification-timeout safeguards."""
    command = tool_input.get("command")
    if tool_input.get("run_in_background") is True:
        return Decision(False, "Bash commands must run in the foreground", rule="foreground_policy")
    timeout = tool_input.get("timeout")
    if (_is_verification_command(command, policy)
            and isinstance(timeout, int) and not isinstance(timeout, bool)
            and timeout < CLAUDE_VERIFICATION_TIMEOUT_MS):
        return Decision(
            False,
            "configured verification needs a timeout of at least "
            f"{CLAUDE_VERIFICATION_TIMEOUT_MS} ms",
            rule="verification_timeout",
        )
    if not policy.active:
        return Decision(True, "foreground policy only")
    return check(command, policy, cwd=cwd)


#: Claude's file-editing tools and the input key that names their target.
FILE_EDIT_TOOLS ={"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
                   "NotebookEdit": "notebook_path"}


def _git_config_paths() -> tuple[Path, ...]:
    home = Path.home()
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return home / ".gitconfig", Path(xdg) / "git"


def check_file_edit(tool_name: str, tool_input, policy: StagePolicy, *,
                    cwd: str | None = None) -> Decision:
    """Decide whether this stage may edit a file with a non-Bash tool. Never raises.

    A non-writing stage edits only inside its own `$TMPDIR`, where its skills
    write the comment body they publish with `--body-file` (#272). Writing
    stages need recorded exact consent to edit Git's configuration or `.git`:
    settings such as `core.fsmonitor` can make later allowed reads run code.
    """
    try:
        sensitive = sensitive_operation(tool_name, tool_input or {}, policy, cwd)
        if sensitive:
            answer = matching_answer(tool_name, tool_input or {}, policy, cwd)
            if answer == "allow" and protected_reason(tool_name, tool_input or {}, policy, cwd) is None:
                return Decision(True, "allowed by recorded sensitive operation consent")
            return Decision(False, sensitive["warning"], rule="sensitive_consent")
        path = (tool_input or {}).get(FILE_EDIT_TOOLS[tool_name])
        if not isinstance(path, str) or not path.strip():
            raise PolicyError("the edit names no file")
        protected = credential_reason([path], policy, cwd)
        if protected:
            return Decision(False, protected, rule="credential_guard")
        if matching_answer(tool_name, tool_input or {}, policy, cwd) == "deny":
            return Decision(False, "the user denied this exact edit; skip it, checks not_verified",
                            rule="user_denial")
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
        if (not policy.static_only
                and matching_answer(tool_name, tool_input or {}, policy, cwd) == "allow"
                and protected_reason(tool_name, tool_input or {}, policy, cwd) is None):
            return Decision(True, "allowed by exact operation consent")
        return Decision(False, str(error), rule="edit_rule")
    except Exception as error:  # fail closed on anything unexpected
        return Decision(False, f"the edit could not be validated ({type(error).__name__})",
                        rule="validation_error")
    return Decision(True, "allowed by the stage edit rule")


class _DockerDenied(PolicyError):
    pass


def _check_words(words: list[str], parsed: ParsedCommand, policy: StagePolicy, *,
                 cwd: str | None, run) -> None:
    environment, command_words = split_environment(words)
    if environment:
        # `NAME=value program ...`: the prefix changes what the program does, so
        # the command is never a table entry; exact consent may still cover it.
        # Docker keeps its separate socket permission first (#398).
        if command_words and _program(command_words[0]) == "docker":
            if policy.static_only:
                raise PolicyError("native isolation unavailable: code-executing checks are not_verified")
            if not policy.docker:
                raise _DockerDenied("docker needs the docker_host grant, which this "
                                    "dispatch does not have")
        raise PolicyError("a command with environment prefixes needs exact operation consent")
    # User authorization never changes the publication or Git invariants.
    guarded_program = os.path.basename(words[0]).removesuffix(".exe")
    if guarded_program == "git":
        return _check_git(words[1:], policy, cwd=cwd, run=run)
    if guarded_program == "gh":
        return _check_gh(words[1:], policy, cwd=cwd, run=run)
    if guarded_program == "docker":
        if policy.static_only:
            raise PolicyError("native isolation unavailable: code-executing checks are not_verified")
        return _check_docker(words[1:], policy)
    if words[0] in policy.tools and not policy.static_only:
        return
    # The permission view (#311): exactly the runtime's command, no arguments
    # of the stage's own, so the repository and branch are never its choice.
    if policy.permissions_command and tuple(words) == policy.permissions_command:
        if parsed.redirects:
            raise PolicyError("the permissions command may not redirect its output")
        return
    if tuple(words) in policy.publication_commands:
        if parsed.redirects:
            raise PolicyError("the publication command may not redirect its evidence")
        return
    if (policy.role in {"review", "rereview", "resolve"} and policy.tmpdir
            and len(words) == 3
            and words[:2] == ["python3", policy.triage_validator_path]
            and Path(words[2]).name == "pr-comment.md"
            and is_tmpdir_helper(words[2], policy.tmpdir)):
        return
    for command in (() if policy.static_only else policy.commands):
        expected = _parse_simple(command, None).words
        if words[:len(expected)] == expected:
            return
    for configured in policy.verification_commands:
        expected = configured.split()
        exact = configured == policy.test_command
        if words == expected or (not exact and words[:len(expected)] == expected):
            return
    program = words[0]
    if _inspection_python(words, policy, cwd):
        raise ConsentRequired("inline Python inspection needs operation consent")
    if program == "sha256sum":
        if policy.role not in REVIEW_ROLES:
            raise PolicyError("sha256sum is available only in review stages")
        operands = words[1:]
        if not operands or any(word.startswith("-") for word in operands):
            raise PolicyError("sha256sum accepts evidence file paths only; options are not allowed")
        evidence = {Path(path).resolve() for path in policy.evidence_paths}
        if any(Path(path).resolve() not in evidence for path in operands):
            raise PolicyError("sha256sum may read only runtime-supplied evidence files")
        return
    if program == "printenv":
        if words[1:] and set(words[1:]) <= {"GIT_SSH_COMMAND", "GIT_SSH"} and not parsed.redirects:
            return
        if policy.tmpdir and words == ["printenv", "TMPDIR"] and not parsed.redirects:
            return
        raise PolicyError("only `printenv TMPDIR` is allowed to inspect the dispatch temp path")
    if program in READ_SHELL:
        if parsed.redirects:
            raise PolicyError(f"{program} may not redirect its output")
        if program == "find":
            for word in words[1:]:
                if word in FIND_FORBIDDEN:
                    raise PolicyError(f"find {word} runs, deletes or writes; it is not allowed")
        elif program == "sed":
            check_sed(words[1:])
        elif program == "date":
            check_date(words[1:])
        elif program == "ps":
            check_ps(words[1:])
        if cwd and not _read_paths_allowed(read_operands(words), policy, cwd):
            raise PolicyError("reading outside the stage workspace needs operation consent")
        return
    raise PolicyError(f"{program!r} is not in the {policy.role} stage's command table")


def _sed_parts(args: list[str]) -> tuple[list[str], list[str]]:
    """The scripts and file operands of a read-only `sed`; refuse anything else.

    Only `-n`, `-E`, `-r`, `-s`, `-z` and `-e`/`--expression` are read. `-i`,
    `--in-place`, `-f`, `--file` and every unknown option are refused, and a
    `sed` without a file would read the stage's standard input, so it needs one.
    """
    scripts: list[str] = []
    positional: list[str] = []
    options = True
    index = 0
    while index < len(args):
        word = args[index]
        index += 1
        if options and word == "--":
            options = False
        elif options and word.startswith("-") and word != "-":
            expression = re.fullmatch(r"-([nErsz]*)e(.*)", word, re.DOTALL)
            if word in _SED_FLAGS or re.fullmatch(r"-[nErsz]+", word):
                continue
            if expression:
                if expression.group(2):
                    scripts.append(expression.group(2))
                elif index < len(args):
                    scripts.append(args[index])
                    index += 1
                else:
                    raise PolicyError("sed -e needs a script")
            elif word.startswith("--expression="):
                scripts.append(word.partition("=")[2])
            elif word == "--expression" and index < len(args):
                scripts.append(args[index])
                index += 1
            else:
                raise PolicyError(f"sed option {word} writes, runs a program or reads a script "
                                  "file; it is not allowed")
        else:
            positional.append(word)
    # GNU sed permutes its options: once any `-e`/`--expression` appears, every
    # non-option word is an input file wherever it stands; without one, the
    # first non-option word is the script. Deciding after the whole line keeps
    # `sed -n /tmp/p -e 1p README.md` from hiding `/tmp/p` as a script.
    if scripts:
        files = positional
    else:
        scripts, files = positional[:1], positional[1:]
    if not scripts:
        raise PolicyError("sed needs a script")
    if not files or "-" in files:
        raise PolicyError("sed needs a file operand; it never reads the stage's standard input")
    return scripts, files


def _skip_sed_regex(script: str, index: int, delimiter: str, brackets: bool) -> int:
    """The index after the regex or replacement that starts at `index`."""
    size = len(script)
    while index < size:
        char = script[index]
        if char == "\\":
            index += 2
            continue
        if char == "\n" and delimiter != "\n":
            raise PolicyError("sed script: a newline inside a pattern is not allowed")
        if brackets and char == "[":
            index += 1
            if index < size and script[index] == "^":
                index += 1
            if index < size and script[index] == "]":
                index += 1
            while index < size and script[index] != "]":
                if script[index] == "[" and script[index + 1:index + 2] in {":", ".", "="}:
                    closing = script.find(script[index + 1] + "]", index + 2)
                    if closing < 0:
                        raise PolicyError("sed script: unterminated bracket expression")
                    index = closing + 2
                else:
                    index += 1
            if index >= size:
                raise PolicyError("sed script: unterminated bracket expression")
            index += 1
            continue
        if char == delimiter:
            return index + 1
        index += 1
    raise PolicyError("sed script: unterminated pattern")


def _sed_commands(script: str, brackets: bool) -> list[str]:
    """The command letters of a read-only sed script, or a refusal."""
    commands: list[str] = []
    size, index = len(script), 0

    def address(position: int, second: bool) -> int:
        if position < size and script[position] == "$":
            return position + 1
        if position < size and script[position] == "/":
            position = _skip_sed_regex(script, position + 1, "/", brackets)
            while position < size and script[position] in "IM":
                position += 1
            return position
        if second and position < size and script[position] in "+~":
            position += 1
        digits = re.compile(r"\d+").match(script, position)
        if not digits:
            raise PolicyError("sed script: an address is a line number, $ or /regex/")
        position = digits.end()
        step = re.compile(r"~\d+").match(script, position)
        return step.end() if step and not second else position

    while True:
        while index < size and script[index] in " \t\n;":
            index += 1
        if index >= size:
            break
        if script[index] in "$/0123456789":
            index = address(index, False)
            if index < size and script[index] == ",":
                index = address(index + 1, True)
        while index < size and script[index] in " \t!":
            index += 1
        if index >= size:
            raise PolicyError("sed script: an address needs a command")
        command = script[index]
        index += 1
        if command == "s":
            if index >= size or script[index] in "\\\n \t;" or script[index].isalnum():
                raise PolicyError("sed script: s needs a plain delimiter")
            delimiter = script[index]
            index = _skip_sed_regex(script, index + 1, delimiter, brackets)
            index = _skip_sed_regex(script, index, delimiter, False)
            while index < size and script[index] not in " \t\n;":
                if script[index] not in _SED_S_FLAGS:
                    raise PolicyError(f"sed script: the s flag {script[index]!r} may write a file "
                                      "or run a command")
                index += 1
        elif command in _SED_COMMANDS:
            if command in "qQl":
                digits = re.compile(r"\d*").match(script, index)
                index = digits.end() if digits else index
        else:
            raise PolicyError(f"sed command {command!r} writes, reads a file, runs a command "
                              "or is outside the read-only set")
        while index < size and script[index] in " \t":
            index += 1
        if index < size and script[index] not in "\n;":
            raise PolicyError("sed script: unexpected text after a command")
        commands.append(command)
    return commands


def check_sed(args: list[str]) -> None:
    """Allow `sed` only in its read-only forms (#464); raise PolicyError otherwise.

    The script is read twice, with and without bracket expressions as sed's
    own parsers differ on a delimiter inside `[...]`; a script the two readings
    disagree on is refused.
    """
    if not args or args[0] not in SED_FIRST_OPTIONS:
        raise PolicyError("sed is allowed only when it starts with -n, -E or -e "
                          "(prefixes: " + ", ".join(f"sed {option}" for option in SED_FIRST_OPTIONS)
                          + ")")
    scripts, _ = _sed_parts(args)
    for script in scripts:
        if _sed_commands(script, True) != _sed_commands(script, False):
            raise PolicyError("sed script: ambiguous bracket expression is not allowed")


_DATE_FLAGS = frozenset({"-u", "--utc", "--universal", "-R", "--rfc-email", "--rfc-822",
                         "--rfc-2822"})


def check_date(args: list[str]) -> None:
    """Allow `date` to print a time, never to set one or read a file (#464)."""
    format_seen = False
    index = 0
    while index < len(args):
        word = args[index]
        index += 1
        if word.startswith("+"):
            if format_seen:
                raise PolicyError("date takes one +FORMAT")
            format_seen = True
        elif (word in _DATE_FLAGS
              or re.fullmatch(r"-I(date|hours|minutes|seconds|ns)?", word)
              or re.fullmatch(r"--iso-8601(=(date|hours|minutes|seconds|ns))?", word)
              or re.fullmatch(r"--rfc-3339=(date|seconds|ns)", word)
              or re.fullmatch(r"--date=.+", word, re.DOTALL)):
            continue
        elif word in {"-d", "--date"} and index < len(args):
            index += 1
        else:
            raise PolicyError(f"date {word} sets the clock, reads a file or is not a "
                              "read-only form; it is not allowed")


def _check_ps_fields(value: str) -> None:
    for item in re.split(r"[,\s]+", value.strip()):
        if item and item.partition("=")[0].lower() not in PS_SAFE_FIELDS:
            raise PolicyError(f"ps field {item.partition('=')[0]!r} can show a command line or "
                              "environment; it is not allowed")


def check_ps(args: list[str]) -> None:
    """Allow `ps` to list processes with fields that expose no arguments (#464)."""
    index = 0
    while index < len(args):
        word = args[index]
        index += 1
        if word in _PS_SELECT_FLAGS:
            continue
        if re.fullmatch(r"-[eAa]*o", word) and index < len(args):
            _check_ps_fields(args[index])
            index += 1
        elif word.startswith("--format="):
            _check_ps_fields(word.partition("=")[2])
        elif word in {"--format"} and index < len(args):
            _check_ps_fields(args[index])
            index += 1
        elif word in {"-p", "--pid"} and index < len(args) and re.fullmatch(
                r"\d+(,\d+)*", args[index]):
            index += 1
        elif re.fullmatch(r"--pid=\d+(,\d+)*", word):
            continue
        else:
            raise PolicyError(f"ps {word} can show a command line or environment, or is not "
                              "a read-only form; it is not allowed")


def read_operands(words: list[str]) -> list[str]:
    """File operands of the read tools, excluding filters and option data.

    This is path classification, not a replacement for external read isolation.
    """
    program, args = words[0], words[1:]
    if program in {"date", "ps"}:
        return []
    if program == "sed":
        return _sed_parts(args)[1]
    paths: list[str] = []
    pattern_seen = program not in {"grep", "jq"}
    find_roots = True
    options = True
    index = 0
    while index < len(args):
        word = args[index]
        index += 1
        if options and word == "--":
            options = False
            continue
        if program == "find":
            if find_roots and (word in {"-H", "-L", "-P"} or re.fullmatch(r"-O[0-3]", word)):
                continue
            if find_roots and word == "-D":
                index += 1
                continue
            if (word in {"-f", "-files0-from", "-newer", "-anewer", "-cnewer", "-samefile"}
                    or re.fullmatch(r"-newer[aBcm][aBcm]", word)):
                if index < len(args):
                    paths.append(args[index])
                    index += 1
                find_roots &= word == "-f"
            elif word.startswith("-") or word in {"!", "(", ")"}:
                find_roots = False
            elif find_roots:
                paths.append(word)
            continue
        if options and word.startswith("-") and word != "-":
            name, separator, value = word.partition("=")
            count, path_value = 0, False
            if program == "grep":
                if name in {"--regexp", "--file", "--exclude-from"}:
                    pattern_seen |= name != "--exclude-from"
                    count, path_value = 1, name != "--regexp"
                elif name in {"--after-context", "--before-context", "--context", "--max-count",
                              "--include", "--exclude", "--exclude-dir", "--binary-files",
                              "--directories", "--devices", "--label"}:
                    count = 1
                elif not word.startswith("--"):
                    for offset, letter in enumerate(word[1:], 2):
                        if letter in "efABCm":
                            count, path_value = 1, letter == "f"
                            pattern_seen |= letter in "ef"
                            value = word[offset:]
                            separator = "=" if value else ""
                            break
            elif program == "jq":
                if name in {"--arg", "--argjson", "--slurpfile", "--rawfile"}:
                    count, path_value = 2, name in {"--slurpfile", "--rawfile"}
                elif name in {"-f", "--from-file"}:
                    pattern_seen = True
                    count, path_value = 1, True
                elif name in {"-L", "--indent"}:
                    count, path_value = 1, name == "-L"
                elif word.startswith(("-f", "-L")):
                    pattern_seen |= word.startswith("-f")
                    count, path_value, separator, value = 1, True, "=", word[2:]
            elif program in {"head", "tail"} and name in {"-n", "-c", "--lines", "--bytes", "--sleep-interval", "--pid"}:
                count = 1
            elif program == "wc" and name == "--files0-from":
                count, path_value = 1, True
            if count:
                values = ([value] if separator else []) + args[index:index + count - bool(separator)]
                index += count - bool(separator)
                if path_value and len(values) == count:
                    paths.append(values[-1])
            continue
        if not pattern_seen:
            pattern_seen = True
        elif word != "-":
            paths.append(word)
    return paths


def _git_global_options(args: list[str]) -> tuple[list[str] | None, list[str]]:
    """Return the command and safe selectors; None means opaque option arity."""
    args = list(args)
    selection: list[str] = []
    # Git's handle_options, including older --super-prefix. Never guess that
    # an unknown option is valueless or name its value as the subcommand.
    value_options = {"-c", "-C", "--git-dir", "--work-tree", "--namespace",
                     "--config-env", "--attr-source", "--shallow-file", "--super-prefix"}
    flags = {"--no-pager", "-P", "--paginate", "-p", "--bare",
             "--no-replace-objects", "--no-lazy-fetch", "--no-optional-locks", "--no-advice",
             "--literal-pathspecs", "--no-literal-pathspecs", "--glob-pathspecs",
             "--noglob-pathspecs", "--icase-pathspecs"}
    while args and args[0].startswith("-"):
        option = args.pop(0)
        if option in value_options:
            if not args:
                raise PolicyError(f"git {option} needs a value")
            value = args.pop(0)
            if option in {"-C", "--git-dir", "--work-tree", "--namespace"}:
                selection.extend((option, value))
        elif option.startswith(("--git-dir=", "--work-tree=", "--namespace=")) or option == "--bare":
            selection.append(option)
        elif (option in flags or option.startswith("--exec-path=")
              or "=" in option and option.partition("=")[0] in value_options):
            pass
        elif (option in {"--version", "-v", "--help", "-h", "--html-path", "--man-path", "--info-path"}
              or option.startswith("--list-cmds=") or option.startswith("--exec-path")):
            # Git exits or dispatches help/version before later words.
            return [], selection
        else:
            return None, selection
    return args, selection


def _check_git(args: list[str], policy: StagePolicy, *, cwd: str | None, run,
               consent: bool = False, directory_consent: bool = False) -> None:
    if not consent:
        selected = list(args)
        directory = Path(cwd or ".")
        changed_directory = False
        while selected and selected[0] in {"-C", "--no-pager", "-P"}:
            option = selected.pop(0)
            if option == "-C":
                changed_directory = True
                if not selected:
                    raise PolicyError("git -C needs a directory")
                directory = (directory / selected.pop(0)).resolve()
                root = Path(cwd or ".").resolve()
                if (directory != root and root not in directory.parents and not directory_consent
                        and not _explicit_command_authorization(["git", *args], policy)):
                    raise ConsentRequired("git -C outside the workspace needs operation consent")
        if selected != args:
            if changed_directory and (not selected or selected[0] not in GIT_COMMON_READ | GIT_REVIEW_READ):
                raise PolicyError("Git directory selection is automatic only for read commands")
            protected = credential_reason([str(directory), *selected[1:]], policy, str(directory))
            if protected:
                raise PolicyError(protected)
            args = selected
            cwd = str(directory)
    # Known syntax keeps the direct guards. Unknown syntax is disclosed opaque
    # interpreter consent, never a guessed command or an extra toolkit veto.
    if consent:
        command_args, selection = _git_global_options(args)
        if command_args is None or not command_args:
            return
        args = command_args
        original_run = run
        def selected_run(command, **kwargs):
            # Inspection runs outside the sandbox. Never forward -c, --paginate
            # or --exec-path: consent authorizes execution inside the executor,
            # not a pager or alias in the host-side guard.
            return original_run([command[0], "--no-pager", *selection, *command[1:]], **kwargs)
        run = selected_run
    while args and args[0] == "--no-pager":
        args = args[1:]
    if not args or args[0].startswith("-"):
        raise PolicyError("git global options other than --no-pager are not allowed")
    subcommand, rest = args[0], args[1:]
    read_form = (subcommand == "stash" and rest[:1] in (["list"], ["show"]) or
                 subcommand == "tag" and (not rest or rest[:1] in (["--list"], ["-l"])) or
                 subcommand == "config" and rest[:1] in (["--get"], ["--get-all"], ["--list"], ["-l"]))
    if (consent and policy.role not in WRITING_ROLES
            and not read_form
            and subcommand not in GIT_COMMON_READ | GIT_REVIEW_READ | {
                "ls-tree", "ls-remote", "branch", "worktree", "fetch"}):
        raise PolicyError("this role may not modify the Git workspace")
    if not consent and subcommand in {"branch", "tag", "update-ref"} and any(
            word == "--delete" or word.startswith("--delete=") or
            word.startswith("-") and not word.startswith("--") and
            any(letter in word[1:] for letter in "dD") for word in rest):
        raise PolicyError("deletion of Git refs is never allowed")
    if subcommand == "push" and policy.static_only and not consent:
        raise PolicyError("publication requires the stage sandbox; no host-side push fallback")
    allowed = set(GIT_COMMON_READ)
    if policy.role in GIT_SEARCH_ROLES:
        allowed |= GIT_REVIEW_READ
    if policy.role in WRITING_ROLES:
        allowed |= GIT_WRITE
    if consent:
        allowed.add("ls-remote")
    if subcommand == "branch":
        if rest != ["--show-current"]:
            listing = not rest or all(word in {"--list", "--all", "-a", "--remotes", "-r",
                                               "--verbose", "-v", "-vv", "--no-color"} for word in rest)
            if not consent or policy.role not in WRITING_ROLES and not listing:
                raise PolicyError("git branch is allowed only as git branch --show-current")
        if not consent:
            return
    if subcommand == "worktree" and policy.role in WRITING_ROLES:
        # The implementer inspects linked worktrees; it never creates one.
        if rest not in (["list"], ["list", "--porcelain"]):
            if not consent:
                raise PolicyError("git worktree is allowed only as git worktree list")
        if not consent:
            return
    if consent and subcommand == "worktree" and policy.role not in WRITING_ROLES:
        if rest not in (["list"], ["list", "--porcelain"]):
            raise PolicyError("this role may only inspect linked worktrees")
    if policy.role in REVIEW_ROLES and subcommand == "fetch":
        if not policy.fetch_origin:
            raise PolicyError("this review workspace has no origin remote to fetch")
        if not rest or rest[0] != "origin" or any(word.startswith("-") for word in rest):
            raise PolicyError("a review may only run git fetch origin [<ref>...]")
        return
    if (subcommand == "merge" and policy.code_host == "github"
            and policy.role == "resolve"):
        _check_merge(rest, policy, cwd=cwd, run=run)
        return
    if subcommand not in allowed and not consent:
        raise PolicyError(f"git {subcommand} is not in the {policy.role} stage's command table")
    if subcommand == "ls-remote" and not consent:
        positional = []
        options = True
        for word in rest:
            if options and word == "--":
                options = False
            elif options and word.startswith("-"):
                if word not in {"--heads", "--tags", "--refs", "--quiet", "--symref",
                                "--exit-code", "--get-url", "-h", "-t", "-q"}:
                    raise PolicyError(f"git ls-remote option {word} retains its argument guard")
            else:
                positional.append(word)
        if not positional or positional[0] != "origin":
            raise PolicyError("git ls-remote may use only the origin remote")
    for word in (() if consent else rest):
        if (any(_names_git_option(word, option) for option in GIT_EXECUTING_OPTIONS)
                or word.startswith("-O")
                # `git grep -O` runs a pager; short options bundle, as in `-iO<cmd>`.
                or (subcommand == "grep" and word.startswith("-")
                    and not word.startswith("--") and "O" in word)):
            raise PolicyError(f"git option {word} runs another program")
    for index, word in enumerate(() if consent else rest):
        if not _names_git_option(word, "--output"):
            continue
        if "=" in word:
            target = word.split("=", 1)[1]
        else:
            target = rest[index + 1] if index + 1 < len(rest) else ""
        if not inside_tmpdir(target, policy.tmpdir):
            raise PolicyError("git --output may write only into $TMPDIR")
    if subcommand == "push":
        _check_push(rest, policy, cwd=cwd, run=run, consent=consent)
    elif subcommand == "pull" and policy.code_host == "github" and not consent:
        _check_pull(rest, policy)
    elif subcommand in {"fetch", "pull"} and not consent:
        remotes = [word for word in rest if not word.startswith("-")]
        if remotes and remotes[0] != "origin":
            raise PolicyError(f"git {subcommand} may use only the origin remote")


_PLAIN_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")


def _check_pull(args: list[str], policy: StagePolicy) -> None:
    """Bind pulls to the live PR refs and neutralize pull.rebase config."""
    if not policy.change_request_id or not policy.base_ref or not policy.head_ref:
        raise PolicyError("git pull needs a verified open GitHub change request")
    if len(args) != 3 or args[0] != "--no-rebase" or args[1] != "origin":
        raise PolicyError("GitHub git pull is allowed only as git pull --no-rebase origin <PR-ref>")
    ref = args[2]
    if (not _PLAIN_BRANCH.fullmatch(policy.base_ref)
            or not _PLAIN_BRANCH.fullmatch(policy.head_ref)
            or ref not in {policy.base_ref, policy.head_ref}):
        raise PolicyError("git pull may name only the verified PR head or base branch")


def _check_merge(args: list[str], policy: StagePolicy, *, cwd: str | None, run) -> None:
    """Allow a resolver to merge only the live PR base, or finish that merge."""
    if not policy.change_request_id or not policy.base_ref:
        raise PolicyError("git merge needs a verified open GitHub change request")
    if not _PLAIN_BRANCH.fullmatch(policy.base_ref):
        raise PolicyError("the verified pull request base is not a plain branch name")
    if args in (["--abort"], ["--continue"]):
        try:
            result = run(["git", "rev-parse", "--quiet", "--verify", "MERGE_HEAD"],
                         cwd=cwd, capture_output=True, text=True, timeout=10,
                         stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as error:
            raise PolicyError(f"could not check merge state ({type(error).__name__})") from error
        if result.returncode:
            raise PolicyError("git merge --abort/--continue is allowed only during a merge")
        return
    options = [word for word in args if word.startswith("-")]
    refs = [word for word in args if not word.startswith("-")]
    if (len(options) != len(set(options))
            or any(option not in {"--no-edit", "--no-ff"} for option in options)
            or refs != [f"origin/{policy.base_ref}"]):
        raise PolicyError("git merge may merge only origin/<PR-base> with --no-edit/--no-ff")


def _names_git_option(word: str, option: str) -> bool:
    """Whether `word` is the long `option`, in full or as a prefix Git expands to it."""
    name = word.split("=", 1)[0]
    return name.startswith("--") and len(name) > 2 and option.startswith(name)


def _check_push(args: list[str], policy: StagePolicy, *, cwd: str | None, run,
                consent: bool = False) -> None:
    if consent:
        # The rebuilt command has lost its runner, variables and global
        # options, so never classify it as the consent-free working-branch push.
        detail = sensitive_operation("Bash", {"command": shlex.join(["git", "push", *args])},
                                     policy, cwd, resolve=False, run=run, _working_branch=False)
        if detail and detail["kind"] == "git_refs":
            return  # These guards now require warned, bound exact consent.
    positional: list[str] = []
    index = 0
    while index < len(args):
        word = args[index]
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            if consent and any(_names_git_option(word, option) for option in ("--receive-pack", "--exec")):
                index += 1 if "=" in word else 2
                continue
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
        raise PolicyError("git push needs an explicit branch, such as git push origin HEAD:<branch>")
    # The usual default names stay protected even when the base is another.
    protected = {"main", "master"} | ({policy.base_branch} if policy.base_branch else set())
    for refspec in refspecs:
        if refspec.startswith("+"):
            raise PolicyError("a +refspec forces the push; it is never allowed")
        source, separator, destination = refspec.partition(":")
        if not source:
            raise PolicyError("an empty source deletes the remote ref; it is never allowed")
        if not separator and source not in {"HEAD", "@"}:
            # Git resolves an unqualified source like rev-parse does, tags
            # first: only a local branch keeps the destination in refs/heads/.
            expected = source if source.startswith("refs/heads/") else "refs/heads/" + source
            if _full_ref_name(source, cwd, run) != expected:
                raise PolicyError(f"git push source {source!r} is not only a local branch; "
                                  "use git push origin HEAD:<branch>")
        destination = destination or source
        if (destination.startswith("refs/") and not destination.startswith("refs/heads/")
                or destination.startswith("tags/")):
            raise PolicyError("git push destination needs branch publication or warned exact ref consent")
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
        if not separator and _push_destination_remapped(cwd, run):
            # remote.origin.push or push.default=upstream|tracking can send a
            # colon-less refspec to another branch, such as the base it tracks.
            raise PolicyError("Git configuration can remap this push destination; "
                              "use git push origin HEAD:<branch>")


def _push_destination_remapped(cwd: str | None, run) -> bool:
    """Whether the cycle worktree's Git configuration may remap a bare refspec."""
    values = []
    for key in ("remote.origin.push", "push.default"):
        try:
            completed = run(["git", "config", "--get-all", key], cwd=cwd, capture_output=True,
                            text=True, timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return True
        if completed.returncode not in {0, 1}:
            return True
        values.append((completed.stdout or "").strip() if completed.returncode == 0 else "")
    mapping, defaults = values
    # Several configuration files may set push.default; treat any remapping
    # value as decisive rather than guessing which one Git applies last.
    return bool(mapping) or any(line.strip().casefold() in {"upstream", "tracking"}
                                for line in defaults.splitlines())


def _full_ref_name(name: str, cwd: str | None, run) -> str | None:
    """The full ref an unqualified name resolves to in the cycle worktree."""
    if name.startswith("-"):
        return None
    try:
        completed = run(["git", "rev-parse", "--symbolic-full-name", "--verify", "--quiet", name],
                        cwd=cwd, capture_output=True, text=True, timeout=10,
                        stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return (completed.stdout or "").strip() if completed.returncode == 0 else None


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
                  "security", "bootstrap", "coordinate", "verify"})
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

#: The resolve stage's read of one failed check run's annotations (#306): the
#: literal path, for the configured repository and a numeric ID, nothing else.
_ANNOTATIONS_PATH = re.compile(
    r"repos/([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+)/check-runs/([1-9][0-9]{0,19})/annotations")
_SAFE_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+")
_NUMERIC_ID = re.compile(r"[1-9][0-9]{0,19}")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
#: `gh run view` options that take a value, so their value is not the run ID.
_RUN_VIEW_JOB = ("--job", "-j")
_RUN_VIEW_LOGS = ("--log", "--log-failed")


def _same_repo(value: str, repo: str | None) -> bool:
    return bool(repo) and value.lower().removesuffix(".git") == repo.lower()


def _comments_api_read(args: list[str], policy: StagePolicy) -> bool:
    """GET comments in this repository; fields/input never imply a POST."""
    remaining = []
    index = 0
    while index < len(args):
        word = args[index]
        if word == "--paginate":
            index += 1
            continue
        if word in {"--jq", "-q", "--method", "-X"}:
            if index + 1 >= len(args):
                return False
            if word in {"--method", "-X"} and args[index + 1] != "GET":
                return False
            index += 2
            continue
        if word.startswith(("--jq=", "--method=", "-X")):
            if word.startswith("--method=") and word != "--method=GET":
                return False
            if word.startswith("-X") and word != "-XGET":
                return False
            index += 1
            continue
        remaining.append(word)
        index += 1
    if len(remaining) != 1 or not policy.repo:
        return False
    return re.fullmatch(r"repos/" + re.escape(policy.repo) +
                        r"/issues/[1-9][0-9]*/comments", remaining[0]) is not None


def _check_gh(args: list[str], policy: StagePolicy, *, cwd: str | None, run,
              consent: bool = False) -> None:
    bare = []
    index = 0
    while index < len(args):
        word = args[index]
        if word in {"-R", "--repo", "--hostname"}:
            index += 2
            continue
        if not word.startswith(("--repo=", "--hostname=")):
            bare.append(word)
        index += 1
    if bare[:1] == ["auth"]:
        # Authentication status without tokens is a read. Credential output
        # and mutations retain their guard even after exact consent (#456).
        if bare[1:2] != ["status"] or any(
                word.startswith("--show-token") or
                word.startswith("-") and not word.startswith("--") and "t" in word[1:]
                for word in bare[2:]):
            raise PolicyError("gh auth credential disclosure and mutations retain the credential guard")
    if consent:
        # The table is a default, not a veto. Keep the stage's artefact and
        # publication contracts before permitting an exact exception (#456).
        key = tuple(bare[:2])
        if key in {("pr", "merge"), ("pr", "close"), ("issue", "close")}:
            raise PolicyError("the cycle never merges or closes provider artefacts")
        if key == ("pr", "review"):
            # Approval and another role's review remain outside this contract.
            return _check_gh(args, policy, cwd=cwd, run=run)
        if key == ("pr", "create") and policy.role != "implement":
            raise PolicyError("only implement opens the cycle's change request")
        if key == ("pr", "edit") and policy.role != "implement":
            raise PolicyError("only implement edits the change-request summary")
        if bare[:1] == ["api"]:
            if bare[1:2] == ["graphql"]:
                if policy.role != "resolve":
                    raise PolicyError("only resolve may mutate review threads")
                return _check_thread_call(bare[2:], policy, cwd=cwd, run=run)
            methods = [word.partition("=")[2] if "=" in word else
                       (bare[i + 1] if i + 1 < len(bare) else "")
                       for i, word in enumerate(bare)
                       if word in {"-X", "--method"} or word.startswith("--method=")]
            methods.extend(word[2:] for word in bare if word.startswith("-X") and word != "-X")
            fields = any(word in {"-f", "-F", "--field", "--raw-field", "--input"}
                         or word.startswith(("--field=", "--raw-field=", "--input=", "-f", "-F"))
                         for word in bare[1:])
            read = (not methods and not fields or methods == ["GET"])
            if not read:
                # Arbitrary API bodies can close or merge a PR. Keep the
                # concrete publication guard; named CLI writes below disclose
                # their target and effect without inspecting opaque payloads.
                raise PolicyError("provider API writes must use an attributed CLI operation")
            return
        read = len(bare) >= 2 and (bare[1] in {
            "view", "list", "diff", "checks", "status"} or bare[0] == "search")
        if read or policy.role in WRITING_ROLES:
            return
        # Reviewers may still comment on their own PR, with all existing
        # argument and target checks. They cannot publish elsewhere.
        return _check_gh(args, policy, cwd=cwd, run=run)
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
    if remaining[:1] == ["api"] and _comments_api_read(remaining[1:], policy):
        return
    if remaining[:1] == ["api"] and policy.role == "resolve":
        if remaining != args:
            raise PolicyError("the annotations read takes only --jq and --paginate")
        return _check_annotations_read(remaining[1:], policy, cwd=cwd, run=run)
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
    if key == ("run", "view") and policy.role == "resolve":
        _check_resolve_run_view(remaining[2:], positional, seen, policy, cwd=cwd, run=run)
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


def lookup_github_field(path: str, jq: str, *, cwd: str | None = None,
                        run=subprocess.run) -> str | None:
    """One field of one GitHub REST resource, read-only, or None on any doubt."""
    try:
        completed = run(["gh", "api", path, "--jq", jq], cwd=cwd, capture_output=True,
                        text=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode:
        return None
    value = (completed.stdout or "").strip()
    return value or None


#: Indirection for tests; the hook always performs the real lookup.
GITHUB_FIELD = lookup_github_field


def _checked_repository(policy: StagePolicy) -> str:
    if not policy.repo or not _SAFE_REPOSITORY.fullmatch(policy.repo):
        raise PolicyError("this stage has no configured repository to read failed checks in")
    return policy.repo


def _require_current_head(policy: StagePolicy, resource: str, identifier: str, *,
                          cwd: str | None, run) -> None:
    """Deny unless the run or check run ran on the change request's current head.

    Both SHAs are read by the hook, from the configured repository, at the
    moment of the call. Anything it cannot read or verify denies (fail closed).
    """
    repo = _checked_repository(policy)
    number = policy.pr_number
    if number is None:
        raise PolicyError("reading failed check output needs this cycle's change request")
    head = GITHUB_FIELD(f"repos/{repo}/pulls/{number}", ".head.sha", cwd=cwd, run=run)
    path = (f"repos/{repo}/actions/runs/{identifier}" if resource == "run"
            else f"repos/{repo}/check-runs/{identifier}")
    ran_on = GITHUB_FIELD(path, ".head_sha", cwd=cwd, run=run)
    if (head is None or ran_on is None or not _FULL_SHA.fullmatch(head)
            or not _FULL_SHA.fullmatch(ran_on)):
        raise PolicyError(f"whether {resource} {identifier} ran on change request {number}'s "
                          "current head could not be verified, so its output stays closed")
    if head != ran_on:
        raise PolicyError(f"{resource} {identifier} ran on {ran_on[:12]}, not on change request "
                          f"{number}'s current head {head[:12]}")


def _check_annotations_read(args: list[str], policy: StagePolicy, *, cwd: str | None,
                            run) -> None:
    """`gh api repos/<repo>/check-runs/<id>/annotations`, GET, for the current head.

    Only `--jq` and `--paginate` may accompany the literal path. Every option
    that would send a body, choose a method or read a file stays denied, and
    no other `gh api` path is opened by this.
    """
    path = None
    index = 0
    while index < len(args):
        word = args[index]
        if word == "--paginate":
            pass
        elif word == "--jq":
            index += 1
            if index >= len(args):
                raise PolicyError("gh api --jq needs a filter")
        elif word.startswith("--jq="):
            pass
        elif word.startswith("-") and word != "-":
            raise PolicyError(f"gh api option {word.split('=', 1)[0]} is not allowed; the "
                              "annotations read takes only --jq and --paginate")
        elif path is not None:
            raise PolicyError("gh api reads exactly one path")
        else:
            path = word
        index += 1
    match = _ANNOTATIONS_PATH.fullmatch(path or "")
    if match is None:
        raise PolicyError("gh api is not allowed except the documented calls; the resolve "
                          "stage may read repos/<owner>/<repo>/check-runs/<id>/annotations")
    if not _same_repo(match.group(1), _checked_repository(policy)):
        raise PolicyError("gh api may read check-run annotations only in this cycle's repository")
    _require_current_head(policy, "check run", match.group(2), cwd=cwd, run=run)


def _check_resolve_run_view(args: list[str], positional: list[str], seen: set[str],
                            policy: StagePolicy, *, cwd: str | None, run) -> None:
    """`gh run view` in resolve: a numeric run, its own jobs, logs of the current head."""
    if len(positional) != 1 or not _NUMERIC_ID.fullmatch(positional[0]):
        raise PolicyError("gh run view in the resolve stage needs a numeric run ID")
    run_id = positional[0]
    repo = _checked_repository(policy)
    jobs: list[str] = []
    index = 0
    while index < len(args):
        name, has_value, value = args[index].partition("=")
        if name in _RUN_VIEW_JOB:
            if not has_value:
                index += 1
                value = args[index] if index < len(args) else ""
            jobs.append(value)
        elif name in {"--json", "--jq", "-q", "--template", "-t", "--attempt", "-a"} and not has_value:
            index += 1
        index += 1
    for job in jobs:
        if not _NUMERIC_ID.fullmatch(job):
            raise PolicyError("gh run view --job needs a numeric job ID")
        owner = GITHUB_FIELD(f"repos/{repo}/actions/jobs/{job}", ".run_id", cwd=cwd, run=run)
        if owner is None:
            raise PolicyError(f"whether job {job} belongs to run {run_id} could not be verified")
        if owner != run_id:
            raise PolicyError(f"job {job} does not belong to run {run_id}")
    if seen.intersection(_RUN_VIEW_LOGS):
        _require_current_head(policy, "run", run_id, cwd=cwd, run=run)


def _reference_in_repo(reference: str, repo: str | None) -> bool:
    """A number or `#n` is this repository's; a URL must name it."""
    if "/" not in reference:
        return True
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/(?:pull|issues)/\d+/?", reference)
    return bool(match) and _same_repo(match.group(1), repo)


def _check_docker(args: list[str], policy: StagePolicy) -> None:
    """The socket permission first, then the table; the rest asks for consent.

    `docker_host` is its own permission (#398): without it every Docker
    command reports `needs_docker`, and exact operation consent never stands
    in for it. With it, only `docker compose up|down|ps|logs|build` is in the
    table; `compose run`, `docker run` and every other form is outside the
    table, which makes it an exact operation-consent question, not a veto.
    """
    if not policy.docker:
        raise _DockerDenied("docker needs the docker_host grant, which this "
                            "dispatch does not have")
    if args[:1] != ["compose"]:
        raise PolicyError("only docker compose up/down/ps/logs/build is in the table; "
                          "any other Docker command needs exact operation consent")
    rest = args[1:]
    index = 0
    while index < len(rest) and rest[index].startswith("-"):
        name, has_value, _ = rest[index].partition("=")
        if name not in DOCKER_COMPOSE_GLOBAL:
            raise PolicyError(f"docker compose option {name} is not in the table; "
                              "it needs exact operation consent")
        index += 1 if has_value or not DOCKER_COMPOSE_GLOBAL[name] else 2
    subcommand = rest[index] if index < len(rest) else ""
    if subcommand not in DOCKER_COMPOSE_SUBCOMMANDS:
        raise PolicyError(f"docker compose {subcommand or '(none)'} is outside the table "
                          "(up, down, ps, logs, build); it needs exact operation consent")


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
    """`--allowedTools` patterns for the stage's table: the executor's filter.

    The patterns are prefixes; the hook validates the arguments behind them.
    Git and isolated inline Python are documented broad exceptions (#524):
    docs/host-permissions.md#dispatch-only-inspection-exceptions-524.
    """
    prefixes = [f"git {sub}" for sub in sorted(GIT_COMMON_READ)]
    # Deliberately admit all Git commands and inline Python to the hook for
    # no-consent inspection. These are not read-only executor patterns; the
    # immutable hook is their sole toolkit operation filter (see docstring).
    wildcards: list[str] = ["git *"]
    prefixes += ["python3 -I -c", "printenv GIT_SSH_COMMAND", "printenv GIT_SSH"]
    if policy.repo and _SAFE_REPOSITORY.fullmatch(policy.repo):
        wildcards.append(f"gh api repos/{policy.repo}/issues/*/comments*")
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
        if policy.repo and _SAFE_REPOSITORY.fullmatch(policy.repo):
            # The annotations path ends in an ID, so it is matched with a
            # wildcard rather than a word prefix; the hook checks the rest.
            wildcards += [f"gh api repos/{policy.repo}/check-runs/*",
                          f"gh api * repos/{policy.repo}/check-runs/*"]
    if policy.role in {"review", "rereview", "resolve"} and policy.tmpdir:
        validator = shlex.quote(policy.triage_validator_path)
        tmpdirs = dict.fromkeys((policy.tmpdir,
                                 str(Path(policy.tmpdir).resolve(strict=False))))
        prefixes.append(f'python3 {validator} "$TMPDIR/pr-comment.md"')
        for tmpdir in tmpdirs:
            root = Path(tmpdir)
            prefixes.append(
                f'python3 {validator} {shlex.quote(str(root / "pr-comment.md"))}')
            # shlex.quote would wrap a path holding `*` in literal quotes that
            # an unquoted command never matches, so quote only the root. The
            # whole-path quoted form covers an agent that quotes the argument.
            for nested in (f'{shlex.quote(str(root))}/claude-*/pr-comment.md',
                           shlex.quote(str(root / "claude-*" / "pr-comment.md"))):
                wildcards.append(f'python3 {validator} {nested}')
    prefixes += sorted(READ_SHELL - {"sed"})
    # `sed` has no bare prefix: only the read-only openings the hook accepts (#464).
    prefixes += [f"sed {option}" for option in SED_FIRST_OPTIONS]
    if policy.permissions_command:
        prefixes.append(shlex.join(policy.permissions_command))
    prefixes += [shlex.join(command) for command in policy.publication_commands]
    if not policy.static_only:
        prefixes += list(policy.tools) + list(policy.commands)
        for entry in policy.command_permissions:
            if entry.get("decision") == "allow":
                op = entry["operation"]
                if op["tool"] == "Bash":
                    if op.get("version") == 2 or op.get("check") or op["pattern"].get("words", [])[:1] == ["<SHELL>"]:
                        # The regex/compound/env shape cannot be expressed in
                        # Claude's prefix grammar. The hook remains the concrete
                        # gate and runs before every Bash call, including guards.
                        wildcards.append("*")
                        continue
                    # The broad first barrier never replaces exact hook matching.
                    _, command_words = split_environment(op["pattern"]["words"])
                    program = command_words[0] if command_words else None
                    if isinstance(program, str) and re.fullmatch(r"[A-Za-z0-9_./+-]+", program):
                        prefixes.append(program)
    if policy.docker and not policy.static_only:
        prefixes += [f"docker compose {sub}" for sub in sorted(DOCKER_COMPOSE_SUBCOMMANDS)]
    patterns = []
    patterns.extend(policy.integrations)
    if not policy.static_only:
        for entry in policy.command_permissions:
            op = entry["operation"]
            if entry.get("decision") == "allow" and op["tool"] in FILE_EDIT_TOOLS:
                patterns.append(op["tool"])
    if policy.tmpdir:
        patterns.append("Bash(printenv TMPDIR:*)")
        if policy.rtk:
            patterns += ["Bash(rtk printenv TMPDIR:*)",
                         "Bash(rtk proxy printenv TMPDIR:*)"]
    for prefix in prefixes:
        patterns.append(f"Bash({prefix}:*)")
        if policy.rtk:
            patterns += [f"Bash(rtk {prefix}:*)", f"Bash(rtk proxy {prefix}:*)"]
    for pattern in wildcards:
        patterns.append(f"Bash({pattern})")
        if policy.rtk:
            patterns += [f"Bash(rtk {pattern})", f"Bash(rtk proxy {pattern})"]
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
            patterns += [f"Read({root}/**)"]
            if policy.role in REVIEW_ROLES:
                patterns += [f"Read({root}/delta.diff)", f"Read({root}/claude-*/delta.diff)"]
            patterns += [f"Edit({root}/*.log)", f"Edit({root}/claude-*/*.log)"]
        patterns += [pattern.replace("Edit(", "Write(") for pattern in list(patterns)
                     if pattern.startswith("Edit(")]
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


def hook_settings(hook_command: str, *, bash_only: bool = False) -> dict:
    """The `--settings` hook block that makes the hook part of the dispatch.

    `bash_only` is the minimal hook of a non-GitHub cycle: it validates Bash
    calls for the foreground rule and leaves file edits to the executor.

    A `PreToolUse` hook that times out or fails lets the call continue by
    default, so the hook is no gate unless `onFailure` is `block` (Claude Code
    2.1.295 or later). The hook resolves Compose configurations on the host,
    which an untrusted change can make slow (#398): its failure must refuse.
    """
    return {"hooks": {"PreToolUse": [{
        "matcher": "Bash" if bash_only else "|".join(["Bash", *FILE_EDIT_TOOLS]),
        "hooks": [{"type": "command", "command": hook_command, "timeout": HOOK_TIMEOUT_SECONDS,
                   "onFailure": "block"}],
    }]}}


def prompt_note(policy: StagePolicy) -> str:
    """Claude -p guidance; active GitHub policies also explain their command table."""
    foreground = (
        "This is a dispatched Claude `-p` stage. Run every Bash command in "
        "the foreground and wait for it to finish before returning the structured "
        "result. Never use `run_in_background: true`. For a configured "
        "verification command, do not set a timeout below 10 minutes; if "
        "verification cannot finish within that limit, return an explicit "
        "`BLOCKED` result with the timeout reason."
    )
    if not policy.active:
        return foreground

    verification = (
        " The configured verification commands are " + ", ".join(
            f"`{command}`" for command in policy.verification_commands) + "."
        if policy.verification_commands else
        " No test or verify command is configured for this repository, so no "
        "verification command is available."
    )
    tmpdir_note = (
        " For native dispatches, the actual per-dispatch TMPDIR path is "
        f"available with `{'rtk ' if policy.rtk else ''}printenv TMPDIR`."
    )
    choices = {key: getattr(policy, key) for key in
               ("tools", "commands", "integrations", "registry_domains", "credential_reads")
               if getattr(policy, key)}
    if choices:
        tmpdir_note += " Effective project choices: " + json.dumps(choices) + "."
    if policy.permissions_command:
        tmpdir_note += (
            " The permission decisions in force for this repository and branch are "
            f"listed by `{shlex.join(policy.permissions_command)}`; it is read-only "
            "and grants nothing, and takes no other arguments.")
    if policy.role in {"review", "rereview", "resolve"}:
        tmpdir_note += (
            " The staged-comment validator is installed with the runtime at "
            f"`{policy.triage_validator_path}`; run "
            f'`python3 {shlex.quote(policy.triage_validator_path)} '
            '"$TMPDIR/pr-comment.md"`. '
        )
        if policy.role in {"review", "rereview"}:
            tmpdir_note += (
                " Validate the staged comment with this command before publishing; "
                "if validation fails, fix the comment and validate it again."
            )
    return (
        foreground +
        f" Command policy for this {policy.role} stage: a runtime hook validates "
        "every Bash command against this stage's table. Prefer one simple "
        "command per call. Compound syntax needs interpreter consent with "
        "per-segment guards. " + verification +
        " Run configured checks as written, without prefixes or redirects. "
        "Toolkit refusals carry [layer=toolkit_hook]; an unlabelled refusal "
        "comes from the host permission layer. A toolkit grant does not widen "
        "host or operating-system permissions. Propose a reusable matcher in "
        "command_pattern_proposals with command, pattern, proposer, justification, "
        "positive and negative examples; only the user's answer authorizes it. " +
        " When a command you need is denied, distinguish a forbidden operation "
        "from an unavailable executable. For authorized source reads, use an "
        "available search alternative: `git grep` or `grep`. Do not bypass a "
        "forbidden operation. Report its exact command when no authorized path "
        "remains. Explicit project tool, command and integration choices take "
        "precedence over the built-in table; a table warning is informational "
        "only. Write multiline temporary helpers with the native file-write "
        "tools, then publish using --body-file. Read large outputs in smaller "
        "pieces, or write them under the supplied $TMPDIR; a read under this "
        "session's own tool-results directory is allowed like one under $TMPDIR."
        + tmpdir_note
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


def reduced_guarantee_overrides(config_dir: Path | None = None,
                                managed: tuple[Path, ...] | None = None,
                                cwd: str | None = None,
                                policy: Any = None) -> tuple[list[dict], list[str]]:
    """Describe user-owned Claude settings that disable a dispatch guarantee.

    Managed settings are reported as executor policy limits. The returned
    records contain only the effective setting and its source metadata; callers
    bind consent to their fingerprint and never infer consent from prose.
    """
    from reduced_guarantees import binding

    config_dir = config_dir or claude_config_dir()
    managed_paths = managed if managed is not None else managed_settings_paths()
    overrides: list[dict] = []
    limits: list[str] = []
    for path in managed_paths:
        settings = _read_settings(path)
        if settings.get("allowManagedHooksOnly"):
            limits.append(f"managed policy at {path} enforces allowManagedHooksOnly")
        if settings.get("disableAllHooks"):
            limits.append(f"managed policy at {path} disables all hooks")

    settings_path = config_dir / "settings.json"
    settings = _read_settings(settings_path)
    if settings.get("disableAllHooks"):
        overrides.append(binding(
            setting="disableAllHooks", value=True, source=str(settings_path.resolve()),
            layer="user", executor="claude", guarantees=["stage command hook"],
            provenance="trusted_user_configuration"))

    additional_settings = [config_dir / "settings.local.json"]
    if cwd:
        additional_settings.extend(Path(cwd) / ".claude" / name
                                   for name in ("settings.json", "settings.local.json"))
    for path in additional_settings:
        if not path.is_file() or not _read_settings(path).get("disableAllHooks"):
            continue
        local = cwd and path.resolve().is_relative_to(Path(cwd).resolve())
        overrides.append(binding(
            setting="disableAllHooks", value=True, source=str(path.resolve()),
            layer="repository" if local else "user", executor="claude",
            guarantees=["stage command hook"],
            provenance="untrusted_change" if local else "trusted_user_configuration"))

    mods = tool_check_mod_fingerprints(config_dir)
    if mods:
        overrides.append(binding(
            setting="tool.check plugin", value=mods,
            source=str((config_dir / "plugins" / "installed_plugins.json").resolve()),
            layer="user_plugin_configuration", executor="claude",
            guarantees=["stage command hook decisions"],
            provenance="trusted_user_configuration"))

    sandbox_relevant = policy is None or policy.uses_bash_sandbox
    paths = [settings_path] if sandbox_relevant else []
    if cwd and sandbox_relevant:
        paths.extend(Path(cwd) / ".claude" / name
                     for name in ("settings.json", "settings.local.json"))
    for path in paths:
        if not path.is_file():
            continue
        source_settings = _read_settings(path)
        sandbox = source_settings.get("sandbox") or {}
        if not isinstance(sandbox, dict):
            continue
        for keys in SANDBOX_WIDENING_KEYS:
            value: Any = sandbox
            for key in keys:
                value = value.get(key) if isinstance(value, dict) else None
            if not value:
                continue
            local = cwd and path.resolve().is_relative_to(Path(cwd).resolve())
            overrides.append(binding(
                setting=f"sandbox.{'.'.join(keys)}", value=value,
                source=str(path.resolve()),
                layer="repository" if local else "user",
                executor="claude",
                guarantees=["native sandbox filesystem and network restrictions"],
                provenance="untrusted_change" if local else "trusted_user_configuration"))
    return overrides, limits


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


def tool_check_mod_fingerprints(config_dir: Path | None = None) -> list[dict[str, str]]:
    """Identify each enabled `tool.check` plugin and its effective source tree."""
    config_dir = config_dir or claude_config_dir()
    settings = _read_settings(config_dir / "settings.json")
    enabled = {name for name, on in (settings.get("enabledPlugins") or {}).items() if on}
    installed = _read_settings(config_dir / "plugins" / "installed_plugins.json")
    found: list[dict[str, str]] = []
    extensions = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}
    for name in sorted(enabled):
        for entry in (installed.get("plugins") or {}).get(name) or []:
            raw_path = entry.get("installPath") if isinstance(entry, dict) else None
            root = Path(raw_path) if raw_path else None
            if root is None or not root.is_dir() or not _handles_tool_check(root):
                continue
            files: list[tuple[str, str]] = []
            for directory, subdirectories, names in os.walk(root):
                subdirectories[:] = [child for child in subdirectories
                                     if child not in {"node_modules", ".git"}]
                for filename in names:
                    path = Path(directory) / filename
                    if path.suffix not in extensions:
                        continue
                    try:
                        content = path.read_bytes()
                    except OSError:
                        continue
                    files.append((path.relative_to(root).as_posix(),
                                  hashlib.sha256(content).hexdigest()))
            tree = hashlib.sha256(json.dumps(sorted(files), separators=(",", ":"))
                                  .encode("utf-8")).hexdigest()
            found.append({"plugin": name, "source": str(root.resolve()), "sha256": tree})
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
    ("filesystem", "allowWrite"), ("filesystem", "allowRead"),
    ("filesystem", "disabled"), ("excludedCommands",),
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


def review_sandbox_settings(writable: tuple[str, ...],
                            registry_domains: tuple[str, ...] = (), *,
                            docker_host: bool = False, publishes: bool = False,
                            credential_reads: tuple[str, ...] = ()) -> dict:
    """Claude's Bash sandbox for a reviewer that verifies in its clone.

    A sandboxed command has no network route except the allowed domains, and a
    `claude -p` reviewer has nobody to answer a prompt for any other host.
    """
    return {"sandbox": {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "filesystem": {"allowWrite": list(writable),
                       "denyRead": list(credential_read_denials(
                           publishes=publishes, credential_reads=credential_reads))},
        "network": {
            "allowedDomains": list(dict.fromkeys(
                (*REVIEW_SANDBOX_DOMAINS, *registry_domains))),
            **({"allowAllUnixSockets": True} if docker_host else {}),
        },
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
    deny rules are meant to stay in force. `--allowedTools` is not a complete
    filter for the documented Git and inline-Python exceptions (#524), so this
    hook is their only operation check. The hook only ever narrows.
    """
    global _RESOLUTIONS
    _RESOLUTIONS = {}
    try:
        return _hook_main(arguments, stdin, stdout)
    finally:
        _RESOLUTIONS = None


def _hook_main(arguments: list[str], stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    policy = None
    try:
        policy = StagePolicy.from_json(Path(arguments[0]).read_text(encoding="utf-8"))
        event = json.loads(stdin.read())
        # Only the host's event names the session; a stored policy never does.
        policy = replace(policy, tool_results=session_tool_results(event))
        tool = event.get("tool_name")
        if tool in FILE_EDIT_TOOLS and not policy.active:
            return 0
        if tool in FILE_EDIT_TOOLS:
            decision = check_file_edit(tool, event.get("tool_input"), policy,
                                       cwd=event.get("cwd"))
        elif tool != "Bash":
            return 0
        else:
            decision = check_bash_tool_input(event.get("tool_input") or {}, policy,
                                             cwd=event.get("cwd"))
        if decision.allowed:
            try:
                approved = operation(tool, event.get("tool_input") or {}, policy, event.get("cwd"))
                if approved.get("sensitive") and policy.journal_path and policy.cycle_id:
                    from cycle_journal import record_event
                    record_event(policy.journal_path, repo_id=policy.repo or "unknown",
                                 task_id=policy.work_item_id or "unknown", cycle_id=policy.cycle_id,
                                 kind="stage_result", role=policy.role, stage_seq=policy.stage_seq,
                                 attempt_id=policy.attempt_id, component="permission_execution",
                                 status="authorized_operation", reason=approved["sensitive"]["warning"],
                                 correlation={"operation": denial_operation_id(approved, None),
                                              "tool_use_id": event.get("tool_use_id")})
            except Exception:
                pass  # Recording never supplies or revokes authorization.
    except Exception as error:
        reason = f"the stage policy could not run ({type(error).__name__})"
        note = _journal_denial(policy, None, "hook_bash", None, reason) if policy else None
        print(_hook_output("deny", f"[layer=toolkit_hook] code-cycle {HOOK_DENIAL_MARKER}: {reason}"
                                   f"{_journal_note(note)}. {DENY_ADVICE}"), file=stdout)
        return 0
    if not decision.allowed:
        editing = tool in FILE_EDIT_TOOLS
        advice = EDIT_DENY_ADVICE if editing else DENY_ADVICE
        tool_input = event.get("tool_input") or {}
        if editing:
            target = tool_input.get(FILE_EDIT_TOOLS[tool]) if isinstance(tool_input, dict) else None
            command = f"{tool} {target}" if isinstance(target, str) else tool
        else:
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
        inspection_note = ""
        try:
            # A missing docker_host grant is its own question, asked first (#398).
            requested_operation = (None if decision.needs_docker
                                   else operation(tool, tool_input, policy, event.get("cwd")))
        except (ValueError, TypeError, OSError) as error:
            requested_operation = None
            # A sensitive operation whose bindings cannot be resolved gets no
            # question; say why instead of leaving the agent waiting for one.
            if isinstance(error, PolicyError) and decision.rule == "sensitive_consent":
                inspection_note = f"; no consent can be asked: {error}"
        # Every denial is recorded with the rule that applied and an operation
        # identifier, including an operation that can never be granted (#364).
        rule = decision.rule or ("edit_rule" if editing else "command_table")
        if rule == "compose_unresolved":
            advice = UNRESOLVED_COMPOSE_ADVICE
        operation_id = denial_operation_id(requested_operation, command)
        intent = None if editing else pr_edit_intent(command, policy)
        note = _journal_denial(policy, event, "hook_edit" if editing else "hook_bash",
                               command, decision.reason, rule=rule, operation_id=operation_id)
        print(_hook_output("deny", f"[layer=toolkit_hook] code-cycle {policy.role} {HOOK_DENIAL_MARKER}: "
                                   f"{decision.reason}{inspection_note}{_journal_note(note)}. {advice} "
                                   f"COMMAND_PERMISSION_TMPDIR={json.dumps(policy.tmpdir)} "
                                   f"COMMAND_PERMISSION_OPERATION={json.dumps(requested_operation)} "
                                   f"COMMAND_PERMISSION_RULE={json.dumps(rule)} "
                                   f"COMMAND_PERMISSION_ID={json.dumps(operation_id)} "
                                   f"COMMAND_PERMISSION_INTENT={json.dumps(intent)} "
                                   f"COMMAND_PERMISSION_TOOL_RESULTS={json.dumps(policy.tool_results)}"),
              file=stdout)
    return 0


def denial_operation_id(requested_operation: dict[str, Any] | None, command) -> str:
    """A stable identifier of a refused operation, grantable or not (#364).

    A grantable operation keeps the identifier its grant would have; one the
    table can never parse (a heredoc, a pipeline) is identified by its text.
    """
    import hashlib
    if requested_operation is not None:
        return "command_" + hashlib.sha256(json.dumps(
            requested_operation, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return "refused_" + hashlib.sha256(json.dumps(
        {"command": command if isinstance(command, str) else None},
        sort_keys=True).encode()).hexdigest()


#: `gh pr edit` flags and the metadata field each one changes.
_PR_EDIT_VALUE_FLAGS = {"--title": "title", "-t": "title", "--body": "body", "-b": "body",
                        "--body-file": "body", "-F": "body", "--base": "base", "-B": "base",
                        "--add-label": "labels_add", "--remove-label": "labels_remove",
                        "--milestone": "milestone", "-m": "milestone"}
_BODY_FILE_LIMIT = 1_000_000


def pr_edit_intent(command, policy: StagePolicy) -> dict[str, Any] | None:
    """What a refused `gh pr edit` meant to change, read while its files exist.

    The body file lives in the dispatch's `$TMPDIR`, which is gone when the
    runtime classifies the refusal, so its digest is taken here. Any flag this
    table does not know leaves the intent unknown: nothing is inferred (#364).
    """
    if not isinstance(command, str):
        return None
    try:
        words = list(parse_command(command, policy.tmpdir).words)
    except Exception:
        return None
    if words[:1] == ["rtk"]:
        words = words[2:] if words[1:2] == ["proxy"] else words[1:]
    if words[:3] != ["gh", "pr", "edit"]:
        return None
    import hashlib
    fields: dict[str, Any] = {}
    unknown: list[str] = []
    index = 3
    while index < len(words):
        word = words[index]
        flag, equals, inline = word.partition("=") if word.startswith("--") else (word, "", "")
        if flag in {"--repo", "-R"}:
            # Names the repository, not metadata to change.
            index += 1 if equals else 2
            continue
        target = _PR_EDIT_VALUE_FLAGS.get(flag)
        if target is None:
            if word.startswith("-"):
                unknown.append(flag)
            index += 1
            continue
        if equals:
            value = inline
        elif index + 1 < len(words):
            value, index = words[index + 1], index + 1
        else:
            unknown.append(flag)
            break
        index += 1
        if flag in {"--body-file", "-F"}:
            try:
                if not inside_tmpdir(value, policy.tmpdir):
                    raise OSError("outside $TMPDIR")
                with open(value, "rb") as handle:
                    raw = handle.read(_BODY_FILE_LIMIT + 1)
                if len(raw) > _BODY_FILE_LIMIT:
                    raise OSError("too large")
                fields["body"] = {"sha256": hashlib.sha256(raw.decode("utf-8").strip().encode()).hexdigest()}
            except (OSError, UnicodeDecodeError):
                unknown.append(flag)
        elif target == "body":
            fields["body"] = {"sha256": hashlib.sha256(value.strip().encode()).hexdigest()}
        elif target in {"labels_add", "labels_remove"}:
            fields.setdefault(target, []).extend(item for item in value.split(",") if item)
        else:
            fields[target] = value
    return {"fields": fields, "unknown": unknown}


def _journal_denial(policy: StagePolicy, event: dict[str, object] | None, component: str,
                    command, reason: str, *, rule: str | None = None,
                    operation_id: str | None = None) -> str | None:
    """Record a denial in the cycle journal; a failure is returned, never raised.

    The decision is already made and stays made: this only writes it down.
    """
    try:
        from cycle_journal import record_hook_denial
    except Exception as error:
        return f"the cycle journal is unavailable ({type(error).__name__})"
    event = event if isinstance(event, dict) else {}
    try:
        return record_hook_denial(
            policy, component=component,
            command=command if isinstance(command, str) else None, reason=reason,
            correlation={"tool_use_id": event.get("tool_use_id"),
                         "layer": "toolkit_hook",
                         "session_id": event.get("session_id"),
                         "rule": rule, "operation": operation_id})
    except Exception as error:
        return f"the cycle journal could not record this denial ({type(error).__name__})"


def _journal_note(note: str | None) -> str:
    # Only a dispatch that has a journal reports that it could not write to it.
    if not note or note == "the cycle journal is not configured for this dispatch":
        return ""
    return f" ({note})"


_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
#: `NAME+=value` is an assignment to the shell, never a program; the runtime
#: does not normalize it, so a command that starts with one is not identified.
_APPEND_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+=")
#: Prefixes that redirect git or gh to another host, repository, token or
#: configuration, which their guards cannot see (#398).
#: `PATH` and the loader variables choose which program runs as `git` or `gh`;
#: editor, pager, browser and askpass variables name programs they start.
#: Case-insensitive: Windows environment names are.
_GUARD_DEFEATING_ENV = re.compile(
    r"(GIT_.*|GH_.*|GITHUB_.*|HOME|XDG_.*|PATH|LD_.*|DYLD_.*|EDITOR|VISUAL|PAGER|LESS.*"
    r"|BROWSER|SSH_ASKPASS.*|SSH_.*COMMAND)\Z", re.IGNORECASE)
#: Prefixes the Compose resolver never receives: they would choose which
#: program, library, plugin or Docker configuration the host-side hook runs.
_RESOLVER_UNSAFE_ENV = re.compile(
    r"(PATH|HOME|ENV|BASH_ENV|IFS|LD_.*|DYLD_.*|DOCKER_.*|XDG_.*|PYTHON.*|PERL.*|NODE_.*)\Z")


def split_environment(words: list[Any]) -> tuple[list[Any], list[Any]]:
    """`NAME=value` prefixes, and the command they apply to.

    A stored pattern spells a normalized prefix as `{"env": NAME, ...}`.
    """
    index = 0
    while index < len(words) and (
            (isinstance(words[index], str) and _ENV_ASSIGNMENT.match(words[index]))
            or (isinstance(words[index], dict) and "env" in words[index])):
        index += 1
    return list(words[:index]), list(words[index:])


def _program(word: str) -> str:
    return os.path.basename(word).removesuffix(".exe")


def hidden_program(environment: list[Any], command_words: list[Any]) -> str | None:
    """Why the program after the prefixes cannot be identified, if it cannot.

    Both forms would hide the real program from its guards and from the
    consent identity, so they are a real limit, never a consent question.
    """
    first = command_words[0] if command_words else None
    if isinstance(first, str) and _APPEND_ASSIGNMENT.match(first):
        return "an append-style NAME+=value prefix hides the program it applies to"
    # Callers strip one leading `rtk` or `rtk proxy`; one still in the program
    # position follows an environment prefix or another rtk.
    if isinstance(first, str) and _program(first) == "rtk":
        return ("an environment prefix before rtk hides the program it applies to" if environment
                else "a nested rtk hides the program it applies to")
    return None


def consent_role(role: str | None) -> str | None:
    """The role an operation consent is keyed by.

    Review and rereview judge the same untrusted change of the same
    repository, so one decision covers both (#398). Every other role keeps
    its own decisions.
    """
    return "review" if role in REVIEW_ROLES else role


def checkout_root(cwd: str | None) -> Path | None:
    """The working tree containing `cwd`: the nearest directory holding `.git`."""
    if not cwd:
        return None
    try:
        path = Path(cwd).resolve()
    except (OSError, RuntimeError):
        return None
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _relative_value(value: str, policy: Any, root: Path | None) -> dict[str, str] | None:
    """A per-dispatch path as a placeholder, only when it really lies there.

    The `$TMPDIR` of each dispatch and the clone of each review differ, so the
    same command would never match its consent across cycles. Membership is
    checked after resolving symlinks and `..`; anything else stays literal.
    """
    if not os.path.isabs(value):
        return None
    if _tmp_operand(value, policy.tmpdir):
        return {"tmpdir": _tmp_relative(value, policy)}
    if inside_tool_results(value, policy):
        return {"tool_results": TOOL_RESULT_OUTPUT}
    if root is not None:
        try:
            resolved = Path(value).resolve()
        except (OSError, RuntimeError):
            return None
        if resolved == root or root in resolved.parents:
            return {"checkout": resolved.relative_to(root).as_posix() or "."}
    return None


def _tmp_relative(value: str, policy: Any) -> str:
    relative = Path(value).resolve().relative_to(Path(policy.tmpdir).resolve()).as_posix()
    for field, placeholder in (("attempt_id", "<ATTEMPT_ID>"), ("cycle_id", "<CYCLE_ID>")):
        identifier = getattr(policy, field, None)
        if isinstance(identifier, str) and identifier:
            relative = relative.replace(identifier, placeholder)
    return relative


def _tmp_operand(value: str, tmpdir: str | None) -> bool:
    """Normalize the directory itself too; redirect guards stay strict children."""
    return bool(tmpdir) and (Path(value).resolve() == Path(tmpdir).resolve()
                            or inside_tmpdir(value, tmpdir))


def _shell_identity(parsed: ParsedCommand, policy: Any, cwd: str | None) -> str:
    source = parsed.shell_source or ""
    for segment in parsed.segments:
        environment, words = command_parts(segment.words)
        operands = [item.partition("=")[2] for item in environment]
        operands += [word for index, word in enumerate(words) if index and words[index - 1] not in {"-c", "-e", "--eval"}]
        operands += [target for _, _, target in segment.redirects if target is not None]
        for operand in operands:
            if not operand:
                # An empty quoted word names no path; `Path(cwd) / ""` is cwd.
                continue
            candidate = str(Path(cwd or ".") / operand)
            if not (os.path.isabs(operand) or operand.startswith(("./", "../"))
                    or "/" in operand or Path(candidate).exists()):
                continue
            if _tmp_operand(candidate, policy.tmpdir):
                replacement = "<TMPDIR>/" + _tmp_relative(candidate, policy)
            elif inside_tool_results(candidate, policy):
                replacement = TOOL_RESULTS_OPERAND
            else:
                continue
            # Replace a complete operand, never a substring of opaque code.
            forms = [operand]
            home = os.path.expanduser("~")
            if home != "~" and operand.startswith(home.rstrip("/") + "/"):
                # The parser expanded a bare `~`; the source still spells it.
                forms.append("~" + operand[len(home.rstrip("/")):])
            if policy.tmpdir and operand.startswith(policy.tmpdir + "/"):
                suffix = operand[len(policy.tmpdir):]
                forms += ["$TMPDIR" + suffix, "${TMPDIR}" + suffix]
            for form in forms:
                source = re.sub(r"(?<![A-Za-z0-9_./-])" + re.escape(form)
                                + r"(?=$|[\s'\"<>|&;)])", lambda _: replacement, source)
    return source


def configured_check_id(words: list[str], policy: Any) -> str | None:
    """The configured check this exact command is, if any (#398).

    The configured test command is `configured-test`; a configured
    verification command is `verify:<command>`. A command is the check only
    when it is the configured command itself, with or without environment
    prefixes, or a verification prefix followed by more words.
    """
    environment, command_words = command_parts(words)
    for candidate in (words, command_words):
        if policy.test_command:
            try:
                if candidate == list(_parse_simple(policy.test_command, None).words):
                    return "configured-test"
            except PolicyError:
                pass
        for prefix in getattr(policy, "verify_prefixes", ()) or ():
            try:
                expected = list(_parse_simple(prefix, None).words)
            except PolicyError:
                continue
            if expected and candidate[:len(expected)] == expected:
                return f"verify:{prefix}"
    return None


def command_parts(words: list[str]) -> tuple[list[str], list[str]]:
    """Unwrap RTK on either side of ordinary environment assignments."""
    environment, command = split_environment(words)
    if command[:1] == ["rtk"]:
        command = command[2:] if command[1:2] == ["proxy"] else command[1:]
        inner_environment, command = split_environment(command)
        environment += inner_environment
    return environment, command


#: Seconds Claude Code waits for the hook before cancelling it.
HOOK_TIMEOUT_SECONDS = 60
#: Seconds one hook invocation may spend resolving a Compose configuration, all
#: attempts together; well under the hook timeout so the hook decides (#398).
COMPOSE_RESOLUTION_BUDGET_SECONDS = 20
#: Resolutions of the current hook invocation, or None outside the hook. The
#: hook identifies one command several times; it resolves it once. The runtime
#: never caches: it must see a configuration changed during a run.
_RESOLUTIONS: dict[Any, Any] | None = None


@contextlib.contextmanager
def compose_resolution_cache():
    """Resolve a Compose configuration once while one decision is being made.

    The hook sets the same cache for its whole invocation; callers outside it
    (the runtime reading a stage's refusals) open this one so that naming an
    unresolved configuration does not repeat `docker compose config`.
    """
    global _RESOLUTIONS
    previous = _RESOLUTIONS
    if previous is None:
        _RESOLUTIONS = {}
    try:
        yield
    finally:
        _RESOLUTIONS = previous

#: Effective Compose settings the consent question and the evidence disclose.
_HOST_NAMESPACES = ("network_mode", "pid", "ipc", "uts", "userns_mode", "cgroup")


def compose_warnings(config: dict[str, Any], root: Path | None) -> list[str]:
    """Host access in an effective Compose configuration, as warnings only.

    Disclose, never veto (#450): nothing here refuses a run the person allowed.
    """
    warnings: list[str] = []
    services = config.get("services") if isinstance(config, dict) else None
    for name, service in sorted((services or {}).items()) if isinstance(services, dict) else ():
        if not isinstance(service, dict):
            continue
        if service.get("privileged") is True:
            warnings.append(f"service {name} runs privileged")
        for key in _HOST_NAMESPACES:
            if service.get(key) == "host":
                warnings.append(f"service {name} uses the host {key.removesuffix('_mode')} namespace")
        if service.get("cap_add"):
            warnings.append(f"service {name} adds capabilities {', '.join(map(str, service['cap_add']))}")
        if service.get("devices"):
            warnings.append(f"service {name} maps host devices")
        for volume in service.get("volumes") or ():
            source = volume.get("source") if isinstance(volume, dict) else None
            if not isinstance(volume, dict) or volume.get("type") != "bind" or not isinstance(source, str):
                continue
            if source.endswith("docker.sock"):
                warnings.append(f"service {name} mounts the Docker socket {source}")
                continue
            try:
                resolved = Path(source).resolve()
            except (OSError, RuntimeError):
                resolved = Path(source)
            if root is None or not (resolved == root or root in resolved.parents):
                warnings.append(f"service {name} mounts {source} from outside the checkout")
    return warnings


def _redacted(warnings: list[str], env: dict[str, str]) -> list[str]:
    """Warnings without interpolated environment values, bounded and printable.

    Compose interpolates the host environment into the configuration, so a
    mount source can carry a token. The warnings reach the agent and the
    consent records: each environment value becomes `<env:NAME>`.
    """
    values = sorted(((value, name) for name, value in env.items() if len(value) >= 6),
                    key=lambda item: -len(item[0]))
    cleaned = []
    for warning in warnings:
        for value, name in values:
            warning = warning.replace(value, f"<env:{name}>")
        cleaned.append(re.sub(r"[\x00-\x1f\x7f]", "?", warning)[:200])
    return cleaned


def _location_free(text: str, config: dict[str, Any], cwd: str | None, root: Path | None,
                   tmpdir: str | None) -> str:
    """The resolved configuration without the per-dispatch clone and `$TMPDIR` paths.

    Compose resolves relative paths against the clone and names the project
    after its directory; both differ per review dispatch, so the digest would
    never match its consent again. Every other byte stays.
    """
    if cwd and isinstance(config.get("name"), str):
        directory = re.sub(r"[^a-z0-9_-]", "", Path(cwd).resolve().name.lower())
        if config["name"] == directory:
            text = text.replace(json.dumps(directory), json.dumps("<project>"))
    locations = []
    for path, marker in ((root, "<checkout>"), (tmpdir, "<tmpdir>")):
        if path:
            for location in (str(path), str(Path(path).resolve())):
                # In JSON a Windows path's backslashes are escaped.
                locations += [(location, marker), (json.dumps(location)[1:-1], marker)]
    for location, marker in sorted(set(locations), key=lambda item: -len(item[0])):
        text = text.replace(location, marker)
    return text


def _resolve_compose(words: list[str], environment: dict[str, str], cwd: str | None,
                     root: Path | None, tmpdir: str | None = None) -> dict[str, Any]:
    """The SHA-256 of the effective Compose configuration and its warnings.

    The same global options (files, project, profiles, env files, project
    directory), the same environment prefixes and the same directory as the
    real run, so the digest names what that run would use. The clone and
    `$TMPDIR` locations are normalized as in the command words.
    """
    globals_end = 2
    while globals_end < len(words) and words[globals_end].startswith("-"):
        name, has_value, _ = words[globals_end].partition("=")
        takes_value = DOCKER_COMPOSE_GLOBAL.get(name, False)
        globals_end += 1 if has_value or not takes_value else 2
    # The hook runs on the host, outside the sandbox and before any consent, so
    # the command chooses nothing it executes: always the hook's own `docker`
    # from its own PATH, never the command's first word, and no prefix that
    # picks a program, library, CLI plugin or Docker configuration (#398).
    # Git remote includes stay off; the configuration is resolved locally.
    safe = {name: value for name, value in environment.items()
            if not _RESOLVER_UNSAFE_ENV.match(name)}
    base = ["docker", "compose", *words[2:globals_end], "config"]
    env = {**os.environ, **safe, "COMPOSE_EXPERIMENTAL_GIT_REMOTE": "false"}
    key = (tuple(base), cwd, tuple(sorted(safe.items())), str(root), tmpdir)
    if _RESOLUTIONS is not None and key in _RESOLUTIONS:
        cached = _RESOLUTIONS[key]
        if isinstance(cached, PolicyError):
            raise cached
        return dict(cached)
    try:
        resolved = _resolve_within_budget(base, env, cwd, root, tmpdir)
    except PolicyError as error:
        if _RESOLUTIONS is not None:
            _RESOLUTIONS[key] = error
        raise
    if _RESOLUTIONS is not None:
        _RESOLUTIONS[key] = resolved
    return dict(resolved)


def _run_bounded(arguments: list[str], cwd: str | None, env: dict[str, str],
                 timeout: float) -> subprocess.CompletedProcess:
    """`subprocess.run`, killing the whole process group when time runs out.

    `docker compose` runs as a CLI plugin, a child of `docker`: killing only
    `docker` would leave the plugin holding the hook's pipes.
    """
    if os.name == "nt":
        return subprocess.run(arguments, cwd=cwd or None, env=env, capture_output=True,
                              text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    process = subprocess.Popen(arguments, cwd=cwd or None, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        raise
    return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


def _compose_failure_cause(stderr: object, env: dict[str, str], cwd: str | None,
                           root: Path | None, tmpdir: str | None) -> str:
    """The reason `docker compose config` gave, without warnings, paths or secrets.

    The last line that is not a warning names what failed: a missing Compose
    file, a missing `env_file`, an invalid service. The clone and `$TMPDIR`
    locations become placeholders and interpolated environment values `<env:NAME>`.
    """
    if not isinstance(stderr, str):
        return ""
    lines = [line.strip() for line in stderr.splitlines()
             if line.strip() and "level=warning" not in line and "variable is not set" not in line]
    if not lines:
        return ""
    return _redacted([_location_free(lines[-1], {}, cwd, root, tmpdir)], env)[0]


def _resolve_within_budget(base: list[str], env: dict[str, str], cwd: str | None,
                           root: Path | None, tmpdir: str | None) -> dict[str, Any]:
    """Both resolution attempts share one budget well under the hook's timeout.

    A hook cancelled by its timeout decides nothing, so the budget expiring is
    an explicit refusal instead (#398): an untrusted Compose file that makes
    `config` slow cannot let its run through unidentified.
    """
    deadline = time.monotonic() + COMPOSE_RESOLUTION_BUDGET_SECONDS
    completed = None
    cause = ""
    for arguments in ([*base, "--format", "json"], base):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            completed = COMPOSE_RUN(arguments, cwd, env, remaining)
        except subprocess.TimeoutExpired as error:
            raise ComposeResolutionTimeout("the effective Compose configuration could not be resolved within "
                              f"{COMPOSE_RESOLUTION_BUDGET_SECONDS} seconds; the runtime cannot "
                              "identify this run and refuses it") from error
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            # ValueError includes undecodable output: still a refusal, cached once.
            raise PolicyError("the effective Compose configuration could not be resolved "
                              f"({type(error).__name__}); the runtime cannot identify this run") from error
        if completed.returncode != 0:
            # Both attempts fail for the same reason; keep the first one given.
            cause = cause or _compose_failure_cause(completed.stderr, env, cwd, root, tmpdir)
            continue
        try:
            config = json.loads(completed.stdout)
        except ValueError:
            config = None
        if isinstance(config, dict):
            warnings = _redacted(compose_warnings(config, root), env)
            canonical = _location_free(json.dumps(config, sort_keys=True, separators=(",", ":")),
                                       config, cwd, root, tmpdir)
            return {"config_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    "warnings": warnings}
        canonical = _location_free(completed.stdout, {}, cwd, root, tmpdir)
        return {"config_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "warnings": ["the effective configuration is not JSON; its mounts and "
                             "privileges were not inspected"]}
    if completed is None:
        raise ComposeResolutionTimeout("the effective Compose configuration could not be resolved within "
                          f"{COMPOSE_RESOLUTION_BUDGET_SECONDS} seconds; the runtime cannot "
                          "identify this run and refuses it")
    raise PolicyError("the effective Compose configuration could not be resolved "
                      f"(docker compose config exited {completed.returncode}"
                      f"{': ' + cause if cause else ''}); the runtime cannot identify this run")


#: Replaced in tests; the hook calls it to identify a Compose run.
COMPOSE_RESOLVER = _resolve_compose
#: Replaced in tests; runs one `docker compose config` attempt with a timeout.
COMPOSE_RUN = _run_bounded


def operation(tool: str, tool_input: dict[str, Any], policy: Any, cwd: str | None = None,
              *, _include_sensitive: bool = True) -> dict[str, Any]:
    """Normalize only verified per-dispatch paths. Code, flags and destinations stay exact.

    A quoted interpreter argument is opaque, even if it contains a temp path.
    Symlinks escaping TMPDIR are never replaced with a placeholder. The value
    of an environment prefix and a Docker argument that lie in `$TMPDIR` or in
    the checkout become placeholders; the prefix itself stays, so a command
    with `DOCKER_HOST` or `LD_PRELOAD` is another command (#398). A Docker
    operation also carries its repository-relative directory, and a Compose
    one the digest of its effective configuration.
    """
    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            raise ValueError("command must be a string")
        parsed = parse_command(command, policy.tmpdir)
        if parsed.shell_source is not None:
            sensitive = sensitive_operation(tool, tool_input, policy, cwd) if _include_sensitive else None
            return {"version": 1, "role": policy.role if sensitive else consent_role(policy.role), "tool": tool,
                    "pattern": {"words": ["<SHELL>", _shell_identity(parsed, policy, cwd)], "redirects": []},
                    **({"sensitive": sensitive} if sensitive else {}),
                    **({"guard_consent": True} if guard_consent_reason(tool, tool_input, policy, cwd) else {})}
        words = list(parsed.words)
        if not words:
            raise ValueError("empty command")
        environment, command_words = command_parts(words)
        if not command_words:
            raise ValueError("an environment prefix needs a command")
        hidden = hidden_program(environment, command_words)
        if hidden:
            raise ValueError(hidden)
        docker = _program(command_words[0]) == "docker"
        root = checkout_root(cwd)
        normalized: list[Any] = []
        for assignment in environment:
            name, _, value = assignment.partition("=")
            placeholder = _relative_value(value, policy, root)
            normalized.append({"env": name, **placeholder} if placeholder else assignment)
        # Normalize path operands for every program. Inline code remains opaque:
        # a string containing a path is not itself a filesystem operand.
        for index, word in enumerate(command_words):
            candidate = str((Path(cwd or ".") / word).resolve())
            exists = False
            # Long filters and GraphQL queries are opaque argv, not necessarily
            # filesystem names. They must still have an exact consent identity.
            with contextlib.suppress(OSError):
                exists = Path(candidate).exists()
            path_operand = (os.path.isabs(word) or word.startswith(("./", "../"))
                            or "/" in word and _program(command_words[0]) != "gh"
                            or exists)
            inline_code = index > 0 and command_words[index - 1] in {"-c", "-e", "--eval", "--execute"}
            if index and path_operand and not word.startswith("-") and not inline_code and _tmp_operand(candidate, policy.tmpdir):
                normalized.append({"tmpdir": _tmp_relative(candidate, policy)})
            elif (index and path_operand and not word.startswith("-") and not inline_code
                  and inside_tool_results(word if word.startswith("~/") else candidate, policy)):
                # A saved output of the current session (#439 AC-13).
                normalized.append({"tool_results": TOOL_RESULT_OUTPUT})
            elif index and docker and (placeholder := _relative_value(word, policy, root)):
                normalized.append(placeholder)
            elif (index and word.startswith("-") and "=" in word
                  and (placeholder := _relative_value(word.partition("=")[2], policy, root if docker else None))):
                normalized.append({"option": word.partition("=")[0], **placeholder})
            else:
                normalized.append(word)
        redirects = []
        for fd, operator, target in parsed.redirects:
            target = str(Path(cwd or ".") / target) if target is not None else None
            if target is not None and not inside_tmpdir(target, policy.tmpdir):
                raise ValueError("output may be redirected only into $TMPDIR")
            redirects.append([fd, operator, None if target is None else
                              "<TMPDIR>/" + _tmp_relative(target, policy)])
        pattern: dict[str, Any] = {"words": normalized, "redirects": redirects}
        extra: dict[str, Any] = {}
        if docker:
            # The directory a Docker command runs in decides which Compose
            # files and build contexts it reads: part of what was agreed to.
            if root is not None and cwd:
                pattern["cwd"] = Path(cwd).resolve().relative_to(root).as_posix() or "."
            else:
                pattern["cwd"] = str(Path(cwd).resolve()) if cwd else None
            # Without the socket grant nothing resolves the configuration on
            # the host: the socket question comes first (#398), and an
            # identity without a digest matches no stored Compose consent.
            if command_words[1:2] == ["compose"] and getattr(policy, "docker", False):
                values = {assignment.partition("=")[0]: assignment.partition("=")[2]
                          for assignment in environment}
                try:
                    extra["compose"] = COMPOSE_RESOLVER(command_words, values, cwd, root, policy.tmpdir)
                except ComposeResolutionTimeout:
                    extra["compose"] = {"resolution": "timeout", "warnings": [
                        "effective Compose configuration inspection timed out; its mounts and "
                        "privileges were not inspected; exact consent is for this cycle only"]}
                    extra["guard_consent"] = True
        if guard_consent_reason(tool, tool_input, policy, cwd):
            extra["guard_consent"] = True
        check_id = configured_check_id(words, policy)
        if check_id:
            extra["check"] = check_id
        sensitive = sensitive_operation(tool, tool_input, policy, cwd) if _include_sensitive else None
        if sensitive:
            extra["sensitive"] = sensitive
        return {"version": 1, "role": policy.role if sensitive else consent_role(policy.role), "tool": tool,
                "pattern": pattern, **extra}
    elif tool in FILE_EDIT_TOOLS:
        path = tool_input.get(FILE_EDIT_TOOLS[tool])
        if not isinstance(path, str) or not path:
            raise ValueError("edit needs a target")
        path = str((Path(cwd or ".") / path).resolve())
        pattern = {"target": path}
    else:
        raise ValueError("unsupported tool")
    sensitive = sensitive_operation(tool, tool_input, policy, cwd) if _include_sensitive else None
    return {"version": 1, "role": policy.role if sensitive else consent_role(policy.role), "tool": tool, "pattern": pattern,
            **({"sensitive": sensitive} if sensitive else {})}


def same_operation(stored: Any, op: dict[str, Any]) -> bool:
    """Exact equality of the operation, whatever role it was decided in (#500)."""
    if not isinstance(stored, dict):
        return False
    if stored.get("sensitive") or op.get("sensitive"):
        if stored.get("sensitive") != op.get("sensitive"):
            return False
        if op.get("sensitive", {}).get("role_bound") and stored.get("role") != op.get("role"):
            return False
        return {**stored, "role": op.get("role")} == op
    if stored.get("guard_consent") or op.get("guard_consent"):
        # #456 exceptions are exact, including environment and configured
        # checks. An earlier writing-stage consent never reaches review.
        if (stored.get("role") != op.get("role") and
                (stored.get("role") == "review" or op.get("role") == "review")):
            return False
        return {**stored, "role": op.get("role")} == op
    def ordinary(value):
        for word in value.get("pattern", {}).get("words", []):
            name = (word.get("env") if isinstance(word, dict) else
                    word.partition("=")[0] if _ENV_ASSIGNMENT.match(word) else None)
            if name and (_GUARD_DEFEATING_ENV.match(name) or _RESOLVER_UNSAFE_ENV.match(name)):
                return False
        return True
    if (stored.get("check") and stored.get("check") == op.get("check")
            and ordinary(stored) and ordinary(op)):
        def check_words(value):
            words = value.get("pattern", {}).get("words", [])
            return [word for word in words if not (
                isinstance(word, dict) and "env" in word
                or isinstance(word, str) and _ENV_ASSIGNMENT.match(word))]
        return (stored.get("tool") == op.get("tool") == "Bash"
                and stored.get("compose") == op.get("compose")
                and stored.get("pattern", {}).get("cwd") == op.get("pattern", {}).get("cwd")
                and check_words(stored) == check_words(op))
    return {**stored, "role": op.get("role")} == op


def matching_answer(tool: str, tool_input: dict[str, Any], policy: Any, cwd: str | None) -> str | None:
    if not policy.command_permissions:
        return None
    try:
        op = operation(tool, tool_input, policy, cwd)
    except (ValueError, OSError, TypeError):
        return None
    # Decisions stored under different roles can disagree about one operation.
    # A denial wins whatever the order of the entries, never an allow that
    # happened to be loaded first (#500).
    exact = {entry["decision"] for entry in policy.command_permissions
             if same_operation(entry.get("operation"), op) and entry.get("decision") in {"allow", "deny"}}
    if exact:
        return "deny" if "deny" in exact else "allow"
    # Pattern authorization lifts table membership, never a concrete guard.
    patterns = [entry for entry in policy.command_permissions
                if entry.get("operation", {}).get("version") == 2]
    if (patterns and tool == "Bash" and not op.get("guard_consent") and not op.get("sensitive")
            and protected_reason(tool, tool_input, policy, cwd) is None):
        from grants import pattern_covers_operation
        covering = set()
        for entry in patterns:
            stored = entry.get("operation", {})
            if (stored.get("version") == 2 and stored.get("tool") == "Bash"
                    and entry.get("decision") in {"allow", "deny"}
                    and pattern_covers_operation(stored, op)):
                covering.add(entry["decision"])
        if covering:
            return "deny" if "deny" in covering else "allow"
    return None


def normalized_command(op: dict[str, Any]) -> str:
    """The documented single-space text on which regex grants fullmatch."""
    pattern = op.get("pattern", {})
    if pattern.get("words", [])[:1] == ["<SHELL>"]:
        return pattern["words"][1]
    words = []
    for word in pattern.get("words", []):
        if isinstance(word, dict):
            value = ("<TMPDIR>/" + word["tmpdir"] if "tmpdir" in word
                     else "<TOOL_RESULTS>/" + word["tool_results"] if "tool_results" in word
                     else "<CHECKOUT>/" + word["checkout"])
            prefix = word.get("env", word.get("option"))
            words.append((prefix + "=" if prefix else "") + value)
        else:
            words.append(word)
    for fd, operator, target in pattern.get("redirects", []):
        words.append(fd + operator + (target or "1"))
    return " ".join(words)


#: Builtins that change the working directory, also through `builtin` or
#: `command` (#439 REV-008), and builtins that run code which may change it.
_DIRECTORY_CHANGES = frozenset({"cd", "pushd", "popd"})
_CODE_LOADERS = frozenset({"eval", "source", ".", "trap", "alias"})
#: Files found inside directory credential stores, and subdirectories that
#: hold cached tokens. After a directory change the hook cannot resolve, the
#: unknown directory may lie inside such a store (REV-006).
_CREDENTIAL_FILE_PATTERNS = ("id_*", "*_rsa", "*_ed25519", "*_ecdsa", "*_dsa", "*_key", "deploy_key*",
                             "*.pem", "*.key", "*.ppk", "known_hosts*", "authorized_keys*",
                             "credentials", "config", "hosts.yml")
_CREDENTIAL_FILE_EXAMPLES = ("id_rsa", "github_rsa", "deploy_key", "key.pem", "server.key",
                             "known_hosts", "authorized_keys", "credentials", "config", "hosts.yml")
#: The key-like names above, which may lie at any depth of a directory store.
_CREDENTIAL_KEY_PATTERNS = tuple(name for name in _CREDENTIAL_FILE_PATTERNS if name not in {"config", "hosts.yml"})
_CREDENTIAL_KEY_EXAMPLES = tuple(name for name in _CREDENTIAL_FILE_EXAMPLES if name not in {"config", "hosts.yml"})
_CREDENTIAL_SUBDIRECTORIES = ("sso", "cli")
#: A brace part may expand to any single name, dotfiles included.
_ANY_PART = "\0any-part"
#: A '..' after an expanded part may lead to any directory.
_ANY_PARTS = "\0any-parts"
#: A working-directory component: compared literally, never as a glob.
_LITERAL = "\0literal:"
#: A bracket expression, including negation and character classes.
_BRACKET = re.compile(r"\[[!^]?\]?(?:\[:[a-z]+:\]|[^\]])*\]")


def _unwrapped_builtin(words: list[str]) -> list[str] | None:
    """The words a segment runs through builtin/command; None if only described."""
    while words[:1] in (["builtin"], ["command"]):
        prefix, words = words[0], words[1:]
        while words and words[0].startswith("-"):
            option, words = words[0], words[1:]
            if option == "--":
                break
            if prefix == "builtin" or option != "-p":
                # `command -v`/`-V` describe the builtin, never run it; an
                # invalid option fails before any directory change.
                return None
    return words


def _directory_builtin(words: list[str]) -> list[str] | None:
    """The cd, pushd or popd words a segment runs, through builtin/command."""
    words = _unwrapped_builtin(words) or []
    return words if words[:1] and words[0] in _DIRECTORY_CHANGES else None


def _runner_command_parts(words: list[str], cwd: str | None) -> tuple[list[str], list[str], str | None]:
    """Inspect literal known runners without changing the granted command.

    Unknown runner options and interpreter source (such as env -S) remain
    opaque. Never search arbitrary arguments for a program name.
    """
    environment: list[str] = []
    while words:
        assignments, words = command_parts(words)
        environment.extend(assignments)
        if not words:
            break
        program = _program(words[0])
        if program in {"command", "builtin"}:
            words = _unwrapped_builtin([program, *words[1:]]) or []
            continue
        if program not in {"env", "nice", "timeout"}:
            break
        remaining = words[1:]
        while remaining and remaining[0].startswith("-"):
            option, remaining = remaining[0], remaining[1:]
            if option == "--":
                break
            if program == "env" and option in {"-", "-i", "--ignore-environment"}:
                environment.clear()
                continue
            flags = {"env": {"-v", "--debug"}, "nice": set(),
                     "timeout": {"--foreground", "--preserve-status", "-v", "--verbose"}}
            if option in flags[program] or program == "nice" and re.fullmatch(r"-[0-9]+", option):
                continue
            options = {"env": ("-u", "--unset", "-C", "--chdir"),
                       "nice": ("-n", "--adjustment"),
                       "timeout": ("-s", "--signal", "-k", "--kill-after")}[program]
            name = next((name for name in options if option == name or
                         (option.startswith(name + "=") if name.startswith("--") else
                          option.startswith(name) and len(option) > len(name))), None)
            if name is None:
                return environment, words, cwd
            if option == name:
                if not remaining:
                    return environment, words, cwd
                value, remaining = remaining[0], remaining[1:]
            else:
                value = option[len(name):].removeprefix("=")
            if program == "env":
                if name in {"-C", "--chdir"}:
                    cwd = str(Path(cwd or ".") / os.path.expanduser(value))
                else:
                    environment = [entry for entry in environment if entry.partition("=")[0] != value]
        if program == "timeout":
            remaining = remaining[1:]  # duration is not the executed program
        words = remaining
    return environment, words, cwd


def _operand_values(word: str) -> list[str]:
    """The path spellings a word may carry: itself, `name=value`, `-Xvalue`."""
    values = [] if word.startswith("-") else [word]
    if "=" in word:
        values.append(word.partition("=")[2])
    if word.startswith("-") and not word.startswith("--") and len(word) > 2 and word[2] in "/~.":
        values.append(word[2:])
    return [value for value in values if value]


#: The refusal for an operand that may name a store below an unknown directory.
_UNRESOLVED_CREDENTIAL = ("cannot guard an operand that may name a credential store after an "
                          "unresolved directory change; use an explicit absolute path")


def _tilde_directory(value: str) -> tuple[str, str] | None:
    """The `~+`, `~-` or `~N`/`~+N`/`~-N` prefix of a value, and the rest.

    `~+` is $PWD, `~-` is $OLDPWD and the others name directory-stack
    entries. A bare `~` and `~user` are left to `os.path.expanduser`.
    """
    match = re.match(r"~([+-]?[0-9]*)(?=/|$)", value)
    return (match[1], value[match.end():]) if match and match[1] else None


#: Programs that only print or search their arguments, so an `env` argument
#: never runs (REV-010). Every other program may run `env` as a runner.
_ARGUMENT_READERS = frozenset({"echo", "printf", "grep", "egrep", "fgrep", "rg"})


def _could_name_credential(word: str, policy: Any, dotglob: bool, nocaseglob: bool = False) -> bool:
    """Whether a relative operand from an unknown directory may name a store.

    The unknown directory may be any directory: a store's parent, so the
    operand must spell the rest of a store's path, or the inside of a
    directory store, so the operand names one of its files.
    """
    dotglob = dotglob or _environment_dotglob()
    nocaseglob = nocaseglob or _environment_nocaseglob()
    stores = [candidate.parts
              for store in _guarded_stores(policy)
              for candidate in (Path(os.path.expanduser(store)), Path(os.path.expanduser(store)).resolve())]
    if not stores:
        return False
    for value in _operand_values(word):
        tilde = _tilde_directory(value)
        if tilde:
            # `~+/x`, `~-/x`: the rest is relative to an unknown directory.
            value = tilde[1].lstrip("/")
        if os.path.isabs(os.path.expanduser(value)):
            continue
        parts = Path(value).parts
        if ".." in parts:
            # A '..' may cross a symlink: what follows starts anywhere.
            parts = parts[len(parts) - parts[::-1].index(".."):]
        if not parts:
            continue
        pattern = tuple(_ANY_PART if "{" in part or "}" in part else part for part in parts)
        if any(_reaches(pattern, store[index:], dotglob, nocaseglob)
               for store in stores for index in range(1, len(store))):
            return True
        # A store's parent below the home directory, such as `.codex`, is
        # read whole by recursive copies and archives.
        home = len(Path.home().parts)
        if any(len(pattern) < len(store) - index
               and all(_part_matches(part, name, dotglob, nocaseglob) for part, name in zip(pattern, store[index:]))
               for store in stores for index in range(home, len(store))):
            return True
        # A key-like last part (`id_rsa`, `x/*.pem`, `keys/*rsa`) may name a
        # file of a directory store at any depth. A bare wildcard, a brace,
        # `config` or `hosts.yml` does so only when it stands alone, so the
        # unknown directory may be the store, or when its parent may name a
        # store's directory (`.s?h/*`, `cache/*`): `build/*` below an
        # unknown directory names no store.
        last = pattern[-1]
        literal = len(_BRACKET.sub("", last).replace("*", "").replace("?", ""))
        store_names = {name for store in stores for name in store[home:]}
        if (any(fnmatch.fnmatchcase(last, name) for name in _CREDENTIAL_KEY_PATTERNS)
                or (last != _ANY_PART and literal >= 2
                    and any(_part_matches(last, name, dotglob, nocaseglob) for name in _CREDENTIAL_KEY_EXAMPLES))
                or (any(_part_matches(last, name, dotglob, nocaseglob) for name in _CREDENTIAL_FILE_EXAMPLES)
                    and (len(pattern) == 1
                         or any(_part_matches(pattern[-2], name, dotglob, nocaseglob) for name in store_names)
                         # `cache/*` from inside `sso` or `cli`; `build/cache/*` is no cache.
                         or (len(pattern) == 2 and _part_matches(pattern[0], "cache", dotglob, nocaseglob))))):
            return True
        # Token caches sit in `sso/cache` and `cli/cache`.
        if any(_part_matches(part, name, dotglob, nocaseglob) and _part_matches(following, "cache", dotglob, nocaseglob)
               for part, following in zip(pattern, pattern[1:]) for name in _CREDENTIAL_SUBDIRECTORIES):
            return True
    return False


def _environment_dotglob() -> bool:
    """Whether the inherited shell state may let globs match dotfiles."""
    return bool(os.environ.get("GLOBIGNORE")) or "dotglob" in os.environ.get("BASHOPTS", "")


def _environment_nocaseglob() -> bool:
    return "nocaseglob" in os.environ.get("BASHOPTS", "").split(":")


def _shell_settings(parsed: ParsedCommand) -> set[str]:
    """Settings visible after the same decoding used by the operand guards.

    Flattened shell analysis cannot prove order or computed assignment names.
    An assigning builtin may therefore set any path/glob setting; an unknown
    shopt/set option may enable either glob mode. No shell code is evaluated.
    """
    settings: set[str] = set()
    names = {"CDPATH", "HOME", "OLDPWD", "PWD", "GLOBIGNORE", "BASHOPTS", "dotglob", "nocaseglob"}
    assigners = {"declare", "typeset", "export", "local", "readonly", "read", "printf", "mapfile", "readarray"}
    for segment in parsed.segments:
        for word in segment.words:
            settings.update(name for name in names if re.search(
                r"(?<![$A-Za-z0-9_{])" + name + r"(?![A-Za-z0-9_])", word))
        _, words = command_parts(segment.words)
        words = _unwrapped_builtin(words) or []
        if words[:1] and words[0] in assigners:
            settings.update(names)
        if words[:1] and words[0] in {"shopt", "set"} and any("$" in word or "`" in word for word in words[1:]):
            settings.update({"dotglob", "nocaseglob"})
    return settings


#: A `-C`/`--chdir` option inside an `env -S` value, whatever separates its
#: arguments (`env` also splits on `\_`).
_SPLIT_CHDIR = re.compile(r"-(?:[A-Za-z0-9]*C|-c)")


def _env_chdir(words: list[str]) -> str | None:
    """The directory `env -C`/`--chdir` runs its program in, if any."""
    index = 1
    while index < len(words):
        word = words[index]
        following = words[index + 1] if index + 1 < len(words) else None
        if word == "--":
            return None
        if word.startswith("--"):
            # Long options may be abbreviated to any unique prefix: --c=dir.
            name, equals, value = word[2:].partition("=")
            if name and "chdir".startswith(name):
                return value if equals else following
            if name and "split-string".startswith(name) and _SPLIT_CHDIR.search((value if equals else following) or ""):
                return "$SPLIT_STRING"
            if not equals and name and any(option.startswith(name) for option in ("unset", "split-string", "argv0")):
                index += 1
        elif word.startswith("-"):
            # Combined short options: -vC dir, -iCdir. The value of -u, -S
            # or -a is the rest of the word or the next word: -uLC_ALL.
            for position, letter in enumerate(word[1:], start=2):
                rest = word[position:]
                if letter == "C":
                    return rest or following
                if letter in "uSa":
                    # -S splits its value into options: "-S-C dir" changes directory.
                    if letter == "S" and _SPLIT_CHDIR.search(rest or following or ""):
                        return "$SPLIT_STRING"
                    if not rest:
                        index += 1
                    break
        elif not _ENV_ASSIGNMENT.match(word):
            return None
        index += 1
    return None


def _compound_analysis(tool: str, tool_input: dict[str, Any], parsed: ParsedCommand,
                       policy: Any, cwd: str | None, *, _credential_only: bool = False) -> tuple[str | None, bool]:
    """Guard every segment of shell source against every directory it may run in.

    Returns the guard, and whether a directory change could not be resolved.
    """
    settings = _shell_settings(parsed)
    dotglob = bool(settings & {"dotglob", "GLOBIGNORE", "BASHOPTS"}) or _environment_dotglob()
    nocaseglob = bool(settings & {"nocaseglob", "BASHOPTS"}) or _environment_nocaseglob()
    # CDPATH resolves a relative cd target through other directories.
    cdpath = "CDPATH" in settings or bool(os.environ.get("CDPATH"))
    # A reassigned HOME moves `cd` and `cd ~`, and the parser expands $HOME
    # to the real home; a reassigned OLDPWD moves `cd -`.
    # Any HOME that is not an expansion may set it (HOME=, read HOME,
    # printf -v HOME), and so may a builtin that assigns a quoted or
    # computed name (declare "HO""ME=…").
    home_assigned = "HOME" in settings
    oldpwd_assigned = "OLDPWD" in settings
    # A reassigned PWD moves `~+` (REV-009).
    pwd_assigned = "PWD" in settings
    tilde_unresolved = False
    home = str(Path.home())
    segment_cwd: str | None = str(Path(cwd or ".").resolve())
    possible_cwds: set[str | None] = {segment_cwd, os.path.abspath(cwd or ".")}
    previous_cwd: str | None = None
    directory_stack: list[str | None] = []
    for segment in parsed.segments:
        _, segment_words = command_parts(segment.words)
        # A cd may fail, be conditional or run in a subshell. The
        # flattened analysis cannot discard any earlier cwd.
        possible_cwds.add(segment_cwd)
        operands = segment_words[1:] + [target for _, _, target in segment.redirects if target]
        # `~-`, `~N` and, after a PWD assignment, `~+` name a directory the
        # hook cannot resolve, whatever the working directory (REV-009).
        unknown_tilde = [value for word in operands for value in _operand_values(word)
                         if (tilde := _tilde_directory(value)) and (tilde[0] != "+" or pwd_assigned)]
        if unknown_tilde:
            tilde_unresolved = True
            if any(_could_name_credential(value, policy, dotglob, nocaseglob) for value in unknown_tilde):
                return _UNRESOLVED_CREDENTIAL, True
        for possible_cwd in possible_cwds:
            if possible_cwd is None:
                if any(_could_name_credential(word, policy, dotglob, nocaseglob) for word in operands):
                    return _UNRESOLVED_CREDENTIAL, True
            guard = protected_reason(tool, tool_input, policy, possible_cwd, _parsed=segment,
                                     _dotglob=dotglob, _nocaseglob=nocaseglob, _credential_only=_credential_only)
            if guard:
                return guard, None in possible_cwds
        unwrapped = _unwrapped_builtin(segment_words) or []
        if unwrapped[:1] and unwrapped[0] in _CODE_LOADERS:
            segment_cwd = None
            continue
        directory_words = _directory_builtin(segment_words)
        if directory_words is None:
            continue
        program = directory_words[0]
        args = directory_words[1:]
        no_chdir = False
        while args and args[0].startswith("-") and args[0] != "-":
            option, args = args[0], args[1:]
            if option == "--":
                break
            if option == "-n" and program in {"pushd", "popd"}:
                no_chdir = True
            elif option not in {"-L", "-P", "-e", "-@"}:
                args = ["$UNKNOWN"]
                break
        if no_chdir:
            # -n changes only the stack, never the working
            # directory. Do not infer its later restored path.
            if program == "pushd":
                directory_stack.append(None)
            elif directory_stack:
                directory_stack.pop()
            continue
        if program == "popd":
            segment_cwd = directory_stack.pop() if directory_stack and not args else None
            continue
        if program == "pushd" and not args:
            if directory_stack:
                segment_cwd, directory_stack[-1] = directory_stack[-1], segment_cwd
            else:
                segment_cwd = None
            continue
        target = args[0] if args else home
        tilde = _tilde_directory(target)
        if tilde:
            # `cd ~+/x` stays relative to the current directory; `cd ~-` and
            # stack entries go where the hook cannot follow.
            target = "." + tilde[1] if tilde[0] == "+" and not pwd_assigned else "$TILDE"
        if (cdpath and target != "-" and not target.startswith(("+", "-"))
                and not os.path.isabs(os.path.expanduser(target))):
            target = "$CDPATH"
        if home_assigned and (target.startswith("~") or target == home or target.startswith(home + "/")):
            target = "$HOME"
        if program == "pushd":
            directory_stack.append(segment_cwd)
        if target == "-" and program == "cd":
            segment_cwd, previous_cwd = None if oldpwd_assigned else previous_cwd, segment_cwd
            continue
        previous_cwd, current = segment_cwd, segment_cwd
        expanded = os.path.expanduser(target)
        dynamic_target = any(char in target for char in "$`*?[") or _has_extglob(target)
        if not dynamic_target:
            # Relative cd must transition every possible
            # cwd, not just the last scanned one. Retain
            # logical (-L) and physical (-P) spellings.
            bases = {"/"} if os.path.isabs(expanded) else set(possible_cwds)
            for base in bases:
                if base is None:
                    possible_cwds.add(None)
                else:
                    joined = Path(base) / expanded
                    possible_cwds.update((os.path.abspath(joined), str(joined.resolve())))
            if len(possible_cwds) > 64:
                possible_cwds = {None}
        segment_cwd = (str((Path(current or ".") / expanded).resolve())
                       if not dynamic_target
                       and (current is not None or os.path.isabs(expanded))
                       and not (program == "pushd" and target.startswith(("+", "-"))) else None)
    return None, None in possible_cwds or segment_cwd is None or tilde_unresolved


def unresolved_directory(command: str, policy: Any, cwd: str | None = None) -> bool:
    """Whether shell source changes into a directory the hook cannot resolve."""
    try:
        parsed = parse_command(command, policy.tmpdir)
        if parsed.shell_source is None:
            return False
        return _compound_analysis("Bash", {"command": command}, parsed, policy, cwd)[1]
    except (PolicyError, ValueError, TypeError, OSError, KeyError):
        return False


def guard_consent_reason(tool: str, tool_input: dict[str, Any], policy: Any,
                         cwd: str | None) -> str | None:
    """The #456 warning for an exact-only Git/gh exception, never authority."""
    if tool != "Bash":
        return None
    parsed = parse_command(tool_input.get("command", ""), policy.tmpdir)
    class InspectionNeeded(Exception):
        pass
    def no_execution(*args, **kwargs):
        # Identity construction never performs provider lookups or runs a
        # subprocess. Live publication checks belong to protected_reason.
        raise InspectionNeeded()
    # Segments are already parsed and include empty redirect-only entries and
    # shell grammar words. Re-parsing their joined words can recreate the same
    # compound node indefinitely (case statements inside substitutions).
    for segment in parsed.segments if parsed.shell_source is not None else (parsed,):
        environment, words, guard_cwd = _runner_command_parts(list(segment.words), cwd)
        if not words or _program(words[0]) not in {"git", "gh"}:
            continue
        if any(_GUARD_DEFEATING_ENV.match(value.partition("=")[0]) for value in environment):
            return "environment prefixes may select programs or redirect Git/gh operations"
        checker = _check_git if _program(words[0]) == "git" else _check_gh
        try:
            if checker is _check_git and _git_global_options(list(words[1:]))[0] is None:
                return "unknown Git option arity leaves the remaining operation opaque; exact interpreter consent required"
            checker(list(words[1:]), policy, cwd=guard_cwd, run=no_execution)
        except PolicyError as error:
            return str(error)
        except InspectionNeeded:
            continue
    return None


def protected_reason(tool: str, tool_input: dict[str, Any], policy: Any, cwd: str | None,
                     *, _parsed: ParsedCommand | None = None, _dotglob: bool = False,
                     _nocaseglob: bool = False, _credential_only: bool = False) -> str | None:
    """Guards consent cannot remove on directly expressed operations.

    Never pretend to inspect code inside an interpreter or a project program.
    """
    try:
        # Segment analysis uses the raw credential guard to discover candidate
        # stores before consent. The outer operation binds the approved stores.
        sensitive = (None if _parsed is not None else
                     sensitive_operation(tool, tool_input, policy, cwd, resolve=False))
        if sensitive and (sensitive["kind"] == "unconfined_publication" or sensitive.get("publication_unconfined")):
            if tool == "Bash":
                source = _parsed or parse_command(tool_input.get("command", ""), policy.tmpdir)
                if source.shell_source is not None:
                    for segment in source.segments:
                        detail = sensitive_operation(tool, tool_input, policy, cwd, resolve=False, _parsed=segment)
                        if detail and (detail.get("publication_unconfined") or detail["kind"] == "unconfined_publication"):
                            continue
                        _, words, directory = _runner_command_parts(list(segment.words), cwd)
                        if not words or _program(words[0]) in {"echo", "printf", "true", "false", ":", "wait"}:
                            continue
                        try:
                            _check_words(words, segment, policy, cwd=directory, run=subprocess.run)
                        except ConsentRequired:
                            # The outer answer is that consent; it is not a guard.
                            # Git keeps every other direct argument guard.
                            inner = (words[2:] if words[:2] == ["rtk", "proxy"]
                                     else words[1:] if words[:1] == ["rtk"] else words)
                            if inner and os.path.basename(inner[0]).removesuffix(".exe") == "git":
                                _check_git(inner[1:], policy, cwd=directory, run=subprocess.run,
                                           directory_consent=True)
            policy = replace(policy, static_only=False)
        if sensitive and sensitive.get("stores"):
            policy = replace(policy, credential_reads=tuple(dict.fromkeys(
                (*policy.credential_reads, *sensitive["stores"]))))
        if sensitive and sensitive["kind"] == "git_config":
            if policy.role not in WRITING_ROLES:
                return "this role may not modify the Git workspace"
            return None
        if tool == "Bash":
            command = tool_input.get("command")
            if not isinstance(command, str):
                return "command must be a string"
            parsed = _parsed or parse_command(command, policy.tmpdir)
            if parsed.shell_source is not None:
                return _compound_analysis(tool, tool_input, parsed, policy, cwd, _credential_only=_credential_only)[0]
            for _, operator, target in parsed.redirects:
                if (target is not None and operator.startswith(">")
                        and not inside_tmpdir(str(Path(cwd or ".") / target), policy.tmpdir)):
                    return "output may be redirected only into $TMPDIR; pattern grants do not remove this guard"
            redirect_paths = [target for _, _, target in parsed.redirects if target is not None]
            words = list(parsed.words)
            if not words:
                return credential_reason(redirect_paths, policy, cwd, dotglob=_dotglob, nocaseglob=_nocaseglob)
            # An environment prefix never hides the program from its guards.
            environment, words = command_parts(words)
            if not words:
                return "an environment prefix needs a command"
            hidden = hidden_program(environment, words)
            if hidden:
                return hidden
            outer_program = _program(words[0])
            guard_environment, guarded_words, guard_cwd = _runner_command_parts(list(parsed.words), cwd)
            hidden = hidden_program(guard_environment, guarded_words)
            if hidden:
                return hidden
            program = _program(guarded_words[0]) if guarded_words else ""
            # Re-run all Git/GitHub argument, publication and workspace guards.
            # A grant is not a different stage role or a different PR context.
            if program == "git" and not _credential_only:
                # Apply only repository selectors when the guard reads HEAD.
                # Other environment settings may execute programs and remain
                # inside the executor rather than the host-side inspector.
                selectors = ["--" + {"GIT_DIR": "git-dir", "GIT_WORK_TREE": "work-tree"}[name] + "=" + value
                             for assignment in guard_environment
                             for name, _, value in [assignment.partition("=")]
                             if name in {"GIT_DIR", "GIT_WORK_TREE"}]
                _check_git([*selectors, *guarded_words[1:]], policy, cwd=guard_cwd, run=subprocess.run, consent=True)
            elif program == "gh" and not _credential_only:
                _check_gh(guarded_words[1:], policy, cwd=guard_cwd, run=subprocess.run, consent=True)
            elif program == "find" and not _credential_only:
                if any(word in FIND_FORBIDDEN for word in guarded_words[1:]):
                    return "find arguments that execute or write retain their direct-command guard"
            paths = [assignment.partition("=")[2] for assignment in environment] + words[1:] + redirect_paths
            # `env -C` runs its program in another directory (REV-008), also
            # behind `command`, `nice`, `timeout -s KILL 5` or any other runner
            # (REV-010). Only a program that just prints or searches its
            # arguments cannot run `env`.
            chdir = None if outer_program in _ARGUMENT_READERS else next(
                (found for index, word in enumerate(words)
                 if os.path.basename(word).removesuffix(".exe") == "env"
                 for found in (_env_chdir(words[index:]),) if found is not None), None)
            if chdir is not None:
                tilde = _tilde_directory(chdir)
                if tilde:
                    chdir = "." + tilde[1] if tilde[0] == "+" else "$TILDE"
                if any(char in chdir for char in "$`*?[{}") or _has_extglob(chdir):
                    if any(_could_name_credential(path, policy, _dotglob, _nocaseglob) for path in paths):
                        return _UNRESOLVED_CREDENTIAL
                else:
                    protected = credential_reason(
                        paths, policy, str(Path(cwd or ".") / os.path.expanduser(chdir)),
                        dotglob=_dotglob, nocaseglob=_nocaseglob)
                    if protected:
                        return protected
        else:
            paths = [tool_input.get(FILE_EDIT_TOOLS[tool], "")]
        protected = credential_reason(paths, policy, cwd, dotglob=_dotglob, nocaseglob=_nocaseglob)
        if protected:
            return protected
        for path in paths:
            candidate = (Path(cwd or ".") / os.path.expanduser(path)).resolve()
            if tool != "Bash" and (any(
                    part.casefold().split(":", 1)[0].rstrip(" .") == ".git"
                    for part in candidate.parts) or any(
                    candidate == root or root in candidate.parents for root in _git_config_paths())):
                return "Git configuration edits defeat command validation; use external administration"
    except (PolicyError, ValueError, TypeError, OSError, KeyError) as error:
        return str(error)
    except Exception as error:  # fail closed: a guard that cannot run grants nothing
        return f"the guard could not inspect the command ({type(error).__name__})"
    return None



def _guarded_stores(policy: Any) -> list[str]:
    return [store for store in (*CREDENTIAL_STORES, GH_CREDENTIAL_STORE)
            if store not in policy.credential_reads and not
            (store == GH_CREDENTIAL_STORE and policy.credential_publishing)]


def _part_matches(pattern: str, name: str, dotglob: bool, nocaseglob: bool = False) -> bool:
    """Whether one path component pattern may match one name, as bash globs."""
    if pattern.startswith(_LITERAL):
        return pattern[len(_LITERAL):] == name
    if pattern in {_ANY_PART, name}:
        return True
    if _has_extglob(pattern):
        # Alternatives, repetition and negation are not fnmatch syntax. A
        # component may match any name; it still cannot cross a slash.
        return True
    if not any(char in pattern for char in "*?["):
        return False
    # Without dotglob, a leading * or ? never matches a dotfile.
    if name.startswith(".") and pattern[:1] in {"*", "?"} and not dotglob:
        return False
    # fnmatch knows neither negation with ^ nor character classes: a bracket
    # expression matches one character, which is enough to stay conservative.
    pattern = _BRACKET.sub("?", pattern)
    return fnmatch.fnmatchcase(name.casefold(), pattern.casefold()) if nocaseglob else fnmatch.fnmatchcase(name, pattern)


def _reaches(pattern: tuple[str, ...], store: tuple[str, ...], dotglob: bool, nocaseglob: bool = False) -> bool:
    """Whether a path spelled by these parts may be the store or inside it.

    Iterative over (pattern, store) positions: bounded by their product, so
    no input recurses or backtracks exponentially.
    """
    seen: set[tuple[int, int]] = set()
    pending = [(0, 0)]
    while pending:
        state = pending.pop()
        if state in seen:
            continue
        seen.add(state)
        index, depth = state
        if depth == len(store):
            return True
        if index == len(pattern):
            continue
        head = pattern[index]
        if head == _ANY_PARTS:
            pending += [(index + 1, depth), (index, depth + 1)]
        elif head == "**":
            pending.append((index + 1, depth))
            if _part_matches("*", store[depth], dotglob, nocaseglob):
                pending.append((index, depth + 1))
        elif _part_matches(head, store[depth], dotglob, nocaseglob):
            pending.append((index + 1, depth + 1))
    return False


def _brace_crosses_path(value: str) -> bool:
    """Whether a brace alternative holds a '/' or '~', so may start anywhere."""
    depth = 0
    for char in value:
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(depth - 1, 0)
        elif depth and char in "/~":
            return True
    return False


def _has_extglob(part: str) -> bool:
    return any(operator + "(" in part for operator in "@+!*?")


def credential_reason(words, policy, cwd: str | None, *, dotglob: bool = False,
                      nocaseglob: bool = False) -> str | None:
    dotglob = dotglob or _environment_dotglob()
    nocaseglob = nocaseglob or _environment_nocaseglob()
    stores = [candidate.parts
              for store in _guarded_stores(policy)
              for candidate in (Path(os.path.expanduser(store)), Path(os.path.expanduser(store)).resolve())]
    base = Path(os.path.abspath(cwd or "."))
    real_base = Path(cwd or ".").resolve()

    def literal(parts: tuple[str, ...], prefix: tuple[str, ...]) -> tuple[str, ...]:
        # The working directory is a real path: its glob characters are literal.
        count = len(prefix) if parts[:len(prefix)] == prefix else 0
        return tuple(_LITERAL + part for part in parts[:count]) + parts[count:]

    for word in words:
        for value in dict.fromkeys([word, *_operand_values(word)]):
            tilde = _tilde_directory(value)
            if tilde:
                # `~+` is the working directory; `~-` and stack entries are
                # unknown, so only a name check applies (REV-009).
                if tilde[0] != "+":
                    if _could_name_credential(value, policy, dotglob, nocaseglob):
                        return _UNRESOLVED_CREDENTIAL
                    continue
                value = "." + tilde[1]
            operand = Path(os.path.expanduser(value))
            own = operand.parts
            path = base / operand
            lexical = Path(os.path.abspath(path)).parts
            normalized = Path(os.path.normpath(operand)).parts
            ups = len(normalized) - len(tuple(itertools.dropwhile(lambda part: part == "..", normalized)))
            kept = base.parts[:max(1, len(base.parts) - ups)]
            # Retain both spellings: a glob cannot resolve a symlink in a filename.
            spellings = [lexical if operand.is_absolute() else literal(lexical, kept),
                         path.resolve().parts if operand.is_absolute()
                         else literal((real_base / operand).resolve().parts, real_base.parts)]
            expanded = [index for index, part in enumerate(own)
                        if any(char in part for char in "{}*?[") or _has_extglob(part)]
            if expanded:
                # Lexical normalization cannot cross an expanded part: a '..'
                # after one, or inside braces, may lead to any directory.
                escapes = [index for index, part in enumerate(own)
                           if (part == ".." and index > expanded[0])
                           or (".." in part and ("{" in part or "}" in part))]
                if escapes:
                    spellings.append((_ANY_PARTS, *own[escapes[-1] + 1:]))
            if _brace_crosses_path(value):
                spellings.append((_ANY_PARTS, *own))
            for parts in spellings:
                # Brace alternatives expand before a program receives argv. Match
                # one component at a time, so `*` never crosses a '/'.
                pattern = tuple(_ANY_PART if ("{" in part or "}" in part) and not part.startswith(_LITERAL)
                                else part for part in parts)
                if any(_reaches(pattern, store, dotglob, nocaseglob) for store in stores):
                    return "protected credential reads cannot be granted"
    return None



if __name__ == "__main__":  # pragma: no cover - exercised through the hook tests
    if sys.argv[1:2] == ["hook"] and len(sys.argv) == 3:
        sys.exit(hook_main(sys.argv[2:]))
    print("usage: stage_permissions.py hook <policy.json>", file=sys.stderr)
    sys.exit(2)
