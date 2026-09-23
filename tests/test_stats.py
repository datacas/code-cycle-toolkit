"""Privacy-safe, sample-aware telemetry reports."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import stats  # noqa: E402
import telemetry as tm  # noqa: E402


class StatsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database = self.directory / "telemetry.sqlite"
        self.store = tm.Telemetry(self.database)

    @staticmethod
    def as_of() -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=1)

    def add_task(self, task: str, status: str, *, repo: str = "owner/repo") -> None:
        self.store.record_stage(repo, task, "implement", profile="cheap_coder",
                                duration_ms=1000, cost_usd=0.125)
        self.store.record_stage(repo, task, "review", profile="senior_reviewer",
                                status=status, findings_high=1, tests_passed=status == "APPROVED")

    def test_report_aggregates_rates_and_never_returns_task_ids(self) -> None:
        for index in range(10):
            self.add_task(f"PRIVATE-TASK-{index}", "APPROVED" if index < 7 else "CHANGES_REQUESTED")
        self.add_task("OTHER-REPO-TASK", "APPROVED", repo="other/repo")

        rows = stats._read_rows(self.database, "owner/repo")
        report = stats.aggregate(rows, repo_id="owner/repo", now=self.as_of())
        serialized = json.dumps(report)

        self.assertEqual(10, report["summary"]["tasks"])
        self.assertEqual({"passed": 7, "total": 10, "minimum": 10, "value": 0.7},
                         report["summary"]["first_pass"])
        self.assertEqual(10, report["summary"]["findings"]["high"]["count"])
        self.assertEqual(10, report["summary"]["duration_ms"]["measured"])
        self.assertEqual(1.25, report["summary"]["cost_usd"]["total"])
        self.assertEqual({"passed": 7, "failed": 3, "measured": 10, "not_reported": 0},
                         report["summary"]["verification"])
        self.assertNotIn("PRIVATE-TASK", serialized)
        self.assertNotIn("OTHER-REPO-TASK", serialized)

    def test_small_samples_remain_unknown_and_json_has_no_fake_zero_rate(self) -> None:
        for index in range(9):
            self.add_task(f"TASK-{index}", "APPROVED")

        report = stats.aggregate(stats._read_rows(self.database, "owner/repo"),
                                 repo_id="owner/repo", now=self.as_of())
        markdown = stats.render_markdown(report)

        self.assertIsNone(report["summary"]["first_pass"]["value"])
        self.assertIn("unknown (9/9; need 10)", markdown)
        self.assertIn("Not compared", markdown)

    def test_jev_summary_uses_correlated_observations_and_sample_gate(self) -> None:
        for index in range(10):
            cycle_id = f"cycle-{index}"
            approved = index < 6
            self.store.record_stage(
                "owner/repo", f"task-{index}", "implement", cycle_id=cycle_id,
                stage_seq=1, record_kind="shadow", jev_status="suggested",
                jev_suggested_profile="cheap_coder", jev_rule_profile="deep_coder",
                jev_agreement=index % 2 == 0, jev_confidence=0.8,
            )
            self.store.record_stage(
                "owner/repo", f"task-{index}", "coordinate", cycle_id=cycle_id,
                record_kind="cycle", first_pass_approved=approved,
            )

        report = stats.aggregate(stats._read_rows(self.database, "owner/repo"),
                                 repo_id="owner/repo", now=self.as_of())
        jev = report["jev"]

        self.assertEqual({"agree": 5, "disagree": 5}, jev["agreement"])
        self.assertEqual(10, jev["shadow_rows"])
        self.assertEqual({"suggested": 10}, jev["status"])
        self.assertEqual(10, jev["confidence_buckets"]["counts"]["high"])
        self.assertEqual(0.6, jev["first_pass_by_suggested_profile"]["cheap_coder"]["value"])

    def test_missing_database_is_not_created(self) -> None:
        absent = self.directory / "absent.sqlite"

        self.assertEqual([], stats._read_rows(absent, "owner/repo"))
        self.assertFalse(absent.exists())

    def test_repository_identity_is_required_without_echoing_local_paths(self) -> None:
        with self.assertRaises(stats.StatsError) as failure:
            stats._report_config(self.directory)

        self.assertIn("code_cycle.repository.selector", str(failure.exception))
        self.assertNotIn(str(self.directory), str(failure.exception))

    def test_markdown_reports_missing_cost_as_unmeasured(self) -> None:
        self.store.record_stage("owner/repo", "task-1", "implement")
        report = stats.aggregate(stats._read_rows(self.database, "owner/repo"),
                                 repo_id="owner/repo", now=self.as_of())

        self.assertIn("Cost: not measured", stats.render_markdown(report))
        self.assertIsNone(report["summary"]["findings"]["critical"]["count"])
        self.assertIsNone(report["summary"]["verification"]["passed"])
        self.assertIn("Test verification: not reported", stats.render_markdown(report))


if __name__ == "__main__":
    unittest.main()
