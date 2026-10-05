"""Behavioral verification core: run Playwright, judge the result, keep the evidence.

`cc-verify` asks one question of a browser flow: did it really work, and can that
be shown later? This module answers it by running the project's own Playwright
tests and turning the machine-readable report into one normalised run record.
It is the only path that executes behavioral tests; the skills decide *what* to
run and *why*, never *how*.

Four rules shape it.

**The report is the evidence, not the exit code.** Playwright exits 0 for a flaky
test, for a run that selected only skipped tests, for `--pass-with-no-tests`, for
`--only-changed` with nothing changed, and for a `test.only` that silently drops
every other test. So a PASS is decided from the JSON report, the selection that
was asked for, and the counts:

    PASS = exit 0 AND valid report AND selection not empty
           AND every selected test executed AND nothing unexpected, flaky or skipped
           AND no run-level error AND the run record persisted

**Read the file, never stdout.** A host may compact a command's output. The only
inputs are the report file the reporter wrote and the process exit status.

**The expected selection comes from `--list`.** Tests that never reach the report
cannot be seen in it, so the selection is listed first, with the same filter,
and compared with what ran.

**Classification is a hint.** `category` is derived from the error text and is
heuristic. Playwright does not label causes, and the report cannot say that a
failure happened in a hook. The normalised record keeps the full message so the
agent decides.

The project's Playwright configuration is authoritative and is never modified.
The toolkit controls only the reporter, the trace mode, the retry count, the
output directory, the `--forbid-only` guard and the selection. Screenshots and
video follow the project's configuration and are never required for a PASS.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

SCHEMA_VERSION = 1

#: The oldest Playwright this module has been exercised against. Nothing older
#: is claimed to work.
MIN_PLAYWRIGHT: tuple[int, int, int] = (1, 63, 0)

BEHAVIORAL_TAG = "behavioral"

#: A test identity: an area, a name, and a number, e.g. `AUTH-LOGIN-001`. It is
#: carried as a Playwright tag (`@AUTH-LOGIN-001`), which the JSON reporter
#: reports without the leading `@`.
TEST_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-[0-9]{3,}$")

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CODE_FRAME_RE = re.compile(r"^>\s*\d+\s*\|\s?(.*)$")
_EXPECTED_RE = re.compile(r"^\s*Expected:\s*(.*\S)\s*$")
_RECEIVED_RE = re.compile(r"^\s*Received:\s*(.*\S)\s*$")
#: A failure that says the application was never reachable. `ERR_ABORTED` is not
#: here on purpose: an application can abort its own navigation.
_ENVIRONMENT_RE = re.compile(
    r"net::ERR_(?:CONNECTION_REFUSED|NAME_NOT_RESOLVED|CONNECTION_RESET|"
    r"ADDRESS_UNREACHABLE|INTERNET_DISCONNECTED)|ECONNREFUSED"
)

MAX_ERROR_LINES = 40
SUMMARY_FAILURES = 5
SUMMARY_ERROR_LINES = 6

#: Exit status of `behavioral.py run`, one per run status.
EXIT_CODES: dict[str, int] = {
    "pass": 0,
    "fail": 10,
    "flaky": 11,
    "skipped": 12,
    "error": 20,
    "blocked": 30,
    "not_configured": 40,
}

CONFIG_NAMES = tuple(
    f"playwright.config.{ext}" for ext in ("ts", "js", "mjs", "cjs", "mts", "cts")
)

PLAYWRIGHT_COMMAND: tuple[str, ...] = ("npx", "--no-install", "playwright")

#: What the process runner returns: the exit status, or `None` when it timed out.
Runner = Callable[[Sequence[str], Path, dict[str, str], float], "int | None"]


class BehavioralError(ValueError):
    """The module was asked for something it must not do."""


# --------------------------------------------------------------------------- text


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_text(text: str, root: Path | None) -> str:
    """Strip colour codes and make host paths relative to the project."""
    text = strip_ansi(text or "")
    if root is not None:
        for prefix in {str(root), root.as_posix()}:
            if prefix:
                text = text.replace(prefix.rstrip("/\\") + "/", "")
    return text


def _relative(path: str | None, root: Path | None) -> str | None:
    if not path:
        return None
    if root is None:
        return path
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return clean_text(path, root)


def _short_hash(*parts: str, size: int = 12) -> str:
    return sha256_bytes("\x1f".join(parts).encode("utf-8"))[:size]


# --------------------------------------------------------------------- selection


def test_ids_in(tags: Iterable[str]) -> list[str]:
    """The identities among a spec's tags (the report gives them without `@`)."""
    return [tag for tag in tags if TEST_ID_RE.match(tag)]


def selection_grep(ids: Sequence[str] | None = None, tag: str = BEHAVIORAL_TAG) -> str:
    """The `--grep` that selects exactly these tests.

    `@AUTH-LOGIN-001` alone also selects `@AUTH-LOGIN-0010`, so every pattern ends
    in a lookahead that refuses a following word character or hyphen. The result
    is passed to Playwright as one argv element, never through a shell.
    """
    if ids:
        for test_id in ids:
            if not TEST_ID_RE.match(test_id):
                raise BehavioralError(f"not a test ID: {test_id!r}")
        return "|".join(f"@{test_id}(?![\\w-])" for test_id in ids)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", tag):
        raise BehavioralError(f"not a tag: {tag!r}")
    return f"@{tag}(?![\\w-])"


# ------------------------------------------------------------------------- report


@dataclasses.dataclass(frozen=True)
class ListedTest:
    """One test as `--list` or a run reports it, before any outcome."""

    key: str
    test_id: str | None
    title: str
    describe: tuple[str, ...]
    file: str
    line: int | None
    column: int | None
    tags: tuple[str, ...]
    covers: tuple[str, ...]
    project: str

    @property
    def identity(self) -> str:
        return self.test_id or f"{self.file}::{'/'.join((*self.describe, self.title))}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "testId": self.test_id,
            "title": self.title,
            "describe": list(self.describe),
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "tags": list(self.tags),
            "covers": list(self.covers),
        }


def project_root_of(report: dict[str, Any]) -> Path | None:
    config = report.get("config") or {}
    config_file = config.get("configFile")
    if config_file:
        return Path(config_file).parent
    root = config.get("rootDir")
    return Path(root) if root else None


def iter_specs(report: dict[str, Any]):
    """Yield `(describe path, file suite, spec)` for every spec in a report.

    A top-level suite is a file; a nested one is a `describe`.
    """
    def inner(file_suite: dict[str, Any], suite: dict[str, Any], path: tuple[str, ...]):
        for spec in suite.get("specs") or []:
            yield path, file_suite, spec
        for child in suite.get("suites") or []:
            yield from inner(file_suite, child, (*path, child.get("title", "")))

    for file_suite in report.get("suites") or []:
        yield from inner(file_suite, file_suite, ())


def _spec_file(spec: dict[str, Any], file_suite: dict[str, Any], report: dict[str, Any],
               root: Path | None) -> str:
    name = spec.get("file") or file_suite.get("file") or file_suite.get("title") or ""
    test_dir = (report.get("config") or {}).get("rootDir")
    if test_dir and root is not None and not Path(name).is_absolute():
        return _relative(str(Path(test_dir) / name), root) or name
    return _relative(name, root) or name


def listed_tests(report: dict[str, Any], root: Path | None = None) -> list[ListedTest]:
    """Every test in a `--list` (or run) report, with its identity and `covers`."""
    root = root or project_root_of(report)
    out: list[ListedTest] = []
    for describe, file_suite, spec in iter_specs(report):
        tags = tuple(spec.get("tags") or ())
        ids = test_ids_in(tags)
        for test in spec.get("tests") or []:
            covers = tuple(
                str(a.get("description", ""))
                for a in test.get("annotations") or []
                if a.get("type") == "covers" and a.get("description")
            )
            project = str(test.get("projectName") or "")
            out.append(ListedTest(
                key=f"{spec.get('id', '')}::{project}",
                test_id=ids[0] if ids else None,
                title=str(spec.get("title", "")),
                describe=tuple(describe),
                file=_spec_file(spec, file_suite, report, root),
                line=spec.get("line"),
                column=spec.get("column"),
                tags=tags,
                covers=covers,
                project=project,
            ))
    return out


def registry_problems(tests: Sequence[ListedTest]) -> list[dict[str, Any]]:
    """What is wrong with the identities in a listing.

    A duplicate ID would make one finding stand for two tests; an unidentified
    test cannot be named in a finding or rerun by ID; two IDs on one test make
    the identity ambiguous.
    """
    problems: list[dict[str, Any]] = []
    seen: dict[str, list[ListedTest]] = {}
    for test in tests:
        ids = test_ids_in(test.tags)
        if not ids:
            problems.append({"kind": "unidentified", "file": test.file, "title": test.title})
            continue
        if len(ids) > 1:
            problems.append({"kind": "multiple_ids", "ids": ids, "file": test.file, "title": test.title})
        seen.setdefault(ids[0], []).append(test)
    for test_id, owners in sorted(seen.items()):
        specs = {owner.key.split("::")[0] for owner in owners}
        if len(specs) > 1:
            problems.append({
                "kind": "duplicate_id",
                "id": test_id,
                "where": sorted({f"{o.file}::{o.title}" for o in owners}),
            })
    return problems


# --------------------------------------------------------------------- evaluation


def _error_entries(result: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [e for e in (result.get("errors") or []) if isinstance(e, dict)]
    if not entries and isinstance(result.get("error"), dict):
        entries = [result["error"]]
    return entries


def _primary_message(result: dict[str, Any]) -> str:
    entries = _error_entries(result)
    return str(entries[0].get("message", "")) if entries else ""


def _all_messages(result: dict[str, Any]) -> str:
    return "\n".join(str(e.get("message", "")) for e in _error_entries(result))


def _split_frame(message: str) -> tuple[str, str | None]:
    """The message without its code frame, and the statement that failed.

    The statement is the source line the frame points at (`> 24 | ...`). It is
    used as the failure's logical position because, unlike the line number, it
    survives code that moves.
    """
    statement: str | None = None
    kept: list[str] = []
    for line in message.splitlines():
        match = _CODE_FRAME_RE.match(line)
        if match and statement is None:
            statement = " ".join(match.group(1).split())
        kept.append(line)
    return "\n".join(kept), statement


def _category(message: str, everything: str) -> str:
    if _ENVIRONMENT_RE.search(everything):
        return "environment"
    first = message.lstrip().splitlines()[0] if message.strip() else ""
    if "Test timeout of" in first:
        return "timeout"
    if first.startswith("TimeoutError") and "locator" in message:
        return "selector"
    if "strict mode violation" in message:
        return "selector"
    if any("expect(" in line for line in message.lstrip().splitlines()[:6]):
        return "assertion"
    if "Timeout" in first or "timed out" in first.lower():
        return "timeout"
    return "unknown"


def _expected_received(message: str) -> tuple[str | None, str | None]:
    """Best effort: both values, or neither. The message is always kept whole."""
    expected = received = None
    for line in message.splitlines():
        if expected is None:
            found = _EXPECTED_RE.match(line)
            if found:
                expected = found.group(1)
                continue
        if received is None:
            found = _RECEIVED_RE.match(line)
            if found:
                received = found.group(1)
    if expected is not None and received is not None:
        return expected, received
    return None, None


def _signature_text(message: str, expected: str | None, received: str | None) -> str:
    if expected is not None and received is not None:
        head = message.splitlines()[0] if message.strip() else ""
        return f"{head}\nExpected: {expected}\nReceived: {received}"
    return "\n".join(message.splitlines()[:3])


def _attachment_kind(attachment: dict[str, Any]) -> str:
    name = str(attachment.get("name", ""))
    if name in {"screenshot", "video", "trace", "error-context"}:
        return name
    return "other"


def _attempt_record(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt": result.get("retry", 0),
        "status": result.get("status"),
        "durationMs": result.get("duration"),
        "startedAt": result.get("startTime"),
    }


def _failure_of(test: ListedTest, results: list[dict[str, Any]], root: Path | None) -> dict[str, Any]:
    failing = [r for r in results if r.get("status") not in {"passed", "skipped"}]
    primary = failing[0] if failing else results[0]
    raw_message = clean_text(_primary_message(primary), root)
    everything = clean_text(_all_messages(primary), root)
    message, statement = _split_frame(raw_message)
    category = _category(raw_message, everything)
    expected, received = _expected_received(raw_message)
    location = (primary.get("errorLocation") or
                (_error_entries(primary)[0].get("location") if _error_entries(primary) else None))
    loc = None
    if isinstance(location, dict):
        loc = {"file": _relative(location.get("file"), root),
               "line": location.get("line"), "column": location.get("column")}
    step = statement or test.title
    fingerprint = _short_hash(test.identity, test.file, "/".join(test.describe), step, category)
    signature = _signature_text(raw_message, expected, received)
    lines = message.splitlines()
    truncated = len(lines) > MAX_ERROR_LINES
    return {
        "attempt": primary.get("retry", 0),
        "step": step,
        "category": category,
        "categoryIsHeuristic": True,
        "expected": expected,
        "observed": received,
        "error": "\n".join(lines[:MAX_ERROR_LINES]),
        "errorTruncated": truncated,
        "location": loc,
        "fingerprint": fingerprint,
        "observedSignature": {"sha": _short_hash(signature), "text": signature[:200]},
    }


def _status_of(test_entry: dict[str, Any]) -> str:
    return {
        "expected": "pass",
        "unexpected": "fail",
        "flaky": "flaky",
        "skipped": "skipped",
    }.get(str(test_entry.get("status")), "error")


def _executed(test_entry: dict[str, Any]) -> bool:
    return any(r.get("status") != "skipped" for r in test_entry.get("results") or [])


def _attachments(results: list[dict[str, Any]], root: Path | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for result in results:
        for attachment in result.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            out.append({
                "kind": _attachment_kind(attachment),
                "attempt": result.get("retry", 0),
                "contentType": attachment.get("contentType"),
                "source": attachment.get("path"),
            })
    return out


def evaluate(
    report: dict[str, Any] | None,
    expected: Sequence[ListedTest],
    exit_code: int | None,
    *,
    selection_kind: str = "tag",
    root: Path | None = None,
) -> dict[str, Any]:
    """Judge one Playwright run. Pure: a report, the selection, and an exit status.

    Returns the status, why, the counts, and one result per selected test.
    Precedence: `blocked`, `fail`, `error`, `flaky`, `skipped`, `pass`. A real
    failure is evidence even when the run is also incomplete; an incomplete run
    is never a pass.
    """
    outcome: dict[str, Any] = {"status": "error", "reason": None, "errors": [],
                               "counts": _empty_counts(len(expected)), "results": []}
    if exit_code is None:
        outcome["reason"] = "timeout"
        return outcome
    if report is None:
        outcome["reason"] = "report_missing_or_invalid"
        return outcome

    root = root or project_root_of(report)
    run_errors = [clean_text(str(e.get("message", "")), root)
                  for e in report.get("errors") or [] if isinstance(e, dict)]
    outcome["errors"] = run_errors

    by_key: dict[str, tuple[ListedTest, dict[str, Any]]] = {}
    for listed in listed_tests(report, root):
        by_key[listed.key] = (listed, {})
    for describe, file_suite, spec in iter_specs(report):
        for test_entry in spec.get("tests") or []:
            key = f"{spec.get('id', '')}::{test_entry.get('projectName') or ''}"
            if key in by_key:
                by_key[key] = (by_key[key][0], test_entry)

    if not expected:
        outcome["status"] = "not_configured" if selection_kind == "tag" else "error"
        outcome["reason"] = "no_tests_selected" if selection_kind == "tag" else "unknown_test_ids"
        return outcome

    counts = _empty_counts(len(expected))
    results: list[dict[str, Any]] = []
    missing: list[str] = []
    for listed in expected:
        _, entry = by_key.get(listed.key, (listed, {}))
        if not entry:
            missing.append(listed.identity)
            results.append(_result_record(listed, None, root))
            continue
        record = _result_record(listed, entry, root)
        results.append(record)
        if _executed(entry):
            counts["executed"] += 1
        status = record["status"]
        counts[{"pass": "passed", "fail": "failed", "flaky": "flaky",
                "skipped": "skipped"}.get(status, "other")] += 1
    counts["missing"] = len(missing)
    outcome["counts"] = counts
    outcome["results"] = results

    if run_errors and counts["executed"] == 0:
        blocked = any("webServer" in message for message in run_errors)
        outcome["status"] = "blocked" if blocked else "error"
        outcome["reason"] = "web_server_failed" if blocked else "run_level_error"
        return outcome

    failed = [r for r in results if r["status"] == "fail"]
    if failed:
        environment = all((r.get("failure") or {}).get("category") == "environment" for r in failed)
        if environment and counts["passed"] == 0:
            outcome["status"] = "blocked"
            outcome["reason"] = "environment_suspected"
        else:
            outcome["status"] = "fail"
            outcome["reason"] = None
        return outcome

    if missing:
        outcome["status"] = "error"
        outcome["reason"] = "selection_incomplete"
        outcome["missing"] = missing
        return outcome
    expected_keys = {listed.key for listed in expected}
    extra = sorted(listed.identity for key, (listed, entry) in by_key.items()
                   if entry and _executed(entry) and key not in expected_keys)
    if extra:
        outcome["status"] = "error"
        outcome["reason"] = "selection_exceeded"
        outcome["unexpected_tests"] = extra
        return outcome
    if exit_code != 0 and not run_errors:
        outcome["status"] = "error"
        outcome["reason"] = "exit_code_inconsistent"
        return outcome
    if run_errors:
        outcome["status"] = "error"
        outcome["reason"] = "run_level_error"
        return outcome
    if counts["flaky"]:
        outcome["status"] = "flaky"
        return outcome
    if counts["skipped"] or counts["executed"] != len(expected):
        outcome["status"] = "skipped"
        outcome["reason"] = "selected_test_skipped"
        return outcome
    outcome["status"] = "pass"
    return outcome


def _empty_counts(total: int) -> dict[str, int]:
    return {"selected": total, "executed": 0, "passed": 0, "failed": 0,
            "flaky": 0, "skipped": 0, "other": 0, "missing": 0}


def _result_record(listed: ListedTest, entry: dict[str, Any] | None,
                   root: Path | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "testId": listed.test_id,
        "identity": listed.identity,
        "title": listed.title,
        "file": listed.file,
        "line": listed.line,
        "tags": [t for t in listed.tags if t != BEHAVIORAL_TAG],
        "covers": list(listed.covers),
        "executionMode": "deterministic",
    }
    if not entry:
        record.update({"status": "error", "reason": "absent_from_report",
                       "stability": None, "attempts": [], "durationMs": 0, "evidence": []})
        return record
    results = [r for r in entry.get("results") or [] if isinstance(r, dict)]
    status = _status_of(entry)
    failing = [r for r in results if r.get("status") not in {"passed", "skipped"}]
    stability = None
    if status == "fail":
        stability = "repeated_in_run" if len(failing) >= 2 else "single_attempt"
    elif status == "flaky":
        stability = "passed_on_retry"
    record.update({
        "status": status,
        "stability": stability,
        "attempts": [_attempt_record(r) for r in results],
        "durationMs": round(sum(float(r.get("duration") or 0) for r in results)),
        "evidence": _attachments(results, root),
    })
    if status in {"fail", "flaky"} and results:
        record["failure"] = _failure_of(listed, results, root)
    return record


# ----------------------------------------------------------------- run record I/O


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_run_id(now: dt.datetime | None = None) -> str:
    stamp = (now or _utc_now()).strftime("%Y%m%d-%H%M%S")
    return f"bv-{stamp}-{uuid.uuid4().hex[:6]}"


def evidence_root() -> Path:
    """Beside the other host-local state, outside every repository."""
    from telemetry import default_database_path

    return default_database_path().parent / "evidence" / "behavioral"


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:80] or "test"


def _materialize_evidence(run_dir: Path, output_dir: Path | None,
                          results: list[dict[str, Any]]) -> None:
    """Move referenced artifacts under `artifacts/<test>/attempt-N/`.

    Only files inside Playwright's output directory are copied. A test can attach
    any path it likes; a path outside the output directory is recorded and left
    alone, so a test cannot make the evidence directory a copy of the host.
    """
    for result in results:
        kept: list[dict[str, Any]] = []
        for item in result.get("evidence") or []:
            source = item.pop("source", None)
            entry = {k: v for k, v in item.items()}
            path = Path(source) if source else None
            inside = False
            if path is not None and output_dir is not None:
                try:
                    path.resolve().relative_to(output_dir.resolve())
                    inside = True
                except (ValueError, OSError):
                    inside = False
            if path is None or not inside:
                entry["status"] = "not_collected"
                entry["reason"] = ("no_path" if path is None else
                                   "output_dir_unknown" if output_dir is None else "outside_output_dir")
            elif not path.is_file():
                entry["status"] = "missing"
            else:
                dest_dir = run_dir / "artifacts" / _safe_name(result["identity"]) / f"attempt-{item['attempt']}"
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / path.name
                shutil.copy2(path, dest)
                entry.update({"status": "collected", "path": dest.relative_to(run_dir).as_posix(),
                              "bytes": dest.stat().st_size, "sha256": sha256_file(dest)})
            kept.append(entry)
        result["evidence"] = kept


def render_summary(run: dict[str, Any]) -> str:
    counts = run["counts"]
    sel = run["selection"]
    lines = [
        f"# Behavioral run {run['runId']}: {run['status'].upper()}",
        "",
        f"- commit: {run.get('commit') or 'unknown'}"
        f"{' (dirty)' if run.get('dirty') else ''} · playwright {run.get('toolVersion') or 'unknown'}"
        f" · selection {sel['kind']}:{sel.get('value')}",
        f"- selected {counts['selected']} · executed {counts['executed']} · passed {counts['passed']}"
        f" · failed {counts['failed']} · flaky {counts['flaky']} · skipped {counts['skipped']}"
        f" · missing {counts['missing']}",
    ]
    if run.get("reason"):
        lines.append(f"- reason: {run['reason']}")
    for message in run.get("errors") or []:
        lines.append(f"- run-level error: {message.splitlines()[0] if message else ''}")
    problem = [r for r in run["results"] if r["status"] in {"fail", "flaky", "error"}]
    if problem:
        shown = problem[:SUMMARY_FAILURES]
        lines += ["", f"## Failures (showing {len(shown)} of {len(problem)})"]
        for result in shown:
            failure = result.get("failure") or {}
            attempts = ", ".join(str(a.get("status")) for a in result["attempts"]) or "none"
            lines += ["", f"### {result['identity']}: {result['title']}"
                          f" ({result['file']}:{result.get('line')})",
                      f"- status {result['status']} · attempts: {attempts}"
                      f"{' · ' + result['stability'] if result.get('stability') else ''}"
                      f" · category: {failure.get('category', 'n/a')} (heuristic)"]
            if failure:
                lines.append(f"- step: {failure.get('step')}")
                if failure.get("expected") is not None:
                    lines.append(f"- expected: {failure['expected']} · observed: {failure['observed']}")
                excerpt = [l for l in failure["error"].splitlines() if l.strip()][:SUMMARY_ERROR_LINES]
                lines += ["- error:"] + [f"    {l}" for l in excerpt]
            kinds = sorted({f"{e['kind']}@{e['attempt']}" for e in result["evidence"]
                            if e.get("status") == "collected"})
            if kinds:
                lines.append(f"- evidence: {', '.join(kinds)}")
            if failure:
                lines.append(f"- details: failures/{_safe_name(result['identity'])}/bundle.json")
    return "\n".join(lines) + "\n"


def write_run(run_dir: Path, run: dict[str, Any], raw: bytes | None,
              output_dir: Path | None) -> dict[str, Any]:
    """Persist one run. `raw/playwright.json` is written first and hashed."""
    run_dir.mkdir(parents=True, exist_ok=True)
    source = {"kind": "playwright-json", "path": None, "sha256": None, "retained": False}
    if raw is not None:
        (run_dir / "raw").mkdir(exist_ok=True)
        (run_dir / "raw" / "playwright.json").write_bytes(raw)
        source.update({"path": "raw/playwright.json", "sha256": sha256_bytes(raw), "retained": True})
    run["source"] = source
    _materialize_evidence(run_dir, output_dir, run["results"])
    for result in run["results"]:
        failure = result.get("failure")
        if not failure:
            continue
        bundle_dir = run_dir / "failures" / _safe_name(result["identity"])
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle = {"schema": SCHEMA_VERSION, "runId": run["runId"], "result": result}
        (bundle_dir / "bundle.json").write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps(run, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(render_summary(run), encoding="utf-8")
    run["runJsonSha256"] = sha256_file(run_dir / "run.json")
    return run


# ---------------------------------------------------------------- process running


def _git(project: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(project), *args], capture_output=True,
                              text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def git_state(project: Path) -> tuple[str | None, bool | None]:
    head = _git(project, "rev-parse", "HEAD")
    status = _git(project, "status", "--porcelain")
    return (head.strip() if head else None,
            (bool(status.strip()) if status is not None else None))


def find_config(project: Path) -> Path | None:
    for name in CONFIG_NAMES:
        candidate = project / name
        if candidate.is_file():
            return candidate
    return None


def run_process(argv: Sequence[str], cwd: Path, env: dict[str, str], timeout: float) -> int | None:
    """Run `argv` without a shell. `None` means it timed out and was killed."""
    kwargs: dict[str, Any] = {"cwd": str(cwd), "env": env, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    process = subprocess.Popen(list(argv), **kwargs)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass
        process.wait()
        return None


def _env(report_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PLAYWRIGHT_JSON_OUTPUT_NAME": str(report_path),
        "PLAYWRIGHT_HTML_OPEN": "never",
        "FORCE_COLOR": "0",
        "NO_COLOR": "1",
    })
    env.update(extra or {})
    return env


def parse_version(text: str) -> tuple[int, int, int] | None:
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(p) for p in found.groups()) if found else None  # type: ignore[return-value]


def playwright_version(project: Path, command: Sequence[str] = PLAYWRIGHT_COMMAND) -> str | None:
    try:
        done = subprocess.run([*command, "--version"], cwd=str(project), capture_output=True,
                              text=True, timeout=60, env={**os.environ, "NO_COLOR": "1"})
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    version = parse_version(done.stdout)
    return ".".join(str(p) for p in version) if version else None


def load_report(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "suites" in data and "stats" in data else None


def list_selection(project: Path, grep: str, work_dir: Path, *, runner: Runner = run_process,
                   command: Sequence[str] = PLAYWRIGHT_COMMAND, timeout: float = 120,
                   env_extra: dict[str, str] | None = None) -> tuple[list[ListedTest] | None, Path]:
    """The tests `grep` selects, from `--list --reporter=json` (the report file only)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    report_path = work_dir / "list.json"
    argv = [*command, "test", "--list", "--reporter=json", "--grep", grep]
    code = runner(argv, project, _env(report_path, env_extra), timeout)
    report = load_report(report_path) if code is not None else None
    if report is None:
        return None, report_path
    return listed_tests(report, project), report_path


def run_behavioral(
    project: Path,
    evidence_dir: Path,
    *,
    ids: Sequence[str] | None = None,
    tag: str = BEHAVIORAL_TAG,
    retries: int = 1,
    timeout: float = 900,
    base_url: str | None = None,
    runner: Runner = run_process,
    command: Sequence[str] = PLAYWRIGHT_COMMAND,
    min_version: tuple[int, int, int] = MIN_PLAYWRIGHT,
    probe_version: Callable[[Path], str | None] | None = None,
) -> dict[str, Any]:
    """Run the selected tests and persist the run under `evidence_dir`."""
    started = _utc_now()
    run_id = new_run_id(started)
    run_dir = evidence_dir / run_id
    grep = selection_grep(ids, tag)
    selection = {"kind": "ids" if ids else "tag", "value": ",".join(ids) if ids else tag, "grep": grep}
    commit, dirty = git_state(project)
    config = find_config(project)
    version = (probe_version or (lambda p: playwright_version(p, command)))(project)
    run: dict[str, Any] = {
        "schema": SCHEMA_VERSION, "runId": run_id, "tool": "playwright", "toolVersion": version,
        "configHash": sha256_file(config) if config else None,
        "commit": commit, "dirty": dirty, "selection": selection, "exitCode": None,
        "startedAt": started.isoformat(), "errors": [], "counts": _empty_counts(0), "results": [],
    }

    def finish(status: str, reason: str | None, raw: bytes | None = None,
               output_dir: Path | None = None) -> dict[str, Any]:
        finished = _utc_now()
        run.update({"status": status, "reason": reason, "finishedAt": finished.isoformat(),
                    "durationMs": round((finished - started).total_seconds() * 1000)})
        write_run(run_dir, run, raw, output_dir)
        shutil.rmtree(run_dir / ".playwright-output", ignore_errors=True)
        return run

    if config is None:
        return finish("not_configured", "no_playwright_config")
    parsed = parse_version(version or "")
    if version is None:
        return finish("blocked", "playwright_unavailable")
    if parsed is None or parsed < min_version:
        return finish("blocked", f"playwright_below_{'.'.join(map(str, min_version))}")

    env_extra = {"BASE_URL": base_url} if base_url else None
    expected, list_path = list_selection(project, grep, run_dir / ".list", runner=runner,
                                         command=command, env_extra=env_extra)
    shutil.rmtree(run_dir / ".list", ignore_errors=True)
    if expected is None:
        return finish("error", "list_failed")

    output_dir = run_dir / ".playwright-output"
    report_path = run_dir / "raw" / "playwright.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [*command, "test", "--reporter=line,json", f"--retries={retries}",
            "--trace=retain-on-first-failure", f"--output={output_dir}", "--forbid-only",
            "--grep", grep]
    code = runner(argv, project, _env(report_path, env_extra), timeout)
    run["exitCode"] = code
    raw = report_path.read_bytes() if report_path.is_file() else None
    report = load_report(report_path) if raw is not None else None
    outcome = evaluate(report, expected, code, selection_kind=selection["kind"], root=project)
    run.update({k: outcome[k] for k in ("errors", "counts", "results")})
    for extra_key in ("missing", "unexpected_tests"):
        if outcome.get(extra_key):
            run[extra_key] = outcome[extra_key]
    return finish(outcome["status"], outcome["reason"], raw, output_dir)


def normalize(raw_path: Path, list_path: Path, exit_code: int, evidence_dir: Path, *,
              selection_kind: str = "tag", selection_value: str = BEHAVIORAL_TAG,
              project: Path | None = None, output_dir: Path | None = None,
              tool_version: str | None = None) -> dict[str, Any]:
    """Normalise a run that already happened (a CI job): report, listing, exit status."""
    started = _utc_now()
    run_id = new_run_id(started)
    raw = raw_path.read_bytes() if raw_path.is_file() else None
    report = load_report(raw_path) if raw is not None else None
    listing = load_report(list_path)
    expected = listed_tests(listing, project) if listing else []
    outcome = evaluate(report, expected, exit_code, selection_kind=selection_kind, root=project)
    commit, dirty = git_state(project) if project else (None, None)
    run: dict[str, Any] = {
        "schema": SCHEMA_VERSION, "runId": run_id, "tool": "playwright", "toolVersion": tool_version,
        "configHash": None, "commit": commit, "dirty": dirty,
        "selection": {"kind": selection_kind, "value": selection_value, "grep": None},
        "exitCode": exit_code, "startedAt": started.isoformat(),
        "finishedAt": _utc_now().isoformat(), "durationMs": 0, "status": outcome["status"],
        "reason": outcome["reason"], "errors": outcome["errors"], "counts": outcome["counts"],
        "results": outcome["results"],
    }
    return write_run(evidence_dir / run_id, run, raw, output_dir)


# --------------------------------------------------------------------------- CLI


def _summary(run: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    return {
        "runId": run["runId"], "status": run["status"], "reason": run.get("reason"),
        "exitCode": EXIT_CODES[run["status"]], "counts": run["counts"],
        "runDir": str(run_dir), "runJsonSha256": run.get("runJsonSha256"),
        "summary": str(run_dir / "summary.md"),
    }


def _ids(value: str | None) -> list[str] | None:
    return [part.strip() for part in value.split(",") if part.strip()] if value else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="behavioral.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list the behavioral tests and the problems in their identities")
    listing.add_argument("--project-dir", default=".")
    listing.add_argument("--tag", default=BEHAVIORAL_TAG)

    run = sub.add_parser("run", help="run the selected tests and persist the run")
    run.add_argument("--project-dir", default=".")
    run.add_argument("--ids", help="comma-separated test IDs; default: every test with the tag")
    run.add_argument("--tag", default=BEHAVIORAL_TAG)
    run.add_argument("--cycle-id", default="manual")
    run.add_argument("--evidence-dir", help="default: <CODE_CYCLE_HOME>/evidence/behavioral/<cycle-id>")
    run.add_argument("--retries", type=int, default=1)
    run.add_argument("--timeout", type=float, default=900)
    run.add_argument("--base-url")

    norm = sub.add_parser("normalize", help="normalise a run that already happened (CI)")
    norm.add_argument("--report", required=True, help="the reporter's JSON file")
    norm.add_argument("--list-report", required=True, help="`--list --reporter=json` output for the same selection")
    norm.add_argument("--exit-code", type=int, required=True)
    norm.add_argument("--project-dir", default=".")
    norm.add_argument("--ids")
    norm.add_argument("--tag", default=BEHAVIORAL_TAG)
    norm.add_argument("--output-dir", help="Playwright's output directory, for the artifacts")
    norm.add_argument("--cycle-id", default="manual")
    norm.add_argument("--evidence-dir")

    args = parser.parse_args(argv)
    project = Path(args.project_dir).resolve()
    try:
        if args.command == "list":
            grep = selection_grep(None, args.tag)
            work = Path(tempfile.mkdtemp(prefix="behavioral-list-"))
            try:
                tests, _ = list_selection(project, grep, work)
            finally:
                shutil.rmtree(work, ignore_errors=True)
            if tests is None:
                print(json.dumps({"error": "list_failed"}))
                return EXIT_CODES["error"]
            print(json.dumps({"tests": [t.as_dict() for t in tests],
                              "problems": registry_problems(tests)}, indent=2))
            return 0
        evidence = (Path(args.evidence_dir) if args.evidence_dir
                    else evidence_root() / _safe_name(args.cycle_id))
        if args.command == "run":
            result = run_behavioral(project, evidence, ids=_ids(args.ids), tag=args.tag,
                                    retries=args.retries, timeout=args.timeout,
                                    base_url=args.base_url)
        else:
            ids = _ids(args.ids)
            result = normalize(Path(args.report), Path(args.list_report), args.exit_code, evidence,
                               selection_kind="ids" if ids else "tag",
                               selection_value=",".join(ids) if ids else args.tag, project=project,
                               output_dir=Path(args.output_dir) if args.output_dir else None)
    except BehavioralError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return EXIT_CODES["error"]
    run_dir = evidence / result["runId"]
    print(json.dumps(_summary(result, run_dir), indent=2))
    return EXIT_CODES[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
