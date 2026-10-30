from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import execution_plan as plan  # noqa: E402
import execution_profiles as profiles  # noqa: E402
import run_cycle  # noqa: E402

Res = plan.Resolution


def resolved(config=None, **invocation):
    return profiles.resolve_execution(
        profiles.repository_settings(config or {}),
        profiles.invocation_settings(**invocation),
    )


def build(config=None, *, signals=None, start_from="implement", continue_work=False,
          change_request=None, **invocation):
    return profiles.build_plan(
        resolved(config, **invocation), repository="o/r", work_item="328",
        signals=signals, start_from=start_from, continue_work=continue_work,
        change_request=change_request,
    )


def resolutions(built):
    return {stage.stage_id: (stage.resolution, stage.selected, stage.obligation)
            for stage in built.stages}


REQ, OPT = plan.Obligation.REQUIRED, plan.Obligation.OPTIONAL


class ProfileMatrixTests(unittest.TestCase):
    def test_no_configuration_records_the_compatible_standard_flow(self) -> None:
        built = build()
        self.assertEqual(built.execution_profile, "standard")
        self.assertEqual(built.objective, "merge_ready")
        self.assertEqual(built.effective_configuration["execution_profile"]["source"],
                         "built-in default")
        self.assertEqual(resolutions(built), {
            "issue_review": (Res.RESOLVED, True, OPT),
            "implement": (Res.RESOLVED, True, REQ),
            "verify": (Res.RESOLVED, True, REQ),
            "review": (Res.RESOLVED, True, REQ),
            "security": (Res.PENDING, False, OPT),
            "resolve": (Res.PENDING, False, OPT),
            "rereview": (Res.PENDING, False, OPT),
        })
        self.assertEqual(built.stage("verify").embedded_in, "implement")
        self.assertEqual(built.stage("security").embedded_in, "review")
        self.assertEqual(built.control("ci").requirement, plan.ControlRequirement.REQUIRED)
        self.assertEqual(built.control("ci").timing, "strict")
        merge = built.control("merge")
        self.assertEqual(merge.requirement, plan.ControlRequirement.REQUIRED)
        self.assertFalse(merge.automatic)

    def test_each_profile_resolves_with_its_default_objective(self) -> None:
        expected_objective = {"prototype": "local_change", "standard": "merge_ready",
                              "full": "merge_ready", "hotfix": "merge_ready",
                              "custom": "merge_ready"}
        for name in profiles.PROFILES:
            with self.subTest(profile=name):
                built = build(execution_profile=name)
                self.assertEqual(built.execution_profile, name)
                self.assertEqual(built.objective, expected_objective[name])
                self.assertEqual(built.effective_policy["profile_intent"],
                                 profiles.PROFILE_INTENT[name])
                plan.ExecutionPlan.from_dict(built.to_dict())

    def test_prototype_is_local_output_without_review_ci_or_merge(self) -> None:
        built = build(execution_profile="prototype")
        for stage in ("review", "security", "resolve", "rereview"):
            self.assertIs(built.stage(stage).resolution, Res.NOT_APPLICABLE, stage)
        self.assertIs(built.stage("implement").obligation, REQ)
        for control in built.controls:
            self.assertIs(control.resolution, Res.NOT_APPLICABLE)
            self.assertIs(control.requirement, plan.ControlRequirement.NOT_REQUIRED)
        self.assertEqual(built.decisions, ())

    def test_publishing_a_prototype_activates_review_and_ci(self) -> None:
        built = build(execution_profile="prototype", objective="merge_ready")
        review = built.stage("review")
        self.assertTrue(review.selected)
        self.assertIs(review.obligation, REQ)
        self.assertEqual(review.policy_source, "objective merge_ready")
        overridden = built.effective_configuration["stages"]["review"]["overridden"]
        self.assertEqual(overridden[-1]["value"], "disabled")
        self.assertIs(built.control("ci").requirement, plan.ControlRequirement.REQUIRED)

    def test_full_requires_issue_review_and_security_when_it_applies(self) -> None:
        built = build(execution_profile="full")
        self.assertIs(built.stage("issue_review").obligation, REQ)
        decision = next(item for item in built.decisions if item.subject == "security")
        self.assertTrue(decision.required)
        self.assertEqual(decision.awaits, "diff")

    def test_hotfix_keeps_review_and_security(self) -> None:
        built = build(execution_profile="hotfix")
        self.assertIs(built.stage("review").obligation, REQ)
        self.assertIs(built.stage("security").resolution, Res.PENDING)

    def test_verified_change_makes_ci_optional_and_merge_not_required(self) -> None:
        built = build(objective="verified_change")
        self.assertIs(built.control("ci").requirement, plan.ControlRequirement.OPTIONAL)
        self.assertIs(built.control("merge").requirement, plan.ControlRequirement.NOT_REQUIRED)
        self.assertIs(built.stage("review").obligation, REQ)


class PolicySemanticsTests(unittest.TestCase):
    def test_disabled_auto_enabled_required_for_issue_review(self) -> None:
        trivial = profiles.PlanSignals(difficulty=1)
        cases = {
            "disabled": (Res.DISABLED, False, OPT),
            "auto": (Res.RESOLVED, False, OPT),
            "enabled": (Res.RESOLVED, True, OPT),
            "required": (Res.RESOLVED, True, REQ),
        }
        for policy, expected in cases.items():
            with self.subTest(policy=policy):
                built = build(signals=trivial, execution_profile="custom",
                              stages=[f"issue_review={policy}"])
                self.assertEqual(resolutions(built)["issue_review"], expected)

    def test_unknown_risk_is_not_read_as_no_risk(self) -> None:
        built = build()
        self.assertIs(built.stage("security").resolution, Res.PENDING)
        self.assertIs(built.obligations[0].state, plan.ObligationState.PENDING)
        # An undeclared difficulty is reviewed, not skipped.
        self.assertTrue(built.stage("issue_review").selected)

    def test_declared_security_sensitivity_requires_the_audit(self) -> None:
        built = build(signals=profiles.PlanSignals(security_sensitive=True))
        security = built.stage("security")
        self.assertTrue(security.selected)
        self.assertIs(security.obligation, REQ)
        self.assertEqual(security.policy_source, "--security-sensitive")
        with self.assertRaisesRegex(profiles.ExecutionConfigError, "security-sensitive"):
            build(signals=profiles.PlanSignals(security_sensitive=True),
                  stages=["security=disabled"])

    def test_disabled_security_keeps_its_conditional_obligation(self) -> None:
        built = build(stages=["security=disabled"])
        self.assertIs(built.stage("security").resolution, Res.DISABLED)
        obligation = next(item for item in built.obligations
                          if item.obligation_id == "security_policy")
        self.assertIs(obligation.state, plan.ObligationState.PENDING)
        with self.assertRaisesRegex(plan.ContractError, "cannot remove an obligation"):
            built.settle_obligation("security_policy", True, "auth/ changed")

    def test_conditions_that_need_the_diff_stay_pending(self) -> None:
        built = build()
        self.assertEqual({item.subject: item.awaits for item in built.decisions},
                         {"security": "diff", "resolve": "review_outcome",
                          "rereview": "post_review_changes"})
        for subject in ("security", "resolve", "rereview"):
            with self.assertRaisesRegex(plan.ContractError, "pending"):
                built.check_executable(subject)
        built.check_executable("implement")

    def test_execution_profiles_leave_model_profiles_untouched(self) -> None:
        config = {"code_cycle": {
            "profiles": {"cheap_coder": {"primary": "codex:openai/gpt-6-luna high"}},
            "execution": {"profile": "full"},
        }}
        before = copy.deepcopy(config)
        built = build(config)
        self.assertEqual(config, before)
        self.assertNotIn("profiles", built.effective_configuration)
        self.assertNotIn("cheap_coder", json.dumps(built.to_dict()))


class PrecedenceTests(unittest.TestCase):
    def test_conflicting_explicit_objectives_across_layers_are_rejected(self) -> None:
        config = {"code_cycle": {"execution": {"objective": "merge_ready"}}}
        for invocation in ({"local_only": True}, {"objective": "verified_change"}):
            with self.subTest(invocation=invocation):
                with self.assertRaisesRegex(profiles.ExecutionConfigError,
                                            "conflicting explicit objectives"):
                    resolved(config, **invocation)
        self.assertEqual(resolved(config, objective="merge_ready").objective.layer,
                         "invocation")

    def test_layers_resolve_default_profile_repository_invocation(self) -> None:
        config = {"code_cycle": {"execution": {
            "profile": "full", "objective": "verified_change",
            "stages": {"security": "enabled"},
        }}}
        result = resolved(config, execution_profile="custom",
                          stages=["security=required"])
        self.assertEqual(result.profile.source, "--execution-profile")
        self.assertEqual(result.objective.value, "verified_change")
        self.assertEqual(result.objective.layer, "repository")
        security = result.stages["security"]
        self.assertEqual((security.policy.value, security.source),
                         ("required", "--stage security=required"))
        self.assertEqual([(item.value, item.layer) for item in security.overridden],
                         [("auto", "profile"), ("enabled", "repository")])

    def test_existing_switches_map_to_stage_policies(self) -> None:
        config = {"code_cycle": {"issue_review": {"mode": "off"}}}
        result = resolved(config)
        self.assertEqual(result.stages["issue_review"].policy, plan.StagePolicy.DISABLED)
        self.assertIn("code_cycle.issue_review.mode", result.stages["issue_review"].source)
        result = resolved(config, issue_review="auto")
        self.assertEqual(result.stages["issue_review"].policy, plan.StagePolicy.AUTO)
        self.assertEqual(result.stages["issue_review"].source, "--issue-review auto")
        self.assertEqual(resolved(local_only=True).objective.source, "--local-only")

    def test_conflicting_selections_in_one_layer_are_rejected(self) -> None:
        with self.assertRaisesRegex(profiles.ExecutionConfigError, "conflicting policies"):
            profiles.invocation_settings(issue_review="off", stages=["issue_review=auto"])
        with self.assertRaisesRegex(profiles.ExecutionConfigError, "choose one"):
            profiles.invocation_settings(local_only=True, objective="merge_ready")
        with self.assertRaisesRegex(profiles.ExecutionConfigError, "conflicting policies"):
            profiles.repository_settings({"code_cycle": {
                "issue_review": {"mode": "off"},
                "execution": {"stages": {"issue_review": "auto"}},
            }})
        # Agreeing settings are not a conflict.
        profiles.invocation_settings(issue_review="off", stages=["issue_review=disabled"])

    def test_effective_overrides_and_sources_are_visible(self) -> None:
        built = build(stages=["resolve=required"])
        entry = built.effective_configuration["stages"]["resolve"]
        self.assertEqual(entry["source"], "--stage resolve=required")
        self.assertEqual(entry["overridden"][0]["source"], "profile standard")
        self.assertIn("overrides auto from profile standard",
                      profiles.render_text(built))


class ConflictTests(unittest.TestCase):
    def assertConflict(self, pattern, **kwargs) -> None:
        with self.assertRaisesRegex(profiles.ExecutionConfigError, pattern):
            build(**kwargs)

    def test_an_override_does_not_remove_an_obligation(self) -> None:
        self.assertConflict(r"issue_review=disabled \(--issue-review off\).*profile full",
                            execution_profile="full", issue_review="off")
        self.assertConflict("profile standard requires review",
                            stages=["review=auto"])
        self.assertConflict("requires an independent review",
                            execution_profile="custom", stages=["review=disabled"])
        self.assertConflict("requires verification",
                            execution_profile="custom", stages=["verify=disabled"])

    def test_impossible_dependencies_are_explained_before_effects(self) -> None:
        self.assertConflict("publishes no change request, so security",
                            execution_profile="prototype", stages=["security=required"])
        self.assertConflict("needs a change", execution_profile="custom",
                            stages=["implement=disabled"])
        self.assertConflict("--from resolve", start_from="resolve", change_request="9",
                            stages=["resolve=disabled"])
        self.assertConflict("publishes no change request, so review",
                            local_only=True, stages=["review=enabled"])

    def test_every_conflict_is_reported_at_once(self) -> None:
        with self.assertRaises(profiles.ExecutionConfigError) as caught:
            build(execution_profile="full", issue_review="off", stages=["review=auto"])
        self.assertIn("issue_review=disabled", str(caught.exception))
        self.assertIn("review=auto", str(caught.exception))

    def test_a_custom_plan_may_drop_review_for_a_local_change(self) -> None:
        built = build(execution_profile="custom", local_only=True,
                      stages=["review=disabled", "verify=disabled"])
        self.assertIs(built.stage("verify").resolution, Res.DISABLED)
        self.assertIs(built.stage("review").resolution, Res.NOT_APPLICABLE)

    def test_resumed_and_continued_full_cycles_are_not_refused(self) -> None:
        built = build(execution_profile="full", start_from="review", change_request="12")
        for stage in ("issue_review", "implement", "verify"):
            self.assertIs(built.stage(stage).resolution, Res.NOT_APPLICABLE)
            self.assertIn("--from review --pr 12", built.stage(stage).provenance)
        self.assertTrue(built.stage("review").selected)
        built = build(execution_profile="full", continue_work=True)
        self.assertIs(built.stage("issue_review").resolution, Res.NOT_APPLICABLE)
        self.assertEqual(built.stage("issue_review").provenance,
                         "continued run (--continue)")
        built = build(execution_profile="full", start_from="rereview", change_request="12")
        self.assertTrue(built.stage("rereview").selected)
        self.assertIs(built.stage("resolve").resolution, Res.NOT_APPLICABLE)


class InvalidKeyTests(unittest.TestCase):
    def test_invalid_configuration_keys_and_values(self) -> None:
        cases = (
            ({"execution": {"depth": "full"}}, "unknown key under code_cycle.execution"),
            ({"execution": {"profile": "fast"}}, "unknown execution profile 'fast'"),
            ({"execution": {"objective": "shipped"}}, "unknown objective"),
            ({"execution": {"stages": {"deploy": "auto"}}}, "unknown stage 'deploy'"),
            ({"execution": {"stages": {"review": "sometimes"}}}, "not a stage policy"),
            ({"execution": {"stages": {"ci": "required"}}}, "ci is a control"),
            ({"execution": {"stages": ["review"]}}, "must be a mapping"),
            ({"execution": "full"}, "must be a mapping"),
        )
        for section, pattern in cases:
            with self.subTest(section=section):
                with self.assertRaisesRegex(profiles.ExecutionConfigError, pattern):
                    profiles.repository_settings({"code_cycle": section})

    def test_invalid_stage_flags(self) -> None:
        for text, pattern in (("review", "STAGE=POLICY"), ("merge=required", "merge"),
                              ("review=off", "not a stage policy"),
                              ("bootstrap=auto", "unknown stage")):
            with self.subTest(text=text):
                with self.assertRaisesRegex(profiles.ExecutionConfigError, pattern):
                    profiles.invocation_settings(stages=[text])


class RereviewRuleTests(unittest.TestCase):
    SCENARIOS = {
        "clean review, no changes": dict(changes_after_review=False, open_findings=False),
        "open findings, no fixes": dict(changes_after_review=False, open_findings=True),
        "later changes invalidate evidence": dict(changes_after_review=True,
                                                  open_findings=True),
    }

    def test_rereview_runs_only_after_relevant_changes_in_every_profile(self) -> None:
        for name in profiles.PROFILES:
            for scenario, observed in self.SCENARIOS.items():
                with self.subTest(profile=name, scenario=scenario):
                    built = build(execution_profile=name, objective="merge_ready")
                    decision = profiles.rereview_decision(**observed)
                    after = profiles.apply_rereview_decision(built, decision)
                    stage = after.stage("rereview")
                    self.assertEqual(after.revision, 2)
                    self.assertEqual(stage.selected, observed["changes_after_review"])
                    if stage.selected:
                        self.assertIs(stage.obligation, REQ)
                        after.check_executable("rereview")
                    else:
                        with self.assertRaises(plan.ContractError):
                            after.check_executable("rereview")
                    self.assertEqual(
                        decision.blocks_retained,
                        scenario == "open findings, no fixes")

    def test_a_local_prototype_never_plans_a_rereview(self) -> None:
        built = build(execution_profile="prototype")
        self.assertIs(built.stage("rereview").resolution, Res.NOT_APPLICABLE)

    def test_disabled_rereview_makes_changes_after_review_invalid(self) -> None:
        built = build(execution_profile="custom", stages=["rereview=disabled"])
        unchanged = profiles.apply_rereview_decision(
            built, profiles.rereview_decision(changes_after_review=False,
                                              open_findings=False))
        self.assertIs(unchanged.stage("rereview").resolution, Res.DISABLED)
        with self.assertRaisesRegex(plan.ContractError, "disabled"):
            profiles.apply_rereview_decision(
                built, profiles.rereview_decision(changes_after_review=True,
                                                  open_findings=False))


class DryRunTests(unittest.TestCase):
    BASE = ["--task", "328", "--repo", "datacas/code-cycle-toolkit", "--no-config"]

    def spies(self):
        names = ("select_workspace", "Telemetry", "ConsentBroker", "run_cycle",
                 "detach", "check_change_request", "check_continuation",
                 "CycleHistoryStore", "save_decisions")
        stack = contextlib.ExitStack()
        mocks = {name: stack.enter_context(mock.patch.object(run_cycle, name))
                 for name in names}
        mocks["subprocess.run"] = stack.enter_context(
            mock.patch.object(run_cycle.subprocess, "run"))
        mocks["subprocess.Popen"] = stack.enter_context(
            mock.patch.object(run_cycle.subprocess, "Popen"))
        return stack, mocks

    def run_main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        stack, mocks = self.spies()
        with stack, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = run_cycle.main(argv)
            except SystemExit as exit_:
                code = exit_.code
        return code, stdout.getvalue(), stderr.getvalue(), mocks

    def test_dry_run_prints_the_json_contract_without_effects(self) -> None:
        for profile in profiles.PROFILES:
            with self.subTest(profile=profile):
                code, out, err, mocks = self.run_main(
                    self.BASE + ["--dry-run", "--json", "--execution-profile", profile])
                self.assertEqual(code, 0, err)
                payload = json.loads(out)
                self.assertEqual(payload["contract_version"], plan.CONTRACT_VERSION)
                self.assertEqual(payload["execution_profile"], profile)
                plan.ExecutionPlan.from_dict(payload)
                for name, spy in mocks.items():
                    self.assertFalse(spy.called, name)

    def test_dry_run_text_is_readable(self) -> None:
        code, out, err, mocks = self.run_main(
            self.BASE + ["--dry-run", "--stage", "security=required"])
        self.assertEqual(code, 0, err)
        self.assertIn("profile:   standard (built-in default)", out)
        self.assertIn("pending (required when it applies) until diff", out)
        self.assertIn("policy required from --stage security=required", out)
        self.assertIn("no stage, worktree, dependency preparation, grant or agent", out)
        self.assertFalse(any(spy.called for spy in mocks.values()))

    def test_dry_run_reports_conflicts_before_any_effect(self) -> None:
        code, _, err, mocks = self.run_main(
            self.BASE + ["--dry-run", "--execution-profile", "full", "--issue-review", "off"])
        self.assertEqual(code, 2)
        self.assertIn("profile full requires issue_review", err)
        self.assertFalse(any(spy.called for spy in mocks.values()))

    def test_flag_combinations(self) -> None:
        code, _, err, _ = self.run_main(self.BASE + ["--json"])
        self.assertEqual(code, 2)
        self.assertIn("--json needs --dry-run", err)
        code, _, err, _ = self.run_main(self.BASE + ["--dry-run", "--detach"])
        self.assertEqual(code, 2)

    def test_a_full_run_reaches_workspace_selection(self) -> None:
        stack, mocks = self.spies()
        mocks["select_workspace"].side_effect = run_cycle.CycleDriverError("reached full workspace")
        stderr = io.StringIO()
        with stack, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                run_cycle.main(self.BASE + ["--execution-profile", "full"])
        self.assertIn("reached full workspace", stderr.getvalue())
        self.assertFalse(mocks["run_cycle"].called)

    def test_a_standard_run_reaches_the_current_flow(self) -> None:
        stack, mocks = self.spies()
        mocks["select_workspace"].side_effect = run_cycle.CycleDriverError("reached")
        stderr = io.StringIO()
        with stack, contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                run_cycle.main(self.BASE + ["--execution-profile", "custom",
                                            "--stage", "issue_review=disabled"])
        self.assertIn("reached", stderr.getvalue())
        self.assertFalse(mocks["run_cycle"].called)


if __name__ == "__main__":
    unittest.main()
