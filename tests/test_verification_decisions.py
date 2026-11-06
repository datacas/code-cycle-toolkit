"""Required verification decisions use journal state, never plan policy."""
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cycle_plan import PlanRuntime
from cycle_journal import Journal, events
from cycle_history import plan_projection, restore_plan, plan_digest
from execution_profiles import ExecutionSettings, PlanSignals, build_plan, resolve_execution
from verification_plan import Check, CheckResult, Criteria, VerificationPlan
import cycle_status
import telemetry
import stats


class VerificationDecisionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "journal.sqlite"
        self.head = "a" * 40
        self.check = Check("required", "python3 -m unittest", "codex", "deep_coder", "project", True, ("AC-1",))
        self.plan = VerificationPlan(Criteria("github", "b" * 64, "evaluable", ("works",)), (self.check,))
        self.plan = self.plan.record(CheckResult(self.check.id, self.head, "unverified", cause="environment"))

    def session(self, plan=None, timeout=600, answers=None):
        runtime = PlanRuntime()
        runtime.state = SimpleNamespace(verification_plan=plan or self.plan)
        runtime.execution_plan = replace(build_plan(resolve_execution(ExecutionSettings(), ExecutionSettings()),
            repository="owner/repo", work_item="414", signals=PlanSignals()), verification=runtime.state.verification_plan)
        runtime.telemetry = SimpleNamespace(path=self.path)
        runtime.repo_id, runtime.task_id = "owner/repo", "414"
        runtime.timeout = timeout
        runtime.user_tool_choices = None
        runtime.consent_scopes = None
        runtime.verification_answers = answers or {}
        runtime.verification_resume = False
        runtime.verification_retry = False
        runtime.authorized_bindings = lambda: (self.check.command,)
        runtime.report = SimpleNamespace(pending_verification=[], verification_questions=[])
        runtime.journal = Journal(self.path, "owner/repo", "414", "cycle-test")
        runtime.stop = Mock(return_value="stopped")
        return runtime

    def test_two_resumes_do_not_repeat_question_or_dispatch(self):
        for _ in range(3):
            session = self.session()
            self.assertEqual(session.verification_decision(self.head), "stopped")
            session.verification_resume = True
            session.verification_barrier = lambda: session.verification_decision(self.head)
            self.assertEqual(session.verify_pending(), "stopped")
        asked = events(self.path, "owner/repo", task_id="414", kinds=("decision_asked",))
        self.assertEqual(len(asked), 1)

    def test_acceptance_persists_without_changing_verification_or_plan(self):
        session = self.session()
        before = session.execution_plan.to_dict()
        session.verification_decision(self.head)
        key = session.report.verification_questions[0]["id"]
        session = self.session(answers={key: "accept"})
        self.assertIsNone(session.verification_decision(self.head))
        self.assertEqual(session.state.verification_plan.current(self.check.id, self.head), "unverified")
        self.assertEqual(session.execution_plan.to_dict(), before)
        resumed = self.session()
        self.assertIsNone(resumed.verification_decision(self.head))
        self.assertEqual(resumed.report.pending_verification[0]["decision"], "accept")
        rows = events(self.path, "owner/repo", task_id="414", kinds=("decision_answered",))
        self.assertEqual(rows[0]["correlation"]["head_sha"], self.head)
        self.assertEqual(rows[0]["correlation"]["check"], self.check.id)
        self.assertIn("conditions", rows[0]["correlation"])

    def test_changed_head_or_check_invalidates_answer(self):
        session = self.session()
        session.verification_decision(self.head)
        key = session.report.verification_questions[0]["id"]
        self.session(answers={key: "accept"}).verification_decision(self.head)
        changed = replace(self.plan, checks=(replace(self.check, command="python3 -m unittest tests"),))
        for plan, head in ((changed, self.head), (self.plan, "c" * 40)):
            self.assertEqual(self.session(plan).verification_decision(head), "stopped")

    def test_changed_conditions_permit_one_retry(self):
        self.session().verification_decision(self.head)
        session = self.session(timeout=1200)
        session.verification_resume = True
        self.assertIsNone(session.verification_decision(self.head))
        self.assertTrue(session.verification_retry)
        resumed = self.session(timeout=1200)
        resumed.verification_resume = True
        self.assertEqual(resumed.verification_decision(self.head), "stopped")
        self.assertFalse(resumed.verification_retry)

    def test_outside_option_is_unavailable_and_never_executes(self):
        session = self.session()
        session.verification_decision(self.head)
        key = session.report.verification_questions[0]["id"]
        session = self.session(answers={key: "outside"})
        self.assertEqual(session.verification_decision(self.head), "stopped")
        self.assertIn("unavailable", session.stop.call_args.args[0])

    def test_timeout_records_command_limit_and_evidence(self):
        session = self.session()
        session.allowed_test_command = None
        session.plan_head = self.head
        session.save_plan = Mock()
        session.verification_barrier = Mock(return_value="stopped")
        outcome = SimpleNamespace(result=SimpleNamespace(failure_summary=lambda: "partial output; process reaped"))
        self.assertEqual(session.verification_timeout(outcome), "stopped")
        result = session.state.verification_plan.results[-1]
        self.assertEqual((result.result, result.cause), ("unverified", "time_limit"))
        self.assertEqual(result.evidence[0]["command"], self.check.command)
        self.assertEqual(result.evidence[0]["limit_seconds"], 600)
        self.assertIn("process reaped", result.attempts[0]["evidence"])
        projected = plan_projection(replace(session.execution_plan, verification=session.state.verification_plan))
        record = {"payload": projected, "digest": plan_digest(projected)}
        restored = restore_plan(record, (self.check.command,))
        self.assertEqual(restored.verification.results[-1].cause, "time_limit")
        self.assertEqual(restored.verification.results[-1].evidence[0]["limit_seconds"], 600)

    def test_terminal_status_is_non_successful_and_serializable(self):
        self.assertEqual(telemetry.SCHEMA_VERSION, 29)
        self.assertEqual(cycle_status._result_icon("VERIFICATION_INCOMPLETE"), "⚠️")
        self.assertNotIn("VERIFICATION_INCOMPLETE", cycle_status._SUCCESS_RESULTS)
        from run_cycle import CycleReport
        report = CycleReport("owner/repo", "414", status="VERIFICATION_INCOMPLETE")
        self.assertFalse(report.approved)
        self.assertEqual(json.loads(json.dumps(report.__dict__))["status"], "VERIFICATION_INCOMPLETE")


if __name__ == "__main__":
    unittest.main()
