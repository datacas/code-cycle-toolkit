from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def test_an_unavailable_cost_estimate_does_not_prevent_fixed_routing(self) -> None:
        with patch.object(router, "estimate_cost", side_effect=router.RouterError("missing price")):
            decision = router.route("implement", signals(), READY)

        self.assertFalse(decision.blocked)
        self.assertEqual("codex", decision.target.executor)
        self.assertIsNone(decision.cost)
        self.assertEqual("unavailable", decision.cost_status)
        self.assertIn("cycle cost estimate unavailable", decision.explain())

    def test_an_invalid_measured_rate_only_disables_the_estimate(self) -> None:
        decision = router.route(
            "implement", signals(), READY,
            strategy=router.RoutingStrategy.MEASURED,
            first_pass_rate=1.1,
        )

        self.assertFalse(decision.blocked)
        self.assertEqual("codex", decision.target.executor)
        self.assertIsNone(decision.cost)
        self.assertEqual("unavailable", decision.cost_status)

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

    def test_review_profiles_use_the_executor_that_enforces_read_only(self) -> None:
        self.assertEqual("codex", router.load_profiles()["reviewer"].primary.executor)
        self.assertEqual("codex", router.load_profiles()["senior_reviewer"].primary.executor)

    def test_the_security_audit_always_uses_the_security_profile(self) -> None:
        d = router.route("security", signals(security_sensitive=False), READY)

        self.assertEqual("security", d.profile)
        self.assertEqual("codex", d.target.executor)

    def test_steps_judged_by_execution_stay_cheap(self) -> None:
        for role in ("verify", "run", "bootstrap"):
            with self.subTest(role=role):
                self.assertEqual("auxiliary_tool", router.route(role, signals(), READY).profile)
                self.assertEqual("codex", router.route(role, signals(), READY).target.executor)

    def test_coordination_stays_cheap_even_when_it_sequences_expensive_work(self) -> None:
        self.assertEqual("coordinator", router.route("coordinate", signals(), READY).profile)

    def test_resolving_findings_routes_where_implementing_does(self) -> None:
        """It edits code and is judged by tests, so it is implementation work."""
        self.assertEqual("cheap_coder", router.route("resolve", signals(difficulty=2), READY).profile)
        self.assertEqual("deep_coder", router.route("resolve", signals(difficulty=3), READY).profile)

    def test_a_rereview_routes_where_a_review_does(self) -> None:
        self.assertEqual("reviewer", router.route("rereview", signals(), READY).profile)
        self.assertEqual(
            "senior_reviewer",
            router.route("rereview", signals(security_sensitive=True), READY).profile,
        )

    def test_every_cycle_stage_can_be_routed(self) -> None:
        """A stage the router cannot route is a stage the cycle cannot run."""
        for role in ("implement", "review", "resolve", "rereview", "security",
                     "verify", "run", "bootstrap", "coordinate"):
            with self.subTest(role=role):
                self.assertFalse(router.route(role, signals(), READY).blocked)

    def test_an_unknown_role_is_refused(self) -> None:
        with self.assertRaises(router.RouterError):
            router.route("whatever", signals(), READY)


def choose(profile, reasons=("chosen by a test selector",)):
    """A selector that always names `profile`, recording what it was asked."""
    calls = []

    def selector(role, candidates, task_signals):
        calls.append((role, candidates, task_signals))
        return profile, reasons

    selector.calls = calls
    return selector


class ProfileSelectorTests(unittest.TestCase):
    """The selector names a profile; everything else stays in `route()`."""

    def test_every_candidate_is_a_known_profile(self) -> None:
        for role, candidates in router.ROLE_CANDIDATES.items():
            with self.subTest(role=role):
                self.assertTrue(candidates)
                self.assertLessEqual(set(candidates), set(router.DEFAULT_PROFILES))

    def test_the_rule_selector_is_the_default(self) -> None:
        for role in router.ROLE_CANDIDATES:
            for task in (signals(), signals(difficulty=3), signals(security_sensitive=True)):
                with self.subTest(role=role, signals=task):
                    self.assertEqual(
                        router.route(role, task, NO_CODEX),
                        router.route(role, task, NO_CODEX, selector=router.rule_selector),
                    )

    def test_the_rule_selector_stays_within_the_candidates(self) -> None:
        for role, candidates in router.ROLE_CANDIDATES.items():
            for difficulty in (1, 2, 3):
                for sensitive in (False, True):
                    task = signals(difficulty=difficulty, security_sensitive=sensitive)
                    with self.subTest(role=role, signals=task):
                        name, _ = router.rule_selector(role, candidates, task)
                        self.assertIn(name, candidates)

    def test_the_selector_receives_the_role_candidates_and_signals(self) -> None:
        selector = choose("deep_coder")
        task = signals(difficulty=1)

        decision = router.route("implement", task, READY, selector=selector)

        self.assertEqual("deep_coder", decision.profile)
        self.assertEqual(("implement", ("cheap_coder", "deep_coder"), task),
                         selector.calls[0])
        self.assertEqual(("chosen by a test selector",), decision.reasons)

    def test_a_selector_cannot_invent_a_profile_outside_the_candidates(self) -> None:
        for bad in ("senior_reviewer", "no_such_profile", None):
            with self.subTest(profile=bad):
                with self.assertRaises(router.RouterError) as refused:
                    router.route("implement", signals(), READY, selector=choose(bad))

                self.assertIn("outside the candidates", str(refused.exception))

    def test_a_selector_cannot_return_a_target_instead_of_a_profile(self) -> None:
        target = router.parse_target("claude:anthropic/claude-sonnet-5 high")

        with self.assertRaises(router.RouterError):
            router.route("implement", signals(), READY, selector=choose(target))

    def test_a_malformed_selector_result_is_refused(self) -> None:
        for bad in ("cheap_coder", ("cheap_coder",), ("cheap_coder", "why"),
                    ("cheap_coder", (3,))):
            with self.subTest(result=bad):
                with self.assertRaises(router.RouterError):
                    router.route("implement", signals(), READY,
                                 selector=lambda role, candidates, task: bad)

    def test_an_unknown_role_is_refused_before_the_selector_runs(self) -> None:
        selector = choose("cheap_coder")

        with self.assertRaises(router.RouterError):
            router.route("whatever", signals(), READY, selector=selector)

        self.assertEqual([], selector.calls)

    def test_the_availability_gate_still_blocks_the_selected_profile(self) -> None:
        d = router.route(
            "review", signals(), {"codex": router.Availability.QUOTA_EXHAUSTED},
            selector=choose("senior_reviewer"),
        )

        self.assertTrue(d.blocked)
        self.assertEqual("senior_reviewer", d.profile)
        self.assertIsNone(d.target)

    def test_the_workspace_policy_still_blocks_the_selected_profile(self) -> None:
        d = router.route(
            "implement", signals(), READY,
            eligible_executors=frozenset(), selector=choose("deep_coder"),
        )

        self.assertTrue(d.blocked)
        self.assertIn("no executor for this profile satisfies the workspace policy",
                      d.reasons)

    def test_fallback_and_calibration_rules_do_not_depend_on_the_selector(self) -> None:
        selector = choose("deep_coder")

        production = router.route("implement", signals(), NO_CODEX, selector=selector)
        calibration = router.route("implement", signals(), NO_CODEX, selector=selector,
                                   mode=router.RoutingMode.CALIBRATION)

        self.assertTrue(production.used_fallback)
        self.assertEqual("claude", production.target.executor)
        self.assertTrue(calibration.blocked)
        self.assertIn("blocks and waits", calibration.explain())

    def test_the_target_comes_from_the_configured_profile_not_the_selector(self) -> None:
        profiles = router.load_profiles({"code_cycle": {"profiles": {
            "deep_coder": {"primary": "claude:anthropic/claude-sonnet-5 high"},
        }}})

        d = router.route("implement", signals(), READY, profiles=profiles,
                         selector=choose("deep_coder"))

        self.assertEqual(profiles["deep_coder"].primary, d.target)

    def test_the_cost_estimate_prices_what_the_selector_chose(self) -> None:
        def deep(role, candidates, task):
            return candidates[-1], ()

        d = router.route("implement", signals(), READY, selector=deep)

        self.assertEqual(router.PROFILE_COST["deep_coder"], d.cost.implementation)
        self.assertEqual(router.PROFILE_COST["senior_reviewer"], d.cost.review)

    def test_a_selector_that_fails_only_for_pricing_does_not_block_routing(self) -> None:
        def only_security(role, candidates, task):
            if role != "security":
                raise router.RouterError("not priced")
            return "security", ()

        d = router.route("security", signals(), READY, selector=only_security)

        self.assertFalse(d.blocked)
        self.assertEqual("unavailable", d.cost_status)

    def test_profile_for_keeps_its_rule_based_answers(self) -> None:
        self.assertEqual("deep_coder", router.profile_for("resolve", signals(difficulty=3))[0])
        with self.assertRaises(router.RouterError):
            router.profile_for("whatever", signals())


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

    def test_security_cost_matches_its_codex_reviewer_target(self) -> None:
        self.assertEqual(router.PROFILE_COST["reviewer"], router.PROFILE_COST["security"])


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

    def test_policy_filtered_fallback_explains_the_policy_skip(self) -> None:
        profiles = router.load_profiles({"code_cycle": {"profiles": {
            "reviewer": {
                "primary": "claude:anthropic/claude-sonnet-5 high",
                "fallback": "codex:openai/gpt-5.6-terra high",
            },
        }}})
        decision = router.route(
            "review", signals(),
            {"claude": router.Availability.READY, "codex": router.Availability.READY},
            profiles=profiles, eligible_executors={"codex"},
        )

        self.assertTrue(decision.used_fallback)
        self.assertIn(
            "primary executor claude cannot satisfy the workspace policy -> fell back to codex",
            decision.reasons,
        )

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

    def test_effective_profiles_expose_declared_models(self) -> None:
        profiles = router.load_profiles({"code_cycle": {"profiles": {
            "cheap_coder": {
                "primary": "codex:openai/gpt-fictional-9 high",
            },
        }}})

        models = router.models_from_profiles(profiles)

        self.assertIn("gpt-fictional-9", models)
        self.assertIn("gpt-5.6-terra", models)



class DeclaredProfileShapeTests(unittest.TestCase):
    """A declaration is a mapping of target strings, refused here or never."""

    def load(self, profiles):
        return router.load_profiles({"code_cycle": {"profiles": profiles}})

    def test_a_profile_that_is_not_a_mapping_is_refused(self) -> None:
        with self.assertRaises(router.RouterError) as refused:
            self.load({"cheap_coder": "nope"})

        self.assertIn("must be a mapping of targets", str(refused.exception))

    def test_a_target_that_is_not_a_string_is_refused(self) -> None:
        """It used to be carried through: `primary: 3` became the integer 3,
        and only something trying to route with it ever found out."""
        for key, value in (("primary", 3), ("fallback", ["a", "b"]),
                           ("primary", {"model": "x"})):
            with self.subTest(key=key, value=value):
                with self.assertRaises(router.RouterError) as refused:
                    self.load({"cheap_coder": {"primary": "codex:openai/gpt-5.6-luna high",
                                               key: value}})

                self.assertIn("not a target string", str(refused.exception))

    def test_an_empty_target_string_is_refused(self) -> None:
        with self.assertRaises(router.RouterError):
            self.load({"cheap_coder": {"primary": "   "}})

    def test_a_malformed_target_string_is_still_refused(self) -> None:
        with self.assertRaises(router.RouterError) as refused:
            self.load({"cheap_coder": {"primary": "nonsense"}})

        self.assertIn("executor:provider/model effort", str(refused.exception))

    def test_a_well_formed_declaration_still_loads(self) -> None:
        profiles = self.load({"cheap_coder": {
            "primary": "claude:anthropic/claude-sonnet-5 high",
            "fallback": "codex:openai/gpt-5.6-luna high"}})

        self.assertEqual("claude-sonnet-5", profiles["cheap_coder"].primary.model)
        self.assertEqual("gpt-5.6-luna", profiles["cheap_coder"].fallback.model)

    def test_a_section_that_is_not_a_mapping_does_not_raise_its_own_error(self) -> None:
        """`code_cycle: not-a-mapping` reached `.get` on a string."""
        with self.assertRaises(router.RouterError):
            router.load_profiles({"code_cycle": "not-a-mapping"})


if __name__ == "__main__":
    unittest.main()
