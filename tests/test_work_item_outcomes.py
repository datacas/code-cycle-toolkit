"""Provider-observed work-item dispositions remain separate from cycle outcomes."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import telemetry as tm  # noqa: E402
import work_item_outcomes as outcomes  # noqa: E402


class WorkItemOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "telemetry.sqlite"
        self.store = tm.Telemetry(self.database)

    def add_cycle(self, task_id: str, cycle_id: str) -> None:
        self.store.record_stage("owner/repo", task_id, "implement",
                                record_kind="dispatch", cycle_id=cycle_id)

    def add_scoped_cycle(
        self, repo_id: str, task_id: str, cycle_id: str,
        work_item_repository: str, pull_request_id: str,
    ) -> None:
        identity = {
            "work_item_provider": "github",
            "work_item_repository": work_item_repository,
        }
        self.store.record_stage(repo_id, task_id, "implement",
                                record_kind="dispatch", cycle_id=cycle_id, **identity)
        self.store.record_stage(repo_id, task_id, "implement",
                                record_kind="verdict", cycle_id=cycle_id,
                                pull_request_id=pull_request_id, **identity)

    def test_schema_fourteen_records_transitions_and_deduplicates_snapshots(self) -> None:
        first = self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="120", outcome="resolved", source="provider_query",
            observed_at="2026-10-01T10:00:00Z", cycle_ids=["cycle-1"],
            pull_request_ids=["44"], resolving_pr_ids=["44"],
        )
        repeated = self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="120", outcome="resolved", source="provider_query",
            observed_at="2026-10-02T10:00:00Z", cycle_ids=["cycle-1"],
            pull_request_ids=["44"], resolving_pr_ids=["44"],
        )
        stale = self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="120", outcome="reopened", source="provider_query",
            observed_at="2026-09-30T10:00:00Z",
        )

        self.assertTrue(first["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual("unchanged", repeated["reason"])
        self.assertFalse(stale["created"])
        self.assertEqual("stale", stale["reason"])
        self.assertEqual(16, tm.SCHEMA_VERSION)
        self.assertEqual(1, len(self.store.work_item_disposition_events("owner/repo")))
        event = self.store.latest_work_item_disposition("owner/repo", "github", "owner/repo", "120")
        self.assertEqual(["cycle-1"], event["cycle_ids"])
        self.assertEqual(["44"], event["resolving_pr_ids"])
        self.assertEqual("2026-10-01T10:00:00+00:00", event["observed_at"])

        reopened = self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="120", outcome="reopened", source="provider_query",
            observed_at="2026-10-03T10:00:00Z",
        )
        self.assertTrue(reopened["created"])
        self.assertEqual("reopened", self.store.latest_work_item_disposition(
            "owner/repo", "github", "owner/repo", "120")["outcome"])

    def test_disposition_rejects_secret_shaped_references_and_bad_links(self) -> None:
        common = dict(repo_id="owner/repo", provider="github", repository="owner/repo",
                      work_item_id="120", source="explicit")
        with self.assertRaises(tm.TelemetryError):
            self.store.record_work_item_disposition(**{**common, "work_item_id": "ghp_not-a-reference"},
                                                    outcome="resolved")
        with self.assertRaises(tm.TelemetryError):
            self.store.record_work_item_disposition(**common, outcome="resolved",
                                                    pull_request_ids=["44"], resolving_pr_ids=["45"])
        self.assertEqual([], self.store.work_item_disposition_events())

    def test_per_pr_merge_updates_do_not_rewrite_the_resolving_pr_snapshot(self) -> None:
        self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="120", outcome="resolved", source="provider_query",
            observed_at="2026-10-01T10:00:00Z", pull_request_ids=["44"],
            merged_pr_ids=["44"], resolving_pr_ids=["44"],
        )
        linked_later = self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="120", outcome="resolved", source="provider_query",
            observed_at="2026-10-01T12:00:00Z", cycle_ids=["cycle-2"],
            pull_request_ids=["44", "45"], resolving_pr_ids=["44", "45"],
        )
        self.assertTrue(linked_later["created"])
        self.assertEqual(["44", "45"], linked_later["event"]["pull_request_ids"])
        self.assertEqual(["44"], linked_later["event"]["resolving_pr_ids"])

        later_merge = self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="120", outcome="resolved", source="provider_query",
            observed_at="2026-10-02T10:00:00Z", pull_request_ids=["44", "45"],
            merged_pr_ids=["44", "45"], resolving_pr_ids=["44", "45"],
        )

        self.assertTrue(later_merge["created"])
        latest = self.store.latest_work_item_disposition("owner/repo", "github", "owner/repo", "120")
        self.assertEqual(["44", "45"], latest["merged_pr_ids"])
        self.assertEqual(["44"], latest["resolving_pr_ids"])
        self.assertEqual(3, len(self.store.work_item_disposition_events("owner/repo")))

    def test_a_later_pr_joins_the_resolving_snapshot_only_when_explicitly_associated(self) -> None:
        self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="120", outcome="resolved", source="provider_query",
            observed_at="2026-10-01T10:00:00Z", pull_request_ids=["44"],
            merged_pr_ids=["44"], resolving_pr_ids=["44"],
        )
        linked_later = self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="120", outcome="resolved", source="provider_query",
            observed_at="2026-10-02T10:00:00Z", pull_request_ids=["44", "45"],
            resolving_pr_ids=["44", "45"],
        )
        self.assertTrue(linked_later["created"])
        self.assertEqual(["44"], linked_later["event"]["resolving_pr_ids"])

        corrected = self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="120", outcome="resolved", source="explicit",
            observed_at="2026-10-03T10:00:00Z", pull_request_ids=["45"],
            resolving_pr_ids=["45"],
        )

        self.assertTrue(corrected["created"])
        self.assertEqual(["44", "45"], corrected["event"]["resolving_pr_ids"])

    def test_only_reverting_a_merged_resolving_pr_revokes_resolution(self) -> None:
        self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="120", outcome="resolved", source="provider_query",
            observed_at="2026-10-01T10:00:00Z", pull_request_ids=["44", "45"],
            merged_pr_ids=["44", "45"], resolving_pr_ids=["44"],
        )
        with self.assertRaises(tm.TelemetryError):
            self.store.record_work_item_disposition(
                repo_id="owner/repo", provider="github", repository="owner/repo",
                work_item_id="120", outcome="reverted", source="provider_query",
                observed_at="2026-10-02T10:00:00Z", reverted_pr_ids=["45"],
            )
        self.assertEqual("resolved", self.store.latest_work_item_disposition(
            "owner/repo", "github", "owner/repo", "120")["outcome"])

        recorded = self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="120", outcome="reverted", source="provider_query",
            observed_at="2026-10-03T10:00:00Z", reverted_pr_ids=["44"],
        )
        self.assertTrue(recorded["created"])
        self.assertEqual("reverted", recorded["event"]["outcome"])
        self.assertEqual(["44"], recorded["event"]["resolving_pr_ids"])

        completed_again = self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="120", outcome="resolved", source="provider_query",
            observed_at="2026-10-04T10:00:00Z", pull_request_ids=["44"],
            merged_pr_ids=["44"], resolving_pr_ids=["44"],
        )
        self.assertTrue(completed_again["created"])
        self.assertEqual("resolved", completed_again["event"]["outcome"])
        self.assertEqual(["44"], completed_again["event"]["reverted_pr_ids"])

    def test_provider_mappings_keep_unknown_distinct_and_allow_custom_jira_resolutions(self) -> None:
        self.assertEqual("resolved", outcomes.map_github_state("CLOSED", "COMPLETED"))
        self.assertEqual("closed_unresolved", outcomes.map_github_state("CLOSED", "DUPLICATE"))
        self.assertEqual("reopened", outcomes.map_github_state("OPEN", None, "resolved"))
        self.assertEqual("pr_merged", outcomes.map_github_state("OPEN", None, "pr_merged"))
        self.assertEqual("unknown", outcomes.map_github_state("OPEN", None))
        reverted = {"outcome": "reverted", "observed_at": "2026-10-03T10:00:00+00:00"}
        self.assertEqual("reverted", outcomes.map_github_state(
            "CLOSED", "COMPLETED", reverted, closed_at="2026-10-01T10:00:00Z"))
        self.assertEqual("resolved", outcomes.map_github_state(
            "CLOSED", "COMPLETED", reverted, closed_at="2026-10-04T10:00:00Z"))
        self.assertEqual("unknown", outcomes.map_github_issue({
            "state": "OPEN", "closedByPullRequestsReferences": [{"number": 44, "mergedAt": "now"}],
        })["outcome"])
        self.assertEqual("resolved", outcomes.map_plane_group("completed"))
        self.assertEqual("closed_unresolved", outcomes.map_plane_group("cancelled"))
        self.assertEqual("unknown", outcomes.map_plane_group("started"))
        self.assertEqual("reopened", outcomes.map_plane_group("started", "resolved"))
        self.assertEqual("reverted", outcomes.map_plane_group("completed", "reverted"))
        self.assertEqual("resolved", outcomes.map_plane_group(
            "completed", "reverted", completion_after_previous=True))
        self.assertEqual("unknown", outcomes.map_jira_resolution("Done", None))
        self.assertEqual("unknown", outcomes.map_jira_resolution("Done", "Custom Resolution"))
        self.assertEqual("resolved", outcomes.map_jira_resolution(
            "Done", "Custom Resolution", {"Custom Resolution": "resolved"}))
        self.assertEqual("closed_unresolved", outcomes.map_jira_resolution(
            "Done", "Won't Do"))
        self.assertEqual("reverted", outcomes.map_jira_resolution(
            "Done", "Fixed", previous="reverted"))
        self.assertEqual("resolved", outcomes.map_jira_resolution(
            "Done", "Fixed", previous="reverted", completion_after_previous=True))
        self.assertEqual("unknown", outcomes.map_jira_resolution(
            "Done", "Custom Resolution", previous="reverted"))
        self.assertEqual("unknown", outcomes.map_jira_resolution("In Progress", "Fixed"))
        self.assertEqual("reopened", outcomes.map_jira_resolution("In Progress", None,
                                                                  previous="resolved"))


    def test_explicit_unresolved_closures_override_older_reopened_and_reverted_states(self) -> None:
        reopened = {"outcome": "reopened", "observed_at": "2026-10-01T10:00:00+00:00"}
        reverted = {"outcome": "reverted", "observed_at": "2026-10-01T10:00:00+00:00"}

        self.assertEqual("closed_unresolved", outcomes.map_github_state(
            "CLOSED", "NOT_PLANNED", reopened, closed_at="2026-10-02T10:00:00Z"))
        self.assertEqual("closed_unresolved", outcomes.map_github_state(
            "CLOSED", "DUPLICATE", reverted, closed_at="2026-10-02T10:00:00Z"))
        self.assertEqual("closed_unresolved", outcomes.map_plane_group("cancelled", "reverted"))
        self.assertEqual("closed_unresolved", outcomes.map_jira_resolution(
            "Done", "Duplicate", previous="reverted"))

    def test_failed_github_query_does_not_replace_a_prior_disposition(self) -> None:
        self.add_cycle("123", "cycle-123")
        self.add_cycle("456", "cycle-456")
        self.store.record_work_item_disposition(
            repo_id="owner/repo", provider="github", repository="owner/repo",
            work_item_id="456", outcome="resolved", source="explicit",
            observed_at="2026-10-01T09:00:00Z", cycle_ids=["cycle-456"],
        )
        responses = [
            subprocess.CompletedProcess([], 0, json.dumps({
                "number": 123, "state": "CLOSED", "stateReason": "COMPLETED",
                "updatedAt": "2026-10-02T10:00:00Z",
                "closedByPullRequestsReferences": [],
            }), ""),
            subprocess.CompletedProcess([], 1, "", "provider unavailable"),
        ]
        runner = Mock(side_effect=responses)

        updated, failed = outcomes.reconcile_github(
            self.store, repo_id="owner/repo", repository="owner/repo", runner=runner
        )

        self.assertEqual((1, 1), (updated, failed))
        self.assertEqual("resolved", self.store.latest_work_item_disposition(
            "owner/repo", "github", "owner/repo", "123")["outcome"])
        preserved = self.store.latest_work_item_disposition("owner/repo", "github", "owner/repo", "456")
        self.assertEqual("resolved", preserved["outcome"])
        self.assertEqual("explicit", preserved["source"])
        self.assertEqual("2026-10-01T09:00:00+00:00", preserved["observed_at"])
        failures = [failure for failure in self.store.work_item_reconciliation_failures("owner/repo")
                    if failure["work_item_id"] == "456"]
        self.assertEqual(1, len(failures))
        self.assertEqual("provider_query_failed", failures[0]["failure_kind"])
        self.assertTrue(failures[0]["observed_at"])

    def test_failed_linked_pr_query_does_not_record_issue_resolution(self) -> None:
        self.add_cycle("123", "cycle-123")
        runner = Mock(side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps({
                "number": 123, "state": "CLOSED", "stateReason": "COMPLETED",
                "updatedAt": "2026-10-02T10:00:00Z",
                "closedByPullRequestsReferences": [{"number": 44}],
            }), ""),
            subprocess.CompletedProcess([], 1, "", "provider unavailable"),
        ])

        updated, failed = outcomes.reconcile_github(
            self.store, repo_id="owner/repo", repository="owner/repo", runner=runner
        )

        self.assertEqual((0, 1), (updated, failed))
        self.assertIsNone(self.store.latest_work_item_disposition(
            "owner/repo", "github", "owner/repo", "123"))
        failures = self.store.work_item_reconciliation_failures("owner/repo")
        self.assertEqual("pull_request_query_failed", failures[0]["failure_kind"])
        self.assertEqual("44", failures[0]["pull_request_id"])

    def test_invalid_linked_pr_snapshot_records_failure_without_disposition(self) -> None:
        self.add_cycle("123", "cycle-123")
        runner = Mock(side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps({
                "number": 123, "state": "CLOSED", "stateReason": "COMPLETED",
                "updatedAt": "2026-10-02T10:00:00Z",
                "closedByPullRequestsReferences": [{"number": 44}],
            }), ""),
            subprocess.CompletedProcess([], 0, "[]", ""),
        ])

        updated, failed = outcomes.reconcile_github(
            self.store, repo_id="owner/repo", repository="owner/repo", runner=runner
        )

        self.assertEqual((0, 1), (updated, failed))
        self.assertIsNone(self.store.latest_work_item_disposition(
            "owner/repo", "github", "owner/repo", "123"))
        failure = self.store.work_item_reconciliation_failures("owner/repo")[0]
        self.assertEqual("pull_request_query_failed", failure["failure_kind"])
        self.assertEqual("44", failure["pull_request_id"])

    def test_reconcile_snapshots_a_pull_request_from_a_toolkit_cycle(self) -> None:
        self.add_cycle("123", "cycle-123")
        self.store.record_stage("owner/repo", "123", "implement",
                                record_kind="verdict", cycle_id="cycle-123",
                                pull_request_id="44")

        def runner(command, **_kwargs):
            if command[1] == "issue":
                snapshot = {
                    "number": 123, "state": "CLOSED", "stateReason": "COMPLETED",
                    "updatedAt": "2026-10-06T10:00:00Z",
                    "closedAt": "2026-10-06T10:00:00Z",
                    "closedByPullRequestsReferences": [],
                }
            else:
                self.assertEqual("44", command[3])
                snapshot = {
                    "number": 44, "mergedAt": "2026-10-05T10:00:00Z",
                    "updatedAt": "2026-10-05T10:00:00Z",
                }
            return SimpleNamespace(returncode=0, stdout=json.dumps(snapshot), stderr="")

        updated, failed = outcomes.reconcile_github(
            self.store, repo_id="owner/repo", repository="owner/repo", runner=runner
        )

        latest = self.store.latest_work_item_disposition(
            "owner/repo", "github", "owner/repo", "123"
        )
        self.assertEqual((1, 0), (updated, failed))
        self.assertEqual(["44"], latest["pull_request_ids"])
        self.assertEqual(["44"], latest["resolving_pr_ids"])

    def test_reconcile_scopes_cycles_and_toolkit_prs_to_the_selected_work_item_repository(self) -> None:
        self.store.record_stage("owner/code", "120", "implement",
                                record_kind="dispatch", cycle_id="legacy-cycle-120")
        self.add_scoped_cycle("owner/code", "120", "cycle-a", "owner/issues-a", "44")
        self.add_scoped_cycle("owner/code", "120", "cycle-b", "owner/issues-b", "45")
        runner = Mock(side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps({
                "number": 120, "state": "OPEN", "stateReason": None,
                "updatedAt": "2026-10-06T10:00:00Z",
                "closedByPullRequestsReferences": [],
            }), ""),
            subprocess.CompletedProcess([], 0, json.dumps({
                "number": 44, "mergedAt": None, "updatedAt": "2026-10-06T10:00:00Z",
            }), ""),
        ])

        updated, failed = outcomes.reconcile_github(
            self.store, repo_id="owner/code", repository="owner/issues-a", runner=runner
        )

        self.assertEqual((1, 0), (updated, failed))
        commands = [call.args[0] for call in runner.call_args_list]
        self.assertEqual("owner/issues-a", commands[0][5])
        self.assertEqual(["gh", "pr", "view", "44", "--repo", "owner/code",
                          "--json", "number,mergedAt,updatedAt"], commands[1])
        event = self.store.latest_work_item_disposition(
            "owner/code", "github", "owner/issues-a", "120"
        )
        self.assertEqual(["cycle-a"], event["cycle_ids"])
        self.assertEqual(["owner/code#44"], event["pull_request_ids"])

    def test_reconcile_uses_repository_from_provider_linked_pr_reference(self) -> None:
        self.add_scoped_cycle("owner/code", "120", "cycle-120", "owner/issues", "44")

        def runner(command, **_kwargs):
            if command[1] == "issue":
                self.assertEqual("owner/issues", command[5])
                snapshot = {
                    "number": 120, "state": "CLOSED", "stateReason": "COMPLETED",
                    "updatedAt": "2026-10-06T10:00:00Z",
                    "closedAt": "2026-10-06T10:00:00Z",
                    "closedByPullRequestsReferences": [{
                        "number": 46,
                        "repository": {"name": "linked-prs", "owner": {"login": "owner"}},
                    }],
                }
            else:
                if command[3] == "44":
                    self.assertEqual("owner/code", command[5])
                    snapshot = {"number": 44, "mergedAt": None,
                                "updatedAt": "2026-10-05T10:00:00Z"}
                else:
                    self.assertEqual("46", command[3])
                    self.assertEqual("owner/linked-prs", command[5])
                    snapshot = {
                        "number": 46, "mergedAt": "2026-10-05T10:00:00Z",
                        "updatedAt": "2026-10-05T10:00:00Z",
                    }
            return SimpleNamespace(returncode=0, stdout=json.dumps(snapshot), stderr="")

        updated, failed = outcomes.reconcile_github(
            self.store, repo_id="owner/code", repository="owner/issues", runner=runner
        )

        event = self.store.latest_work_item_disposition(
            "owner/code", "github", "owner/issues", "120"
        )
        self.assertEqual((1, 0), (updated, failed))
        self.assertEqual("resolved", event["outcome"])
        self.assertEqual(["owner/code#44", "owner/linked-prs#46"], event["pull_request_ids"])
        self.assertEqual(["owner/linked-prs#46"], event["merged_pr_ids"])
        self.assertEqual(["owner/code#44", "owner/linked-prs#46"], event["resolving_pr_ids"])

    def test_cli_records_explicit_observations_and_reconciles_through_provider_runner(self) -> None:
        self.add_cycle("123", "cycle-123")
        self.add_cycle("456", "cycle-456")
        script = ROOT / "scripts" / "work_item_outcomes.py"
        explicit = subprocess.run(
            [sys.executable, str(script), "--database", str(self.database), "record",
             "--repo-id", "owner/repo", "--provider", "jira", "--repository", "PROJECT",
             "--work-item-id", "PROJECT-77", "--outcome", "resolved",
             "--observed-at", "2026-10-01T10:00:00Z"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, explicit.returncode, explicit.stderr)
        self.assertEqual("explicit", json.loads(explicit.stdout)["event"]["source"])

        runner = Mock(side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps({
                "number": 123, "state": "CLOSED", "stateReason": "COMPLETED",
                "updatedAt": "2026-10-06T10:00:00Z",
                "closedByPullRequestsReferences": [{"number": 44}],
            }), ""),
            subprocess.CompletedProcess([], 0, json.dumps({
                "number": 44, "mergedAt": "2026-10-05T10:00:00Z",
                "updatedAt": "2026-10-05T10:00:00Z",
            }), ""),
            subprocess.CompletedProcess([], 1, "", "query failed"),
        ])
        reconcile = outcomes.reconcile_github

        def reconcile_from_cli(telemetry, *, repo_id: str, repository: str):
            return reconcile(telemetry, repo_id=repo_id, repository=repository, runner=runner)

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(outcomes, "reconcile_github", side_effect=reconcile_from_cli):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = outcomes.main([
                    "--database", str(self.database), "reconcile", "--repo-id", "owner/repo",
                    "--repository", "owner/repo",
                ])

        self.assertEqual(1, exit_code)
        self.assertEqual({"updated": 1, "failed": 1}, json.loads(stdout.getvalue()))
        self.assertIn("no observation recorded", stderr.getvalue())
        self.assertEqual(3, runner.call_count)
        self.assertEqual(["gh", "pr", "view", "44"], runner.call_args_list[1].args[0][:4])
        self.assertEqual("resolved", self.store.latest_work_item_disposition(
            "owner/repo", "github", "owner/repo", "123")["outcome"])
        self.assertEqual(["44"], self.store.latest_work_item_disposition(
            "owner/repo", "github", "owner/repo", "123")["merged_pr_ids"])
        self.assertIsNone(self.store.latest_work_item_disposition(
            "owner/repo", "github", "owner/repo", "456"))


if __name__ == "__main__":
    unittest.main()
