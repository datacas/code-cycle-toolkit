"""What a publishing stage left on the code host, checked after it (#261).

The cycle never merges, never force-pushes and never deletes refs. A stage's
command table (#142) narrows what it can run, but code a verification executes
and tools its host pre-approves can still reach the code host. So after every
publishing stage the runtime compares the state the invariants name with the
state it read before the stage:

1. every change request of the cycle that was open is still open, and any the
   stage created is open; none was merged or closed;
2. the cycle branch did not move, or moved fast-forward from its old head;
3. the default branch and the branches the code host reports as protected did
   not move, and none of the audited refs was deleted.

Only the refs the invariants name are read, on `origin` only, so a push to an
unrelated branch elsewhere never fails the check (#197). The runtime cannot
tell who moved a ref during a stage, so a breach is reported as an invariant
that failed, "changed outside the stage?", never as proof that the stage did
it. A state that cannot be read is never read as "nothing there": it raises
`AuditUnreadable`, and the cycle stops instead of continuing with incomplete
coverage. A code host this module cannot read (anything but GitHub until #238)
is reported `unverified` for the parts it could not check, never as passed.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.parse import quote

#: The only remote the audit reads.
AUDITED_REMOTE = "origin"
#: At most this many commits per direction are kept as evidence of a move.
COMMIT_LIMIT = 20

PASSED = "passed"
VIOLATED = "violated"
UNVERIFIED = "unverified"

#: Breach kinds, closed vocabulary.
MERGED = "change_request_merged"
CLOSED = "change_request_closed"
NON_FAST_FORWARD = "non_fast_forward"
MOVED = "protected_ref_moved"
DELETED = "ref_deleted"

#: Parts of the audit a code host without a reader leaves unchecked.
CHANGE_REQUEST_STATE = "change_request_state"
PROTECTED_BRANCHES = "protected_branches"
#: A remote with no branch yet and no configured default branch.
DEFAULT_BRANCH = "default_branch"

_OPEN = "OPEN"


class AuditUnreadable(RuntimeError):
    """The audited state could not be read; the coverage would be incomplete."""


class HostReader(Protocol):
    """How the audit reads a code host; injected so tests need no network."""

    def protected_branches(self, repo: str) -> tuple[str, ...]:
        """Every branch the code host reports as protected, possibly none."""

    def change_requests(self, repo: str, branch: str | None,
                        change_request_id: str | None) -> dict[str, dict]:
        """`{number: {"state": OPEN|CLOSED|MERGED, "head_ref": name}}` of the
        change requests whose head is `branch`, and of `change_request_id`."""


class GitHubHostReader:
    """`HostReader` for GitHub, through `gh api`."""

    def __init__(self, cwd: str | None = None, *, run: Callable = subprocess.run,
                 timeout: float = 30) -> None:
        self.cwd = cwd
        self.run = run
        self.timeout = timeout

    def _api(self, path: str, *, paginate: bool = False):
        command = ["gh", "api"]
        if paginate:
            command += ["--paginate", "--slurp"]
        command.append(path)
        try:
            done = self.run(command, cwd=self.cwd, capture_output=True, text=True,
                            timeout=self.timeout, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise AuditUnreadable("GitHub tooling unavailable or timed out") from error
        if done.returncode:
            raise AuditUnreadable(f"gh api {path.split('?')[0]} exited {done.returncode}")
        try:
            return json.loads(done.stdout)
        except ValueError as error:
            raise AuditUnreadable("GitHub returned invalid JSON") from error

    @staticmethod
    def _items(pages) -> list[dict]:
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise AuditUnreadable("GitHub returned pages in an unknown shape")
        items = [item for page in pages for item in page]
        if any(not isinstance(item, dict) for item in items):
            raise AuditUnreadable("GitHub returned an entry in an unknown shape")
        return items

    def protected_branches(self, repo: str) -> tuple[str, ...]:
        names = []
        for branch in self._items(self._api(
                f"repos/{repo}/branches?protected=true&per_page=100", paginate=True)):
            name = branch.get("name")
            if not isinstance(name, str) or not name:
                raise AuditUnreadable("a protected branch names no branch")
            names.append(name)
        return tuple(dict.fromkeys(names))

    @staticmethod
    def _pull(pull: dict) -> tuple[str, dict]:
        number, state = pull.get("number"), pull.get("state")
        head = pull.get("head")
        head_ref = head.get("ref") if isinstance(head, dict) else None
        if (isinstance(number, bool) or not isinstance(number, int)
                or not isinstance(state, str) or not isinstance(head_ref, str)):
            raise AuditUnreadable("a pull request came back in an unknown shape")
        merged = pull.get("merged") is True or bool(pull.get("merged_at"))
        return str(number), {"state": "MERGED" if merged else state.upper(),
                             "head_ref": head_ref}

    def change_requests(self, repo: str, branch: str | None,
                        change_request_id: str | None) -> dict[str, dict]:
        found: dict[str, dict] = {}
        if branch:
            owner = repo.partition("/")[0]
            head = quote(f"{owner}:{branch}", safe="")
            for pull in self._items(self._api(
                    f"repos/{repo}/pulls?state=all&head={head}&per_page=100",
                    paginate=True)):
                number, state = self._pull(pull)
                found[number] = state
        if change_request_id:
            pull = self._api(f"repos/{repo}/pulls/{change_request_id}")
            if not isinstance(pull, dict):
                raise AuditUnreadable("the pull request came back in an unknown shape")
            number, state = self._pull(pull)
            found[number] = state
        return found


@dataclass(frozen=True)
class Snapshot:
    """The audited state, read once before a stage."""

    cycle_branch: str | None
    default_branch: str | None
    protected: tuple[str, ...]
    #: Branch name to remote head SHA, None when the branch was absent.
    refs: dict[str, str | None]
    change_requests: dict[str, dict]
    unverified: tuple[str, ...] = ()


@dataclass(frozen=True)
class Breach:
    kind: str
    ref: str
    before: str | None = None
    after: str | None = None
    commits_added: tuple[str, ...] = ()
    commits_removed: tuple[str, ...] = ()

    def evidence(self) -> dict:
        item = {"kind": self.kind, "ref": self.ref,
                "before": self.before, "after": self.after}
        if self.commits_added:
            item["commits_added"] = list(self.commits_added)
        if self.commits_removed:
            item["commits_removed"] = list(self.commits_removed)
        return item

    def explain(self) -> str:
        if self.kind in {MERGED, CLOSED}:
            return f"{self.ref} is {self.after} (was {self.before or 'absent'})"
        if self.kind == DELETED:
            return f"{self.ref} was deleted (was {self.before})"
        what = "moved non-fast-forward" if self.kind == NON_FAST_FORWARD else "moved"
        return f"{self.ref} {what}: {self.before or 'absent'} -> {self.after}"


@dataclass(frozen=True)
class AuditResult:
    status: str
    before: Snapshot
    after_refs: dict[str, str | None]
    after_change_requests: dict[str, dict]
    breaches: tuple[Breach, ...] = ()
    unverified: tuple[str, ...] = ()

    def evidence(self) -> dict:
        """JSON-safe: SHAs, ref names, states and breach kinds, never prose."""
        refs = {name: {"before": sha, "after": self.after_refs.get(name)}
                for name, sha in sorted(self.before.refs.items())}
        requests = {number: {"before": (self.before.change_requests.get(number) or {}).get("state"),
                             "after": state.get("state")}
                    for number, state in sorted(self.after_change_requests.items())}
        evidence = {"status": self.status, "remote": AUDITED_REMOTE,
                    "cycle_branch": self.before.cycle_branch,
                    "default_branch": self.before.default_branch,
                    "protected": list(self.before.protected),
                    "refs": refs, "change_requests": requests,
                    "breaches": [breach.evidence() for breach in self.breaches]}
        if self.unverified:
            evidence["unverified"] = list(self.unverified)
        return evidence

    def explain(self, role: str) -> str:
        details = "; ".join(breach.explain() for breach in self.breaches)
        return (f"publication invariant failed during the {role} stage (changed outside "
                f"the stage?): {details}")


def _remote_heads(git: Callable[..., str]) -> dict[str, str]:
    heads = {}
    for line in git("ls-remote", "--heads", AUDITED_REMOTE).splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/") and sha:
            heads[ref[len("refs/heads/"):]] = sha
    return heads


def _default_branch(git: Callable[..., str], heads: dict[str, str]) -> str | None:
    """`origin`'s default branch; None only for a remote that has no branch yet."""
    for line in git("ls-remote", "--symref", AUDITED_REMOTE, "HEAD").splitlines():
        target, _, name = line.partition("\t")
        if target.startswith("ref: refs/heads/") and name == "HEAD":
            return target[len("ref: refs/heads/"):]
    if not heads:
        return None
    raise AuditUnreadable(f"{AUDITED_REMOTE} names no default branch")


class PublicationAuditor:
    """Read the audited state before a publishing stage and verify it after."""

    def __init__(self, cwd: str, repo: str, host: HostReader | None, *,
                 default_branch: str | None = None,
                 run: Callable = subprocess.run, timeout: float = 60) -> None:
        """`default_branch` is the configured one; otherwise `origin`'s HEAD."""
        self.cwd = cwd
        self.repo = repo
        self.host = host
        self.default_branch = default_branch
        self.run = run
        self.timeout = timeout

    def _git(self, *args: str, check: bool = True) -> str:
        try:
            done = self.run(["git", "-C", self.cwd, *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=self.timeout,
                            check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise AuditUnreadable(f"git {args[0]} could not run") from error
        if check and done.returncode:
            raise AuditUnreadable(f"git {args[0]} exited {done.returncode}")
        return done.stdout

    def _status(self, *args: str) -> int:
        try:
            return self.run(["git", "-C", self.cwd, *args], capture_output=True, text=True,
                            timeout=self.timeout, check=False).returncode
        except (OSError, subprocess.SubprocessError) as error:
            raise AuditUnreadable(f"git {args[0]} could not run") from error

    def _change_requests(self, branch: str | None, request: str | None) -> dict[str, dict]:
        if self.host is None:
            return {}
        return self.host.change_requests(self.repo, branch, request)

    def capture(self, *, checkout_branch: str | None,
                change_request_id: str | None = None) -> Snapshot:
        """The audited state now. Raises `AuditUnreadable` on any doubt."""
        heads = _remote_heads(self._git)
        default = self.default_branch or _default_branch(self._git, heads)
        unverified: list[str] = [] if default else [DEFAULT_BRANCH]
        if self.host is None:
            protected: tuple[str, ...] = ()
            unverified += [CHANGE_REQUEST_STATE, PROTECTED_BRANCHES]
        else:
            protected = tuple(self.host.protected_branches(self.repo))
        requests = self._change_requests(checkout_branch, change_request_id)
        branch = checkout_branch
        if change_request_id and change_request_id in requests:
            # The change request's own head, when the checkout names it otherwise.
            branch = requests[change_request_id].get("head_ref") or branch
            if branch != checkout_branch:
                requests.update(self._change_requests(branch, None))
        names = dict.fromkeys(name for name in (branch, default, *protected) if name)
        return Snapshot(cycle_branch=branch, default_branch=default, protected=protected,
                        refs={name: heads.get(name) for name in names},
                        change_requests=requests, unverified=tuple(unverified))

    def verify(self, before: Snapshot, *,
               change_request_id: str | None = None) -> AuditResult:
        """Compare the same audited state with `before`."""
        heads = _remote_heads(self._git)
        after_refs = {name: heads.get(name) for name in before.refs}
        breaches = []
        for name, old in before.refs.items():
            new = after_refs[name]
            if new == old:
                continue
            ref = f"refs/heads/{name}"
            if new is None:
                breaches.append(Breach(DELETED, ref, old, None))
                continue
            strict = name == before.default_branch or name in before.protected
            if old is None:
                if strict:
                    breaches.append(Breach(MOVED, ref, None, new))
                continue  # The cycle branch was created.
            # Any move of the default or a protected branch is a breach, even
            # a fast-forward; the cycle branch may only move forward.
            if strict or not self._is_ancestor(old, new, name):
                breaches.append(Breach(
                    MOVED if strict else NON_FAST_FORWARD, ref, old, new,
                    commits_added=self._commits(old, new),
                    commits_removed=self._commits(new, old)))
        after_requests = self._change_requests(before.cycle_branch, change_request_id)
        for number in before.change_requests:
            if number not in after_requests and self.host is not None:
                after_requests.update(self._change_requests(None, number))
        for number, state in sorted(after_requests.items()):
            was = (before.change_requests.get(number) or {}).get("state")
            now = state.get("state")
            if now != _OPEN and was in (None, _OPEN):
                breaches.append(Breach(MERGED if now == "MERGED" else CLOSED,
                                       f"change_request:{number}", was, now))
        status = VIOLATED if breaches else UNVERIFIED if before.unverified else PASSED
        return AuditResult(status=status, before=before, after_refs=after_refs,
                           after_change_requests=after_requests,
                           breaches=tuple(breaches), unverified=before.unverified)

    def _has(self, sha: str) -> bool:
        return self._status("cat-file", "-e", f"{sha}^{{commit}}") == 0

    def _is_ancestor(self, old: str, new: str, branch: str) -> bool:
        if not self._has(new):
            # Into FETCH_HEAD only: the audit leaves no ref behind.
            self._status("fetch", "--no-tags", "--quiet", AUDITED_REMOTE, f"refs/heads/{branch}")
        if not (self._has(old) and self._has(new)):
            raise AuditUnreadable(
                f"the move of refs/heads/{branch} cannot be compared: a commit is not available")
        code = self._status("merge-base", "--is-ancestor", old, new)
        if code not in (0, 1):
            raise AuditUnreadable(f"the move of refs/heads/{branch} cannot be compared")
        return code == 0

    def _commits(self, exclude: str, include: str) -> tuple[str, ...]:
        """The commits in `include` and not in `exclude`, when available."""
        if not (self._has(include) and self._has(exclude)):
            return ()
        return tuple(self._git("rev-list", f"--max-count={COMMIT_LIMIT}", include,
                               f"^{exclude}", check=False).split())
