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

    def test_aggregate_counts_dispatch_stages_once_and_excludes_other_row_kinds(self) -> None:
        cycle_id = "cycle-private"
        for role, seq, profile in (
            ("implement", 1, "cheap_coder"),
            ("implement", 1, "deep_coder"),  # rerouted attempt for the same stage
            ("review", 2, "senior_reviewer"),
        ):
            self.store.record_stage(
                "owner/repo", "task-private", role, profile=profile,
                cycle_id=cycle_id, stage_seq=seq, record_kind="dispatch",
                used_fallback=True, duration_ms=100, cost_usd=0.05,
            )
        self.store.record_stage(
            "owner/repo", "task-private", "implement", cycle_id=cycle_id,
            stage_seq=1, record_kind="shadow", jev_status="suggested",
        )
        self.store.record_stage(
            "owner/repo", "task-private", "review", cycle_id=cycle_id,
            stage_seq=2, record_kind="verdict", status="APPROVED",
        )
        self.store.record_stage(
            "owner/repo", "task-private", "coordinate", cycle_id=cycle_id,
            record_kind="cycle", first_pass_approved=True, fallback_stages=1,
        )

        rows = stats._read_rows(self.database, "owner/repo")
        report = stats.aggregate(rows, repo_id="owner/repo", now=self.as_of(), days=None)

        self.assertEqual(2, report["summary"]["stages"])
        self.assertEqual({"implement": 1, "review": 1}, report["summary"]["roles"])
        self.assertEqual({"deep_coder": 1}, report["profiles_by_role"]["implement"])
        self.assertEqual(1, report["summary"]["fallback_stages"])
        self.assertEqual(3, report["summary"]["duration_ms"]["measured"])
        self.assertAlmostEqual(0.15, report["summary"]["cost_usd"]["total"])
        self.assertEqual(2, sum(report["trend_daily"].values()))
        self.assertNotIn("coordinate", report["summary"]["roles"])
        self.assertIsNone(report["comparison"]["previous_first_pass"])

    def test_rows_before_record_kind_count_only_dispatches_as_stages(self) -> None:
        # Schema 2: no record_kind, but verdicts and the close were own rows.
        self.store.record_stage("owner/repo", "task-1", "implement", profile="cheap_coder",
                                outcome="succeeded", duration_ms=1000)
        self.store.record_stage("owner/repo", "task-1", "review", profile="senior_reviewer",
                                outcome="succeeded", duration_ms=500)
        self.store.record_stage("owner/repo", "task-1", "review", status="APPROVED")
        self.store.record_stage("owner/repo", "task-1", "coordinate",
                                status="READY_FOR_MANUAL_MERGE", iterations=0)

        report = stats.aggregate(stats._read_rows(self.database, "owner/repo"),
                                 repo_id="owner/repo", now=self.as_of(), days=None)

        self.assertEqual(2, report["summary"]["stages"])
        self.assertEqual({"implement": 1, "review": 1}, report["summary"]["roles"])
        self.assertEqual({"APPROVED": 1}, report["summary"]["verdicts"])
        self.assertEqual(2, report["summary"]["duration_ms"]["measured"])
        self.assertEqual(2, sum(report["trend_daily"].values()))

    def test_markdown_renders_breakdowns_as_tables_not_inline_json(self) -> None:
        for index in range(10):
            cycle_id = f"cycle-{index}"
            self.store.record_stage(
                "owner/repo", f"task-{index}", "implement", profile="cheap_coder",
                cycle_id=cycle_id, stage_seq=1, record_kind="dispatch",
                missing_capability="operating_quota" if index == 0 else None,
            )
            self.store.record_stage(
                "owner/repo", f"task-{index}", "implement", cycle_id=cycle_id,
                stage_seq=1, record_kind="shadow", jev_status="suggested",
                jev_suggested_profile="cheap_coder", jev_rule_profile="cheap_coder",
                jev_agreement=index % 3 != 0, jev_confidence=0.6,
            )
            self.store.record_stage(
                "owner/repo", f"task-{index}", "review", cycle_id=cycle_id,
                stage_seq=2, record_kind="verdict",
                status="APPROVED" if index < 8 else "CHANGES_REQUESTED",
                findings_high=1, findings_low=2,
            )
            self.store.record_stage(
                "owner/repo", f"task-{index}", "coordinate", cycle_id=cycle_id,
                record_kind="cycle", first_pass_approved=index < 8,
            )

        markdown = stats.render_markdown(stats.aggregate(
            stats._read_rows(self.database, "owner/repo"),
            repo_id="owner/repo", now=self.as_of()))

        self.assertNotIn("`{", markdown)
        self.assertIn("| APPROVED | 8 | ████████████ |", markdown)
        self.assertIn("| high | 10 | 10 |", markdown)
        self.assertIn("| critical | unknown | 0 | — |", markdown)
        self.assertIn("| operating_quota | 1 |", markdown)
        self.assertIn("| agree | 6 |", markdown)
        self.assertIn("| medium | 0.50–<0.75 | 10 |", markdown)
        self.assertIn("| cheap_coder | 8/10 (80%) |", markdown)

    def test_markdown_says_when_no_telemetry_exists_or_the_period_is_empty(self) -> None:
        empty = stats.render_markdown(stats.aggregate([], repo_id="owner/repo", now=self.as_of()))
        self.assertIn("No telemetry has been recorded for this repository yet", empty)
        self.assertIn("owner/repo", empty)

        self.add_task("OLD-TASK", "APPROVED")
        quiet = stats.render_markdown(stats.aggregate(
            stats._read_rows(self.database, "owner/repo"), repo_id="owner/repo",
            now=self.as_of() + timedelta(days=60)))
        self.assertIn("No telemetry was recorded in this period", quiet)

    def test_markdown_states_model_drift_as_mismatches_out_of_reported(self) -> None:
        for index, resolved in enumerate(("gpt-5.6-luna", "gpt-5.6-luna", "claude-sonnet-5")):
            self.store.record_stage("owner/repo", f"task-{index}", "implement",
                                    profile="cheap_coder", outcome="succeeded",
                                    model_requested="gpt-5.6-luna", model_resolved=resolved)
        self.store.record_stage("owner/repo", "task-9", "implement", profile="cheap_coder",
                                outcome="succeeded", model_requested="gpt-5.6-luna")

        markdown = stats.render_markdown(stats.aggregate(
            stats._read_rows(self.database, "owner/repo"), repo_id="owner/repo", now=self.as_of()))

        self.assertIn("**1 of 3** dispatches that reported their model ran a different one; "
                      "1 did not report a model", markdown)

    def test_activity_is_a_daily_sparkline_that_marks_idle_days(self) -> None:
        marks, unit = stats._sparkline({"2026-09-01": 4, "2026-09-03": 1}, "2026-09-01", "2026-09-04")
        self.assertEqual(("█·▂·", "day"), (marks, unit))

        long_marks, long_unit = stats._sparkline({"2026-07-01": 1}, "2026-07-01", "2026-09-01")
        self.assertEqual("week", long_unit)
        self.assertEqual(9, len(long_marks))  # 63 days

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
