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


class Args:
    """What argparse would have produced, without the parser."""

    def __init__(self, **fields) -> None:
        self.repo = None
        self.cwd = None
        self.config = None
        self.no_config = False
        self.__dict__.update(fields)


class ConfigurationTests(RunCycleTestCase):
    """The repository's declaration reaches the router, or it decides nothing."""

    def setUp(self) -> None:
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def write(self, body: str) -> Path:
        path = self.directory / ".code-cycle.yml"
        path.write_text(body, encoding="utf-8")
        return path

    def profiles_from(self, body: str) -> dict:
        return rc.plan(Args(cwd=str(self.directory), repo="owner/api"))[1]

    def test_a_declared_profile_is_what_actually_runs(self) -> None:
        """The whole point: without this the row describes a policy nobody chose."""
        self.write("""
code_cycle:
  profiles:
    cheap_coder:
      primary: claude:anthropic/claude-sonnet-5 high
""")
        codex, claude = Talker("codex"), Talker("claude")

        report = rc.run_cycle(
            "owner/api", "API-7", router.TaskSignals(), self.store,
            profiles=self.profiles_from(""),
            registry=ex.Registry([codex, claude]),
            availability={"codex": ex.Availability.READY,
                          "claude": ex.Availability.READY},
        )

        implement = report.stages[0]
        self.assertEqual("claude", implement.result.executor)
        self.assertEqual("claude-sonnet-5", implement.decision.target.model)
        self.assertEqual([], codex.dispatched)

    def test_without_a_declaration_the_defaults_are_untouched(self) -> None:
        report = rc.run_cycle(
            "owner/api", "API-7", router.TaskSignals(), self.store,
            profiles=rc.plan(Args(no_config=True, repo="owner/api"))[1],
            registry=ex.Registry([Talker("codex"), Talker("claude")]),
            availability={"codex": ex.Availability.READY,
                          "claude": ex.Availability.READY},
        )

        implement = report.stages[0]
        self.assertEqual("codex", implement.result.executor)
        self.assertEqual("gpt-5.6-luna", implement.decision.target.model)

    def test_the_fallback_is_the_configured_one_not_the_built_in_one(self) -> None:
        """A reroute must stay inside the policy the repository declared."""
        self.write("""
code_cycle:
  profiles:
    cheap_coder:
      primary: codex:openai/gpt-5.6-luna high
      fallback: claude:anthropic/claude-opus-5 high
""")
        codex = ScriptedAdapter(
            "codex", outcomes=[(ex.DispatchOutcome.BLOCKED, "operating_quota")])
        claude = Talker("claude")

        report = rc.run_cycle(
            "owner/api", "API-7", router.TaskSignals(), self.store,
            profiles=self.profiles_from(""),
            registry=ex.Registry([codex, claude]),
            availability={"codex": ex.Availability.READY,
                          "claude": ex.Availability.READY},
        )

        fallback = report.stages[0].attempts[1][0]
        self.assertEqual("claude-opus-5", fallback.target.model)
        self.assertTrue(fallback.used_fallback)

    def test_an_explicit_repository_wins_over_the_declared_one(self) -> None:
        self.write("""
code_cycle:
  repository:
    selector: org-name/service
""")
        repo, _ = rc.plan(Args(cwd=str(self.directory), repo="owner/api"))

        self.assertEqual("owner/api", repo)

    def test_the_declared_repository_is_used_when_none_is_given(self) -> None:
        self.write("""
code_cycle:
  repository:
    selector: org-name/service
""")
        repo, _ = rc.plan(Args(cwd=str(self.directory)))

        self.assertEqual("org-name/service", repo)

    def test_no_repository_anywhere_is_an_error_before_anything_runs(self) -> None:
        with self.assertRaises(rc.CycleDriverError) as refused:
            rc.plan(Args(cwd=str(self.directory)))

        self.assertIn("no repository", str(refused.exception))

    def test_an_unknown_profile_name_stops_the_run_before_dispatch(self) -> None:
        self.write("""
code_cycle:
  profiles:
    cheep_coder:
      primary: codex:openai/gpt-5.6-luna high
""")
        with self.assertRaises(rc.CycleDriverError) as refused:
            rc.plan(Args(cwd=str(self.directory), repo="owner/api"))

        self.assertIn("unknown profile names", str(refused.exception))

    def test_unreadable_configuration_is_refused_not_ignored(self) -> None:
        """Running on the defaults while a file says otherwise would record a
        policy nobody declared."""
        self.write("code_cycle: [this is not a mapping")

        with self.assertRaises(rc.CycleDriverError) as refused:
            rc.plan(Args(cwd=str(self.directory), repo="owner/api"))

        self.assertIn("could not be read", str(refused.exception))

    def test_a_named_configuration_that_is_not_there_is_an_error(self) -> None:
        with self.assertRaises(rc.CycleDriverError):
            rc.plan(Args(config=str(self.directory / "absent.yml"), repo="owner/api"))

    def test_the_defaults_can_be_asked_for_explicitly(self) -> None:
        self.write("""
code_cycle:
  profiles:
    cheap_coder:
      primary: claude:anthropic/claude-sonnet-5 high
""")
        _, profiles = rc.plan(Args(cwd=str(self.directory), repo="owner/api",
                                   no_config=True))

        self.assertEqual("codex", profiles["cheap_coder"].primary.executor)

    def test_asking_for_both_at_once_is_refused(self) -> None:
        with self.assertRaises(rc.CycleDriverError):
            rc.plan(Args(config=str(self.write("code_cycle: {}")),
                         no_config=True, repo="owner/api"))


if __name__ == "__main__":
    unittest.main()
