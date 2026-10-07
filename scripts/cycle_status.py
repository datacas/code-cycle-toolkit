"""Write and display the live status of code-cycle runs.

Status is deliberately separate from telemetry: it is a small, replaceable
snapshot that a terminal can read while a dispatch is still running. The
telemetry database remains the record of what the cycle did.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from usage import normalize_executor_output, usage_summary
from typing import TextIO

from telemetry import default_database_path

ACTIVITY_LIMIT = 500


def _configure_stdout() -> None:
    """Keep emoji progress icons printable when Windows defaults to CP1252."""
    import sys

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _clean_activity(value: str) -> str:
    value = "".join(char for char in value
                    if char in "\t\n\r" or ord(char) >= 32 and ord(char) != 127)
    return " ".join(value.split())[:ACTIVITY_LIMIT]


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, remainder = divmod(total, 60)
    if minutes:
        return f"{minutes}m{remainder:02d}s"
    return f"{remainder}s"


def _total_duration(seconds) -> str:
    try:
        total_seconds = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "unknown"
    if total_seconds < 60 * 60:
        return _duration(total_seconds)
    hours, remainder = divmod(total_seconds, 60 * 60)
    return f"{hours}h{remainder // 60:02d}m"


def _usage_summary(result) -> str | None:
    structured = getattr(result, "usage_observations", ())
    summary = usage_summary(structured)
    if summary:
        return summary
    artifacts = getattr(result, "artifacts", None)
    if not isinstance(artifacts, dict):
        return None
    stdout = artifacts.get("stdout")
    if not isinstance(stdout, str):
        return None
    executor = getattr(result, "executor", None)
    if executor not in {"codex", "claude"}:
        return None
    observations, _costs = normalize_executor_output(executor, stdout, "status-only")
    return usage_summary(observations)


ROLE_ICONS = {
    "bootstrap": "🧭",
    "issue_review": "🧭",
    "implement": "🛠️",
    "review": "🔍",
    "initial-review": "🔍",
    "initial_review": "🔍",
    "resolve": "🩹",
    "rereview": "🔎",
}

_SUCCESS_RESULTS = {
    "APPROVED", "COMPLETED", "IMPLEMENTED", "READY_FOR_MANUAL_MERGE",
    "RESOLVED", "SUCCEEDED", "SUCCESS",
}
_WARNING_RESULTS = {"CHANGES_REQUESTED", "NEEDS_REFINEMENT", "PARTIALLY_RESOLVED"}
_BLOCKED_RESULTS = {"BLOCKED", "HUMAN_INTERVENTION", "INTERRUPTED", "STOPPED"}
_FAILURE_RESULTS = {"CONTRACT_VIOLATION", "ERROR", "FAILED"}


def _result_icon(value: object) -> str | None:
    normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if normalized in _FAILURE_RESULTS or "ERROR" in normalized or "FAILED" in normalized:
        return "❌"
    if normalized in _BLOCKED_RESULTS or "BLOCKED" in normalized or "STOPPED" in normalized:
        return "⛔"
    if normalized in _WARNING_RESULTS or "CHANGES_REQUESTED" in normalized or "NEEDS_REFINEMENT" in normalized:
        return "⚠️"
    if normalized in _SUCCESS_RESULTS or "APPROVED" in normalized or "SUCCEEDED" in normalized:
        return "✅"
    return None


def _progress_icons(role: object, result: object = None) -> str:
    role_icon = ROLE_ICONS.get(str(role or "").strip().lower(), "🔔")
    result_icon = _result_icon(result)
    return f"{result_icon} {role_icon}" if result_icon else role_icon


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _workspace_snapshot(cwd: str | Path | None) -> dict[str, object]:
    try:
        path = Path(cwd or Path.cwd()).expanduser().resolve()
    except (OSError, RuntimeError):
        return {
            "cwd": str(cwd) if cwd else "unknown",
            "repo_root": None,
            "branch": "unknown",
            "kind": "unknown",
            "temporary": False,
        }

    repo_root = None
    branch = "unknown"
    kind = "outside Git"
    try:
        result = subprocess.run(
            [
                "git", "-C", str(path), "rev-parse", "--path-format=absolute",
                "--show-toplevel",
                "--absolute-git-dir", "--git-common-dir", "--abbrev-ref", "HEAD",
            ],
            capture_output=True, check=False, text=True, timeout=2,
        )
        fields = result.stdout.splitlines()
        if result.returncode == 0 and len(fields) >= 4:
            repo_root = str(Path(fields[0]).resolve())
            git_dir = Path(fields[1]).resolve()
            common_dir = Path(fields[2])
            if not common_dir.is_absolute():
                common_dir = Path(repo_root) / common_dir
            kind = "linked worktree" if git_dir != common_dir.resolve() else "regular checkout"
            branch = "detached" if fields[3] == "HEAD" else fields[3]
        elif result.returncode != 0:
            error = result.stderr.lower()
            kind = (
                "outside Git"
                if "not a git repository" in error or "not a git work tree" in error
                else "unknown"
            )
    except (OSError, subprocess.SubprocessError, ValueError):
        kind = "unknown"

    temporary = False
    for root in (Path(tempfile.gettempdir()), Path("/tmp"), Path("/private/tmp")):
        try:
            if _is_within(path, root.expanduser().resolve()):
                temporary = True
                break
        except (OSError, RuntimeError):
            continue
    return {
        "cwd": str(path),
        "repo_root": repo_root,
        "branch": branch,
        "kind": kind,
        "temporary": temporary,
    }


def _workspace_fields(workspace: object) -> list[str]:
    if not isinstance(workspace, dict):
        workspace = {}
    if workspace.get("pending"):
        return ["workspace pending"]
    cwd = workspace.get("cwd") or "unknown"
    repo_root = workspace.get("repo_root") or "unknown"
    branch = workspace.get("branch") or "unknown"
    kind = workspace.get("kind") or "unknown"
    if workspace.get("temporary"):
        kind = f"temporary {kind}"
    return [
        f"cwd {cwd}",
        f"repo {repo_root}",
        f"branch {branch}",
        str(kind),
    ]


def _next_heartbeat_delay(elapsed_seconds: float, short_interval: float) -> float:
    """Seconds until the next task-relative tick on the 1/2/5 minute cadence."""
    elapsed = max(0.0, float(elapsed_seconds))
    short = max(0.01, float(short_interval))
    first_threshold = 5 * 60
    second_threshold = 15 * 60
    if elapsed < first_threshold:
        return min(short - (elapsed % short), first_threshold - elapsed)
    if elapsed < second_threshold:
        interval = 2 * 60
        return min(interval - ((elapsed - first_threshold) % interval),
                   second_threshold - elapsed)
    interval = 5 * 60
    return interval - ((elapsed - second_threshold) % interval)


def _stage_name(stage: dict) -> str:
    role = str(stage.get("role", "stage"))
    substage = stage.get("substage")
    if not substage:
        return role
    parent = stage.get("parent_stage") or role
    suffix = " (delegated)" if stage.get("delegated") else ""
    return f"{parent} › {substage}{suffix}"


def _stage_state(stage: dict | None) -> str:
    if not isinstance(stage, dict):
        return "waiting"
    if stage.get("finished"):
        result = str(stage.get("status") or stage.get("outcome") or "").upper()
        if result in _BLOCKED_RESULTS or result == "NEEDS_REFINEMENT":
            return "blocked"
        if result in _FAILURE_RESULTS:
            return "failed"
        return "done"
    explicit = stage.get("state")
    if explicit in {"running", "waiting", "blocked", "done", "failed"}:
        return explicit
    return "running"


def _stage_line(stage: dict, *, progress: bool = False) -> str:
    timestamp = datetime.now().astimezone().strftime("%H:%M")
    role = str(stage.get("role", "cycle"))
    icon_role = stage.get("parent_stage") if stage.get("delegated") else role
    result = stage.get("status") or stage.get("outcome") if stage.get("finished") else None
    name = _stage_name(stage)
    state = _stage_state(stage)
    parts = [f"{_progress_icons(icon_role, result)} [{timestamp}] {name} · {state}"]
    if stage.get("profile"):
        parts.append(stage["profile"])
    executor = stage.get("executor", "unknown")
    model = stage.get(
        "delegated_model" if stage.get("delegated") else "model", "unknown",
    )
    effort = stage.get(
        "delegated_effort" if stage.get("delegated") else "effort", "unknown",
    )
    provider = stage.get(
        "delegated_provider" if stage.get("delegated") else "provider",
    )
    target = f"{provider}/{model}" if provider else model
    parts.append(f"{executor} {target} {effort}")
    if stage.get("fallback"):
        parts.append("fallback")
    elapsed = stage.get("duration_seconds", stage.get("elapsed_seconds", 0))
    parts.extend([f"total {_total_duration(stage.get('total_elapsed_seconds', 0))}",
                  f"stage {_duration(elapsed)}"])
    if stage.get("iteration") is not None:
        parts.append(f"round {stage['iteration']}")
    if stage.get("tool_count"):
        parts.append(f"{stage['tool_count']} tools")
    parts.extend(_workspace_fields(stage.get("workspace")))
    if stage.get("finished"):
        if stage.get("outcome"):
            parts.append(str(stage["outcome"]))
        if stage.get("status"):
            parts.append(str(stage["status"]))
    activity = stage.get("activity")
    if activity:
        parts.append(_clean_activity(str(activity)))
    return " · ".join(str(part) for part in parts)


def format_progress_line(status: dict) -> str:
    """Format the latest cycle snapshot as one user-facing progress line."""
    updated_at = status.get("updated_at")
    try:
        timestamp = _parse_timestamp(updated_at).astimezone().strftime("%H:%M")
    except (AttributeError, TypeError, ValueError):
        timestamp = datetime.now().astimezone().strftime("%H:%M")

    def field(value, fallback: str, limit: int = ACTIVITY_LIMIT) -> str:
        if value is None:
            return fallback
        return _clean_activity(str(value))[:limit] or fallback

    stage = status.get("stage")
    finished = bool(status.get("finished"))
    outcome = field(status.get("status"), "unknown") if finished else None
    role = (
        (stage.get("parent_stage") if stage.get("delegated") else stage.get("role"))
        if isinstance(stage, dict) else None
    )
    workspace = (
        stage.get("workspace") if isinstance(stage, dict) and stage.get("workspace")
        else status.get("workspace")
    )
    total = status.get("total_elapsed_seconds")
    if total is None and isinstance(stage, dict):
        total = stage.get("total_elapsed_seconds")
    total_label = f"total {_total_duration(total)}" if total is not None else "total unknown"

    if finished:
        final_stage = stage if isinstance(stage, dict) else None
        state_source = {**(final_stage or {}), "finished": True, "status": outcome}
        state = _stage_state(state_source)
        parts = [f"{_progress_icons(role, outcome)} [{timestamp}] cycle done", state, outcome]
        if final_stage is not None:
            parts.append(_stage_name(final_stage))
            executor = field(final_stage.get("executor"), "unknown")
            provider = field(final_stage.get("provider"), "")
            model = field(final_stage.get("model"), "unknown")
            effort = field(final_stage.get("effort"), "unknown")
            target = f"{provider}/{model}" if provider else model
            parts.append(f"{executor} {target} {effort}")
            elapsed = final_stage.get("duration_seconds", final_stage.get("elapsed_seconds"))
            if elapsed is not None:
                parts.append(f"stage {_duration(elapsed)}")
            if final_stage.get("iteration") is not None:
                parts.append(f"round {final_stage['iteration']}")
        parts.append(total_label)
        parts.extend(_workspace_fields(workspace))
        stop_reason = status.get("stop_reason")
        reason = status.get("reason")
        if stop_reason and state in {"blocked", "failed"}:
            parts.append("stop " + field(stop_reason, "unknown"))
        if reason:
            parts.append(field(reason, "stopped"))
        if final_stage is not None and final_stage.get("error_code"):
            parts.append("error_code=" + field(final_stage["error_code"], "unknown"))
            parts.append("start_state=" + field(final_stage.get("start_state"), "unknown"))
            if final_stage.get("failure_detail"):
                parts.append(field(final_stage["failure_detail"], "", limit=600))
        count = int(status.get("decision_count", 0) or 0)
        question = status.get("question")
        if count:
            parts.append(f"requires decision (1/{count})")
        if isinstance(question, dict):
            question_id = field(question.get("id"), "")
            prompt = field(question.get("prompt"), "question pending", limit=200)
            options = question.get("options")
            question_label = f"{question_id}: {prompt}" if question_id else prompt
            if options:
                question_label += " · options: " + " | ".join(
                    field(option, "unknown", limit=100) for option in options
                )
            if question.get("recommended"):
                question_label += (
                    f" · recommended: {field(question['recommended'], 'unknown', limit=100)}"
                )
            parts.append(question_label)
        return " · ".join(parts)

    if not isinstance(stage, dict):
        activity = field(
            status.get("task") or "waiting for the first stage",
            "waiting for the first stage",
        )
        parts = [f"🔔 [{timestamp}] cycle starting · waiting", total_label]
        parts.extend(_workspace_fields(workspace))
        parts.append(activity)
        return " · ".join(parts)

    role = field(stage.get("role", "stage"), "stage")
    icon_role = stage.get("parent_stage") if stage.get("delegated") else role
    state = _stage_state(stage)
    executor = field(stage.get("executor"), "unknown")
    provider = field(
        stage.get("delegated_provider" if stage.get("delegated") else "provider"),
        "unknown" if stage.get("delegated") else "",
    )
    model = field(
        stage.get("delegated_model" if stage.get("delegated") else "model"),
        "unknown",
    )
    target = f"{provider}/{model}" if provider else model
    effort = field(
        stage.get("delegated_effort" if stage.get("delegated") else "effort"),
        "unknown",
    )
    result = stage.get("status") or stage.get("outcome") if stage.get("finished") else None
    parts = [f"{_progress_icons(icon_role, result)} [{timestamp}] {_stage_name(stage)} · {state}"]
    parts.append(f"{executor} {target} {effort}")
    parts.append(total_label)
    elapsed = stage.get("duration_seconds", stage.get("elapsed_seconds", 0))
    parts.append(f"stage {_duration(elapsed)}")
    if stage.get("fallback"):
        parts.append("fallback")
    if stage.get("iteration") is not None:
        parts.append(f"round {stage['iteration']}")
    if stage.get("tool_count"):
        parts.append(f"{stage['tool_count']} tools")
    parts.extend(_workspace_fields(workspace))
    activity = stage.get("activity")
    if stage.get("finished") and result:
        activity = f"{field(result, 'unknown')}: {activity}" if activity else result
    if activity:
        parts.append(field(activity, ""))
    return " · ".join(str(part) for part in parts)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO 8601, for example 2026-09-30T12:00:00Z") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone, for example 2026-09-30T12:00:00Z")
    return parsed


def _updated_after(status: dict, since: datetime) -> bool:
    updated = status.get("updated_at")
    if not isinstance(updated, str):
        return False
    try:
        return _parse_timestamp(updated) > since
    except ValueError:
        return False


def format_status(status: dict) -> str:
    """Format one saved cycle snapshot for a human terminal."""
    line = f"{status.get('cycle_id', 'cycle')}: {format_progress_line(status)}"
    stage = status.get("stage")
    if isinstance(stage, dict) and stage.get("finished") and stage.get("usage"):
        line += f" · {stage['usage']}"
    if status.get("finished"):
        line += f"\n        cycle finished · {status.get('status', 'unknown')}"
    return line


class CycleStatusWriter:
    """Keep one atomic JSON status snapshot current for a running cycle."""

    def __init__(self, database_path: str | Path, cycle_id: str, repo: str,
                 task: str, *, progress_interval: float = 60,
                 verbose: bool = False, stream: TextIO | None = None,
                 workspace: str | Path | None = None) -> None:
        self.path = Path(database_path).expanduser().parent / "status" / f"{cycle_id}.json"
        self.progress_interval = max(0.01, float(progress_interval))
        self.verbose = verbose
        self.stream = stream
        self.workspace_cwd = str(workspace or Path.cwd())
        self._stage_workspace_cwd: str | None = None
        self._workspace_generation = 0
        self._workspace_refresh_lock = threading.Lock()
        self._last_workspace_check = time.monotonic()
        workspace_info = _workspace_snapshot(self.workspace_cwd)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stage_started: float | None = None
        self._cycle_started: float | None = None
        self._last_progress: tuple[object, ...] | None = None
        self._delegated_stages: dict[str, dict[str, str]] = {}
        self._base_stage_context: tuple[str | None, str | None, bool] = (None, None, False)
        self._status = {
            "cycle_id": cycle_id,
            "repo": repo,
            "task": task,
            "workspace": workspace_info,
            "started_at": _now(),
            "updated_at": _now(),
            "finished": False,
            "status": "RUNNING",
            "stage": None,
        }

    def start(self) -> None:
        self._refresh_workspace(force=True)
        self._cycle_started = time.monotonic()
        self._last_workspace_check = self._cycle_started
        self._write()
        self._thread = threading.Thread(target=self._heartbeat,
                                        name=f"cycle-status-{self._status['cycle_id']}",
                                        daemon=True)
        self._thread.start()

    def stage_started(self, role, decision, *, iteration: int | None = None,
                     parent_stage: str | None = None,
                     substage: str | None = None,
                     delegated: bool = False) -> None:
        self._refresh_workspace(force=True)
        target = decision.target
        with self._lock:
            self._workspace_generation += 1
            self._stage_workspace_cwd = None
            self._stage_started = time.monotonic()
            self._delegated_stages.clear()
            self._base_stage_context = (parent_stage, substage, bool(delegated))
            workspace = {
                "pending": True,
                "cwd": None,
                "repo_root": None,
                "branch": None,
                "kind": None,
                "temporary": False,
            }
            stage = {
                "role": role,
                "profile": decision.profile,
                "executor": target.executor,
                "provider": target.provider,
                "model": target.model,
                "effort": target.effort,
                "fallback": bool(decision.used_fallback),
                "workspace": workspace,
                "started_at": _now(),
                "elapsed_seconds": 0,
                "total_elapsed_seconds": self._total_elapsed(),
                "tool_count": 0,
                "activity": None,
                "finished": False,
                "state": "running",
            }
            if iteration is not None:
                stage["iteration"] = iteration
            if parent_stage:
                stage["parent_stage"] = parent_stage
            if substage:
                stage["substage"] = substage
                stage["delegated"] = bool(delegated)
            self._status["stage"] = stage
            snapshot = dict(stage)
            self._mark_progress(snapshot)
        self._write()
        if self.verbose:
            self._print(_stage_line(snapshot))

    def workspace_observed(self, cwd: str | Path) -> bool:
        """Record the directory selected by the active executor for its worker."""
        path = str(cwd or Path.cwd())
        with self._lock:
            self._workspace_generation += 1
            self.workspace_cwd = path
            self._stage_workspace_cwd = path
            self._last_workspace_check = 0
        changed = self._refresh_workspace(force=True)
        if changed:
            with self._lock:
                stage = self._status.get("stage")
                snapshot = dict(stage) if isinstance(stage, dict) else None
                if snapshot is not None:
                    self._mark_progress(snapshot)
            self._write()
            if self.verbose and snapshot is not None:
                self._print(_stage_line(snapshot, progress=True))
        return changed

    def activity(self, *, text: str | None = None, tool: bool = False,
                 delegation_id: str | None = None,
                 delegated_stage: str | None = None,
                 delegated_provider: str | None = None,
                 delegated_model: str | None = None,
                 delegated_effort: str | None = None,
                 delegation_finished: bool = False) -> None:
        workspace_changed = self._refresh_workspace(force=False)
        with self._lock:
            stage = self._status.get("stage")
            if not isinstance(stage, dict) or stage.get("finished"):
                return
            delegation_changed = False
            if delegation_id and delegated_stage:
                clean_stage = _clean_activity(delegated_stage)[:80]
                existing = self._delegated_stages.get(delegation_id)
                child = dict(existing or {
                    "stage": clean_stage,
                    "provider": "unknown",
                    "model": "unknown",
                    "effort": "unknown",
                })
                if clean_stage and (clean_stage != "unknown" or child["stage"] == "unknown"):
                    child["stage"] = clean_stage
                for name, value in (
                    ("provider", delegated_provider),
                    ("model", delegated_model),
                    ("effort", delegated_effort),
                ):
                    if isinstance(value, str) and value.strip():
                        child[name] = _clean_activity(value)[:80]
                if clean_stage and existing != child:
                    self._delegated_stages[delegation_id] = child
                    delegation_changed = True
            if delegation_id and delegation_finished:
                delegation_changed = self._delegated_stages.pop(
                    delegation_id, None,
                ) is not None or delegation_changed
            if self._delegated_stages:
                child = list(self._delegated_stages.values())[-1]
                stage["parent_stage"] = stage["role"]
                stage["substage"] = child["stage"]
                stage["delegated"] = True
                stage["delegated_provider"] = child["provider"]
                stage["delegated_model"] = child["model"]
                stage["delegated_effort"] = child["effort"]
            else:
                parent_stage, substage, delegated = self._base_stage_context
                if parent_stage:
                    stage["parent_stage"] = parent_stage
                else:
                    stage.pop("parent_stage", None)
                if substage:
                    stage["substage"] = substage
                    stage["delegated"] = delegated
                else:
                    stage.pop("substage", None)
                    stage.pop("delegated", None)
                stage.pop("delegated_provider", None)
                stage.pop("delegated_model", None)
                stage.pop("delegated_effort", None)
            if tool:
                stage["tool_count"] += 1
            if text:
                clean = _clean_activity(text)
                if clean:
                    stage["activity"] = clean
            self._refresh_elapsed(stage)
            snapshot = dict(stage)
            if workspace_changed or delegation_changed:
                self._mark_progress(snapshot)
        self._write()
        if self.verbose and (workspace_changed or delegation_changed):
            self._print(_stage_line(snapshot, progress=True))

    def stage_finished(self, result, status: str | None, *,
                       findings: str | None = None,
                       warnings: list[str] | None = None) -> float | None:
        with self._lock:
            stage = self._status.get("stage")
            if not isinstance(stage, dict):
                return None
            self._refresh_elapsed(stage)
            stage["duration_seconds"] = stage.pop("elapsed_seconds", 0)
            stage["outcome"] = result.outcome.value if result else "blocked"
            stage["status"] = status
            stage["finished"] = True
            if result is not None and getattr(result, "failure_code", None) is not None:
                stage["error_code"] = result.failure_code
                stage["start_state"] = result.start_state
                stage["failure_detail"] = result.failure_summary()
            self._workspace_generation += 1
            usage = _usage_summary(result)
            if usage:
                stage["usage"] = usage
            if findings:
                stage["findings"] = findings
            if warnings:
                stage["warnings"] = list(warnings)
            snapshot = dict(stage)
            self._stage_started = None
        self._write()
        if self.verbose:
            parts = [_stage_line(snapshot, progress=True)]
            if snapshot.get("usage"):
                parts.append(snapshot["usage"])
            self._print(" · ".join(parts))
            if findings:
                self._print(f"           {findings}")
            for warning in warnings or []:
                self._print(f"           warning: {warning}")
        return snapshot["duration_seconds"]

    def finish(self, status: str, *, stop_reason: str | None = None,
               reason: str | None = None, readiness=None) -> None:
        with self._lock:
            self._status["finished"] = True
            self._status["status"] = status
            self._status["finished_at"] = _now()
            if stop_reason:
                self._status["stop_reason"] = _clean_activity(stop_reason)
            if reason:
                self._status["reason"] = _clean_activity(reason)
            if readiness is not None and getattr(readiness, "valid", False):
                count = int(getattr(readiness, "decisions", 0) or 0)
                if count:
                    self._status["decision_count"] = count
                questions = getattr(readiness, "questions", [])
                if questions:
                    question = questions[0]
                    recommended = question.get("recommended")
                    self._status["question"] = {
                        "id": _clean_activity(str(question.get("id", ""))),
                        "prompt": _clean_activity(str(question.get("prompt", ""))),
                        "options": [_clean_activity(str(option)) for option in question.get("options", [])],
                        "recommended": _clean_activity(str(recommended)) if recommended else "",
                    }
            total = self._total_elapsed()
            self._status["total_elapsed_seconds"] = total
            stage = self._status.get("stage")
            if isinstance(stage, dict):
                stage["total_elapsed_seconds"] = total
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join()
        self._write()

    def _refresh_workspace(self, *, force: bool) -> bool:
        with self._lock:
            stage = self._status.get("stage")
            if isinstance(stage, dict):
                if stage.get("finished") or self._stage_workspace_cwd is None:
                    return False
                workspace_cwd = self._stage_workspace_cwd
                generation = self._workspace_generation
                stage_workspace = True
            else:
                workspace_cwd = self.workspace_cwd
                generation = self._workspace_generation
                stage_workspace = False
        now = time.monotonic()
        if not force and now - self._last_workspace_check < 5:
            return False
        with self._workspace_refresh_lock:
            now = time.monotonic()
            if not force and now - self._last_workspace_check < 5:
                return False
            snapshot = _workspace_snapshot(workspace_cwd)
            self._last_workspace_check = time.monotonic()
        with self._lock:
            if generation != self._workspace_generation:
                return False
            previous = self._status.get("workspace")
            stage = self._status.get("stage")
            if stage_workspace:
                if (not isinstance(stage, dict) or stage.get("finished")
                        or self._stage_workspace_cwd != workspace_cwd):
                    return False
            elif isinstance(stage, dict) or self.workspace_cwd != workspace_cwd:
                return False
            if isinstance(stage, dict) and not stage.get("finished"):
                previous = stage.get("workspace")
            self._status["workspace"] = snapshot
            if isinstance(stage, dict) and not stage.get("finished"):
                stage["workspace"] = dict(snapshot)
        return snapshot != previous

    def _mark_progress(self, snapshot: dict) -> bool:
        """Record the progress a line shows; False when it repeats the last one."""
        progress = (
            _duration(snapshot.get("elapsed_seconds", 0)),
            int(snapshot.get("tool_count", 0)),
            snapshot.get("activity"),
            snapshot.get("parent_stage"),
            snapshot.get("substage"),
            snapshot.get("delegated"),
            json.dumps(snapshot.get("workspace") or {}, sort_keys=True),
        )
        if progress == self._last_progress:
            return False
        self._last_progress = progress
        return True

    def _refresh_elapsed(self, stage: dict) -> None:
        if self._stage_started is not None:
            stage["elapsed_seconds"] = round(time.monotonic() - self._stage_started, 3)
        stage["total_elapsed_seconds"] = self._total_elapsed()

    def _total_elapsed(self) -> float:
        if self._cycle_started is None:
            return 0.0
        return round(max(0.0, time.monotonic() - self._cycle_started), 3)

    def _heartbeat(self) -> None:
        if self._cycle_started is None:
            self._cycle_started = time.monotonic()
        while True:
            elapsed = time.monotonic() - self._cycle_started
            delay = _next_heartbeat_delay(elapsed, self.progress_interval)
            if self._stop.wait(delay):
                return
            self._refresh_workspace(force=False)
            with self._lock:
                stage = self._status.get("stage")
                if isinstance(stage, dict) and not stage.get("finished"):
                    self._refresh_elapsed(stage)
                    snapshot = dict(stage)
                else:
                    snapshot = None
                changed = snapshot is not None and self._mark_progress(snapshot)
            self._write()
            if self.verbose and snapshot is not None and changed:
                self._print(_stage_line(snapshot, progress=True))

    def _print(self, value: str) -> None:
        import sys

        stream = self.stream or sys.stdout
        if self.stream is None:
            _configure_stdout()
            stream = sys.stdout
        with self._lock:
            print(value, file=stream, flush=True)

    def _write(self) -> None:
        with self._lock:
            self._status["updated_at"] = _now()
            self._status["total_elapsed_seconds"] = self._total_elapsed()
            stage = self._status.get("stage")
            if isinstance(stage, dict):
                stage["total_elapsed_seconds"] = self._status["total_elapsed_seconds"]
            payload = json.dumps(self._status, ensure_ascii=False, separators=(",", ":"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            )
            temporary = Path(handle.name)
            try:
                with handle:
                    handle.write(payload)
                    handle.write("\n")
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError:
            # Progress must never turn an otherwise usable cycle into a failure.
            return


def status_directory(database: str | Path | None = None,
                     explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    database_path = Path(database).expanduser() if database else default_database_path()
    return database_path.parent / "status"


def read_statuses(directory: str | Path) -> list[dict]:
    statuses = []
    for path in sorted(Path(directory).glob("*.json"), key=lambda item: item.stat().st_mtime):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            statuses.append(value)
    return statuses


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(description="Show the status of code-cycle runs.")
    parser.add_argument("--follow", action="store_true", help="refresh until interrupted")
    parser.add_argument("--line", action="store_true",
                        help="print each cycle as one user-facing progress line")
    parser.add_argument("--since", default=None,
                        help="with --line, print snapshots updated after this ISO 8601 timestamp")
    parser.add_argument("--progress-interval", type=float, default=60,
                        help="initial heartbeat seconds (default: 60; later intervals adapt)")
    parser.add_argument("--database", default=None,
                        help="telemetry database; status files sit beside it")
    parser.add_argument("--status-dir", default=None,
                        help="read status files from this directory")
    parser.add_argument("--profiles", action="store_true",
                        help="show the effective routing profiles of --cwd instead")
    parser.add_argument("--cwd", default=".",
                        help="repository whose .code-cycle.yml --profiles reads")
    args = parser.parse_args(argv)
    if args.progress_interval <= 0:
        parser.error("--progress-interval must be greater than zero")
    if args.since and not args.line:
        parser.error("--since requires --line")
    try:
        since = _parse_timestamp(args.since) if args.since else None
    except ValueError as exc:
        parser.error(str(exc))
    if args.profiles:
        import profile_config
        return profile_config.main(["show", "--cwd", args.cwd])
    directory = status_directory(args.database, args.status_dir)
    last_lines: dict[str, str] = {}
    try:
        while True:
            statuses = read_statuses(directory)
            if statuses:
                for status in statuses:
                    line = format_progress_line(status) if args.line else format_status(status)
                    cycle_id = str(status.get("cycle_id", "cycle"))
                    fresh = not args.line or since is None or _updated_after(status, since)
                    if fresh and last_lines.get(cycle_id) != line:
                        print(line)
                        last_lines[cycle_id] = line
            else:
                print(f"No cycle status files in {directory}")
            if not args.follow:
                return 0
            active = [status for status in statuses if not status.get("finished")]
            if active:
                now = datetime.now().astimezone()
                delays = []
                for status in active:
                    elapsed = status.get("total_elapsed_seconds")
                    if elapsed is None:
                        try:
                            elapsed = max(0.0, (now - _parse_timestamp(status["started_at"]).astimezone()).total_seconds())
                        except (KeyError, TypeError, ValueError):
                            elapsed = 0.0
                    delays.append(_next_heartbeat_delay(elapsed, args.progress_interval))
                time.sleep(min(delays))
            else:
                time.sleep(args.progress_interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
