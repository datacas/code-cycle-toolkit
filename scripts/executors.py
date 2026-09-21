"""Turn a resolved `Target` into a real execution.

`router.route()` answers *where* work should go. This module answers *whether it
can actually go there*, and sends it. It adds no routing rules and no learning:
a decision arrives already made.

The uncomfortable fact this design is built around is that the executors differ
in what they can prove about themselves:

- Orca reports its runtime state, so `READY` is provable.
- Codex and Claude expose a version and an auth file. That proves `INSTALLED`
  and at best `AUTHENTICATED`. Nothing documented reports remaining quota, and
  the only way to learn it is to spend some.

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

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

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


class DispatchOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"


class ExecutorError(ValueError):
    """The dispatch layer was asked for something it must not do."""


@dataclass(frozen=True)
class ProbeResult:
    """What an executor could be shown to be, and on what evidence."""

    executor: str
    availability: Availability
    proof: str
    detail: str = ""
    provable_ceiling: Availability = Availability.READY

    @property
    def honest_ceiling_reached(self) -> bool:
        """True when nothing further could have been demonstrated."""
        return self.availability is self.provable_ceiling


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


def _run(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


class Adapter:
    """One way of running work. Subclasses implement `probe` and `dispatch`."""

    name = "abstract"
    #: The strongest state this adapter can demonstrate without spending quota.
    provable_ceiling = Availability.READY

    def probe(self) -> ProbeResult:  # pragma: no cover - interface
        raise NotImplementedError

    def dispatch(self, target: Target, task: str, **kw) -> DispatchResult:  # pragma: no cover
        raise NotImplementedError

    def _blocked(self, target: Target, capability: str, detail: str) -> DispatchResult:
        return DispatchResult(
            DispatchOutcome.BLOCKED, self.name, target,
            missing_capability=capability, detail=detail,
        )


class NativeAdapter(Adapter):
    """Common shape for running a CLI agent directly, without Orca.

    These cannot prove readiness. Their honest ceiling is `AUTHENTICATED`, and
    that is stated rather than rounded up.
    """

    binary = ""
    provable_ceiling = Availability.AUTHENTICATED
    #: Substrings that identify an exhausted window in the agent's own output.
    quota_markers: tuple[str, ...] = ()
    #: Substrings that identify a screen only a human can answer.
    interactive_markers: dict[str, str] = {}

    def auth_evidence(self) -> tuple[bool, str]:  # pragma: no cover - interface
        raise NotImplementedError

    def probe(self, runner=_run) -> ProbeResult:
        path = shutil.which(self.binary)
        if not path:
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

        authenticated, evidence = self.auth_evidence()
        if not authenticated:
            return ProbeResult(self.name, Availability.INSTALLED,
                               "binary runs, no credential found", evidence, self.provable_ceiling)
        # Deliberately stops here. Remaining quota is not observable without
        # spending it, so READY is never claimed from configuration alone.
        return ProbeResult(self.name, Availability.AUTHENTICATED, evidence,
                           "quota is not observable without dispatching",
                           self.provable_ceiling)

    def argv(self, target: Target, task: str, cwd: str | None = None) -> list[str]:  # pragma: no cover
        raise NotImplementedError

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
        return None

    def dispatch(self, target: Target, task: str, *, cwd: str | None = None,
                 timeout: int = 3600, runner=_run) -> DispatchResult:
        """Run the agent non-interactively and classify what came back.

        Non-interactive on purpose. An agent waiting on a trust dialog, a hook
        review or a login cannot be driven from here, and answering such a
        screen blind is not something this layer will do — it reports the
        missing capability and stops.
        """
        argv = self.argv(target, task, cwd)
        try:
            completed = runner(argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            return DispatchResult(
                DispatchOutcome.FAILED, self.name, target,
                detail=f"no result within {timeout}s; the run may still be alive",
            )
        except Exception as exc:
            return DispatchResult(DispatchOutcome.FAILED, self.name, target, detail=str(exc))

        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        friction = self.classify_output(output)
        if friction is not None:
            capability, detail = friction
            return self._blocked(target, capability, detail)
        if completed.returncode != 0:
            return DispatchResult(
                DispatchOutcome.FAILED, self.name, target,
                detail=(completed.stderr or completed.stdout or "").strip()[:400],
                artifacts={"argv": argv, "returncode": completed.returncode},
            )
        return DispatchResult(
            DispatchOutcome.SUCCEEDED, self.name, target,
            model_resolved=self.read_resolved_model(completed.stdout or ""),
            artifacts={"argv": argv, "stdout": (completed.stdout or "")[-4000:]},
        )

    def classify_output(self, text: str) -> tuple[str, str] | None:
        """Recognise a failure that a human, not a retry, has to resolve."""
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
    binary = "codex"
    quota_markers = ("usage limit", "rate limit", "quota")
    interactive_markers = {
        "hooks need review": "hook_trust",
        "trust this folder": "folder_trust",
        "sign in": "authenticated_session",
    }

    def argv(self, target: Target, task: str, cwd: str | None = None) -> list[str]:
        argv = [self.binary, "exec", "-m", target.model,
                "-c", f"model_reasoning_effort={target.effort}", "--json"]
        if cwd:
            argv += ["-C", cwd]
        return argv + [task]

    def auth_evidence(self) -> tuple[bool, str]:
        auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
        if auth.is_file():
            return True, "credential file present"
        return False, f"no credential at {auth}"


class ClaudeAdapter(NativeAdapter):
    name = "claude"
    binary = "claude"
    quota_markers = ("usage limit", "rate limit", "out of credits")
    interactive_markers = {
        "bypass permissions mode": "bypass_acknowledgement",
        "is this a project you created or one you trust": "folder_trust",
        "log in": "authenticated_session",
    }

    def argv(self, target: Target, task: str, cwd: str | None = None) -> list[str]:
        # No bypass flag. A run that needs elevated permissions to proceed is a
        # run a human should be looking at.
        return [self.binary, "-p", task, "--model", target.model,
                "--effort", target.effort, "--output-format", "json"]

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


class OrcaAdapter(Adapter):
    """The one backend that can demonstrate readiness.

    Optional by design. The toolkit stays usable without it, and nothing here
    makes Orca a dependency of anything else.
    """

    name = "orca"
    provable_ceiling = Availability.READY

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or ("orca-ide" if os.name != "nt" else "orca")

    def probe(self, runner=_run) -> ProbeResult:
        if not shutil.which(self.binary):
            return ProbeResult(self.name, Availability.UNKNOWN, "not on PATH")
        try:
            out = runner([self.binary, "status", "--json"], timeout=30)
            payload = json.loads(out.stdout)
        except Exception as exc:
            return ProbeResult(self.name, Availability.INSTALLED, "status unreadable", str(exc))
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
    policy: ReadinessPolicy = ReadinessPolicy.PROVEN,
    probes: dict[str, ProbeResult] | None = None,
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
    registry = registry or Registry()
    target = decision.target
    adapter = registry.get(target.executor)

    probes = probes if probes is not None else registry.probe_all()
    probe = probes.get(target.executor)
    if probe is None:
        raise ExecutorError(f"no probe for executor {target.executor!r}")

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

    result = adapter.dispatch(target, task, **kw)
    return DispatchResult(
        result.outcome, result.executor, result.requested,
        model_resolved=result.model_resolved,
        missing_capability=result.missing_capability,
        detail=result.detail,
        readiness_policy=policy,
        dispatched_from=probe.availability,
        artifacts=result.artifacts,
    )
