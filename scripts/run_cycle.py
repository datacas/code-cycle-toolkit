"""Run one work item through a full cycle, from an installation.

This is the wiring, and only the wiring. `router` decides, `executors` run,
`telemetry` records and `cycle` ties those together so a dispatch cannot forget
its row — but until now nothing constructed a `CycleRecorder` outside a test.
The guarantee existed and no production path reached it, which is the same shape
of failure as an instruction with no code behind it.

So this drives every stage through `recorder.stage()`. There is no branch here
that routes and dispatches by itself, and adding one would quietly reopen the
hole this closes.

**It decides nothing.** No cost model, no learning, no rule that changes itself
from an outcome. It composes each stage's prompt, asks the executor to run, and
reads what came back.

**It does not infer a verdict.** A review's outcome is read from the structured
result the executor was explicitly asked to emit — every review skill documents
that block as opt-in on request — and never from an exit code or from prose.
When the block is absent the cycle stops and says the verdict is unknown, which
is a fact worth recording, unlike a guess that looks like data forever after.

**Orca is a start, not a finish.** Its dispatch returns a started worker and a
`dispatchId`; the stage's own result arrives later, elsewhere. A cycle routed to
Orca therefore stops after the dispatch and says so rather than pretending the
absent output means failure — and `result.asynchronous` is what says so, not the
absence of output, because a succeeded dispatch with nothing to read looks
exactly like a finished one.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from cycle import CycleRecorder, StageOutcome
from executors import DispatchResult, ReadinessPolicy, Registry
from router import RouterError, RoutingMode, TaskSignals, load_profiles
from telemetry import (
    Telemetry,
    TelemetryError,
    default_database_path,
    validate_reference,
)

#: The repository's own declaration of how it wants to be run.
CONFIG_NAME = ".code-cycle.yml"

#: The skill each role runs, as the executor is told to invoke it.
SKILL_FOR_ROLE = {
    "implement": "cc-implement-issue",
    "review": "cc-initial-review",
    "resolve": "cc-resolve-comments",
    "rereview": "cc-rereview",
}

#: Asking for the block is what makes the verdict readable. Every review skill
#: treats it as opt-in and emits it when the request asks in plain language, so
#: this is a documented capability rather than a hopeful one.
STRUCTURED_REQUEST = (
    "Return the structured result: include the ORCHESTRATION_RESULT block in "
    "your response."
)

BEGIN, END = "ORCHESTRATION_RESULT", "END_ORCHESTRATION_RESULT"

#: How a cycle can end. Each one is a fact about this run, not a judgement.
APPROVED_END = "READY_FOR_MANUAL_MERGE"
UNRESOLVED_END = "HUMAN_INTERVENTION"


class CycleDriverError(RuntimeError):
    """The driver was asked for something it cannot honestly do."""


def load_config(path: Path) -> dict:
    """Read `.code-cycle.yml`, or say exactly why it could not be read.

    The toolkit is otherwise pure standard library, and a repository without a
    configuration file needs no parser at all — so PyYAML is required only when
    there is something to parse. Refusing loudly beats running with the built-in
    defaults while a file on disk says otherwise: the whole point of recording a
    run is that the row describes the policy that was actually in force.
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise CycleDriverError(
            f"{path} exists but PyYAML is not installed, so its configuration "
            "cannot be read; install PyYAML or pass --no-config to accept the "
            "built-in defaults"
        ) from exc

    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CycleDriverError(f"{path} could not be read: {exc}") from exc

    if config is None:
        return {}
    if not isinstance(config, dict):
        raise CycleDriverError(f"{path} must contain a mapping at the top level")

    # The shape, not only the top level. A valid YAML document can still say
    # `code_cycle: not-a-mapping`, and every reader below would then crash on
    # its own `.get` — a traceback where a stated refusal belongs.
    for key in ("code_cycle", "code_cycle.repository", "code_cycle.profiles"):
        section, value = config, None
        for part in key.split("."):
            if not isinstance(section, dict):
                break
            value = section.get(part)
            section = value
        if value is not None and not isinstance(value, dict):
            raise CycleDriverError(
                f"{path}: {key} must be a mapping, not "
                f"{type(value).__name__}")
    return config


def repository_of(config: dict) -> str | None:
    """`code_cycle.repository.selector`, when the repository declares one."""
    section = config.get("code_cycle")
    repository = section.get("repository") if isinstance(section, dict) else None
    selector = repository.get("selector") if isinstance(repository, dict) else None
    return selector if isinstance(selector, str) and selector else None


@dataclass
class CycleReport:
    """What the cycle did, and why it stopped there."""

    repo_id: str
    task_id: str
    status: str = UNRESOLVED_END
    iterations: int = 0
    verdict: str | None = None
    stopped_because: str | None = None
    stages: list[StageOutcome] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVED"

    def explain(self) -> str:
        lines = [f"{self.repo_id} {self.task_id}: {self.status}"]
        for stage in self.stages:
            result = stage.result
            ran = result.executor if result else "—"
            outcome = result.outcome.value if result else "blocked"
            lines.append(f"  {stage.role:<9} {stage.decision.profile:<14} {ran:<7} {outcome}")
        if self.stopped_because:
            lines.append(f"  stopped: {self.stopped_because}")
        return "\n".join(lines)


def compose(role: str, repo_id: str, task_id: str, instruction: str = "") -> str:
    """The prompt for one stage. Named skill, named work item, nothing implied."""
    skill = SKILL_FOR_ROLE.get(role)
    if skill is None:
        raise CycleDriverError(f"no skill is defined for the role {role!r}")
    parts = [f"Run {skill} for {task_id} in {repo_id}."]
    if instruction:
        parts.append(instruction)
    parts.append(STRUCTURED_REQUEST)
    return " ".join(parts)


def read_structured_result(result: DispatchResult | None) -> dict | None:
    """The executor's own report, or None when it did not make one.

    None is the honest answer in three different situations — the executor
    printed nothing parseable, the dispatch never produced output because it
    only started a worker, or the block fell outside the captured tail — and the
    caller treats all three the same way: it does not know.
    """
    if result is None or result.asynchronous:
        return None
    stdout = result.artifacts.get("stdout")
    if not isinstance(stdout, str):
        return None
    # From the end, and the last opening before that close. The request this
    # driver composes contains the word ORCHESTRATION_RESULT, so an executor
    # that echoes its own prompt puts a decoy earlier in the stream; reading
    # forwards would parse the prompt and report no verdict.
    end = stdout.rfind(END)
    if end == -1:
        return None
    begin = stdout.rfind(BEGIN, 0, end)
    if begin == -1:
        return None
    try:
        payload = json.loads(stdout[begin + len(BEGIN):end].strip())
    except ValueError:
        return None
    # A block holding a string or a number is delimited, parseable and not a
    # result. Returning it would hand the caller something that only looks like
    # one, and the first `.get` on it ends the run without its closing row.
    return payload if isinstance(payload, dict) else None


def run_cycle(
    repo_id: str,
    task_id: str,
    signals: TaskSignals,
    telemetry: Telemetry,
    *,
    profiles: dict | None = None,
    registry: Registry | None = None,
    availability: dict | None = None,
    mode: RoutingMode = RoutingMode.PRODUCTION,
    policy: ReadinessPolicy | None = None,
    max_iterations: int = 3,
    cwd: str | None = None,
    timeout: int | None = None,
) -> CycleReport:
    """implement -> review -> (resolve -> rereview)*, every stage recorded."""
    registry = registry or Registry()
    policy = policy or ReadinessPolicy.for_mode(mode)
    # Once, for the whole cycle. Re-probing between stages would let an
    # availability change with nothing recording that it had.
    probes = registry.probe_all()
    if availability is None:
        availability = registry.availability(policy, probes)

    recorder = CycleRecorder(
        telemetry, repo_id, task_id, signals,
        availability=availability, registry=registry, mode=mode, policy=policy,
        probes=probes, profiles=profiles,
    )
    report = CycleReport(repo_id=repo_id, task_id=task_id)
    dispatch_kwargs = {}
    if cwd:
        dispatch_kwargs["cwd"] = cwd
    if timeout is not None:
        dispatch_kwargs["timeout"] = timeout

    def run(role: str, instruction: str = "") -> tuple[StageOutcome, dict | None]:
        outcome = recorder.stage(role, compose(role, repo_id, task_id, instruction),
                                 **dispatch_kwargs)
        report.stages.append(outcome)
        return outcome, read_structured_result(outcome.result)

    def stop(reason: str, status: str = UNRESOLVED_END) -> CycleReport:
        report.stopped_because = reason
        report.status = status
        report.iterations = recorder.iteration
        recorder.close(status)
        return report

    outcome, payload = run("implement")
    if not outcome.succeeded:
        return stop(_why(outcome))
    if _started_elsewhere(outcome):
        return stop(_elsewhere(outcome))
    if payload and payload.get("status"):
        recorder.record_verdict("implement", str(payload["status"]))

    outcome, payload = run("review")
    if not outcome.succeeded:
        return stop(_why(outcome))
    if _started_elsewhere(outcome):
        return stop(_elsewhere(outcome))
    verdict = _verdict(payload)
    if verdict is None:
        return stop("the review reported no structured verdict")
    recorder.record_verdict("review", verdict, **_findings(payload))
    report.verdict = verdict

    while verdict == "CHANGES_REQUESTED" and recorder.iteration < max_iterations:
        recorder.next_iteration()

        outcome, payload = run("resolve", "Resolve the findings from the review.")
        if not outcome.succeeded:
            return stop(_why(outcome))
        if _started_elsewhere(outcome):
            return stop(_elsewhere(outcome))
        if payload and payload.get("status"):
            recorder.record_verdict("resolve", str(payload["status"]))

        outcome, payload = run("rereview", "Re-review the change after the fixes.")
        if not outcome.succeeded:
            return stop(_why(outcome))
        if _started_elsewhere(outcome):
            return stop(_elsewhere(outcome))
        verdict = _verdict(payload)
        if verdict is None:
            return stop("the re-review reported no structured verdict")
        recorder.record_verdict("rereview", verdict, **_findings(payload))
        report.verdict = verdict

    if verdict == "APPROVED":
        return stop("", APPROVED_END)
    return stop(f"still {verdict} after {recorder.iteration} round(s)")


def _started_elsewhere(outcome: StageOutcome) -> bool:
    """Whether this stage launched work that finishes outside this process."""
    return outcome.result is not None and outcome.result.asynchronous


def _elsewhere(outcome: StageOutcome) -> str:
    result = outcome.result
    reference = result.artifacts.get("dispatchId") if result else None
    started = f" as {reference}" if reference else ""
    return (f"{outcome.role} was started on {result.executor}{started} and "
            "finishes elsewhere; this cycle cannot see its result")


def _verdict(payload: dict | None) -> str | None:
    if not payload:
        return None
    status = payload.get("status")
    if not isinstance(status, str):
        return None
    status = status.upper()
    return status if status in {"APPROVED", "CHANGES_REQUESTED"} else None


def _findings(payload: dict | None) -> dict:
    """Counts the executor reported. Absent is absent, never zero."""
    if not payload:
        return {}
    out = {}
    unresolved = payload.get("unresolved_findings")
    if isinstance(unresolved, list):
        out["findings_total"] = len(unresolved)
        out["findings_blocking"] = sum(
            1 for finding in unresolved
            if isinstance(finding, dict) and finding.get("blocks_approval")
        )
    return out


def _why(outcome: StageOutcome) -> str:
    result = outcome.result
    if result is None:
        reasons = "; ".join(outcome.decision.reasons or ())
        return f"{outcome.role} was not routed anywhere: {reasons}"
    if result.missing_capability:
        return (f"{outcome.role} on {result.executor} is missing "
                f"{result.missing_capability}: {result.detail}")
    return f"{outcome.role} on {result.executor} {result.outcome.value}: {result.detail}"


def plan(args) -> tuple[str, dict]:
    """Everything the configuration decides, decided before anything runs.

    An unknown profile name is refused by `load_profiles`, and learning that
    after a stage has already run would mean paying for a cycle to discover a
    typo. So this happens first, and it raises rather than falling back.
    """
    config = resolve_config(args)

    repo = args.repo or repository_of(config)
    if not repo:
        raise CycleDriverError(
            "no repository: pass --repo or declare "
            f"code_cycle.repository.selector in {CONFIG_NAME}")

    # Checked by the store's own rule, here rather than at the first row. A
    # reference the store will refuse is one no stage should be dispatched
    # under: that run is paid for and its row cannot be written.
    for field, value in (("repo_id", repo), ("task_id", getattr(args, "task", None))):
        if value is None:
            continue
        try:
            validate_reference(field, value)
        except TelemetryError as error:
            raise CycleDriverError(str(error)) from error

    try:
        profiles = load_profiles(config)
    except RouterError as error:
        raise CycleDriverError(str(error)) from error
    return repo, profiles


def resolve_config(args) -> dict:
    """Which configuration this run is under, and none by accident.

    An explicit `--config` that is not there is an error, because somebody named
    it. A file found beside the work is used when it exists. `--no-config` is
    the only way to run on the built-in defaults while a file sits next to the
    work, and it has to be asked for.
    """
    if args.no_config:
        if args.config:
            raise CycleDriverError("--config and --no-config contradict each other")
        return {}
    if args.config:
        path = Path(args.config)
        if not path.is_file():
            raise CycleDriverError(f"no configuration at {path}")
        return load_config(path)
    path = Path(args.cwd or ".") / CONFIG_NAME
    return load_config(path) if path.is_file() else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_cycle",
        description="Run one work item through a recorded cycle.",
    )
    parser.add_argument("--repo", default=None,
                        help=("repository identifier, owner/name; defaults to "
                              f"code_cycle.repository.selector in {CONFIG_NAME}"))
    parser.add_argument("--task", required=True, help="work item identifier")
    # Labelled before routing, never after: choosing a model from a judgement
    # and then measuring by model measures the routing rather than the models.
    parser.add_argument("--difficulty", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--verifiability", choices=("auto", "partial", "human"),
                        default="auto")
    parser.add_argument("--security-sensitive", action="store_true")
    parser.add_argument("--mode", choices=("production", "calibration"),
                        default="production")
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--cwd", default=None,
                        help="working directory the executor runs in")
    parser.add_argument("--timeout", type=int, default=None,
                        help="seconds one dispatch may take")
    parser.add_argument("--database", default=None,
                        help=f"telemetry database (default: {default_database_path()})")
    parser.add_argument("--config", default=None,
                        help=f"path to {CONFIG_NAME} (default: alongside the work)")
    parser.add_argument("--no-config", action="store_true",
                        help="run on the built-in defaults, ignoring any configuration")
    args = parser.parse_args(argv)

    try:
        repo, profiles = plan(args)
    except CycleDriverError as error:
        parser.error(str(error))

    telemetry = Telemetry(Path(args.database) if args.database else None)
    report = run_cycle(
        repo, args.task,
        TaskSignals(difficulty=args.difficulty,
                    verifiability=args.verifiability,
                    security_sensitive=args.security_sensitive),
        telemetry,
        profiles=profiles,
        mode=RoutingMode[args.mode.upper()],
        max_iterations=args.max_iterations,
        cwd=args.cwd,
        timeout=args.timeout,
    )

    print(report.explain())
    print(f"recorded in {telemetry.path}")
    return 0 if report.status == APPROVED_END else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
