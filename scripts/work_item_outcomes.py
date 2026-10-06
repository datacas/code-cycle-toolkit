#!/usr/bin/env python3
"""Record explicit or provider-observed work-item disposition transitions."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from telemetry import (
    Telemetry,
    TelemetryError,
    WORK_ITEM_OUTCOMES,
    default_database_path,
)


_JIRA_RESOLUTIONS = {
    "fixed": "resolved",
    "done": "resolved",
    "complete": "resolved",
    "completed": "resolved",
    "resolved": "resolved",
    "won't do": "closed_unresolved",
    "wont do": "closed_unresolved",
    "won't fix": "closed_unresolved",
    "wont fix": "closed_unresolved",
    "duplicate": "closed_unresolved",
    "cannot reproduce": "closed_unresolved",
    "declined": "closed_unresolved",
    "incomplete": "closed_unresolved",
    "cancelled": "closed_unresolved",
    "canceled": "closed_unresolved",
}


def _later_timestamp(value: str | None, previous: str | None) -> bool:
    if not value or not previous:
        return False
    try:
        current = datetime.fromisoformat(value.replace("Z", "+00:00"))
        prior = datetime.fromisoformat(previous.replace("Z", "+00:00"))
    except ValueError:
        return False
    return current.tzinfo is not None and prior.tzinfo is not None and current > prior


def map_github_state(
    state: str, state_reason: str | None, previous: str | dict | None = None, *, closed_at: str | None = None
) -> str:
    """Map GitHub issue state and reason without inferring resolution from a PR merge."""
    previous_observed_at = previous.get("observed_at") if isinstance(previous, dict) else None
    previous_outcome = previous.get("outcome") if isinstance(previous, dict) else previous
    normalized_state = (state or "").strip().upper()
    reason = (state_reason or "").strip().upper()
    if normalized_state == "CLOSED":
        if reason in {"NOT_PLANNED", "DUPLICATE"}:
            return "closed_unresolved"
        if previous_outcome in {"reopened", "reverted"}:
            if reason == "COMPLETED" and _later_timestamp(closed_at, previous_observed_at):
                return "resolved"
            return previous_outcome
        if reason == "COMPLETED":
            return "resolved"
        return previous_outcome if previous_outcome in WORK_ITEM_OUTCOMES - {"unknown"} else "unknown"
    if normalized_state == "OPEN":
        if previous_outcome in {"resolved", "closed_unresolved"}:
            return "reopened"
        if previous_outcome in {"pr_merged", "reopened", "reverted"}:
            return previous_outcome
        return "unknown"
    return previous_outcome if previous_outcome in WORK_ITEM_OUTCOMES - {"unknown"} else "unknown"


def map_plane_group(
    group: str | None, previous: str | None = None, *, completion_after_previous: bool = False
) -> str:
    """Map a Plane state group to a work-item disposition."""
    normalized = (group or "").strip().casefold()
    if normalized in {"cancelled", "canceled"}:
        return "closed_unresolved"
    if previous == "reverted":
        if normalized in {"completed", "complete", "done"} and completion_after_previous:
            return "resolved"
        return "reverted"
    if normalized in {"completed", "complete", "done"}:
        return "resolved"
    if previous in {"resolved", "closed_unresolved"}:
        return "reopened"
    if previous in {"pr_merged", "reopened", "reverted"}:
        return previous
    return "unknown"


def map_jira_resolution(
    status_category: str | None,
    resolution: str | None,
    resolution_map: dict[str, str] | None = None,
    previous: str | None = None,
    completion_after_previous: bool = False,
) -> str:
    """Require Jira's Done category and a known resolution before finalizing."""
    if (status_category or "").strip().casefold() != "done":
        if previous in {"resolved", "closed_unresolved"}:
            return "reopened"
        if previous in {"pr_merged", "reopened", "reverted"}:
            return previous
        return "unknown"
    if not resolution:
        return "unknown"
    configured = {
        str(name).strip().casefold(): str(outcome).strip()
        for name, outcome in (resolution_map or {}).items()
    }
    outcome = configured.get(resolution.strip().casefold())
    if outcome is None:
        outcome = _JIRA_RESOLUTIONS.get(resolution.strip().casefold())
    if outcome not in {"resolved", "closed_unresolved"}:
        return "unknown"
    if outcome == "closed_unresolved":
        return outcome
    if previous == "reverted":
        return "resolved" if outcome == "resolved" and completion_after_previous else "reverted"
    return outcome


def _pull_request_ids(snapshot: dict) -> list[str]:
    references = snapshot.get("closedByPullRequestsReferences") or []
    if not isinstance(references, list):
        return []
    result = []
    for reference in references:
        if isinstance(reference, dict) and reference.get("number") is not None:
            result.append(str(reference["number"]))
    return list(dict.fromkeys(result))


def _latest_timestamp(values) -> str | None:
    parsed = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is not None:
            parsed.append(timestamp.astimezone(timezone.utc))
    return max(parsed).isoformat() if parsed else None


def map_github_issue(snapshot: dict, previous: str | dict | None = None) -> dict:
    """Normalize the fields returned by `gh issue view --json`."""
    if not isinstance(snapshot, dict):
        raise TelemetryError("GitHub returned an invalid issue snapshot")
    outcome = map_github_state(snapshot.get("state"), snapshot.get("stateReason"), previous,
                               closed_at=snapshot.get("closedAt"))
    pull_requests = _pull_request_ids(snapshot)
    return {
        "outcome": outcome,
        "observed_at": snapshot.get("updatedAt") or snapshot.get("closedAt"),
        "pull_request_ids": pull_requests,
        "merged_pr_ids": [],
        "resolving_pr_ids": pull_requests if outcome == "resolved" else [],
    }


def _cycle_tasks(telemetry: Telemetry, repo_id: str) -> dict[str, list[str]]:
    tasks: dict[str, set[str]] = {}
    for row in telemetry.rows(repo_id):
        cycle_id = row["payload"].get("cycle_id")
        if cycle_id:
            tasks.setdefault(row["task_id"], set()).add(str(cycle_id))
    return {task_id: sorted(cycle_ids) for task_id, cycle_ids in tasks.items()}


def _cycle_pull_requests(telemetry: Telemetry, repo_id: str) -> dict[str, list[str]]:
    """Read numeric PR references captured on implementation verdicts."""
    pull_requests: dict[str, set[str]] = {}
    for row in telemetry.rows(repo_id):
        if row.get("role") != "implement":
            continue
        pull_request_id = row["payload"].get("pull_request_id")
        if pull_request_id:
            pull_requests.setdefault(str(row["task_id"]), set()).add(str(pull_request_id))
    return {task_id: sorted(ids) for task_id, ids in pull_requests.items()}


def reconcile_github(
    telemetry: Telemetry, *, repo_id: str, repository: str,
    runner=subprocess.run,
) -> tuple[int, int]:
    """Query each cycled issue with gh; a failed query leaves its state untouched."""
    tasks = _cycle_tasks(telemetry, repo_id)
    cycle_pull_requests = _cycle_pull_requests(telemetry, repo_id)
    updated = failed = 0
    for work_item_id, cycle_ids in sorted(tasks.items()):
        previous = telemetry.latest_work_item_disposition(
            repo_id, "github", repository, work_item_id
        )
        previous_outcome = previous["outcome"] if previous else None

        def record_failure(failure_kind: str, pull_request_id: str | None = None) -> None:
            telemetry.record_work_item_reconciliation_failure(
                repo_id=repo_id, provider="github", repository=repository,
                work_item_id=work_item_id, failure_kind=failure_kind,
                pull_request_id=pull_request_id,
            )

        command = [
            "gh", "issue", "view", work_item_id, "--repo", repository,
            "--json", "number,state,stateReason,updatedAt,closedAt,closedByPullRequestsReferences",
        ]
        try:
            result = runner(command, check=False, capture_output=True, text=True)
        except OSError:
            record_failure("provider_unavailable")
            failed += 1
            print(f"GitHub query unavailable for work item {work_item_id}; no observation recorded.",
                  file=sys.stderr)
            continue
        if result.returncode:
            record_failure("provider_query_failed")
            failed += 1
            print(f"GitHub query failed for work item {work_item_id}; no observation recorded.",
                  file=sys.stderr)
            continue
        try:
            snapshot = json.loads(result.stdout)
            normalized = map_github_issue(snapshot, previous)
            item_id = str(snapshot.get("number"))
            if item_id != work_item_id:
                raise TelemetryError("GitHub returned a different issue number")
        except (TypeError, json.JSONDecodeError, TelemetryError):
            record_failure("invalid_snapshot")
            failed += 1
            print(f"GitHub returned an invalid snapshot for work item {work_item_id}; no observation recorded.",
                  file=sys.stderr)
            continue
        pull_request_ids = list(dict.fromkeys(
            (previous or {}).get("pull_request_ids", [])
            + cycle_pull_requests.get(work_item_id, [])
            + normalized["pull_request_ids"]
        ))
        merged_pr_ids = set((previous or {}).get("merged_pr_ids", []))
        observed_timestamps = [normalized["observed_at"]]
        query_failed = False
        for pull_request_id in pull_request_ids:
            command = [
                "gh", "pr", "view", pull_request_id, "--repo", repository,
                "--json", "number,mergedAt,updatedAt",
            ]
            try:
                pr_result = runner(command, check=False, capture_output=True, text=True)
            except OSError:
                record_failure("provider_unavailable", pull_request_id)
                query_failed = True
                break
            if pr_result.returncode:
                record_failure("pull_request_query_failed", pull_request_id)
                query_failed = True
                break
            try:
                pr_snapshot = json.loads(pr_result.stdout)
                if not isinstance(pr_snapshot, dict):
                    raise TelemetryError("GitHub returned an invalid pull request snapshot")
                if str(pr_snapshot.get("number")) != pull_request_id:
                    raise TelemetryError("GitHub returned a different pull request number")
                merged_at = pr_snapshot.get("mergedAt")
                if merged_at:
                    merged_pr_ids.add(pull_request_id)
                    observed_timestamps.append(merged_at)
            except (TypeError, json.JSONDecodeError, TelemetryError):
                record_failure("pull_request_query_failed", pull_request_id)
                query_failed = True
                break
        if query_failed:
            failed += 1
            print(f"GitHub pull request query failed for work item {work_item_id}; "
                  "no observation recorded.", file=sys.stderr)
            continue
        disposition = normalized["outcome"]
        if disposition == "unknown" and merged_pr_ids and previous_outcome not in {
            "resolved", "closed_unresolved", "reopened", "reverted",
        }:
            disposition = "pr_merged"
        saved = telemetry.record_work_item_disposition(
            repo_id=repo_id,
            provider="github",
            repository=repository,
            work_item_id=work_item_id,
            outcome=disposition,
            source="provider_query",
            observed_at=_latest_timestamp(observed_timestamps),
            cycle_ids=cycle_ids,
            pull_request_ids=pull_request_ids,
            merged_pr_ids=sorted(merged_pr_ids),
            resolving_pr_ids=pull_request_ids if disposition == "resolved" else [],
        )
        updated += int(saved["created"])
    return updated, failed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=default_database_path(),
                        help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="record a person-supplied disposition")
    record.add_argument("--repo-id", required=True, help="configured Code Cycle repository ID")
    record.add_argument("--provider", choices=("github", "plane", "jira"), required=True)
    record.add_argument("--repository", required=True, help="provider repository or project key")
    record.add_argument("--work-item-id", required=True)
    record.add_argument("--outcome", choices=sorted(WORK_ITEM_OUTCOMES), required=True)
    record.add_argument("--observed-at", help="ISO-8601 timestamp with timezone; defaults to now")
    record.add_argument("--cycle-id", action="append", default=[])
    record.add_argument("--pull-request-id", action="append", default=[])
    record.add_argument("--merged-pr-id", action="append", default=[])
    record.add_argument("--resolving-pr-id", action="append", default=[])
    record.add_argument("--reverted-pr-id", action="append", default=[],
                        help="resolving PR whose merge was reverted; required for outcome=reverted")

    reconcile = subparsers.add_parser(
        "reconcile", help="query GitHub for work items that have recorded cycles"
    )
    reconcile.add_argument("--repo-id", required=True, help="configured Code Cycle repository ID")
    reconcile.add_argument("--provider", choices=("github",), default="github")
    reconcile.add_argument("--repository", required=True, help="GitHub owner/repository")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        telemetry = Telemetry(args.database)
        if args.command == "record":
            result = telemetry.record_work_item_disposition(
                repo_id=args.repo_id,
                provider=args.provider,
                repository=args.repository,
                work_item_id=args.work_item_id,
                outcome=args.outcome,
                source="explicit",
                observed_at=args.observed_at,
                cycle_ids=args.cycle_id,
                pull_request_ids=args.pull_request_id,
                merged_pr_ids=args.merged_pr_id,
                resolving_pr_ids=args.resolving_pr_id,
                reverted_pr_ids=args.reverted_pr_id,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        updated, failed = reconcile_github(
            telemetry, repo_id=args.repo_id, repository=args.repository
        )
    except (OSError, TelemetryError) as error:
        print(f"cc-work-item-outcomes: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"updated": updated, "failed": failed}, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
