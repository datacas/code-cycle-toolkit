"""The optional issue review before implementation.

A readiness gate is only worth having if it fails closed: anything but a
confirmed READY must stop before `implement`, a READY the runtime cannot
confirm must escalate once or stop, and none of the review's prose may reach
the store. These tests hold the runtime to that, and pin the closed result
shape the skill documents.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import executors as ex  # noqa: E402
import issue_review as ir  # noqa: E402
import router  # noqa: E402
import run_cycle as rc  # noqa: E402
import telemetry as tm  # noqa: E402
from cycle import CycleError, CycleRecorder, role_contract  # noqa: E402
from test_run_cycle import APPROVED, COMPLETED, RunCycleTestCase, Talker, block  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "issue-review"


def result(status: str = "READY", **fields) -> dict:
    """A valid issue-review result, adjusted by the test."""
    payload = {
        "skill": "cc-issue-review", "status": status, "confidence": "high",
        "dimensions": ["applicability", "acceptance_verification"],
        "findings": [], "uncertainties": [],
    }
    if status == "NEEDS_REFINEMENT":
        payload["findings"] = [finding()]
    payload.update(fields)
    return payload


def finding(**fields) -> dict:
    value = {
        "id": "IR-001", "dimension": "acceptance_verification",
        "severity": "high", "blocks_readiness": True,
        "evidence": [{"kind": "work_item", "ref": "API-7"}],
        "summary": "SECRET-PROSE the criteria name no observable result",
        "proposed_change": "SECRET-PROSE state the observable result",
    }
    value.update(fields)
    return value


def uncertainty(**fields) -> dict:
    value = {"id": "IU-001", "material": True, "resolved": False,
             "summary": "SECRET-PROSE whether old rows must stay readable"}
    value.update(fields)
    return value


def spoken(payload: dict) -> str:
    return f"ORCHESTRATION_RESULT\n{json.dumps(payload)}\nEND_ORCHESTRATION_RESULT"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ResultShapeTests(unittest.TestCase):
    def test_a_documented_result_is_valid(self) -> None:
        for status in ir.STATUSES:
            with self.subTest(status=status):
                payload = result(status)
                if status == "BLOCKED":
                    payload["confidence"] = "low"
                self.assertEqual([], ir.result_errors(payload))

    def test_closed_vocabularies_are_enforced(self) -> None:
        cases = {
            "status": result(status="APPROVED"),
            "confidence": result(confidence="certain"),
            "dimension list": result(dimensions=["diagnosis"]),
            "finding dimension": result("NEEDS_REFINEMENT", findings=[finding(dimension="style")]),
            "severity": result("NEEDS_REFINEMENT", findings=[finding(severity="P1")]),
            "evidence kind": result("NEEDS_REFINEMENT", findings=[
                finding(evidence=[{"kind": "memory", "ref": "x"}])]),
            "empty evidence": result("NEEDS_REFINEMENT", findings=[finding(evidence=[])]),
            "skill": result(skill="cc-initial-review"),
        }
        for name, payload in cases.items():
            with self.subTest(name):
                self.assertNotEqual([], ir.result_errors(payload))

    def test_review_finding_identity_and_fields_are_refused(self) -> None:
        """REV-xxx, status and disposition belong to the change-request contract."""
        for bad in (finding(id="REV-001"), {**finding(), "status": "open"},
                    {**finding(), "disposition": "valid"}):
            with self.subTest(bad=bad):
                self.assertNotEqual(
                    [], ir.result_errors(result("NEEDS_REFINEMENT", findings=[bad])))

    def test_an_implementation_diagnosis_is_not_part_of_the_result(self) -> None:
        payload = result(diagnosis={"classification": "feature_request"})

        self.assertIn("keys outside the issue-review contract: diagnosis",
                      ir.result_errors(payload))

    def test_outcome_and_findings_must_agree(self) -> None:
        self.assertNotEqual([], ir.result_errors(result(findings=[finding()])))
        self.assertNotEqual([], ir.result_errors(result("NEEDS_REFINEMENT", findings=[])))
        self.assertEqual([], ir.result_errors(
            result("NEEDS_REFINEMENT", findings=[], uncertainties=[uncertainty()])))

    def test_duplicate_identifiers_are_refused(self) -> None:
        self.assertNotEqual([], ir.result_errors(
            result("NEEDS_REFINEMENT", findings=[finding(), finding()])))
        self.assertNotEqual([], ir.result_errors(
            result(uncertainties=[uncertainty(material=False)] * 2)))

    def test_something_that_is_not_an_object_is_refused(self) -> None:
        for payload in (None, [], "READY", 3):
            with self.subTest(payload=payload):
                self.assertNotEqual([], ir.result_errors(payload))


class GateTests(unittest.TestCase):
    def test_only_a_confirmed_ready_continues(self) -> None:
        self.assertEqual(ir.CONTINUE, ir.assess(result()).gate)
        self.assertEqual(ir.CONTINUE, ir.assess(result(confidence="medium")).gate)

    def test_an_unconfirmed_ready_escalates_and_never_continues(self) -> None:
        for payload in (result(confidence="low"),
                        result(uncertainties=[uncertainty()])):
            with self.subTest(payload=payload):
                self.assertEqual(ir.ESCALATE, ir.assess(payload).gate)

    def test_a_resolved_or_minor_uncertainty_does_not_escalate(self) -> None:
        payload = result(uncertainties=[uncertainty(resolved=True),
                                        uncertainty(id="IU-002", material=False)])

        self.assertEqual(ir.CONTINUE, ir.assess(payload).gate)

    def test_every_other_result_stops(self) -> None:
        for payload in (result("NEEDS_REFINEMENT"), result("BLOCKED", confidence="low"),
                        result(findings=[finding()]), {"status": "READY"}, None):
            with self.subTest(payload=payload):
                self.assertEqual(ir.STOP, ir.assess(payload).gate)

    def test_telemetry_fields_are_counts_and_tokens(self) -> None:
        readiness = ir.assess(result("NEEDS_REFINEMENT", uncertainties=[uncertainty()]))

        fields = readiness.telemetry_fields()
        self.assertEqual({
            "readiness_result_valid": True, "readiness_confidence": "high",
            "readiness_findings_total": 1, "readiness_findings_blocking": 1,
            "readiness_uncertainties_material": 1,
        }, fields)
        for key, value in fields.items():
            tm.validate_reference(key, value)
        self.assertEqual({"readiness_result_valid": False},
                         ir.assess({"status": "READY"}).telemetry_fields())


class ModeTests(unittest.TestCase):
    def test_the_default_is_auto(self) -> None:
        for config in (None, {}, {"code_cycle": {}}, {"code_cycle": {"issue_review": {}}}):
            with self.subTest(config=config):
                self.assertIs(ir.IssueReviewMode.AUTO, ir.load_issue_review_mode(config))

    def test_off_is_supported(self) -> None:
        self.assertIs(ir.IssueReviewMode.OFF, ir.load_issue_review_mode(
            {"code_cycle": {"issue_review": {"mode": "off"}}}))

    def test_a_malformed_setting_is_refused(self) -> None:
        for declared in ("off", {"mode": "sometimes"}, {"mode": "off", "profile": "x"}):
            with self.subTest(declared=declared):
                with self.assertRaises(ir.IssueReviewConfigError):
                    ir.load_issue_review_mode({"code_cycle": {"issue_review": declared}})

    def test_only_declared_trivial_non_sensitive_work_is_skipped(self) -> None:
        auto = ir.IssueReviewMode.AUTO
        self.assertEqual("skipped", ir.dispatch_decision(auto, router.TaskSignals(difficulty=1)))
        self.assertEqual("dispatched", ir.dispatch_decision(
            auto, router.TaskSignals(difficulty=1, security_sensitive=True)))
        # Unclassified work carries the router's default difficulty, 2.
        self.assertEqual("dispatched", ir.dispatch_decision(auto, router.TaskSignals()))
        self.assertEqual("off", ir.dispatch_decision(
            ir.IssueReviewMode.OFF, router.TaskSignals(difficulty=3)))


class RoutingTests(unittest.TestCase):
    READY = {"codex": router.Availability.READY, "claude": router.Availability.READY}

    def profile(self, signals, **kw) -> str:
        return router.route("issue_review", signals, self.READY, **kw).profile

    def test_ordinary_work_uses_the_standard_reviewer(self) -> None:
        self.assertEqual("reviewer", self.profile(router.TaskSignals()))

    def test_high_risk_work_uses_the_stronger_profile(self) -> None:
        self.assertEqual("senior_reviewer", self.profile(router.TaskSignals(difficulty=3)))
        self.assertEqual("senior_reviewer",
                         self.profile(router.TaskSignals(security_sensitive=True)))

    def test_escalation_names_the_stronger_profile_only_for_issue_review(self) -> None:
        self.assertEqual("senior_reviewer", self.profile(
            router.TaskSignals(), selector=router.escalation_selector))
        for role in ("implement", "review", "security"):
            with self.subTest(role=role):
                self.assertEqual(
                    router.route(role, router.TaskSignals(), self.READY),
                    router.route(role, router.TaskSignals(), self.READY,
                                 selector=router.escalation_selector))

    def test_the_role_is_read_only_and_publishes_nothing(self) -> None:
        contract = role_contract("issue_review")

        self.assertIs(ex.WorkspacePolicy.READ_ONLY, contract.workspace_policy)
        self.assertFalse(contract.publishes)
        self.assertEqual((), contract.publication_permissions)

    def test_a_caller_cannot_widen_the_contract(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = tm.Telemetry(Path(temporary.name) / "t.sqlite")
        recorder = CycleRecorder(store, "owner/api", "API-7", router.TaskSignals(),
                                 availability=self.READY,
                                 registry=ex.Registry([Talker("codex")]))
        for kwargs in ({"writes": True}, {"publishes": True},
                       {"publication_permissions": ("comment",)},
                       {"workspace_policy": "workspace_write"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(CycleError):
                    recorder.stage("issue_review", "task", **kwargs)


class Answering(Talker):
    """Answers the issue review from a queue, and every other stage normally."""

    def __init__(self, name: str, readiness: list[str] | None = None,
                 review: str = APPROVED) -> None:
        super().__init__(name)
        self.readiness = list(readiness or [])
        self.review = review
        self.permissions: list[tuple] = []

    def spoken(self, task: str) -> str:
        if "cc-issue-review" in task:
            return self.readiness.pop(0) if self.readiness else spoken(result())
        for skill, answer in COMPLETED.items():
            if skill in task:
                return answer
        return self.review

    def dispatch(self, target, task, **kw):
        self.permissions.append((kw.get("writes"), kw.get("publishes"),
                                 kw.get("publication_permissions")))
        return super().dispatch(target, task, **kw)


class DriverTests(RunCycleTestCase):
    """The stage inside `run_cycle`, with scripted executors."""

    def cycle(self, readiness=None, *, signals=None, **kw):
        # `reviewer` runs on claude in these tests; `senior_reviewer` keeps its
        # default, codex, so an escalation is visible as a change of executor.
        self.codex = Answering("codex", readiness if readiness is not None else [])
        self.claude = Answering("claude")
        self.claude.readiness = self.codex.readiness
        kw.setdefault("issue_review", "auto")
        return rc.run_cycle(
            "owner/api", "API-7", signals or router.TaskSignals(), self.store,
            registry=ex.Registry([self.codex, self.claude]),
            availability={"codex": ex.Availability.READY,
                          "claude": ex.Availability.READY},
            profiles=router.load_profiles({"code_cycle": {"profiles": {
                "reviewer": {"primary": "claude:anthropic/claude-sonnet-5 high"}}}}),
            **kw,
        )

    def roles(self):
        return [(row["role"], row["payload"].get("record_kind")) for row in self.rows()]

    def cycle_row(self):
        return self.rows()[-1]["payload"]

    def implemented(self) -> bool:
        return any("cc-implement-issue" in task
                   for adapter in (self.codex, self.claude) for task in adapter.dispatched)

    def test_a_confirmed_ready_reaches_implementation(self) -> None:
        report = self.cycle()

        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual([("issue_review", "dispatch"), ("issue_review", "verdict"),
                          ("implement", "dispatch")], self.roles()[:3])
        verdict = self.rows()[1]
        self.assertEqual("READY", verdict["status"])
        self.assertEqual("high", verdict["payload"]["readiness_confidence"])
        self.assertEqual("dispatched", self.cycle_row()["issue_review"])
        self.assertEqual("implement", self.cycle_row()["started_from"])

    def test_the_prompt_names_the_skill_and_forbids_publication(self) -> None:
        self.cycle()

        prompt = self.claude.dispatched[0]
        self.assertIn("Run cc-issue-review for API-7 in owner/api.", prompt)
        self.assertIn("it publishes nothing", prompt)
        self.assertIn("ORCHESTRATION_RESULT", prompt)
        self.assertEqual((False, False, ()), self.claude.permissions[0])

    def test_needs_refinement_stops_before_implementation_with_its_evidence(self) -> None:
        report = self.cycle([spoken(result("NEEDS_REFINEMENT"))])

        self.assertFalse(self.implemented())
        self.assertEqual(rc.UNRESOLVED_END, report.status)
        self.assertEqual("needs_refinement", report.stop_reason)
        self.assertEqual("needs_refinement", self.cycle_row()["stop_reason"])
        explained = report.explain()
        self.assertIn("IR-001 high blocks acceptance_verification", explained)
        self.assertIn("work_item:API-7", explained)
        self.assertIn("proposed: SECRET-PROSE state the observable result", explained)

    def test_blocked_missing_and_malformed_results_stop_before_implementation(self) -> None:
        for body in (spoken(result("BLOCKED", confidence="low")), "no block at all",
                     spoken({"status": "READY"}),
                     spoken(result(findings=[finding()])),
                     "ORCHESTRATION_RESULT\n[1]\nEND_ORCHESTRATION_RESULT"):
            with self.subTest(body=body):
                self.setUp()
                report = self.cycle([body])
                self.assertFalse(self.implemented())
                self.assertEqual("stage_not_completed", report.stop_reason)

    def test_a_malformed_ready_is_recorded_as_invalid(self) -> None:
        self.cycle([spoken({"status": "READY"})])

        verdict = self.rows()[1]
        self.assertEqual("READY", verdict["status"])
        self.assertEqual({"readiness_result_valid": False},
                         {key: value for key, value in verdict["payload"].items()
                          if key.startswith("readiness_")})

    def test_an_unconfirmed_ready_escalates_once_to_the_stronger_profile(self) -> None:
        report = self.cycle([spoken(result(confidence="low")), spoken(result())])

        self.assertEqual(rc.APPROVED_END, report.status)
        dispatches = [row for row in self.rows()
                      if row["role"] == "issue_review"
                      and row["payload"]["record_kind"] == "dispatch"]
        self.assertEqual(["reviewer", "senior_reviewer"],
                         [row["profile"] for row in dispatches])
        self.assertNotIn("escalated", dispatches[0]["payload"])
        self.assertIs(True, dispatches[1]["payload"]["escalated"])
        self.assertIn("reported READY without confirming it", self.codex.dispatched[0])
        self.assertTrue(self.implemented())

    def test_a_second_unconfirmed_ready_stops_for_a_person(self) -> None:
        report = self.cycle([spoken(result(confidence="low")),
                             spoken(result(uncertainties=[uncertainty()]))])

        self.assertFalse(self.implemented())
        self.assertEqual("readiness_unconfirmed", report.stop_reason)
        self.assertEqual(2, sum(1 for role, kind in self.roles()
                                if role == "issue_review" and kind == "dispatch"))
        self.assertIn("uncertainty: IU-001 material unresolved", report.explain())

    def test_high_risk_work_starts_on_the_stronger_profile_and_does_not_escalate(self) -> None:
        report = self.cycle([spoken(result(confidence="low"))],
                            signals=router.TaskSignals(difficulty=3))

        self.assertFalse(self.implemented())
        self.assertEqual("readiness_unconfirmed", report.stop_reason)
        self.assertEqual("senior_reviewer", self.rows()[0]["profile"])
        self.assertEqual(1, sum(1 for role, kind in self.roles()
                                if role == "issue_review" and kind == "dispatch"))

    def test_declared_trivial_work_skips_the_review(self) -> None:
        report = self.cycle(signals=router.TaskSignals(difficulty=1))

        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertNotIn("issue_review", [role for role, _ in self.roles()])
        self.assertEqual("skipped", self.cycle_row()["issue_review"])

    def test_trivial_but_sensitive_work_is_reviewed_by_the_stronger_profile(self) -> None:
        self.cycle(signals=router.TaskSignals(difficulty=1, security_sensitive=True))

        self.assertEqual(("issue_review", "senior_reviewer"),
                         (self.rows()[0]["role"], self.rows()[0]["profile"]))

    def test_off_keeps_the_original_flow(self) -> None:
        self.cycle(issue_review="off")

        self.assertEqual("implement", self.rows()[0]["role"])
        self.assertNotIn("issue_review", [role for role, _ in self.roles()])
        self.assertEqual("off", self.cycle_row()["issue_review"])

    def test_a_resumed_cycle_never_reviews_the_work_item(self) -> None:
        self.cycle(start_from="review", change_request_id="4")

        self.assertEqual("review", self.rows()[0]["role"])
        self.assertNotIn("issue_review", [role for role, _ in self.roles()])
        self.assertNotIn("issue_review", self.cycle_row())

    def test_no_prose_from_the_review_reaches_the_store(self) -> None:
        self.cycle([spoken(result("NEEDS_REFINEMENT", uncertainties=[uncertainty()],
                                  summary="SECRET-PROSE summary"))])

        with sqlite3.connect(self.store.path) as connection:
            dump = "\n".join(str(row) for row in connection.execute("SELECT * FROM stages"))
        self.assertNotIn("SECRET-PROSE", dump)
        self.assertNotIn("IR-001", dump)

    def test_a_test_claim_in_the_review_result_is_not_a_test_outcome(self) -> None:
        self.cycle([spoken({**result(), "tests": {"passed": True}})])

        self.assertFalse(self.implemented())
        self.assertNotIn("tests_passed", self.rows()[1]["payload"])
        self.assertNotIn("tests_passed", self.cycle_row())

    def test_an_issue_review_verdict_leaves_no_prior_findings_for_implementation(self) -> None:
        self.cycle([spoken(result(findings=[finding(blocks_readiness=False)]))])

        implement = next(row for row in self.rows() if row["role"] == "implement")
        self.assertFalse(any(key.startswith("prior_findings") for key in implement["payload"]))


class ConceptualFixtureTests(RunCycleTestCase):
    """#92, #90 and #91: a trivial, a moderate and a high-risk work item."""

    def signals(self, case: dict) -> router.TaskSignals:
        return router.TaskSignals(**case["signals"])

    def test_every_fixture_routes_as_declared(self) -> None:
        ready = {"codex": router.Availability.READY, "claude": router.Availability.READY}
        for name in ("issue-92-trivial.json", "issue-90-moderate.json",
                     "issue-91-high-risk.json"):
            case = fixture(name)
            with self.subTest(work_item=case["work_item"]):
                signals = self.signals(case)
                self.assertEqual(case["expected_decision"], ir.dispatch_decision(
                    ir.IssueReviewMode.AUTO, signals))
                if case["expected_profile"] is not None:
                    self.assertEqual(case["expected_profile"],
                                     router.route("issue_review", signals, ready).profile)
                if case["result"] is not None:
                    self.assertEqual([], ir.result_errors(case["result"]))

    def test_92_skips_the_review_and_implements_directly(self) -> None:
        codex, claude = Answering("codex"), Answering("claude")
        rc.run_cycle(
            "owner/api", "92", self.signals(fixture("issue-92-trivial.json")), self.store,
            registry=ex.Registry([codex, claude]),
            availability={"codex": ex.Availability.READY, "claude": ex.Availability.READY},
            issue_review="auto",
        )

        roles = [row["role"] for row in self.store.rows("owner/api")]
        self.assertEqual("implement", roles[0])
        self.assertNotIn("issue_review", roles)

    def test_90_is_ready_without_repeating_the_implementers_diagnosis(self) -> None:
        case = fixture("issue-90-moderate.json")
        readiness = ir.assess(case["result"])

        self.assertEqual(ir.CONTINUE, readiness.gate)
        self.assertNotIn("diagnosis", case["result"])
        # A result that tried to carry the implementer's diagnosis is refused.
        with_diagnosis = {**case["result"], "diagnosis": {"decision": "implement"}}
        self.assertEqual(ir.STOP, ir.assess(with_diagnosis).gate)

    def test_91_asks_for_the_detectors_full_invariant(self) -> None:
        case = fixture("issue-91-high-risk.json")
        readiness = ir.assess(case["result"])

        self.assertEqual(ir.STOP, readiness.gate)
        self.assertEqual("NEEDS_REFINEMENT", readiness.status)
        blocking = [item for item in readiness.findings if item["blocks_readiness"]]
        self.assertEqual(["acceptance_verification"], [item["dimension"] for item in blocking])
        text = (blocking[0]["summary"] + " " + blocking[0]["proposed_change"]).lower()
        # The invariant is "unchanged", and git status alone hides these.
        for hidden in case["hidden_change_classes"]:
            self.assertIn(hidden, text)
        self.assertIn("git status", text)
        self.assertTrue(any(item["kind"] == "repository"
                            and item["ref"].startswith("scripts/executors.py")
                            for item in blocking[0]["evidence"]))

    def test_91_low_confidence_ready_cannot_continue(self) -> None:
        case = fixture("issue-91-high-risk.json")
        ready = copy.deepcopy(case["result"])
        ready.update(status="READY", confidence="low", blocking=False)
        ready["findings"] = [item for item in ready["findings"]
                             if not item["blocks_readiness"]]

        self.assertEqual([], ir.result_errors(ready))
        self.assertNotEqual(ir.CONTINUE, ir.assess(ready).gate)

        codex = Answering("codex", [spoken(ready)])
        claude = Answering("claude")
        report = rc.run_cycle(
            "owner/api", "91", self.signals(case), self.store,
            registry=ex.Registry([codex, claude]),
            availability={"codex": ex.Availability.READY, "claude": ex.Availability.READY},
            issue_review="auto",
        )

        self.assertEqual("readiness_unconfirmed", report.stop_reason)
        self.assertFalse(any("cc-implement-issue" in task for task in codex.dispatched))


class ConfigurationTests(unittest.TestCase):
    def test_the_key_is_known_to_the_driver(self) -> None:
        self.assertIn("issue_review", rc.KNOWN_KEYS)

    def test_the_cli_flag_overrides_the_default(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        report = rc.CycleReport(repo_id="owner/api", task_id="API-7",
                                status=rc.APPROVED_END, verdict="APPROVED")
        for argv, expected in (([], ir.IssueReviewMode.AUTO),
                               (["--issue-review", "off"], ir.IssueReviewMode.OFF)):
            with self.subTest(argv=argv):
                with mock.patch.object(rc, "run_cycle", return_value=report) as run, \
                        contextlib.redirect_stdout(io.StringIO()):
                    rc.main(["--repo", "owner/api", "--task", "API-7", "--no-config",
                             "--database", str(Path(temporary.name) / "t.sqlite"), *argv])
                self.assertIs(expected, run.call_args.kwargs["issue_review"])


if __name__ == "__main__":
    unittest.main()
