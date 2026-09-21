"""The driver's own decisions, in process.

`test_installed_cycle.py` proves a real installation runs a cycle. These are the
cases that are easier to state than to stage: a stage that only started, and an
executor whose structured block is delimited, parseable and not a result.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import executors as ex  # noqa: E402
import router  # noqa: E402
import run_cycle as rc  # noqa: E402
import telemetry as tm  # noqa: E402
from test_cycle import ScriptedAdapter  # noqa: E402


APPROVED = 'ORCHESTRATION_RESULT\n{"status": "APPROVED"}\nEND_ORCHESTRATION_RESULT'


class Talker(ScriptedAdapter):
    """An executor that answers with whatever text the test gives it."""

    def __init__(self, name: str, body: str = APPROVED) -> None:
        super().__init__(name)
        self.body = body

    def dispatch(self, target, task, **kw):
        self.dispatched.append(task)
        return ex.DispatchResult(
            ex.DispatchOutcome.SUCCEEDED, self.name, target,
            model_resolved=target.model, artifacts={"stdout": self.body},
        )


class Starter(ScriptedAdapter):
    """An executor that launches work and hands back a reference to it."""

    completes_work = False

    def dispatch(self, target, task, **kw):
        self.dispatched.append(task)
        return ex.DispatchResult(
            ex.DispatchOutcome.SUCCEEDED, self.name, target,
            model_resolved=target.model, artifacts={"dispatchId": "D-7"},
            asynchronous=True,
        )


class RunCycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = tm.Telemetry(Path(temporary.name) / "t.sqlite")

    def run_cycle(self, implementer, reviewer, **kw):
        return rc.run_cycle(
            "owner/api", "API-7", router.TaskSignals(), self.store,
            registry=ex.Registry([implementer, reviewer]),
            availability={implementer.name: ex.Availability.READY,
                          reviewer.name: ex.Availability.READY},
            **kw,
        )

    def rows(self):
        return self.store.rows("owner/api")


class AsynchronousStageTests(RunCycleTestCase):
    """A started worker is not a finished stage."""

    def test_a_stage_that_only_started_does_not_advance_the_cycle(self) -> None:
        starter, reviewer = Starter("codex"), Talker("claude")

        report = self.run_cycle(starter, reviewer)

        self.assertEqual(["implement"], [stage.role for stage in report.stages])
        self.assertEqual([], reviewer.dispatched)
        self.assertEqual(rc.UNRESOLVED_END, report.status)

    def test_the_reason_names_the_reference_the_work_continues_under(self) -> None:
        report = self.run_cycle(Starter("codex"), Talker("claude"))

        self.assertIn("D-7", report.stopped_because)
        self.assertIn("finishes elsewhere", report.stopped_because)

    def test_the_cycle_still_closes_itself(self) -> None:
        """Stopping is a fact about the run, so it gets its row like any other."""
        self.run_cycle(Starter("codex"), Talker("claude"))

        last = self.rows()[-1]
        self.assertEqual("coordinate", last["role"])
        self.assertEqual(rc.UNRESOLVED_END, last["status"])

    def test_a_review_that_only_started_does_not_produce_a_verdict(self) -> None:
        report = self.run_cycle(Talker("codex"), Starter("claude"))

        self.assertEqual(["implement", "review"], [s.role for s in report.stages])
        self.assertIsNone(report.verdict)
        self.assertEqual(rc.UNRESOLVED_END, report.status)

    def test_the_dispatch_is_still_recorded(self) -> None:
        """What it did is worth knowing even though it is not a result."""
        self.run_cycle(Starter("codex"), Talker("claude"))

        first = self.rows()[0]
        self.assertEqual("implement", first["role"])
        self.assertEqual("succeeded", first["outcome"])


class MalformedResultTests(RunCycleTestCase):
    """Delimited, parseable, and not a result."""

    def blocks(self, body: str):
        return Talker("codex", body), Talker("claude", body)

    def test_a_block_holding_a_string_is_not_a_result(self) -> None:
        payload = rc.read_structured_result(ex.DispatchResult(
            ex.DispatchOutcome.SUCCEEDED, "claude", None,
            artifacts={"stdout": 'ORCHESTRATION_RESULT\n"APPROVED"\nEND_ORCHESTRATION_RESULT'},
        ))

        self.assertIsNone(payload)

    def test_a_block_holding_a_number_is_not_a_result(self) -> None:
        payload = rc.read_structured_result(ex.DispatchResult(
            ex.DispatchOutcome.SUCCEEDED, "claude", None,
            artifacts={"stdout": "ORCHESTRATION_RESULT\n3\nEND_ORCHESTRATION_RESULT"},
        ))

        self.assertIsNone(payload)

    def test_a_malformed_block_stops_the_cycle_instead_of_ending_it(self) -> None:
        """It used to raise, which skipped the closing row entirely."""
        implementer, reviewer = self.blocks(
            'ORCHESTRATION_RESULT\n"APPROVED"\nEND_ORCHESTRATION_RESULT')

        report = self.run_cycle(implementer, reviewer)

        self.assertEqual(rc.UNRESOLVED_END, report.status)
        self.assertIn("no structured verdict", report.stopped_because)
        self.assertEqual(rc.UNRESOLVED_END, self.rows()[-1]["status"])

    def test_an_echoed_prompt_does_not_shadow_the_real_block(self) -> None:
        """The request itself contains the words, so reading forwards found it."""
        decoy = ("prompt received: ... include the ORCHESTRATION_RESULT block\n"
                 + APPROVED)

        report = self.run_cycle(Talker("codex", decoy), Talker("claude", decoy))

        self.assertEqual("APPROVED", report.verdict)
        self.assertEqual(rc.APPROVED_END, report.status)


if __name__ == "__main__":
    unittest.main()
