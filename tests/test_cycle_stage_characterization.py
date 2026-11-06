"""Characterization of `CycleRecorder.stage()` (#187).

`stage()` routes, dispatches and records in one call. Each scenario below runs
it against scripted executors and captures everything it persists — every
telemetry row, every dispatch attempt and update, every journal entry — plus
the outcome it returns, the prompts and options each executor received, and
the recorder state it leaves. The capture is compared, field by field, with a
fixture recorded before `stage()` was split into helpers, so a refactor that
changes one recorded value fails here.

Only values that differ between two identical runs are normalized: generated
identifiers become ordinals in order of appearance, wall-clock timestamps
become `<timestamp>`, temporary paths become `<tmp>`, and the harness
components that hash the toolkit's own files are blanked. Everything else is
compared as recorded.

A deliberate change to what `stage()` records needs a new fixture: empty or
delete `tests/fixtures/cycle_stage_characterization.json`, run this module
once to record it (that run fails, so a recording never passes as a
comparison), run it again, and review the fixture diff, one line per scenario.
"""
from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import cycle as cy  # noqa: E402
import cycle_journal as journal  # noqa: E402
import executors as ex  # noqa: E402
import grants  # noqa: E402
import harness  # noqa: E402
import router  # noqa: E402
import stage_permissions as sp  # noqa: E402
import telemetry as tm  # noqa: E402
from environment_recovery import ENVIRONMENT_RECOVERY_GUIDANCE  # noqa: E402
from stage_signals import ChangeSignals  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "cycle_stage_characterization.json"

#: Harness components that hash the toolkit's own files or identify its commit.
#: They change with every edit to the runtime, `cycle.py` included.
VOLATILE_HARNESS = (
    "toolkit_release", "toolkit_commit", "runtime_manifest_sha256",
    "routing_policy_sha256", "prompt_template_sha256", "skill_sha256",
)

HEAD = "a" * 40
FINGERPRINT = "b" * 64
WORKSPACE = {"head": HEAD, "fingerprint": FINGERPRINT}
CLEAN = {"dirty": False, "workspace": WORKSPACE, "remote": "same",
         "pulls": [], "comments": []}
DIRTY = {**CLEAN, "dirty": True}

SANDBOX_BLOCKER = {
    "kind": "sandbox_local_access", "command": "docker info",
    "error": "sandbox denied access to /var/run/docker.sock",
    "attempted": ["checked existing execution capabilities; none permits host access"],
    "requires_human": False,
}

_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")


def block(status: str, **fields) -> str:
    values = {"skill": "fake", "status": status, **fields}
    return f"ORCHESTRATION_RESULT\n{json.dumps(values)}\nEND_ORCHESTRATION_RESULT"


def stable_harness_snapshot(**kwargs):
    """The real snapshot, minus the hashes of the toolkit's own files."""
    snapshot = harness.build_harness_snapshot(**kwargs)
    components = {**snapshot["components"], **dict.fromkeys(VOLATILE_HARNESS)}
    encoded = json.dumps(components, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    return {"harness_snapshot_id": f"harness-{fingerprint}",
            "fingerprint": fingerprint, "components": components}


def plain(value):
    """A JSON-compatible view of a dataclass, enum or container."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {spec.name: plain(getattr(value, spec.name))
                for spec in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {str(plain(key)) if not isinstance(key, tuple) else "/".join(map(str, key)):
                plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(plain(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if callable(value):
        return f"<callable {type(value).__name__}>"
    return f"<{type(value).__name__}>"


class Scripted(ex.Adapter):
    """An executor whose answers are the scenario's script."""

    provable_ceiling = ex.Availability.AUTHENTICATED
    enforces_read_only = True
    enforces_workspace_boundary = True

    def __init__(self, name, script=None, *, completes_work=True,
                 availability=ex.Availability.AUTHENTICATED):
        self.name = name
        self.availability = availability
        self.script = list(script or [])
        self.completes_work = completes_work
        self.dispatched: list[str] = []
        self.dispatch_kwargs: list[dict] = []

    def probe(self):
        return ex.ProbeResult(self.name, self.availability, "scripted",
                              provable_ceiling=self.provable_ceiling)

    def publication_access(self, probe, *, writes):
        return True, "scripted publication access"

    def dispatch(self, target, task, **kw):
        self.dispatched.append(task)
        self.dispatch_kwargs.append(kw)
        step = self.script.pop(0) if self.script else {}
        if callable(step):
            step = step(target, kw)
        if isinstance(step, BaseException):
            raise step
        progress = step.pop("progress", None) if isinstance(step, dict) else None
        if progress and kw.get("on_progress") is not None:
            kw["on_progress"](**progress)
        interrupt = step.pop("interrupt", None)
        if interrupt is not None:
            raise cy.CycleInterrupted(interrupt)
        fields = {"start_state": "started", "process_exited": True, **step}
        outcome = fields.pop("outcome", ex.DispatchOutcome.SUCCEEDED)
        if outcome is ex.DispatchOutcome.SUCCEEDED:
            fields.setdefault("model_resolved", target.model)
        return ex.DispatchResult(outcome, self.name, target, **fields)


class ProbeFailingCodex(Scripted):
    def probe_git_metadata_write(self, target, cwd, writable_dirs):
        return False, "worktree gitdir is read-only"


def codex_then_claude():
    """Every profile on Codex with a Claude fallback, so every path is reachable."""
    profiles = router.load_profiles()
    primary = router.parse_target("codex:openai/gpt-6-luna high")
    fallback = router.parse_target("claude:anthropic/claude-opus-5-5 high")
    return {name: router.Profile(profile.name, primary, fallback)
            for name, profile in profiles.items()}


class Run:
    """One scenario's world: a fresh store, a journal, and what was observed."""

    def __init__(self, root: Path):
        self.root = root
        self.store = tm.Telemetry(root / "telemetry.sqlite")
        self.adapters: list[Scripted] = []
        self.recorders: list[cy.CycleRecorder] = []
        self.events: list[list] = []
        self.snapshots: list = []

    def recorder(self, adapters, *, availability=None, cycle_id=None, **kw):
        self.adapters.extend(adapters)
        availability = availability or {a.name: ex.Availability.READY for a in adapters}
        index = len(self.recorders) + 1
        recorder = cy.CycleRecorder(
            self.store, "owner/repo", "API-187",
            router.TaskSignals(difficulty=2, verifiability="auto"),
            availability=availability, registry=ex.Registry(adapters),
            cycle_id=cycle_id or f"cycle-characterization-{index}",
            stage_started=lambda role, decision: self.events.append(
                ["stage_started", role, decision.profile,
                 decision.target.executor if decision.target else None]),
            on_progress=lambda **activity: self.events.append(["progress", plain(activity)]),
            **kw,
        )
        recorder.journal = journal.Journal(
            self.store.path, "owner/repo", "API-187", recorder.cycle_id,
            change_request_id=kw.get("change_request_id"))
        recorder.on_dispatch_started = lambda: self.events.append(["dispatch_started"])
        self.recorders.append(recorder)
        return recorder

    def stage(self, recorder, role, task="the work", **kw):
        try:
            outcome = recorder.stage(role, task, **kw)
        except cy.CycleInterrupted as interruption:
            return {"raised": "CycleInterrupted", "signal": interruption.signal_name,
                    "outcome": describe_outcome(interruption.outcome)}
        except Exception as error:
            return {"raised": type(error).__name__, "message": str(error)}
        return describe_outcome(outcome)

    def capture(self, results) -> dict:
        tables = {}
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.row_factory = sqlite3.Row
            names = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            for name in names:
                rows = [dict(row) for row in connection.execute(
                    f"SELECT * FROM {name} ORDER BY rowid")]
                if rows:
                    tables[name] = [{key: decode(value) for key, value in row.items()}
                                    for row in rows]
        captured = {
            "results": results,
            "tables": tables,
            "events": self.events,
            "snapshots": self.snapshots,
            "dispatches": [
                {"executor": adapter.name,
                 "tasks": [prompt(task) for task in adapter.dispatched],
                 "options": [options(kw) for kw in adapter.dispatch_kwargs]}
                for adapter in self.adapters if adapter.dispatched
            ],
            "recorders": [describe_recorder(recorder) for recorder in self.recorders],
        }
        return normalize(captured, str(self.root))


def decode(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def prompt(task: str) -> dict:
    """The prompt's fixed part by hash, and its per-attempt part verbatim.

    Everything before the environment recovery guidance is the task and the
    stage's publication policy; everything after it is what an attempt adds:
    the recovery context, the permission context and the routing line.
    """
    head, marker, tail = task.partition(ENVIRONMENT_RECOVERY_GUIDANCE)
    return {"fixed_sha256": hashlib.sha256(head.encode("utf-8")).hexdigest(),
            "guidance": bool(marker), "attempt": tail}


def options(kw: dict) -> dict:
    out = {}
    for key, value in sorted(kw.items()):
        if key in {"on_progress", "on_workspace", "on_dispatch_attempt"}:
            out[key] = value is not None
        elif key == "command_policy" and value is not None:
            out[key] = {"role": value.role,
                        "command_permissions": plain(value.command_permissions)}
        else:
            out[key] = plain(value)
    return out


def describe_decision(decision):
    if decision is None:
        return None
    return {
        "profile": decision.profile,
        "target": plain(decision.target),
        "used_fallback": decision.used_fallback,
        "blocked": decision.blocked,
        "reasons": list(decision.reasons),
    }


def describe_result(result):
    if result is None:
        return None
    described = plain(result)
    described["failure_code"] = result.failure_code
    return described


def describe_outcome(outcome):
    if outcome is None:
        return None
    return {
        "role": outcome.role,
        "decision": describe_decision(outcome.decision),
        "result": describe_result(outcome.result),
        # Each attempt's full result is in `dispatch_attempts`; this is the
        # outcome's own list of them, in order.
        "attempts": [[plain(decision.target), decision.used_fallback, result.outcome.value,
                      result.failure_code, result.artifacts.get("dispatch_attempt_id")]
                     for decision, result in outcome.attempts],
        "rows": outcome.rows,
        "rerouted": outcome.rerouted,
        "succeeded": outcome.succeeded,
    }


def describe_recorder(recorder):
    return {
        "failed_attempts": recorder.failed_attempts,
        "stage_seq": recorder.stage_seq,
        "latest_seq": dict(recorder._latest_seq),
        "stages": [stage.role for stage in recorder.stages],
        "exhausted_until": plain(recorder.exhausted_until),
        "latest_change": plain(recorder.latest_change),
        "consent": None if recorder.consent is None else {
            "pending": [plain(request.record()) for request in recorder.consent.pending],
            "denied": sorted(recorder.consent.denied_ids),
        },
        "journal_errors": recorder.journal.errors if recorder.journal else None,
    }


def normalize(value, root: str):
    ids: dict[str, str] = {}

    def text(item: str) -> str:
        if root in item:
            # The same path on every platform.
            item = item.replace(root, "<tmp>").replace("\\", "/")
        item = _TIMESTAMP.sub("<timestamp>", item)
        return _ID.sub(lambda match: ids.setdefault(match.group(0), f"<id-{len(ids) + 1}>"), item)

    def walk(item):
        if isinstance(item, str):
            return text(item)
        if isinstance(item, dict):
            # Wall time of a real call: whether it was measured is the
            # characterized fact, not how long it took.
            # A patch's hash covers its `finished_at`; the patch is compared.
            return {text(str(key)): ("<ms>" if key == "duration_ms" and isinstance(child, int)
                                     else "<patch hash>" if key == "patch_hash" and child
                                     else walk(child))
                    for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [walk(child) for child in item]
        return item

    return walk(json.loads(json.dumps(value, default=repr)))


# --- scenarios ---------------------------------------------------------------
#
# Each takes a `Run` and returns the list of results of the calls it made.


def implement_succeeds(run):
    recorder = run.recorder([Scripted("codex", [{"progress": {"tool": "Bash"}}]),
                             Scripted("claude")])
    return [run.stage(recorder, "implement")]


def full_cycle_completes(run):
    """implement -> review -> resolve -> rereview, verdicts, CI and close."""
    codex, claude = Scripted("codex"), Scripted("claude")
    recorder = run.recorder(
        [codex, claude], change_request_id="384",
        change_observer=lambda: ChangeSignals(changed_files_count=2, has_tests=True),
        skills_by_role={"implement": "cc-implement-issue", "review": "cc-initial-review"})
    results = [run.stage(recorder, "implement")]
    results.append(recorder.record_verdict("implement", "IMPLEMENTED"))
    results.append(run.stage(recorder, "review"))
    results.append(recorder.record_verdict("review", "CHANGES_REQUESTED",
                                           findings_total=2, findings_blocking=1,
                                           findings_high=1, findings_low=1))
    results.append(recorder.next_iteration())
    results.append(run.stage(recorder, "resolve"))
    results.append(recorder.record_verdict("resolve", "RESOLVED"))
    results.append(run.stage(recorder, "rereview"))
    results.append(recorder.record_verdict("rereview", "APPROVED", reviewed_head_sha=HEAD,
                                           ))
    results.append(recorder.close("READY_FOR_MANUAL_MERGE"))
    return results


def local_only_review_is_escalated(run):
    recorder = run.recorder([Scripted("codex"), Scripted("claude")], local_only=True)
    return [run.stage(recorder, "review", escalated=True),
            run.stage(recorder, "implement")]


def routing_is_blocked(run):
    recorder = run.recorder(
        [Scripted("codex"), Scripted("claude")],
        availability={"codex": ex.Availability.INSTALLED,
                      "claude": ex.Availability.INSTALLED})
    return [run.stage(recorder, "implement")]


def quota_blocked_primary_reroutes_once(run):
    recorder = run.recorder([
        Scripted("codex", [{"outcome": ex.DispatchOutcome.BLOCKED,
                            "missing_capability": "operating_quota",
                            "start_state": "not_started"}]),
        Scripted("claude")])
    return [run.stage(recorder, "implement")]


def capacity_reroutes_and_the_fallback_fails(run):
    failure = {"outcome": ex.DispatchOutcome.FAILED, "error_code": "capacity",
               "start_state": "not_started", "detail": "overloaded"}
    recorder = run.recorder([Scripted("codex", [dict(failure)]),
                             Scripted("claude", [{**failure, "error_code": "transport"}])])
    return [run.stage(recorder, "review")]


def capacity_without_exit_proof_stops(run):
    recorder = run.recorder([
        Scripted("codex", [{"outcome": ex.DispatchOutcome.FAILED, "error_code": "timeout",
                            "process_exited": False, "detail": "no exit"}]),
        Scripted("claude")])
    return [run.stage(recorder, "implement")]


def started_writer_reconciles_before_rerouting(run):
    codex = Scripted("codex", [{"outcome": ex.DispatchOutcome.FAILED,
                                "error_code": "capacity", "detail": "overloaded"}])
    recorder = run.recorder([codex, Scripted("claude")])
    run.snapshots.append("clean before and after")
    with patch("cycle.effect_snapshot", return_value=CLEAN):
        return [run.stage(recorder, "implement")]


def started_writer_with_changed_workspace_stops(run):
    codex = Scripted("codex", [{"outcome": ex.DispatchOutcome.FAILED,
                                "error_code": "capacity", "detail": "overloaded"}])
    recorder = run.recorder([codex, Scripted("claude")])
    with patch("cycle.effect_snapshot", side_effect=lambda *a, **k:
               CLEAN if not codex.dispatched else DIRTY):
        return [run.stage(recorder, "implement")]


def quota_with_dirty_workspace_records_a_handoff_then_resumes(run):
    codex = Scripted("codex", [{"outcome": ex.DispatchOutcome.FAILED, "error_code": "quota",
                                "detail": "quota exhausted; reset at 9:46 PM"}])
    first = run.recorder([codex, Scripted("claude")], change_request_id="384")
    with patch("cycle.effect_snapshot", side_effect=lambda *a, **k:
               CLEAN if not codex.dispatched else DIRTY):
        results = [run.stage(first, "resolve")]
    resumed = run.recorder([Scripted("codex"), Scripted("claude")], change_request_id="384",
                           started_from="resolve", cycle_id="cycle-characterization-resume")
    with ExitStack() as stack:
        stack.enter_context(patch("cycle.effect_snapshot", return_value=DIRTY))
        stack.enter_context(patch("executors._git_output", return_value=" M one.py"))
        stack.enter_context(patch("executors._workspace_fingerprint", return_value=WORKSPACE))
        results.append(run.stage(resumed, "resolve", cwd=str(run.root)))
    return results


def quota_without_reset_time_stops(run):
    recorder = run.recorder([
        Scripted("codex", [{"outcome": ex.DispatchOutcome.FAILED, "error_code": "quota",
                            "process_exited": False, "detail": "quota exhausted"}]),
        Scripted("claude")])
    return [run.stage(recorder, "implement")]


def success_clears_a_recorded_quota_exhaustion(run):
    run.store.record_quota_exhaustion("owner/repo", "claude", "claude-opus-5-5", "quota exhausted")
    recorder = run.recorder([Scripted("claude")],
                            availability={"claude": ex.Availability.READY},
                            profiles={name: router.Profile(
                                p.name, router.parse_target("claude:anthropic/claude-opus-5-5 high"),
                                None) for name, p in router.load_profiles().items()})
    run.store.clear_quota_exhaustion("owner/repo", "claude", "claude-opus-5-5")
    run.store.record_quota_exhaustion("owner/repo", "claude", "other-model", "quota exhausted")
    return [run.stage(recorder, "implement")]


def calibration_never_reroutes(run):
    ready = ex.Availability.READY
    recorder = run.recorder(
        [Scripted("codex", [{"outcome": ex.DispatchOutcome.FAILED, "error_code": "capacity",
                             "start_state": "not_started"}], availability=ready),
         Scripted("claude", availability=ready)],
        mode=router.RoutingMode.CALIBRATION)
    return [run.stage(recorder, "implement")]


def calibration_refuses_an_unproven_executor(run):
    recorder = run.recorder([Scripted("codex"), Scripted("claude")],
                            mode=router.RoutingMode.CALIBRATION)
    return [run.stage(recorder, "implement")]


def human_action_is_not_rerouted(run):
    recorder = run.recorder([
        Scripted("codex", [{"outcome": ex.DispatchOutcome.BLOCKED,
                            "missing_capability": "authenticated_session",
                            "start_state": "not_started"}]),
        Scripted("claude")])
    return [run.stage(recorder, "implement")]


def nontransient_failure_is_not_rerouted(run):
    recorder = run.recorder([
        Scripted("codex", [{"outcome": ex.DispatchOutcome.FAILED, "error_code": "auth",
                            "detail": "token expired"}]),
        Scripted("claude")])
    return [run.stage(recorder, "review")]


def asynchronous_launch_is_recorded_launched(run):
    recorder = run.recorder([
        Scripted("codex", [{"asynchronous": True, "process_exited": False,
                            "artifacts": {"dispatchId": "orca-dispatch-187"}}],
                 completes_work=False)])
    return [run.stage(recorder, "implement")]


def interruption_records_the_stage_and_raises(run):
    recorder = run.recorder([
        Scripted("codex", [{"progress": {"tool": "Edit"}, "interrupt": "SIGTERM"}]),
        Scripted("claude")])
    with patch.object(cy, "time", SimpleNamespace(monotonic=lambda: 50.0)):
        return [run.stage(recorder, "implement")]


def executor_exception_propagates(run):
    recorder = run.recorder([Scripted("codex", [RuntimeError("adapter crashed")]),
                             Scripted("claude")])
    return [run.stage(recorder, "implement")]


def stage_contract_mismatches_are_refused(run):
    recorder = run.recorder([Scripted("codex"), Scripted("claude")])
    return [
        run.stage(recorder, "review", writes=True),
        run.stage(recorder, "review", publishes=False),
        run.stage(recorder, "implement", publication_permissions=("comment",)),
        run.stage(recorder, "implement", workspace_policy="nonsense"),
        run.stage(recorder, "implement", workspace_policy="read_only"),
        run.stage(recorder, "resolve", effort_override="extreme"),
    ]


def resolve_pins_effort_and_escalates(run):
    codex, claude = Scripted("codex"), Scripted("claude")
    recorder = run.recorder([codex, claude], resolve_effort="medium")
    target = router.parse_target("claude:anthropic/claude-opus-5-5 high")
    return [
        run.stage(recorder, "resolve"),
        run.stage(recorder, "resolve", resolve_escalated=True,
                  escalation_finding_ids=("REV-001", "REV-002")),
        run.stage(recorder, "resolve", target_override=target,
                  target_override_used_fallback=True, resolve_escalated=True,
                  escalation_finding_ids=("REV-003",)),
    ]


def resolve_override_unavailable_or_exhausted(run):
    run.store.record_quota_exhaustion("owner/repo", "claude", "claude-opus-5-5", "quota exhausted")
    recorder = run.recorder([Scripted("codex"), Scripted("claude")])
    exhausted = router.parse_target("claude:anthropic/claude-opus-5-5 high")
    missing = router.parse_target("orca:openai/gpt-6-luna high")
    return [run.stage(recorder, "resolve", target_override=exhausted),
            run.stage(recorder, "resolve", target_override=missing)]


def resolve_escalation_failure_keeps_acceptance_unknown(run):
    recorder = run.recorder(
        [Scripted("codex", [{"outcome": ex.DispatchOutcome.FAILED, "error_code": "auth"}]),
         Scripted("claude")],
        profiles=codex_then_claude())
    return [run.stage(recorder, "resolve", resolve_escalated=True,
                      escalation_finding_ids=("REV-004",))]


def resolve_unsupported_effort_uses_the_fallback(run):
    recorder = run.recorder(
        [Scripted("codex", [{"outcome": ex.DispatchOutcome.FAILED,
                             "error_code": "unsupported_effort",
                             "start_state": "not_started"}]),
         Scripted("claude")],
        profiles=codex_then_claude())
    return [run.stage(recorder, "resolve", resolve_escalated=True,
                      escalation_finding_ids=("REV-005",))]


def resolve_unsupported_effort_without_fallback_stops(run):
    profiles = {name: router.Profile(p.name, router.parse_target("codex:openai/gpt-6-luna high"),
                                     None)
                for name, p in router.load_profiles().items()}
    recorder = run.recorder(
        [Scripted("codex", [{"outcome": ex.DispatchOutcome.FAILED,
                             "error_code": "unsupported_effort"}])],
        profiles=profiles)
    return [run.stage(recorder, "resolve")]


def codex_catalog_rejects_max_effort(run):
    catalog = {"gpt-6-luna": ("low", "medium", "high")}
    results = []
    with patch("profile_config.codex_models", return_value=catalog):
        recorder = run.recorder([Scripted("codex"), Scripted("claude")],
                                profiles=codex_then_claude())
        results.append(run.stage(recorder, "resolve", resolve_escalated=True,
                                 escalation_finding_ids=("REV-006",)))
        lonely = run.recorder([Scripted("codex")], profiles=codex_then_claude())
        results.append(run.stage(lonely, "resolve", resolve_escalated=True))
    with patch("profile_config.codex_models", side_effect=OSError("no catalog")):
        recorder = run.recorder([Scripted("codex"), Scripted("claude")],
                                profiles=codex_then_claude())
        results.append(run.stage(recorder, "resolve", resolve_escalated=True))
    return results


def codex_git_metadata_probe_routes_elsewhere(run):
    recorder = run.recorder([ProbeFailingCodex("codex"), Scripted("claude")])
    return [run.stage(recorder, "implement", cwd=str(run.root),
                      writable_dirs=(str(run.root / ".git"),))]


def environment_restriction_keeps_the_executor(run):
    recorder = run.recorder([
        Scripted("codex", [{"outcome": ex.DispatchOutcome.FAILED,
                            "error_code": "environment_restricted",
                            "start_state": "not_started", "detail": "sandbox refused"}]),
        Scripted("claude")])
    return [run.stage(recorder, "implement")]


def failed_native_preflight_is_a_known_prerequisite(run):
    recorder = run.recorder([
        Scripted("codex", [{"outcome": ex.DispatchOutcome.FAILED,
                            "error_code": "environment_restricted",
                            "start_state": "not_started", "detail": "native probe timed out",
                            "artifacts": {"native_sandbox_preflight": {
                                "status": "unverified", "missing": ["rg"],
                                "probes": [{"name": "rg", "outcome": "not_verified",
                                            "error": "rg: not found"}],
                                "functional_check": {"probe": "bwrap", "passed": False,
                                                     "evidence": "namespace denied"}}}}]),
        Scripted("claude")])
    return [run.stage(recorder, "implement")]


def known_prerequisite_blocker_is_not_rerouted(run):
    blocker = {**SANDBOX_BLOCKER, "command": "srt -- npm ci", "error": "srt: not found"}
    recorder = run.recorder([
        Scripted("codex", [{"agent_output": block("BLOCKED", environment_blocker=blocker)}]),
        Scripted("claude")])
    with patch("cycle.effect_snapshot", return_value=CLEAN):
        return [run.stage(recorder, "implement")]


def sandbox_blocker_without_an_answer_asks(run):
    consent = grants.ConsentBroker("owner/repo", platform="linux",
                                   store=grants.ConsentStore(run.root / "consents.json"))
    recorder = run.recorder([
        Scripted("codex", [{"agent_output": block("BLOCKED", environment_blocker=SANDBOX_BLOCKER)}]),
        Scripted("claude")], consent=consent)
    with patch("cycle.effect_snapshot", return_value=CLEAN):
        return [run.stage(recorder, "implement")]


def sandbox_blocker_answered_reruns_the_same_executor(run):
    for answer in ("cycle", "deny"):
        consent = grants.ConsentBroker("owner/repo", platform="linux",
                                       store=grants.ConsentStore(run.root / f"{answer}.json"),
                                       prompt=lambda question, answer=answer: answer)
        restricted = {"agent_output": block("BLOCKED", environment_blocker=SANDBOX_BLOCKER)}
        recorder = run.recorder([Scripted("codex", [dict(restricted), dict(restricted)])],
                                consent=consent)
        with patch("cycle.effect_snapshot", return_value=CLEAN):
            run.events.append(["answer", answer, run.stage(recorder, "implement")])
    return []


def sandbox_answer_with_changed_effects_stops(run):
    consent = grants.ConsentBroker("owner/repo", platform="linux",
                                   store=grants.ConsentStore(run.root / "consents.json"),
                                   prompt=lambda question: "cycle")
    codex = Scripted("codex", [{"agent_output": block("BLOCKED", environment_blocker=SANDBOX_BLOCKER)}])
    recorder = run.recorder([codex], consent=consent)
    with patch("cycle.effect_snapshot", side_effect=lambda *a, **k:
               CLEAN if not codex.dispatched else {**CLEAN, "remote": "pushed"}):
        return [run.stage(recorder, "implement")]


def denied_grant_before_dispatch_is_not_rerun(run):
    consent = grants.ConsentBroker("owner/repo", platform="linux",
                                   store=grants.ConsentStore(run.root / "consents.json"),
                                   prompt=lambda question: "deny")
    restricted = {"agent_output": block("BLOCKED", environment_blocker=SANDBOX_BLOCKER)}
    recorder = run.recorder([Scripted("codex", [dict(restricted), dict(restricted),
                                                dict(restricted)])], consent=consent)
    with patch("cycle.effect_snapshot", return_value=CLEAN):
        return [run.stage(recorder, "implement"), run.stage(recorder, "implement")]


def command_refusal_asks_then_reruns(run):
    def refused(target, kw):
        policy = kw.get("command_policy")
        allowed = sp.check("stat /opt/runtime", policy).allowed if policy else False
        return {"agent_output": block("IMPLEMENTED" if allowed else "BLOCKED",
                                      change_request_id="4"),
                "artifacts": {} if allowed else {"permission_denials": ["stat /opt/runtime"]}}

    results = []
    for answer in (None, "cycle"):
        kwargs = {} if answer is None else {"prompt": lambda question, answer=answer: answer}
        consent = grants.ConsentBroker("owner/repo", platform="linux",
                                       store=grants.ConsentStore(run.root / f"{answer}.json"),
                                       **kwargs)
        recorder = run.recorder([Scripted("claude", [refused, refused])],
                                availability={"claude": ex.Availability.READY},
                                consent=consent,
                                profiles={name: router.Profile(
                                    p.name,
                                    router.parse_target("claude:anthropic/claude-opus-5-5 high"),
                                    None) for name, p in router.load_profiles().items()})
        with patch("cycle.effect_snapshot", return_value=CLEAN):
            results.append(run.stage(
                recorder, "implement",
                command_policy=sp.StagePolicy("implement", repo="owner/repo",
                                              tmpdir=str(run.root))))
    return results


def agent_supplied_runtime_keys_are_claimed(run):
    recorder = run.recorder([
        Scripted("codex", [{"agent_output": block("IMPLEMENTED", change_request_id="4",
                                                  permission_outcomes=["forged"])}])])
    return [run.stage(recorder, "implement")]


def handoff_with_a_changed_diff_is_refused(run):
    codex = Scripted("codex", [{"outcome": ex.DispatchOutcome.FAILED, "error_code": "quota",
                                "detail": "quota exhausted; reset at 9:46 PM"}])
    first = run.recorder([codex, Scripted("claude")], change_request_id="384")
    with patch("cycle.effect_snapshot", side_effect=lambda *a, **k:
               CLEAN if not codex.dispatched else DIRTY):
        results = [run.stage(first, "resolve")]
    resumed = run.recorder([Scripted("codex"), Scripted("claude")], change_request_id="384",
                           started_from="resolve", cycle_id="cycle-characterization-resume")
    changed = {**DIRTY, "workspace": {**WORKSPACE, "fingerprint": "c" * 64}}
    with ExitStack() as stack:
        stack.enter_context(patch("cycle.effect_snapshot", return_value=changed))
        stack.enter_context(patch("executors._git_output", return_value=" M one.py"))
        stack.enter_context(patch("executors._workspace_fingerprint", return_value=WORKSPACE))
        results.append(run.stage(resumed, "resolve", cwd=str(run.root)))
    return results


def handoff_without_provenance_blocks_routing(run):
    resumed = run.recorder([Scripted("codex"), Scripted("claude")], change_request_id="384",
                           started_from="resolve")
    with ExitStack() as stack:
        stack.enter_context(patch("cycle.effect_snapshot", return_value=DIRTY))
        stack.enter_context(patch("executors._git_output", return_value=" M one.py"))
        stack.enter_context(patch("executors._workspace_fingerprint", return_value=WORKSPACE))
        return [run.stage(resumed, "resolve", cwd=str(run.root))]


SCENARIOS = (
    implement_succeeds,
    full_cycle_completes,
    local_only_review_is_escalated,
    routing_is_blocked,
    quota_blocked_primary_reroutes_once,
    capacity_reroutes_and_the_fallback_fails,
    capacity_without_exit_proof_stops,
    started_writer_reconciles_before_rerouting,
    started_writer_with_changed_workspace_stops,
    quota_with_dirty_workspace_records_a_handoff_then_resumes,
    quota_without_reset_time_stops,
    success_clears_a_recorded_quota_exhaustion,
    calibration_never_reroutes,
    calibration_refuses_an_unproven_executor,
    human_action_is_not_rerouted,
    nontransient_failure_is_not_rerouted,
    asynchronous_launch_is_recorded_launched,
    interruption_records_the_stage_and_raises,
    executor_exception_propagates,
    stage_contract_mismatches_are_refused,
    resolve_pins_effort_and_escalates,
    resolve_override_unavailable_or_exhausted,
    resolve_escalation_failure_keeps_acceptance_unknown,
    resolve_unsupported_effort_uses_the_fallback,
    resolve_unsupported_effort_without_fallback_stops,
    codex_catalog_rejects_max_effort,
    codex_git_metadata_probe_routes_elsewhere,
    environment_restriction_keeps_the_executor,
    failed_native_preflight_is_a_known_prerequisite,
    known_prerequisite_blocker_is_not_rerouted,
    sandbox_blocker_without_an_answer_asks,
    sandbox_blocker_answered_reruns_the_same_executor,
    sandbox_answer_with_changed_effects_stops,
    denied_grant_before_dispatch_is_not_rerun,
    command_refusal_asks_then_reruns,
    agent_supplied_runtime_keys_are_claimed,
    handoff_with_a_changed_diff_is_refused,
    handoff_without_provenance_blocks_routing,
)


def run_scenario(scenario) -> dict:
    with tempfile.TemporaryDirectory(prefix="stage-characterization-") as temporary, \
            ExitStack() as stack:
        stack.enter_context(patch("cycle.build_harness_snapshot",
                                  side_effect=stable_harness_snapshot))
        stack.enter_context(patch("cycle.effect_snapshot", return_value=None))
        # A read-only stage fingerprints the checkout it runs in, which is
        # this repository when no `cwd` is given; its HEAD moves with every
        # commit, so the checkout is a fixed one here.
        stack.enter_context(patch("executors._checkout_fingerprint",
                                  return_value={"head": "c" * 40, "fingerprint": "d" * 64}))
        run = Run(Path(temporary))
        results = scenario(run)
        return run.capture(results)


def differences(expected, observed, path="$") -> list[str]:
    """Where two captures disagree, as paths with both values, for a readable failure."""
    if isinstance(expected, dict) and isinstance(observed, dict):
        out = []
        for key in sorted(set(expected) | set(observed)):
            out += differences(expected.get(key, "<absent>"), observed.get(key, "<absent>"),
                               f"{path}.{key}")
        return out
    if (isinstance(expected, list) and isinstance(observed, list)
            and len(expected) == len(observed)):
        out = []
        for index, (left, right) in enumerate(zip(expected, observed)):
            out += differences(left, right, f"{path}[{index}]")
        return out
    if expected == observed:
        return []
    return [f"{path}: expected {json.dumps(expected)[:300]} observed {json.dumps(observed)[:300]}"]


def encode_fixture(observed: dict) -> str:
    """One scenario per line, so a diff of the fixture names the scenario."""
    lines = [f"{json.dumps(name)}: {json.dumps(capture, sort_keys=True)}"
             for name, capture in sorted(observed.items())]
    return "{\n" + ",\n".join(lines) + "\n}\n"


def observe() -> dict:
    return {scenario.__name__: run_scenario(scenario) for scenario in SCENARIOS}


class StageCharacterizationTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.observed = observe()
        cls.recorded = not FIXTURE.exists() or not FIXTURE.read_text(encoding="utf-8").strip()
        if cls.recorded:
            # Recording mode: deleting or emptying the fixture and running
            # this test records the current behaviour. The run that records
            # fails, so a fresh recording is never mistaken for a comparison.
            FIXTURE.write_text(encode_fixture(cls.observed), encoding="utf-8")
        cls.expected = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_the_fixture_was_compared_not_recorded(self) -> None:
        self.assertFalse(self.recorded, f"{FIXTURE} was recorded by this run; run it again")

    def test_every_scenario_records_exactly_what_the_fixture_holds(self) -> None:
        self.assertEqual(sorted(self.expected), sorted(self.observed),
                         "scenarios and fixture disagree")
        for name, observed in self.observed.items():
            with self.subTest(scenario=name):
                self.assertEqual([], differences(self.expected.get(name), observed)[:20])

    def test_a_run_is_deterministic(self) -> None:
        for scenario in (full_cycle_completes, sandbox_blocker_answered_reruns_the_same_executor,
                         quota_with_dirty_workspace_records_a_handoff_then_resumes):
            with self.subTest(scenario=scenario.__name__):
                self.assertEqual([], differences(self.observed[scenario.__name__],
                                                 run_scenario(scenario))[:20])

    def test_a_changed_recorded_row_is_detected(self) -> None:
        original = tm.Telemetry.record_stage

        def altered(self, *args, **kwargs):
            if kwargs.get("record_kind", "dispatch") == "dispatch":
                kwargs["difficulty"] = 3
            return original(self, *args, **kwargs)

        with patch.object(tm.Telemetry, "record_stage", altered):
            changed = run_scenario(implement_succeeds)
        self.assertNotEqual(self.expected.get("implement_succeeds"), changed)

    def test_a_changed_journal_entry_is_detected(self) -> None:
        original = journal.Journal.record

        def altered(self, kind, **fields):
            if kind == "stage_end":
                fields["status"] = "altered"
            return original(self, kind, **fields)

        with patch.object(journal.Journal, "record", altered):
            changed = run_scenario(implement_succeeds)
        self.assertNotEqual(self.expected.get("implement_succeeds"), changed)

    def test_the_scenarios_reach_the_critical_flows(self) -> None:
        """The fixture is only evidence for the flows the scenarios reach."""
        texts = json.dumps(self.observed)
        markers = ('"reroute_reason": "not_started"', '"reroute_reason": "no_effects"',
                       '"reroute_reason": "dirty_workspace"', '"reroute_reason": "exit_unproven"',
                       '"reroute_reason": "calibration"',
                       '"reroute_reason": "same_sandbox"', '"reroute_reason": "permission_required"',
                       '"reroute_reason": "permission_answered"', '"relationship": "fallback"',
                       '"relationship": "retry"', '"raised": "CycleInterrupted"',
                       '"raised": "CycleError"', '"raised": "RuntimeError"',
                       '"record_kind": "cycle"', '"escalation_acceptance": "accepted"',
                       '"escalation_acceptance": "rejected"', '"escalation_acceptance": "unsupported"',
                       '"escalation_acceptance": "unknown"', '"quota_handoffs"',
                       '"status": "handoff"', '"status": "provenance_recorded"',
                       '"kind": "refusal"', "routing found no dispatchable target",
                       "Codex linked-worktree Git metadata probe failed",
                       "known native prerequisite", "was denied before this dispatch",
                       "previous resolve target has recorded quota exhaustion",
                       "the previous resolve target is no longer available",
                       "the configured fallback is unavailable",
                       "Quota worktree resume")
        self.assertEqual([], [marker for marker in markers if marker not in texts])


if __name__ == "__main__":
    unittest.main()
