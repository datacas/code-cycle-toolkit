"""Quota reset interpretation and durable, scoped worktree handoff evidence.

No provider prose or file contents are stored here. The journal owns redacted
messages. Availability and provenance deliberately have separate lifecycles.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

QUOTA_SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_history (
 id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, executor TEXT NOT NULL,
 model TEXT NOT NULL, exhausted_at TEXT NOT NULL, expires_at TEXT,
 reason TEXT NOT NULL, retry_at TEXT
);
CREATE TABLE IF NOT EXISTS quota_handoffs (
 id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, task_id TEXT NOT NULL,
 stage TEXT NOT NULL, pr TEXT NOT NULL, head TEXT NOT NULL,
 original_executor TEXT NOT NULL, original_model TEXT NOT NULL,
 destination_executor TEXT NOT NULL, fingerprint TEXT NOT NULL,
 provenance TEXT NOT NULL, operator TEXT, created_at TEXT NOT NULL,
 claimed_at TEXT, used_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS quota_operator_attestation_once
 ON quota_handoffs(repo_id,task_id,stage,pr) WHERE provenance='operator_attested';
"""


def local_zone():
    """Use the runtime's local zone, retaining DST rules for a future date."""
    name = os.environ.get("TZ", "").removeprefix(":")
    try:
        if name:
            return ZoneInfo(name)
        with Path("/etc/localtime").open("rb") as handle:
            return ZoneInfo.from_file(handle)
    except (OSError, ValueError, KeyError):
        # Windows normally has neither TZ nor /etc/localtime. Keep local wall
        # times naive there so astimezone() applies OS rules for their date.
        return None


def quota_reset(detail: str | None, now: datetime | None = None):
    """UTC reset, reported flag, closed uncertainty token.

    An explicit invalid date/zone is never reduced to just its time of day.
    Missing reset information alone gets the minimum 30 minute window.
    """
    current = now or datetime.now(timezone.utc)
    text = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", detail or "", flags=re.I)
    clock = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?(?!\d)", text, re.I)
    if clock is None:
        if re.search(r"\b(reset|resets|resetting|until)\b|\b(?:in|after)\s+\d|"
                     r"\b(?:try|retry)\s+again\s+(?:in|after)\b", text, re.I):
            return None, True, "reset_time_ambiguous"
        return (current + timedelta(minutes=30)).isoformat(), False, "quota"
    try:
        hour, minute = int(clock[1]), int(clock[2])
        second = int(clock[3] or "0")
        if clock[4]:
            if not 1 <= hour <= 12:
                raise ValueError("invalid 12-hour clock")
            hour = hour % 12 + (12 if clock[4].upper() == "PM" else 0)
        zone = local_zone()
        suffix = text[clock.end():].strip(" .,")
        if suffix.startswith("(") and suffix.endswith(")"):
            suffix = suffix[1:-1].strip()
            if not suffix:
                raise ValueError("empty timezone")
        zone_match = re.fullmatch(r"(?:UTC|GMT)([+-]\d{1,2}(?::?\d{2})?)?|([+-]\d{2}:?\d{2})|(Z)", suffix, re.I)
        if zone_match:
            offset = zone_match[1] or zone_match[2]
            minutes = 0
            if offset:
                digits = offset[1:].replace(":", "")
                hours = int(digits) if len(digits) <= 2 else int(digits[:-2])
                mins = 0 if len(digits) <= 2 else int(digits[-2:])
                if hours > 14 or mins > 59 or (hours == 14 and mins):
                    raise ValueError("invalid zone")
                minutes = (hours * 60 + mins) * (-1 if offset[0] == "-" else 1)
            zone = timezone(timedelta(minutes=minutes))
        elif suffix:
            # Only an actual IANA zone is unambiguous; CST, IST, etc. aren't.
            if not re.fullmatch(r"[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)+", suffix):
                raise ValueError("ambiguous timezone")
            zone = ZoneInfo(suffix)
        # With no IANA zone, preserve naive wall time until final conversion;
        # today's fixed UTC offset is not valid for every future date.
        local = current.astimezone(zone).replace(tzinfo=zone)
        prefix = text[:clock.start()].strip()
        date_match = re.search(r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})\b", prefix)
        iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})[T ]?$", prefix)
        if date_match:
            date_text = date_match[0].replace(",", "")
            try:
                date = datetime.strptime(date_text, "%b %d %Y")
            except ValueError:
                date = datetime.strptime(date_text, "%B %d %Y")
            candidate = date.replace(hour=hour, minute=minute, second=second, tzinfo=zone)
        elif iso_match:
            candidate = datetime.fromisoformat(iso_match[1]).replace(
                hour=hour, minute=minute, second=second, tzinfo=zone)
        else:
            if re.search(r"\d|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
                         r"Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\b|\b(?:today|tomorrow|next)\b", prefix, re.I):
                raise ValueError("unresolved date")
            candidate = local.replace(hour=hour, minute=minute, second=second, microsecond=0)
        # During a repeated local hour choose the later defensible instant.
        reset = max(candidate.replace(fold=fold).astimezone(timezone.utc)
                    for fold in (0, 1))
        if not date_match and not iso_match and reset <= current:
            candidate += timedelta(days=1)
            reset = max(candidate.replace(fold=fold).astimezone(timezone.utc)
                        for fold in (0, 1))
        return reset.isoformat(), True, "quota"
    except (ValueError, OverflowError, KeyError, OSError):
        return None, True, "reset_time_ambiguous"


class Handoffs:
    def __init__(self, path):
        self.path = path

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, *, repo, task, stage, pr, workspace, original, destination,
               provenance="executor_verified", operator=None):
        if not pr or not workspace or not re.fullmatch(r"[0-9a-f]{40,64}", workspace.get("head", "")):
            raise ValueError("handoff needs a PR and full HEAD")
        if not re.fullmatch(r"[0-9a-f]{64}", workspace.get("fingerprint", "")):
            raise ValueError("handoff needs a complete diff fingerprint")
        record_id = f"handoff-{uuid.uuid4().hex}"
        with closing(self.connect()) as connection, connection:
            if provenance == "operator_attested" and connection.execute(
                    "SELECT 1 FROM quota_handoffs WHERE repo_id=? AND task_id=? AND stage=? AND pr=?",
                    (repo, task, stage, str(pr))).fetchone():
                raise ValueError("legacy attestation already recorded for this scope")
            connection.execute(
                "INSERT INTO quota_handoffs (id,repo_id,task_id,stage,pr,head,"
                "original_executor,original_model,destination_executor,fingerprint,"
                "provenance,operator,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record_id, repo, task, stage, str(pr), workspace["head"],
                 original.executor, original.model, destination.executor,
                 workspace["fingerprint"], provenance, operator,
                 datetime.now(timezone.utc).isoformat()))
        return record_id

    def match(self, *, repo, task, stage, pr, workspace, destination):
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM quota_handoffs WHERE repo_id=? AND task_id=? AND stage=? "
                "AND pr=? AND used_at IS NULL ORDER BY rowid DESC",
                (repo, task, stage, str(pr))).fetchall()
        if not rows:
            raise ValueError("dirty resume has no quota worktree provenance")
        row = dict(rows[0])
        if row["claimed_at"] and row["provenance"] == "operator_attested":
            raise ValueError("operator attestation already used")
        if workspace is None or row["head"] != workspace["head"]:
            raise ValueError("quota handoff HEAD mismatch")
        if row["fingerprint"] != workspace["fingerprint"]:
            raise ValueError("quota handoff diff fingerprint mismatch")
        if destination.executor not in {row["original_executor"], row["destination_executor"]}:
            raise ValueError("quota handoff destination executor mismatch")
        if destination.executor == row["original_executor"] and destination.model != row["original_model"]:
            raise ValueError("quota handoff original model mismatch")
        return row

    def claim(self, row):
        if row["provenance"] != "operator_attested":
            return
        with closing(self.connect()) as connection, connection:
            updated = connection.execute(
                "UPDATE quota_handoffs SET claimed_at=? WHERE id=? AND claimed_at IS NULL AND used_at IS NULL",
                (datetime.now(timezone.utc).isoformat(), row["id"]))
            if updated.rowcount != 1:
                raise ValueError("operator attestation already used")

    def complete(self, row):
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "UPDATE quota_handoffs SET used_at=? WHERE repo_id=? AND task_id=? AND stage=? AND pr=? AND used_at IS NULL",
                (datetime.now(timezone.utc).isoformat(), row["repo_id"], row["task_id"], row["stage"], row["pr"]))


def main(argv=None):
    from telemetry import Telemetry
    from executors import _workspace_fingerprint
    from router import Target
    from cycle_journal import record_event, _identifier, _change_request_identifier

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list quota record IDs and reset uncertainty")
    retry = commands.add_parser("retry", help="authorize one target to be dispatched again")
    retry.add_argument("--record", required=True)
    retry.add_argument("--executor", required=True, choices=("codex", "claude", "orca"))
    for name in ("inspect", "attest"):
        command = commands.add_parser(name)
        command.add_argument("--cwd", required=True)
        command.add_argument("--pr", required=True)
        command.add_argument("--stage", required=True, choices=("review", "resolve", "rereview"))
        command.add_argument("--original", required=True, choices=("codex", "claude", "orca"))
        command.add_argument("--model", required=True)
        command.add_argument("--destination", required=True, choices=("codex", "claude", "orca"))
        command.add_argument("--head", required=True)
        if name == "attest":
            command.add_argument("--confirm-fingerprint", required=True)
    args = parser.parse_args(argv)
    # Validate operator supplied references before writing durable state.
    try:
        _identifier(args.repo)
        _identifier(args.task)
        if args.command not in {"retry", "list"}:
            args.pr = _change_request_identifier(args.pr, args.repo)
    except ValueError as error:
        parser.exit(2, f"quota recovery refused: {error}\n")
    store = Telemetry(args.database)
    ledger = Handoffs(store.path)
    try:
        if args.command == "list":
            with closing(ledger.connect()) as connection:
                rows = connection.execute(
                    "SELECT * FROM quota_history WHERE repo_id=? ORDER BY rowid DESC", (args.repo,)).fetchall()
            print(json.dumps([dict(row) for row in rows]))
            return 0
        workspace = None
        if args.command != "retry":
            store._checked("model_requested", args.model)
            workspace = _workspace_fingerprint(args.cwd)
            if workspace is None:
                raise ValueError("complete worktree fingerprint unavailable")
            if workspace["head"] != args.head:
                raise ValueError("attestation HEAD mismatch")
            if args.command == "inspect":
                print(json.dumps({**vars(args), **workspace}))
                return 0
            if workspace["fingerprint"] != args.confirm_fingerprint:
                raise ValueError("explicit fingerprint confirmation does not match")
        identity = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", identity):
            raise ValueError("operator identity unavailable")
        now = datetime.now(timezone.utc).isoformat()
        if args.command == "retry":
            with closing(ledger.connect()) as connection, connection:
                row = connection.execute(
                    "SELECT * FROM quota_history WHERE id=? AND repo_id=? AND executor=? AND retry_at IS NULL",
                    (args.record, args.repo, args.executor)).fetchone()
                if row is None:
                    raise ValueError("quota record not found or retry already authorized")
                updated = connection.execute(
                    "UPDATE quota_exhaustions SET expires_at=?, reason='retry_authorized' "
                    "WHERE repo_id=? AND executor=? AND model=? AND exhausted_at=?",
                    (now, args.repo, args.executor, row["model"], row["exhausted_at"]))
                if updated.rowcount != 1:
                    raise ValueError("quota record was superseded")
                connection.execute("UPDATE quota_history SET retry_at=? WHERE id=?", (now, args.record))
            record_id = args.record
        else:
            # Legacy attestation still requires historical quota evidence. It
            # grants ownership only; it cannot assert provider availability.
            with closing(ledger.connect()) as connection:
                found = connection.execute(
                    "SELECT q.* FROM quota_exhaustions q WHERE repo_id=? AND executor=? AND model=? "
                    "AND NOT EXISTS (SELECT 1 FROM quota_history h WHERE h.repo_id=q.repo_id "
                    "AND h.executor=q.executor AND h.model=q.model AND h.exhausted_at=q.exhausted_at)",
                    (args.repo, args.original, args.model)).fetchone()
            if not found:
                raise ValueError("attestation requires a legacy quota record without automatic provenance history")
            record_id = ledger.record(
                repo=args.repo, task=args.task, stage=args.stage, pr=args.pr,
                workspace=workspace, original=Target(args.original, "operator", args.model, "high"),
                destination=Target(args.destination, "operator", args.model, "high"),
                provenance="operator_attested", operator=identity)
        record_event(store.path, repo_id=args.repo, task_id=args.task,
                     cycle_id=f"operator-{uuid.uuid4().hex}", kind="stage_result",
                     component="quota", status=args.command, detail=f"operator {identity}",
                     correlation={"record_id": record_id, "fingerprint": workspace["fingerprint"] if workspace else None})
        print(json.dumps({"record_id": record_id, "operator": identity, "action": args.command, "at": now}))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, sqlite3.Error) as error:
        parser.exit(2, f"quota recovery refused: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
