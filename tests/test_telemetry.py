from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import executors as ex  # noqa: E402
import router  # noqa: E402
import telemetry as tm  # noqa: E402


class TelemetryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.store = tm.Telemetry(self.dir / "telemetry.sqlite")

    def cycle(self, task, implementer="cheap_coder", review_status="CHANGES_REQUESTED"):
        self.store.record_stage("repo", task, "implement", profile=implementer,
                                status="IMPLEMENTED")
        self.store.record_stage("repo", task, "review", profile="senior_reviewer",
                                status=review_status)


class StorageTests(TelemetryTestCase):
    def test_it_lives_outside_any_repository(self) -> None:
        self.assertFalse(str(tm.default_database_path()).startswith(str(ROOT)))

    def test_a_stage_needs_its_identifiers(self) -> None:
        for args in (("", "t", "implement"), ("r", "", "implement"), ("r", "t", "")):
            with self.subTest(args=args):
                with self.assertRaises(tm.TelemetryError):
                    self.store.record_stage(*args)

    def test_unknown_fields_are_kept_rather_than_dropped(self) -> None:
        """A schema that rejects an unpromoted field makes callers drop data."""
        self.store.record_stage("repo", "t1", "implement",
                                profile="cheap_coder", tokens_in=1234, whatever={"a": 1})

        row = self.store.rows("repo")[0]

        self.assertEqual("cheap_coder", row["profile"])
        self.assertEqual(1234, row["payload"]["tokens_in"])
        self.assertEqual({"a": 1}, row["payload"]["whatever"])

    def test_every_row_carries_its_schema_version(self) -> None:
        self.store.record_stage("repo", "t1", "implement")

        self.assertEqual(tm.SCHEMA_VERSION, self.store.rows()[0]["schema_version"])

    def test_it_reopens_without_losing_rows(self) -> None:
        self.store.record_stage("repo", "t1", "implement")

        reopened = tm.Telemetry(self.dir / "telemetry.sqlite")

        self.assertEqual(1, len(reopened.rows()))

    def test_rows_filter_by_repository_and_task(self) -> None:
        self.store.record_stage("a", "t1", "implement")
        self.store.record_stage("b", "t2", "implement")

        self.assertEqual(1, len(self.store.rows("a")))
        self.assertEqual(1, len(self.store.rows(task_id="t2")))
        self.assertEqual(2, len(self.store.rows()))


class FirstPassRateTests(TelemetryTestCase):
    """The number router.estimate_cost currently guesses at 0.0."""

    def test_too_few_observations_is_not_a_rate(self) -> None:
        for i in range(3):
            self.cycle(f"t{i}", review_status="APPROVED")

        rate = self.store.first_pass_rate("repo")

        self.assertFalse(rate.known)
        self.assertIsNone(rate.value)
        self.assertEqual(3, rate.observations)
        self.assertIn("fewer than", rate.explain())

    def test_unknown_and_zero_do_not_look_alike(self) -> None:
        """A silent hardening from three runs into a routing constant is worse
        than the honest constant it replaces."""
        few = self.store.first_pass_rate("repo")
        for i in range(10):
            self.cycle(f"t{i}", review_status="CHANGES_REQUESTED")
        measured = self.store.first_pass_rate("repo")

        self.assertIsNone(few.value)
        self.assertEqual(0.0, measured.value)
        self.assertTrue(measured.known)

    def test_a_measured_rate_counts_first_reviews(self) -> None:
        for i in range(6):
            self.cycle(f"t{i}", review_status="APPROVED")
        for i in range(6, 10):
            self.cycle(f"t{i}", review_status="CHANGES_REQUESTED")

        rate = self.store.first_pass_rate("repo")

        self.assertEqual(0.6, rate.value)
        self.assertEqual(10, rate.observations)

    def test_only_the_first_review_of_a_task_counts(self) -> None:
        for i in range(10):
            self.cycle(f"t{i}", review_status="CHANGES_REQUESTED")
        # later iterations approving does not turn a first failure into a pass
        for i in range(10):
            self.store.record_stage("repo", f"t{i}", "review", iteration=1,
                                    profile="senior_reviewer", status="APPROVED")

        self.assertEqual(0.0, self.store.first_pass_rate("repo").value)

    def test_an_unreviewed_implementation_is_not_counted(self) -> None:
        """Nobody reviewed it, so it is not evidence that it would have passed."""
        for i in range(10):
            self.cycle(f"t{i}", review_status="APPROVED")
        self.store.record_stage("repo", "unreviewed", "implement", profile="cheap_coder")

        self.assertEqual(10, self.store.first_pass_rate("repo").observations)

    def test_a_review_without_an_implementation_is_not_counted_either(self) -> None:
        for i in range(10):
            self.cycle(f"t{i}", review_status="APPROVED")
        self.store.record_stage("repo", "orphan", "review", status="APPROVED")

        self.assertEqual(10, self.store.first_pass_rate("repo").observations)

    def test_the_filter_names_the_implementer_not_the_reviewer(self) -> None:
        for i in range(10):
            self.cycle(f"cheap{i}", implementer="cheap_coder", review_status="CHANGES_REQUESTED")
        for i in range(10):
            self.cycle(f"deep{i}", implementer="deep_coder", review_status="APPROVED")

        self.assertEqual(0.0, self.store.first_pass_rate("repo", "cheap_coder").value)
        self.assertEqual(1.0, self.store.first_pass_rate("repo", "deep_coder").value)

    def test_repositories_are_measured_separately(self) -> None:
        for i in range(10):
            self.store.record_stage("a", f"t{i}", "implement", profile="cheap_coder")
            self.store.record_stage("a", f"t{i}", "review", status="APPROVED")
            self.store.record_stage("b", f"t{i}", "implement", profile="cheap_coder")
            self.store.record_stage("b", f"t{i}", "review", status="CHANGES_REQUESTED")

        self.assertEqual(1.0, self.store.first_pass_rate("a").value)
        self.assertEqual(0.0, self.store.first_pass_rate("b").value)

    def test_it_feeds_the_router_cost_model(self) -> None:
        for i in range(6):
            self.cycle(f"t{i}", review_status="APPROVED")
        for i in range(6, 10):
            self.cycle(f"t{i}", review_status="CHANGES_REQUESTED")
        rate = self.store.first_pass_rate("repo")

        measured = router.estimate_cost("cheap_coder", "senior_reviewer",
                                        first_pass_rate=rate.value)
        assumed = router.estimate_cost("cheap_coder", "senior_reviewer")

        self.assertLess(measured.total, assumed.total)


class DispatchRecordingTests(TelemetryTestCase):
    def decision_and_result(self, blocked_capability=None, used_fallback=False):
        target = router.parse_target("codex:openai/gpt-5.6-luna high")
        decision = router.RoutingDecision(
            "cheap_coder", target, router.RoutingMode.PRODUCTION,
            used_fallback=used_fallback, reasons=("difficulty below 3",),
        )
        if blocked_capability:
            result = ex.DispatchResult(
                ex.DispatchOutcome.BLOCKED, "codex", target,
                missing_capability=blocked_capability,
                readiness_policy=ex.ReadinessPolicy.ATTEMPT,
                dispatched_from=ex.Availability.AUTHENTICATED,
            )
        else:
            result = ex.DispatchResult(
                ex.DispatchOutcome.SUCCEEDED, "codex", target,
                model_resolved="gpt-5.6-luna",
                readiness_policy=ex.ReadinessPolicy.ATTEMPT,
                dispatched_from=ex.Availability.AUTHENTICATED,
            )
        return decision, result

    def test_a_dispatch_is_recorded_from_the_objects_not_by_hand(self) -> None:
        decision, result = self.decision_and_result()

        self.store.record_dispatch("repo", "t1", "implement", decision, result)
        row = self.store.rows()[0]

        self.assertEqual("cheap_coder", row["profile"])
        self.assertEqual("codex", row["executor"])
        self.assertEqual("gpt-5.6-luna", row["model_requested"])
        self.assertEqual("gpt-5.6-luna", row["model_resolved"])
        self.assertEqual("succeeded", row["outcome"])
        self.assertEqual("attempt", row["readiness_policy"])
        self.assertEqual("authenticated", row["dispatched_from"])
        self.assertEqual(["difficulty below 3"], row["payload"]["routing_reasons"])

    def test_a_fallback_is_visible_in_the_row(self) -> None:
        decision, result = self.decision_and_result(used_fallback=True)

        self.store.record_dispatch("repo", "t1", "implement", decision, result)

        self.assertEqual(1, self.store.rows()[0]["used_fallback"])

    def test_blocked_capabilities_are_counted_by_name(self) -> None:
        """One exhausted window read as evidence about a model cost a campaign."""
        for capability in ("operating_quota", "operating_quota", "folder_trust"):
            decision, result = self.decision_and_result(blocked_capability=capability)
            self.store.record_dispatch("repo", f"t-{capability}-{id(result)}",
                                       "implement", decision, result)

        self.assertEqual({"operating_quota": 2, "folder_trust": 1},
                         self.store.dispatch_failures("repo"))


class ModelDriftTests(TelemetryTestCase):
    def test_a_different_model_is_reported(self) -> None:
        self.store.record_stage("repo", "t1", "implement",
                                model_requested="gpt-5.6-luna", model_resolved="gpt-5.6-terra")

        drift = self.store.model_drift("repo")

        self.assertEqual(1, len(drift))
        self.assertEqual("gpt-5.6-terra", drift[0]["resolved"])

    def test_a_match_is_not_drift(self) -> None:
        self.store.record_stage("repo", "t1", "implement",
                                model_requested="gpt-5.6-luna", model_resolved="gpt-5.6-luna")

        self.assertEqual([], self.store.model_drift("repo"))

    def test_silence_is_not_counted_as_agreement(self) -> None:
        """Codex reports no model at all; calling that a match hides the point."""
        self.store.record_stage("repo", "t1", "implement",
                                model_requested="gpt-5.6-luna", model_resolved=None)

        self.assertEqual([], self.store.model_drift("repo"))


class SummaryTests(TelemetryTestCase):
    def test_the_summary_says_what_is_not_known_yet(self) -> None:
        self.cycle("t1", review_status="APPROVED")

        summary = self.store.summary("repo")

        self.assertEqual(2, summary["stages"])
        self.assertEqual(1, summary["tasks"])
        self.assertIn("unknown", summary["first_pass_rate"])


if __name__ == "__main__":
    unittest.main()
