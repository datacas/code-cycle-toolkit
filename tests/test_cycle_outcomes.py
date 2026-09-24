"""Observed outcomes of a cycle, correlated with its routing and never mixed in."""

from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import executors as ex  # noqa: E402
import run_cycle as rc  # noqa: E402
import telemetry as tm  # noqa: E402
from test_cycle import CycleTestCase, ScriptedAdapter  # noqa: E402
from test_run_cycle import RunCycleTestCase, Talker, block  # noqa: E402

OUTCOME_ONLY = set().union(*tm.OUTCOME_FIELDS.values())
SEVERITIES = dict(findings_critical=0, findings_high=1, findings_medium=1, findings_low=1)


class ReconstructionTests(CycleTestCase):
    def adapters(self, **kw):
        return [ScriptedAdapter("codex", **kw), ScriptedAdapter("claude")]

    def corrected_cycle(self):
        recorder = self.recorder(self.adapters())
        recorder.stage("implement", "work")
        recorder.record_verdict("implement", "IMPLEMENTED", tests_passed=True)
        recorder.stage("review", "review")
        recorder.record_verdict("review", "CHANGES_REQUESTED", findings_total=3,
                                findings_blocking=1, **SEVERITIES)
        recorder.next_iteration()
        recorder.stage("resolve", "resolve")
        recorder.record_verdict("resolve", "RESOLVED", tests_passed=False)
        recorder.stage("rereview", "rereview")
        recorder.record_verdict("rereview", "APPROVED", findings_total=0)
        recorder.close("READY_FOR_MANUAL_MERGE")
        return recorder

    def test_a_corrected_cycle_is_reconstructed_from_its_rows(self) -> None:
        recorder = self.corrected_cycle()

        cycle = self.store.cycle_outcome("owner/repo", recorder.cycle_id)

        self.assertTrue(cycle["closed"])
        self.assertEqual({
            "status": "READY_FOR_MANUAL_MERGE", "iterations": 1,
            "first_review_status": "CHANGES_REQUESTED", "first_pass_approved": False,
            "resolution_needed": True, "resolution_rounds": 1,
            "final_review_status": "APPROVED", "final_approved": True,
            "tests_passed": False, "fallback_stages": 0, "contract_violations": 0,
        }, cycle["outcome"])
        review = cycle["verdicts"][1]
        self.assertEqual({"stage_seq": 2, "role": "review", "status": "CHANGES_REQUESTED",
                          "findings_total": 3, "findings_blocking": 1, **SEVERITIES}, review)

    def test_a_verdict_points_at_the_dispatch_it_reports_on(self) -> None:
        recorder = self.corrected_cycle()

        cycle = self.store.cycle_outcome("owner/repo", recorder.cycle_id)

        dispatched = {(d["role"], d["stage_seq"]) for d in cycle["dispatches"]}
        self.assertEqual([("implement", 1), ("review", 2), ("resolve", 3), ("rereview", 4)],
                         sorted(dispatched, key=lambda item: item[1]))
        for verdict in cycle["verdicts"]:
            self.assertIn((verdict["role"], verdict["stage_seq"]), dispatched)
        # The rules' choice is on the dispatch row, where a later suggestion can
        # be compared with it by (cycle_id, stage_seq).
        self.assertEqual("cheap_coder", cycle["dispatches"][0]["profile"])

    def test_a_first_pass_approval_needed_no_resolution(self) -> None:
        recorder = self.recorder(self.adapters())
        recorder.stage("implement", "work")
        recorder.stage("review", "review")
        recorder.record_verdict("review", "APPROVED", findings_total=0)
        recorder.close("READY_FOR_MANUAL_MERGE")

        outcome = self.store.cycle_outcome("owner/repo", recorder.cycle_id)["outcome"]

        self.assertIs(True, outcome["first_pass_approved"])
        self.assertIs(False, outcome["resolution_needed"])
        self.assertEqual(0, outcome["resolution_rounds"])
        self.assertIs(True, outcome["final_approved"])


class UnknownIsNotDefaultTests(CycleTestCase):
    def test_a_cycle_that_never_reached_a_verdict_is_not_judged(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])
        recorder.stage("implement", "work")
        recorder.close("HUMAN_INTERVENTION")

        outcome = self.store.cycle_outcome("owner/repo", recorder.cycle_id)["outcome"]

        for absent in ("first_pass_approved", "resolution_needed", "resolution_rounds",
                       "final_approved", "first_review_status", "tests_passed"):
            with self.subTest(field=absent):
                self.assertNotIn(absent, outcome)
        self.assertEqual(0, outcome["contract_violations"])

    def test_a_review_that_reported_blocked_is_no_review_outcome(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])
        recorder.stage("review", "review")
        recorder.record_verdict("review", "BLOCKED")
        recorder.close("HUMAN_INTERVENTION")

        outcome = self.store.cycle_outcome("owner/repo", recorder.cycle_id)["outcome"]

        self.assertNotIn("first_pass_approved", outcome)
        self.assertNotIn("final_approved", outcome)

    def test_a_cycle_that_never_closed_has_no_outcome(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])
        recorder.stage("review", "review")
        recorder.record_verdict("review", "APPROVED")

        cycle = self.store.cycle_outcome("owner/repo", recorder.cycle_id)

        self.assertFalse(cycle["closed"])
        self.assertEqual({}, cycle["outcome"])

    def test_a_verdict_without_findings_does_not_report_zero(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])
        recorder.stage("review", "review")
        recorder.record_verdict("review", "APPROVED")

        verdict = self.store.cycle_outcome("owner/repo", recorder.cycle_id)["verdicts"][0]

        self.assertEqual({"stage_seq": 1, "role": "review", "status": "APPROVED"}, verdict)


class DispatchObservationTests(CycleTestCase):
    def test_fallbacks_and_contract_violations_are_counted(self) -> None:
        codex = ScriptedAdapter("codex", outcomes=[
            (ex.DispatchOutcome.BLOCKED, "operating_quota"),
            (ex.DispatchOutcome.CONTRACT_VIOLATION, None),
        ])
        recorder = self.recorder([codex, ScriptedAdapter("claude")])
        recorder.stage("implement", "work")  # quota, then the fallback
        recorder.availability["codex"] = ex.Availability.READY
        recorder.stage("implement", "again")  # the wrong model answered
        recorder.close("HUMAN_INTERVENTION")

        outcome = self.store.cycle_outcome("owner/repo", recorder.cycle_id)["outcome"]

        self.assertEqual(1, outcome["fallback_stages"])
        self.assertEqual(1, outcome["contract_violations"])


class SeparationTests(CycleTestCase):
    def test_dispatch_rows_carry_no_outcome_and_are_never_rewritten(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])
        recorder.stage("implement", "work")
        recorder.stage("review", "review")
        before = [row for row in self.store.rows("owner/repo")]

        recorder.record_verdict("review", "APPROVED", findings_total=0, tests_passed=True)
        recorder.close("READY_FOR_MANUAL_MERGE")

        after = self.store.rows("owner/repo")
        self.assertEqual(before, after[:len(before)])
        for row in before:
            with self.subTest(role=row["role"]):
                self.assertEqual("dispatch", row["payload"]["record_kind"])
                self.assertIsNone(row["status"])
                self.assertEqual(set(), set(row["payload"]) & OUTCOME_ONLY)

    def test_outcomes_and_pre_routing_signals_do_not_overlap(self) -> None:
        pre_routing = set().union(*tm.PRE_ROUTING_SIGNALS.values())
        self.assertEqual(set(), pre_routing & OUTCOME_ONLY)
        self.assertEqual(set(), pre_routing & tm.CORRELATION_FIELDS)
        self.assertTrue(OUTCOME_ONLY | tm.CORRELATION_FIELDS <= set(tm.FIELD_SPECS))

    def test_two_runs_of_one_work_item_stay_apart(self) -> None:
        first = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])
        second = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")])
        first.stage("implement", "work")
        second.stage("implement", "work")
        first.close("HUMAN_INTERVENTION")

        self.assertNotEqual(first.cycle_id, second.cycle_id)
        self.assertTrue(self.store.cycle_outcome("owner/repo", first.cycle_id)["closed"])
        self.assertFalse(self.store.cycle_outcome("owner/repo", second.cycle_id)["closed"])


class AllowlistTests(CycleTestCase):
    def test_outcome_fields_accept_only_their_types(self) -> None:
        for field, value in (("first_review_status", "LGTM"), ("final_approved", "yes"),
                             ("resolution_rounds", -1), ("tests_passed", {"passed": True}),
                             ("record_kind", "note")):
            with self.subTest(field=field), self.assertRaises(tm.TelemetryError):
                self.store.record_stage("owner/repo", "API-055", "coordinate", **{field: value})

    def test_a_cycle_id_is_refused_before_anything_is_dispatched(self) -> None:
        codex = ScriptedAdapter("codex")
        with self.assertRaises(tm.TelemetryError):
            self.recorder([codex, ScriptedAdapter("claude")], cycle_id="sk-live-abc")
        with self.assertRaises(tm.TelemetryError):
            self.recorder([codex, ScriptedAdapter("claude")], cycle_id="two words")
        self.assertEqual([], codex.dispatched)

    def test_an_explicit_cycle_id_is_kept(self) -> None:
        recorder = self.recorder([ScriptedAdapter("codex"), ScriptedAdapter("claude")],
                                 cycle_id="run-2026-09-23-1")
        recorder.close("HUMAN_INTERVENTION")

        self.assertIsNotNone(self.store.cycle_outcome("owner/repo", "run-2026-09-23-1"))


class LegacyRowTests(unittest.TestCase):
    def test_rows_from_before_cycles_existed_stay_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = tm.Telemetry(Path(directory) / "t.sqlite")
            with unittest.mock.patch.object(tm, "SCHEMA_VERSION", 2):
                store.record_stage("repo", "T-1", "review", status="APPROVED",
                                   changed_files_count=2)

            row = store.rows("repo")[0]

            self.assertEqual(2, row["schema_version"])
            self.assertNotIn("cycle_id", row["payload"])
            self.assertIsNone(store.cycle_outcome("repo", "cycle-anything"))


class DriverTests(RunCycleTestCase):
    def test_every_documented_verdict_outcome_is_one_the_driver_records(self) -> None:
        """REV-001: a documented outcome nothing writes is a promise, not data."""
        payload = {
            "status": "CHANGES_REQUESTED", "tests": {"passed": False},
            "checks": {"passed": 4, "failed": 1},
            "unresolved_findings": [{"severity": "high", "blocks_approval": True}],
        }

        emitted = {"status", *rc._findings(payload), *rc._tests(payload)}

        self.assertEqual(tm.OUTCOME_FIELDS["verdict"], emitted)

    def cycle_row(self):
        return [row for row in self.rows() if row["payload"].get("record_kind") == "cycle"][-1]

    def test_a_reported_test_result_reaches_the_verdict_and_the_cycle(self) -> None:
        implementer = Talker("codex", block("IMPLEMENTED", tests={"passed": True}))

        report = self.run_cycle(implementer, Talker("claude"))

        self.assertTrue(report.approved)
        cycle = self.store.cycle_outcome("owner/api", self.cycle_row()["payload"]["cycle_id"])
        self.assertIs(True, cycle["verdicts"][0]["tests_passed"])
        self.assertIs(True, cycle["outcome"]["tests_passed"])
        self.assertIs(True, cycle["outcome"]["first_pass_approved"])

    def test_an_unreadable_test_result_is_not_a_pass(self) -> None:
        implementer = Talker("codex", block("IMPLEMENTED", tests={"passed": "yes"}))

        self.run_cycle(implementer, Talker("claude"))

        self.assertNotIn("tests_passed", self.cycle_row()["payload"])

    def test_an_implementation_blocked_before_changing_code_records_no_test_result(self) -> None:
        """#53: `passed: false` from a stage that never ran them is not a failure."""
        implementer = Talker("codex", block("BLOCKED", tests={"passed": False}))

        self.run_cycle(implementer, Talker("claude"))

        verdicts = [row for row in self.rows()
                    if row["payload"].get("record_kind") == "verdict"]
        self.assertEqual(["BLOCKED"], [row["status"] for row in verdicts])
        self.assertNotIn("tests_passed", verdicts[0]["payload"])
        self.assertNotIn("tests_passed", self.cycle_row()["payload"])


class TestsReportedTests(unittest.TestCase):
    """Which reported `tests` values are a test outcome, and which are not."""

    def test_executed_tests_that_failed_are_recorded_as_failed(self) -> None:
        for payload in (
            {"status": "IMPLEMENTED", "tests": {"passed": False}},
            {"status": "IMPLEMENTED", "tests": {"ran": True, "passed": False}},
            {"status": "BLOCKED", "tests": {"ran": True, "passed": False}},
        ):
            with self.subTest(payload=payload):
                self.assertEqual({"tests_passed": False}, rc._tests(payload))

    def test_executed_tests_that_passed_are_recorded_as_passed(self) -> None:
        for payload in (
            {"status": "IMPLEMENTED", "tests": {"passed": True}},
            {"status": "IMPLEMENTED", "tests": {"ran": True, "passed": True}},
            {"status": "BLOCKED", "tests": {"passed": True}},
        ):
            with self.subTest(payload=payload):
                self.assertEqual({"tests_passed": True}, rc._tests(payload))

    def test_tests_that_never_ran_record_nothing(self) -> None:
        for payload in (
            {"status": "IMPLEMENTED", "tests": {"ran": False, "passed": False}},
            {"status": "IMPLEMENTED", "tests": {"ran": False}},
            {"status": "BLOCKED", "tests": {"passed": False}},
            {"status": "blocked", "tests": {"passed": False}},
            {"status": "BLOCKED"},
        ):
            with self.subTest(payload=payload):
                self.assertEqual({}, rc._tests(payload))


if __name__ == "__main__":
    unittest.main()
