"""Turn a resolved `Target` into a real execution.

`router.route()` answers *where* work should go. This module answers *whether it
can actually go there*, and sends it. It adds no routing rules and no learning:
a decision arrives already made.

The uncomfortable fact this design is built around is that the executors differ
in what they can prove about themselves:

- Orca reports its runtime state, so `READY` is provable.
- Codex and Claude expose a version and stored credential material. That proves
  `INSTALLED` and at best `AUTHENTICATED`, which here means a credential is
  configured — not that the session behind it is still valid, since a token can
  be expired or revoked without any local sign of it. Nothing documented reports
  remaining quota or session validity without consuming something.

Pretending otherwise is what cost a calibration campaign: a binary on PATH was
read as readiness, and one exhausted window was read as evidence about a model.
So a probe reports what it can demonstrate and says how it demonstrated it, and
a caller that wants to dispatch from merely `AUTHENTICATED` has to ask for that
explicitly through `ReadinessPolicy.ATTEMPT`. The substitution is then recorded
in the result, where nobody can mistake an attempt for proof.

Interactive friction is treated the same way. A trust dialog, a hook-review
screen or a login prompt makes an execution unattainable without a human, and
the adapter returns `BLOCKED` naming the missing capability. It never answers a
security prompt blind.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import threading
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

import stage_permissions as sp
try:
    from gh_environment import gh_environment
except ModuleNotFoundError:
    from .gh_environment import gh_environment
import dependency_setup
import native_prerequisites as native
from router import Availability, RoutingDecision, RoutingMode, Target
from stage_permissions import StagePolicy
from usage import normalize_executor_output

#: The sensitive grants (`grants.py`) Codex knows how to apply, and how.
CODEX_GRANT_OPTIONS = {"docker_host": "unix_sockets"}
#: Claude applies `docker_host` through the sandbox network settings and command table.
CLAUDE_GRANT_OPTIONS = {"docker_host": "docker_compose"}

SCHEMA_VERSION = 1
DEFAULT_DISPATCH_TIMEOUT_SECONDS = 6 * 60 * 60
# How long a stopped dispatch's process group may run its own teardown after
# SIGTERM before SIGKILL, and how long its output pipes may then stay open.
PROCESS_GROUP_GRACE_SECONDS = 10
OUTPUT_DRAIN_SECONDS = 5


class ReadinessPolicy(str, Enum):
    """How much proof a caller demands before dispatching.

    `PROVEN` only dispatches to an executor that demonstrated `READY`. `ATTEMPT`
    also accepts `AUTHENTICATED`, accepting that readiness will be discovered by
    trying — which is how a quota limit is actually found.
    """

    PROVEN = "proven"
    ATTEMPT = "attempt"

    @staticmethod
    def for_mode(mode: RoutingMode) -> "ReadinessPolicy":
        """The policy each mode needs, so neither depends on being remembered.

        Production needs `ATTEMPT`: no native executor can demonstrate
        readiness, so `PROVEN` would refuse to dispatch to Codex or Claude at
        all. A calibration needs `PROVEN`: an arm that started from an
        unestablished state would sit in the sample beside arms that did not.
        """
        return (ReadinessPolicy.PROVEN if mode is RoutingMode.CALIBRATION
                else ReadinessPolicy.ATTEMPT)


class DispatchOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    #: The run completed, but not as the target that was asked for. The cycle
    #: must not treat it as success: `dispatch(target)` promises *that* target
    #: ran, and a different model answering is the promise being broken, not a
    #: detail to note in passing.
    CONTRACT_VIOLATION = "contract_violation"
    #: The process running the cycle was told to stop while the executor ran:
    #: a host that ends its task at a time limit, a closed terminal, Ctrl-C. The
    #: stage neither finished nor failed on its own, and its result is unknown.
    INTERRUPTED = "interrupted"


class ExecutorError(ValueError):
    """The dispatch layer was asked for something it must not do."""

class WorkspacePolicy(str, Enum):
    """The isolation contract a dispatch must satisfy."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    DISPOSABLE = "disposable"


def _canonical_path(path: str) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def _paths_overlap(first: str, second: str) -> bool:
    first_path = Path(_canonical_path(first))
    second_path = Path(_canonical_path(second))
    return (first_path == second_path
            or first_path in second_path.parents
            or second_path in first_path.parents)


@contextmanager
def _native_review_scratch(adapter, argv: list[str], environment: dict | None,
                           *, writes: bool, publishes: bool, cwd: str,
                           command_policy: StagePolicy | None = None):
    """Give every native dispatch isolated scratch space outside its checkout."""
    try:
        code, root = _git_read(cwd, "rev-parse", "--show-toplevel")
        repository = Path(root).resolve() if code == 0 else None
    except (OSError, subprocess.SubprocessError):
        repository = Path(cwd).resolve()
    # TMPDIR may itself point inside the checkout. Do not create even a scratch
    # directory there: read-only verification includes untracked files.
    candidates = (tempfile.gettempdir(), os.environ.get("TEMP"),
                  os.environ.get("TMP"), "/tmp", "/var/tmp")
    # A parent such as /tmp may contain the checkout; only the newly created
    # child must be disjoint from it.
    parent = next((path for path in candidates if path and Path(path).is_dir()
                   and os.access(path, os.W_OK)
                   and (repository is None or (repository != Path(path).resolve()
                        and repository not in Path(path).resolve().parents))), None)
    if parent is None:
        raise ExecutorError("no temporary directory outside the dispatch checkout")
    with tempfile.TemporaryDirectory(prefix="code-cycle-tmp-", dir=parent) as scratch:
        if repository is not None and _paths_overlap(scratch, str(repository)):
            raise ExecutorError("dispatch temporary directory overlaps the checkout")
        worker_env = gh_environment(environment)
        worker_env["TMPDIR"] = scratch
        worker_argv = list(argv)
        if adapter.name == "codex":
            profile = next((option.split("=", 1)[1].strip('"')
                            for option in worker_argv
                            if option.startswith("default_permissions=")), None)
            options = []
            if not writes and profile is None:
                # A named profile replaces the legacy read-only selector, while
                # inheriting its filesystem and network restrictions.
                index = worker_argv.index("-s")
                del worker_argv[index:index + 2]
                profile = "code_cycle_review_read"
                options += ["-c", f'default_permissions="{profile}"',
                            "-c", f'permissions.{profile}.extends=":read-only"']
            if profile is None:
                # The legacy workspace-write selector accepts explicit roots.
                options += ["--add-dir", scratch]
            else:
                rule = f"permissions.{profile}.filesystem="
                entry = f'{json.dumps(scratch)}="write"'
                existing = next((i for i, option in enumerate(worker_argv)
                                 if option.startswith(rule)), None)
                if existing is None:
                    options += ["-c", f"{rule}{{{entry}}}"]
                else:
                    # Keep the linked-worktree Git protections in the same
                    # table; another filesystem override would replace them.
                    table = worker_argv[existing][len(rule):]
                    separator = "," if table[1:-1].strip() else ""
                    worker_argv[existing] = f"{rule}{table[:-1]}{separator}{entry}}}"
            options += [
                "-c", f'shell_environment_policy.set.GH_TELEMETRY={json.dumps(worker_env["GH_TELEMETRY"])}',
                "-c", f'shell_environment_policy.set.GH_NO_UPDATE_NOTIFIER={json.dumps(worker_env["GH_NO_UPDATE_NOTIFIER"])}',
                "-c", f'shell_environment_policy.set.TMPDIR={json.dumps(scratch)}',
            ]
            worker_argv[-1:-1] = options
        with adapter.policy_files(worker_argv, command_policy, scratch=scratch,
                                  cwd=cwd) as policed_argv:
            yield policed_argv, worker_env


@dataclass(frozen=True)
class DisposableWorkspace:
    """An isolated workspace where verification/runtime artifacts may exist."""

    path: str
    source_path: str

    def __post_init__(self) -> None:
        if not self.path:
            raise ExecutorError("a disposable workspace needs a path")
        if not self.source_path:
            raise ExecutorError("a disposable workspace needs the source path")
        if _paths_overlap(self.path, self.source_path):
            raise ExecutorError(
                "a disposable workspace must not contain or be contained by "
                "the source workspace")


@dataclass(frozen=True)
class OrcaReviewWorkspace:
    """The non-mutating workspace a direct Orca review is allowed to use.

    Orca workers are asynchronous and the CLI does not offer a trustworthy
    read-only permission flag. A review therefore needs a workspace that is
    explicitly separate from the implementer's tree. The caller prepares that
    workspace and declares whether it is immutable or disposable; this
    contract verifies the separation before a worker can be launched.
    """

    path: str
    implementer_path: str
    isolation: str

    def __post_init__(self) -> None:
        if not self.path:
            raise ExecutorError("an Orca review workspace needs a path")
        if not self.implementer_path:
            raise ExecutorError("an Orca review workspace needs the implementer path")
        if self.isolation not in {"immutable", "disposable"}:
            raise ExecutorError(
                "an Orca review workspace must be immutable or disposable")
        if _paths_overlap(self.path, self.implementer_path):
            raise ExecutorError(
                "an Orca review workspace must not contain or be contained by "
                "the implementer workspace")


@dataclass(frozen=True)
class ProbeResult:
    """What an executor could be shown to be, and on what evidence."""

    executor: str
    availability: Availability
    proof: str
    detail: str = ""
    provable_ceiling: Availability = Availability.READY
    version: tuple[int, ...] | None = None

    @property
    def honest_ceiling_reached(self) -> bool:
        """True when nothing further could have been demonstrated."""
        return self.availability is self.provable_ceiling


#: Capabilities no retry can supply, because a person has to supply them. An
#: exhausted window is not one of these: it clears on its own, and going around
#: it is the whole reason a fallback exists.
#:
#: Every capability a native adapter's `interactive_markers` can name belongs
#: here — an interactive screen is by definition waiting for somebody. A test
#: asserts that, so a marker added to an adapter cannot quietly become a
#: condition the orchestration layer thinks it may route around.
HUMAN_ACTION_CAPABILITIES = frozenset({
    "authenticated_session",
    "bypass_acknowledgement",
    "folder_trust",
    "hook_trust",
    "trusted_directory",
})

# Closed mapping: availability is not the vocabulary of attempt failures.
CAPABILITY_ERROR_CODES = {
    "operating_quota": "quota", "operating_availability": "unavailable",
    **{key: "unavailable" for key in HUMAN_ACTION_CAPABILITIES},
    **{key: "precondition" for key in (
        "orchestration_context", "review_workspace_isolation", "review_workspace_mismatch",
        "review_workspace_conflict", "provider_agent_mapping", "publication_access",
        "proven_readiness", "read_only_enforcement", "disposable_workspace", "workspace_policy",
    )},
    "read_only_verification": "contract_violation",
}


def failure_excerpt(text: str, limit: int = 400) -> str:
    """Transient diagnostic only; mask before bounding, including terminal escapes."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(text))
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"(?<![A-Za-z0-9_])(?:sk-[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|xox[baprs]-[A-Za-z0-9-]+|AKIA[A-Z0-9]{16})(?![A-Za-z0-9_])",
                  "[redacted]", str(text))
    text = re.sub(r"(?i)((?:bearer|basic)\s+)[A-Za-z0-9._~+/-]+=*", r"\1[redacted]", text)
    # A name starts only where its run starts, so a long `-`/`_` run is scanned once.
    text = re.sub(
        r"(?i)([\"']?(?<![A-Za-z0-9_-])(?:[A-Za-z0-9_-]*[_-])?(?:key|token|secret|password)\b[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", r"\1[redacted]", text,
    )
    return " ".join(text.split())[:limit]


def _events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            # Session logs wrap event_msg and response_item in payload.
            payload = event.get("payload")
            if isinstance(payload, dict):
                events.append({"type": event.get("type"), **payload})
            else:
                events.append(event)
    return events


def _with_allowed_tools(argv: list[str], patterns: list[str]) -> list[str]:
    """`argv` with its `--allowedTools` patterns replaced by `patterns`.

    The table is first rendered before the dispatch knows its TMPDIR, whether
    RTK is installed, or whether the workspace has an `origin`; the hook's
    policy knows, and the first barrier must agree with it.
    """
    if "--allowedTools" not in argv:
        return argv
    start = argv.index("--allowedTools") + 1
    end = start
    while end < len(argv) and argv[end].startswith("Bash("):
        end += 1
    return argv[:start] + patterns + argv[end:]


def _tool_result_text(content) -> str:
    """The text of a `tool_result` block, whether a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def _start_evidence(events: list[dict]) -> str:
    negative = False
    for event in events:
        kind = event.get("type")
        kind = kind if isinstance(kind, str) else ""
        item = event.get("item") or {}
        if (kind in {"thread.started", "session_meta", "tool_use", "function_call",
                     "custom_tool_call", "exec_command_begin", "mcp_tool_call_begin"}
                or (kind == "system" and event.get("subtype") == "init")
                or (kind in {"item.started", "item.completed"} and isinstance(item, dict)
                    and item.get("type") in {"command_execution", "tool_call", "mcp_tool_call",
                                             "collab_tool_call", "file_change", "agent_message"})
                or event.get("start_state") == "started"):
            return "started"
        if event.get("session_id"):
            return "started"
        message = event.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), list) and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in message["content"]
        ):
            return "started"
        negative |= event.get("start_state") == "not_started" or event.get("started") is False
    return "not_started" if negative else "unknown"


def _error_signal(events: list[dict]) -> tuple[str | None, str] | None:
    """Only executor errors, never assistant prose; terminal errors take priority."""
    signals = []
    for event in events:
        kind = event.get("type", "")
        kind = kind if isinstance(kind, str) else ""
        if not (event.get("is_error") is True or event.get("error")
                or event.get("codex_error_info")
                or kind in {"turn.failed", "failure"} or "error" in kind.lower()
                or (kind == "result" and str(event.get("subtype", "")).startswith("error"))):
            if kind in {"turn.completed", "task_complete", "result"}:
                # An executor can recover from an intermediate stream error.
                signals.clear()
            continue
        error = event.get("error")
        error = error if isinstance(error, dict) else {"message": error}
        code = event.get("codex_error_info") or error.get("codex_error_info") or error.get("code")
        if isinstance(code, dict):
            code = next(iter(code), None)
        message = (error.get("message") or event.get("message") or event.get("result")
                   or event.get("errors") or event.get("subtype") or "")
        signals.append((str(code) if code is not None else None, str(message)))
    return signals[-1] if signals else None


def _error_class(code: str | None, message: str) -> str:
    # An unmapped structured code stays executor_error, irrespective of prose.
    code = str(code) if code is not None else None
    mapping = {"server_overloaded": "capacity", "usage_limit_exceeded": "quota",
               "unauthorized": "auth", "authentication_error": "auth",
               "stream_disconnected": "transport", "connection_failed": "transport",
               "unsupported_effort": "unsupported_effort"}
    if code in mapping:
        return mapping[code]
    if code not in {None, "other"}:
        return "executor_error"
    lowered = message.lower()
    # The established markers win: an auth, transport, capacity or quota error
    # that merely mentions the effort setting is not an effort rejection.
    for token, markers in (
        ("auth", ("401", "unauthorized", "invalid api key", "missing credentials")),
        ("transport", ("connection refused", "connection reset", "stream disconnect", "network error")),
        ("capacity", ("at capacity", "server overloaded")),
        ("quota", (
            "usage limit", "quota exceeded", "hit your limit", "rate limit",
            "you've hit your session limit", "you've hit your weekly limit",
            "you've hit your opus limit", "you've hit your sonnet limit",
        )),
    ):
        if any(marker in lowered for marker in markers):
            return token
    # The effort keyword has to sit right next to the rejection phrase.
    effort = r"(?:reasoning\s+)?effort(?:\s+(?:level|value))?"
    rejected = r"(?:is\s+)?(?:unsupported|not\s+supported|invalid)"
    if (re.search(rf"\bunsupported\s+{effort}\b", lowered)
            or re.search(rf"\b{effort}(?:\s*[:=]?\s*['\"`]?\w+['\"`]?)?\s+{rejected}\b", lowered)
            or re.search(rf"--effort(?:[=\s]+\w+)?\s*[:,]?\s+(?:{rejected}|unknown)\b", lowered)
            or re.search(rf"\binvalid\s+(?:choice|value)\s+for\s+(?:--effort\b|{effort}\b)", lowered)):
        return "unsupported_effort"
    return "executor_error"


@dataclass(frozen=True)
class DispatchResult:
    outcome: DispatchOutcome
    executor: str
    requested: Target
    model_resolved: str | None = None
    missing_capability: str | None = None
    detail: str = ""
    readiness_policy: ReadinessPolicy = ReadinessPolicy.PROVEN
    dispatched_from: Availability = Availability.READY
    artifacts: dict = field(default_factory=dict)
    #: What the agent actually said, lifted out of its CLI's envelope. Empty
    #: when the executor produced no message of its own. Transient: it is read
    #: by whoever drives the stage and never stored.
    agent_output: str = ""
    #: True when the call succeeded in *starting* work that finishes elsewhere.
    #: Orca's receipt says a worker launched, not that the stage is done, and a
    #: caller that reads `SUCCEEDED` as "finished" would review work still being
    #: written. Native executors run to completion, so they leave this False.
    asynchronous: bool = False
    #: Wall time of the executor call, from a monotonic clock. None when the
    #: attempt was refused before an executor ran, and when the call only
    #: started work that finishes elsewhere: timing the launch would report a
    #: stage as fast because nobody watched it finish.
    duration_ms: int | None = None
    #: Typed provider-reported categories. Raw stdout is never carried here.
    usage_observations: tuple[dict, ...] = ()
    #: Reported cost evidence; CLI amounts remain unpriced and use `unknown`.
    cost_measures: tuple[dict, ...] = ()
    #: The physical call's lifecycle is independent of usage availability.
    lifecycle_state: str | None = None
    error_code: str | None = None
    start_state: str = "unknown"
    #: Exit evidence, independent of start evidence. False includes unknown.
    process_exited: bool = False
    #: Untrusted executor code and diagnostic: report only, never persisted.
    raw_error_code: str | None = None

    @property
    def failure_code(self) -> str | None:
        if self.outcome is DispatchOutcome.CONTRACT_VIOLATION:
            return "contract_violation"
        if self.outcome is DispatchOutcome.INTERRUPTED:
            return "interrupted"
        if self.lifecycle_state == "timed_out":
            return "timeout"
        if self.outcome is DispatchOutcome.BLOCKED:
            try:
                return CAPABILITY_ERROR_CODES[self.missing_capability]
            except KeyError as exc:
                raise ExecutorError(f"unmapped missing_capability: {self.missing_capability}") from exc
        if self.error_code is not None:
            return self.error_code
        return None if self.outcome is DispatchOutcome.SUCCEEDED else "no_error_report"

    def failure_summary(self) -> str:
        if self.failure_code is None:
            return ""
        parts = [f"error_code={self.failure_code}", f"start_state={self.start_state}"]
        if self.failure_code == "executor_error" and self.raw_error_code:
            parts.append(f"{self.executor}_error_info={failure_excerpt(self.raw_error_code, 80)}")
        if self.detail:
            parts.append(failure_excerpt(self.detail))
        return " · ".join(parts)

    @property
    def learned_availability(self) -> Availability | None:
        """What this attempt demonstrated about the executor, if anything.

        A native probe can never see quota before spending some, so the only
        moment anyone learns a window is exhausted is a dispatch that tried. If
        that discovery stays inside the result, the production fallback is
        unreachable in exactly the case it exists for: the router picked the
        primary from an optimistic `attempt` promotion, the dispatch found the
        truth, and nothing carried it back.

        This reports the evidence. It does not re-route: that decision belongs
        to the orchestration layer, where it can be recorded once instead of
        happening silently inside a call that was asked to dispatch.
        """
        if self.missing_capability == "operating_quota":
            return Availability.QUOTA_EXHAUSTED
        if self.missing_capability == "authenticated_session":
            return Availability.INSTALLED
        return None

    @property
    def needs_human_action(self) -> bool:
        """Whether a person, not another executor, is what this attempt needs.

        Learning something about an executor and being free to go around it are
        two different questions, and using the first as a proxy for the second
        is how a login prompt turns into a silent provider switch. A sign-in
        screen and a trust dialog are both waiting for somebody; running the
        work elsewhere answers neither, it just hides the question and spends
        the other provider's window on it.
        """
        return self.missing_capability in HUMAN_ACTION_CAPABILITIES

    @property
    def model_matches_request(self) -> bool | None:
        """Whether what ran is what was asked for. None when unreported.

        `None` is a third answer, not a soft no: an executor that does not say
        which model it ran leaves the question open, and recording that as a
        mismatch would invent a drift nobody observed.
        """
        if self.model_resolved is None:
            return None
        return self.model_resolved == self.requested.model

    def explain(self) -> str:
        if self.outcome is DispatchOutcome.BLOCKED:
            return f"{self.executor}: BLOCKED, missing {self.missing_capability}: {self.detail}"
        suffix = ""
        if self.dispatched_from is not Availability.READY:
            suffix = (
                f" (dispatched from {self.dispatched_from.value} under"
                f" {self.readiness_policy.value} policy, not proven ready)"
            )
        return f"{self.executor}: {self.outcome.value} as {self.model_resolved or '?'}{suffix}"


def _run(argv: list[str], timeout: int = 30, cwd: str | None = None,
         on_output=None, env: dict | None = None) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=cwd, stdin=subprocess.DEVNULL, start_new_session=os.name == "posix",
        env=env,
    )
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def drain(stream, lines: list[str], publish: bool = False) -> None:
        for line in iter(stream.readline, ""):
            lines.append(line)
            if publish and on_output is not None:
                try:
                    on_output(line)
                except Exception:
                    # Display callbacks must not interrupt the running agent.
                    pass
        stream.close()

    stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout_lines, True),
                                     daemon=True)
    stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr_lines),
                                     daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def join_output() -> None:
        # A descendant that left the process group can hold a pipe open; the
        # cycle must not wait on it forever.
        stdout_thread.join(OUTPUT_DRAIN_SECONDS)
        stderr_thread.join(OUTPUT_DRAIN_SECONDS)

    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        exited = _reap_process_group(process)
        join_output()
        error = subprocess.TimeoutExpired(
            argv, timeout, output="".join(stdout_lines), stderr="".join(stderr_lines),
        )
        error.process_exited = exited
        raise error from exc
    except BaseException:
        # An interrupted cycle must not leave an agent or its children editing
        # a checkout that nothing supervises any more.
        _reap_process_group(process)
        raise
    exited = _reap_process_group(process)
    join_output()
    completed = subprocess.CompletedProcess(argv, returncode,
                                            "".join(stdout_lines), "".join(stderr_lines))
    completed.process_exited = exited
    return completed


def _run_sandbox_probe(argv: list[str], *, cwd: str | None = None, timeout: int = 30,
                       env: dict | None = None, **_options) -> subprocess.CompletedProcess:
    """Run a sandbox probe in its own process group, like a dispatch.

    A timeout or interruption gets the same staged termination, so the
    sandbox can tear down before it is killed.
    """
    return _run(argv, timeout=timeout, cwd=cwd, env=env)


def _docker_host_probe(adapter, *, cwd: str, writes: bool, publishes: bool,
                       network_domains: tuple[str, ...] = (), path: str | None = None,
                       runner=None) -> dict:
    """Check Docker from the granted executor sandbox before a stage relies on it."""
    argv: list[str]
    probe_directory = None
    temporary_settings = None
    probe_scratch = None
    if adapter.name == "codex":
        if native.nested_codex():
            return {"status": "not_verified", "command": "codex sandbox -- docker version",
                    "executor": adapter.name,
                    "error": "known nested Codex limitation (#268); no sandbox probe was launched"}
        if publishes:
            profile = "code_cycle_publish_write" if writes else "code_cycle_publish_read"
        elif network_domains:
            profile = "code_cycle_registry_write" if writes else "code_cycle_registry_read"
        else:
            profile = "code_cycle_write" if writes else "code_cycle_docker_read"
        parent = ":workspace" if writes else ":read-only"
        argv = [adapter.binary, "sandbox", "--permission-profile", profile,
                "-c", f'permissions.{profile}.extends="{parent}"',
                "-c", f"permissions.{profile}.network.enabled=true",
                "-c", f"permissions.{profile}.network.dangerously_allow_all_unix_sockets=true"]
        domains_to_allow = tuple(dict.fromkeys(
            (*(sp.REVIEW_SANDBOX_DOMAINS if publishes else ()), *network_domains)))
        if domains_to_allow:
            domains = ",".join(f'{json.dumps(host)}="allow"'
                                for host in domains_to_allow)
            argv += ["-c", f"permissions.{profile}.network.domains={{{domains}}}"]
        argv += ["--", "docker", "version"]
    elif adapter.name == "claude":
        executable = shutil.which("srt", path=path)
        if executable is None:
            return {"status": "not_verified", "command": "srt docker version",
                    "error": "srt is unavailable; Docker reachability was not checked"}
        try:
            probe_directory = Path(tempfile.mkdtemp(
                prefix="code-cycle-docker-probe-", dir=tempfile.gettempdir()))
            probe_scratch = probe_directory / "scratch"
            temporary_settings = probe_directory / "settings.json"
            probe_scratch.mkdir()
            writable = [cwd, str(probe_scratch)]
            if writes and Path(cwd).is_dir():
                git_directories = []
                for option in ("--git-common-dir", "--git-dir"):
                    status, path_value = _git_read(cwd, "rev-parse", option)
                    if status == 0:
                        git_directories.append(str((Path(cwd) / path_value).resolve()))
                writable.extend(CodexAdapter.git_write_roots(tuple(dict.fromkeys(git_directories))))
            sandbox = sp.review_sandbox_settings(
                tuple(writable), network_domains, docker_host=True)["sandbox"]
            network = dict(sandbox["network"])
            network.setdefault("deniedDomains", [])
            filesystem = dict(sandbox["filesystem"])
            filesystem.setdefault("denyRead", [])
            filesystem.setdefault("denyWrite", [])
            settings = {"network": network, "filesystem": filesystem}
            temporary_settings.write_text(json.dumps(settings), encoding="utf-8")
            temporary_settings.chmod(0o600)
            argv = [executable, "--settings", str(temporary_settings),
                    "--", "docker", "version"]
        except OSError as error:
            if probe_directory is not None:
                shutil.rmtree(probe_directory, ignore_errors=True)
            return {"status": "not_verified", "command": "srt docker version",
                    "error": f"could not prepare the Claude sandbox probe ({type(error).__name__})"}
    else:
        return {"status": "not_verified", "command": "docker version",
                "error": f"{adapter.name} has no Docker sandbox probe"}

    try:
        result = (runner or _run_sandbox_probe)(
            argv, cwd=cwd, capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        stdout = failure_excerpt(result.stdout or "", 1000)
        stderr = failure_excerpt(result.stderr or "", 1000)
        return {
            "status": "verified" if result.returncode == 0 else "not_verified",
            "command": "docker version",
            "executor": adapter.name,
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            **({"error": stderr or stdout or f"probe exited {result.returncode}"}
               if result.returncode else {}),
        }
    except subprocess.TimeoutExpired as error:
        return {"status": "not_verified", "command": "docker version",
                "executor": adapter.name,
                "error": f"sandbox probe timed out after {error.timeout or 30} seconds"}
    except (OSError, subprocess.SubprocessError) as error:
        return {"status": "not_verified", "command": "docker version",
                "executor": adapter.name,
                "error": f"sandbox probe could not run ({type(error).__name__}: {error})"}
    finally:
        if probe_directory is not None:
            shutil.rmtree(probe_directory, ignore_errors=True)


def _process_group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap_process_group(process: subprocess.Popen,
                        grace: float | None = None) -> bool:
    """Stop remaining descendants before declaring a POSIX dispatch exited.

    The group gets SIGTERM first, so a sandbox can unmount and remove what it
    created, then SIGKILL after a bounded grace period (#229).
    """
    if os.name == "posix":
        grace = PROCESS_GROUP_GRACE_SECONDS if grace is None else grace
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait()
            return True
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            # Reap the leader first: a zombie still counts as a group member.
            if process.poll() is not None and not _process_group_alive(process.pid):
                break
            time.sleep(0.05)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        return True
    # Reaping the parent alone cannot prove Windows descendants have exited.
    if process.poll() is None:
        process.kill()
    process.wait()
    return False


def _stop_process(process: subprocess.Popen, grace: float = 5) -> None:
    """Ask a child to stop, then make it: an interrupted host rarely waits long."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _publication_preflight(cwd: str | None, *, check_transport: bool = True) -> tuple[bool, str]:
    """Check the live GitHub/Git remote write path without changing remote state."""
    directory = cwd or os.getcwd()

    def run(argv: list[str]) -> subprocess.CompletedProcess:
        options = {"env": gh_environment()} if argv[:1] == ["gh"] else {}
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, cwd=directory,
                              stdin=subprocess.DEVNULL, **options)

    try:
        root = run(["git", "rev-parse", "--show-toplevel"])
        if root.returncode:
            return False, "publication requires a Git worktree"
        branch = run(["git", "branch", "--show-current"])
        if branch.returncode or not branch.stdout.strip():
            return False, "publication requires a named branch"
        remote = run(["git", "remote", "get-url", "origin"])
        if remote.returncode or not remote.stdout.strip():
            return False, "publication requires an origin remote"
        remote_url = remote.stdout.strip()
        parsed_remote = urlsplit(remote_url)
        hostname = parsed_remote.hostname or ""
        if not hostname:
            scp_remote = re.match(r"(?:[^@]+@)?([^:/]+):", remote_url)
            hostname = scp_remote.group(1) if scp_remote else ""
        hostname = hostname.lower()
        if hostname == "github.com" or hostname.endswith(".github.com"):
            if not shutil.which("gh"):
                return False, "GitHub publication requires gh on PATH"
            for argv in (["gh", "auth", "status"],
                         ["gh", "repo", "view", "--json", "nameWithOwner,viewerPermission"]):
                checked = run(list(argv))
                if checked.returncode:
                    return False, f"GitHub publication readiness failed at {argv[1]} (exit {checked.returncode})"
                if argv[1:3] == ["repo", "view"]:
                    try:
                        permission = json.loads(checked.stdout).get("viewerPermission")
                    except (ValueError, AttributeError):
                        permission = None
                    if permission not in {"WRITE", "MAINTAIN", "ADMIN"}:
                        return False, "GitHub account lacks repository push permission"
        # Do not probe the checked-out branch: cycle workers often start on a
        # protected base and create their feature branch only after dispatch.
        if check_transport:
            pushed = run(["git", "push", "--dry-run", "origin",
                          "HEAD:refs/heads/cc-cycle-preflight"])
            if pushed.returncode:
                return False, f"remote rejected the publication preflight (exit {pushed.returncode})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"publication preflight could not complete ({type(exc).__name__})"
    return True, ("Git authentication and remote write access passed a no-change preflight"
                  if check_transport else "host metadata checked; stage sandbox transport is still unverified")


class Adapter:
    """One way of running work. Subclasses implement `probe` and `dispatch`."""

    name = "abstract"
    #: Whether this adapter can enforce that a stage cannot mutate its assigned
    #: workspace. A review result is not trustworthy when its reviewer can
    #: change the diff it is judging, so unconfined reads fail closed.
    enforces_read_only = False
    #: Whether this adapter confines writes to the declared disposable cwd.
    enforces_workspace_boundary = False
    #: The strongest state this adapter can demonstrate without spending quota.
    provable_ceiling = Availability.READY
    #: Whether a successful dispatch means the work is done. False for a backend
    #: that starts work and returns a handle to it. Declared on the class rather
    #: than remembered at each return, so an adapter cannot report finished work
    #: it only launched by forgetting a keyword.
    completes_work = True
    requires_publication_preflight = False

    def probe(self) -> ProbeResult:  # pragma: no cover - interface
        raise NotImplementedError

    def dispatch(self, target: Target, task: str, **kw) -> DispatchResult:  # pragma: no cover
        raise NotImplementedError

    def _blocked(self, target: Target, capability: str, detail: str) -> DispatchResult:
        return DispatchResult(
            DispatchOutcome.BLOCKED, self.name, target,
            missing_capability=capability, detail=failure_excerpt(detail), start_state="not_started",
        )

    def supports_non_writing(self, **dispatch_kwargs) -> bool:
        """Whether this adapter can keep this non-writing dispatch isolated."""
        return self.enforces_read_only

    def supports_workspace_policy(self, policy: WorkspacePolicy | str,
                                  **dispatch_kwargs) -> bool:
        """Whether this adapter can honour the complete workspace contract."""
        policy = WorkspacePolicy(policy)
        if policy is WorkspacePolicy.READ_ONLY:
            return self.supports_non_writing(**dispatch_kwargs)
        if policy is WorkspacePolicy.WORKSPACE_WRITE:
            return True
        workspace = dispatch_kwargs.get("workspace")
        cwd = dispatch_kwargs.get("cwd")
        return (
            self.enforces_workspace_boundary
            and
            isinstance(workspace, DisposableWorkspace)
            and isinstance(cwd, str)
            and _canonical_path(cwd) == _canonical_path(workspace.path)
        )

    def publication_access(self, probe: ProbeResult, *, writes: bool) -> tuple[bool, str]:
        """Whether this adapter can grant a stage's declared publish access.

        New adapters fail closed until they declare how publication is
        permitted. Native adapters use the common host-side remote preflight.
        """
        return False, f"{self.name} has no declared publication permission contract"


class NativeAdapter(Adapter):
    """Common shape for running a CLI agent directly, without Orca.

    These cannot prove readiness. Their honest ceiling is `AUTHENTICATED`, and
    that is stated rather than rounded up.
    """

    binary = ""
    provable_ceiling = Availability.AUTHENTICATED
    requires_publication_preflight = True
    #: Substrings that identify an exhausted window in the agent's own output.
    quota_markers: tuple[str, ...] = ()
    #: Substrings that identify a screen only a human can answer.
    interactive_markers: dict[str, str] = {}

    def auth_evidence(self) -> tuple[bool, str]:  # pragma: no cover - interface
        raise NotImplementedError

    def probe(self, runner=_run, which=shutil.which) -> ProbeResult:
        if not which(self.binary):
            return ProbeResult(self.name, Availability.UNKNOWN, "not on PATH",
                               provable_ceiling=self.provable_ceiling)
        try:
            version = runner([self.binary, "--version"], timeout=20)
        except Exception as exc:
            return ProbeResult(self.name, Availability.UNKNOWN, "version check failed",
                               str(exc), self.provable_ceiling)
        if version.returncode != 0:
            return ProbeResult(self.name, Availability.INSTALLED, "binary present, version check failed",
                               version.stderr.strip()[:200], self.provable_ceiling)

        match = re.search(r"\b(\d+(?:\.\d+){1,3})\b", version.stdout or version.stderr or "")
        parsed_version = tuple(int(part) for part in match.group(1).split(".")) if match else None

        authenticated, evidence = self.auth_evidence()
        if not authenticated:
            return ProbeResult(self.name, Availability.INSTALLED,
                               "binary runs, no credential found", evidence,
                               self.provable_ceiling, parsed_version)
        # Deliberately stops here. This evidence shows that a credential is
        # configured, not that the session behind it still works or that quota
        # remains: both would cost a request to establish. READY is never
        # claimed from configuration alone.
        return ProbeResult(self.name, Availability.AUTHENTICATED, evidence,
                           "quota is not observable without dispatching",
                           self.provable_ceiling, parsed_version)

    def argv(self, target: Target, task: str, cwd: str | None = None,
             writes: bool = False, publishes: bool = False,
             publication_permissions: tuple[str, ...] = ()) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def dispatch_environment(self, *, writes: bool, publishes: bool,
                             writable_dirs: tuple[str, ...]) -> dict | None:
        """An environment for the dispatched CLI, or None to inherit this one."""
        return None

    def policy_preflight(self, policy: StagePolicy | None, cwd: str | None) -> str | None:
        """Why this executor cannot enforce the stage's command policy, or None."""
        return None

    def policy_notice(self, policy: StagePolicy | None, cwd: str | None) -> dict:
        """What the stage's command policy does not govern, to report; never a stop."""
        return {}

    @contextmanager
    def policy_files(self, argv: list[str], policy: StagePolicy | None, *,
                     scratch: str, cwd: str):
        """`argv` with whatever the command policy needs once TMPDIR exists."""
        yield argv

    def extra_artifacts(self, stdout: str) -> dict:
        """Facts from the complete output that a short tail would lose."""
        return {}

    def environment_restriction(self, message: str) -> str | None:
        """An executor error that means its sandbox could not start, or None."""
        return None

    def agent_output(self, stdout: str) -> str:
        """The agent's own message, out of whatever its CLI wraps it in.

        Every one of these tools prints a machine envelope and puts the reply
        inside it, JSON-encoded. Reading the envelope as if it were the reply
        finds the right words with the wrong escapes: a canary run against the
        real CLIs located `ORCHESTRATION_RESULT` in Claude's output and then
        failed to parse the block, because the newlines and quotes were still
        `\\n` and `\\"` inside a JSON string.

        So each adapter unwraps its own format and hands the rest of the system
        text. The driver above does not learn what `result` or `item.completed`
        mean, and neither does the next executor to be added.
        """
        return stdout

    def read_resolved_model(self, stdout: str) -> str | None:
        """Pull the model the run actually used out of its own output.

        Returns `None` when the output does not say. A silent executor leaves
        the question open; it does not license a guess.
        """
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            for key in ("model", "model_resolved", "resolved_model"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
            # Claude reports it as the key of a per-model usage map rather than
            # as a field. Observed on a live run: without this the resolved
            # model is lost for every Claude dispatch, and the contract check
            # that compares it against the request never fires.
            usage = payload.get("modelUsage")
            if isinstance(usage, dict) and len(usage) == 1:
                name = next(iter(usage))
                if isinstance(name, str) and name:
                    return name
        return None

    def _stream_activity(self, line: str, callback) -> None:
        """Extract activity and child-stage events from native JSON output."""
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return

        event_type = payload.get("type")
        delegated_tool_ids = getattr(self, "_delegated_tool_ids", None)
        if not isinstance(delegated_tool_ids, set):
            delegated_tool_ids = set()
            self._delegated_tool_ids = delegated_tool_ids
        if self.name == "codex":
            item = payload.get("item")
            if not isinstance(item, dict):
                return
            item_type = item.get("type")
            if item_type == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    callback(text=text)
            elif item_type == "collab_tool_call":
                call_id = item.get("id")
                if not isinstance(call_id, str) or not call_id:
                    return
                delegated_children = getattr(self, "_codex_delegated_children", None)
                if not isinstance(delegated_children, dict):
                    delegated_children = {}
                    self._codex_delegated_children = delegated_children

                label = self._codex_delegated_label(item)
                model = item.get("model")
                provider = item.get("provider")
                effort = item.get("reasoning_effort", item.get("reasoningEffort"))
                child_metadata = {
                    "delegated_provider": provider if isinstance(provider, str) else None,
                    "delegated_model": model if isinstance(model, str) else None,
                    "delegated_effort": effort if isinstance(effort, str) else None,
                }
                child_metadata = {
                    key: value for key, value in child_metadata.items()
                    if isinstance(value, str) and value.strip()
                }

                # A completed spawn call can still have live child threads.
                # Track those by their thread IDs and finish them only when a
                # later collaboration event reports a terminal agent status.
                states = item.get("agents_states")
                states = states if isinstance(states, dict) else {}
                receiver_ids = item.get("receiver_thread_ids")
                receiver_ids = {
                    thread_id for thread_id in receiver_ids
                    if isinstance(thread_id, str) and thread_id
                } if isinstance(receiver_ids, list) else set()
                child_threads = receiver_ids | {
                    thread_id for thread_id in states
                    if isinstance(thread_id, str) and thread_id
                }
                for thread_id in child_threads:
                    state = states.get(thread_id)
                    state_value = state.get("status") if isinstance(state, dict) else state
                    status = str(state_value or "").lower()
                    if status in {
                        "interrupted", "completed", "errored", "shutdown", "notfound",
                    }:
                        child_id = delegated_children.pop(thread_id, None)
                        if child_id:
                            callback(
                                text="delegated stage finished",
                                delegation_id=child_id,
                                delegation_finished=True,
                            )
                    elif status in {"pendinginit", "running"} or thread_id in receiver_ids:
                        child_id = delegated_children.get(thread_id)
                        if not child_id:
                            child_id = f"codex-child:{thread_id}"
                            delegated_children[thread_id] = child_id
                        callback(
                            text=f"delegated agent {thread_id} active",
                            delegation_id=child_id,
                            delegated_stage=label,
                            **child_metadata,
                        )

                if event_type in {"item.started", "item.updated"}:
                    # Early spawn events may not include the child thread ID.
                    # Keep a short-lived call entry until the child is reported.
                    if not child_threads:
                        callback(
                            text=f"delegated {label} started",
                            tool=event_type == "item.started",
                            delegation_id=call_id,
                            delegated_stage=label,
                            **child_metadata,
                        )
                elif event_type == "item.completed":
                    callback(
                        text="delegated call completed" if not child_threads else None,
                        delegation_id=call_id,
                        delegation_finished=True,
                    )
            elif event_type == "item.started" and item_type in {
                "command_execution", "mcp_tool_call", "web_search",
            }:
                callback(text=f"{item_type.replace('_', ' ')} started", tool=True)
            return

        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if event_type == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    callback(text=block["text"])
                elif block.get("type") == "tool_use":
                    name = block.get("name")
                    tool_input = block.get("input")
                    if name in {"Task", "Agent"} and isinstance(tool_input, dict):
                        call_id = block.get("id")
                        label = tool_input.get("name") or tool_input.get("subagent_type")
                        if not isinstance(label, str) or not label.strip():
                            label = "delegated agent"
                        if isinstance(call_id, str) and call_id:
                            delegated_tool_ids.add(call_id)
                            callback(
                                text=f"delegated {label} started",
                                tool=True,
                                delegation_id=call_id,
                                delegated_stage=label,
                            )
                        else:
                            callback(text=f"Using {name}", tool=True)
                    else:
                        callback(
                            text=f"Using {name}" if isinstance(name, str) else "Using tool",
                            tool=True,
                        )
            return

        if event_type == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = block.get("tool_use_id")
                if isinstance(call_id, str) and call_id in delegated_tool_ids:
                    delegated_tool_ids.remove(call_id)
                    callback(
                        text="delegated stage finished",
                        delegation_id=call_id,
                        delegation_finished=True,
                    )
            return

        if event_type == "content_block_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("text"), str):
                callback(text=delta["text"])

    @staticmethod
    def _codex_delegated_label(item: dict) -> str:
        agents = item.get("receiver_agents")
        if isinstance(agents, list):
            for agent in agents:
                if not isinstance(agent, dict):
                    continue
                label = agent.get("agent_role") or agent.get("agent_nickname")
                if isinstance(label, str) and label.strip():
                    return label.strip()

        for key in ("agent_role", "agent_type", "subagent_type", "agent_nickname"):
            label = item.get(key)
            if isinstance(label, str) and label.strip():
                return label.strip()

        prompt = item.get("prompt")
        if isinstance(prompt, str):
            normalized = prompt.lower().replace("_", "-")
            known_passes = (
                ("security", r"\b(?:cc[- ]?)?security(?:[- ]review)?\b"),
                ("code review", r"\bcode[- ]review\b"),
                ("issue review", r"\bissue[- ]review\b"),
                ("rereview", r"\brereview\b"),
                ("resolve comments", r"\bresolve[- ]comments\b"),
                ("implement issue", r"\bimplement[- ]issue\b"),
                ("verification", r"\bcc[- ]verify\b|\bverification\b"),
            )
            for label, pattern in known_passes:
                if re.search(pattern, normalized):
                    return label
        return "unknown"

    def readable(self, argv: list[str], read_dirs: tuple[str, ...]) -> list[str]:
        """`argv`, able to read `read_dirs` as well as its own directory.

        The runtime puts evidence outside every workspace, such as the complete
        diff a review judges. This CLI's sandbox already reads the whole file
        system, so the default adds nothing. `dispatch()` passes `read_dirs`
        only to a stage that does not write, because a granted directory may
        be writable too.
        """
        return argv

    def dispatch(self, target: Target, task: str, *, cwd: str | None = None,
                 timeout: int = DEFAULT_DISPATCH_TIMEOUT_SECONDS, runner=_run,
                 writes: bool = False, publishes: bool = False,
                 publication_permissions: tuple[str, ...] = (),
                 claude_allowed_commands: tuple[str, ...] = (),
                 read_dirs: tuple[str, ...] = (),
                 writable_dirs: tuple[str, ...] = (),
                 sandbox_grants: tuple[str, ...] = (),
                 command_policy: StagePolicy | None = None,
                 review_clone: str | None = None,
                 on_progress=None, on_workspace=None,
                 dispatch_attempt_id: str | None = None,
                 docker_probe_runner=None) -> DispatchResult:
        """Run the agent non-interactively and classify what came back.

        `writes` is what the stage is for, not what it might want: an
        implementation edits the tree it was given, a review reads it. The
        default is the smaller permission, so a caller that says nothing asks
        for nothing.

        Non-interactive on purpose. An agent waiting on a trust dialog, a hook
        review or a login cannot be driven from here, and answering such a
        screen blind is not something this layer will do — it reports the
        missing capability and stops.

        `command_policy` is the stage's command table (#142). An inactive one,
        for a non-GitHub cycle, changes nothing and is only reported.
        """
        if command_policy is not None and dispatch_attempt_id is not None:
            # The hook records its own denials under this attempt (#306).
            command_policy = replace(command_policy, attempt_id=dispatch_attempt_id)
        policy = command_policy if command_policy is not None and command_policy.active else None
        requested_verification = command_policy.verification_commands if command_policy is not None else ()
        policy_note: dict[str, Any] = ({} if command_policy is None else {"command_policy": (
            "active" if policy is not None
            else f"inactive: {command_policy.inactive_reason}")})
        # Record native-sandbox prerequisites before dependency setup or agent
        # dispatch. This evidence is attached to the stage attempt; a missing
        # capability must not silently turn an unrun check into a pass.
        prerequisite_path = os.environ.get("PATH", "")
        if command_policy is not None:
            sandbox_evidence = native.observe(self.name, recover=runner is _run)
            prerequisite_path = native.tool_path(prerequisite_path, probes=sandbox_evidence.get("probes", []))
            missing = sandbox_evidence["missing"]
            needs_bash_sandbox = (self.name == "claude" and policy is not None
                                  and (policy.requires_confined_verification or policy.registry_domains
                                       or policy.publication_commands))
            if needs_bash_sandbox and sys.platform.startswith("linux") and runner is _run:
                ready, detail = native.isolation_probe("bwrap", runner=_run_sandbox_probe, path=prerequisite_path)
                sandbox_evidence["functional_check"] = {"probe": "bwrap", "passed": ready,
                                                          "evidence": detail}
                sandbox_evidence["status"] = "verified" if ready else "unverified"
                if not ready or any(tool in missing for tool in ("bwrap", "socat")):
                    # The existing hook + disposable clone remains the static
                    # boundary. Remove code execution grants, never run tests
                    # outside a sandbox. Writing stages may still edit, but
                    # publication requires a working sandbox and stays denied.
                    command_policy = replace(command_policy, static_only=True)
                    policy = command_policy
                    sandbox_evidence["mode"] = "static_only"
                    sandbox_evidence["status"] = "unverified"
            if (self.name == "claude" and sys.platform.startswith("linux")
                    and runner is _run and "srt" not in missing):
                ready, detail = native.isolation_probe("srt", runner=_run_sandbox_probe, path=prerequisite_path)
                sandbox_evidence["preparation_isolation"] = {"passed": ready, "evidence": detail}
                if not ready:
                    sandbox_evidence["status"] = "unverified"
            policy_note["native_sandbox_preflight"] = sandbox_evidence
            if sandbox_evidence["status"] == "unverified":
                task += (
                    "\n\nNative sandbox preflight evidence (runtime observed): "
                    f"{json.dumps(sandbox_evidence, sort_keys=True)}. Keep the selected executor. "
                    "Do not launch a nested sandbox or weaken confinement. Continue independent "
                    "work that is safe to perform; identify each affected check as not_verified "
                    "with its cause and evidence, never as passed. For reviews, continue static "
                    "analysis only if the existing read-only boundary remains safe; otherwise "
                    "report the specific remaining blocker. When main work is complete, return "
                    "its completed functional status with tests.conclusion=not_verified; a known "
                    "prerequisite alone is not BLOCKED. Never describe an incomplete implementation "
                    "as complete."
                )
        refused = self.policy_preflight(policy, cwd)
        if refused is not None:
            return DispatchResult(
                DispatchOutcome.FAILED, self.name, target,
                detail=failure_excerpt(f"BLOCKED: {refused}", 600),
                error_code="precondition", start_state="not_started",
                lifecycle_state="failed",
                artifacts={**policy_note, "command_policy": f"blocked: {failure_excerpt(refused, 300)}",
                           "policy_preflight_refusal": failure_excerpt(refused, 600)},
            )
        try:
            policy_note = {**policy_note, **self.policy_notice(policy, cwd)}
        except Exception:
            pass
        docker_granted = ("docker_host" in sandbox_grants
                          or bool(command_policy is not None and command_policy.docker))
        if docker_granted:
            native_preflight = policy_note.get("native_sandbox_preflight", {})
            if ((command_policy is not None and command_policy.static_only)
                    or native_preflight.get("status") == "unverified"):
                docker_probe = {
                    "status": "not_verified", "command": "docker version",
                    "executor": self.name,
                    "error": "the stage's effective native sandbox could not be reproduced; "
                             "Docker reachability was not checked",
                    "sandbox_evidence": native_preflight,
                }
            else:
                docker_probe = _docker_host_probe(
                    self, cwd=cwd or os.getcwd(), writes=writes, publishes=publishes,
                    network_domains=tuple(command_policy.registry_domains
                                          if command_policy is not None
                                          else ()),
                    path=prerequisite_path, runner=docker_probe_runner,
                )
            policy_note["docker_host_probe"] = docker_probe
            if docker_probe.get("status") != "verified":
                task += (
                    "\n\nDocker grant sandbox probe (runtime evidence): "
                    f"{json.dumps(docker_probe, sort_keys=True)}. Keep the selected executor "
                    "and do not ask for this grant again. Continue independent implementation "
                    "and checks. Do not run Docker-dependent checks unless reachability is "
                    "verified; report those checks as not_verified with this error and continue. "
                    "When the main work is complete, return its functional status with "
                    "tests.conclusion=not_verified rather than stopping for this prerequisite."
                )
        worker_cwd = cwd or os.getcwd()
        if publishes and writes and command_policy is not None and command_policy.publication_commands:
            preflight, push = (shlex.join(command) for command in command_policy.publication_commands)
            task += (
                "\n\nGitHub upload recovery (#284): before editing, run the publication "
                f"preflight inside this stage's own sandbox: {preflight}. "
                f"Upload the existing working-branch commit with: {push}. "
                "The helper retains the original push failure and retries using command-scoped "
                "HTTPS and the gh credential helper, without changing remotes or credentials. "
                "A host preflight or successful HTTP comment is not upload evidence. "
                "Do not use a host-side push. If upload fails, retain the commit and return "
                "BLOCKED with publication evidence; a progress comment must say that the "
                "push failed. Before publishing any success receipt or claiming a fix as "
                "remotely applied, read the PR with gh and confirm its head equals the uploaded "
                "commit. Reuse an unpublished commit on resume; do not recreate it or repeat "
                "a success comment already published for this attempt."
            )
        preparation = {"status": "not_required", "items": []}
        if command_policy is not None and command_policy.role in sp.WRITING_ROLES | sp.REVIEW_ROLES:
            try:
                preflight = policy_note.get("native_sandbox_preflight", {})
                if (preflight.get("codex_socket") or command_policy.static_only
                        or preflight.get("preparation_isolation", {}).get("passed") is False):
                    preparation = {"status": "incomplete", "items": [],
                                   "error": "native preparation isolation unavailable",
                                   "prerequisite_evidence": preflight}
                else:
                    preparation = dependency_setup.prepare(
                        worker_cwd, self.name, self.binary, domains=command_policy.registry_domains,
                        prerequisite_path=prerequisite_path)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                preparation = {"status": "incomplete", "items": [], "error": str(error)}
            if preparation["status"] != "not_required":
                # Package-manager launchers and configured verification can need
                # the same registries after installation. Keep that narrow route
                # in the worker, including when preparation exhausted recovery.
                # Do not mutate the source policy or authorize general networking.
                command_policy = replace(command_policy, registry_domains=tuple(dict.fromkeys(
                    (*dependency_setup.REGISTRIES, *command_policy.registry_domains))))
                policy = command_policy if policy is not None else None
                policy_note["dependency_preparation"] = preparation
        argv_options = {}
        if self.name == "codex" and command_policy is not None:
            # The owner explicitly authorizes issue-review GitHub reads with
            # the same credentials as publishing stages (#279). Its workspace
            # and publication contract remain read-only. Other non-publishing
            # roles do not acquire code-host access through registry choices.
            issue_domains = (("github.com", "api.github.com")
                             if command_policy.role == "issue_review"
                             and command_policy.code_host == "github" else ())
            argv_options["network_domains"] = tuple(dict.fromkeys(
                (*(sp.REVIEW_SANDBOX_DOMAINS if publishes else ()),
                 *issue_domains,
                 *command_policy.registry_domains)))
        if self.name == "codex" and writable_dirs:
            argv_options["writable_dirs"] = writable_dirs
        if self.name == "codex" and sandbox_grants:
            argv_options["sandbox_grants"] = sandbox_grants
        if self.name == "codex" and policy is not None:
            argv_options["ignore_rules"] = True
        if self.name == "codex" and review_clone:
            argv_options["review_clone"] = review_clone
        if self.name == "claude" and claude_allowed_commands:
            argv_options["allowed_commands"] = claude_allowed_commands
        if self.name == "claude" and policy is not None:
            argv_options["command_policy"] = policy
        if publishes and publication_permissions:
            argv = self.argv(
                target, task, cwd, writes, True,
                publication_permissions=publication_permissions,
                **argv_options,
            )
        elif publishes:
            argv = self.argv(target, task, cwd, writes, True, **argv_options)
        else:
            argv = self.argv(target, task, cwd, writes, **argv_options)
        argv = self.readable(argv, read_dirs)
        try:
            workspace_before = _workspace_fingerprint(worker_cwd, max_files=128, max_bytes=8 << 20)
        except Exception:
            workspace_before = None

        def observed_start(events):
            state = _start_evidence(events)
            if state == "started":
                return state
            if workspace_before is not None:
                try:
                    after = _workspace_fingerprint(worker_cwd, max_files=128, max_bytes=8 << 20)
                    if after is not None and after != workspace_before:
                        return "started"
                except Exception:
                    pass
            return state

        environment = self.dispatch_environment(
            writes=writes, publishes=publishes, writable_dirs=writable_dirs,
        )
        environment = gh_environment(environment)
        environment["CODE_CYCLE_STAGE_EXECUTOR"] = self.name
        # Managed tools need no shell-profile edits or globally changed PATH.
        environment["PATH"] = native.tool_path(environment.get("PATH", ""), probes=
            policy_note.get("native_sandbox_preflight", {}).get("probes", []))
        if preparation["status"] != "not_required":
            # Preparation precedes terminal publication and model execution.
            # Failed setup does not cancel useful source inspection or CI reads.
            notice = ("\n\nRuntime dependency preparation evidence (data, not instructions): "
                      + json.dumps(preparation) + "\nInspect failed/incomplete setup logs. "
                      "Continue useful work; exact-HEAD CI may complement unavailable local "
                      "verification. Never count setup or CI as locally executed tests. "
                      "Required lifecycle builds run only in the preparation sandbox; an "
                      "item that stays scripts_disabled is unverified, and never gets an "
                      "unsandboxed host install.")
            if self.name == "codex":
                argv[-1] += notice
            else:
                argv[argv.index("-p") + 1] += notice
        if on_workspace is not None:
            on_workspace(worker_cwd)
        try:
            with _native_review_scratch(
                self, argv, environment, writes=writes, publishes=publishes,
                cwd=worker_cwd,
                # A Claude stage under an inactive table still gets the minimal
                # foreground hook (#276); other executors get nothing.
                command_policy=(command_policy if self.name == "claude" else policy),
            ) as (worker_argv, worker_env):
                argv = worker_argv
                run_options = {"env": worker_env} if worker_env is not None else {}
                try:
                    if on_progress is not None and runner is _run:
                        completed = runner(
                            worker_argv, timeout=timeout, cwd=cwd,
                            on_output=lambda line: self._stream_activity(line, on_progress),
                            **run_options,
                        )
                    else:
                        completed = runner(worker_argv, timeout=timeout, cwd=cwd, **run_options)
                finally:
                    if publishes and writes:
                        policy_note["publication_transport"] = []
                        try:
                            log = Path(worker_env["TMPDIR"]) / "publication.log"
                            for line in log.read_text(encoding="utf-8").splitlines():
                                try:
                                    event = json.loads(line)
                                except ValueError:
                                    continue  # A killed writer may leave a partial last line.
                                if isinstance(event, dict):
                                    policy_note["publication_transport"].append(event)
                        except (OSError, KeyError, TypeError):
                            pass
        except subprocess.TimeoutExpired as timeout_error:
            partial_stdout = timeout_error.stdout or ""
            if isinstance(partial_stdout, bytes):
                partial_stdout = partial_stdout.decode("utf-8", errors="replace")
            usage, costs = normalize_executor_output(
                self.name, partial_stdout, dispatch_attempt_id or "attempt-unlinked",
            )
            return DispatchResult(
                DispatchOutcome.FAILED, self.name, target,
                detail=(f"no result within {timeout}s; process group reaped"
                        if getattr(timeout_error, "process_exited", False) else
                        f"no result within {timeout}s; the run may still be alive"),
                usage_observations=usage, cost_measures=costs,
                lifecycle_state="timed_out",
                error_code="timeout", start_state=observed_start(_events(partial_stdout)),
                process_exited=getattr(timeout_error, "process_exited", False),
                artifacts=policy_note,
            )
        except Exception as exc:
            not_launched = isinstance(exc, OSError)
            return DispatchResult(DispatchOutcome.FAILED, self.name, target,
                                  detail=failure_excerpt(str(exc)), lifecycle_state="failed",
                                  error_code="precondition" if not_launched else "executor_error",
                                  start_state="not_started" if not_launched else "unknown",
                                  artifacts=policy_note)

        if on_workspace is not None:
            on_workspace(worker_cwd)

        stdout = completed.stdout or ""
        usage, costs = normalize_executor_output(
            self.name, stdout, dispatch_attempt_id or "attempt-unlinked",
        )
        events = _events(stdout)
        signal = _error_signal(events)
        terminal_success = any(event.get("type") in {"turn.completed", "task_complete"}
                               for event in events) and signal is None
        has_agent_result = any(
            isinstance(event.get("item"), dict)
            and event["item"].get("type") == "agent_message"
            and isinstance(event["item"].get("text"), str)
            and event["item"]["text"].strip()
            for event in events
        )
        from environment_recovery import codex_sandbox_denials, needs_sandbox_evidence
        needs_failure_log = (signal is None and (completed.returncode != 0
                             or (not terminal_success and not has_agent_result)))
        session_events = (self._session_events(events) if self.name == "codex"
                          and (needs_failure_log or needs_sandbox_evidence(self.agent_output(stdout)))
                          else [])
        if (self.name == "codex" and signal is None
                and (completed.returncode != 0
                     or (not terminal_success and not has_agent_result))):
            events += session_events
            signal = _error_signal(session_events)
        start_state = observed_start(events)
        evidence = {"usage_observations": usage, "cost_measures": costs,
                    "start_state": start_state,
                    "process_exited": getattr(completed, "process_exited", False)}
        annotations = {**policy_note, **self.extra_artifacts(stdout)}
        if self.name == "codex":
            denials = codex_sandbox_denials(session_events)
            if denials:
                annotations["sandbox_denials"] = denials
        result = self._classify_completed(target, completed, argv, stdout, signal, evidence)
        if policy is not None and policy.static_only:
            result = replace(result, agent_output=native.static_result(
                result.agent_output, requested_verification,
                policy_note.get("native_sandbox_preflight", {})))
        if annotations:
            # A classification's own note, such as a sandbox that did not
            # start, outranks the policy's general one.
            result = replace(result, artifacts={**annotations, **result.artifacts})
        return result

    def _classify_completed(self, target: Target, completed, argv: list[str], stdout: str,
                            signal, evidence: dict) -> DispatchResult:
        """Classify a run whose process returned."""
        if signal is not None:
            raw_code, message = signal
            friction = self._markers_in(message)
            code = _error_class(raw_code, message)
            # Preserve availability signals without letting them overrule a
            # structured code (especially a new executor code).
            if code == "quota":
                friction = ("operating_quota", message)
            elif raw_code not in {None, "other"}:
                friction = None
            if friction is not None:
                capability, _ = friction
                return replace(self._blocked(target, capability, message), **evidence,
                               raw_error_code=raw_code, lifecycle_state="failed")
            return DispatchResult(
                DispatchOutcome.FAILED, self.name, target,
                detail=failure_excerpt(message), error_code=code, raw_error_code=raw_code,
                **evidence, lifecycle_state="failed",
            )
        friction = self.classify_failure(
            completed.returncode, completed.stderr or "", ""
        )
        if friction is not None:
            capability, detail = friction
            observed = failure_excerpt(completed.stderr or detail)
            return replace(self._blocked(target, capability, observed), **evidence,
                           lifecycle_state="failed")
        if completed.returncode != 0:
            stderr = completed.stderr or ""
            message = "\n".join(line for line in stderr.splitlines()
                                if line.strip() != "Reading additional input from stdin...")
            restriction = self.environment_restriction(message)
            if restriction is not None:
                # The sandbox a confined stage requires did not start, so no
                # tool ran. Another executor may still provide the guarantee.
                return DispatchResult(
                    DispatchOutcome.FAILED, self.name, target,
                    detail=failure_excerpt(restriction, 600),
                    error_code="environment_restricted",
                    artifacts={"argv": argv, "returncode": completed.returncode,
                               "command_policy": "blocked: sandbox unavailable"},
                    **{**evidence, "start_state": "not_started"}, lifecycle_state="failed",
                )
            return DispatchResult(
                DispatchOutcome.FAILED, self.name, target,
                detail=failure_excerpt(message),
                error_code=_error_class(None, message) if message.strip() else "no_error_report",
                artifacts={"argv": argv, "returncode": completed.returncode},
                **evidence, lifecycle_state="failed",
            )
        resolved = self.read_resolved_model(stdout)
        # Two different jobs, and only one of them may be bounded. The tail in
        # `artifacts` is for a human reading afterwards, so it is cut to keep a
        # result small. `agent_output` is what the caller parses its answer out
        # of, so it is whole: a limit on it silently deletes valid results,
        # which is what a length cap here did to any reply that kept talking
        # past its own structured block.
        artifacts = {"argv": argv, "stdout": stdout[-4000:]}
        spoken = self.agent_output(stdout)
        if resolved is not None and resolved != target.model:
            return DispatchResult(
                DispatchOutcome.CONTRACT_VIOLATION, self.name, target,
                model_resolved=resolved,
                detail=(f"requested {target.model}, the executor reported running"
                        f" {resolved}"),
                artifacts=artifacts, agent_output=spoken, **evidence,
                lifecycle_state="completed",
            )
        return DispatchResult(
            DispatchOutcome.SUCCEEDED, self.name, target,
            model_resolved=resolved, artifacts=artifacts, agent_output=spoken,
            **evidence, lifecycle_state="completed",
        )

    def _session_events(self, events: list[dict]) -> list[dict]:
        """Read only the session identified by this invocation, never the newest log."""
        thread_ids = [event.get("thread_id") for event in events
                      if event.get("type") == "thread.started"]
        root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
        for thread_id in thread_ids:
            if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", thread_id):
                continue
            for path in root.glob(f"**/*{thread_id}.jsonl"):
                try:
                    logged = _events(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    continue
                # The filename alone is not authority for session identity.
                if any(event.get("id") == thread_id for event in logged):
                    return logged
        return []

    def classify_failure(self, returncode: int, stderr: str, stdout: str) -> tuple[str, str] | None:
        """Recognise a failure that a human, not a retry, has to resolve.

        Only a failed run is classified. Scanning a successful run's output for
        words like "quota" or "sign in" reads the agent's own answer as the
        executor's error: an implementation that adds rate-limit handling says
        "quota" for entirely ordinary reasons, and reporting that as an
        exhausted window would be the tool inventing an outage.

        stderr is searched first because that is where an executor reports its
        own trouble; stdout is searched only as a fallback, and only once the
        exit status has already established that something went wrong.
        """
        if returncode == 0:
            return None
        found = self._markers_in(stderr or "")
        if found is not None:
            return found
        # stdout carries the agent's own answer, and a failed run does not make
        # that answer evidence about the executor: "Implemented quota handling"
        # says nothing about a window being exhausted whatever the exit status
        # was. Only structured events are read here, never free text.
        return self._markers_in_events(stdout or "")

    def _markers_in_events(self, stdout: str) -> tuple[str, str] | None:
        """Read terminal error events from a structured stream, if there is one.

        Codex `exec --json` and Claude `--output-format json` both emit JSON
        objects. Only fields an executor uses to report its own trouble are
        inspected; the assistant's message text is not one of them.
        """
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not self._is_error_event(payload):
                continue
            for key in ("error", "message", "reason", "detail", "subtype"):
                value = payload.get(key)
                if isinstance(value, dict):
                    value = value.get("message") or value.get("code")
                if isinstance(value, str):
                    found = self._markers_in(value)
                    if found is not None:
                        return found
        return None

    @staticmethod
    def _is_error_event(payload: dict) -> bool:
        if payload.get("is_error") is True:
            return True
        kind = payload.get("type") or payload.get("event") or ""
        return isinstance(kind, str) and ("error" in kind.lower() or kind == "failure")

    def _markers_in(self, text: str) -> tuple[str, str] | None:
        lowered = text.lower()
        for marker in self.quota_markers:
            if marker.lower() in lowered:
                return "operating_quota", f"the executor reported an exhausted window: {marker}"
        for marker, capability in self.interactive_markers.items():
            if marker.lower() in lowered:
                return capability, f"an interactive screen is waiting: {marker}"
        return None


class CodexAdapter(NativeAdapter):
    name = "codex"
    enforces_workspace_boundary = True
    binary = "codex"
    enforces_read_only = True
    quota_markers = ("usage limit", "rate limit", "quota")
    interactive_markers = {
        "hooks need review": "hook_trust",
        "trust this folder": "folder_trust",
        "not inside a trusted directory": "trusted_directory",
        "sign in": "authenticated_session",
    }
    # The only parts of the common Git directory a publishing stage may write.
    # The common root stays read-only, so its `config`, `hooks`, `packed-refs`,
    # `commondir`, `gitdir`, `modules`, other worktrees' pointers, and the main
    # worktree's HEAD, index and operation state need no rule of their own.
    # Codex's Linux sandbox materialises a missing rule destination as an empty
    # file (#229), so a rule may only name a path that already exists.
    WRITABLE_COMMON_DIRS = ("objects", "refs", "logs")
    # Pointers and configuration inside the writable private directory of a
    # linked worktree. Git follows `commondir` and `gitdir` to find the
    # repository, and reads `config.worktree` when `extensions.worktreeConfig`
    # is on, so each one that exists stays read-only.
    WORKTREE_POINTERS = ("commondir", "gitdir")
    WORKTREE_CONFIG = ("config.worktree", "config.worktree.lock")
    # Main-worktree state that lives below the writable `refs` and `logs`
    # roots. Existing entries stay read-only; an absent one is left out rather
    # than created, and Git creates it on demand for the main worktree.
    MAIN_WORKTREE_WRITABLE_STATE = (
        "logs/HEAD",
        "refs/bisect", "refs/worktree", "refs/rewritten",
        "logs/refs/bisect", "logs/refs/worktree", "logs/refs/rewritten",
    )
    # Automatic maintenance repacks refs and prunes objects, which needs the
    # read-only common root; inside the sandbox it would only fail.
    SANDBOX_GIT_CONFIG = (("gc.auto", "0"), ("maintenance.auto", "false"))

    def publication_access(self, probe: ProbeResult, *, writes: bool) -> tuple[bool, str]:
        if probe.version is None or probe.version < (0, 138, 0):
            return False, "Codex permission profiles (required for scoped publication access) need CLI 0.138.0 or later"
        return True, "Codex permission profile scopes network access to code hosts and preserves the stage filesystem boundary"

    @classmethod
    def git_write_roots(cls, writable_dirs: tuple[str, ...]) -> tuple[str, ...]:
        """The Git directories a writing stage may write; never the common root.

        Only directories that exist are returned: a missing one would become a
        synthetic mount destination rather than a grant.
        """
        if not writable_dirs:
            return ()
        common = Path(writable_dirs[0])
        roots = [common / name for name in cls.WRITABLE_COMMON_DIRS]
        if len(writable_dirs) > 1:
            roots.append(Path(writable_dirs[-1]))
        return tuple(str(root) for root in roots if root.is_dir())

    def unprotectable_git_paths(
        self, cwd: str, writable_dirs: tuple[str, ...], *, git=None,
    ) -> str | None:
        """Name a dangerous absent path the sandbox could only protect by creating it."""
        if len(writable_dirs) < 2:
            return None
        worktree = Path(writable_dirs[-1])
        read = git or _git_read
        try:
            status, enabled = read(cwd, "config", "--type=bool", "--get",
                                   "extensions.worktreeConfig")
            if status not in (0, 1):
                return "could not read extensions.worktreeConfig before the Codex sandbox"
            if status == 0 and enabled == "true":
                missing = [name for name in self.WORKTREE_CONFIG
                           if not (worktree / name).exists()]
                if missing:
                    return (
                        "extensions.worktreeConfig is enabled and the worktree's "
                        f"{', '.join(missing)} is absent; the Codex sandbox could "
                        "only protect it by creating it"
                    )
            status, hooks = read(cwd, "config", "--type=path", "--get", "core.hooksPath")
            if status not in (0, 1):
                return "could not read core.hooksPath before the Codex sandbox"
            if status == 1 or not hooks:
                # The default hooks directory sits in the read-only common root.
                return None
            hooks_path = Path(hooks)
            if not hooks_path.is_absolute():
                # Git resolves a relative hooksPath from the worktree's top level.
                status, top = read(cwd, "rev-parse", "--show-toplevel")
                if status != 0 or not top:
                    return "could not resolve a relative core.hooksPath before the Codex sandbox"
                hooks_path = Path(top) / hooks_path
            hooks_path = hooks_path.resolve()
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return f"could not read the Git configuration before the Codex sandbox ({type(exc).__name__})"
        for root in self.git_write_roots(writable_dirs):
            if hooks_path.is_relative_to(Path(root).resolve()):
                return (
                    f"core.hooksPath resolves inside the writable Git directory {Path(root).name}; "
                    "a hook written there would run outside the sandbox"
                )
        return None

    @classmethod
    def _protected_git_paths(
        cls, common: Path, worktree: Path, cwd: str | None,
    ) -> tuple[list[Path], list[Path]]:
        """Existing directories and files a linked-worktree stage must not write."""
        directories = [common, common / "worktrees", common / "hooks"]
        files = [common / "config"]
        files.extend(worktree / name for name in (*cls.WORKTREE_POINTERS, *cls.WORKTREE_CONFIG))
        if cwd and Path(cwd) / ".git" != common:
            files.append(Path(cwd) / ".git")
        for name in cls.MAIN_WORKTREE_WRITABLE_STATE:
            path = common / name
            (directories if path.is_dir() else files).append(path)
        return (
            [path for path in directories if path.is_dir()],
            [path for path in files if path.is_file()],
        )

    def probe_git_metadata_write(
        self, target: Target, cwd: str, writable_dirs: tuple[str, ...], *,
        runner=None,
    ) -> tuple[bool, str]:
        """Check the required Git writes and that the rest of the metadata stays read-only.

        Neither the probe nor the profile it runs under names an absent path:
        the Linux sandbox would materialise it as an empty mount destination
        on the host (#229). Its own markers carry a unique name and token, so
        cleanup removes only what this probe wrote.
        """
        if not writable_dirs:
            return True, "no additional Git metadata roots are required"
        common = Path(writable_dirs[0])
        if len(writable_dirs) < 2:
            return False, (
                "Codex cannot publish from the main worktree: its index and HEAD live "
                "in the common Git directory, which the sandbox keeps read-only"
            )
        worktree = Path(writable_dirs[-1])
        writable_paths = [common / name for name in self.WRITABLE_COMMON_DIRS]
        writable_paths.append(worktree)
        for directory in writable_paths:
            if not directory.is_dir():
                return False, f"required Git metadata directory is missing: {directory.name}"
        unsafe = self.unprotectable_git_paths(cwd, writable_dirs)
        if unsafe:
            return False, unsafe
        write_markers = [
            directory / f".code-cycle-write-probe-{uuid.uuid4().hex}"
            for directory in writable_paths
        ]
        tokens = [uuid.uuid4().hex for _ in write_markers]
        cleanup_tokens = dict(zip(write_markers, tokens))
        protected_directories, protected_files = self._protected_git_paths(common, worktree, cwd)
        denied_markers: list[tuple[Path, Path, str]] = []
        for directory in protected_directories:
            marker = directory / f".code-cycle-deny-probe-{uuid.uuid4().hex}"
            token = uuid.uuid4().hex
            cleanup_tokens[marker] = token
            denied_markers.append((directory, marker, token))

        writes = "; ".join(
            f"create_exclusive({json.dumps(str(marker))}, {json.dumps(token)})"
            for marker, token in zip(write_markers, tokens)
        )
        script_lines = [
            "from pathlib import Path; import errno, os",
            "def create_exclusive(name, token):",
            " fd=os.open(name, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)",
            " try:",
            "  payload=token.encode(); offset=0",
            "  while offset < len(payload):",
            "   count=os.write(fd, payload[offset:])",
            "   if count <= 0: raise OSError('short write during Git metadata probe')",
            "   offset += count",
            " finally: os.close(fd)",
            writes,
        ]
        denied = ("except OSError as exc:",
                  " if exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS): raise")
        for directory, marker, token in denied_markers:
            script_lines.extend((
                "try:",
                f" create_exclusive({json.dumps(str(marker))}, {json.dumps(token)})",
                *denied,
                "else:",
                f" raise SystemExit({json.dumps(f'protected Git metadata path is writable: {directory.name}')})",
            ))
        for path in protected_files:
            # No O_CREAT: a path that vanished fails the probe instead of reappearing.
            script_lines.extend((
                "try:",
                f" fd=os.open({json.dumps(str(path))}, os.O_WRONLY|os.O_APPEND); os.close(fd)",
                *denied,
                "else:",
                f" raise SystemExit({json.dumps(f'protected Git metadata path is writable: {path.name}')})",
            ))
        # Read-only pointers must stay readable, or Git cannot open the
        # worktree at all; a write-only check would not notice.
        for path in protected_files:
            if path.name not in ("commondir", "gitdir", ".git", "config"):
                continue
            script_lines.extend((
                "try:",
                f" Path({json.dumps(str(path))}).read_bytes()",
                "except OSError as exc:",
                f" raise SystemExit({json.dumps(f'protected Git metadata path is not readable: {path.name}')})",
            ))
        script = "\n".join(script_lines) + "\n"
        profile = "code_cycle_publish_write"
        filesystem = self._publish_filesystem_rules(writable_dirs, cwd=cwd)
        argv = [
            self.binary, "sandbox", "--permission-profile", profile,
            "--cd", cwd,
        ]
        argv.extend((
            "-c", f'permissions.{profile}.extends=":workspace"',
            "-c", f"permissions.{profile}.filesystem={filesystem}",
            "--", sys.executable, "-c", script,
        ))

        def cleanup_markers() -> bool:
            failed = False
            for marker, token in cleanup_tokens.items():
                try:
                    if marker.is_file() and marker.read_text() == token:
                        marker.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    failed = True
            return not failed

        try:
            with tempfile.TemporaryDirectory(
                prefix=".code-cycle-codex-probe-", dir=cwd,
            ) as codex_home:
                if os.name != "nt":
                    os.chmod(codex_home, 0o700)
                environment = os.environ.copy()
                environment["CODEX_HOME"] = codex_home
                result = (runner or _run_sandbox_probe)(
                    argv, cwd=cwd, capture_output=True, text=True, timeout=15,
                    stdin=subprocess.DEVNULL, env=environment,
                )
        except subprocess.TimeoutExpired:
            cleanup_markers()
            return False, "Codex Git metadata sandbox probe timed out after 15 seconds"
        except Exception as exc:
            cleanup_markers()
            return False, f"Codex Git metadata sandbox probe could not start ({type(exc).__name__})"
        except BaseException:
            # An interrupted cycle still removes the markers this probe owns.
            cleanup_markers()
            raise
        verified = False
        if result.returncode == 0:
            try:
                verified = all(
                    marker.is_file() and marker.read_text() == token
                    for marker, token in zip(write_markers, tokens)
                )
            except OSError:
                verified = False
        if not cleanup_markers():
            return False, "Codex Git metadata write probe could not remove its temporary markers"
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")[:1000]
            suffix = f": {detail}" if detail else ""
            return False, f"Codex Git metadata sandbox probe exited {result.returncode}{suffix}"
        if not verified:
            return False, "Codex sandbox could not verify every linked-worktree Git metadata root"
        return True, "Codex sandbox wrote and verified each linked-worktree Git metadata root"

    @staticmethod
    def _publish_filesystem_rules(
        writable_dirs: tuple[str, ...], *, cwd: str | None = None,
    ) -> str:
        """Serialize a read-only common root, its narrow write grants, and carve-outs.

        Every rule names a path that exists when the profile is built. Codex's
        Linux sandbox mounts each rule on its destination and creates a missing
        one as an empty file, which outlives the dispatch: an empty `commondir`
        turns a checkout into a broken linked worktree (#229). The read-only
        common root protects absent files without naming them.
        """
        if not writable_dirs:
            return "{}"
        common = Path(writable_dirs[0])
        entries = [(common, "read")]
        entries.extend(
            (Path(root), "write") for root in CodexAdapter.git_write_roots(writable_dirs)
        )
        if len(writable_dirs) > 1:
            worktree = Path(writable_dirs[-1])
            entries.extend(
                (worktree / name, "read")
                for name in (*CodexAdapter.WORKTREE_POINTERS, *CodexAdapter.WORKTREE_CONFIG)
            )
            if cwd and Path(cwd) / ".git" != common:
                # Keep the workspace's Git indirection immutable so a later
                # dispatch cannot redirect these grants to another repository.
                # It must stay readable: Git opens it to find the gitdir.
                entries.append((Path(cwd) / ".git", "read"))
            entries.extend(
                (common / name, "read") for name in CodexAdapter.MAIN_WORKTREE_WRITABLE_STATE
            )
        # A main worktree keeps its index and HEAD in the read-only common
        # root, so its publishing commands fail closed; the probe routes it away.
        return "{" + ",".join(
            f"{json.dumps(str(path))}={json.dumps(access)}"
            for path, access in entries
            if path.exists()
        ) + "}"

    def argv(self, target: Target, task: str, cwd: str | None = None,
             writes: bool = False, publishes: bool = False,
             publication_permissions: tuple[str, ...] = (),
             writable_dirs: tuple[str, ...] = (),
             sandbox_grants: tuple[str, ...] = (),
             ignore_rules: bool = False,
             review_clone: str | None = None,
             network_domains: tuple[str, ...] | None = None) -> list[str]:
        # `codex exec` is read-only unless told otherwise, which is why four
        # canary runs had an implementer that could not implement: it reported
        # BLOCKED on "the read-only workspace" and nothing here had ever asked
        # for anything else. `workspace-write` is the directory this dispatch
        # was given and nothing beyond it; `danger-full-access` stays out of
        # this file entirely.
        argv = [self.binary, "exec", "-m", target.model,
                "-c", f"model_reasoning_effort={target.effort}", "--json"]
        unknown = sorted(set(sandbox_grants) - set(CODEX_GRANT_OPTIONS))
        if unknown:
            raise ExecutorError(f"Codex cannot apply sandbox grants: {', '.join(unknown)}")
        # Only a person grants this (`grants.py`). Codex's Linux sandbox blocks
        # every new AF_UNIX socket unless all are allowed, through its network
        # proxy; a filesystem rule on the socket path makes the helper fail to
        # start (#239). The proxy keeps the domain allowlist, or none at all.
        unix_sockets = "unix_sockets" in {CODEX_GRANT_OPTIONS[grant] for grant in sandbox_grants}
        profile = None
        if publishes or network_domains or unix_sockets:
            if publishes:
                profile = "code_cycle_publish_write" if writes else "code_cycle_publish_read"
            elif network_domains:
                profile = "code_cycle_registry_write" if writes else "code_cycle_registry_read"
            else:
                profile = "code_cycle_write" if writes else "code_cycle_docker_read"
            parent = ":workspace" if writes else ":read-only"
            argv += [
                "-c", f'default_permissions="{profile}"',
                "-c", "features.network_proxy=true",
                "-c", f'permissions.{profile}.extends="{parent}"',
                "-c", f"permissions.{profile}.network.enabled=true",
            ]
            if publishes or network_domains:
                domains = tuple(dict.fromkeys(
                    (*(sp.REVIEW_SANDBOX_DOMAINS if publishes else ()),
                     *(network_domains or ()))))
                rules = ",".join(json.dumps(host) + '=\"allow\"' for host in domains)
                argv += ["-c", f"permissions.{profile}.network.domains={{{rules}}}"]
            if unix_sockets:
                argv += ["-c",
                         f"permissions.{profile}.network.dangerously_allow_all_unix_sockets=true"]
        elif review_clone and not writes:
            profile = "code_cycle_review_read"
            argv += ["-c", f'default_permissions="{profile}"',
                     "-c", f'permissions.{profile}.extends=":read-only"']
        else:
            argv += ["-s", "workspace-write" if writes else "read-only"]
        if review_clone and not writes:
            # A reviewer verifies in its disposable clone (#142): the clone and,
            # added with the scratch directory later, its TMPDIR are the only
            # writable paths. The source checkout stays read-only.
            argv += ["-c", f"permissions.{profile}.filesystem="
                           f"{{{json.dumps(review_clone)}=\"write\"}}"]
        if cwd:
            argv += ["-C", cwd]
        if writes:
            # The common root itself is never a writable root (#229).
            for directory in self.git_write_roots(writable_dirs):
                argv += ["--add-dir", directory]
            if profile is not None and writable_dirs:
                argv += [
                    "-c",
                    f"permissions.{profile}.filesystem="
                    f"{self._publish_filesystem_rules(writable_dirs, cwd=cwd)}",
                ]
        if ignore_rules:
            # Execpolicy rules load only from configuration layers, with no
            # per-run option, so a stage does not inherit the user's
            # `~/.codex/rules`, including their `forbidden` entries (#142).
            argv.append("--ignore-rules")
        return argv + [task]

    def probe_review_confinement(self, *, runner=None) -> tuple[bool, str]:
        """Show, with the installed CLI, that a review profile confines writes.

        A profile extending `:read-only` with a clone and a TMPDIR writable must
        let a command write those two and nothing else: not a sibling
        `code-cycle-tmp-*`, not the clone's parent through `..`, and not a
        symlink out of the clone. The result is kept for this adapter's life.
        """
        cached = getattr(self, "_review_confinement", None)
        if cached is not None and runner is None:
            return cached
        base = Path(tempfile.mkdtemp(prefix="code-cycle-confinement-"))
        try:
            clone = base / "clone"
            clone.mkdir()
            scratch = Path(tempfile.mkdtemp(prefix="code-cycle-tmp-"))
            sibling = Path(tempfile.mkdtemp(prefix="code-cycle-tmp-"))
            try:
                checks = [("clone", clone / "written", True),
                          ("TMPDIR", scratch / "written", True),
                          ("another code-cycle-tmp directory", sibling / "denied", False),
                          ("the clone's parent through ..", clone / ".." / "denied", False)]
                if os.name != "nt":
                    (clone / "escape").symlink_to(sibling, target_is_directory=True)
                    checks.append(("a symlink out of the clone", clone / "escape" / "link", False))
                script = "\n".join([
                    "import sys",
                    "failed = []",
                    *(f"try:\n open({json.dumps(str(path))}, 'w').close(); ok = True\n"
                      f"except OSError:\n ok = False\n"
                      f"if ok != {expected}: failed.append({json.dumps(label)})"
                      for label, path, expected in checks),
                    "sys.exit('; '.join(failed) if failed else 0)",
                ])
                profile = "code_cycle_review_probe"
                filesystem = (f"{{{json.dumps(str(clone))}=\"write\","
                              f"{json.dumps(str(scratch))}=\"write\"}}")
                argv = [self.binary, "sandbox", "--permission-profile", profile,
                        "-c", f'permissions.{profile}.extends=":read-only"',
                        "-c", f"permissions.{profile}.filesystem={filesystem}",
                        "--", sys.executable, "-c", script]
                result = (runner or _run_sandbox_probe)(
                    argv, cwd=str(clone), capture_output=True, text=True, timeout=30,
                    stdin=subprocess.DEVNULL,
                )
                leaked = [label for label, path, expected in checks
                          if not expected and Path(os.path.normpath(path)).exists()]
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
                shutil.rmtree(sibling, ignore_errors=True)
        except subprocess.TimeoutExpired:
            outcome = (False, "the Codex review-sandbox probe timed out")
        except Exception as error:
            outcome = (False, f"the Codex review-sandbox probe could not run ({type(error).__name__})")
        else:
            if result.returncode != 0 or leaked:
                detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")
                outcome = (False, "the installed Codex sandbox did not confine a review "
                                  f"profile to its clone and TMPDIR: "
                                  f"{', '.join(leaked) or failure_excerpt(detail, 300)}")
            else:
                outcome = (True, "the installed Codex sandbox confined writes to the clone "
                                 "and TMPDIR")
        finally:
            shutil.rmtree(base, ignore_errors=True)
        if runner is None:
            self._review_confinement = outcome
        return outcome

    def dispatch_environment(self, *, writes: bool, publishes: bool,
                             writable_dirs: tuple[str, ...]) -> dict | None:
        # Under the read-only common root, auto-gc and maintenance could only
        # fail; Codex passes this variable through to the agent's commands.
        if writes and publishes and writable_dirs:
            return _sandbox_git_environment()
        return None

    def agent_output(self, stdout: str) -> str:
        """Codex prints NDJSON; the reply is the text of its agent messages.

        Observed shape, from a live run:

            {"type":"item.completed","item":{"type":"agent_message","text":"..."}}

        When no agent message is there at all, the whole output is returned
        unchanged rather than some filtered part of it. Returning only the lines
        this method recognised would quietly drop whatever it did not — and an
        executor that fell out of its own format still said something.
        """
        spoken = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            item = payload.get("item") if isinstance(payload, dict) else None
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    spoken.append(text)
        return "\n".join(spoken) if spoken else stdout

    def auth_evidence(self) -> tuple[bool, str]:
        auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
        if auth.is_file():
            return True, "credential file present"
        return False, f"no credential at {auth}"


_GIT_PATH_OVERRIDES = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def _git_read(cwd: str, *args: str, timeout: int = 10) -> tuple[int, str]:
    """Run a read-only Git query for `cwd`, ignoring a caller's path overrides."""
    environment = os.environ.copy()
    for name in _GIT_PATH_OVERRIDES:
        environment.pop(name, None)
    completed = subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=timeout, env=environment,
    )
    return completed.returncode, completed.stdout.strip()


def _sandbox_git_environment(base: dict | None = None) -> dict:
    """Disable Git's automatic maintenance for commands inside the sandbox."""
    environment = dict(os.environ if base is None else base)
    parameters = " ".join(
        f"'{key}={value}'" for key, value in CodexAdapter.SANDBOX_GIT_CONFIG
    )
    inherited = environment.get("GIT_CONFIG_PARAMETERS", "").strip()
    environment["GIT_CONFIG_PARAMETERS"] = (
        f"{inherited} {parameters}" if inherited else parameters
    )
    return environment


def _git_output(cwd: str, *args: str, timeout: int = 10) -> str:
    completed = subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=timeout,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "git command failed").strip()
        raise ExecutorError(detail)
    return completed.stdout.rstrip("\n")


def _remote_ref_fingerprint(root: str) -> str:
    """Fingerprint every advertised ref at each configured fetch/push URL."""
    snapshot = []
    for remote in _git_output(root, "remote").splitlines():
        urls = set()
        for args in (("remote", "get-url", "--all", remote),
                     ("remote", "get-url", "--push", "--all", remote)):
            urls.update(filter(None, _git_output(root, *args).splitlines()))
        for url in sorted(urls):
            try:
                refs = _git_output(root, "ls-remote", "--refs", url)
            except Exception as exc:
                raise ExecutorError(
                    f"could not inspect configured remote refs ({type(exc).__name__})"
                ) from None
            snapshot.extend((remote, url, line) for line in refs.splitlines())
    encoded = json.dumps(sorted(snapshot), separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _checkout_fingerprint(path: str) -> dict:
    """Exhaustive checkout evidence for the read-only contract."""
    return _workspace_fingerprint(path)


def _workspace_fingerprint(path: str, *, max_files: int | None = None,
                           max_bytes: int | None = None) -> dict | None:
    """What a read-only stage must leave exactly as it found it.

    HEAD, the branch ref, the porcelain status including every untracked file,
    the staged blob IDs, and a digest of each changed or untracked file's
    bytes. The status alone would miss a second edit to a file that was already
    dirty, and a read-only stage is not required to start from a clean tree.
    Ignored files are left out because nobody commits them; file contents are
    digested and never kept.

    Optional budgets are used only for best-effort start evidence. Exceeding
    one returns None. Read-only contract callers omit them and remain exhaustive.
    """
    root = _git_output(path, "rev-parse", "--show-toplevel")
    head = _git_output(root, "rev-parse", "HEAD")
    branch = _git_output(root, "rev-parse", "--symbolic-full-name", "HEAD")
    status = _git_output(root, "status", "--porcelain", "--untracked-files=all")
    staged = _git_output(root, "diff", "--cached", "--raw", "--no-abbrev", "--no-renames")
    changed = _git_output(
        root, "ls-files", "-z", "--modified", "--others", "--exclude-standard",
    )
    paths = sorted(set(filter(None, changed.split("\0"))))
    if max_files is not None and len(paths) > max_files:
        return None
    files = []
    remaining = max_bytes
    for relative in paths:
        full = os.path.join(root, relative)
        if os.path.islink(full):
            files.append((relative, "link", os.readlink(full)))
        elif os.path.isfile(full):
            digest = hashlib.sha256()
            with open(full, "rb") as handle:
                while True:
                    size = 1 << 20 if remaining is None else min(1 << 20, remaining + 1)
                    chunk = handle.read(size)
                    if not chunk:
                        break
                    if remaining is not None:
                        remaining -= len(chunk)
                        if remaining < 0:
                            return None
                    digest.update(chunk)
            files.append((relative, "file", digest.hexdigest()))
        else:
            files.append((relative, "absent", ""))
    encoded = json.dumps([head, branch, status, staged, files], separators=(",", ":"))
    return {"root": root, "head": head, "branch": branch,
            "fingerprint": hashlib.sha256(encoded.encode("utf-8")).hexdigest()}


def _verified_read_only(cwd: str | None, call) -> DispatchResult | str:
    """Run a read-only stage and prove afterwards that its checkout is unchanged.

    The contract is about the result, not the cause: a write by the agent's
    shell, an MCP server, a plugin, a hook or anything they spawn changes the
    same repository state. So this takes no executor, tool or product, and an
    adapter's sandbox is defence in depth, never a reason to skip it. It fails
    closed: a checkout that cannot be fingerprinted before the stage returns a
    reason instead of a result, and one that cannot be fingerprinted after it
    is a violation.

    Only a stage that succeeded and finished its work can be `verified`. One
    that started work elsewhere, or ended any other way, such as a timeout
    that can leave the worker or its children running, still fails on an
    observed change, but an unchanged checkout then proves nothing about what
    may still write to it. Such a result is marked `pending` until
    `verify_read_only_completion` compares again once the work is known to
    have stopped.
    """
    checkout = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    try:
        before = _checkout_fingerprint(checkout)
    except Exception as exc:
        return f"could not fingerprint the read-only checkout {checkout}: {exc}"
    result = call()
    artifacts = dict(result.artifacts)
    artifacts["workspace_head_before"] = before["head"]
    artifacts["workspace_fingerprint_before"] = before["fingerprint"]
    finished = (result.outcome is DispatchOutcome.SUCCEEDED
                and not result.asynchronous)
    if not finished:
        try:
            returned = _checkout_fingerprint(checkout)
        except Exception as exc:
            return _read_only_violation(
                result, artifacts,
                f"could not verify the read-only checkout after the dispatch: {exc}")
        if returned != before:
            artifacts["workspace_head_after"] = returned["head"]
            artifacts["workspace_fingerprint_after"] = returned["fingerprint"]
            return _read_only_violation(
                result, artifacts,
                "the branch, HEAD, index, or working tree changed during a "
                "read-only stage")
        artifacts.pop("read_only_mode", None)
        artifacts["read_only_verification"] = "pending"
        return replace(result, artifacts=artifacts)
    return _compare_read_only(result, artifacts, checkout, before["fingerprint"])


def verify_read_only_completion(result: DispatchResult, cwd: str | None) -> DispatchResult:
    """Finish the check that an asynchronous read-only stage left pending.

    Whoever observes the started work finish calls this with the same
    checkout. Anything but a pending result is returned unchanged.
    """
    artifacts = dict(result.artifacts)
    if artifacts.get("read_only_verification") != "pending":
        return result
    checkout = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    return _compare_read_only(result, artifacts, checkout,
                              artifacts.get("workspace_fingerprint_before"))


def _compare_read_only(result: DispatchResult, artifacts: dict, checkout: str,
                       before: str | None) -> DispatchResult:
    try:
        after = _checkout_fingerprint(checkout)
    except Exception as exc:
        return _read_only_violation(
            result, artifacts,
            f"could not verify the read-only checkout after the stage: {exc}")
    artifacts["workspace_head_after"] = after["head"]
    artifacts["workspace_fingerprint_after"] = after["fingerprint"]
    if before is not None and after["fingerprint"] == before:
        artifacts["read_only_verification"] = "verified"
        return replace(result, artifacts=artifacts)
    return _read_only_violation(
        result, artifacts,
        "the branch, HEAD, index, or working tree changed during a read-only stage")


def _read_only_violation(result: DispatchResult, artifacts: dict,
                         problem: str) -> DispatchResult:
    artifacts.pop("read_only_mode", None)
    artifacts.pop("read_only_verification", None)
    if result.outcome is DispatchOutcome.CONTRACT_VIOLATION and result.detail:
        detail = f"{result.detail}; {problem}"
    else:
        detail = "read-only contract violation: " + problem
    return replace(result, outcome=DispatchOutcome.CONTRACT_VIOLATION,
                   missing_capability=None, detail=detail, artifacts=artifacts,
                   error_code="contract_violation", start_state="started")


def _remove_read_only_path(function, path, exc_info, root=None) -> None:
    # Removal needs a writable parent, and on Windows a writable entry. chmod
    # follows links, and a reviewer can point one anywhere, so a link is never
    # chmodded, and a parent is made writable only inside `root`, the
    # workspace being removed; rmtree does not descend through links.
    try:
        parent = os.path.dirname(path)
        if (root is not None and parent and not os.path.islink(parent)
                and os.path.commonpath([os.path.abspath(root), os.path.abspath(parent)])
                == os.path.abspath(root)):
            os.chmod(parent, 0o700)
        if not os.path.islink(path):
            os.chmod(path, 0o700)
        function(path)
    except OSError:
        raise exc_info[1]


def _remove_workspace(path: str) -> str | None:
    """Remove a disposable directory; return what is left on disk, or None."""
    try:
        shutil.rmtree(path, onerror=lambda function, failed, exc_info:
                      _remove_read_only_path(function, failed, exc_info, root=path))
    except FileNotFoundError:
        return None
    except Exception as exc:
        shutil.rmtree(path, ignore_errors=True)
        if Path(path).exists():
            return f"{path} ({type(exc).__name__})"
        return None
    if Path(path).exists():
        return path
    return None


def _isolated_review_dispatch(adapter, target, task, *, cwd, dispatch_call,
                              read_only_mode: str = "detected",
                              command_policy: bool = False):
    """Run a reviewer in an independent detached clone and verify its source checkout.

    `read_only_mode` is what the clone adds to the executor's own guarantee:
    `detected` for Claude, whose writes are found afterwards, and `enforced`
    for a Codex sandbox that keeps the source checkout read-only.
    `command_policy` says whether the stage's tools are already narrowed to
    its command table (#142), which changes what the prompt can promise.
    """
    if not isinstance(cwd, str) or not cwd:
        return adapter._blocked(
            target, "review_workspace_isolation",
            f"a {adapter.name} isolated review needs an implementer Git checkout to verify",
        )
    worktree = None
    root = None
    before_head = before_status = before_branch = before_remote_refs = None
    try:
        root = _git_output(cwd, "rev-parse", "--show-toplevel")
        before_head = _git_output(root, "rev-parse", "HEAD")
        before_branch = _git_output(root, "rev-parse", "--symbolic-full-name", "HEAD")
        before_status = _git_output(
            root, "status", "--porcelain", "--untracked-files=all",
        )
        if before_status:
            raise ExecutorError(
                "the implementer checkout must be clean before an isolated review"
            )
        before_remote_refs = _remote_ref_fingerprint(root)
        worktree = tempfile.mkdtemp(prefix="code-cycle-review-")
        Path(worktree).rmdir()
        if _paths_overlap(worktree, root):
            raise ExecutorError("the disposable review workspace overlaps the implementer checkout")
        _git_output(
            root, "clone", "--no-hardlinks", "--no-checkout", root, worktree,
            timeout=300,
        )
        for remote in _git_output(worktree, "remote").splitlines():
            _git_output(worktree, "remote", "remove", remote)
        _git_output(worktree, "checkout", "--detach", before_head)
        review_head = _git_output(worktree, "rev-parse", "HEAD")
        if review_head != before_head:
            raise ExecutorError("the disposable clone is not at the reviewed HEAD")
    except Exception as exc:
        left = _remove_workspace(worktree) if worktree else None
        detail = f"could not prepare an isolated review workspace: {exc}"
        if left:
            detail += f"; cleanup failed, left on disk: {left}"
        return adapter._blocked(
            target, "review_workspace_isolation", detail,
        )

    access = (
        "Your commands are limited to this stage's table: reading, running the "
        "configured verification inside this clone, and publishing the comment "
        "this stage allows."
        if command_policy else
        "You keep your normal gh and git access; use it only to read and to "
        "publish the comment this stage allows."
    )
    prompt = (
        f"{task}\n\nREAD-ONLY HARNESS CONTRACT: Do not modify files, commit, or push. "
        "This stage runs in an independent disposable clone without remotes; any "
        f"local edits will be discarded. {access} A push, merge, or edit to the "
        "change request cannot be undone by the harness and invalidates the "
        "review it is judging. Any change to the implementer's branch or "
        "working tree fails this stage."
    )
    result = None
    workspace_changed = False
    source_error = "the implementer branch, HEAD, working tree, or configured remote refs changed during review"
    left = None
    after_head = after_status_fingerprint = after_remote_refs = None
    try:
        result = dispatch_call(prompt, worktree)
        try:
            after_head = _git_output(root, "rev-parse", "HEAD")
            after_branch = _git_output(root, "rev-parse", "--symbolic-full-name", "HEAD")
            after_status = _git_output(
                root, "status", "--porcelain", "--untracked-files=all",
            )
            after_status_fingerprint = hashlib.sha256(after_status.encode("utf-8")).hexdigest()
            after_remote_refs = _remote_ref_fingerprint(root)
            source_changed = (
                after_head != before_head or after_branch != before_branch
                or after_status != before_status
                or after_remote_refs != before_remote_refs
            )
        except Exception as exc:
            source_changed = True
            source_error = f"could not verify the implementer checkout after review: {exc}"
        try:
            workspace_head = _git_output(worktree, "rev-parse", "HEAD")
            workspace_status = _git_output(
                worktree, "status", "--porcelain", "--untracked-files=all",
            )
            workspace_changed = workspace_head != review_head or bool(workspace_status)
        except Exception:
            workspace_changed = True
    finally:
        left = _remove_workspace(worktree)

    warnings = []
    if workspace_changed:
        warnings.append("the reviewer changed its disposable workspace; those edits were discarded")
    artifacts = dict(result.artifacts)
    artifacts["read_only_mode"] = read_only_mode
    artifacts["implementer_head_before"] = before_head
    artifacts["implementer_status_fingerprint_before"] = hashlib.sha256(
        before_status.encode("utf-8"),
    ).hexdigest()
    artifacts["remote_refs_fingerprint_before"] = before_remote_refs
    if after_head is not None:
        artifacts["implementer_head_after"] = after_head
    if after_status_fingerprint is not None:
        artifacts["implementer_status_fingerprint_after"] = after_status_fingerprint
    if after_remote_refs is not None:
        artifacts["remote_refs_fingerprint_after"] = after_remote_refs
    if warnings:
        artifacts["warnings"] = warnings
    cleanup_error = (
        f"the disposable review workspace could not be removed; left on disk: {left}"
        if left else None
    )
    if source_changed:
        artifacts.pop("read_only_mode", None)
        detail = "read-only contract violation: " + source_error
        if cleanup_error:
            detail += "; " + cleanup_error
        return replace(
            result, outcome=DispatchOutcome.CONTRACT_VIOLATION,
            missing_capability=None, detail=detail, artifacts=artifacts,
        )
    if cleanup_error:
        # A workspace that outlives its stage is not disposable, so the stage
        # does not report its read-only mode. Only a success is downgraded: a
        # failure the run already reported, such as model drift, stays.
        artifacts.pop("read_only_mode", None)
        outcome = (DispatchOutcome.FAILED
                   if result.outcome is DispatchOutcome.SUCCEEDED else result.outcome)
        detail = cleanup_error if not result.detail else f"{result.detail}; {cleanup_error}"
        return replace(result, outcome=outcome, detail=detail, artifacts=artifacts)
    detail = result.detail
    if warnings:
        detail = detail + ("; " if detail else "") + "warning: " + "; ".join(warnings)
    return replace(result, detail=detail, artifacts=artifacts)


class ClaudeAdapter(NativeAdapter):
    name = "claude"
    binary = "claude"
    quota_markers = (
        "usage limit",
        "rate limit",
        "out of credits",
        "you've hit your session limit",
        "you've hit your weekly limit",
        "you've hit your opus limit",
        "you've hit your sonnet limit",
    )
    interactive_markers = {
        "bypass permissions mode": "bypass_acknowledgement",
        "is this a project you created or one you trust": "folder_trust",
        "log in": "authenticated_session",
    }

    def publication_access(self, probe: ProbeResult, *, writes: bool) -> tuple[bool, str]:
        return True, ("Claude uses gh for comments and git for writing stages; on a "
                      "GitHub cycle the stage's command table and hook bound them")

    def supports_non_writing(self, **dispatch_kwargs) -> bool:
        cwd = dispatch_kwargs.get("cwd")
        return isinstance(cwd, str) and bool(cwd) and Path(cwd).is_dir()

    def dispatch(self, target: Target, task: str, *, cwd: str | None = None,
                 timeout: int = DEFAULT_DISPATCH_TIMEOUT_SECONDS, runner=_run, writes: bool = False,
                 publishes: bool = False,
                 publication_permissions: tuple[str, ...] = (),
                 claude_allowed_commands: tuple[str, ...] = (),
                 read_dirs: tuple[str, ...] = (),
                 command_policy: StagePolicy | None = None,
                 on_progress=None, on_workspace=None,
                 dispatch_attempt_id: str | None = None,
                 docker_probe_runner=None) -> DispatchResult:
        return super().dispatch(
            target, task, cwd=cwd, timeout=timeout, runner=runner,
            writes=writes, publishes=publishes,
            publication_permissions=publication_permissions,
            claude_allowed_commands=claude_allowed_commands,
            read_dirs=read_dirs, command_policy=command_policy,
            on_progress=on_progress,
            on_workspace=on_workspace, dispatch_attempt_id=dispatch_attempt_id,
            docker_probe_runner=docker_probe_runner,
        )

    def policy_preflight(self, policy: StagePolicy | None, cwd: str | None) -> str | None:
        """Refuse a dispatch whose loaded configuration could override the hook."""
        if policy is None:
            return None
        try:
            reason = sp.hook_override()
            if reason:
                return f"the stage command hook would not run: {reason}"
            mods = sp.tool_check_mods()
            if mods:
                return ("the loaded Claude configuration enables a tool.check mod that "
                        "could approve commands the stage policy denies: "
                        f"{', '.join(mods)}. Claude Code offers no flag that turns mods "
                        "off for one dispatch while keeping the user's other hooks, so "
                        "disable that plugin for cycle runs")
            if policy.requires_confined_verification:
                reason = sp.sandbox_override()
                if reason:
                    return reason
                # The clone's project settings come from the change under review.
                for name in ("settings.json", "settings.local.json"):
                    project = Path(cwd or ".") / ".claude" / name
                    reason = sp.sandbox_override(project) if project.is_file() else None
                    if reason:
                        return reason
        except sp.PolicyError as error:
            return f"the Claude configuration could not be checked: {error}"
        return None

    def policy_notice(self, policy: StagePolicy | None, cwd: str | None) -> dict:
        """The non-Bash tools the user's configuration pre-approves for this stage.

        The table and hook govern the stage's Bash commands and file edits; an
        MCP tool the user allows runs under the user's own configuration. The
        cycle names such tools once and keeps running (#142).
        """
        if policy is None:
            return {}
        tools = sp.preapproved_outside_table(cwd)
        return {"preapproved_tools": tools} if tools else {}

    @contextmanager
    def policy_files(self, argv: list[str], policy: StagePolicy | None, *,
                     scratch: str, cwd: str):
        """Write the hook and its policy outside the workspace and TMPDIR.

        The directory is the runtime's: read-only to the agent's tools, gone
        when the dispatch ends, and never inside a path the stage may write.
        """
        if policy is None:
            yield argv
            return
        directory = Path(tempfile.mkdtemp(prefix="code-cycle-policy-",
                                          dir=str(Path(scratch).parent)))
        try:
            if _paths_overlap(str(directory), cwd) or _paths_overlap(str(directory), scratch):
                raise ExecutorError("the stage policy directory overlaps a writable path")
            try:
                code, _ = _git_read(cwd, "remote", "get-url", "origin")
            except (OSError, subprocess.SubprocessError):
                code = 1
            runtime_policy = policy.at_dispatch(
                tmpdir=scratch, rtk=shutil.which("rtk") is not None,
                fetch_origin=code == 0, docker=policy.docker,
            )
            script = directory / "stage_permissions.py"
            shutil.copyfile(Path(__file__).with_name("stage_permissions.py"), script)
            # The hook records its denials with this module beside it (#306).
            journal_module = directory / "cycle_journal.py"
            shutil.copyfile(Path(__file__).with_name("cycle_journal.py"), journal_module)
            policy_file = directory / "policy.json"
            policy_file.write_text(runtime_policy.to_json(), encoding="utf-8")
            parts = [sys.executable, str(script), "hook", str(policy_file)]
            command = (subprocess.list2cmdline(parts) if os.name == "nt"
                       else " ".join(shlex.quote(part) for part in parts))
            settings = sp.hook_settings(command, bash_only=not runtime_policy.active)
            settings.setdefault("env", {}).update({
                "BASH_DEFAULT_TIMEOUT_MS": str(sp.CLAUDE_VERIFICATION_TIMEOUT_MS),
                "BASH_MAX_TIMEOUT_MS": str(sp.CLAUDE_VERIFICATION_TIMEOUT_MS),
            })
            if runtime_policy.active and not runtime_policy.static_only and (runtime_policy.requires_confined_verification
                                          or runtime_policy.registry_domains or runtime_policy.publication_commands
                                          or runtime_policy.docker):
                writable = [cwd, scratch]
                if runtime_policy.role in sp.WRITING_ROLES:
                    git_directories = []
                    for option in ("--git-common-dir", "--git-dir"):
                        status, path = _git_read(cwd, "rev-parse", option)
                        if status == 0:
                            git_directories.append(str((Path(cwd) / path).resolve()))
                    writable.extend(CodexAdapter.git_write_roots(tuple(dict.fromkeys(git_directories))))
                settings.update(sp.review_sandbox_settings(
                    tuple(writable), runtime_policy.registry_domains,
                    docker_host=runtime_policy.docker))
            if os.name != "nt":
                for path in (script, journal_module, policy_file):
                    os.chmod(path, 0o400)
                os.chmod(directory, 0o500)
            if runtime_policy.active:
                argv = _with_allowed_tools(argv, sp.allowed_tool_patterns(runtime_policy))
            index = argv.index("--verbose") + 1
            yield argv[:index] + ["--settings", json.dumps(settings)] + argv[index:]
        finally:
            if os.name != "nt":
                try:
                    os.chmod(directory, 0o700)
                except OSError:
                    pass
            shutil.rmtree(directory, ignore_errors=True)

    def extra_artifacts(self, stdout: str) -> dict:
        """The Bash commands the permission layer or the stage hook denied.

        Claude's final `result` event lists the permission layer's in
        `permission_denials`. A hook denial reaches the agent as an error
        `tool_result` carrying the hook's reason, so those are matched to the
        Bash call they answer. The cycle reports each command so a person can
        authorize it once (#142).
        """
        denied: list[str] = []
        sandbox_startup_failures: list[dict[str, str]] = []
        # The permission layer's own list, every tool, for the cycle journal
        # (#306). The hook records its denials itself, so it is kept apart.
        layer: list[dict[str, object]] = []

        def add(command) -> None:
            if isinstance(command, str) and command.strip():
                text = failure_excerpt(command, 300)
                if text not in denied:
                    denied.append(text)

        bash_calls: dict[str, str] = {}
        for event in _events(stdout):
            kind = event.get("type")
            if kind == "result":
                for denial in event.get("permission_denials") or []:
                    if not isinstance(denial, dict):
                        continue
                    tool = denial.get("tool_name")
                    tool_input = denial.get("tool_input")
                    tool_input = tool_input if isinstance(tool_input, dict) else {}
                    if tool == "Bash":
                        add(tool_input.get("command"))
                    target = (tool_input.get("command") or tool_input.get("file_path")
                              or tool_input.get("notebook_path") or tool_input.get("url"))
                    entry: dict[str, object] = {"tool": str(tool or "unknown")[:80],
                             "command": failure_excerpt(target, 300) if isinstance(target, str) else None,
                             "tool_use_id": denial.get("tool_use_id")
                             if isinstance(denial.get("tool_use_id"), str) else None}
                    if entry not in layer:
                        layer.append(entry)
                continue
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if (kind == "assistant" and block.get("type") == "tool_use"
                        and block.get("name") == "Bash"
                        and isinstance(block.get("input"), dict)):
                    bash_calls[str(block.get("id"))] = block["input"].get("command")
                elif (kind == "user" and block.get("type") == "tool_result"
                        and block.get("is_error")):
                    tool_use_id = str(block.get("tool_use_id"))
                    tool_output = _tool_result_text(block.get("content"))
                    command = bash_calls.get(tool_use_id)
                    if sp.HOOK_DENIAL_MARKER in tool_output:
                        add(command)
                    # Only inspect an error result tied to a real Bash tool call.
                    # Text in files, comments, assistant narration, and historical
                    # stream events is not evidence that the sandbox failed to start.
                    missing_source = re.search(
                        r"bwrap: Can't find source path (.+?): No such file or directory",
                        tool_output,
                    )
                    if command and missing_source:
                        failure = {
                            "capability": "sandbox_startup_failure",
                            "path": missing_source.group(1),
                            "cause": (
                                "Claude Code's Bash sandbox protects the path, but bubblewrap "
                                "cannot bind a source that does not exist"
                            ),
                            "limitation": (
                                "Claude Code sandbox settings do not provide a supported "
                                "runtime-owned bind mount for this protected path"
                            ),
                            "command": failure_excerpt(command, 300),
                        }
                        if failure not in sandbox_startup_failures:
                            sandbox_startup_failures.append(failure)
        artifacts: dict[str, object] = {"permission_denials": denied} if denied else {}
        if layer:
            artifacts["claude_permission_denials"] = layer
        if sandbox_startup_failures:
            artifacts["sandbox_startup_failures"] = sandbox_startup_failures
        pending = self.pending_background_evidence(stdout)
        if pending:
            artifacts["background_command"] = pending
        return artifacts

    #: A final message that says the stage is waiting on something it started.
    _WAITING_TEXT = re.compile(
        r"still running|in the background|runs? in (the )?background|"
        r"\bwaiting (on|for)\b|when it finishes|once it finishes", re.IGNORECASE)

    def pending_background_evidence(self, stdout: str) -> str | None:
        """`confirmed` or `suspected` when the stage exited with a command pending (#276).

        Confirmed needs the execution events: a Bash call made with
        `run_in_background` that no hook denial answered. Suspected rests on the
        final message alone, which says it is waiting. Neither is reported
        without such evidence.
        """
        calls: dict[str, bool] = {}
        for event in _events(stdout):
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if (event.get("type") == "assistant" and block.get("type") == "tool_use"
                        and block.get("name") == "Bash"
                        and isinstance(block.get("input"), dict)
                        and block["input"].get("run_in_background") is True):
                    calls[str(block.get("id"))] = True
                elif (event.get("type") == "user" and block.get("type") == "tool_result"
                        and block.get("is_error")
                        and sp.HOOK_DENIAL_MARKER in _tool_result_text(block.get("content"))):
                    calls.pop(str(block.get("tool_use_id")), None)
        if calls:
            return "confirmed"
        if self._WAITING_TEXT.search(self.agent_output(stdout) or ""):
            return "suspected"
        return None

    def environment_restriction(self, message: str) -> str | None:
        lowered = message.lower()
        if any(marker in lowered for marker in sp.SANDBOX_UNAVAILABLE_MARKERS):
            return ("BLOCKED: Claude's Bash sandbox is unavailable, so this review "
                    "cannot run its verification confined to the clone and TMPDIR. "
                    "On Linux and WSL2 it needs bubblewrap and socat "
                    "(sudo apt install bubblewrap socat); see "
                    "docs/host-permissions.md: " + message)
        return None

    def readable(self, argv: list[str], read_dirs: tuple[str, ...]) -> list[str]:
        # A non-interactive Claude reads only inside its working directory
        # without asking, and nobody is there to answer. `--add-dir` is the
        # documented way to grant another one, for reading and editing alike,
        # which is why `dispatch()` never passes one to a writing stage. It
        # takes several values, so it goes last, where nothing positional
        # follows it.
        return argv + ["--add-dir", *read_dirs] if read_dirs else argv

    def argv(self, target: Target, task: str, cwd: str | None = None,
             writes: bool = False, publishes: bool = False,
             publication_permissions: tuple[str, ...] = (),
             allowed_commands: tuple[str, ...] = (),
             command_policy: StagePolicy | None = None) -> list[str]:
        # No bypass flag. A run that needs elevated permissions to proceed is a
        # run a human should be looking at.
        #
        # This CLI is permissive where Codex is restrictive: a dispatched Claude
        # already edits files with no flag at all, observed on a live run. So
        # `acceptEdits` declares what a writing stage is doing rather than
        # granting it something new.
        #
        # There is no flag that confines a non-writing stage here, and this
        # does not pretend otherwise. `--permission-mode plan` refuses the edit
        # but turns the task into planning it, which is not a review; and
        # disallowing Edit, Write and NotebookEdit does not stop a write, as a
        # live probe confirmed — the file was created anyway. A reviewer is
        # confined by the directory it is given, not by an argument.
        #
        # `cwd` is absent here on purpose: this CLI has no directory flag, so the
        # working directory is set on the process itself. A multi-repository
        # dispatcher that silently ran in the coordinator's directory would
        # implement the wrong repository without saying so.
        argv = [self.binary, "-p", task, "--model", target.model,
                "--effort", target.effort, "--output-format", "stream-json", "--verbose"]
        if writes:
            argv += ["--permission-mode", "acceptEdits"]
        if command_policy is not None and command_policy.active:
            # A GitHub cycle (#142): the stage's closed table is the first
            # barrier, and the hook `policy_files` adds validates every
            # argument behind these prefixes. A reviewer writes only its helper
            # files in $TMPDIR, through the `Edit` rules the table grants: an
            # explicit `default` mode keeps the user's `defaultMode` (`auto`,
            # `acceptEdits`) from approving an edit to `.git/config` that an
            # allowed `git status` would then run, and the hook refuses one too.
            if not writes:
                argv += ["--permission-mode", "default"]
            return argv + ["--allowedTools", *sp.allowed_tool_patterns(command_policy)]
        # Without a command policy (another code host or issue provider) the
        # earlier contract holds. A reading stage keeps its full tooling: it is
        # told not to write, runs in a disposable clone, and dispatch() detects
        # a change to the implementer's checkout or remote refs. A publishing
        # stage keeps its ordinary GitHub tooling, and which operations it may
        # perform is a behavioural rule in its prompt
        # (`cycle.publication_policy`).
        allowed_tools = ["Bash(gh:*)", "Bash(git:*)"] if publishes else []
        if writes:
            allowed_tools.extend(f"Bash({command})" for command in allowed_commands)
        if allowed_tools:
            argv += ["--allowedTools", *allowed_tools]
        return argv

    def agent_output(self, stdout: str) -> str:
        """Unwrap the final result event from Claude's verbose JSONL stream.

        Observed on a live run alongside `modelUsage`, `usage` and timings. If
        the envelope is not there, the output is returned unchanged: a caller
        with the raw text is better off than one with nothing.
        """
        try:
            payload = json.loads(stdout)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            spoken = payload.get("result")
            if isinstance(spoken, str):
                return spoken
        for line in stdout.splitlines():
            if not line.lstrip().startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                spoken = event.get("result")
                if isinstance(spoken, str):
                    return spoken
        return stdout

    def auth_evidence(self) -> tuple[bool, str]:
        config = Path.home() / ".claude.json"
        if not config.is_file():
            return False, f"no configuration at {config}"
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except Exception as exc:
            return False, f"configuration unreadable: {exc}"
        if data.get("hasCompletedOnboarding"):
            return True, "onboarding completed"
        return False, "onboarding not completed"


@dataclass(frozen=True)
class OrcaDispatchContext:
    """What the orchestration layer must have prepared before an Orca dispatch.

    These are not things a dispatcher may conjure. A Run and a Task are
    durable state in the user's workspace, and a coordinator terminal is a
    process: whoever owns the run creates them deliberately, and the adapter
    only consumes them.

    `task_id` is deliberately separate from the `task` argument every other
    adapter receives. For Codex and Claude that argument is the prompt; for
    Orca the prompt already lives inside the Task, and `worker-start --task`
    wants the Task's identifier. Letting one name mean both would send prose
    where an ID belongs.
    """

    coordinator: str
    run_id: str
    task_id: str
    worker_agent: str | None = None
    review_workspace: OrcaReviewWorkspace | None = None

    def __post_init__(self) -> None:
        for field_name in ("coordinator", "run_id", "task_id"):
            if not getattr(self, field_name):
                raise ExecutorError(f"an Orca dispatch context needs {field_name}")
        if (self.review_workspace is not None
                and not isinstance(self.review_workspace, OrcaReviewWorkspace)):
            raise ExecutorError("an Orca review workspace must use its explicit contract")


class OrcaAdapter(Adapter):
    """The one backend that can demonstrate readiness.

    Optional by design. The toolkit stays usable without it, and nothing here
    makes Orca a dependency of anything else.
    """

    name = "orca"
    def publication_access(self, probe: ProbeResult, *, writes: bool) -> tuple[bool, str]:
        return False, "Orca publication permissions are not exposed to the local readiness check"
    # Orca does not provide an OS-enforced read-only permission. It supports a
    # non-writing review only when the explicit isolated-workspace contract is
    # present; `dispatch()` validates it again for direct callers.
    enforces_read_only = False
    provable_ceiling = Availability.READY
    # A receipt, not a result: `worker-start` returns once the worker is alive,
    # and the stage it is running finishes later and somewhere else.
    completes_work = False

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or ("orca-ide" if os.name != "nt" else "orca")

    #: Which Orca agent launches a given provider. Orca picks the agent, the
    #: target picks the model, and the two have to agree: asking for the Codex
    #: agent with an Anthropic model is a request no worker can satisfy.
    AGENT_FOR_PROVIDER = {"openai": "codex", "anthropic": "claude"}

    def supports_non_writing(self, **dispatch_kwargs) -> bool:
        context = dispatch_kwargs.get("context")
        return (isinstance(context, OrcaDispatchContext)
                and context.review_workspace is not None)

    def dispatch(self, target: Target, task: str, *, cwd: str | None = None,
                 timeout: int = DEFAULT_DISPATCH_TIMEOUT_SECONDS, runner=_run,
                 context: "OrcaDispatchContext | None" = None,
                 writes: bool = False, on_workspace=None) -> DispatchResult:
        """Run the work as a supervised Orca worker.

        Orca is the one backend that reports which model it actually launched,
        in the dispatch receipt's `launch.effective`. That is worth more than it
        looks: everywhere else the resolved model is either self-reported by the
        agent — which was observed to be wrong in both arms of a campaign — or
        simply unavailable.

        A coordinator terminal and a Run are required and are not invented here.
        Creating them is a side effect on the user's workspace, and a dispatcher
        that quietly spawns terminals is one nobody can reason about; without
        them this returns `BLOCKED` naming what to provide.
        """
        if context is None:
            return self._blocked(
                target, "orchestration_context",
                "an Orca dispatch needs a coordinator terminal, a Run and a Task that "
                "already exist; create them deliberately rather than having the "
                "dispatcher spawn them in someone's workspace",
            )
        review_workspace = None
        if not writes:
            review_workspace = context.review_workspace
            if review_workspace is None:
                return self._blocked(
                    target, "review_workspace_isolation",
                    "an Orca review needs an explicit immutable or disposable "
                    "workspace separate from the implementer's workspace",
                )
            if (cwd is None
                    or _canonical_path(cwd)
                    != _canonical_path(review_workspace.implementer_path)):
                return self._blocked(
                    target, "review_workspace_mismatch",
                    "the dispatch cwd must identify the implementer's workspace "
                    "declared by the Orca review contract",
                )
        elif context.review_workspace is not None:
            return self._blocked(
                target, "review_workspace_conflict",
                "a review workspace cannot be supplied to a writing Orca dispatch",
            )
        agent = context.worker_agent or self.AGENT_FOR_PROVIDER.get(target.provider)
        if agent is None:
            return self._blocked(
                target, "provider_agent_mapping",
                f"no Orca agent is known for provider {target.provider!r}; "
                "name one in the dispatch context rather than guessing",
            )
        # `task` here is the Task ID Orca already holds, not the prompt. The
        # prompt went into that Task when it was created; passing prose to
        # --task would silently create work nobody can find again.
        worktree = review_workspace.path if review_workspace is not None else cwd
        worker_cwd = worktree or os.getcwd()
        if on_workspace is not None:
            on_workspace(worker_cwd)
        argv = [self.binary, "orchestration", "worker-start",
                "--from", context.coordinator, "--run", context.run_id,
                "--task", context.task_id, "--agent", agent,
                "--model", target.model, "--effort", target.effort, "--json"]
        if worktree:
            argv += ["--worktree", f"path:{worktree}"]
        try:
            completed = runner(argv, timeout=timeout, cwd=worktree)
            payload = json.loads(completed.stdout or "{}")
        except subprocess.TimeoutExpired:
            return DispatchResult(DispatchOutcome.FAILED, self.name, target,
                                  detail=f"no receipt within {timeout}s; the worker may still be alive",
                                  error_code="timeout", lifecycle_state="timed_out")
        except json.JSONDecodeError:
            return DispatchResult(DispatchOutcome.FAILED, self.name, target,
                                  detail="the executor returned an unreadable receipt",
                                  error_code="unreadable_result")
        except Exception as exc:
            return DispatchResult(DispatchOutcome.FAILED, self.name, target,
                                  detail=failure_excerpt(str(exc)),
                                  error_code="precondition" if isinstance(exc, OSError) else "executor_error",
                                  start_state="not_started" if isinstance(exc, OSError) else "unknown")

        if not isinstance(payload, dict):
            return DispatchResult(DispatchOutcome.FAILED, self.name, target,
                                  detail="the executor returned a non-object receipt",
                                  error_code="unreadable_result")

        if on_workspace is not None:
            on_workspace(worker_cwd)

        if not payload.get("ok") or completed.returncode != 0:
            # The CLI exits 0 only for `ready`; a failed or outcome_unknown
            # launch exits non-zero and still returns a JSON body. Reading the
            # body and ignoring the status reports a partial launch as a
            # success, and the body is exactly where the recovery information
            # lives.
            error = payload.get("error") or {}
            result = payload.get("result") or {}
            detail = (error.get("message") or error.get("code")
                      or result.get("lastError")
                      or f"worker-start exited {completed.returncode}")
            return DispatchResult(
                DispatchOutcome.FAILED, self.name, target,
                detail=failure_excerpt(detail),
                error_code=_error_class(error.get("code"), str(detail)) if error else "no_error_report",
                raw_error_code=error.get("code"),
                start_state="started" if result.get("dispatchId") else "unknown",
                artifacts={"argv": argv, "returncode": completed.returncode,
                           "stage": result.get("stage"),
                           "failedStage": result.get("failedStage"),
                           "residualResources": result.get("residualResources")},
            )
        result = payload.get("result") or {}
        effective = (result.get("launch") or {}).get("effective") or {}
        resolved = effective.get("model")
        state = result.get("state")
        artifacts = {"argv": argv, "dispatchId": result.get("dispatchId"),
                     "state": state, "launch": result.get("launch")}
        start_state = "started" if result.get("dispatchId") else "unknown"
        if review_workspace is not None:
            artifacts["reviewWorkspace"] = {
                "path": review_workspace.path,
                "implementerPath": review_workspace.implementer_path,
                "isolation": review_workspace.isolation,
            }

        if resolved is not None and resolved != target.model:
            return DispatchResult(
                DispatchOutcome.CONTRACT_VIOLATION, self.name, target,
                model_resolved=resolved,
                detail=f"requested {target.model}, the receipt reports {resolved}",
                artifacts=artifacts,
                start_state=start_state,
            )
        if state != "ready":
            return DispatchResult(
                DispatchOutcome.FAILED, self.name, target, model_resolved=resolved,
                detail=failure_excerpt(result.get("lastError") or f"worker state {state!r}"),
                error_code="executor_error" if result.get("lastError") else "no_error_report",
                start_state=start_state,
                artifacts=artifacts,
            )
        return DispatchResult(DispatchOutcome.SUCCEEDED, self.name, target,
                              model_resolved=resolved, artifacts=artifacts,
                              asynchronous=True, start_state=start_state)

    def probe(self, runner=_run, which=shutil.which) -> ProbeResult:
        if not which(self.binary):
            return ProbeResult(self.name, Availability.UNKNOWN, "not on PATH")
        try:
            out = runner([self.binary, "status", "--json"], timeout=30)
            payload = json.loads(out.stdout)
        except Exception as exc:
            return ProbeResult(self.name, Availability.INSTALLED, "status unreadable", str(exc))
        if out.returncode != 0:
            # Readiness is the one thing this adapter can actually prove, so it
            # does not get proven by a command that failed.
            return ProbeResult(self.name, Availability.INSTALLED,
                               f"status exited {out.returncode}",
                               (out.stderr or out.stdout or "").strip()[:200])
        runtime = (payload.get("result") or {}).get("runtime") or {}
        if runtime.get("state") == "ready" and runtime.get("reachable"):
            return ProbeResult(self.name, Availability.READY,
                               "runtime reports ready and reachable")
        return ProbeResult(self.name, Availability.INSTALLED,
                           "runtime is not reachable", json.dumps(runtime)[:200])


class Registry:
    """The adapters available in this environment."""

    def __init__(self, adapters: list[Adapter] | None = None) -> None:
        self._adapters = {a.name: a for a in (adapters or [CodexAdapter(), ClaudeAdapter(), OrcaAdapter()])}

    def __contains__(self, name: str) -> bool:
        return name in self._adapters

    def get(self, name: str) -> Adapter:
        if name not in self._adapters:
            raise ExecutorError(f"no adapter for executor {name!r}")
        return self._adapters[name]

    def compatible_executors(self, policy: WorkspacePolicy | str, **dispatch_kwargs) -> frozenset[str]:
        """Return only adapters that can satisfy a workspace contract."""
        return frozenset(
            name for name, adapter in self._adapters.items()
            if adapter.supports_workspace_policy(policy, **dispatch_kwargs)
        )

    def probe_all(self) -> dict[str, ProbeResult]:
        return {name: adapter.probe() for name, adapter in self._adapters.items()}

    def availability(
        self,
        policy: ReadinessPolicy = ReadinessPolicy.PROVEN,
        probes: dict[str, ProbeResult] | None = None,
    ) -> dict[str, Availability]:
        """Build the map the router consumes.

        Under `ATTEMPT`, an executor that reached its own provable ceiling of
        `AUTHENTICATED` is reported as `READY`, because that is the strongest
        claim it could ever make and refusing it would mean never dispatching
        natively at all. The promotion is a policy the caller chose, not a fact
        the probe found, and `dispatched_from` on the result keeps the two apart.
        """
        probes = probes if probes is not None else self.probe_all()
        out = {}
        for name, probe in probes.items():
            state = probe.availability
            if (
                policy is ReadinessPolicy.ATTEMPT
                and state is Availability.AUTHENTICATED
                and probe.honest_ceiling_reached
            ):
                state = Availability.READY
            out[name] = state
        return out


def dispatch(
    decision: RoutingDecision,
    task: str,
    registry: Registry | None = None,
    *,
    policy: ReadinessPolicy | None = None,
    probes: dict[str, ProbeResult] | None = None,
    clock=time.monotonic,
    **kw,
) -> DispatchResult:
    """Execute a routing decision, or refuse and say exactly what is missing.

    A blocked decision is never quietly re-routed here: the router already
    decided, and substituting a target at dispatch time would make the recorded
    decision a lie.
    """
    if decision.blocked or decision.target is None:
        raise ExecutorError(
            "a blocked decision has no target to dispatch; "
            "resolve availability and route again"
        )
    # Derived from the mode rather than defaulting to PROVEN: a caller that
    # forgot to pass a policy would otherwise silently never reach a native
    # executor, which is the quiet failure this whole layer exists to avoid.
    policy = policy if policy is not None else ReadinessPolicy.for_mode(decision.mode)
    registry = registry or Registry()
    target = decision.target
    adapter = registry.get(target.executor)
    on_progress = kw.pop("on_progress", None)
    on_workspace = kw.pop("on_workspace", None)
    dispatch_attempt_id = kw.pop("dispatch_attempt_id", None)
    on_dispatch_attempt = kw.pop("on_dispatch_attempt", None)
    sandbox_grants = tuple(kw.pop("sandbox_grants", ()) or ())
    command_policy = kw.pop("command_policy", None)
    if command_policy is not None and not isinstance(command_policy, StagePolicy):
        raise ExecutorError("a command policy must be a StagePolicy")
    if (command_policy is not None and target.executor == "claude"
            and "docker_host" in sandbox_grants):
        # The grant opens docker compose up/down/ps/logs/build in the table.
        command_policy = replace(command_policy, docker=True)

    probes = probes if probes is not None else registry.probe_all()
    probe = probes.get(target.executor)
    if probe is None:
        raise ExecutorError(f"no probe for executor {target.executor!r}")

    publishes = bool(kw.get("publishes", False))
    claude_allowed_commands = tuple(kw.pop("claude_allowed_commands", ()) or ())
    if target.executor == "claude" and claude_allowed_commands:
        kw["claude_allowed_commands"] = claude_allowed_commands
    if publishes:
        can_publish, detail = adapter.publication_access(
            probe, writes=bool(kw.get("writes", False)),
        )
        if not can_publish:
            return DispatchResult(
                DispatchOutcome.BLOCKED, target.executor, target,
                missing_capability="publication_access", detail=detail,
                start_state="not_started",
                readiness_policy=policy, dispatched_from=probe.availability,
            )
        if adapter.requires_publication_preflight:
            ready, detail = _publication_preflight(
                kw.get("cwd"), check_transport=not (
                    isinstance(adapter, NativeAdapter) and kw.get("writes", False)
                    and command_policy is not None and command_policy.publication_commands))
            if not ready:
                return DispatchResult(
                    DispatchOutcome.BLOCKED, target.executor, target,
                    missing_capability="publication_access", detail=detail,
                    start_state="not_started",
                    readiness_policy=policy, dispatched_from=probe.availability,
                )

    effective = registry.availability(policy, probes).get(target.executor, Availability.UNKNOWN)
    if not effective.dispatchable:
        return DispatchResult(
            DispatchOutcome.BLOCKED, target.executor, target,
            missing_capability="operating_availability",
            start_state="not_started",
            detail=f"{target.executor} is {probe.availability.value}: {probe.proof}",
            readiness_policy=policy, dispatched_from=probe.availability,
        )

    if (
        decision.mode is RoutingMode.CALIBRATION
        and probe.availability is not Availability.READY
    ):
        return DispatchResult(
            DispatchOutcome.BLOCKED, target.executor, target,
            missing_capability="proven_readiness",
            start_state="not_started",
            detail=("a calibration dispatch requires demonstrated readiness; "
                    f"{target.executor} could only show {probe.availability.value}"),
            readiness_policy=policy, dispatched_from=probe.availability,
        )

    requested_policy = kw.pop("workspace_policy", None)
    if requested_policy is None:
        requested_policy = (
            WorkspacePolicy.WORKSPACE_WRITE
            if kw.get("writes", False)
            else WorkspacePolicy.READ_ONLY
        )
    try:
        workspace_policy = WorkspacePolicy(requested_policy)
    except ValueError as exc:
        raise ExecutorError(
            f"unknown workspace policy {requested_policy!r}") from exc

    if not adapter.supports_workspace_policy(workspace_policy, **kw):
        capability = {
            WorkspacePolicy.READ_ONLY: "read_only_enforcement",
            WorkspacePolicy.DISPOSABLE: "disposable_workspace",
        }.get(workspace_policy, "workspace_policy")
        detail = {
            WorkspacePolicy.READ_ONLY: (
                f"{target.executor} cannot establish a non-mutating workspace; "
                "choose an adapter that can or provide an isolated review workspace"
            ),
            WorkspacePolicy.DISPOSABLE: (
                (
                    "a disposable policy needs a DisposableWorkspace whose path is "
                    "the dispatch cwd and does not overlap the source workspace"
                    if not (
                        isinstance(kw.get("workspace"), DisposableWorkspace)
                        and isinstance(kw.get("cwd"), str)
                        and _canonical_path(kw["cwd"])
                        == _canonical_path(kw["workspace"].path)
                    ) else (
                        f"{target.executor} cannot confine writes to a disposable "
                        "workspace"
                    )
                )
            ),
        }.get(workspace_policy, "the adapter cannot satisfy the workspace policy")
        return DispatchResult(
            DispatchOutcome.BLOCKED, target.executor, target,
            missing_capability=capability,
            start_state="not_started",
            detail=detail,
            readiness_policy=policy, dispatched_from=probe.availability,
        )

    policy_note = None
    if command_policy is not None and isinstance(adapter, OrcaAdapter):
        # The table and hook are applied to native dispatches only (#142).
        # Orca workers keep their behaviour and are not presented as
        # equivalent; a stage that needs the confined verification of a
        # review clone does not run there.
        if command_policy.active and command_policy.requires_confined_verification:
            return DispatchResult(
                DispatchOutcome.BLOCKED, target.executor, target,
                missing_capability="review_workspace_isolation",
                start_state="not_started",
                detail=("this review verifies in a confined disposable clone, which "
                        f"{target.executor} workers cannot provide; route the review to a "
                        "native Codex or Claude executor"),
                readiness_policy=policy, dispatched_from=probe.availability,
            )
        policy_note = (f"inactive: {target.executor} workers keep their current tooling; "
                       "the per-stage command table applies to native dispatches only")
        command_policy = None
    elif command_policy is not None:
        kw["command_policy"] = command_policy
    active_policy = command_policy if command_policy is not None and command_policy.active else None

    # The contract is consumed by this layer. Adapters receive only concrete
    # execution arguments, never an unrecognised policy keyword.
    kw.pop("workspace", None)
    if isinstance(adapter, NativeAdapter) and on_progress is not None:
        kw["on_progress"] = on_progress
    if on_workspace is not None and isinstance(adapter, (NativeAdapter, OrcaAdapter)):
        kw["on_workspace"] = on_workspace
    # Evidence the runtime wrote outside the workspace. Only a native CLI is
    # started here and can be granted a directory; Orca's worker runs where
    # Orca puts it, and its prompt already says what to do without the file.
    # A writing stage is granted nothing: Claude's added directories are
    # editable under `acceptEdits`, and evidence must stay outside the writable
    # boundary of the stage it is evidence for.
    read_dirs = tuple(kw.pop("read_dirs", ()) or ())
    writable_dirs = tuple(kw.pop("writable_dirs", ()) or ())
    if target.executor == "codex" and kw.get("writes", False) and writable_dirs:
        kw["writable_dirs"] = writable_dirs
    if target.executor == "codex" and sandbox_grants:
        kw["sandbox_grants"] = sandbox_grants
    if isinstance(adapter, NativeAdapter) and read_dirs and not kw.get("writes", False):
        kw["read_dirs"] = read_dirs

    def invoke_adapter(
        prompt: str, *, cwd: str | None = None, **overrides,
    ) -> DispatchResult:
        call_kw = dict(kw)
        call_kw.update(overrides)
        if cwd is not None:
            call_kw["cwd"] = cwd
        attempt_id = on_dispatch_attempt() if on_dispatch_attempt is not None else dispatch_attempt_id
        if attempt_id is not None and any(skill in prompt for skill in (
                "cc-implement-issue", "cc-initial-review", "cc-resolve-comments", "cc-rereview")):
            prompt += (
                f"\n\nRuntime attempt_id: {attempt_id}. In the published change-request "
                "summary include a schema-2 receipt as <!-- code-cycle-stage {JSON} --> "
                "with schema: 2, repo, change_request_id (string), stage, attempt_id, "
                "head_sha (full current PR SHA), status, and finding_outcomes "
                "(explicit objects with id and status for this attempt only). "
                "Use the actual functional status, including BLOCKED when applicable. "
                "This receipt is evidence only; still emit the requested structured result."
            )
        if isinstance(adapter, NativeAdapter):
            if attempt_id is not None:
                call_kw["dispatch_attempt_id"] = attempt_id
            elif on_dispatch_attempt is not None:
                call_kw.pop("dispatch_attempt_id", None)
        return adapter.dispatch(target, prompt, **call_kw)

    def run_stage() -> DispatchResult:
        if (workspace_policy is WorkspacePolicy.READ_ONLY
                and target.executor == "claude" and not adapter.enforces_read_only):
            return _isolated_review_dispatch(
                adapter, target, task, cwd=kw.get("cwd"),
                dispatch_call=lambda prompt, isolated_cwd: invoke_adapter(
                    prompt, cwd=isolated_cwd, writes=False,
                ),
                command_policy=active_policy is not None,
            )
        if (workspace_policy is WorkspacePolicy.READ_ONLY and target.executor == "codex"
                and active_policy is not None and active_policy.role in sp.REVIEW_ROLES):
            # Codex reviewers verify in a disposable clone too (#142), under a
            # profile whose only writable paths are that clone and TMPDIR.
            if active_policy.requires_confined_verification:
                if native.nested_codex():
                    confined, detail = False, (
                        "known nested Codex limitation (#268): app-server socket directory must be "
                        "a user-owned directory with mode 0700; no host directory probe")
                else:
                    confined, detail = adapter.probe_review_confinement()
                if not confined:
                    return DispatchResult(
                        DispatchOutcome.FAILED, target.executor, target,
                        detail=failure_excerpt(f"BLOCKED: {detail}", 600),
                        error_code="environment_restricted", start_state="not_started",
                        lifecycle_state="failed", process_exited=True,
                        artifacts={"command_policy": "blocked: review sandbox unverified",
                                   "native_sandbox_preflight": {
                                       "status": "unverified", "functional_check": {
                                           "probe": "codex review confinement", "passed": False,
                                           "evidence": detail}}},
                    )
            return _isolated_review_dispatch(
                adapter, target, task, cwd=kw.get("cwd"),
                dispatch_call=lambda prompt, isolated_cwd: invoke_adapter(
                    prompt, cwd=isolated_cwd, writes=False, review_clone=isolated_cwd,
                ),
                read_only_mode="enforced", command_policy=True,
            )
        result = invoke_adapter(task)
        if not adapter.completes_work and result.outcome is DispatchOutcome.SUCCEEDED:
            result = replace(result, asynchronous=True)
        if workspace_policy is WorkspacePolicy.READ_ONLY and adapter.enforces_read_only:
            result = replace(result, artifacts={"read_only_mode": "enforced",
                                                **result.artifacts})
        return result

    started = clock()
    if workspace_policy is WorkspacePolicy.READ_ONLY:
        # Every read-only stage, on every executor: the sandbox flag and the
        # isolated clone narrow what can be written, and this proves nothing
        # was. `enforced` survives only when the checkout is unchanged.
        result = _verified_read_only(kw.get("cwd"), run_stage)
        if isinstance(result, str):
            return DispatchResult(
                DispatchOutcome.BLOCKED, target.executor, target,
                missing_capability="read_only_verification", detail=result,
                start_state="not_started",
                readiness_policy=policy, dispatched_from=probe.availability,
            )
    else:
        result = run_stage()
    elapsed = max(0, round((clock() - started) * 1000))
    if policy_note is not None:
        result = replace(result, artifacts={"command_policy": policy_note, **result.artifacts})
    # From what the adapter is, not from what this result remembered to say.
    asynchronous = (result.asynchronous
                    or (not adapter.completes_work
                        and result.outcome is DispatchOutcome.SUCCEEDED))
    # `replace` rather than a rebuild by hand: the fields below are what this
    # layer knows and the adapter does not, and everything else is the
    # adapter's answer. Listing the rest again would mean every new field on a
    # result has to be remembered here too, and the one that is forgotten is
    # silently dropped on its way out.
    return replace(
        result, readiness_policy=policy, dispatched_from=probe.availability,
        asynchronous=asynchronous,
        duration_ms=None if asynchronous else elapsed,
    )
