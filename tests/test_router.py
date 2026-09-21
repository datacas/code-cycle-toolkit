from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import router  # noqa: E402

READY = {"codex": router.Availability.READY, "claude": router.Availability.READY}
NO_CODEX = {"codex": router.Availability.QUOTA_EXHAUSTED, "claude": router.Availability.READY}


def signals(**kw) -> router.TaskSignals:
    return router.TaskSignals(**kw)


class AvailabilityGateTests(unittest.TestCase):
    """Availability is decided before the model is, never after."""

    def test_a_ready_executor_gets_the_primary(self) -> None:
        d = router.route("implement", signals(), READY)

        self.assertFalse(d.blocked)
        self.assertEqual("cheap_coder", d.profile)
        self.assertEqual("codex", d.target.executor)

    def test_installed_is_not_dispatchable(self) -> None:
        """A binary on PATH proves no session, no repo access and no quota."""
        d = router.route(
            "implement", signals(),
            {"codex": router.Availability.INSTALLED, "claude": router.Availability.INSTALLED},
        )

        self.assertTrue(d.blocked)
        self.assertIn("installed", d.explain())

    def test_authenticated_is_not_dispatchable_either(self) -> None:
        d = router.route(
            "implement", signals(),
            {"codex": router.Availability.AUTHENTICATED, "claude": router.Availability.AUTHENTICATED},
        )

        self.assertTrue(d.blocked)

    def test_an_unreported_executor_is_unknown_not_assumed_ready(self) -> None:
        d = router.route("implement", signals(), {})

        self.assertTrue(d.blocked)
        self.assertIn("unknown", d.explain())

    def test_exhausted_quota_blocks_the_primary(self) -> None:
        d = router.route("implement", signals(), NO_CODEX)

        self.assertFalse(d.blocked)
        self.assertTrue(d.used_fallback)
        self.assertIn("quota_exhausted", d.explain())


class FallbackPolicyTests(unittest.TestCase):
    def test_production_falls_back(self) -> None:
        d = router.route("implement", signals(), NO_CODEX, mode=router.RoutingMode.PRODUCTION)

        self.assertFalse(d.blocked)
        self.assertEqual("claude", d.target.executor)

    def test_calibration_never_falls_back(self) -> None:
        """Substituting an arm answers a different question with the same sample."""
        d = router.route("implement", signals(), NO_CODEX, mode=router.RoutingMode.CALIBRATION)

        self.assertTrue(d.blocked)
        self.assertIsNone(d.target)
        self.assertIn("blocks and waits", d.explain())

    def test_the_fallback_reason_describes_the_primary_truthfully(self) -> None:
        """A routing decision is only auditable if its reasons are true.

        The message used to report the fallback's state as the primary's, so a
        run routed around an exhausted Codex while claiming Codex was ready.
        """
        d = router.route("implement", signals(), NO_CODEX)
        text = d.explain()

        self.assertIn("primary executor codex is quota_exhausted", text)
        self.assertNotIn("primary executor codex is ready", text)
        self.assertIn("fell back to claude", text)

    def test_every_reported_state_matches_the_executor_it_names(self) -> None:
        availability = {
            "codex": router.Availability.AUTHENTICATED,
            "claude": router.Availability.READY,
        }

        d = router.route("implement", signals(), availability)

        self.assertIn("codex is authenticated", d.explain())
        self.assertNotIn("codex is ready", d.explain())

    def test_a_profile_without_a_fallback_blocks_rather_than_improvising(self) -> None:
        d = router.route("review", signals(), {"claude": router.Availability.QUOTA_EXHAUSTED})

        self.assertTrue(d.blocked)


class RoleRoutingTests(unittest.TestCase):
    def test_difficulty_three_escalates_the_implementer(self) -> None:
        self.assertEqual("deep_coder", router.route("implement", signals(difficulty=3), READY).profile)
        self.assertEqual("cheap_coder", router.route("implement", signals(difficulty=2), READY).profile)
        self.assertEqual("cheap_coder", router.route("implement", signals(difficulty=1), READY).profile)

    def test_a_security_sensitive_change_gets_the_senior_reviewer(self) -> None:
        d = router.route("review", signals(security_sensitive=True), READY)

        self.assertEqual("senior_reviewer", d.profile)

    def test_the_security_audit_always_uses_the_security_profile(self) -> None:
        d = router.route("security", signals(security_sensitive=False), READY)

        self.assertEqual("security", d.profile)

    def test_steps_judged_by_execution_stay_cheap(self) -> None:
        for role in ("verify", "run", "bootstrap"):
            with self.subTest(role=role):
                self.assertEqual("cheap_tool", router.route(role, signals(), READY).profile)

    def test_coordination_stays_cheap_even_when_it_sequences_expensive_work(self) -> None:
        self.assertEqual("coordinator", router.route("coordinate", signals(), READY).profile)

    def test_an_unknown_role_is_refused(self) -> None:
        with self.assertRaises(router.RouterError):
            router.route("whatever", signals(), READY)


class TaskSignalTests(unittest.TestCase):
    def test_difficulty_outside_the_scale_is_refused(self) -> None:
        for bad in (0, 4, -1):
            with self.subTest(difficulty=bad):
                with self.assertRaises(router.RouterError):
                    signals(difficulty=bad)

    def test_an_unknown_verifiability_is_refused(self) -> None:
        with self.assertRaises(router.RouterError):
            signals(verifiability="maybe")


class CostModelTests(unittest.TestCase):
    """Five of five real implementations needed changes."""

    def test_the_estimate_includes_the_correction_round_by_default(self) -> None:
        cost = router.estimate_cost("cheap_coder", "reviewer")

        self.assertEqual(1.0, cost.implementation)
        self.assertEqual(4.0, cost.expected_resolutions)  # 1.0 * (1.0 + 3.0)
        self.assertEqual(3.0, cost.review)
        self.assertEqual(8.0, cost.total)

    def test_a_measured_first_pass_rate_lowers_it(self) -> None:
        cost = router.estimate_cost("cheap_coder", "reviewer", first_pass_rate=0.5)

        self.assertEqual(2.0, cost.expected_resolutions)
        self.assertEqual(6.0, cost.total)

    def test_the_cheap_profile_is_not_automatically_cheaper_end_to_end(self) -> None:
        """The whole point: first-pass price is not cycle price."""
        cheap = router.estimate_cost("cheap_coder", "senior_reviewer")
        deep = router.estimate_cost("deep_coder", "senior_reviewer", first_pass_rate=0.6)

        self.assertLess(deep.total, cheap.total)

    def test_an_impossible_rate_is_refused(self) -> None:
        for bad in (-0.1, 1.1):
            with self.subTest(rate=bad):
                with self.assertRaises(router.RouterError):
                    router.estimate_cost("cheap_coder", "reviewer", first_pass_rate=bad)

    def test_the_breakdown_is_readable(self) -> None:
        text = router.estimate_cost("cheap_coder", "reviewer").explain()

        self.assertIn("expected resolution work", text)
        self.assertIn("= 8.0", text)


class ProfileConfigTests(unittest.TestCase):
    def test_defaults_load_without_configuration(self) -> None:
        profiles = router.load_profiles()

        self.assertEqual(set(router.DEFAULT_PROFILES), set(profiles))
        self.assertEqual("codex", profiles["cheap_coder"].primary.executor)

    def test_a_repository_overrides_only_what_it_declares(self) -> None:
        profiles = router.load_profiles(
            {"code_cycle": {"profiles": {"cheap_coder": {"primary": "claude:anthropic/claude-sonnet-5 high"}}}}
        )

        self.assertEqual("claude", profiles["cheap_coder"].primary.executor)
        self.assertEqual("codex", profiles["coordinator"].primary.executor)

    def test_an_unknown_profile_name_is_refused_not_ignored(self) -> None:
        with self.assertRaises(router.RouterError):
            router.load_profiles({"code_cycle": {"profiles": {"cheep_coder": {"primary": "a:b/c d"}}}})

    def test_a_malformed_target_is_refused(self) -> None:
        for bad in ("claude/opus high", "claude:opus high", "claude:anthropic/opus"):
            with self.subTest(spec=bad):
                with self.assertRaises(router.RouterError):
                    router.parse_target(bad)

    def test_a_target_round_trips_readably(self) -> None:
        self.assertEqual(
            "codex:openai/gpt-5.6-luna high",
            str(router.parse_target("codex:openai/gpt-5.6-luna high")),
        )


if __name__ == "__main__":
    unittest.main()
