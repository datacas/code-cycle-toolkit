#!/usr/bin/env python3
"""Aggregate local Code Cycle telemetry into a privacy-safe report."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from run_cycle import CycleDriverError, load_config, repository_of
from telemetry import MINIMUM_SAMPLE, default_database_path


CONFIDENCE_BUCKETS = (
    ("low", 0.0, 0.5),
    ("medium", 0.5, 0.75),
    ("high", 0.75, 1.0000001),
)
FINDING_FIELDS = ("findings_critical", "findings_high", "findings_medium", "findings_low")
PASSING_REVIEW_STATUSES = frozenset({"APPROVED"})
#: Columns only a dispatch writes. Before schema 3 no row carried a
#: `record_kind`, and verdicts and the closing row were already separate rows,
#: so these are what tell a historical dispatch from its verdict or close.
DISPATCH_EVIDENCE = ("outcome", "executor", "profile")
TERMINAL_REVIEW_STATUSES = frozenset({"APPROVED", "CHANGES_REQUESTED"})


class StatsError(ValueError):
    """The requested report cannot be produced from this local store."""


def _read_rows(database: Path, repo_id: str) -> list[dict]:
    """Read only the configured repository's aggregate source rows."""
    if not database.is_file():
        return []
    uri = database.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            result = []
            for raw in connection.execute(
                "SELECT repo_id, task_id, role, profile, executor, outcome, status, missing_capability, "
                "used_fallback, duration_ms, model_requested, model_resolved, recorded_at, payload "
                "FROM stages WHERE repo_id = ? ORDER BY recorded_at, id",
                (repo_id,),
            ):
                row = dict(raw)
                try:
                    row["payload"] = json.loads(row.get("payload") or "{}")
                except (TypeError, json.JSONDecodeError):
                    row["payload"] = {}
                result.append(row)
            return result
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise StatsError("could not read the local telemetry database") from error


def _timestamp(row: dict) -> datetime | None:
    try:
        value = datetime.fromisoformat(row["recorded_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _rate(passed: int, total: int, minimum: int = MINIMUM_SAMPLE) -> dict:
    return {
        "passed": passed,
        "total": total,
        "minimum": minimum,
        "value": passed / total if total >= minimum and total else None,
    }


def _first_pass(rows: list[dict], start: datetime | None = None,
                end: datetime | None = None) -> dict:
    by_task: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["payload"].get("record_kind") != "shadow":
            by_task[row["task_id"]].append(row)
    passed = total = 0
    for task_rows in by_task.values():
        if not any(row["role"] == "implement" for row in task_rows):
            continue
        first_review = next(
            (row for row in task_rows
             if row["role"] == "review"
             and str(row.get("status") or "").upper() in TERMINAL_REVIEW_STATUSES),
            None,
        )
        if first_review is None:
            continue
        stamp = _timestamp(first_review)
        if stamp is None or (start is not None and stamp < start) or (end is not None and stamp >= end):
            continue
        total += 1
        passed += str(first_review.get("status") or "").upper() in PASSING_REVIEW_STATUSES
    return _rate(passed, total)


def _counter(counter: Counter) -> dict:
    return dict(sorted(counter.items(), key=lambda item: (-item[1], str(item[0]))))


def _period_rows(rows: list[dict], start: datetime | None, end: datetime) -> list[dict]:
    selected = []
    for row in rows:
        stamp = _timestamp(row)
        if stamp is None or stamp >= end or (start is not None and stamp < start):
            continue
        selected.append(row)
    return selected


def _is_dispatch(row: dict) -> bool:
    """Whether a row records a dispatch attempt, in any schema version."""
    kind = row["payload"].get("record_kind")
    if kind:
        return kind == "dispatch"
    return any(row.get(column) for column in DISPATCH_EVIDENCE)


def _stage_rows(rows: list[dict]) -> list[dict]:
    """Return one row per stage, retaining compatibility with older schemas."""
    stages = [row for row in rows
              if not row["payload"].get("record_kind") and _is_dispatch(row)]
    dispatches: dict[tuple, dict] = {}
    for index, row in enumerate(rows):
        payload = row["payload"]
        if payload.get("record_kind") != "dispatch":
            continue
        cycle_id, stage_seq = payload.get("cycle_id"), payload.get("stage_seq")
        key = ("stage", cycle_id, stage_seq) if cycle_id and isinstance(stage_seq, int) else ("row", index)
        # Rows are read in recorded order; keep the final attempt's profile and
        # timestamp as the representative values for this stage.
        dispatches[key] = row
    stages.extend(dispatches.values())
    return stages


def _fallback_stage_count(rows: list[dict], stages: list[dict]) -> int:
    """Prefer closing cycle totals; otherwise count fallback stages once each."""
    cycle_fallbacks = {}
    for row in rows:
        payload = row["payload"]
        cycle_id, value = payload.get("cycle_id"), payload.get("fallback_stages")
        if (payload.get("record_kind") == "cycle" and cycle_id
                and isinstance(value, int) and not isinstance(value, bool)):
            cycle_fallbacks[cycle_id] = value

    total = sum(cycle_fallbacks.values())
    for row in stages:
        cycle_id = row["payload"].get("cycle_id")
        if cycle_id in cycle_fallbacks:
            continue
        total += bool(row.get("used_fallback"))
    return total


def _profile_outcomes(rows: list[dict], minimum: int) -> dict:
    """Join implement-stage suggestions to that cycle's observed first review."""
    shadow_by_cycle: dict[tuple[str, int], dict] = {}
    outcome_by_cycle: dict[str, bool] = {}
    for row in rows:
        payload = row["payload"]
        cycle_id = payload.get("cycle_id")
        if not cycle_id:
            continue
        kind = payload.get("record_kind")
        if kind == "shadow" and row["role"] == "implement":
            seq = payload.get("stage_seq")
            if isinstance(seq, int):
                shadow_by_cycle[(cycle_id, seq)] = payload
        elif kind == "cycle":
            approved = payload.get("first_pass_approved")
            if isinstance(approved, bool):
                outcome_by_cycle[cycle_id] = approved

    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for (cycle_id, _), shadow in shadow_by_cycle.items():
        suggested = shadow.get("jev_suggested_profile")
        if suggested not in {"cheap_coder", "deep_coder"} or cycle_id not in outcome_by_cycle:
            continue
        totals[suggested][0] += int(outcome_by_cycle[cycle_id])
        totals[suggested][1] += 1
    return {
        profile: _rate(values[0], values[1], minimum)
        for profile, values in sorted(totals.items())
    }


def aggregate(rows: list[dict], *, repo_id: str, days: int | None = 30,
              now: datetime | None = None, minimum: int = MINIMUM_SAMPLE) -> dict:
    """Build a JSON-safe report without returning raw telemetry rows."""
    now = now or datetime.now(timezone.utc)
    rows = [row for row in rows if row.get("repo_id") == repo_id]
    start = now - timedelta(days=days) if days is not None else None
    current = _period_rows(rows, start, now)
    previous = (
        _period_rows(rows, start - timedelta(days=days), start)
        if start is not None else []
    )

    stages = _stage_rows(current)
    stage_row_ids = {id(row) for row in stages}
    stage_event_row_ids = {id(row) for row in current if _is_dispatch(row)}
    roles = Counter(row["role"] for row in stages)
    profiles: dict[str, Counter] = defaultdict(Counter)
    verdicts, blockages = Counter(), Counter()
    findings, findings_measured = Counter(), Counter()
    fallbacks = _fallback_stage_count(current, stages)
    model_resolutions = Counter()
    verification_by_cycle: dict[str, bool] = {}
    confidence = Counter()
    jev_agreement = Counter()
    jev_status = Counter()
    jev_shadow_rows = 0
    daily = Counter()
    durations, costs = [], []
    for row in current:
        role, profile = row.get("role"), row.get("profile")
        if profile and id(row) in stage_row_ids:
            profiles[role][profile] += 1
        if role in {"review", "rereview"} and row.get("status"):
            verdicts[str(row["status"]).upper()] += 1
        if row.get("missing_capability"):
            blockages[row["missing_capability"]] += 1
        payload = row["payload"]
        tests_passed = payload.get("tests_passed")
        cycle_key = payload.get("cycle_id") or row["task_id"]
        if isinstance(tests_passed, bool):
            verification_by_cycle[cycle_key] = tests_passed
        for key in FINDING_FIELDS:
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                findings[key.removeprefix("findings_")] += value
                findings_measured[key.removeprefix("findings_")] += 1
        resolution = payload.get("model_resolution")
        if resolution is None and row.get("model_requested") and row.get("model_resolved"):
            resolution = (
                "matched" if row["model_requested"] == row["model_resolved"]
                else "mismatch_known"
            )
        if resolution in {"matched", "mismatch_known", "mismatch_unrecognized", "unreported"}:
            model_resolutions[resolution] += 1
        if payload.get("record_kind") == "shadow":
            jev_shadow_rows += 1
            if payload.get("jev_status"):
                jev_status[payload["jev_status"]] += 1
            agreement = payload.get("jev_agreement")
            if isinstance(agreement, bool):
                jev_agreement["agree" if agreement else "disagree"] += 1
            value = payload.get("jev_confidence")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                for name, lower, upper in CONFIDENCE_BUCKETS:
                    if lower <= value < upper:
                        confidence[name] += 1
                        break
        if id(row) in stage_event_row_ids and isinstance(row.get("duration_ms"), int) and row["duration_ms"] >= 0:
            durations.append(row["duration_ms"])
        cost = payload.get("cost_usd")
        if id(row) in stage_event_row_ids and isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            costs.append(float(cost))
    for row in stages:
        stamp = _timestamp(row)
        if stamp:
            daily[stamp.astimezone(timezone.utc).date().isoformat()] += 1

    tasks = {row["task_id"] for row in current}
    prev_tasks = {row["task_id"] for row in previous}
    comparison_ready = len(tasks) >= minimum and len(prev_tasks) >= minimum
    profiles_by_role = {
        role: _counter(counts) for role, counts in sorted(profiles.items())
    }
    drift_count = model_resolutions["mismatch_known"] + model_resolutions["mismatch_unrecognized"]
    drift_measured = (
        model_resolutions["matched"] + model_resolutions["mismatch_known"]
        + model_resolutions["mismatch_unrecognized"]
    )
    verification = Counter("passed" if passed else "failed"
                            for passed in verification_by_cycle.values())
    verification_groups = {
        row["payload"].get("cycle_id") or row["task_id"] for row in current
    }

    return {
        "repository": repo_id,
        "period": {
            "days": days,
            "label": f"last {days} days" if days is not None else "all time",
            "from": start.isoformat() if start else None,
            "through": now.isoformat(),
        },
        "sample_minimum": minimum,
        "summary": {
            "tasks": len(tasks),
            "stages": len(stages),
            "roles": _counter(roles),
            "first_pass": _first_pass(rows, start, now),
            "fallback_stages": fallbacks if stages else None,
            "dispatch_blockages": _counter(blockages),
            "model_drift": {
                "mismatches": drift_count if drift_measured else None,
                "measured": drift_measured,
                "unreported": model_resolutions["unreported"],
            },
            "verdicts": _counter(verdicts),
            "findings": {
                name: {"count": findings[name] if findings_measured[name] else None,
                       "measured": findings_measured[name]}
                for name in ("critical", "high", "medium", "low")
            },
            "verification": {
                "passed": verification["passed"] if verification else None,
                "failed": verification["failed"] if verification else None,
                "measured": sum(verification.values()),
                "not_reported": len(verification_groups) - sum(verification.values()),
            },
            "duration_ms": {
                "measured": len(durations),
                "mean": sum(durations) / len(durations) if durations else None,
                "total": sum(durations) if durations else None,
            },
            "cost_usd": {
                "measured": len(costs),
                "total": sum(costs) if costs else None,
            },
        },
        "profiles_by_role": profiles_by_role,
        "trend_daily": dict(sorted(daily.items())),
        "comparison": {
            "available": comparison_ready,
            "reason": None if comparison_ready else f"at least {minimum} tasks in each period are required",
            "current_tasks": len(tasks),
            "previous_tasks": len(prev_tasks),
            "task_change": len(tasks) - len(prev_tasks) if comparison_ready else None,
            "current_first_pass": _first_pass(rows, start, now),
            "previous_first_pass": _first_pass(rows, start - timedelta(days=days), start)
            if start is not None else None,
        },
        "jev": {
            "shadow_rows": jev_shadow_rows,
            "observations": sum(jev_agreement.values()),
            "status": _counter(jev_status),
            "agreement": _counter(jev_agreement),
            "confidence_buckets": {
                "ranges": {"low": "0.00–<0.50", "medium": "0.50–<0.75", "high": "0.75–1.00"},
                "counts": {name: confidence[name] for name, _, _ in CONFIDENCE_BUCKETS},
            },
            "first_pass_by_suggested_profile": _profile_outcomes(current, minimum),
        },
    }


def _bar(value: int, maximum: int, width: int = 12) -> str:
    if maximum <= 0:
        return "░" * width
    filled = round(width * value / maximum)
    return "█" * filled + "░" * (width - filled)


def _count_table(label: str, unit: str, counts: dict, *, empty: str | None = None) -> list[str]:
    """A small ranked table with exact counts beside proportional bars."""
    if not counts:
        return [empty] if empty else []
    maximum = max(counts.values())
    rows = [f"| {label} | {unit} | |", "|---|---:|---|"]
    rows.extend(f"| {name} | {count} | {_bar(count, maximum)} |" for name, count in counts.items())
    return rows


def _format_rate(rate: dict) -> str:
    if rate["value"] is None:
        return f"unknown ({rate['passed']}/{rate['total']}; need {rate['minimum']})"
    return f"{rate['passed']}/{rate['total']} ({rate['value']:.0%})"


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    rate = summary["first_pass"]
    rate_text = _format_rate(rate)
    lines = [
        f"## Code Cycle stats · {report['period']['label']}",
        "",
        f"**{summary['tasks']} tasks** · {summary['stages']} stages · first-pass approval **{rate_text}**",
        "",
        "### Activity",
        "",
        "| Day (UTC) | Stages | Activity |",
        "|---|---:|---|",
    ]
    daily = report["trend_daily"]
    recent_days = list(daily.items())[-14:]
    maximum = max((count for _, count in recent_days), default=0)
    if recent_days:
        lines.extend(f"| {day} | {count} | {_bar(count, maximum)} |" for day, count in recent_days)
    else:
        lines.append("| No recorded activity | 0 | — |")
    lines += ["", "### By role and profile", ""]
    lines += _count_table("Role", "Stages", summary["roles"], empty="No stages recorded.")
    if report["profiles_by_role"]:
        lines += ["", "| Role | Profile | Stages |", "|---|---|---:|"]
        for role, profiles in report["profiles_by_role"].items():
            lines.extend(f"| {role} | {profile} | {count} |" for profile, count in profiles.items())
    else:
        lines += ["", "No profile breakdown is available yet."]

    lines += ["", "### Review verdicts", ""]
    lines += _count_table("Verdict", "Reviews", summary["verdicts"], empty="No review verdicts recorded.")

    lines += ["", "### Findings by severity", ""]
    findings = summary["findings"]
    if any(item["measured"] for item in findings.values()):
        known = [item["count"] for item in findings.values() if item["count"] is not None]
        maximum = max(known, default=0)
        lines += ["| Severity | Findings | Reports | |", "|---|---:|---:|---|"]
        for name in ("critical", "high", "medium", "low"):
            item = findings[name]
            if item["count"] is None:
                lines.append(f"| {name} | unknown | 0 | — |")
            else:
                lines.append(f"| {name} | {item['count']} | {item['measured']} | {_bar(item['count'], maximum)} |")
    else:
        lines.append("No review reported finding counts; severities are unknown.")

    lines += ["", "### Operations", ""]
    lines.append(
        f"- Fallback stages: **{summary['fallback_stages']}**"
        if summary["fallback_stages"] is not None else "- Fallback stages: not measured"
    )
    drift = summary["model_drift"]
    lines.append(
        f"- Model drift: **{drift['mismatches']}/{drift['measured']} measured**; {drift['unreported']} unreported"
        if drift["measured"] else "- Model drift: not measured"
    )
    verification = summary["verification"]
    if verification["measured"]:
        lines.append(
            f"- Test verification: **{verification['passed']} passed, {verification['failed']} failed**; "
            f"{verification['measured']} reported, {verification['not_reported']} not reported"
        )
    else:
        lines.append(f"- Test verification: not reported for {verification['not_reported']} task(s)")
    duration, cost = summary["duration_ms"], summary["cost_usd"]
    lines.append(
        f"- Duration: **{duration['total'] / 1000:.1f}s total** across {duration['measured']} measured stages"
        if duration["total"] is not None else "- Duration: not measured"
    )
    lines.append(
        f"- Recorded cost: **${cost['total']:.6f}** across {cost['measured']} measured stages"
        if cost["total"] is not None else "- Cost: not measured"
    )
    lines += ["", "Dispatch blockages:", ""]
    lines += _count_table("Missing capability", "Dispatches", summary["dispatch_blockages"],
                          empty="None recorded.")

    jev = report["jev"]
    lines += ["", "### Rules vs. Jev", ""]
    if jev["shadow_rows"]:
        lines += _count_table("Shadow status", "Records", jev["status"])
    if jev["observations"]:
        lines += ["", f"Agreement across {jev['observations']} observations:", ""]
        lines += _count_table("Rules vs. Jev", "Observations", jev["agreement"])
        lines += ["", "| Confidence | Range | Suggestions | |", "|---|---|---:|---|"]
        counts = jev["confidence_buckets"]["counts"]
        ranges = jev["confidence_buckets"]["ranges"]
        maximum = max(counts.values(), default=0)
        lines.extend(
            f"| {name} | {ranges[name]} | {count} | {_bar(count, maximum)} |"
            for name, count in counts.items()
        )
        outcomes = jev["first_pass_by_suggested_profile"]
        lines += [""]
        if outcomes:
            lines += ["| Suggested profile | First-pass approval |", "|---|---|"]
            lines.extend(f"| {profile} | {_format_rate(rate)} |" for profile, rate in outcomes.items())
        else:
            lines.append("No suggestion could be joined to a first-review outcome yet.")
    elif jev["shadow_rows"]:
        lines += ["", "No Jev suggestions could be compared; recorded statuses are shown above."]
    else:
        lines.append("No Jev shadow observations in this period; these metrics are not applicable.")

    comparison = report["comparison"]
    lines += ["", "### Period comparison", ""]
    if comparison["available"]:
        lines += [
            "| | This period | Previous period |",
            "|---|---:|---:|",
            f"| Tasks | {comparison['current_tasks']} | {comparison['previous_tasks']} "
            f"({comparison['task_change']:+d}) |",
            f"| First-pass approval | {_format_rate(comparison['current_first_pass'])} | "
            f"{_format_rate(comparison['previous_first_pass'])} |",
        ]
    else:
        lines.append(f"Not compared: {comparison['reason']}.")
    lines += ["", "Rates and suggested-profile outcomes need at least "
              f"{report['sample_minimum']} observations. Missing measurements remain unknown."]
    return "\n".join(lines)


def _report_config(cwd: Path) -> str:
    config_path = cwd / ".code-cycle.yml"
    try:
        config = load_config(config_path) if config_path.exists() else {}
    except CycleDriverError as error:
        raise StatsError("could not read or validate the repository configuration") from error
    repo_id = repository_of(config)
    if not repo_id:
        raise StatsError(
            "no repository identity: set code_cycle.repository.selector in .code-cycle.yml"
        )
    return repo_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    span = parser.add_mutually_exclusive_group()
    span.add_argument("--days", type=int, default=30, help="look back this many days (default: 30)")
    span.add_argument("--all-time", action="store_true", help="include all recorded history")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    parser.add_argument("--database", type=Path, default=default_database_path(), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.all_time and args.days < 1:
        parser.error("--days must be at least 1")
    try:
        repo_id = _report_config(args.cwd)
        rows = _read_rows(args.database, repo_id)
        report = aggregate(rows, repo_id=repo_id, days=None if args.all_time else args.days)
    except StatsError as error:
        print(f"cc-stats: {error}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
