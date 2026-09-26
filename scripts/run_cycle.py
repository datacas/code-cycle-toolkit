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
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from cycle import CycleRecorder, StageOutcome
from cycle_status import CycleStatusWriter
from executors import DispatchResult, ReadinessPolicy, Registry
from jev_shadow import JevConfig, JevConfigError, JevShadow, load_jev_config
from review_contract import (
    RESOLUTION_RUN,
    REVIEW_RUN,
    RunStatuses,
    claimed_fix_survivals,
)
from router import (
    RouterError,
    RoutingMode,
    RoutingStrategy,
    TaskSignals,
    load_profiles,
    load_routing_strategy,
)
from stage_signals import collect_change_signals
from telemetry import (
    CYCLE_STARTS,
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

#: The keys under `code_cycle.routing`, all read by this driver.
ROUTING_KEYS = frozenset({"strategy", "jev"})

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

#: The stages that resume a change request instead of creating one.
RESUMES = frozenset(CYCLE_STARTS) - {"implement"}

#: How a cycle can end. Each one is a fact about this run, not a judgement.
APPROVED_END = "READY_FOR_MANUAL_MERGE"
UNRESOLVED_END = "HUMAN_INTERVENTION"

#: The escalation ladder for a finding that survives a claimed fix, as defined
#: by `review_contract.claimed_fix_survivals`. One survival can be an honestly
#: incomplete fix, so the next resolution is told to reproduce before editing.
#: Two survivals of one ID across different heads is the pattern neither the
#: iteration limit nor the no-progress guard catches, so the cycle stops before
#: another resolution is dispatched. Named constants, not configuration; the
#: ladder never changes a model, profile or provider.
SURVIVALS_BEFORE_DIRECTIVE = 1
SURVIVALS_BEFORE_STOP = 2

#: What the next resolution is told about the findings that survived once.
CONTESTED_DIRECTIVE = (
    "These findings survived a claimed fix and the next re-review reopened "
    "them: {ids}. For each one, before editing, reproduce it with the "
    "reviewer's reproduction and re-derive its cause, using the diagnosis "
    "procedure when one is available. Do not publish them as not_applicable "
    "again without evidence the re-review did not have; otherwise report "
    "PARTIALLY_RESOLVED and state that the finding is contested."
)

#: A finding ID that may be put into a later stage's prompt.
FINDING_ID = re.compile(r"REV-[A-Za-z0-9-]{1,40}")
HEAD_SHA = re.compile(r"[0-9a-f]{7,40}")


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

    routing = (config.get("code_cycle") or {}).get("routing") or {}
    unknown = sorted(str(key) for key in set(routing) - ROUTING_KEYS)
    if unknown:
        raise CycleDriverError(
            f"{path}: unknown key under code_cycle.routing: " + ", ".join(unknown)
            + "; known keys are " + ", ".join(sorted(ROUTING_KEYS)))

    try:
        load_routing_strategy(config)
        load_jev_config(config)
    except (RouterError, JevConfigError) as error:
        raise CycleDriverError(str(error)) from error
    return config


def repository_of(config: dict) -> str | None:
    """`code_cycle.repository.selector`, when the repository declares one."""
    section = config.get("code_cycle")
    repository = section.get("repository") if isinstance(section, dict) else None
    selector = repository.get("selector") if isinstance(repository, dict) else None
    return selector if isinstance(selector, str) and selector else None


def change_bases_of(config: dict) -> tuple[str, ...]:
    """The refs a change is measured against, most specific first.

    `code_cycle.repository.default_branch` when declared, otherwise the
    remote's own default. A base that does not resolve is skipped, and when
    none does the change signals are left out rather than recorded as empty.
    """
    section = config.get("code_cycle")
    repository = section.get("repository") if isinstance(section, dict) else None
    branch = repository.get("default_branch") if isinstance(repository, dict) else None
    if isinstance(branch, str) and branch:
        return (f"origin/{branch}", branch)
    return ("origin/HEAD",)


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
    def change_request_id(self) -> str | None:
        """A safe change-request reference, including the legacy alias.

        The identifier is inserted into a later stage's prompt, so accept only
        a short scalar made from reference characters. It is never stored in
        telemetry.
        """
        if not self.payload:
            return None
        for key in ("change_request_id", "pr_number"):
            value = self.payload.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                reference = str(value)
            elif isinstance(value, str):
                reference = value.strip()
            else:
                continue
            if safe_reference(reference):
                return reference
        return None

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
    #: The exit condition, one of `telemetry.STOP_REASONS`.
    stop_reason: str | None = None
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


def safe_reference(reference: str) -> bool:
    """Whether a change-request reference may be put into a stage's prompt."""
    return (bool(reference) and len(reference) <= 128
            and all(char.isalnum() or char in "#._:/-" for char in reference))


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
            *, change_request_id: str | None = None,
            local_only: bool = False) -> str:
    """The prompt for one stage. Named skill, named work item, nothing implied."""
    skill = SKILL_FOR_ROLE.get(role)
    if skill is None:
        raise CycleDriverError(f"no skill is defined for the role {role!r}")
    if role in {"review", "resolve", "rereview"} and change_request_id:
        parts = [f"Run {skill} for change request `{change_request_id}` in "
                 f"{repo_id} (work item {task_id})."]
    else:
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


def validate_start(start_from: str, change_request_id: str | None,
                   *, local_only: bool = False) -> None:
    """Refuse a resume that cannot be honest, before anything is dispatched.

    A resumed cycle works on a change request somebody names: without one there
    is nothing to review, and guessing it from the work item would be a guess.
    A local-only run creates no change request, so it has nothing to resume.
    """
    if start_from not in CYCLE_STARTS:
        raise CycleDriverError(
            f"--from must be one of {', '.join(CYCLE_STARTS)}, not {start_from!r}")
    if start_from == "implement":
        if change_request_id is not None:
            raise CycleDriverError(
                "--pr names an existing change request; it needs --from "
                "review, resolve or rereview")
        return
    if not change_request_id:
        raise CycleDriverError(f"--from {start_from} requires --pr")
    if not safe_reference(change_request_id):
        raise CycleDriverError(
            "--pr must be a change-request reference: letters, digits and #._:/-")
    if local_only:
        raise CycleDriverError(
            f"--local-only stops after implementation; it cannot resume from {start_from}")


def check_change_request(repo_id: str, change_request_id: str, cwd: str | None,
                         *, run=subprocess.run) -> None:
    """Refuse to resume a change request that is not there to work on.

    A resume trusts the pull request's comments to carry the earlier review,
    so it has to be the pull request this worktree is on: open, with its head
    branch still present, checked out in `cwd`, and at the pull request's head
    commit. A matching branch name alone is not enough: a checkout behind or
    ahead of the pull request would be reviewed or fixed as if it were the
    pull request. Each is read, not assumed,
    and the first that fails is named. Only GitHub pull requests can be read.
    """
    def call(command: list[str], what: str) -> str:
        try:
            done = run(command, capture_output=True, text=True, check=False,
                       stdin=subprocess.DEVNULL, cwd=cwd or None)
        except OSError as error:
            raise CycleDriverError(f"could not {what}: {error}") from error
        if done.returncode != 0:
            raise CycleDriverError(
                f"could not {what}: {readable(done.stderr or done.stdout, 200)}")
        return done.stdout.strip()

    reference = change_request_id.lstrip("#")
    raw = call(["gh", "pr", "view", reference, "--repo", repo_id, "--json",
                "state,headRefName,headRefOid,headRepository,headRepositoryOwner"],
               f"read pull request {change_request_id} in {repo_id}")
    try:
        pull = json.loads(raw)
    except ValueError as error:
        raise CycleDriverError(
            f"pull request {change_request_id} could not be read") from error
    state = str(pull.get("state") or "").upper()
    if state != "OPEN":
        raise CycleDriverError(
            f"pull request {change_request_id} is {state or 'in an unknown state'}, not open")
    branch = pull.get("headRefName")
    if not isinstance(branch, str) or not branch:
        raise CycleDriverError(f"pull request {change_request_id} names no head branch")
    head = pull.get("headRefOid")
    if not isinstance(head, str) or not head:
        raise CycleDriverError(f"pull request {change_request_id} names no head commit")
    owner = (pull.get("headRepositoryOwner") or {}).get("login")
    name = (pull.get("headRepository") or {}).get("name")
    head_repo = f"{owner}/{name}" if owner and name else repo_id
    call(["gh", "api", f"repos/{head_repo}/branches/{branch}", "--silent"],
         f"find the head branch {branch} of pull request {change_request_id}")
    current = call(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                   "read the branch checked out in the working directory")
    if current != branch:
        raise CycleDriverError(
            f"the working directory is on {current}, not on {branch}, the head "
            f"branch of pull request {change_request_id}")
    checked_out = call(["git", "rev-parse", "HEAD"],
                       "read the commit checked out in the working directory")
    if checked_out != head:
        raise CycleDriverError(
            f"the working directory is at {checked_out[:12]}, not at {head[:12]}, "
            f"the head commit of pull request {change_request_id}; update it "
            "to the pull request's head before resuming")


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
    change_bases: tuple[str, ...] | None = None,
    verification_available: bool | None = None,
    jev: JevConfig | None = None,
    shadow: JevShadow | None = None,
    verbose: bool = False,
    progress_interval: float = 60,
    start_from: str = "implement",
    change_request_id: str | None = None,
) -> CycleReport:
    """implement -> review -> (resolve -> rereview)*, every stage recorded.

    `start_from` resumes an existing change request, `change_request_id`,
    instead: `review` runs a fresh initial review, `resolve` enters the loop
    at its resolution and `rereview` at its re-review. The stages before it
    are skipped, not recorded, and the loop keeps the same routing, recording
    and iteration limit. It is a new cycle, and every row says where it began.

    With `change_bases`, each stage routed after the implementation is routed
    knowing the diff against the first base that resolves. Without them, the
    change signals are unknown and left out of every row.

    With `jev` in shadow mode, `implement` and `resolve` stages also record
    Jev's suggestion beside the rules' choice; the rules still route every
    stage. `shadow` injects an already-built observer instead, for tests.
    """
    validate_start(start_from, change_request_id, local_only=local_only)
    if shadow is None and jev is not None and jev.enabled:
        shadow = JevShadow(jev)
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
        started_from=start_from,
        routing_strategy=routing_strategy,
        change_observer=(
            (lambda: collect_change_signals(cwd, change_bases))
            if change_bases else None
        ),
        verification_available=verification_available,
        shadow=shadow,
    )
    status_writer = CycleStatusWriter(
        telemetry.path, recorder.cycle_id, repo_id, task_id,
        progress_interval=progress_interval, verbose=verbose,
    )
    recorder.stage_started = status_writer.stage_started
    recorder.on_progress = status_writer.activity
    status_writer.start()
    report = CycleReport(repo_id=repo_id, task_id=task_id)
    dispatch_kwargs = {}
    if cwd:
        dispatch_kwargs["cwd"] = cwd
    if timeout is not None:
        dispatch_kwargs["timeout"] = timeout

    def run(role: str, instruction: str = "", *,
            change_request_id: str | None = None) -> tuple[StageOutcome, Reported]:
        outcome = recorder.stage(
            role, compose(role, repo_id, task_id, instruction,
                          change_request_id=change_request_id, local_only=local_only),
                                 **dispatch_kwargs)
        report.stages.append(outcome)
        reported = read_structured_result(outcome.result)
        status_writer.stage_finished(outcome.result, reported.status)
        return outcome, reported

    def stop(because: str, status: str = UNRESOLVED_END,
             reported: Reported | None = None, *,
             stop_reason: str = "stage_not_completed") -> CycleReport:
        report.stopped_because = because
        report.status = status
        report.iterations = recorder.iteration
        report.stop_reason = stop_reason
        if reported is not None:
            report.reason = reported.reason
        recorder.close(status, stop_reason=stop_reason)
        status_writer.finish(status)
        return report

    def advance(role: str, instruction: str = "", *,
                change_request_id: str | None = None) -> tuple[Reported, CycleReport | None]:
        """Run one stage and decide whether the cycle may continue past it.

        Every reason to stop is here rather than repeated per stage: a stop
        condition that has to be remembered four times is one that will be
        missing from the fourth.
        """
        outcome, reported = run(role, instruction, change_request_id=change_request_id)
        if not outcome.succeeded:
            return reported, stop(_why(outcome), reported=reported,
                                  stop_reason="dispatch_failed")
        if _started_elsewhere(outcome):
            return reported, stop(_elsewhere(outcome), reported=reported)
        # The dispatch and the work are different facts. This records what the
        # agent said it did; the row already says the call returned. The prose
        # beside it is not recorded: the store holds references and counts.
        if reported.status:
            recorder.record_verdict(role, reported.status,
                                    **_findings(reported.payload, role),
                                    **_tests(reported.payload),
                                    **_checks(reported.payload))
        if not reported.completes(role):
            return reported, stop(reported.explain(role), reported=reported)
        # Only a completed stage enters the history the ladder reads. The
        # cycle already stops on anything else, so an unreadable result can
        # neither reset nor advance a count.
        run_statuses = _run_statuses(role, reported.payload)
        if run_statuses is not None:
            history.append(run_statuses)
        if role in {"review", "rereview"}:
            progress.append(_progress_key(role, reported.payload))
        return reported, None

    # What the ladder and the no-progress guard read: every completed run in
    # this cycle, and each review's head and open set. A resumed cycle starts
    # with neither, so it can stop later than a whole cycle would, never
    # earlier. `recorder.repeated_findings` stays unknown until the ladder is
    # first evaluated: a cycle that never reached it was not measured at zero.
    history: list[RunStatuses] = []
    progress: list[tuple[str, frozenset[str]] | None] = []

    reported = Reported()
    verdict = None
    if start_from == "implement":
        reported, stopped = advance("implement")
        if stopped is not None:
            return stopped
        if local_only:
            return stop(
                "local-only run stops after implementation; no change request was created for review",
                UNRESOLVED_END,
                reported=reported,
                stop_reason="local_only",
            )

        change_request_id = reported.change_request_id
        if change_request_id is None:
            return stop(
                "implement completed without change_request_id or legacy pr_number; "
                "stopping before review",
                reported=reported,
            )

    if start_from in {"implement", "review"}:
        reported, stopped = advance("review", change_request_id=change_request_id)
        if stopped is not None:
            return stopped
        verdict = reported.status
        report.verdict = verdict
    elif start_from == "rereview":
        reported, stopped = advance("rereview", "Re-review the change after the fixes.",
                                    change_request_id=change_request_id)
        if stopped is not None:
            return stopped
        verdict = reported.status
        report.verdict = verdict
    else:
        # Resuming at the resolution: the change request's own comments carry
        # the review it resolves, and the stage recovers those findings. The
        # verdict is what this cycle acts on, not one it observed, so the
        # report's verdict stays unset until a re-review gives one.
        verdict = "CHANGES_REQUESTED"

    while verdict == "CHANGES_REQUESTED":
        survivals = claimed_fix_survivals(history)
        recorder.repeated_findings = len(survivals)
        repeated = sorted(finding for finding, count in survivals.items()
                          if count >= SURVIVALS_BEFORE_STOP)
        if repeated:
            return stop(
                f"{', '.join(repeated)} survived {SURVIVALS_BEFORE_STOP} claimed "
                "fixes; stopping before another resolution",
                reported=reported, stop_reason="repeated_findings")
        if len(progress) >= 2 and progress[-1] is not None and progress[-1] == progress[-2]:
            return stop(
                "the re-review found the same head and the same open findings "
                "as the previous review",
                reported=reported, stop_reason="no_progress")
        if recorder.iteration >= max_iterations:
            break
        recorder.next_iteration()

        instruction = "Resolve the findings from the review."
        contested = sorted(finding for finding, count in survivals.items()
                           if count >= SURVIVALS_BEFORE_DIRECTIVE)
        if contested:
            instruction = (f"{instruction} "
                           f"{CONTESTED_DIRECTIVE.format(ids=', '.join(contested))}")
        _, stopped = advance("resolve", instruction,
                             change_request_id=change_request_id)
        if stopped is not None:
            return stopped

        reported, stopped = advance("rereview", "Re-review the change after the fixes.",
                                    change_request_id=change_request_id)
        if stopped is not None:
            return stopped
        verdict = reported.status
        report.verdict = verdict

    # The last stage's own account travels with every ending, not only the bad
    # ones: a run that finished still said something about how.
    if verdict == "APPROVED":
        return stop("", APPROVED_END, reported=reported, stop_reason="approved")
    return stop(f"still {verdict} after {recorder.iteration} round(s)",
                reported=reported, stop_reason="iteration_limit")


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


SEVERITIES = ("critical", "high", "medium", "low")

#: The finding statuses each role's structured result may publish, and the
#: contract status each one reads as. `still_open` is the re-review's word for
#: a previous finding it reopened.
ROLE_FINDING_STATUSES = {
    "review": {"open": "open", "resolved": "resolved",
               "not_applicable": "not_applicable"},
    "rereview": {"open": "open", "still_open": "open", "resolved": "resolved",
                 "not_applicable": "not_applicable"},
    "resolve": {"open": "open", "resolved": "resolved",
                "not_applicable": "not_applicable"},
}


def _finding_statuses(payload: dict | None, role: str) -> dict[str, str] | None:
    """The last status each finding ended a stage's result in, or None.

    None means the result carries no readable finding list: an entry without a
    finding ID or with a status outside the role's vocabulary makes the whole
    list unreadable rather than silently shorter, because a shorter list could
    look like progress. A later entry for an ID overrides an earlier one, as the
    last header of a comment does.
    """
    if not payload:
        return None
    if role == "review":
        keys = ("findings",)
    elif role == "rereview":
        keys = ("verified_findings", "new_findings")
    elif role == "resolve":
        keys = (("finding_outcomes",) if "finding_outcomes" in payload
                else ("resolved_findings", "unresolved_findings"))
    else:
        return None
    lists = [payload.get(key) for key in keys if key in payload]
    if not lists or any(not isinstance(entries, list) for entries in lists):
        return None
    vocabulary = ROLE_FINDING_STATUSES[role]
    statuses: dict[str, str] = {}
    for entries in lists:
        for item in entries:
            if not isinstance(item, dict):
                return None
            finding_id, status = item.get("id"), item.get("status")
            # The type first: a JSON list or object is unhashable, and a
            # membership test on it would raise instead of reading as unknown.
            if (not isinstance(finding_id, str) or FINDING_ID.fullmatch(finding_id) is None
                    or not isinstance(status, str) or status not in vocabulary):
                return None
            statuses[finding_id] = vocabulary[status]
    return statuses


def _run_statuses(role: str, payload: dict | None) -> RunStatuses | None:
    """One completed stage as one run of `claimed_fix_survivals`.

    A review whose findings cannot be read is still the next review after a
    claim: it consumes the claim without rejecting it. A resolution whose
    findings cannot be read claims nothing.
    """
    statuses = _finding_statuses(payload, role)
    if role in {"review", "rereview"}:
        return RunStatuses(REVIEW_RUN, statuses or {})
    if role == "resolve" and statuses is not None:
        return RunStatuses(RESOLUTION_RUN, statuses)
    return None


def _progress_key(role: str, payload: dict | None) -> tuple[str, frozenset[str]] | None:
    """A review's head and open finding set, or None when either is unknown.

    Unknown never matches, so the no-progress guard fires only on two reviews
    that both reported the same head and the same readable open set.
    """
    head = payload.get("head_sha") if payload else None
    if not isinstance(head, str) or HEAD_SHA.fullmatch(head) is None:
        return None
    statuses = _finding_statuses(payload, role)
    if statuses is None:
        return None
    return head, frozenset(finding for finding, status in statuses.items()
                           if status == "open")


def _findings(payload: dict | None, role: str) -> dict:
    """Count the findings documented for this stage. Absent is never zero."""
    if not payload:
        return {}
    if role == "review":
        candidates = payload.get("findings")
        if not isinstance(candidates, list):
            return {}
        if _valid_findings(candidates, {"open", "resolved", "not_applicable"}) is None:
            return {}
        findings = [item for item in candidates if item.get("status") == "open"]
    elif role == "rereview":
        new_findings = payload.get("new_findings")
        verified_findings = payload.get("verified_findings")
        if (("new_findings" in payload and not isinstance(new_findings, list))
                or ("verified_findings" in payload
                    and not isinstance(verified_findings, list))):
            return {}
        if not isinstance(new_findings, list) and not isinstance(verified_findings, list):
            return {}
        new_findings = new_findings if isinstance(new_findings, list) else []
        verified_findings = (verified_findings
                             if isinstance(verified_findings, list) else [])
        if _valid_findings(new_findings, {"open", "resolved", "not_applicable"}) is None:
            return {}
        if _valid_findings(
                verified_findings,
                {"still_open", "open", "resolved", "not_applicable"}) is None:
            return {}
        all_findings = _unique_findings(new_findings + verified_findings)
        if all_findings is None:
            return {}
        findings = [item for item in all_findings
                    if item.get("status") in {"open", "still_open"}]
    elif role == "resolve":
        findings = payload.get("unresolved_findings")
        if (not isinstance(findings, list)
                or any(not isinstance(item, dict) for item in findings)):
            return {}
    else:
        return {}
    # A malformed entry or conflicting duplicate makes the count unknown. In
    # particular, dropping a finding with no status would turn a reported
    # review issue into a false zero.
    if findings is None:
        return {}

    out = {"findings_total": len(findings)}
    blocking = payload.get("blocking_findings")
    if isinstance(blocking, list):
        if any(not isinstance(identifier, str) for identifier in blocking):
            return {}
        if not _blocking_findings_consistent(findings, blocking):
            return {}
        out["findings_blocking"] = len(set(blocking))
    else:
        out["findings_blocking"] = sum(
            1 for finding in findings
            if isinstance(finding, dict) and finding.get("blocks_approval") is True
        )
    severities = [
        finding.get("severity") if isinstance(finding, dict) else None
        for finding in findings
    ]
    # A partial breakdown would read as zero for severities it missed.
    if all(severity in SEVERITIES for severity in severities):
        for severity in SEVERITIES:
            out[f"findings_{severity}"] = severities.count(severity)
    return out


def _valid_findings(entries: list, statuses: set[str]) -> list | None:
    """Validate entry shapes and statuses, or return None when ambiguous."""
    for item in entries:
        if not isinstance(item, dict):
            return None
        status = item.get("status")
        if not isinstance(status, str) or status not in statuses:
            return None
    return entries


def _unique_findings(findings: list) -> list | None:
    """Deduplicate identical IDs and reject duplicate IDs with conflicting data."""
    unique = {}
    anonymous = []
    for finding in findings:
        identifier = finding.get("id")
        if not isinstance(identifier, str) or not identifier:
            anonymous.append(finding)
            continue
        previous = unique.get(identifier)
        if previous is None:
            unique[identifier] = finding
        elif any(previous.get(key) != finding.get(key)
                 for key in ("status", "severity", "blocks_approval")):
            return None
    return list(unique.values()) + anonymous


def _blocking_findings_consistent(findings: list, blocking: list[str]) -> bool:
    """Reject conflicting blocker signals when every finding is classifiable."""
    if any(not isinstance(item, dict) for item in findings):
        return False
    if not all(isinstance(item.get("id"), str)
               and isinstance(item.get("blocks_approval"), bool)
               for item in findings):
        return True
    by_id = {}
    for finding in findings:
        identifier = finding["id"]
        value = finding["blocks_approval"]
        if identifier in by_id and by_id[identifier] != value:
            return False
        by_id[identifier] = value
    expected = {identifier for identifier, blocks in by_id.items() if blocks}
    return expected == set(blocking)


def _tests(payload: dict | None) -> dict:
    """Whether the stage reported its tests passing. Absent is not passing.

    Only a boolean `tests.passed` counts: anything else is a report nobody can
    read as a result, and recording it as one would invent a test outcome.

    Nor does a `passed` that describes tests nobody ran. `tests.ran: false`
    says so outright. A `BLOCKED` stage reporting `passed: false` without
    claiming `ran: true` is read the same way: a skill that stopped before
    touching code has no failure to report, and counting its `false` would
    turn "never ran" into "failed". A `true` needs no such claim; nothing
    passes without running.
    """
    tests = payload.get("tests") if payload else None
    if not isinstance(tests, dict) or not isinstance(tests.get("passed"), bool):
        return {}
    if tests.get("ran") is False:
        return {}
    if (tests["passed"] is False and tests.get("ran") is not True
            and _status_of(payload) == "BLOCKED"):
        return {}
    return {"tests_passed": tests["passed"]}


CHECK_COUNTS = ("passed", "failed", "pending")


def _checks(payload: dict | None) -> dict:
    """The code-host checks of the head the stage finished on. Absent is absent.

    A count is recorded only when the stage reported all three, each a
    non-negative integer: a partial report would read the missing states as
    zero, and "no pending check" is exactly what a stage that stopped waiting
    must not be taken to have said. Nor are checks that ran on another head
    this stage's: when both the result and its `checks` name a head, they must
    agree, or the counts describe a commit the stage did not leave behind.
    """
    checks = payload.get("checks") if payload else None
    if not isinstance(checks, dict):
        return {}
    counts = {state: checks.get(state) for state in CHECK_COUNTS}
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
               for value in counts.values()):
        return {}
    ran_on, head = checks.get("head_sha"), payload.get("head_sha")
    if isinstance(ran_on, str) and isinstance(head, str) and ran_on != head:
        return {}
    return {f"checks_{state}": value for state, value in counts.items()}


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
    parser.add_argument("--verification", choices=("available", "unavailable"),
                        default=None,
                        help=("whether the change can be verified automatically; "
                              "recorded as a pre-routing signal, unknown when omitted"))
    parser.add_argument("--mode", choices=("production", "calibration"),
                        default="production")
    parser.add_argument("--from", dest="start_from", choices=CYCLE_STARTS,
                        default="implement",
                        help=("stage to start at; anything but implement resumes "
                              "the change request named by --pr (default: implement)"))
    parser.add_argument("--pr", dest="change_request", default=None,
                        help="existing change request a resumed cycle works on")
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--cwd", default=None,
                        help="working directory the executor runs in")
    parser.add_argument("--local-only", action="store_true",
                        help=("run only in the explicit --cwd linked worktree; "
                              "stop after implementation without publishing"))
    parser.add_argument("--timeout", type=int, default=None,
                        help="seconds one dispatch may take")
    parser.add_argument("--verbose", action="store_true",
                        help="show stage starts, live progress, and stage results")
    parser.add_argument("--progress-interval", type=float, default=60,
                        help="seconds between live progress lines (default: 60)")
    parser.add_argument("--database", default=None,
                        help=f"telemetry database (default: {default_database_path()})")
    parser.add_argument("--config", default=None,
                        help=f"path to {CONFIG_NAME} (default: alongside the work)")
    parser.add_argument("--no-config", action="store_true",
                        help="run on the built-in defaults, ignoring any configuration")
    args = parser.parse_args(argv)
    if args.progress_interval <= 0:
        parser.error("--progress-interval must be greater than zero")

    try:
        validate_start(args.start_from, args.change_request, local_only=args.local_only)
        repo, profiles, routing_strategy = plan_with_strategy(args)
        config = resolve_config(args)
        if args.start_from in RESUMES:
            host = (config.get("code_cycle") or {}).get("code_host")
            if host not in (None, "github"):
                raise CycleDriverError(
                    f"resuming reads the pull request through GitHub; code_host "
                    f"{host} is not supported")
            check_change_request(repo, args.change_request, args.cwd)
    except CycleDriverError as error:
        parser.error(str(error))

    change_bases = change_bases_of(config)
    # Already validated by `load_config`; read here so the run carries it.
    jev = load_jev_config(config)
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
            verbose=args.verbose,
            progress_interval=args.progress_interval,
            local_only=args.local_only,
            change_bases=change_bases,
            verification_available=(
                None if args.verification is None
                else args.verification == "available"
            ),
            jev=jev,
            start_from=args.start_from,
            change_request_id=args.change_request,
        )
    except CycleDriverError as error:
        parser.error(str(error))

    print(report.explain())
    print(f"recorded in {telemetry.path}")
    return 0 if report.status == APPROVED_END else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
