"""Write and display the live status of code-cycle runs.

Status is deliberately separate from telemetry: it is a small, replaceable
snapshot that a terminal can read while a dispatch is still running. The
telemetry database remains the record of what the cycle did.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TextIO

from telemetry import default_database_path

ACTIVITY_LIMIT = 500


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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


def _stage_line(stage: dict, *, progress: bool = False) -> str:
    timestamp = datetime.now().astimezone().strftime("%H:%M")
    parts = [stage.get("role", "cycle"), stage.get("profile", "?")]
    executor = stage.get("executor", "?")
    model = stage.get("model", "?")
    effort = stage.get("effort", "?")
    provider = stage.get("provider")
    target = f"{provider}/{model}" if provider else model
    parts.append(f"{executor} {target} {effort}")
    if stage.get("fallback"):
        parts.append("fallback")
    if progress:
        parts.extend((_duration(stage.get("elapsed_seconds", 0)),
                      f"{stage.get('tool_count', 0)} tools"))
    return f"[{timestamp}] " + " · ".join(str(part) for part in parts)


def format_status(status: dict) -> str:
    """Format one saved cycle snapshot for a human terminal."""
    stage = status.get("stage")
    if not isinstance(stage, dict):
        state = "finished" if status.get("finished") else "starting"
        return f"{status.get('cycle_id', 'cycle')}: {state} · {status.get('task', '?')}"

    if stage.get("finished"):
        parts = [f"{stage.get('role', 'stage')} done",
                 stage.get("outcome", "unknown")]
        if stage.get("status"):
            parts.append(stage["status"])
        parts.append(_duration(stage.get("duration_seconds", 0)))
        if stage.get("usage"):
            parts.append(stage["usage"])
        line = f"{status.get('cycle_id', 'cycle')}: " + " · ".join(parts)
    else:
        line = f"{status.get('cycle_id', 'cycle')}: {_stage_line(stage, progress=True)}"
    activity = stage.get("activity")
    if activity:
        line += f"\n        └ {json.dumps(activity, ensure_ascii=False)}"
    if status.get("finished"):
        line += f"\n        cycle finished · {status.get('status', 'unknown')}"
    return line


class CycleStatusWriter:
    """Keep one atomic JSON status snapshot current for a running cycle."""

    def __init__(self, database_path: str | Path, cycle_id: str, repo: str,
                 task: str, *, progress_interval: float = 60,
                 verbose: bool = False, stream: TextIO | None = None) -> None:
        self.path = Path(database_path).expanduser().parent / "status" / f"{cycle_id}.json"
        self.progress_interval = max(0.01, float(progress_interval))
        self.verbose = verbose
        self.stream = stream
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stage_started: float | None = None
        self._status = {
            "cycle_id": cycle_id,
            "repo": repo,
            "task": task,
            "started_at": _now(),
            "updated_at": _now(),
            "finished": False,
            "status": "RUNNING",
            "stage": None,
        }

    def start(self) -> None:
        self._write()
        self._thread = threading.Thread(target=self._heartbeat,
                                        name=f"cycle-status-{self._status['cycle_id']}",
                                        daemon=True)
        self._thread.start()

    def stage_started(self, role, decision) -> None:
        target = decision.target
        with self._lock:
            self._stage_started = time.monotonic()
            self._status["stage"] = {
                "role": role,
                "profile": decision.profile,
                "executor": target.executor,
                "provider": target.provider,
                "model": target.model,
                "effort": target.effort,
                "fallback": bool(decision.used_fallback),
                "started_at": _now(),
                "elapsed_seconds": 0,
                "tool_count": 0,
                "activity": None,
                "finished": False,
            }
            snapshot = dict(self._status["stage"])
        self._write()
        if self.verbose:
            self._print(_stage_line(snapshot))

    def activity(self, *, text: str | None = None, tool: bool = False) -> None:
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
        self._write()

    def stage_finished(self, result, status: str | None) -> None:
        with self._lock:
            stage = self._status.get("stage")
            if not isinstance(stage, dict):
                return
            self._refresh_elapsed(stage)
            stage["duration_seconds"] = stage.pop("elapsed_seconds", 0)
            stage["outcome"] = result.outcome.value if result else "blocked"
            stage["status"] = status
            stage["finished"] = True
            usage = _usage_summary(result)
            if usage:
                stage["usage"] = usage
            snapshot = dict(stage)
            self._stage_started = None
        self._write()
        if self.verbose:
            parts = [f"{snapshot.get('role')} done", snapshot["outcome"]]
            if status:
                parts.append(status)
            parts.append(_duration(snapshot["duration_seconds"]))
            if snapshot.get("usage"):
                parts.append(snapshot["usage"])
            if snapshot.get("tool_count"):
                parts.append(f"{snapshot['tool_count']} tools")
            self._print(f"[{datetime.now().astimezone().strftime('%H:%M')}] "
                        + " · ".join(parts))

    def finish(self, status: str) -> None:
        with self._lock:
            self._status["finished"] = True
            self._status["status"] = status
            self._status["finished_at"] = _now()
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join()
        self._write()

    def _refresh_elapsed(self, stage: dict) -> None:
        if self._stage_started is not None:
            stage["elapsed_seconds"] = round(time.monotonic() - self._stage_started, 3)

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.progress_interval):
            with self._lock:
                stage = self._status.get("stage")
                if isinstance(stage, dict) and not stage.get("finished"):
                    self._refresh_elapsed(stage)
                    snapshot = dict(stage)
                else:
                    snapshot = None
            self._write()
            if self.verbose and snapshot is not None:
                self._print(_stage_line(snapshot, progress=True))
                if snapshot.get("activity"):
                    self._print(f"        └ {json.dumps(snapshot['activity'], ensure_ascii=False)}")

    def _print(self, value: str) -> None:
        import sys

        stream = self.stream or sys.stdout
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
    parser = argparse.ArgumentParser(description="Show the status of code-cycle runs.")
    parser.add_argument("--follow", action="store_true", help="refresh until interrupted")
    parser.add_argument("--progress-interval", type=float, default=60,
                        help="seconds between refreshes when following (default: 60)")
    parser.add_argument("--database", default=None,
                        help="telemetry database; status files sit beside it")
    parser.add_argument("--status-dir", default=None,
                        help="read status files from this directory")
    args = parser.parse_args(argv)
    if args.progress_interval <= 0:
        parser.error("--progress-interval must be greater than zero")
    directory = status_directory(args.database, args.status_dir)
    try:
        while True:
            statuses = read_statuses(directory)
            if statuses:
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
