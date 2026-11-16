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

    def test_inline_bold_section_label_ends_criteria(self):
        for label in ("**Tests.** Fixtures cover the parser.",
                      "**Tests:** Fixtures cover the parser."):
            with self.subTest(label=label):
                parsed = criteria(f"## Acceptance criteria\n- A\n- B\n\n{label}\nOther prose")
                self.assertEqual(parsed.status, "evaluable")
                self.assertEqual(parsed.items, ("A", "B"))

    def test_malformed_criteria_remain_needs_refinement(self):
        for body, reason in (
            ("## Acceptance criteria\nprose", "criteria must be a flat Markdown list"),
            ("## Acceptance criteria\n- Works\n**Prose** is still prose.",
             "criteria must be a flat Markdown list"),
            ("## Acceptance criteria\n- Works\n  - nested", "criteria must be a flat Markdown list"),
            ("## Acceptance criteria\n- TODO", "placeholder criterion"),
            ("## Acceptance criteria\n", "acceptance-criteria list is empty"),
        ):
            with self.subTest(body=body):
                parsed = criteria(body)
                self.assertEqual(parsed.status, "needs_refinement")
                self.assertEqual(parsed.reason, reason)

    def test_bold_markup_inside_criterion_does_not_end_section(self):
        parsed = criteria("## Acceptance criteria\n- Keeps **bold markup** inside the criterion\n"
                          "**Tests.** Regression coverage follows.")
        self.assertEqual(parsed.status, "evaluable")
        self.assertEqual(parsed.items, ("Keeps **bold markup** inside the criterion",))

    def test_issue_330_body_is_evaluable_without_remote_edit(self):
        body = (ROOT / "tests" / "fixtures" / "issue-330.md").read_text(encoding="utf-8")
        parsed = criteria(body)
        self.assertEqual(parsed.status, "evaluable")
        self.assertEqual(len(parsed.items), 8)

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

    def test_executed_evidence_survives_unexecuted_attempts(self):
        for states in (("pass", "unverified", "unverified"),
                       ("pass", "fail", "unverified"),
                       ("fail", "pass", "unverified")):
            plan = self.plan()
            for state in states:
                plan = plan.record(result(state))
            expected = next(state for state in reversed(states) if state != "unverified")
            self.assertEqual(plan.current("check-1", HEAD), expected)
            self.assertEqual(len(plan.results), len(states))

    def test_binding_refuses_other_identity_configuration_and_provenance(self):
        plan = self.plan().record(result())
        for field, value in (("command", "python3 -m unittest other"),
                             ("executor", "claude"), ("profile", "cheap_coder"),
                             ("context", "other"), ("level", "boundary"),
                             ("provenance", "other")):
            changed = replace(plan, checks=(replace(check(), **{field: value}),))
            self.assertEqual(changed.current("check-1", HEAD), "unverified", field)
        changed = replace(plan, criteria=replace(plan.criteria, content_sha256="c" * 64))
        self.assertEqual(changed.current("check-1", HEAD), "unverified")
        self.assertEqual(plan.current("check-1", OTHER), "unverified")

    def test_legacy_results_acquire_the_original_binding_before_revision(self):
        legacy = self.plan().to_dict()
        legacy["results"] = [result().to_dict()]
        restored = vp.VerificationPlan.from_dict(legacy)
        self.assertEqual(restored.current("check-1", HEAD), "pass")
        changed = replace(restored, checks=(replace(check(), executor="claude"),))
        self.assertEqual(changed.current("check-1", HEAD), "unverified")

    def test_normalized_report_explains_selection_and_keeps_attempts(self):
        plan = self.plan().record(result())
        normalized, updated = vp.normalize_tests({"version": 2, "conclusion": "verified",
            "checks": [report("unverified")]}, HEAD, plan=plan)
        self.assertEqual(normalized["checks"][0]["result"], "pass")
        self.assertEqual(normalized["checks"][0]["selection"]["index"], 0)
        self.assertEqual(len(normalized["checks"][0]["history"]), 2)
        self.assertEqual(updated.results[-1].result, "unverified")

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
        self.assertEqual(updated.results[:-1], plan.results)
        self.assertEqual(updated.results[-1].result, "unverified")

    def test_invalid_followup_never_raises_failed_agent_or_frozen_conclusion(self):
        for plan, conclusion in ((None, "failed"), (
                vp.VerificationPlan(criteria()).extend((check(),)).record(result("fail")), "verified")):
            payload, _ = rc._normalize_test_payload({"head_sha": HEAD, "tests": {
                "version": 99, "conclusion": conclusion, "passed": True}}, plan)
            self.assertEqual(rc._legacy_tests(payload)["verification"], "failed")

    def test_v2_rejects_string_evidence_entry_with_check_name(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        payload = {"version": 2, "conclusion": "verified", "checks": [report(evidence=["text"])]}
        with self.assertRaisesRegex(
                vp.VerificationError, r"check-1\]\.evidence\[0\].*received str.*expected an object"):
            vp.normalize_tests(payload, HEAD, plan=plan)

    def test_attempts_representation_is_normalized_and_recorded(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        cases = (
            (3, [], {"type": "int", "value": 3}),
            (0, [], {"type": "int", "value": 0}),
            ("retried twice", [{"note": "retried twice"}],
             {"type": "str", "value": "retried twice"}),
            (["first", {"command": "x"}], [{"note": "first"}, {"command": "x"}],
             {"type": "array", "value": ["first", {"command": "x"}]}),
        )
        for original, attempts, record in cases:
            payload = {"version": 2, "conclusion": "verified",
                       "checks": [report(attempts=original)]}
            with self.subTest(original=original):
                normalized, updated = vp.normalize_tests(payload, HEAD, plan=plan)
                self.assertEqual(normalized["checks"][0]["attempts"], attempts)
                self.assertEqual(normalized["checks"][0]["attempts_normalized"], record)
                assert updated is not None
                self.assertEqual(updated.current("check-1", HEAD), "pass")
                # The recorded plan survives a store and restore round trip.
                self.assertEqual(vp.VerificationPlan.from_dict(updated.to_dict()), updated)

    def test_attempts_already_objects_carry_no_normalization_record(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        for attempts in ([], [{"command": "x"}], None):
            payload = {"version": 2, "conclusion": "verified",
                       "checks": [report(attempts=attempts)]}
            with self.subTest(attempts=attempts):
                normalized, _ = vp.normalize_tests(payload, HEAD, plan=plan)
                self.assertNotIn("attempts_normalized", normalized["checks"][0])

    def test_attempts_normalization_reaches_history_entries(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        entry = report(history=[{**result("fail", head=OTHER).to_dict(), "attempts": 2}])
        normalized, updated = vp.normalize_tests(
            {"version": 2, "conclusion": "verified", "checks": [entry]}, HEAD, plan=plan)
        assert updated is not None
        prior = next(item for item in updated.results if item.head_sha == OTHER)
        self.assertEqual(prior.attempts, ())
        self.assertEqual(prior.attempts_normalized, {"type": "int", "value": 2})

    def test_attempts_that_cannot_be_normalized_name_field_type_and_shape(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        cases = (
            ({"command": "x"}, r"checks\[0\]\.attempts: received dict; expected an array of objects"),
            (1.5, r"checks\[0\]\.attempts: received float; expected an array of objects"),
            (True, r"checks\[0\]\.attempts: received bool; expected an array of objects"),
            ([4], r"checks\[check-1\]\.attempts\[0\]: received int; expected an object"),
            ([["x"]], r"checks\[check-1\]\.attempts\[0\]: received list; expected an object"),
        )
        for attempts, message in cases:
            payload = {"version": 2, "conclusion": "verified", "checks": [report(attempts=attempts)]}
            with self.subTest(attempts=attempts), self.assertRaisesRegex(vp.VerificationError, message):
                vp.normalize_tests(payload, HEAD, plan=plan)

    def test_attempts_normalization_is_accepted_in_the_four_stage_reports(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        for status in ("IMPLEMENTED", "APPROVED", "RESOLVED", "REVIEWED"):
            for original, attempts in ((3, []), ("note", [{"note": "note"}]),
                                       (["note"], [{"note": "note"}])):
                payload = {"status": status, "head_sha": HEAD, "tests": {
                    "version": 2, "conclusion": "verified", "criteria": criteria().to_dict(),
                    "checks": [report(attempts=original)]}}
                with self.subTest(status=status, attempts=original):
                    normalized, updated = rc._normalize_test_payload(payload, plan)
                    self.assertNotIn("rejected", normalized["tests"])
                    self.assertEqual(normalized["tests"]["checks"][0]["attempts"], attempts)
                    self.assertEqual(normalized["tests"]["checks"][0]["attempts_normalized"]["value"],
                                     original)
                    assert updated is not None
                    self.assertEqual(updated.current("check-1", HEAD), "pass")
                    # A report without a runtime plan (a review) takes the same path.
                    fields = rc._tests(dict(payload))
                    self.assertTrue(fields["tests_passed"])

    def test_evidence_representation_stays_rejected_in_the_four_stage_reports(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        for status in ("IMPLEMENTED", "APPROVED", "RESOLVED", "REVIEWED"):
            for evidence in (["text"], 3):
                payload = {"status": status, "head_sha": HEAD, "tests": {
                    "version": 2, "conclusion": "verified",
                    "checks": [report(evidence=evidence)]}}
                with self.subTest(status=status, evidence=evidence):
                    normalized, _ = rc._normalize_test_payload(payload, plan)
                    self.assertIn("evidence", normalized["tests"]["rejected"])
                    self.assertFalse(normalized["tests"]["passed"])

    def test_v2_normalizes_single_object_and_null_optional_values(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        entry = report(evidence={"exit_code": 0, "executed": 1}, cause=None, attempts=None)
        normalized, _ = vp.normalize_tests(
            {"version": 2, "conclusion": "verified", "checks": [entry]}, HEAD, plan=plan)
        self.assertEqual(normalized["checks"][0]["evidence"], [{"exit_code": 0, "executed": 1}])
        self.assertEqual(normalized["checks"][0]["cause"], "")
        self.assertEqual(normalized["checks"][0]["attempts"], [])

    def test_issue_406_pass_checks_with_null_causes_are_accepted(self):
        ids = ("command-permissions", "evidence-wiring", "package-validation")
        checks = [vp.Check(**{**check(id=identifier).to_dict(), "criteria": ()}) for identifier in ids]
        plan = vp.VerificationPlan(criteria()).extend(checks)
        entries = [report(id=identifier, cause=None, evidence=[{"exit_code": 0, "executed": 1}])
                   for identifier in ids]
        normalized, _ = vp.normalize_tests(
            {"version": 2, "conclusion": "verified", "checks": entries}, HEAD, plan=plan)
        self.assertEqual([item["id"] for item in normalized["checks"]], list(ids))
        self.assertTrue(all(item["cause"] == "" for item in normalized["checks"]))

    def test_v2_null_evidence_is_empty_but_does_not_make_pass_valid(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        entry = report(evidence=None, cause=None, attempts=None)
        payload = {"version": 2, "conclusion": "verified", "checks": [entry]}
        with self.assertRaisesRegex(vp.VerificationError, r"evidence.*received NoneType.*at least one"):
            vp.normalize_tests(payload, HEAD, plan=plan)

    def test_v2_unverified_still_requires_nonempty_cause(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        entry = report(result="unverified", evidence=None, cause=None, attempts=None)
        payload = {"version": 2, "conclusion": "not_verified", "checks": [entry]}
        with self.assertRaisesRegex(vp.VerificationError, r"cause.*received NoneType.*non-empty text"):
            vp.normalize_tests(payload, HEAD, plan=plan)

    def test_v2_unverified_non_text_cause_is_field_level_rejection(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        entry = report(result="unverified", evidence=None, cause=5, attempts=None)
        payload = {"version": 2, "conclusion": "not_verified", "checks": [entry]}
        with self.assertRaises(vp.VerificationError) as caught:
            vp.normalize_tests(payload, HEAD, plan=plan)
        message = str(caught.exception)
        self.assertIn("cause", message)
        self.assertIn("int", message)
        self.assertIn("omitted or text", message)

    def test_v2_non_text_result_is_field_level_rejection(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        entry = report(result=[], evidence=None, cause=None, attempts=None)
        payload = {"version": 2, "conclusion": "not_verified", "checks": [entry]}
        with self.assertRaises(vp.VerificationError) as caught:
            vp.normalize_tests(payload, HEAD, plan=plan)
        message = str(caught.exception)
        self.assertIn("result", message)
        self.assertIn("list", message)
        self.assertIn("pass, fail, or unverified", message)

    def test_v2_rejections_describe_field_received_type_and_expected_format(self):
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        for field, value, expected in (("evidence", "bad", "array"), ("attempts", 1.5, "array"),
                                       ("cause", [], "text")):
            entry = report(**{field: value})
            payload = {"version": 2, "conclusion": "verified", "checks": [entry]}
            with self.subTest(field=field), self.assertRaises(vp.VerificationError) as caught:
                vp.normalize_tests(payload, HEAD, plan=plan)
            message = str(caught.exception)
            self.assertIn(field, message)
            self.assertIn(type(value).__name__, message)
            self.assertIn(expected, message)

    def test_v2_frozen_check_left_without_identity_fields_takes_them_from_the_plan(self):
        # A report naming a frozen check and only how it ended is that check (#450).
        plan = vp.VerificationPlan(criteria(), (check(),))
        entry = report()
        for field in ("command", "executor", "profile", "context", "required"):
            del entry[field]
        normalized, updated = vp.normalize_tests(
            {"version": 2, "conclusion": "verified", "checks": [entry]}, HEAD, plan=plan)
        [reported] = normalized["checks"]
        self.assertEqual(check().command, reported["command"])
        self.assertEqual(plan.checks, updated.checks)

    def test_v2_missing_required_check_field_names_missing_value_and_format(self):
        plan = vp.VerificationPlan(criteria(), (check(),))
        entry = report(id="another-check")
        del entry["command"]
        with self.assertRaises(vp.VerificationError) as caught:
            vp.normalize_tests({"version": 2, "conclusion": "not_verified",
                                "checks": [entry]}, HEAD, plan=plan)
        self.assertIn("command", str(caught.exception))
        self.assertIn("received missing", str(caught.exception))
        self.assertIn("expected text", str(caught.exception))

    def test_v2_invalid_check_validation_names_field_value_and_format(self):
        plan = vp.VerificationPlan(criteria(), (check(),))
        entry = report(level="invalid")
        with self.assertRaises(vp.VerificationError) as caught:
            vp.normalize_tests({"version": 2, "conclusion": "not_verified",
                                "checks": [entry]}, HEAD, plan=plan)
        message = str(caught.exception)
        self.assertIn("check.level", message)
        self.assertIn("received str", message)
        self.assertIn("expected static, test, or boundary", message)

    def test_v2_duplicate_and_command_mismatch_rejections_name_field_details(self):
        plan = vp.VerificationPlan(criteria(), (check(),))
        entry = report(evidence=[{"command": "python3 other", "exit_code": 0, "executed": 1}])
        with self.assertRaises(vp.VerificationError) as caught:
            vp.normalize_tests({"version": 2, "conclusion": "verified",
                                "checks": [entry]}, HEAD, plan=plan)
        self.assertIn("evidence.command", str(caught.exception))
        self.assertIn("received str", str(caught.exception))
        self.assertIn("expected the frozen check command", str(caught.exception))
        with self.assertRaises(vp.VerificationError) as caught:
            vp.normalize_tests({"version": 2, "conclusion": "not_verified",
                                "checks": [report(), report()]}, HEAD, plan=plan)
        self.assertIn("checks[1].id", str(caught.exception))
        self.assertIn("received str", str(caught.exception))
        self.assertIn("expected a unique reported check ID", str(caught.exception))

    def test_v2_unhashable_check_id_is_field_level_rejection(self):
        plan = vp.VerificationPlan(criteria(), (check(),))
        with self.assertRaises(vp.VerificationError) as caught:
            vp.normalize_tests({"version": 2, "conclusion": "not_verified",
                                "checks": [report(id=["x"])]}, HEAD, plan=plan)
        self.assertIn("checks[0].id", str(caught.exception))
        self.assertIn("received list", str(caught.exception))
        self.assertIn("expected a non-empty string", str(caught.exception))

    def test_v1_string_evidence_rejects_with_plan_and_stays_unchanged_without_one(self):
        legacy = {"version": 1, "conclusion": "verified", "evidence": ["text"]}
        plan = vp.VerificationPlan(criteria()).extend((check(),))
        with self.assertRaisesRegex(vp.VerificationError, r"evidence.command.*received str.*expected text"):
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
