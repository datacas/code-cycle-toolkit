"""The local copy of a change request's review history, verified before use.

A resumed cycle needs the review runs, claims and survivals its change request
already published. Reading them from the change request means fetching and
parsing every comment on every resume. This store keeps the tokens those
comments carry in the telemetry database, and a resume on the same machine
reuses them once one lightweight remote check shows that nothing changed.

Three rules hold it in place:

- **The change request stays the record.** Nothing a resume needs exists only
  here. A missing, outdated or unverifiable store is rebuilt from the trusted
  comments, never read as "no earlier findings".
- **Identifiers and closed tokens only.** Comment IDs and edit times, run IDs,
  `REV-` IDs with their severity, status, disposition and blocking flag, attempt
  IDs, stage tokens and SHAs. Finding titles, comment bodies and authors' logins
  are never stored; a hash of the trusted-author list says when it changed.
- **Verified, or not used.** Local history counts only when its sync watermark
  matches the change request's current head, comment count and latest edit. On
  a mismatch the changed comments are fetched and reconciled; when the count
  still disagrees, a comment was deleted and the history is rebuilt.

Stages keep publishing their own comments. The outbox here only records which
attempt is expected to publish and whether a comment carrying that attempt's
schema-2 receipt was observed, so a lost publication is detected instead of
silently missing from the durable record.

GitHub offers no atomic check-and-publish. The per-machine writer lock, and
the sync after each publishing stage that treats a receipt from an attempt
nobody here dispatched as a conflict, reduce conflicting writers; they do not
exclude another machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .execution_plan import ExecutionPlan

from review_contract import (
    CLAIM_STATUSES,
    RESOLUTION_RUN,
    ContractError,
    Finding,
    HistoryEntry,
    ProviderComment,
    RunStatuses,
    claimed_fix_survivals,
    fold_history,
    history_entry,
    trusted_logins,
)

#: Bumped when the meaning of the history rows changes. A watermark written
#: under another version is not trusted, and the history is rebuilt.
HISTORY_SCHEMA_VERSION = 1

#: A lock older than this is stale even when its process cannot be checked.
LOCK_STALE_AFTER = timedelta(hours=24)

#: Stages whose result is published as a change-request comment, so the outbox
#: can confirm it. `implement` publishes the change request itself.
COMMENT_STAGES = frozenset({"review", "resolve", "rereview"})
STAGES = frozenset({"implement"}) | COMMENT_STAGES

_COMMENT_ID = re.compile(r"[0-9]{1,20}")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})")
_RUN_ID = re.compile(r"CC[RT]-[A-Za-z0-9-]{1,60}")
_FINDING_ID = re.compile(r"REV-[A-Za-z0-9-]{1,40}")
_ATTEMPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA = re.compile(r"[0-9a-f]{40}")
_STATUS_TOKEN = re.compile(r"[A-Z_]{1,40}")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#@:+-]{0,199}")


class HistoryError(ValueError):
    """The local history cannot be used or written as asked."""


class LockHeld(HistoryError):
    """Another live writer on this machine holds the change request."""


@dataclass(frozen=True)
class RemoteMark:
    """What one lightweight remote check observed about a change request.

    `latest_updated_at` is the newest edit time of any comment, so a new
    comment and an edit to an old one both move it; a deletion moves the count.
    """

    head_sha: str | None
    comment_count: int
    latest_updated_at: str | None


class HistoryRemote(Protocol):
    """How the store reads a change request; injected so tests need no network."""

    def probe(self, repo_id: str, change_request_id: str) -> RemoteMark | None:
        """The current mark, or None when this remote cannot provide one."""

    def comments(self, repo_id: str, change_request_id: str,
                 since: str | None = None) -> list[ProviderComment]:
        """Comments oldest first; with `since`, only those edited at or after it."""


@dataclass(frozen=True)
class HistorySync:
    """The history a sync produced and how it was obtained.

    `source` is `local` when the watermark matched and nothing was fetched,
    `reconciled` when only changed comments were fetched, and `rebuilt` when
    every comment was. `foreign` lists the attempts whose receipts appeared
    since the previous sync without this store expecting them: another writer
    published. It is empty on a first sync, which has nothing to compare with.
    """

    runs: list[RunStatuses]
    source: str
    fetched: int
    foreign: tuple[str, ...] = ()


def trusted_fingerprint(trusted_authors: Iterable[str]) -> str:
    """A hash that says whether the trusted-author list changed, not who is in it."""
    joined = "\n".join(sorted(trusted_logins(trusted_authors)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _match(pattern: re.Pattern, value, what: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise HistoryError(f"{what} is not a valid identifier: {value!r}")
    return value


#: Windows API values `_windows_process_alive` uses.
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102


def _windows_process_alive(pid: int, kernel32=None, last_error=None) -> bool | None:
    """Whether `pid` runs on Windows, or None when the API cannot tell.

    `kernel32` and `last_error` are injected by tests on other platforms.
    """
    if not 0 < pid <= 0xFFFFFFFF:
        return None  # not a Windows process ID; the age rule decides
    if kernel32 is None:
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except (ImportError, OSError, AttributeError):
            return None
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        last_error = ctypes.get_last_error
    handle = kernel32.OpenProcess(
        _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = last_error()
        if error == _ERROR_INVALID_PARAMETER:
            return False  # no process has this ID
        if error == _ERROR_ACCESS_DENIED:
            return True  # it exists, owned by someone else
        return None
    try:
        # A process that exited is signalled even while handles to it remain.
        waited = kernel32.WaitForSingleObject(handle, 0)
    finally:
        kernel32.CloseHandle(handle)
    if waited == _WAIT_OBJECT_0:
        return False
    if waited == _WAIT_TIMEOUT:
        return True
    return None


def _process_alive(pid: int) -> bool | None:
    """True, False, or None when this platform cannot tell."""
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def command_digest(command: str) -> str:
    if re.fullmatch(r"sha256:[a-f0-9]{64}", command):
        return command
    return "sha256:" + hashlib.sha256(command.encode()).hexdigest()


def plan_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def plan_projection(plan: ExecutionPlan) -> dict[str, Any]:
    """Versioned metadata, without criterion prose, commands, output or prompts."""
    def clean(value, key=""):
        if key in {"reason", "condition", "source", "policy_source", "provenance",
                   "plan_provenance"}:
            return "runtime"
        if key == "command":
            return command_digest(value)
        if key in {"id", "executor", "profile", "context"} and isinstance(value, str):
            return (value if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value)
                    else "ref-" + hashlib.sha256(value.encode()).hexdigest())
        if key == "items":
            return [f"AC-{i}" for i in range(1, len(value) + 1)]
        if key == "cause":
            if value in {"defect", "blocked", "omitted", "missing_tool", "inconclusive",
                         "environment", "stale", "unverified", ""}:
                return value
            lowered = value.lower()
            for token, phrases in {"omitted": ("no execution result", "omitted"),
                                   "missing_tool": ("not found", "missing tool"),
                                   "blocked": ("denied", "blocked", "permission"),
                                   "inconclusive": ("inconclusive",),
                                   "environment": ("sandbox", "environment",)}.items():
                if any(phrase in lowered for phrase in phrases):
                    return token
            return "unverified"
        if key == "metadata":
            conclusion = value.get("verification_conclusion")
            return ({"verification_conclusion": conclusion} if conclusion in {
                "verified", "verified_with_reservations", "not_verified", "failed"} else {})
        if key in {"attempts", "review"}:
            return [] if key == "attempts" else {}
        if key == "evidence":
            return [{k: clean(v, k) for k, v in item.items()
                     if k in {"command", "level", "exit_code", "executed", "test_sha256",
                              "output_sha256", "context_sha256"}} for item in value]
        if isinstance(value, dict):
            return {k: clean(v, k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        return value
    return {"projection_version": 1, "plan": clean(plan.to_dict())}


def restore_plan(record: dict[str, Any], commands: tuple[str, ...]) -> ExecutionPlan:
    """Recover only checks whose command is still explicitly authorized."""
    if not TYPE_CHECKING:
        from execution_plan import ExecutionPlan
    payload = record["payload"]
    if payload.get("projection_version") != 1 or plan_digest(payload) != record["digest"]:
        raise HistoryError("unsupported or corrupt execution plan")
    bindings = {command_digest(command): command for command in commands}
    def bind(value, key=""):
        if key == "command":
            if value not in bindings:
                raise HistoryError("stored check has no currently authorized command binding")
            return bindings[value]
        if isinstance(value, dict):
            return {k: bind(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [bind(v) for v in value]
        return value
    return ExecutionPlan.from_dict(bind(payload["plan"]))


class CycleHistoryStore:
    """The cycle-history tables of a telemetry database.

    The tables are created by `telemetry.Telemetry`, so there is one database
    and one schema; this class only reads and writes them.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def save_plan(self, plan: ExecutionPlan, *, cycle_id: str, change_request_id: str | None,
                  provenance: str, stop_reason: str | None = None) -> dict[str, Any]:
        """Append a privacy-safe revision. Commands and prose stay out of SQLite.

        Digests use canonical JSON projection version 1, not a prompt or a
        platform-dependent rendering. Rebinding command hashes on recovery
        requires a currently authorized command; history grants no execution.
        """
        if provenance not in {"initial", "observed", "recovered", "partial", "stopped"}:
            raise HistoryError("unknown plan provenance")
        payload = plan_projection(plan)
        digest = plan_digest(payload)
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO cycle_execution_plans "
                "(repo_id,task_id,change_request_id,cycle_id,plan_id,revision,digest,"
                "head_sha,provenance,stop_reason,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (plan.repository, plan.work_item, change_request_id, cycle_id,
                 plan.plan_id, plan.revision, digest, plan.head_sha, provenance,
                 stop_reason, json.dumps(payload, sort_keys=True, separators=(",", ":"))))
            connection.commit()
        return {"plan_id": plan.plan_id, "revision": plan.revision, "digest": digest}

    def latest_plan(self, repo_id: str, task_id: str, change_request_id: str | None) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM cycle_execution_plans WHERE repo_id=? AND task_id=? "
                "AND change_request_id IS ? ORDER BY sequence DESC LIMIT 1",
                (repo_id, task_id, change_request_id)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        if plan_digest(value["payload"]) != value["digest"]:
            raise HistoryError("execution plan digest mismatch")
        return value

    def has_plan_receipt(self, repo_id: str, receipt: dict[str, Any]) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM cycle_execution_plans WHERE repo_id=? AND plan_id=? "
                "AND revision=? AND digest=? AND head_sha=?",
                (repo_id, receipt.get("plan_id"), receipt.get("revision"),
                 receipt.get("digest"), receipt.get("head_sha"))).fetchone()
        return row is not None

    # -- writer lock -------------------------------------------------------

    def acquire(self, repo_id: str, change_request_id: str) -> str:
        """Take the per-machine writer lock and return its owner token.

        A lock whose process is gone, or that is older than `LOCK_STALE_AFTER`,
        is stale and taken over; a live one raises `LockHeld`.
        """
        repo_id = _match(_REFERENCE, repo_id, "repository")
        change_request_id = _match(_REFERENCE, change_request_id, "change request")
        owner = f"lock-{uuid.uuid4().hex}"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner, pid, acquired_at FROM cycle_history_locks "
                "WHERE repo_id=? AND change_request_id=?",
                (repo_id, change_request_id)).fetchone()
            if row is not None and not self._stale(row):
                connection.rollback()
                raise LockHeld(
                    f"change request {change_request_id} is already being driven by "
                    f"process {row['pid']} on this machine since {row['acquired_at']}")
            connection.execute(
                "INSERT OR REPLACE INTO cycle_history_locks VALUES (?, ?, ?, ?, ?)",
                (repo_id, change_request_id, owner, os.getpid(), _now()))
            connection.commit()
        return owner

    @staticmethod
    def _stale(row) -> bool:
        alive = _process_alive(int(row["pid"]))
        if alive is False:
            return True
        try:
            acquired = datetime.fromisoformat(row["acquired_at"])
        except (TypeError, ValueError):
            return True
        if acquired.tzinfo is None:
            acquired = acquired.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - acquired > LOCK_STALE_AFTER

    def release(self, repo_id: str, change_request_id: str, owner: str) -> None:
        """Release the lock if this owner still holds it."""
        with closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM cycle_history_locks "
                "WHERE repo_id=? AND change_request_id=? AND owner=?",
                (repo_id, change_request_id, owner))
            connection.commit()

    # -- history -----------------------------------------------------------

    def watermark(self, repo_id: str, change_request_id: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM cycle_history_sync WHERE repo_id=? AND change_request_id=?",
                (repo_id, change_request_id)).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _matches(watermark: dict | None, mark: RemoteMark | None, fingerprint: str) -> bool:
        # Without an edit time a non-empty history cannot be shown unchanged.
        return (watermark is not None and mark is not None
                and watermark["schema_version"] == HISTORY_SCHEMA_VERSION
                and watermark["trusted_fingerprint"] == fingerprint
                and (watermark["latest_updated_at"] is not None
                     or watermark["comment_count"] == 0)
                and watermark["head_sha"] == mark.head_sha
                and watermark["comment_count"] == mark.comment_count
                and watermark["latest_updated_at"] == mark.latest_updated_at)

    def sync(self, repo_id: str, change_request_id: str, *,
             trusted_authors: Iterable[str], remote: HistoryRemote) -> HistorySync:
        """Bring the local history up to the change request, as cheaply as is safe.

        One remote check first. A matching watermark returns the stored history
        without fetching anything. A watermark from the same schema and trusted
        authors is reconciled from the comments edited since it; any other case,
        and a reconciliation whose comment count still disagrees, rebuilds from
        every comment. Remote failures propagate to the caller, which stops.
        """
        trusted_authors = tuple(trusted_authors)
        fingerprint = trusted_fingerprint(trusted_authors)
        trusted = trusted_logins(trusted_authors)
        if not trusted:
            raise ContractError("history recovery needs at least one trusted author")
        mark = remote.probe(repo_id, change_request_id)
        watermark = self.watermark(repo_id, change_request_id)
        if self._matches(watermark, mark, fingerprint):
            return HistorySync(self.runs(repo_id, change_request_id), "local", 0)
        if (mark is not None and watermark is not None
                and watermark["schema_version"] == HISTORY_SCHEMA_VERSION
                and watermark["trusted_fingerprint"] == fingerprint
                and watermark["latest_updated_at"] is not None):
            changed = remote.comments(repo_id, change_request_id,
                                      since=watermark["latest_updated_at"])
            foreign = self._write(repo_id, change_request_id, changed, trusted, mark,
                                  fingerprint, replace_all=False)
            if foreign is not None:
                return HistorySync(self.runs(repo_id, change_request_id), "reconciled",
                                   len(changed), tuple(foreign))
        comments = remote.comments(repo_id, change_request_id)
        foreign = self._write(repo_id, change_request_id, comments, trusted, mark,
                              fingerprint, replace_all=True)
        return HistorySync(self.runs(repo_id, change_request_id), "rebuilt", len(comments),
                           tuple(foreign or ()) if watermark is not None else ())

    def _entries(self, comments: list[ProviderComment], trusted: frozenset[str]
                 ) -> list[tuple[int, ProviderComment, HistoryEntry | None]]:
        read = []
        seen: set[int] = set()
        for number, comment in enumerate(comments, start=1):
            entry = history_entry(comment, trusted=trusted, number=number)
            ordinal = int(_match(_COMMENT_ID, comment.id, "comment id"))
            if ordinal in seen:
                raise HistoryError(f"comment id {comment.id} appears twice")
            seen.add(ordinal)
            if comment.updated_at is not None:
                _match(_TIMESTAMP, comment.updated_at, "comment edit time")
            read.append((ordinal, comment, entry))
        return read

    def _write(self, repo_id: str, change_request_id: str,
               comments: list[ProviderComment], trusted: frozenset[str],
               mark: RemoteMark | None, fingerprint: str, *, replace_all: bool
               ) -> list[str] | None:
        """Store comments idempotently and return the unexpected new attempts.

        None means a reconciliation must rebuild. Every comment is parsed before
        anything is written, so an unreadable one leaves the previous history
        and watermark untouched.
        """
        read = self._entries(comments, trusted)
        scope = (repo_id, change_request_id)
        receipts_query = ("SELECT DISTINCT attempt_id FROM cycle_history_receipts "
                          "WHERE repo_id=? AND change_request_id=?")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = {row[0] for row in connection.execute(receipts_query, scope)}
            if replace_all:
                for table in ("cycle_history_comments", "cycle_history_runs",
                              "cycle_history_findings", "cycle_history_receipts"):
                    connection.execute(
                        f"DELETE FROM {table} WHERE repo_id=? AND change_request_id=?", scope)
            for ordinal, comment, entry in read:
                self._put(connection, scope, ordinal, comment, entry)
            count = connection.execute(
                "SELECT COUNT(*) FROM cycle_history_comments "
                "WHERE repo_id=? AND change_request_id=?", scope).fetchone()[0]
            if not replace_all and mark is not None and count != mark.comment_count:
                # A comment was deleted, or the reconciliation missed one.
                connection.rollback()
                return None
            # An attempt this machine dispatched is expected even before the
            # outbox heard of it: telemetry records it before the dispatch.
            expected = {row[0] for row in connection.execute(
                "SELECT attempt_id FROM cycle_publications "
                "WHERE repo_id=? AND change_request_id=? "
                "UNION SELECT attempt_id FROM dispatch_attempts WHERE repo_id=?",
                (*scope, repo_id))}
            foreign = sorted({row[0] for row in connection.execute(receipts_query, scope)}
                             - before - expected)
            entries = self._stored_entries(connection, scope)
            try:
                runs = fold_history(entries)
            except ContractError:
                connection.rollback()
                raise
            self._project(connection, scope, runs)
            self._confirm_publications(connection, scope)
            latest = connection.execute(
                "SELECT comment_id, updated_at FROM cycle_history_comments "
                "WHERE repo_id=? AND change_request_id=? AND updated_at IS NOT NULL "
                "ORDER BY updated_at DESC, ordinal DESC LIMIT 1", scope).fetchone()
            if mark is None:
                connection.execute(
                    "DELETE FROM cycle_history_sync WHERE repo_id=? AND change_request_id=?",
                    scope)
            else:
                previous = connection.execute(
                    "SELECT version FROM cycle_history_sync "
                    "WHERE repo_id=? AND change_request_id=?", scope).fetchone()
                connection.execute(
                    "INSERT OR REPLACE INTO cycle_history_sync VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*scope, mark.head_sha, count,
                     latest["comment_id"] if latest else None,
                     latest["updated_at"] if latest else None,
                     fingerprint, (previous["version"] + 1) if previous else 1,
                     _now(), HISTORY_SCHEMA_VERSION))
            connection.commit()
        return foreign

    @staticmethod
    def _put(connection, scope, ordinal: int, comment: ProviderComment,
             entry: HistoryEntry | None) -> None:
        """Replace one comment's rows; processing it twice leaves the same rows."""
        comment_id = str(ordinal)
        for table in ("cycle_history_runs", "cycle_history_findings",
                      "cycle_history_receipts"):
            connection.execute(
                f"DELETE FROM {table} WHERE repo_id=? AND change_request_id=? "
                "AND comment_id=?", (*scope, comment_id))
        connection.execute(
            "INSERT OR REPLACE INTO cycle_history_comments VALUES (?, ?, ?, ?, ?, ?)",
            (*scope, comment_id, ordinal, comment.updated_at, int(entry is not None)))
        if entry is None:
            return
        for position, (run_id, kind) in enumerate(entry.runs):
            connection.execute(
                "INSERT INTO cycle_history_runs VALUES (?, ?, ?, ?, ?, ?)",
                (*scope, comment_id, position, _match(_RUN_ID, run_id, "run id"), kind))
        for position, finding in enumerate(entry.findings):
            connection.execute(
                "INSERT INTO cycle_history_findings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*scope, comment_id, position,
                 _match(_FINDING_ID, finding.id, "finding id"), finding.severity,
                 finding.status, finding.disposition, int(finding.blocks_approval)))
        repo_id, change_request_id = scope
        for attempt_id, stage, status, head_sha, repo, request in entry.receipts:
            # A receipt for another change request is not this one's publication.
            if (repo != repo_id or request != change_request_id or stage not in STAGES
                    or _ATTEMPT_ID.fullmatch(attempt_id) is None
                    or _STATUS_TOKEN.fullmatch(status) is None
                    or _SHA.fullmatch(head_sha) is None):
                continue
            connection.execute(
                "INSERT OR IGNORE INTO cycle_history_receipts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (*scope, comment_id, attempt_id, stage, status, head_sha))

    @staticmethod
    def _stored_entries(connection, scope) -> list[HistoryEntry]:
        """The trusted comments as fold input, oldest first, from stored tokens."""
        runs: dict[str, list[tuple[str, str]]] = {}
        for row in connection.execute(
                "SELECT comment_id, review_run_id, kind FROM cycle_history_runs "
                "WHERE repo_id=? AND change_request_id=? ORDER BY comment_id, position",
                scope):
            runs.setdefault(row["comment_id"], []).append((row["review_run_id"], row["kind"]))
        findings: dict[str, list[Finding]] = {}
        for row in connection.execute(
                "SELECT * FROM cycle_history_findings "
                "WHERE repo_id=? AND change_request_id=? ORDER BY comment_id, position",
                scope):
            findings.setdefault(row["comment_id"], []).append(Finding(
                id=row["finding_id"], severity=row["severity"], status=row["status"],
                blocks_approval=bool(row["blocks_approval"]),
                disposition=row["disposition"]))
        return [
            HistoryEntry(row["comment_id"], runs=tuple(runs.get(row["comment_id"], ())),
                         findings=tuple(findings.get(row["comment_id"], ())))
            for row in connection.execute(
                "SELECT comment_id FROM cycle_history_comments "
                "WHERE repo_id=? AND change_request_id=? AND trusted=1 ORDER BY ordinal",
                scope)
        ]

    @staticmethod
    def _project(connection, scope, runs: list[RunStatuses]) -> None:
        """Refresh each finding's last status, claims and survivals."""
        survivals = claimed_fix_survivals(runs)
        state: dict[str, list] = {}
        for run in runs:
            for finding_id, status in run.statuses.items():
                current = state.setdefault(finding_id, [status, 0])
                current[0] = status
                if run.kind == RESOLUTION_RUN and status in CLAIM_STATUSES:
                    current[1] += 1
        connection.execute(
            "DELETE FROM cycle_history_finding_state WHERE repo_id=? AND change_request_id=?",
            scope)
        for finding_id, (status, claims) in state.items():
            connection.execute(
                "INSERT INTO cycle_history_finding_state VALUES (?, ?, ?, ?, ?, ?)",
                (*scope, finding_id, status, claims, survivals.get(finding_id, 0)))

    def runs(self, repo_id: str, change_request_id: str) -> list[RunStatuses]:
        """The stored history folded into runs, as `comment_run_history` would."""
        with closing(self._connect()) as connection:
            return fold_history(self._stored_entries(connection, (repo_id, change_request_id)))

    def finding_state(self, repo_id: str, change_request_id: str) -> dict[str, dict]:
        with closing(self._connect()) as connection:
            return {row["finding_id"]: {"status": row["status"], "claims": row["claims"],
                                        "survivals": row["survivals"]}
                    for row in connection.execute(
                        "SELECT * FROM cycle_history_finding_state "
                        "WHERE repo_id=? AND change_request_id=?",
                        (repo_id, change_request_id))}

    # -- publication outbox ------------------------------------------------

    def expect_publication(self, attempt_id: str, repo_id: str,
                           change_request_id: str, stage: str) -> None:
        """Record that `attempt_id` should publish on the change request.

        Idempotent: recording the same attempt again changes nothing.
        """
        if stage not in STAGES:
            raise HistoryError(f"unknown stage {stage!r}")
        attempt_id = _match(_ATTEMPT_ID, attempt_id, "attempt id")
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO cycle_publications VALUES "
                "(?, ?, ?, ?, 'pending', NULL, NULL, ?, ?, ?)",
                (attempt_id, repo_id, change_request_id, stage, now, now,
                 HISTORY_SCHEMA_VERSION))
            self._confirm_publications(connection, (repo_id, change_request_id))
            connection.commit()

    @staticmethod
    def _confirm_publications(connection, scope) -> None:
        """Mark pending attempts whose receipt a stored comment carries."""
        now = _now()
        for row in connection.execute(
                "SELECT p.attempt_id, p.stage, MIN(CAST(r.comment_id AS INTEGER)) AS comment_id "
                "FROM cycle_publications p JOIN cycle_history_receipts r "
                "ON r.attempt_id=p.attempt_id AND r.repo_id=p.repo_id "
                "AND r.change_request_id=p.change_request_id AND r.stage=p.stage "
                "WHERE p.repo_id=? AND p.change_request_id=? AND p.state != 'published' "
                "GROUP BY p.attempt_id, p.stage", scope).fetchall():
            comment_id = str(row["comment_id"])
            head = connection.execute(
                "SELECT head_sha FROM cycle_history_receipts WHERE repo_id=? AND "
                "change_request_id=? AND comment_id=? AND attempt_id=?",
                (*scope, comment_id, row["attempt_id"])).fetchone()["head_sha"]
            connection.execute(
                "UPDATE cycle_publications SET state='published', comment_id=?, "
                "head_sha=?, updated_at=? WHERE attempt_id=?",
                (comment_id, head, now, row["attempt_id"]))
            # A later confirmed publication of the same stage supersedes an
            # earlier attempt that never appeared; the record is complete again.
            connection.execute(
                "UPDATE cycle_publications SET state='superseded', updated_at=? "
                "WHERE repo_id=? AND change_request_id=? AND stage=? AND state='pending' "
                "AND created_at <= (SELECT created_at FROM cycle_publications "
                "WHERE attempt_id=?) AND attempt_id != ?",
                (now, *scope, row["stage"], row["attempt_id"], row["attempt_id"]))

    def record_attempt_start(self, attempt_id: str, repo_id: str, change_request_id: str,
                             stage: str, head_sha: str) -> None:
        """Bind the runtime-observed stage start head to its dispatch attempt."""
        attempt_id = _match(_ATTEMPT_ID, attempt_id, "attempt ID")
        repo_id = _match(_REFERENCE, repo_id, "repository")
        change_request_id = _match(_REFERENCE, change_request_id, "change request")
        if stage not in STAGES:
            raise HistoryError("invalid stage for attempt start head")
        if _SHA.fullmatch(head_sha) is None:
            raise HistoryError("attempt start head must be a full commit SHA")
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS cycle_history_attempt_heads ("
                "attempt_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, "
                "change_request_id TEXT NOT NULL, stage TEXT NOT NULL, "
                "head_sha TEXT NOT NULL, created_at TEXT NOT NULL)")
            connection.execute(
                "INSERT OR REPLACE INTO cycle_history_attempt_heads VALUES (?, ?, ?, ?, ?, ?)",
                (attempt_id, repo_id, change_request_id, stage, head_sha, _now()))
            connection.commit()

    def attempt_start_head(self, attempt_id: str, repo_id: str,
                           change_request_id: str) -> str | None:
        """Return a start head only for the exact repository and request scope."""
        attempt_id = _match(_ATTEMPT_ID, attempt_id, "attempt ID")
        repo_id = _match(_REFERENCE, repo_id, "repository")
        change_request_id = _match(_REFERENCE, change_request_id, "change request")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT head_sha FROM cycle_history_attempt_heads WHERE attempt_id=? "
                "AND repo_id=? AND change_request_id=? AND stage='resolve'",
                (attempt_id, repo_id, change_request_id)).fetchone()
        return row["head_sha"] if row is not None else None

    def publication(self, attempt_id: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM cycle_publications WHERE attempt_id=?",
                (attempt_id,)).fetchone()
        return dict(row) if row is not None else None

    def record_unpublished_head(self, attempt_id: str, repo_id: str,
                                change_request_id: str, workspace: str, branch: str,
                                start_sha: str, end_sha: str) -> None:
        """Runtime-only attribution, separate from agent-authored success receipts."""
        attempt_id = _match(_ATTEMPT_ID, attempt_id, "attempt ID")
        repo_id = _match(_REFERENCE, repo_id, "repository")
        change_request_id = _match(_REFERENCE, change_request_id, "change request")
        branch = _match(_REFERENCE, branch, "branch")
        if not _SHA.fullmatch(start_sha) or not _SHA.fullmatch(end_sha):
            raise HistoryError("stage heads must be full commit SHAs")
        scope = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS cycle_unpublished_heads ("
                "attempt_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, "
                "change_request_id TEXT NOT NULL, workspace TEXT NOT NULL, "
                "branch TEXT NOT NULL, start_sha TEXT NOT NULL, end_sha TEXT NOT NULL)")
            connection.execute(
                "INSERT OR IGNORE INTO cycle_unpublished_heads VALUES (?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, repo_id, change_request_id, scope, branch, start_sha, end_sha))
            connection.commit()

    def unpublished_heads(self, repo_id: str, change_request_id: str, workspace: str,
                          branch: str, remote_head: str) -> tuple[str, str] | None:
        """Latest runtime-recorded head; the caller proves PR-to-local ancestry.

        The PR may already contain an intermediate commit uploaded during the
        attempt, so its head need not equal the stage's starting head.
        """
        scope = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT end_sha FROM cycle_unpublished_heads "
                    "WHERE repo_id=? AND change_request_id=? AND workspace=? AND branch=? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (repo_id, change_request_id, scope, branch)).fetchone()
        except sqlite3.OperationalError:
            return None  # Older/missing evidence never grants a resume exception.
        # The caller still checks HEAD equality, cleanliness and ancestry.
        return (remote_head, row["end_sha"]) if row is not None and row["end_sha"] != remote_head else None

    def pending_publications(self, repo_id: str, change_request_id: str,
                             stage: str | None = None) -> list[dict]:
        """Attempts whose publication was never observed, oldest first."""
        query = ("SELECT * FROM cycle_publications WHERE repo_id=? AND "
                 "change_request_id=? AND state='pending'")
        arguments: tuple = (repo_id, change_request_id)
        if stage is not None:
            query += " AND stage=?"
            arguments += (stage,)
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                query + " ORDER BY created_at, attempt_id", arguments)]
