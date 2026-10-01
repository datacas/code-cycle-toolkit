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


def _compact_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value // 1_000}k"
    return str(value)


def _usage_summary(result) -> str | None:
    artifacts = getattr(result, "artifacts", None)
    if not isinstance(artifacts, dict):
        return None
    stdout = artifacts.get("stdout")
    if not isinstance(stdout, str):
        return None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                total = usage.get("input_tokens")
                cached = usage.get("cached_input_tokens", 0)
                output = usage.get("output_tokens")
                if all(isinstance(value, int) for value in (total, cached, output)):
                    percent = round(cached / total * 100) if total else 0
                    return (f"in {_compact_tokens(total)} ({percent}% cached) / "
                            f"out {_compact_tokens(output)}")
        if event.get("type") == "result":
            model_usage = event.get("modelUsage")
            if isinstance(model_usage, dict) and model_usage:
                total = cached = output = 0
                valid = True
                for values in model_usage.values():
                    if not isinstance(values, dict):
                        valid = False
                        break
                    normal = values.get("inputTokens", 0)
                    cache_read = values.get("cacheReadInputTokens", 0)
                    cache_create = values.get("cacheCreationInputTokens", 0)
                    out = values.get("outputTokens", 0)
                    if not all(isinstance(value, int)
                               for value in (normal, cache_read, cache_create, out)):
                        valid = False
                        break
                    total += normal + cache_read + cache_create
                    cached += cache_read + cache_create
                    output += out
                if valid:
                    percent = round(cached / total * 100) if total else 0
                    return (f"in {_compact_tokens(total)} ({percent}% cached) / "
                            f"out {_compact_tokens(output)}")
    return None


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
    """Seconds until the next task-relative tick: short cadence through 15m, then 5m."""
    elapsed = max(0.0, float(elapsed_seconds))
    short = max(0.01, float(short_interval))
    threshold = 15 * 60
    if elapsed < threshold:
        return min(short - (elapsed % short), threshold - elapsed)
    long = 5 * 60
    return long - ((elapsed - threshold) % long)


def _stage_line(stage: dict, *, progress: bool = False) -> str:
    timestamp = datetime.now().astimezone().strftime("%H:%M")
    role = str(stage.get("role", "cycle"))
    result = stage.get("status") or stage.get("outcome") if stage.get("finished") else None
    label = f"{role} done" if stage.get("finished") else role
    parts = [f"{_progress_icons(role, result)} [{timestamp}] {label}"]
    if stage.get("profile"):
        parts.append(stage["profile"])
    executor = stage.get("executor", "unknown")
    model = stage.get("model", "unknown")
    effort = stage.get("effort", "unknown")
    provider = stage.get("provider")
    target = f"{provider}/{model}" if provider else model
    parts.append(f"{executor} {target} {effort}")
    if stage.get("fallback"):
        parts.append("fallback")
    elapsed = stage.get("duration_seconds", stage.get("elapsed_seconds", 0))
    if progress or stage.get("finished"):
        parts.append(_duration(elapsed))
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

    def field(value, fallback: str) -> str:
        if value is None:
            return fallback
        return _clean_activity(str(value)) or fallback

    stage = status.get("stage")
    if status.get("finished"):
        outcome = field(status.get("status"), "unknown")
        role = stage.get("role") if isinstance(stage, dict) else None
        workspace = (
            stage.get("workspace") if isinstance(stage, dict) and stage.get("workspace")
            else status.get("workspace")
        )
        parts = [f"{_progress_icons(role, outcome)} [{timestamp}] cycle done", outcome]
        parts.extend(_workspace_fields(workspace))
        return " · ".join(parts)

    workspace = (
        stage.get("workspace") if isinstance(stage, dict) and stage.get("workspace")
        else status.get("workspace")
    )
    if not isinstance(stage, dict):
        activity = field(
            status.get("task") or "waiting for the first stage",
            "waiting for the first stage",
        )
        parts = [f"🔔 [{timestamp}] cycle starting"]
        parts.extend(_workspace_fields(workspace))
        parts.append(activity)
        return " · ".join(parts)

    role = field(stage.get("role", "stage"), "stage")
    finished = bool(stage.get("finished"))
    label = f"{role} done" if finished else role
    result = stage.get("status") or stage.get("outcome") if finished else None
    executor = field(stage.get("executor"), "unknown")
    provider = field(stage.get("provider"), "")
    model = field(stage.get("model"), "unknown")
    target = f"{provider}/{model}" if provider else model
    effort = field(stage.get("effort"), "unknown")
    parts = [f"{_progress_icons(role, result)} [{timestamp}] {label}"]
    parts.append(f"{executor} {target} {effort}")
    elapsed = stage.get("duration_seconds", stage.get("elapsed_seconds", 0))
    parts.append(_duration(elapsed))
    if stage.get("fallback"):
        parts.append("fallback")
    if stage.get("tool_count"):
        parts.append(f"{stage['tool_count']} tools")
    parts.extend(_workspace_fields(workspace))
    activity = stage.get("activity")
    if finished:
        outcome = stage.get("status") or stage.get("outcome")
        if outcome:
            activity = f"{field(outcome, 'unknown')}: {activity}" if activity else outcome
    if activity:
        cleaned = field(activity, "")
        if cleaned:
            parts.append(cleaned)
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
                 task: str, *, progress_interval: float = 120,
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

    def stage_started(self, role, decision) -> None:
        self._refresh_workspace(force=True)
        target = decision.target
        with self._lock:
            self._workspace_generation += 1
            self._stage_workspace_cwd = None
            self._stage_started = time.monotonic()
            workspace = {
                "cwd": "unknown",
                "repo_root": None,
                "branch": "unknown",
                "kind": "unknown",
                "temporary": False,
            }
            self._status["stage"] = {
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
                "tool_count": 0,
                "activity": None,
                "finished": False,
            }
            self._last_progress = (
                _duration(0), 0, None, json.dumps(workspace, sort_keys=True),
            )
            snapshot = dict(self._status["stage"])
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
            self._write()
            if self.verbose and snapshot is not None:
                self._print(_stage_line(snapshot, progress=True))
        return changed

    def activity(self, *, text: str | None = None, tool: bool = False) -> None:
        workspace_changed = self._refresh_workspace(force=False)
        with self._lock:
            stage = self._status.get("stage")
            if not isinstance(stage, dict) or stage.get("finished"):
                return
            if tool:
                stage["tool_count"] += 1
            if text:
                clean = _clean_activity(text)
                if clean:
                    stage["activity"] = clean
            self._refresh_elapsed(stage)
            snapshot = dict(stage)
        self._write()
        if self.verbose and workspace_changed:
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

    def finish(self, status: str) -> None:
        with self._lock:
            self._status["finished"] = True
            self._status["status"] = status
            self._status["finished_at"] = _now()
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

    def _refresh_elapsed(self, stage: dict) -> None:
        if self._stage_started is not None:
            stage["elapsed_seconds"] = round(time.monotonic() - self._stage_started, 3)

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
                if snapshot is not None:
                    progress = (
                        _duration(snapshot.get("elapsed_seconds", 0)),
                        int(snapshot.get("tool_count", 0)),
                        snapshot.get("activity"),
                        json.dumps(snapshot.get("workspace") or {}, sort_keys=True),
                    )
                    previous = self._last_progress
                    changed = progress != previous
                    if changed:
                        self._last_progress = progress
                else:
                    changed = False
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
                        help="seconds between refreshes when following (default: 60)")
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
            if args.line:
                for status in statuses:
                    line = format_progress_line(status)
                    cycle_id = str(status.get("cycle_id", "cycle"))
                    if (since is None or _updated_after(status, since)) and \
                            last_lines.get(cycle_id) != line:
                        print(line)
                        last_lines[cycle_id] = line
            elif statuses:
                for status in statuses:
                    print(format_status(status))
            else:
                print(f"No cycle status files in {directory}")
            if not args.follow:
                return 0
            time.sleep(args.progress_interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
