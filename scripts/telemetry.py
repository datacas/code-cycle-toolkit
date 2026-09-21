"""What the cycle did, recorded so a later question can be answered from data.

This exists to replace one guess in particular. `router.DEFAULT_FIRST_PASS_RATE`
is `0.0` because five real implementations out of five needed a correction
round, and the cost model has carried that number ever since. Five is not a
measurement; it is the smallest sample that produced a unanimous answer. Every
routing cost estimate is built on it until a repository measures its own.

Two design decisions are worth stating, because both are arguable.

**The schema will change.** A schema designed before its questions are known is
a schema that gets migrated, and that risk was accepted deliberately: recording
now, with adapters that were just proven correct, beats recording later from a
larger but unexamined pile. So only the fields already known to be queried get
columns, a short allowlist of counts and flags travels in `payload`, and
`schema_version` is on every row from the first one.

**A rate from too few rows is not reported.** `first_pass_rate()` returns the
sample size alongside the value and returns `None` for the value when the sample
is too small to mean anything. An estimate that silently hardens from three
observations into a routing decision is worse than the honest constant it would
replace.

The database lives outside every repository and never enters Git. It holds
identifiers, statuses and counts — no diffs, no prose, no credentials — and that
boundary is enforced by an allowlist rather than asserted in this paragraph. A
field nobody has considered is refused by name, loudly, so adding one is a
decision somebody made rather than a transcript that arrived by accident.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
APP_DIRNAME = "code-cycle-toolkit"
DATABASE_NAME = "telemetry.sqlite"

#: Below this many observations a rate is reported as unknown. Not a
#: significance test — just a refusal to let three runs set a routing constant.
MINIMUM_SAMPLE = 10

#: Fields allowed into `payload`, beyond the promoted columns.
#:
#: An allowlist rather than an open dictionary, because the store promises to
#: hold no prose and no credentials and a promise the code does not enforce is
#: not a promise. An unknown key is refused loudly rather than stored or quietly
#: dropped: a caller that wanted to record something learns it must be added
#: here on purpose, which is the moment to ask whether it is safe to keep.
ALLOWED_PAYLOAD_KEYS = frozenset({
    "routing_reason_count",
    "iterations",
    "findings_critical",
    "findings_high",
    "findings_medium",
    "findings_low",
    "security_audit_ran",
    "security_gate_half",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "checks_passed",
    "checks_failed",
    "exit_code",
})

#: Values are counts, flags, short identifiers and enum-like tokens. Anything
#: longer is prose by another name.
MAX_PAYLOAD_VALUE_LENGTH = 120

#: Review statuses that settle whether the first pass succeeded. A row with any
#: other status, or none, has not finished saying what happened, and counting it
#: as a failure would invent an outcome.
TERMINAL_REVIEW_STATUSES = frozenset({"APPROVED", "CHANGES_REQUESTED"})
PASSING_REVIEW_STATUSES = frozenset({"APPROVED"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS stages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version     INTEGER NOT NULL,
    recorded_at        TEXT    NOT NULL,
    repo_id            TEXT    NOT NULL,
    task_id            TEXT    NOT NULL,
    role               TEXT    NOT NULL,
    skill              TEXT,
    iteration          INTEGER NOT NULL DEFAULT 0,
    profile            TEXT,
    executor           TEXT,
    provider           TEXT,
    model_requested    TEXT,
    model_resolved     TEXT,
    effort             TEXT,
    readiness_policy   TEXT,
    dispatched_from    TEXT,
    used_fallback      INTEGER NOT NULL DEFAULT 0,
    outcome            TEXT,
    status             TEXT,
    missing_capability TEXT,
    difficulty         INTEGER,
    verifiability      TEXT,
    security_sensitive INTEGER,
    findings_total     INTEGER,
    findings_blocking  INTEGER,
    duration_ms        INTEGER,
    payload            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS stages_repo_task ON stages (repo_id, task_id);
CREATE INDEX IF NOT EXISTS stages_profile   ON stages (repo_id, profile, role);
"""


class TelemetryError(ValueError):
    """The store was asked for something it must not answer."""


def default_database_path() -> Path:
    """Outside every repository, beside the other host-local state."""
    override = os.environ.get("CODE_CYCLE_HOME")
    if override:
        base = Path(override)
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) / APP_DIRNAME if appdata else Path.home() / APP_DIRNAME
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = (Path(xdg) if xdg else Path.home() / ".config") / APP_DIRNAME
    return base / DATABASE_NAME


@dataclass(frozen=True)
class Rate:
    """A measured rate, or an honest refusal to give one.

    `value` is `None` when `observations` is below the minimum. Callers get the
    count either way, so "we do not know yet" and "it is genuinely zero" never
    look the same.
    """

    value: float | None
    observations: int
    minimum: int = MINIMUM_SAMPLE

    @property
    def known(self) -> bool:
        return self.value is not None

    def explain(self) -> str:
        if self.known:
            return f"{self.value:.2f} from {self.observations} observations"
        return (f"unknown: {self.observations} observation(s), "
                f"fewer than the {self.minimum} required")


#: Columns promoted out of `payload` because they are already queried.
_COLUMNS = (
    "repo_id", "task_id", "role", "skill", "iteration", "profile", "executor",
    "provider", "model_requested", "model_resolved", "effort", "readiness_policy",
    "dispatched_from", "used_fallback", "outcome", "status", "missing_capability",
    "difficulty", "verifiability", "security_sensitive", "findings_total",
    "findings_blocking", "duration_ms",
)


class Telemetry:
    """Append-only record of what each stage did."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def record_stage(self, repo_id: str, task_id: str, role: str, **fields) -> int:
        """Append one stage execution.

        A keyword that is neither a column nor an allowed payload key is
        refused by name. Refusing loudly is the point: storing it would break
        the no-prose promise, and dropping it silently would lose an
        observation the caller believed it had recorded.
        """
        if not repo_id or not task_id or not role:
            raise TelemetryError("a stage needs repo_id, task_id and role")

        row = {"repo_id": repo_id, "task_id": task_id, "role": role}
        payload = {}
        for key, value in fields.items():
            if key in _COLUMNS:
                row[key] = value
                continue
            if key not in ALLOWED_PAYLOAD_KEYS:
                raise TelemetryError(
                    f"{key!r} is not an allowed telemetry field. This store holds "
                    "counts, flags and identifiers, never prose or credentials; add "
                    "the field to ALLOWED_PAYLOAD_KEYS deliberately if it belongs."
                )
            if isinstance(value, str) and len(value) > MAX_PAYLOAD_VALUE_LENGTH:
                raise TelemetryError(
                    f"{key!r} is {len(value)} characters, over the "
                    f"{MAX_PAYLOAD_VALUE_LENGTH} allowed; telemetry stores tokens, "
                    "not text"
                )
            payload[key] = value
        for flag in ("used_fallback", "security_sensitive"):
            if flag in row and row[flag] is not None:
                row[flag] = int(bool(row[flag]))
        row.setdefault("iteration", 0)
        row.setdefault("used_fallback", 0)

        columns = ["schema_version", "recorded_at", *row, "payload"]
        values = [SCHEMA_VERSION, datetime.now(timezone.utc).isoformat(),
                  *row.values(), json.dumps(payload, sort_keys=True, default=str)]
        placeholders = ", ".join("?" * len(columns))
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                f"INSERT INTO stages ({', '.join(columns)}) VALUES ({placeholders})", values
            )
            connection.commit()
            return int(cursor.lastrowid)

    def record_dispatch(self, repo_id: str, task_id: str, role: str, decision, result,
                        **extra) -> int:
        """Record a routing decision and what dispatching it did.

        Takes the objects rather than a hand-copied dict, so the row cannot
        disagree with what actually happened.
        """
        target = decision.target
        return self.record_stage(
            repo_id, task_id, role,
            profile=decision.profile,
            used_fallback=decision.used_fallback,
            executor=getattr(target, "executor", None),
            provider=getattr(target, "provider", None),
            model_requested=getattr(target, "model", None),
            effort=getattr(target, "effort", None),
            model_resolved=getattr(result, "model_resolved", None),
            outcome=getattr(getattr(result, "outcome", None), "value", None),
            missing_capability=getattr(result, "missing_capability", None),
            readiness_policy=getattr(getattr(result, "readiness_policy", None), "value", None),
            dispatched_from=getattr(getattr(result, "dispatched_from", None), "value", None),
            # The reasons themselves are prose and belong in the published
            # comment, not in a store that promises to hold none. How many there
            # were is still useful for spotting a decision that needed
            # explaining.
            routing_reason_count=len(getattr(decision, "reasons", ()) or ()),
            **extra,
        )

    def rows(self, repo_id: str | None = None, task_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM stages"
        clauses, params = [], []
        if repo_id:
            clauses.append("repo_id = ?")
            params.append(repo_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        with closing(self._connect()) as connection:
            out = []
            for row in connection.execute(query, params):
                record = dict(row)
                record["payload"] = json.loads(record["payload"] or "{}")
                out.append(record)
            return out

    def first_pass_rate(
        self, repo_id: str, profile: str | None = None, *, minimum: int = MINIMUM_SAMPLE
    ) -> Rate:
        """How often an implementation passed its first review, per repository.

        This is the number `router.estimate_cost` needs and currently guesses.
        A task counts once: its first review that reached a verdict, whatever
        happened afterwards. A task with no review recorded is not counted at
        all — an implementation nobody reviewed is not evidence that it would
        have passed — and neither is one whose review has not said yet.

        The profile filter names the *implementer* whose work was reviewed, not
        the reviewer, because the question is which implementer is good enough.
        """
        implementers = {}
        for row in self.rows(repo_id):
            if row["role"] == "implement" and row["task_id"] not in implementers:
                implementers[row["task_id"]] = row["profile"]

        passed = total = 0
        seen_reviews = set()
        for row in self.rows(repo_id):
            if row["role"] != "review" or row["task_id"] in seen_reviews:
                continue
            if row["task_id"] not in implementers:
                continue
            if profile is not None and implementers[row["task_id"]] != profile:
                continue
            status = (row["status"] or "").upper()
            if status not in TERMINAL_REVIEW_STATUSES:
                # Not yet an outcome. The task stays unconsumed so a later
                # terminal review still counts: a row recorded before its
                # verdict arrived — which an append-only API makes ordinary —
                # must not be read as a failed first pass.
                continue
            seen_reviews.add(row["task_id"])
            total += 1
            if status in PASSING_REVIEW_STATUSES:
                passed += 1

        if total < minimum:
            return Rate(None, total, minimum)
        return Rate(passed / total, total, minimum)

    def dispatch_failures(self, repo_id: str | None = None) -> dict[str, int]:
        """Which capability blocked dispatches, and how often.

        A campaign was lost to an exhausted window read as evidence about a
        model. Counting the capability by name keeps that distinction visible.
        """
        counts: dict[str, int] = {}
        for row in self.rows(repo_id):
            capability = row["missing_capability"]
            if capability:
                counts[capability] = counts.get(capability, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def model_drift(self, repo_id: str | None = None) -> list[dict]:
        """Rows where the executor ran a model other than the one requested.

        Rows with no reported model are excluded rather than counted as
        matching: Codex reports none at all, and treating that silence as
        agreement would hide exactly what this is for.
        """
        return [
            {"task_id": row["task_id"], "role": row["role"],
             "requested": row["model_requested"], "resolved": row["model_resolved"]}
            for row in self.rows(repo_id)
            if row["model_resolved"] and row["model_requested"]
            and row["model_resolved"] != row["model_requested"]
        ]

    def summary(self, repo_id: str) -> dict:
        rows = self.rows(repo_id)
        return {
            "repo_id": repo_id,
            "stages": len(rows),
            "tasks": len({row["task_id"] for row in rows}),
            "first_pass_rate": self.first_pass_rate(repo_id).explain(),
            "dispatch_failures": self.dispatch_failures(repo_id),
            "model_drift": len(self.model_drift(repo_id)),
        }
