from __future__ import annotations

import json
import sys
import tempfile
import unittest
import unittest.mock
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

    def test_allowed_payload_fields_are_kept(self) -> None:
        self.store.record_stage("repo", "t1", "implement",
                                profile="cheap_coder", tokens_in=1234, findings_high=2)

        row = self.store.rows("repo")[0]

        self.assertEqual("cheap_coder", row["profile"])
        self.assertEqual(1234, row["payload"]["tokens_in"])
        self.assertEqual(2, row["payload"]["findings_high"])

    def test_a_credential_cannot_be_persisted(self) -> None:
        """The no-secrets promise is enforced, not asserted in a docstring."""
        with self.assertRaises(tm.TelemetryError):
            self.store.record_stage("repo", "t1", "implement", credential="TOP-SECRET")

        self.assertEqual([], self.store.rows())

    def test_prose_cannot_be_persisted(self) -> None:
        for field in ("transcript", "prompt", "summary", "diff", "reason"):
            with self.subTest(field=field):
                with self.assertRaises(tm.TelemetryError):
                    self.store.record_stage("repo", "t1", "implement", **{field: "some prose"})

    def test_an_unknown_field_is_refused_by_name_not_dropped(self) -> None:
        """Dropping silently would lose an observation the caller believed it
        had recorded; refusing says which field and where to add it."""
        with self.assertRaises(tm.TelemetryError) as raised:
            self.store.record_stage("repo", "t1", "implement", whatever=1)

        self.assertIn("whatever", str(raised.exception))
        self.assertIn("FIELD_SPECS", str(raised.exception))

    def test_a_short_secret_in_a_numeric_field_is_refused(self) -> None:
        """A string is not safe because it is short: TOP-SECRET is ten characters."""
        with self.assertRaises(tm.TelemetryError):
            self.store.record_stage("repo", "t1", "implement", tokens_in="TOP-SECRET")

        self.assertEqual([], self.store.rows())

    def test_a_container_under_an_allowed_key_is_refused(self) -> None:
        """An approved key does not make its contents approved; prose just
        moves one level down."""
        for value in ({"transcript": "private prose"}, ["prose"], ("prose",), b"prose"):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(tm.TelemetryError):
                    self.store.record_stage("repo", "t1", "implement", cost_usd=value)

    def test_a_token_field_is_a_closed_vocabulary_not_short_text(self) -> None:
        with self.assertRaises(tm.TelemetryError):
            self.store.record_stage("repo", "t1", "implement", security_gate_half="secret")

        self.store.record_stage("repo", "t1", "implement", security_gate_half="deterministic")
        self.assertEqual("deterministic", self.store.rows()[0]["payload"]["security_gate_half"])

    def test_each_kind_accepts_only_its_own_shape(self) -> None:
        bad = {"findings_high": 1.5, "cost_usd": "0.10", "security_audit_ran": 1,
               "iterations": True}
        for key, value in bad.items():
            with self.subTest(key=key):
                with self.assertRaises(tm.TelemetryError):
                    self.store.record_stage("repo", "t1", "implement", **{key: value})

    def test_the_well_formed_values_are_kept(self) -> None:
        self.store.record_stage("repo", "t1", "implement", tokens_in=1234, cost_usd=0.1,
                                security_audit_ran=True, security_gate_half="both")

        payload = self.store.rows()[0]["payload"]

        self.assertEqual(1234, payload["tokens_in"])
        self.assertEqual(0.1, payload["cost_usd"])
        self.assertIs(True, payload["security_audit_ran"])
        self.assertEqual("both", payload["security_gate_half"])

    def test_none_is_allowed_as_an_absent_measurement(self) -> None:
        self.store.record_stage("repo", "t1", "implement", cost_usd=None)

        self.assertIsNone(self.store.rows()[0]["payload"]["cost_usd"])

    def test_routing_reasons_are_reduced_to_a_count(self) -> None:
        """The reasons are prose and belong in the published comment."""
        target = router.parse_target("codex:openai/gpt-5.6-luna high")
        decision = router.RoutingDecision(
            "cheap_coder", target, router.RoutingMode.PRODUCTION,
            reasons=("difficulty below 3", "codex is ready"),
        )
        result = ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, "codex", target)

        self.store.record_dispatch("repo", "t1", "implement", decision, result)
        payload = self.store.rows()[0]["payload"]

        self.assertEqual(2, payload["routing_reason_count"])
        self.assertNotIn("routing_reasons", payload)
        self.assertNotIn("difficulty below 3", json.dumps(payload))

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


class ColumnBoundaryTests(TelemetryTestCase):
    """A promoted column is not safer than a payload key; it is another door.

    This boundary was breached three times, and every time the fix covered the
    door that had just been pointed out.
    """

    def test_a_secret_cannot_arrive_through_a_promoted_column(self) -> None:
        with self.assertRaises(tm.TelemetryError):
            self.store.record_stage("repo", "task", "review", status="TOP-SECRET")

        self.assertEqual([], self.store.rows())

    def test_prose_cannot_arrive_through_a_promoted_column(self) -> None:
        with self.assertRaises(tm.TelemetryError):
            self.store.record_stage("repo", "task", "review", skill="private prose here")

    def test_a_key_shaped_value_cannot_arrive_as_a_profile(self) -> None:
        with self.assertRaises(tm.TelemetryError):
            self.store.record_stage("repo", "task", "review", profile="sk-live-abc123")

    def test_an_over_long_value_cannot_arrive_through_a_column(self) -> None:
        with self.assertRaises(tm.TelemetryError):
            self.store.record_stage("repo", "task", "review", effort="y" * 300)

    def test_an_identifier_with_whitespace_is_refused(self) -> None:
        """A reference has no spaces; prose does."""
        with self.assertRaises(tm.TelemetryError):
            self.store.record_stage("repo", "my secret token here", "review")

    def test_an_over_long_identifier_is_refused(self) -> None:
        with self.assertRaises(tm.TelemetryError):
            self.store.record_stage("repo", "t" * 300, "review")

    def test_every_column_is_covered_by_the_same_table(self) -> None:
        """No column may be exempt, which is how the last three breaches happened."""
        uncovered = sorted(set(tm._COLUMNS) - set(tm.FIELD_SPECS))

        self.assertEqual([], uncovered)

    def test_well_formed_values_still_pass(self) -> None:
        self.store.record_stage("repo", "task-1", "review", status="APPROVED",
                                profile="senior_reviewer", skill="cc-initial-review",
                                executor="claude", provider="anthropic", effort="high",
                                model_requested="claude-opus-5")

        row = self.store.rows()[0]

        self.assertEqual("APPROVED", row["status"])
        self.assertEqual("claude-opus-5", row["model_requested"])


class IdentifierBoundaryTests(TelemetryTestCase):
    """A model name and a credential have the same shape."""

    def test_a_credential_prefix_is_refused_in_any_identifier(self) -> None:
        for value in ("sk-live-abc123", "ghp_abc123", "AKIAIOSFODNN7", "xoxb-1-2",
                      "glpat-abc", "-----BEGIN RSA"):
            with self.subTest(value=value):
                with self.assertRaises(tm.TelemetryError):
                    self.store.record_stage(value, "task", "implement")

    def test_a_credential_shaped_model_is_refused(self) -> None:
        with self.assertRaises(tm.TelemetryError):
            self.store.record_stage("repo", "task", "implement",
                                    model_requested="sk-live-abc123")

        self.assertEqual([], self.store.rows())

    def test_a_model_must_be_one_this_toolkit_knows(self) -> None:
        """The closed set is the only real guarantee: no grammar separates
        gpt-5.6-luna from sk-live-abc123."""
        with self.assertRaises(tm.TelemetryError):
            self.store.record_stage("repo", "task", "implement",
                                    model_requested="some-unknown-model")

    def test_the_known_models_come_from_the_router_profiles(self) -> None:
        self.assertIn("gpt-5.6-luna", tm.known_models())
        self.assertIn("claude-opus-5", tm.known_models())

    def test_the_model_check_survives_a_package_qualified_import(self) -> None:
        """A guarantee that depends on import topology is not a guarantee."""
        import importlib, subprocess, sys

        script = (
            "import sys, tempfile; sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from scripts.telemetry import Telemetry, TelemetryError\n"
            "with tempfile.TemporaryDirectory() as d:\n"
            "    t = Telemetry(Path(d)/'t.sqlite')\n"
            "    try:\n"
            "        t.record_stage('repo','task','implement', model_requested='some-unknown-model')\n"
            "        print('ACCEPTED')\n"
            "    except TelemetryError:\n"
            "        print('REFUSED')\n"
        ) % str(ROOT)

        out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                             text=True, cwd=str(ROOT))

        self.assertEqual("REFUSED", out.stdout.strip(), out.stderr)

    def test_an_unavailable_model_set_refuses_rather_than_waves_through(self) -> None:
        """A check that switches itself off when it cannot run is not a check."""
        original = tm._KNOWN_MODELS_CACHE
        broken = [lambda: (_ for _ in ()).throw(RuntimeError("no router"))]
        self.addCleanup(setattr, tm, "_KNOWN_MODELS_CACHE", original)
        tm._KNOWN_MODELS_CACHE = None

        with unittest.mock.patch.object(tm, "_router_flat", broken[0]), \
             unittest.mock.patch.object(tm, "_router_packaged", broken[0]):
            with self.assertRaises(tm.TelemetryError):
                self.store.record_stage("repo", "task", "implement",
                                        model_requested="gpt-5.6-luna")

    def test_a_reference_grammar_rejects_what_a_selector_never_contains(self) -> None:
        for value in ("has space", "tab\there", "new\nline", "quote'inside", "<angle>"):
            with self.subTest(value=value):
                with self.assertRaises(tm.TelemetryError):
                    self.store.record_stage(value, "task", "implement")

    def test_real_selectors_still_pass(self) -> None:
        for repo, task in (("owner/api", "API-055"),
                           ("org-name/service", "156"),
                           ("workspace/repo", "ENG-123")):
            with self.subTest(repo=repo):
                self.store.record_stage(repo, task, "implement",
                                        model_requested="gpt-5.6-luna")

        self.assertEqual(3, len(self.store.rows()))


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

    def test_a_review_without_a_verdict_is_not_a_failed_first_pass(self) -> None:
        """An append-only API makes recording before the verdict ordinary, and
        counting that as a failure would poison the cost model."""
        for i in range(10):
            self.store.record_stage("repo", f"t{i}", "implement", profile="cheap_coder")
            self.store.record_stage("repo", f"t{i}", "review")           # no status yet
            self.store.record_stage("repo", f"t{i}", "review", iteration=1, status="APPROVED")

        rate = self.store.first_pass_rate("repo")

        self.assertEqual(1.0, rate.value)
        self.assertEqual(10, rate.observations)

    def test_a_pending_review_leaves_the_rate_unknown(self) -> None:
        for i in range(10):
            self.store.record_stage("repo", f"t{i}", "implement", profile="cheap_coder")
            self.store.record_stage("repo", f"t{i}", "review", status="IN_PROGRESS")

        rate = self.store.first_pass_rate("repo")

        self.assertFalse(rate.known)
        self.assertEqual(0, rate.observations)

    def test_an_unrecognised_status_cannot_be_recorded_at_all(self) -> None:
        """With a closed vocabulary the question moves earlier: an invented
        status is refused at write time rather than mis-counted at read time."""
        with self.assertRaises(tm.TelemetryError):
            self.store.record_stage("repo", "t1", "review", status="SOMETHING_ELSE")

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
        self.assertEqual(1, row["payload"]["routing_reason_count"])

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
                                model_requested="gpt-5.6-luna", model_resolved="claude-sonnet-5")

        drift = self.store.model_drift("repo")

        self.assertEqual(1, len(drift))
        self.assertEqual("claude-sonnet-5", drift[0]["resolved"])

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
