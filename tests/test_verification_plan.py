"""Verification contracts and their runtime/telemetry boundaries."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import execution_plan as ep
import executors as ex
import execution_profiles as profiles
import run_cycle as rc
import stats
import verification_plan as vp
from test_run_cycle import RunCycleTestCase, Talker, Sequence, block

HEAD = "a" * 40
OTHER = "b" * 40


def criteria(body="## Acceptance criteria\n- Works\n"):
    return vp.parse_criteria("github", body)


def check(**kw):
    return vp.Check(**{**dict(id="check-1", command="python3 -m unittest",
                              executor="codex", profile="deep_coder", context="local",
                              required=True, criteria=("AC-1",)), **kw})


def result(state="pass", head=HEAD, id="check-1"):
    return vp.CheckResult(id, head, state,
                          ({"exit_code": 0 if state == "pass" else 1,
                            "executed": 2},) if state != "unverified" else (),
                          "sandbox unavailable" if state == "unverified" else "")


def report(state="pass", **kw):
    return {**check().to_dict(), **result(state).to_dict(), **kw}


class CriteriaTests(unittest.TestCase):
    def test_closed_grammar_english_spanish_heading_bold_lists(self):
        for heading in ("## Acceptance criteria", "### Criterios de aceptación",
                        "**Acceptance criteria:**", "**Criterios de aceptación.**"):
            for bullet in ("-", "*", "+", "1.", "2)"):
                with self.subTest(heading=heading, bullet=bullet):
                    parsed = criteria(f"{heading}\n{bullet} [ ] Works\n\n## Tests\nOther prose")
                    self.assertEqual(parsed.status, "evaluable")
                    self.assertEqual(parsed.items, ("Works",))

    def test_missing_empty_duplicate_unparseable_and_placeholder_stay_open(self):
        for body in ("## Problem\nWorks", "## Acceptance criteria\n", "## Acceptance criteria\nprose",
                     "## Acceptance criteria\n- Works\n  - nested",
                     "## Acceptance criteria\n- TODO", "## Acceptance criteria\n- A\n## Acceptance criteria\n- B"):
            with self.subTest(body=body):
                self.assertEqual(criteria(body).status, "needs_refinement")

    def test_provider_independent_unavailable_readers_never_satisfied(self):
        runner = Mock()
        for provider in ("plane", "jira"):
            parsed = vp.read_criteria(provider, "project", "work", runner=runner)
            self.assertEqual(parsed.status, "not_evaluable")
            self.assertEqual(vp.VerificationPlan(parsed).cap("verified", HEAD), "not_verified")
        runner.assert_not_called()

    def test_github_reader_failure_and_body_change(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"body": "## Acceptance criteria\n- A"})))
        parsed = vp.read_criteria("github", "o/r", "1", runner=runner)
        self.assertEqual(parsed.items, ("A",))
        self.assertNotEqual(parsed.content_sha256, criteria().content_sha256)
        self.assertEqual(runner.call_args.args[0], ["gh", "issue", "view", "1", "--repo", "o/r", "--json", "body"])
        for value in (subprocess.CompletedProcess([], 1, ""), subprocess.CompletedProcess([], 0, "{}")):
            runner.return_value = value
            with self.assertRaises(vp.VerificationError):
                vp.read_criteria("github", "o/r", "1", runner=runner)

    def test_reader_crosses_real_process_boundary_without_live_credentials(self):
        def local_provider(_command, **kwargs):
            return subprocess.run([
                sys.executable, "-c",
                "import json; print(json.dumps({'body': '## Acceptance criteria\\n- Observable result'}))"
            ], **kwargs)

        parsed = vp.read_criteria("github", "o/r", "290", runner=local_provider)
        self.assertEqual(parsed.status, "evaluable")
        self.assertEqual(parsed.items, ("Observable result",))


class PlanTests(unittest.TestCase):
    def plan(self):
        return vp.VerificationPlan(criteria()).extend((check(),))

    def test_freeze_allows_addition_promotion_but_never_rewrite_or_demotion(self):
        plan = self.plan()
        for changed in (check(required=False), check(command="true"), check(executor="claude"),
                        check(profile="cheap_coder"), check(context="ci"), check(criteria=())):
            with self.subTest(changed=changed), self.assertRaises(vp.VerificationError):
                plan.extend((changed,))
        optional = check(id="optional", required=False, criteria=())
        plan = plan.extend((optional,)).extend((replace(optional, required=True),))
        self.assertTrue(plan.checks[-1].required)
        self.assertEqual(plan.extend(()), plan)

    def test_results_preserve_history_and_invalidate_stale_head(self):
        plan = self.plan().record(result())
        self.assertEqual(plan.cap("verified", HEAD), "verified")
        self.assertEqual(plan.cap("verified", OTHER), "not_verified")
        newer = plan.record(result("unverified", OTHER))
        self.assertEqual(len(newer.results), 2)
        # The caller supplies the current HEAD. History for another SHA never
        # erases proof retained for this SHA (including Hotfix test-first runs).
        self.assertEqual(newer.current("check-1", HEAD), "pass")
        newer = newer.record(result("pass", OTHER))
        self.assertEqual(newer.cap("verified", OTHER), "verified")
        self.assertEqual(vp.VerificationPlan.from_dict(newer.to_dict()), newer)

    def test_caps_required_fail_unverified_open_and_never_raise(self):
        for state, cap in (("pass", "verified"), ("fail", "failed"), ("unverified", "not_verified")):
            plan = self.plan().record(result(state))
            self.assertEqual(plan.cap("verified", HEAD), cap)
            self.assertEqual(plan.cap("failed", HEAD), "failed")
        self.assertEqual(vp.VerificationPlan(criteria()).cap("verified", HEAD), "not_verified")
        optional = vp.VerificationPlan(criteria()).extend((check(), check(id="optional", required=False, criteria=())))
        optional = optional.record(result()).record(result("unverified", id="optional"))
        self.assertEqual(optional.cap("verified", HEAD), "verified")

    def test_result_rejects_invalid_counts_exit_codes_contradictions_and_unknown_id(self):
        for value in (replace(result(), evidence=()), replace(result(), evidence=({"exit_code": True, "executed": 0},)),
                      replace(result(), evidence=({"exit_code": 1, "executed": 0},)),
                      replace(result(), head_sha="short"), replace(result("unverified"), cause=""),
                      result(id="unknown")):
            with self.subTest(value=value), self.assertRaises(vp.VerificationError):
                self.plan().record(value)

    def test_execution_plan_versions_and_owner_revision(self):
        owner = profiles.build_plan(profiles.resolve_execution(profiles.ExecutionSettings(), profiles.ExecutionSettings()),
                                    repository="o/r", work_item="290")
        owner = replace(owner, verification=self.plan().record(result()))
        self.assertEqual(owner.to_dict()["contract_version"], 3)
        self.assertEqual(ep.ExecutionPlan.from_dict(owner.to_dict()), owner)
        for version in (1, 2):
            with self.assertRaises(ep.ContractError):
                owner.to_dict(version)
            legacy_owner = profiles.build_plan(profiles.resolve_execution(
                profiles.ExecutionSettings(), profiles.invocation_settings(execution_profile="prototype")),
                repository="o/r", work_item="290")
            older = legacy_owner.to_dict(version)
            self.assertIsNone(ep.ExecutionPlan.from_dict(older).verification)
        extended = owner.extend_verification((check(id="new", required=False, criteria=()),))
        self.assertEqual(extended.revision, owner.revision + 1)
        self.assertEqual(extended.verification.results, owner.verification.results)


class ResultTests(unittest.TestCase):
    def test_no_test_report_preserves_plan_and_does_not_invent_an_outcome(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),)).record(result())
        for omitted in ({}, {"tests": {"reason": "no_code_change"}},
                        {"tests": {"version": 2, "reason": "no_code_change"}}):
            with self.subTest(payload=omitted):
                original = {"status": "RESOLVED", "head_sha": HEAD, **omitted}
                payload, updated = rc._normalize_test_payload(original, plan)
                self.assertEqual(payload, original)
                self.assertEqual(updated, plan)
                self.assertEqual(rc._legacy_tests(payload), {})

    def test_no_code_change_cannot_hide_an_explicit_required_failure(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        payload, updated = rc._normalize_test_payload({"status": "RESOLVED", "head_sha": HEAD,
            "tests": {"version": 2, "reason": "no_code_change", "conclusion": "verified",
                      "passed": True, "checks": [report("fail")]}}, plan)
        self.assertEqual(updated.current("check-1", HEAD), "fail")
        self.assertEqual(rc._legacy_tests(payload)["verification"], "failed")
        self.assertFalse(rc._legacy_tests(payload)["tests_passed"])

    def test_v2_records_caps_and_boundary_without_touching_host_checks(self):
        payload = {"status": "IMPLEMENTED", "head_sha": HEAD, "tests": {
            "version": 2, "conclusion": "verified", "criteria": criteria().to_dict(),
            "boundary": "required", "checks": [report(level="boundary")]},
            "checks": {"head_sha": HEAD, "passed": 3, "failed": 0, "pending": 1}}
        before = dict(payload["checks"])
        fields = rc._tests(payload)
        # A boundary-only report cannot prove that tests ran.
        self.assertFalse(fields["tests_passed"])
        payload["tests"]["checks"].append(report(id="test"))
        fields = rc._tests(payload)
        self.assertTrue(fields["tests_passed"])
        self.assertTrue(fields["boundary_verified"])
        self.assertEqual(fields["tests_basis"], "agent_reported")
        self.assertEqual(payload["checks"], before)
        self.assertEqual(rc._checks(payload), {"checks_passed": 3, "checks_failed": 0, "checks_pending": 1,
                                              "checks_head_sha": HEAD})

    def test_legacy_adapter_does_not_invent_missing_required_pass(self):
        plan = vp.VerificationPlan(criteria()).extend((check(), vp.configured_check("required", "codex", "deep_coder")))
        legacy = {"conclusion": "verified", "passed": True,
                  "evidence": [{"command": check().command, "level": "test", "exit_code": 0, "executed": 2}]}
        tests, updated = vp.normalize_tests(legacy, HEAD, plan=plan)
        self.assertEqual(tests["conclusion"], "not_verified")
        self.assertEqual(updated.current("configured-test", HEAD), "unverified")
        self.assertEqual(updated.current("check-1", HEAD), "pass")

    def test_omitted_failure_cannot_be_erased_by_later_report(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),)).record(result("fail"))
        tests, updated = vp.normalize_tests(
            {"version": 2, "conclusion": "verified", "checks": []}, HEAD, plan=plan)
        self.assertEqual(tests["conclusion"], "failed")
        self.assertEqual(updated.results, plan.results)

    def test_invalid_followup_never_raises_failed_agent_or_frozen_conclusion(self):
        for plan, conclusion in ((None, "failed"), (
                vp.VerificationPlan(criteria()).extend((check(),)).record(result("fail")), "verified")):
            payload, _ = rc._normalize_test_payload({"head_sha": HEAD, "tests": {
                "version": 99, "conclusion": conclusion, "passed": True}}, plan)
            self.assertEqual(rc._legacy_tests(payload)["verification"], "failed")

    def test_v2_rejects_string_evidence_and_attempts_with_check_name(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        for field in ("evidence", "attempts"):
            entry = report(**{field: ["text"]})
            payload = {"version": 2, "conclusion": "verified", "checks": [entry]}
            with self.subTest(field=field), self.assertRaisesRegex(
                    vp.VerificationError, rf"check check-1 {field}\[0\].*str"):
                vp.normalize_tests(payload, HEAD, plan=plan)

    def test_v1_string_evidence_rejects_with_plan_and_stays_unchanged_without_one(self):
        legacy = {"version": 1, "conclusion": "verified", "evidence": ["text"]}
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        with self.assertRaisesRegex(vp.VerificationError, "invalid legacy evidence"):
            vp.normalize_tests(legacy, HEAD, plan=plan)
        unchanged, updated = vp.normalize_tests(legacy, HEAD)
        self.assertEqual(unchanged, legacy)
        self.assertIsNone(updated)

    def test_string_check_entries_reach_cycle_as_not_verified(self):
        for field in ("evidence", "attempts"):
            payload, _ = rc._normalize_test_payload({"head_sha": HEAD, "tests": {
                "version": 2, "conclusion": "verified", "passed": True,
                "checks": [report(**{field: ["text"]})]}})
            with self.subTest(field=field):
                self.assertEqual(payload["tests"]["conclusion"], "not_verified")
                self.assertFalse(payload["tests"]["passed"])

    def test_legacy_unmatched_failures_are_retained(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        payload, updated = rc._normalize_test_payload({"status": "IMPLEMENTED", "head_sha": HEAD,
            "tests": {"passed": True, "conclusion": "verified", "evidence": [
                {"level": "test", "command": check().command, "exit_code": 0, "executed": 2},
                {"level": "static", "command": "lint", "exit_code": 1, "executed": None}]}}, plan)
        self.assertFalse(rc._legacy_tests(payload)["tests_passed"])
        self.assertEqual(len(updated.results), 2)

    def test_invalid_and_stale_reports_fail_closed_and_ran_false_cannot_hide_caps(self):
        for changed in ({"head_sha": OTHER}, {"result": "unverified", "evidence": [], "cause": "no sandbox"},
                        {"evidence": [{"exit_code": False, "executed": 1}]}, {"criteria": ["AC-99"]}):
            fields = rc._tests({"status": "IMPLEMENTED", "head_sha": HEAD, "tests": {
                "version": 2, "criteria": criteria().to_dict(), "ran": False, "passed": True,
                "conclusion": "verified", "checks": [report(**changed)]}})
            self.assertEqual(fields["verification"], "not_verified")
            self.assertFalse(fields["tests_passed"])
        self.assertEqual(rc._tests({"tests": {"version": 9, "passed": True}})["verification"], "not_verified")


class CriteriaGateTests(RunCycleTestCase):
    def test_gate_runs_for_every_issue_review_mode_and_difficulty_before_dispatch(self):
        for mode in ("off", "auto"):
            for provider in ("github", "plane", "jira"):
                for difficulty in (1, 2, 3):
                    reader = Mock(return_value=vp.parse_criteria(provider, "## Problem\nNo section"))
                    implementer, reviewer = Talker("codex"), Talker("claude")
                    report_ = rc.run_cycle(
                        "owner/api", "290", rc.TaskSignals(difficulty=difficulty), self.store,
                        registry=rc.Registry([implementer, reviewer]),
                        availability={"codex": ex.Availability.READY, "claude": ex.Availability.READY},
                        work_item_provider=provider, work_item_repository="owner/issues",
                        issue_review=mode, criteria_reader=reader)
                    self.assertEqual(report_.status, "HUMAN_INTERVENTION" if provider == "github" else "BLOCKED")
                    self.assertIn("acceptance criteria", report_.stopped_because)
                    self.assertEqual(implementer.dispatched, [])
                    reader.assert_called_once()
                    self.assertEqual(self.rows()[-1]["payload"]["stop_reason"],
                                     "needs_refinement" if provider == "github" else "readiness_unconfirmed")
                    if provider != "github":
                        self.assertIn("criteria reader unavailable", report_.stopped_because)
                        self.assertNotIn("refine the work item", report_.stopped_because)
                    self.assertNotIn("verification", self.rows()[-1]["payload"])

    def test_valid_criteria_read_once_and_context_reaches_implementation(self):
        reader = Mock(return_value=criteria())
        implementer, reviewer = Talker("codex"), Talker("claude")
        self.run_cycle(implementer, reviewer, work_item_provider="github",
                       work_item_repository="owner/issues", criteria_reader=reader)
        reader.assert_called_once()
        self.assertIn("Runtime acceptance criteria", implementer.dispatched[0])

    def test_v2_conclusion_reaches_telemetry_and_stats_as_agent_reported(self):
        tests = {"version": 2, "conclusion": "verified", "checks": [report("unverified")]}
        implementer = Talker("codex", block("IMPLEMENTED", head_sha=HEAD, tests=tests))
        self.run_cycle(implementer, Talker("claude"), work_item_provider="github",
                       work_item_repository="owner/issues",
                       criteria_reader=lambda *_a, **_kw: criteria())
        verdict = next(row["payload"] for row in self.rows() if row["role"] == "implement"
                       and row["payload"].get("record_kind") == "verdict")
        self.assertEqual(verdict["verification"], "not_verified")
        self.assertFalse(verdict["tests_passed"])
        # No executed command: it stays a claim, not runtime-observed evidence.
        self.assertEqual(verdict["tests_basis"], "claimed")
        aggregate = stats.aggregate(self.rows(), repo_id="owner/api", days=None)
        self.assertEqual(aggregate["summary"]["verification"]["conclusions"]["not_verified"], 1)

    def test_reported_passes_keep_their_basis_through_telemetry_and_stats(self):
        tests = {"version": 2, "conclusion": "verified", "checks": [report()]}
        self.run_cycle(Talker("codex", block("IMPLEMENTED", head_sha=HEAD, tests=tests)),
                       Talker("claude"), work_item_provider="github", work_item_repository="o/r",
                       criteria_reader=lambda *_a, **_kw: criteria())
        aggregate = stats.aggregate(self.rows(), repo_id="owner/api", days=None)
        self.assertEqual(aggregate["summary"]["verification"]["by_basis"]["agent_reported"]["passed"], 1)

    def test_no_test_resolution_and_rereview_keep_implementation_outcome(self):
        for tests in (None, {"reason": "no_code_change"}):
            with self.subTest(tests=tests):
                initial = {"version": 2, "conclusion": "verified", "checks": [report(), {
                    **vp.configured_check(check().command, "codex", "cheap_coder").to_dict(),
                    **result(id="configured-test").to_dict()}]}
                resolution = {} if tests is None else {"tests": tests}
                implementer = Sequence("codex", [
                    block("IMPLEMENTED", head_sha=HEAD, tests=initial),
                    block("RESOLVED", head_sha=HEAD, **resolution)])
                reviewer = Sequence("claude", [block("CHANGES_REQUESTED"),
                                               block("APPROVED", head_sha=HEAD, **resolution)])
                run = self.run_cycle(implementer, reviewer, allowed_test_command=check().command,
                                     work_item_provider="github", work_item_repository="owner/issues")
                self.assertEqual([stage.role for stage in run.stages],
                                 ["implement", "review", "resolve", "rereview"])
                verdicts = [row["payload"] for row in self.rows()
                            if row["payload"].get("record_kind") == "verdict"
                            and row["role"] in {"resolve", "rereview"}]
                for verdict in verdicts:
                    self.assertNotIn("verification", verdict)
                    self.assertNotIn("tests_passed", verdict)
                closing = self.rows()[-1]["payload"]
                self.assertEqual(closing["verification"], "verified")
                self.assertTrue(closing["tests_passed"])

    def test_later_profiles_receive_and_reuse_frozen_check_identity(self):
        configured = replace(vp.configured_check(check().command, "codex", "cheap_coder"),
                             criteria=("AC-1",))
        tests = {"version": 2, "conclusion": "verified", "checks": [
            {**configured.to_dict(), **result(id="configured-test").to_dict()}]}
        prompts = []

        class FollowingReporter(Talker):
            def spoken(self, task):
                if "cc-implement-issue" in task:
                    return block("IMPLEMENTED", head_sha=HEAD, tests=tests)
                if "cc-initial-review" in task:
                    return block("CHANGES_REQUESTED")
                prompts.append(task)
                marker = "Runtime frozen verification checks (data): "
                frozen = json.loads(task.split(marker, 1)[1].splitlines()[0])
                checks = [{**item, **result(id=item["id"]).to_dict()} for item in frozen]
                return block("RESOLVED" if "cc-resolve-comments" in task else "APPROVED",
                             head_sha=HEAD, tests={"version": 2, "conclusion": "verified", "checks": checks})

        run = self.run_cycle(FollowingReporter("codex"), FollowingReporter("claude"),
                             allowed_test_command=check().command, work_item_provider="github",
                             work_item_repository="owner/issues")
        self.assertEqual([stage.role for stage in run.stages],
                         ["implement", "review", "resolve", "rereview"])
        self.assertNotEqual(run.stages[0].decision.profile, run.stages[-1].decision.profile)
        assert run.stages[0].result is not None
        assert run.stages[-1].result is not None
        self.assertNotEqual(run.stages[0].result.executor, run.stages[-1].result.executor)
        self.assertEqual(len(prompts), 2)
        for row in self.rows():
            if row["payload"].get("record_kind") == "verdict" and row["role"] in {"resolve", "rereview"}:
                self.assertEqual(row["payload"]["verification"], "verified")
                self.assertTrue(row["payload"]["tests_passed"])


if __name__ == "__main__":
    unittest.main()
