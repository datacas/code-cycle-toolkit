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
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from router import Availability, RoutingDecision, RoutingMode, Target

SCHEMA_VERSION = 1


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


def _run(argv: list[str], timeout: int = 30, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                          stdin=subprocess.DEVNULL)


def _publication_preflight(cwd: str | None) -> tuple[bool, str]:
    """Check the live GitHub/Git remote write path without changing remote state."""
    directory = cwd or os.getcwd()

    def run(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, cwd=directory,
                              stdin=subprocess.DEVNULL)

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
        pushed = run(["git", "push", "--dry-run", "origin",
                      "HEAD:refs/heads/cc-cycle-preflight"])
        if pushed.returncode:
            return False, f"remote rejected the publication preflight (exit {pushed.returncode})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"publication preflight could not complete ({type(exc).__name__})"
    return True, "Git authentication and remote write access passed a no-change preflight"


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
            missing_capability=capability, detail=detail,
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

    def dispatch(self, target: Target, task: str, *, cwd: str | None = None,
                 timeout: int = 3600, runner=_run,
                 writes: bool = False, publishes: bool = False,
                 publication_permissions: tuple[str, ...] = ()) -> DispatchResult:
        """Run the agent non-interactively and classify what came back.

        `writes` is what the stage is for, not what it might want: an
        implementation edits the tree it was given, a review reads it. The
        default is the smaller permission, so a caller that says nothing asks
        for nothing.

        Non-interactive on purpose. An agent waiting on a trust dialog, a hook
        review or a login cannot be driven from here, and answering such a
        screen blind is not something this layer will do — it reports the
        missing capability and stops.
        """
        if publishes and publication_permissions:
            argv = self.argv(
                target, task, cwd, writes, True,
                publication_permissions=publication_permissions,
            )
        else:
            argv = (self.argv(target, task, cwd, writes, True)
                    if publishes else self.argv(target, task, cwd, writes))
        try:
            completed = runner(argv, timeout=timeout, cwd=cwd)
        except subprocess.TimeoutExpired:
            return DispatchResult(
                DispatchOutcome.FAILED, self.name, target,
                detail=f"no result within {timeout}s; the run may still be alive",
            )
        except Exception as exc:
            return DispatchResult(DispatchOutcome.FAILED, self.name, target, detail=str(exc))

        friction = self.classify_failure(
            completed.returncode, completed.stderr or "", completed.stdout or ""
        )
        if friction is not None:
            capability, detail = friction
            return self._blocked(target, capability, detail)
        if completed.returncode != 0:
            return DispatchResult(
                DispatchOutcome.FAILED, self.name, target,
                detail=(completed.stderr or completed.stdout or "").strip()[:400],
                artifacts={"argv": argv, "returncode": completed.returncode},
            )
        stdout = completed.stdout or ""
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
                artifacts=artifacts, agent_output=spoken,
            )
        return DispatchResult(
            DispatchOutcome.SUCCEEDED, self.name, target,
            model_resolved=resolved, artifacts=artifacts, agent_output=spoken,
        )

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

    def publication_access(self, probe: ProbeResult, *, writes: bool) -> tuple[bool, str]:
        if probe.version is None or probe.version < (0, 138, 0):
            return False, "Codex permission profiles (required for scoped publication access) need CLI 0.138.0 or later"
        return True, "Codex permission profile scopes network access to code hosts and preserves the stage filesystem boundary"

    def argv(self, target: Target, task: str, cwd: str | None = None,
             writes: bool = False, publishes: bool = False,
             publication_permissions: tuple[str, ...] = ()) -> list[str]:
        # `codex exec` is read-only unless told otherwise, which is why four
        # canary runs had an implementer that could not implement: it reported
        # BLOCKED on "the read-only workspace" and nothing here had ever asked
        # for anything else. `workspace-write` is the directory this dispatch
        # was given and nothing beyond it; `danger-full-access` stays out of
        # this file entirely.
        argv = [self.binary, "exec", "-m", target.model,
                "-c", f"model_reasoning_effort={target.effort}", "--json"]
        if publishes:
            profile = "code_cycle_publish_write" if writes else "code_cycle_publish_read"
            parent = ":workspace" if writes else ":read-only"
            argv += [
                "-c", f'default_permissions="{profile}"',
                "-c", "features.network_proxy=true",
                "-c", f'permissions.{profile}.extends="{parent}"',
                "-c", f"permissions.{profile}.network.enabled=true",
                "-c", (
                    f'permissions.{profile}.network.domains={{'
                    '"github.com"="allow",'
                    '"api.github.com"="allow",'
                    '"bitbucket.org"="allow",'
                    '"api.bitbucket.org"="allow"}'
                ),
            ]
        else:
            argv += ["-s", "workspace-write" if writes else "read-only"]
        if cwd:
            argv += ["-C", cwd]
        return argv + [task]

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


def _isolated_review_dispatch(adapter, target, task, *, cwd, dispatch_call):
    """Run a reviewer in an independent detached clone and verify its source checkout."""
    if not isinstance(cwd, str) or not cwd:
        return adapter._blocked(
            target, "review_workspace_isolation",
            "a Claude read-only stage needs an implementer Git checkout to verify",
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
        if worktree:
            shutil.rmtree(worktree, ignore_errors=True)
        return adapter._blocked(
            target, "review_workspace_isolation",
            f"could not prepare an isolated review workspace: {exc}",
        )

    prompt = (
        f"{task}\n\nREAD-ONLY HARNESS CONTRACT: Do not modify files, commit, or push. "
        "This stage runs in an independent disposable clone; any local edits will "
        "be discarded. Any change to the implementer's branch or working tree "
        "fails this stage."
    )
    result = None
    workspace_changed = False
    source_error = "the implementer branch, HEAD, working tree, or configured remote refs changed during review"
    cleanup_warning = None
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
        try:
            shutil.rmtree(worktree)
            if Path(worktree).exists():
                cleanup_warning = "the disposable review workspace could not be removed"
        except Exception as exc:
            shutil.rmtree(worktree, ignore_errors=True)
            cleanup_warning = f"could not remove the disposable review workspace: {exc}"

    warnings = []
    if workspace_changed:
        warnings.append("the reviewer changed its disposable workspace; those edits were discarded")
    if cleanup_warning:
        warnings.append(cleanup_warning)
    artifacts = dict(result.artifacts)
    artifacts["read_only_mode"] = "isolated_verified"
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
    if source_changed:
        artifacts.pop("read_only_mode", None)
        return replace(
            result, outcome=DispatchOutcome.CONTRACT_VIOLATION,
            missing_capability=None,
            detail="read-only contract violation: " + source_error,
            artifacts=artifacts,
        )
    detail = result.detail
    if warnings:
        detail = detail + ("; " if detail else "") + "warning: " + "; ".join(warnings)
    return replace(result, detail=detail, artifacts=artifacts)


class ClaudeAdapter(NativeAdapter):
    name = "claude"
    binary = "claude"
    quota_markers = ("usage limit", "rate limit", "out of credits")
    interactive_markers = {
        "bypass permissions mode": "bypass_acknowledgement",
        "is this a project you created or one you trust": "folder_trust",
        "log in": "authenticated_session",
    }

    def publication_access(self, probe: ProbeResult, *, writes: bool) -> tuple[bool, str]:
        return True, "Claude uses gh for comments and git for writing stages; publication policy stays in its prompt"

    def supports_non_writing(self, **dispatch_kwargs) -> bool:
        cwd = dispatch_kwargs.get("cwd")
        return isinstance(cwd, str) and bool(cwd) and Path(cwd).is_dir()

    def dispatch(self, target: Target, task: str, *, cwd: str | None = None,
                 timeout: int = 3600, runner=_run, writes: bool = False,
                 publishes: bool = False,
                 publication_permissions: tuple[str, ...] = ()) -> DispatchResult:
        return super().dispatch(
            target, task, cwd=cwd, timeout=timeout, runner=runner,
            writes=writes, publishes=publishes,
            publication_permissions=publication_permissions,
        )

    def argv(self, target: Target, task: str, cwd: str | None = None,
             writes: bool = False, publishes: bool = False,
             publication_permissions: tuple[str, ...] = ()) -> list[str]:
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
                "--effort", target.effort, "--output-format", "json"]
        if writes:
            argv += ["--permission-mode", "acceptEdits"]
        else:
            # Defence in depth. Read-only safety comes from the disposable
            # worktree and the source-checkout verification in dispatch().
            argv += ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
        # Publication remains behavioural. A read-only publisher may use gh to
        # post its comment, but does not need git access to the reviewed branch.
        if publishes:
            argv += (["--allowedTools", "Bash(gh:*)", "Bash(git:*)"] if writes
                     else ["--allowedTools", "Bash(gh:*)"])
        return argv

    def agent_output(self, stdout: str) -> str:
        """Claude prints one JSON object whose `result` holds the reply.

        Observed on a live run alongside `modelUsage`, `usage` and timings. If
        the envelope is not there, the output is returned unchanged: a caller
        with the raw text is better off than one with nothing.
        """
        try:
            payload = json.loads(stdout)
        except ValueError:
            return stdout
        if isinstance(payload, dict):
            spoken = payload.get("result")
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
                 timeout: int = 3600, runner=_run,
                 context: "OrcaDispatchContext | None" = None,
                 writes: bool = False) -> DispatchResult:
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
                                  detail=f"no receipt within {timeout}s; the worker may still be alive")
        except Exception as exc:
            return DispatchResult(DispatchOutcome.FAILED, self.name, target, detail=str(exc))

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
                detail=str(detail)[:400],
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
            )
        if state != "ready":
            return DispatchResult(
                DispatchOutcome.FAILED, self.name, target, model_resolved=resolved,
                detail=str(result.get("lastError") or f"worker state {state!r}")[:400],
                artifacts=artifacts,
            )
        return DispatchResult(DispatchOutcome.SUCCEEDED, self.name, target,
                              model_resolved=resolved, artifacts=artifacts,
                              asynchronous=True)

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

    probes = probes if probes is not None else registry.probe_all()
    probe = probes.get(target.executor)
    if probe is None:
        raise ExecutorError(f"no probe for executor {target.executor!r}")

    publishes = bool(kw.get("publishes", False))
    if publishes:
        can_publish, detail = adapter.publication_access(
            probe, writes=bool(kw.get("writes", False)),
        )
        if not can_publish:
            return DispatchResult(
                DispatchOutcome.BLOCKED, target.executor, target,
                missing_capability="publication_access", detail=detail,
                readiness_policy=policy, dispatched_from=probe.availability,
            )
        if adapter.requires_publication_preflight:
            ready, detail = _publication_preflight(kw.get("cwd"))
            if not ready:
                return DispatchResult(
                    DispatchOutcome.BLOCKED, target.executor, target,
                    missing_capability="publication_access", detail=detail,
                    readiness_policy=policy, dispatched_from=probe.availability,
                )

    effective = registry.availability(policy, probes).get(target.executor, Availability.UNKNOWN)
    if not effective.dispatchable:
        return DispatchResult(
            DispatchOutcome.BLOCKED, target.executor, target,
            missing_capability="operating_availability",
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
            detail=detail,
            readiness_policy=policy, dispatched_from=probe.availability,
        )

    # The contract is consumed by this layer. Adapters receive only concrete
    # execution arguments, never an unrecognised policy keyword.
    kw.pop("workspace", None)

    started = clock()
    if (workspace_policy is WorkspacePolicy.READ_ONLY
            and target.executor == "claude" and not adapter.enforces_read_only):
        result = _isolated_review_dispatch(
            adapter, target, task, cwd=kw.get("cwd"),
            dispatch_call=lambda prompt, isolated_cwd: adapter.dispatch(
                target, prompt, **{**kw, "cwd": isolated_cwd, "writes": False},
            ),
        )
    else:
        result = adapter.dispatch(target, task, **kw)
    elapsed = max(0, round((clock() - started) * 1000))
    if workspace_policy is WorkspacePolicy.READ_ONLY:
        artifacts = dict(result.artifacts)
        if adapter.enforces_read_only:
            artifacts.setdefault("read_only_mode", "enforced")
        result = replace(result, artifacts=artifacts)
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
