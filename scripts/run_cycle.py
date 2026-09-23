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

**A dispatch that succeeded is not a stage that worked.** Those are two facts
about different layers and the store keeps them apart: `outcome` says the call
returned, `status` says what the agent reported doing. A canary run found both
true at once — Codex exited 0 having changed nothing, because the work item did
not exist — and the cycle went on to review it. Now a stage whose own report is
not a completion stops the cycle, with its dispatch still recorded as having
succeeded, because it did.

**Orca is a start, not a finish.** Its dispatch returns a started worker and a
`dispatchId`; the stage's own result arrives later, elsewhere. A cycle routed to
Orca therefore stops after the dispatch and says so rather than pretending the
absent output means failure — and `result.asynchronous` is what says so, not the
absence of output, because a succeeded dispatch with nothing to read looks
exactly like a finished one.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from cycle import CycleRecorder, StageOutcome
from executors import DispatchResult, ReadinessPolicy, Registry
from router import (
    RouterError,
    RoutingMode,
    RoutingStrategy,
    TaskSignals,
    load_profiles,
    load_routing_strategy,
)
from telemetry import (
    Telemetry,
    TelemetryError,
    default_database_path,
    validate_reference,
)

#: The repository's own declaration of how it wants to be run.
CONFIG_NAME = ".code-cycle.yml"

#: The keys under `code_cycle` this driver reads itself.
DRIVER_KEYS = frozenset({"repository", "profiles", "routing"})

#: The keys under `code_cycle` that belong to a skill or another module. They
#: are legitimate here and deliberately not interpreted: recognising a key is
#: not the same as consuming it, and this driver consumes none of these.
#: `calibration` is read by `cc-orca-orchestrator`'s paired review only; an
#: explicitly launched `--mode calibration` does not take its arms from it.
FOREIGN_KEYS = frozenset({
    "issue_provider", "code_host", "issue", "verification",  # provider bootstrap
    "review",            # review skills: trusted_authors
    "security_review",   # security_gate.py
    "orchestration",     # cc-orchestrator
    "calibration",       # cc-orca-orchestrator paired review
})

#: Every key `code_cycle` may carry. Anything else is a typo or a key this
#: version does not know, and either way a run under it would follow a policy
#: nobody declared.
KNOWN_KEYS = DRIVER_KEYS | FOREIGN_KEYS

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
    "your response. Do not omit the block when the stage is blocked; report "
    "the blocking status and reason in it."
)

# This is deliberately a named policy rather than an incidental sentence in a
# caller's prompt.  A local-only run must be visible in the prompt, telemetry,
# and the CLI invocation, so a later operator can tell a safe rehearsal from a
# run that was allowed to publish changes.
LOCAL_ONLY_REQUEST = (
    "Safety boundary: work only in the supplied cwd linked worktree. This local-only "
    "cycle stops after implementation; keep all changes local, and do not push, "
    "merge, publish issue or review comments, or create a pull request."
)

BEGIN, END = "ORCHESTRATION_RESULT", "END_ORCHESTRATION_RESULT"

#: Everything a terminal acts on rather than shows: the C0 controls and DEL,
#: minus tab, newline and carriage return, which whitespace collapsing handles.
#: Removing the bytes is the guarantee; recognising escape *sequences* is not,
#: because that means keeping a grammar in step with every terminal.
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: How much of an agent's prose is worth a line of somebody's screen.
REASON_LIMIT = 500

#: What each role's own report has to say for the cycle to keep going. Anything
#: else — `BLOCKED`, `FAILED`, a status this driver does not know — is a stage
#: that did not complete, whatever its exit code was. `PARTIALLY_RESOLVED`
#: continues because the re-review is what judges how much was resolved.
COMPLETES = {
    "implement": frozenset({"IMPLEMENTED"}),
    "resolve": frozenset({"RESOLVED", "PARTIALLY_RESOLVED"}),
    "review": frozenset({"APPROVED", "CHANGES_REQUESTED"}),
    "rereview": frozenset({"APPROVED", "CHANGES_REQUESTED"}),
}

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
    for key in (
        "code_cycle", "code_cycle.repository", "code_cycle.profiles",
        "code_cycle.routing", "code_cycle.calibration",
    ):
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

    # A misspelt key used to pass through unread, so `profles` ran the built-in
    # profiles while the file on disk declared others. Named here, before a
    # stage is dispatched, with the nearest known key when there is one.
    unknown = sorted(set(config.get("code_cycle") or {}) - KNOWN_KEYS)
    if unknown:
        described = []
        for key in unknown:
            near = difflib.get_close_matches(str(key), sorted(KNOWN_KEYS), n=1)
            described.append(
                f"{key!r} (did you mean {near[0]!r}?)" if near else repr(key))
        raise CycleDriverError(
            f"{path}: unknown key under code_cycle: " + ", ".join(described)
            + "; known keys are " + ", ".join(sorted(KNOWN_KEYS)))

    try:
        load_routing_strategy(config)
    except RouterError as error:
        raise CycleDriverError(str(error)) from error
    return config


def repository_of(config: dict) -> str | None:
    """`code_cycle.repository.selector`, when the repository declares one."""
    section = config.get("code_cycle")
    repository = section.get("repository") if isinstance(section, dict) else None
    selector = repository.get("selector") if isinstance(repository, dict) else None
    return selector if isinstance(selector, str) and selector else None


@dataclass(frozen=True)
class Reported:
    """What a stage said about itself, including saying nothing.

    Three outcomes are different facts and used to arrive as the same `None`:
    the agent reported a status, the agent reported something unreadable, and
    the agent reported nothing at all. A cycle that cannot tell them apart
    records "no verdict" for a run that was explicitly blocked.
    """

    present: bool = False
    payload: dict | None = None
    status: str | None = None

    @property
    def readable(self) -> bool:
        return self.payload is not None

    @property
    def reason(self) -> str | None:
        """What the agent said about its own outcome, in its own words.

        The agent's claim, never a verified fact. A canary run reported "GitHub
        authentication is invalid and the GitHub API is unreachable" while `gh`
        worked from the same sandbox, same directory, minutes later. Showing it
        is what let that be checked at all; believing it would have sent
        somebody to look at GitHub's status page.

        So it is displayed and nothing else: it never decides the flow, and it
        never reaches the store, which holds references and counts, not prose.
        """
        if not self.payload:
            return None
        for key in ("error", "summary", "blocking_reason", "reason"):
            value = self.payload.get(key)
            if isinstance(value, str) and readable(value):
                return readable(value)
        return None

    def completes(self, role: str) -> bool:
        return self.status in COMPLETES.get(role, frozenset())

    def explain(self, role: str) -> str:
        if not self.present:
            return f"the {role} reported no structured result"
        if not self.readable:
            return f"the {role} reported a structured result that could not be read"
        if self.status is None:
            return f"the {role} reported a structured result with no status"
        return f"the {role} reported {self.status}"


@dataclass
class CycleReport:
    """What the cycle did, and why it stopped there."""

    repo_id: str
    task_id: str
    status: str = UNRESOLVED_END
    iterations: int = 0
    verdict: str | None = None
    stopped_because: str | None = None
    #: The agent's own account of why, when it gave one. Its claim, not a
    #: verified fact, and informative only: nothing branches on it.
    reason: str | None = None
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
        if self.reason:
            # What the agent said, so a person can weigh it rather than pay for
            # another run to find out what it was.
            lines.append(f"  reason:  {self.reason}")
        return "\n".join(lines)


def readable(text: str, limit: int = REASON_LIMIT) -> str:
    """Text from an executor, made safe to put on somebody's screen.

    Everything it is given arrives from an agent or from a CLI's stderr, and is
    printed straight into a terminal report. Collapsing whitespace was not
    enough: an escape is not whitespace, so `\x1b[31m...` survived and an agent
    could recolour, erase or forge the lines around its own.

    The control bytes are removed rather than the escape sequences recognised.
    Without `ESC` such a sequence is inert text, and that holds without keeping
    a grammar in step with every terminal that might read the output.
    """
    if not isinstance(text, str):
        return ""
    cleaned = " ".join(CONTROL_CHARACTERS.sub("", text).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "\u2026"


def compose(role: str, repo_id: str, task_id: str, instruction: str = "",
            *, local_only: bool = False) -> str:
    """The prompt for one stage. Named skill, named work item, nothing implied."""
    skill = SKILL_FOR_ROLE.get(role)
    if skill is None:
        raise CycleDriverError(f"no skill is defined for the role {role!r}")
    parts = [f"Run {skill} for {task_id} in {repo_id}."]
    if instruction:
        parts.append(instruction)
    if local_only:
        parts.append(LOCAL_ONLY_REQUEST)
    parts.append(STRUCTURED_REQUEST)
    return " ".join(parts)


def validate_local_only_cwd(cwd: str | None) -> None:
    """Require an explicit Git worktree before enabling the local-only policy.

    Without this check ``--local-only`` could silently run in the coordinator's
    current directory, which is exactly the accidental write the flag is meant
    to make difficult.  This is a policy boundary, not an OS sandbox; the
    caller still needs normal network and credential isolation for a hard
    guarantee against remote side effects.
    """
    if not cwd:
        raise CycleDriverError("--local-only requires an explicit --cwd worktree")
    path = Path(cwd).expanduser()
    if not path.is_dir():
        raise CycleDriverError(f"--local-only requires an existing worktree: {path}")
    marker = path / ".git"
    if not marker.is_file():
        raise CycleDriverError(
            f"--local-only requires a linked Git worktree, not a live repository: {path}"
        )
    try:
        marker_text = marker.read_text(encoding="utf-8", errors="strict")
    except OSError as error:
        raise CycleDriverError(f"--local-only could not read the worktree marker: {error}") from error
    if not marker_text.startswith("gitdir:"):
        raise CycleDriverError(f"--local-only requires a linked Git worktree: {path}")


def read_structured_result(result: DispatchResult | None) -> Reported:
    """The executor's own report of what it did, and how much of it is readable.

    It reads `agent_output`, which each adapter lifts out of its own CLI's
    envelope. Reading the envelope instead finds the right words with the wrong
    escapes — which is how a canary located the block in Claude's output and
    then failed to parse it, because `\n` and `\"` were still escapes inside a
    JSON string.
    """
    if result is None or result.asynchronous:
        return Reported()
    spoken = result.agent_output
    if not isinstance(spoken, str) or not spoken:
        return Reported()
    # From the end, and the last opening before that close. The request this
    # driver composes contains the word ORCHESTRATION_RESULT, so an executor
    # that echoes its own prompt puts a decoy earlier in the stream; reading
    # forwards would parse the prompt and report no verdict.
    end = spoken.rfind(END)
    if end == -1:
        return Reported()
    begin = spoken.rfind(BEGIN, 0, end)
    if begin == -1:
        return Reported()
    try:
        payload = json.loads(spoken[begin + len(BEGIN):end].strip())
    except ValueError:
        return Reported(present=True)
    # A block holding a string or a number is delimited, parseable and not a
    # result. Present but unreadable: the caller must not `.get` on it.
    if not isinstance(payload, dict):
        return Reported(present=True)
    return Reported(present=True, payload=payload, status=_status_of(payload))


def _status_of(payload: dict) -> str | None:
    """The reported status, when it is one the store can actually hold.

    A status outside the vocabulary is treated as no status rather than carried
    to `record_verdict`, where it would raise and end the run without its
    closing row — a failure this driver has already made once.
    """
    status = payload.get("status")
    if not isinstance(status, str):
        return None
    status = status.strip().upper()
    try:
        validate_reference("status", status)
    except TelemetryError:
        return None
    return status


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
    routing_strategy: RoutingStrategy = RoutingStrategy.FIXED,
    policy: ReadinessPolicy | None = None,
    max_iterations: int = 3,
    cwd: str | None = None,
    timeout: int | None = None,
    local_only: bool = False,
) -> CycleReport:
    """implement -> review -> (resolve -> rereview)*, every stage recorded."""
    if local_only:
        validate_local_only_cwd(cwd)
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
        probes=probes, profiles=profiles, local_only=local_only,
        routing_strategy=routing_strategy,
    )
    report = CycleReport(repo_id=repo_id, task_id=task_id)
    dispatch_kwargs = {}
    if cwd:
        dispatch_kwargs["cwd"] = cwd
    if timeout is not None:
        dispatch_kwargs["timeout"] = timeout

    def run(role: str, instruction: str = "") -> tuple[StageOutcome, Reported]:
        outcome = recorder.stage(
            role, compose(role, repo_id, task_id, instruction, local_only=local_only),
                                 **dispatch_kwargs)
        report.stages.append(outcome)
        return outcome, read_structured_result(outcome.result)

    def stop(because: str, status: str = UNRESOLVED_END,
             reported: Reported | None = None) -> CycleReport:
        report.stopped_because = because
        report.status = status
        report.iterations = recorder.iteration
        if reported is not None:
            report.reason = reported.reason
        recorder.close(status)
        return report

    def advance(role: str, instruction: str = "") -> tuple[Reported, CycleReport | None]:
        """Run one stage and decide whether the cycle may continue past it.

        Every reason to stop is here rather than repeated per stage: a stop
        condition that has to be remembered four times is one that will be
        missing from the fourth.
        """
        outcome, reported = run(role, instruction)
        if not outcome.succeeded:
            return reported, stop(_why(outcome), reported=reported)
        if _started_elsewhere(outcome):
            return reported, stop(_elsewhere(outcome), reported=reported)
        # The dispatch and the work are different facts. This records what the
        # agent said it did; the row already says the call returned. The prose
        # beside it is not recorded: the store holds references and counts.
        if reported.status:
            recorder.record_verdict(role, reported.status, **_findings(reported.payload))
        if not reported.completes(role):
            return reported, stop(reported.explain(role), reported=reported)
        return reported, None

    reported, stopped = advance("implement")
    if stopped is not None:
        return stopped
    if local_only:
        return stop(
            "local-only run stops after implementation; no change request was created for review",
            UNRESOLVED_END,
            reported=reported,
        )

    reported, stopped = advance("review")
    if stopped is not None:
        return stopped
    verdict = reported.status
    report.verdict = verdict

    while verdict == "CHANGES_REQUESTED" and recorder.iteration < max_iterations:
        recorder.next_iteration()

        _, stopped = advance("resolve", "Resolve the findings from the review.")
        if stopped is not None:
            return stopped

        reported, stopped = advance("rereview", "Re-review the change after the fixes.")
        if stopped is not None:
            return stopped
        verdict = reported.status
        report.verdict = verdict

    # The last stage's own account travels with every ending, not only the bad
    # ones: a run that finished still said something about how.
    if verdict == "APPROVED":
        return stop("", APPROVED_END, reported=reported)
    return stop(f"still {verdict} after {recorder.iteration} round(s)",
                reported=reported)


def _started_elsewhere(outcome: StageOutcome) -> bool:
    """Whether this stage launched work that finishes outside this process."""
    return outcome.result is not None and outcome.result.asynchronous


def _elsewhere(outcome: StageOutcome) -> str:
    result = outcome.result
    reference = result.artifacts.get("dispatchId") if result else None
    reference = readable(reference, 100) if isinstance(reference, str) else None
    started = f" as {reference}" if reference else ""
    return (f"{outcome.role} was started on {result.executor}{started} and "
            "finishes elsewhere; this cycle cannot see its result")


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
    # `detail` is the executor's own stderr or stdout, so it is exactly as
    # untrusted as the agent's prose and reaches the same terminal.
    detail = readable(result.detail)
    if result.missing_capability:
        return (f"{outcome.role} on {result.executor} is missing "
                f"{result.missing_capability}: {detail}")
    return f"{outcome.role} on {result.executor} {result.outcome.value}: {detail}"


def plan(args) -> tuple[str, dict]:
    """Return the repository and profiles for existing callers."""
    repo, profiles, _ = plan_with_strategy(args)
    return repo, profiles


def plan_with_strategy(args) -> tuple[str, dict, RoutingStrategy]:
    """Everything the configuration decides, decided before anything runs.

    An unknown profile name is refused by `load_profiles`, and learning that
    after a stage has already run would mean paying for a cycle to discover a
    typo. So this happens first, and it raises rather than falling back.
    """
    if getattr(args, "local_only", False):
        validate_local_only_cwd(getattr(args, "cwd", None))

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
        routing_strategy = load_routing_strategy(config)
    except RouterError as error:
        raise CycleDriverError(str(error)) from error
    return repo, profiles, routing_strategy


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
    parser.add_argument("--local-only", action="store_true",
                        help=("run only in the explicit --cwd linked worktree; "
                              "stop after implementation without publishing"))
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
        repo, profiles, routing_strategy = plan_with_strategy(args)
    except CycleDriverError as error:
        parser.error(str(error))

    telemetry = Telemetry(Path(args.database) if args.database else None)
    try:
        report = run_cycle(
            repo, args.task,
            TaskSignals(difficulty=args.difficulty,
                        verifiability=args.verifiability,
                        security_sensitive=args.security_sensitive),
            telemetry,
            profiles=profiles,
            mode=RoutingMode[args.mode.upper()],
            routing_strategy=routing_strategy,
            max_iterations=args.max_iterations,
            cwd=args.cwd,
            timeout=args.timeout,
            local_only=args.local_only,
        )
    except CycleDriverError as error:
        parser.error(str(error))

    print(report.explain())
    print(f"recorded in {telemetry.path}")
    return 0 if report.status == APPROVED_END else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
