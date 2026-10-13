"""The one deterministic wait on a change request's checks (#228).

Under the `parallel` CI policy a review starts while the code host's checks are
still running, so something has to wait for them before approval, and it must
not be a model. A model polling `gh pr checks --watch` spends turns on waiting,
two stages that each wait poll twice, and a stage that waits holds the next one
back. This module is that wait, once: `run_cycle.py` calls it after a clean
review, and both orchestrator skills run its command line instead of polling.

It answers one question: did every required check of one head SHA pass? It
never approves anything. Approval is the caller's, from a clean review *and*
this result on the same SHA. It never cancels, reruns or edits anything on the
code host either: it reads.

**Required means declared, or else everything reported.** The required checks
are the ones the base branch's classic protection and its rulesets declare.
When they declare none, every check GitHub reports on the SHA is required,
identified by name and source app. A declared check that has not reported yet
is pending until the deadline, and missing after it.

**Only `success` passes.** `failure`, `cancelled`, `timed_out` and every other
unsuccessful conclusion fail; `skipped` and `neutral` do not pass either, even
where GitHub's merge rules would accept them. Repeated runs of one check count
once, as their latest run, so an earlier failed attempt does not block a
successful retry.

**Never success by default.** No checks at all, an unreadable response, a moved
head and an expired deadline each have their own result, and none of them is
`CI_PASSED`. Only GitHub is read; another code host has no reader here.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote

# Every result the gate can return; telemetry stores them as `ci_status`, so
# the vocabulary is defined once, there. Only `CI_PASSED` can lead to
# approval. `CI_SKIPPED` is a required check that concluded `skipped` or
# `neutral`. `CI_PENDING` is a single read (`once`) that found checks still
# running: a wait never returns it, it waits and times out. `CI_HEAD_MOVED`
# means the change request's head is no longer the SHA asked about.
from telemetry import (  # noqa: F401  (re-exported for callers)
    CI_FAILED,
    CI_HEAD_MOVED,
    CI_PASSED,
    CI_PENDING,
    CI_SKIPPED,
    CI_STATUSES,
    CI_TIMEOUT,
    CI_UNREADABLE,
    DEFINITIVE_FAILURES,
    NO_CI_CHECKS_FOUND,
)

#: About fifteen minutes, which is what the stage skills wait when a
#: repository states no limit of its own.
DEFAULT_TIMEOUT = 900
MAX_TIMEOUT = 86_400
DEFAULT_POLL_INTERVAL = 30
#: How long a SHA may report no check at all before that is the answer. Checks
#: are created a few seconds after a push; a review that just finished started
#: minutes after it, so this is a margin, not an expected wait.
NO_CHECKS_GRACE = 120

#: Normalised states of one check.
SUCCESS, PENDING, FAILED, SKIPPED = "success", "pending", "failed", "skipped"

#: Concluded without passing and without failing. Not a pass here.
_NOT_RUN = frozenset({"skipped", "neutral"})

FULL_SHA = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+")
_NUMBER = re.compile(r"[1-9][0-9]{0,9}")


class CheckReadError(RuntimeError):
    """The code host could not be read, or answered in an unknown shape."""


@dataclass(frozen=True)
class RequiredCheck:
    """One check the base branch requires. `app_id` None accepts any source."""

    context: str
    app_id: int | None = None

    @property
    def label(self) -> str:
        return self.context if self.app_id is None else f"{self.context} (app {self.app_id})"


@dataclass(frozen=True)
class Check:
    """One reported check run or commit status, already normalised.

    `source` is the app's slug for a check run and `status` for a commit
    status; with the name it identifies the check. `order` ranks repeated runs
    of one check: the higher is the later run.
    """

    name: str
    source: str
    state: str
    order: int = 0
    app_id: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.name, self.source

    @property
    def label(self) -> str:
        return f"{self.name} ({self.source})"


@dataclass(frozen=True)
class PullState:
    """What the code host says about the change request right now."""

    state: str
    head_sha: str
    base_ref: str


class CheckProvider(Protocol):
    """How the gate reads a code host; injected so tests need no network."""

    def pull(self, repo: str, change_request_id: str) -> PullState:
        """The change request's state, head SHA and base branch."""

    def required(self, repo: str, base_ref: str) -> tuple[RequiredCheck, ...]:
        """The checks the base branch declares as required, possibly none."""

    def checks(self, repo: str, sha: str) -> tuple[Check, ...]:
        """Every check reported on `sha`, repeated runs included."""


@dataclass(frozen=True)
class GateResult:
    """What one wait, or one read, established about one SHA.

    The lists hold check labels. `late` names checks first reported after the
    first read: they were part of the evaluation, and say the set grew while
    it was waited on. `waited_seconds` is wall time the gate spent, during
    which no stage ran.
    """

    status: str
    head_sha: str
    required_source: str | None = None
    passed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    late: tuple[str, ...] = ()
    polls: int = 0
    waited_seconds: float = 0.0
    pr_head_sha: str | None = None
    reason: str | None = None

    @property
    def passed_gate(self) -> bool:
        return self.status == CI_PASSED

    def counts(self) -> dict[str, int]:
        return {"passed": len(self.passed), "failed": len(self.failed),
                "skipped": len(self.skipped), "pending": len(self.pending),
                "missing": len(self.missing), "late": len(self.late)}

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "head_sha": self.head_sha,
            "pr_head_sha": self.pr_head_sha,
            "required_source": self.required_source,
            "counts": self.counts(),
            "passed": list(self.passed),
            "failed": list(self.failed),
            "skipped": list(self.skipped),
            "pending": list(self.pending),
            "missing": list(self.missing),
            "late": list(self.late),
            "polls": self.polls,
            "waited_seconds": round(self.waited_seconds, 3),
            "reason": self.reason,
        }


@dataclass
class _Buckets:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    source: str = "reported"
    empty: bool = False

    def put(self, check: Check) -> None:
        {SUCCESS: self.passed, FAILED: self.failed, SKIPPED: self.skipped,
         PENDING: self.pending}[check.state].append(check.label)


def latest_runs(checks) -> list[Check]:
    """One entry per check, its latest run, in a stable order."""
    latest: dict[tuple[str, str], Check] = {}
    for check in checks:
        current = latest.get(check.key)
        if current is None or check.order > current.order:
            latest[check.key] = check
    return sorted(latest.values(), key=lambda check: check.key)


def evaluate(checks, required: tuple[RequiredCheck, ...]) -> _Buckets:
    """Sort the checks of one read into the buckets the barrier reads.

    With declared requirements, each one is matched by name and, when it names
    one, by app; a commit status has no app, so it satisfies only a requirement
    that accepts any source. Every match must pass. Without declarations,
    every reported check is required.
    """
    runs = latest_runs(checks)
    buckets = _Buckets()
    if required:
        buckets.source = "declared"
        for requirement in required:
            matches = [check for check in runs if check.name == requirement.context
                       and (requirement.app_id is None or check.app_id == requirement.app_id)]
            if not matches:
                buckets.missing.append(requirement.label)
            for check in matches:
                buckets.put(check)
    else:
        buckets.empty = not runs
        for check in runs:
            buckets.put(check)
    return buckets


def _result(status: str, sha: str, buckets: _Buckets | None = None, **fields) -> GateResult:
    if buckets is not None:
        fields = {"required_source": buckets.source, "passed": tuple(buckets.passed),
                  "failed": tuple(buckets.failed), "skipped": tuple(buckets.skipped),
                  "pending": tuple(buckets.pending), "missing": tuple(buckets.missing),
                  **fields}
    return GateResult(status=status, head_sha=sha, **fields)


def wait_for_checks(provider: CheckProvider, repo: str, change_request_id: str, sha: str, *,
                    timeout: float = DEFAULT_TIMEOUT,
                    poll_interval: float = DEFAULT_POLL_INTERVAL,
                    no_checks_grace: float = NO_CHECKS_GRACE,
                    once: bool = False,
                    clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep,
                    on_poll: Callable[[GateResult], None] | None = None) -> GateResult:
    """Wait until every required check of `sha` concluded, or the deadline passed.

    Returns as soon as approval is decided either way: every required check
    passed, or one failed or did not run. Each read first confirms that `sha`
    is still the change request's head, so a moved head ends the wait at once
    rather than after the deadline. A pass is confirmed by one more read of the
    head, which is the final barrier. With `once`, the checks are read a single
    time and nothing is waited for: still-pending checks are `CI_PENDING`.

    `on_poll` receives the state after each read; `clock` and `sleep` are
    injected so a test controls time.
    """
    if not isinstance(sha, str) or FULL_SHA.fullmatch(sha) is None:
        raise ValueError("the checks are read for a full 40-character head SHA")
    if timeout <= 0 or poll_interval <= 0:
        raise ValueError("timeout and poll_interval must be positive")
    started = clock()
    deadline = started + timeout
    polls = 0
    first_seen: set[tuple[str, str]] | None = None

    def elapsed() -> float:
        return max(0.0, clock() - started)

    def unreadable(reason: str, **fields) -> GateResult:
        return _result(CI_UNREADABLE, sha, polls=polls, waited_seconds=elapsed(),
                       reason=reason, **fields)

    def read_pull() -> PullState | GateResult:
        try:
            pull = provider.pull(repo, change_request_id)
        except CheckReadError as error:
            return unreadable(f"the change request could not be read: {error}")
        if pull.state != "OPEN":
            return unreadable(f"the change request is {pull.state.lower()}, not open",
                              pr_head_sha=pull.head_sha)
        if pull.head_sha != sha:
            return _result(CI_HEAD_MOVED, sha, polls=polls, waited_seconds=elapsed(),
                           pr_head_sha=pull.head_sha,
                           reason="the change request's head is no longer this SHA")
        return pull

    pull = read_pull()
    if isinstance(pull, GateResult):
        return pull
    try:
        required = tuple(dict.fromkeys(provider.required(repo, pull.base_ref)))
    except CheckReadError as error:
        return unreadable(f"the required checks of {pull.base_ref} could not be read: {error}",
                          pr_head_sha=pull.head_sha)

    while True:
        if polls:
            pull = read_pull()
            if isinstance(pull, GateResult):
                return pull
        try:
            checks = tuple(provider.checks(repo, sha))
        except CheckReadError as error:
            return unreadable(f"the checks could not be read: {error}", pr_head_sha=sha)
        polls += 1
        keys = {check.key for check in latest_runs(checks)}
        if first_seen is None:
            first_seen = keys
        late = tuple(f"{name} ({source})" for name, source in sorted(keys - first_seen))
        buckets = evaluate(checks, required)
        now = clock()
        common = {"polls": polls, "waited_seconds": elapsed(), "pr_head_sha": sha, "late": late}
        status = None
        if buckets.failed:
            status = CI_FAILED
        elif buckets.skipped:
            status = CI_SKIPPED
        elif buckets.empty:
            # One read cannot tell "no CI" from "not created yet".
            if not once and now - started >= min(no_checks_grace, timeout):
                status = NO_CI_CHECKS_FOUND
        elif not buckets.pending and not buckets.missing:
            confirmed = read_pull()
            if isinstance(confirmed, GateResult):
                return confirmed
            status = CI_PASSED
        if status is None and (once or now >= deadline):
            status = CI_PENDING if once else CI_TIMEOUT
        result = _result(status or CI_PENDING, sha, buckets, **common)
        if on_poll is not None:
            on_poll(result)
        if status is not None:
            return result
        sleep(max(0.0, min(poll_interval, deadline - now)))


class GitHubChecks:
    """`CheckProvider` for GitHub, through `gh api`.

    Required checks come from `GET /repos/{repo}/branches/{base}` (classic
    protection, ignored when its enforcement is `off`) and
    `GET /repos/{repo}/rules/branches/{base}` (active rulesets). Reported checks
    are the check runs and the combined commit status of the SHA.
    """

    def __init__(self, cwd: str | None = None, *, run=subprocess.run,
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
            raise CheckReadError("GitHub tooling unavailable or timed out") from error
        if done.returncode:
            raise CheckReadError(f"gh api exited {done.returncode}")
        try:
            return json.loads(done.stdout)
        except ValueError as error:
            raise CheckReadError("GitHub returned invalid JSON") from error

    @staticmethod
    def _pages(data, key: str | None = None) -> list:
        if not isinstance(data, list) or any(not isinstance(page, (list, dict)) for page in data):
            raise CheckReadError("GitHub returned pages in an unknown shape")
        items = []
        for page in data:
            entries = page.get(key) if key and isinstance(page, dict) else page
            if not isinstance(entries, list):
                raise CheckReadError("GitHub returned a page in an unknown shape")
            items.extend(entries)
        return items

    def pull(self, repo: str, change_request_id: str) -> PullState:
        data = self._api(f"repos/{repo}/pulls/{change_request_id}")
        try:
            head, base = data["head"]["sha"], data["base"]["ref"]
            state = "MERGED" if data.get("merged") else str(data["state"]).upper()
        except (KeyError, TypeError) as error:
            raise CheckReadError("the pull request came back in an unknown shape") from error
        if not isinstance(head, str) or not isinstance(base, str) or not base:
            raise CheckReadError("the pull request names no head SHA or base branch")
        return PullState(state=state, head_sha=head, base_ref=base)

    def required(self, repo: str, base_ref: str) -> tuple[RequiredCheck, ...]:
        branch = quote(base_ref, safe="")
        found: list[RequiredCheck] = []
        data = self._api(f"repos/{repo}/branches/{branch}")
        protection = data.get("protection") if isinstance(data, dict) else None
        classic = protection.get("required_status_checks") if isinstance(protection, dict) else None
        if isinstance(classic, dict) and classic.get("enforcement_level") != "off":
            checks = classic.get("checks")
            if isinstance(checks, list) and checks:
                found.extend(self._required(item, "app_id") for item in checks)
            else:
                contexts = classic.get("contexts") or []
                if not isinstance(contexts, list):
                    raise CheckReadError("the branch protection came back in an unknown shape")
                found.extend(self._required({"context": context}, "app_id")
                             for context in contexts)
        for rule in self._pages(self._api(f"repos/{repo}/rules/branches/{branch}",
                                          paginate=True)):
            if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
                continue
            parameters = rule.get("parameters")
            entries = (parameters.get("required_status_checks")
                       if isinstance(parameters, dict) else None)
            if not isinstance(entries, list):
                raise CheckReadError("a ruleset came back in an unknown shape")
            found.extend(self._required(item, "integration_id") for item in entries)
        return tuple(dict.fromkeys(found))

    @staticmethod
    def _required(item, app_key: str) -> RequiredCheck:
        context = item.get("context") if isinstance(item, dict) else None
        if not isinstance(context, str) or not context:
            raise CheckReadError("a required check names no context")
        app_id = item.get(app_key)
        # -1 and an absent app both accept any source.
        if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id < 0:
            app_id = None
        return RequiredCheck(context, app_id)

    def checks(self, repo: str, sha: str) -> tuple[Check, ...]:
        found: list[Check] = []
        for run in self._pages(self._api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
                                         paginate=True), "check_runs"):
            if not isinstance(run, dict) or not isinstance(run.get("name"), str):
                raise CheckReadError("a check run came back in an unknown shape")
            app = run.get("app") if isinstance(run.get("app"), dict) else {}
            app_id = app.get("id") if isinstance(app.get("id"), int) else None
            source = app.get("slug") or (str(app_id) if app_id is not None else "unknown")
            status, conclusion = run.get("status"), run.get("conclusion")
            if status != "completed":
                # A status GitHub adds later has not concluded either.
                state = PENDING
            elif conclusion == "success":
                state = SUCCESS
            elif conclusion in _NOT_RUN:
                state = SKIPPED
            else:
                state = FAILED
            order = run.get("id") if isinstance(run.get("id"), int) else 0
            found.append(Check(run["name"], str(source), state, order, app_id))
        for status in self._pages(self._api(f"repos/{repo}/commits/{sha}/status?per_page=100",
                                            paginate=True), "statuses"):
            if not isinstance(status, dict) or not isinstance(status.get("context"), str):
                raise CheckReadError("a commit status came back in an unknown shape")
            state = {"success": SUCCESS, "pending": PENDING}.get(status.get("state"), FAILED)
            order = status.get("id") if isinstance(status.get("id"), int) else 0
            found.append(Check(status["context"], "status", state, order))
        return tuple(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ci_gate",
        description=("Wait once for the required checks of one change-request head "
                     "and print the result as JSON. Exit 0 only on CI_PASSED."))
    parser.add_argument("--repo", required=True, help="owner/name on GitHub")
    parser.add_argument("--pr", required=True, help="pull request number")
    parser.add_argument("--sha", required=True, help="the reviewed head, a full SHA")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"seconds to wait at most (default {DEFAULT_TIMEOUT})")
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL,
                        help=f"seconds between reads (default {DEFAULT_POLL_INTERVAL})")
    parser.add_argument("--once", action="store_true",
                        help="read the checks once and do not wait")
    args = parser.parse_args(argv)
    if _REPOSITORY.fullmatch(args.repo) is None:
        parser.error("--repo must be owner/name")
    if _NUMBER.fullmatch(args.pr.lstrip("#")) is None:
        parser.error("--pr must be a pull request number")
    if FULL_SHA.fullmatch(args.sha) is None:
        parser.error("--sha must be a full 40-character lowercase SHA")
    if not 1 <= args.timeout <= MAX_TIMEOUT:
        parser.error(f"--timeout must be between 1 and {MAX_TIMEOUT} seconds")
    if args.poll_interval < 1:
        parser.error("--poll-interval must be at least 1 second")
    result = wait_for_checks(GitHubChecks(), args.repo, args.pr.lstrip("#"), args.sha,
                             timeout=args.timeout, poll_interval=args.poll_interval,
                             once=args.once)
    print(json.dumps(result.to_json(), indent=2))
    return 0 if result.passed_gate else 1


if __name__ == "__main__":
    sys.exit(main())
