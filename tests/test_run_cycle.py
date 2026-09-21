"""The driver's own decisions, in process.

`test_installed_cycle.py` proves a real installation runs a cycle. These are the
cases that are easier to state than to stage: a stage that only started, and an
executor whose structured block is delimited, parseable and not a result.
"""

from __future__ import annotations

import json
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


def block(status: str, **fields) -> str:
    """A structured result the way a skill emits one."""
    payload = json.dumps({"skill": "fake", "status": status, **fields})
    return f"ORCHESTRATION_RESULT\n{payload}\nEND_ORCHESTRATION_RESULT"


APPROVED = block("APPROVED")

#: What each role reports when it does its job. A stage that answered APPROVED
#: to an implementation used to be enough, which is what the canary found.
COMPLETED = {
    "cc-implement-issue": block("IMPLEMENTED"),
    "cc-resolve-comments": block("RESOLVED"),
}


class Talker(ScriptedAdapter):
    """An executor that answers with a structured result, already unwrapped.

    `agent_output` is what an adapter hands over after lifting the reply out of
    its CLI's envelope, so a double that sets only `artifacts["stdout"]` would
    be testing a path no real executor takes.
    """

    def __init__(self, name: str, body: str | None = None) -> None:
        super().__init__(name)
        self.body = body

    def spoken(self, task: str) -> str:
        if self.body is not None:
            return self.body
        for skill, answer in COMPLETED.items():
            if skill in task:
                return answer
        return APPROVED

    def dispatch(self, target, task, **kw):
        self.dispatched.append(task)
        spoken = self.spoken(task)
        return ex.DispatchResult(
            ex.DispatchOutcome.SUCCEEDED, self.name, target,
            model_resolved=target.model, artifacts={"stdout": spoken},
            agent_output=spoken,
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

    def read(self, spoken: str) -> rc.Reported:
        return rc.read_structured_result(ex.DispatchResult(
            ex.DispatchOutcome.SUCCEEDED, "claude", None, agent_output=spoken))

    def test_a_block_holding_a_string_is_present_but_unreadable(self) -> None:
        """Present and unreadable are different facts from absent."""
        reported = self.read('ORCHESTRATION_RESULT\n"APPROVED"\nEND_ORCHESTRATION_RESULT')

        self.assertTrue(reported.present)
        self.assertFalse(reported.readable)
        self.assertIsNone(reported.status)

    def test_a_block_holding_a_number_is_present_but_unreadable(self) -> None:
        reported = self.read("ORCHESTRATION_RESULT\n3\nEND_ORCHESTRATION_RESULT")

        self.assertTrue(reported.present)
        self.assertFalse(reported.readable)

    def test_nothing_at_all_is_absent_rather_than_unreadable(self) -> None:
        reported = self.read("I had a look and it seems fine.")

        self.assertFalse(reported.present)
        self.assertFalse(reported.readable)

    def test_a_malformed_block_stops_the_cycle_instead_of_ending_it(self) -> None:
        """It used to raise, which skipped the closing row entirely."""
        implementer, reviewer = self.blocks(
            'ORCHESTRATION_RESULT\n"APPROVED"\nEND_ORCHESTRATION_RESULT')

        report = self.run_cycle(implementer, reviewer)

        self.assertEqual(rc.UNRESOLVED_END, report.status)
        self.assertIn("could not be read", report.stopped_because)
        self.assertEqual(rc.UNRESOLVED_END, self.rows()[-1]["status"])

    def test_an_echoed_prompt_does_not_shadow_the_real_block(self) -> None:
        """The request itself contains the words, so reading forwards found it."""
        def decoyed(answer: str) -> str:
            return ("prompt received: ... include the ORCHESTRATION_RESULT block\n"
                    + answer)

        report = self.run_cycle(Talker("codex", decoyed(block("IMPLEMENTED"))),
                                Talker("claude", decoyed(APPROVED)))

        self.assertEqual("APPROVED", report.verdict)
        self.assertEqual(rc.APPROVED_END, report.status)


class FunctionalStopTests(RunCycleTestCase):
    """A dispatch that returned is not a stage that worked."""

    def test_an_implementation_that_reports_blocked_stops_the_cycle(self) -> None:
        """The canary: Codex exited 0 having changed nothing, because the work
        item did not exist, and the cycle reviewed it anyway."""
        codex, claude = Talker("codex", block("BLOCKED")), Talker("claude")

        report = self.run_cycle(codex, claude)

        self.assertEqual(["implement"], [stage.role for stage in report.stages])
        self.assertEqual([], claude.dispatched)
        self.assertIn("reported BLOCKED", report.stopped_because)

    def test_the_dispatch_is_still_recorded_as_having_succeeded(self) -> None:
        """Two layers, two facts, both true: the call returned and the work did
        not happen. The store keeps them in separate columns on purpose."""
        self.run_cycle(Talker("codex", block("BLOCKED")), Talker("claude"))

        dispatch, reported = self.rows()[0], self.rows()[1]

        self.assertEqual("succeeded", dispatch["outcome"])
        self.assertIsNone(dispatch["status"])
        self.assertEqual("BLOCKED", reported["status"])

    def test_a_status_the_store_cannot_hold_is_not_carried_to_it(self) -> None:
        """It would raise on the way in and end the run without its last row."""
        report = self.run_cycle(Talker("codex", block("MOSTLY_FINE")), Talker("claude"))

        self.assertEqual(rc.UNRESOLVED_END, report.status)
        self.assertEqual("coordinate", self.rows()[-1]["role"])
        self.assertIn("no status", report.stopped_because)

    def test_a_review_that_reports_blocked_is_not_a_verdict(self) -> None:
        report = self.run_cycle(Talker("codex"), Talker("claude", block("BLOCKED")))

        self.assertIsNone(report.verdict)
        self.assertIn("reported BLOCKED", report.stopped_because)
        self.assertEqual("BLOCKED", self.rows()[-2]["status"])

    def test_a_partial_resolution_still_reaches_its_rereview(self) -> None:
        """The re-review is what judges how much was resolved."""
        codex = Talker("codex")
        codex.body = None
        reviewer = Sequence("claude", [block("CHANGES_REQUESTED"), APPROVED])

        report = self.run_cycle(codex, reviewer)

        self.assertEqual(["implement", "review", "resolve", "rereview"],
                         [stage.role for stage in report.stages])
        self.assertEqual(rc.APPROVED_END, report.status)

    def test_a_long_reply_still_reports_its_status(self) -> None:
        """End to end, the case a length cap used to eat."""
        verbose = block("IMPLEMENTED") + "\n" + ("and then some more. " * 600)

        report = self.run_cycle(Talker("codex", verbose), Talker("claude"))

        self.assertEqual(["implement", "review"], [s.role for s in report.stages])
        self.assertEqual("IMPLEMENTED", self.rows()[1]["status"])

    def test_an_implementation_that_completes_continues(self) -> None:
        report = self.run_cycle(Talker("codex"), Talker("claude"))

        self.assertEqual(["implement", "review"], [s.role for s in report.stages])
        self.assertEqual(rc.APPROVED_END, report.status)


class RolePermissionTests(RunCycleTestCase):
    """The driver decides what a stage may touch, from what the stage is."""

    class Watching(Talker):
        def __init__(self, name, body=None):
            super().__init__(name, body)
            self.permissions = []

        def dispatch(self, target, task, **kw):
            self.permissions.append(kw.get("writes"))
            return super().dispatch(target, task, **kw)

    def test_the_implementer_may_write_and_the_reviewer_may_not(self) -> None:
        codex = self.Watching("codex")
        claude = self.Watching("claude")

        self.run_cycle(codex, claude)

        self.assertEqual([True], codex.permissions)
        self.assertEqual([False], claude.permissions)

    def test_a_resolution_may_write_and_its_rereview_may_not(self) -> None:
        codex = self.Watching("codex")
        claude = self.Watching("claude")
        claude.body = None
        reviewer = Sequence("claude", [block("CHANGES_REQUESTED"), APPROVED])
        reviewer.permissions = []
        original = reviewer.dispatch

        def watching(target, task, **kw):
            reviewer.permissions.append(kw.get("writes"))
            return original(target, task, **kw)

        reviewer.dispatch = watching

        self.run_cycle(codex, reviewer)

        self.assertEqual([True, True], codex.permissions)
        self.assertEqual([False, False], reviewer.permissions)


class ReportedReasonTests(RunCycleTestCase):
    """Why a stage stopped, in the agent's own words and nowhere else.

    A canary spent three extra dispatches working out a reason the agent had
    already given, because the driver printed the status and dropped the prose.
    """

    def test_the_reason_the_agent_gave_reaches_the_operator(self) -> None:
        said = "GitHub authentication is invalid and the API is unreachable"
        codex = Talker("codex", block("BLOCKED", summary=said))

        report = self.run_cycle(codex, Talker("claude"))

        self.assertEqual(said, report.reason)
        self.assertIn(said, report.explain())

    def test_an_error_field_is_preferred_over_a_summary(self) -> None:
        """`error` is what a blocked skill fills in; `summary` may describe the
        run rather than the failure."""
        codex = Talker("codex", block("BLOCKED", summary="ran the bootstrap",
                                      error="no credential for the code host"))

        report = self.run_cycle(codex, Talker("claude"))

        self.assertEqual("no credential for the code host", report.reason)

    def test_a_cycle_that_finished_still_says_how(self) -> None:
        """Not only the failures: the last stage's own account travels with
        every ending."""
        codex = Talker("codex", block("IMPLEMENTED", summary="one file changed"))
        reviewer = Talker("claude", block("APPROVED", summary="no findings"))

        report = self.run_cycle(codex, reviewer)

        self.assertEqual(rc.APPROVED_END, report.status)
        self.assertEqual("no findings", report.reason)

    def test_a_block_without_one_behaves_as_before(self) -> None:
        report = self.run_cycle(Talker("codex", block("BLOCKED")), Talker("claude"))

        self.assertIsNone(report.reason)
        self.assertNotIn("reason:", report.explain())

    def test_an_unreadable_block_has_no_reason_to_give(self) -> None:
        unreadable = 'ORCHESTRATION_RESULT\n"BLOCKED"\nEND_ORCHESTRATION_RESULT'

        report = self.run_cycle(Talker("codex", unreadable), Talker("claude"))

        self.assertIsNone(report.reason)

    def test_a_multi_line_reason_stays_on_one_line(self) -> None:
        """It is printed in a report whose shape a person reads at a glance."""
        codex = Talker("codex", block("BLOCKED", error="first line\n\nsecond   line\t"))

        report = self.run_cycle(codex, Talker("claude"))

        self.assertEqual("first line second line", report.reason)
        self.assertEqual(1, report.explain().count("reason:"))

    def test_a_reason_that_is_not_text_is_not_a_reason(self) -> None:
        for value in (3, ["a"], {"why": "x"}, "", "   "):
            with self.subTest(value=value):
                self.setUp()
                codex = Talker("codex", block("BLOCKED", error=value))

                report = self.run_cycle(codex, Talker("claude"))

                self.assertIsNone(report.reason)

    def test_a_terminal_control_sequence_does_not_reach_the_terminal(self) -> None:
        """An escape is not whitespace, so collapsing whitespace let it through.

        The prose is printed into somebody's terminal report; an agent that can
        emit ESC can recolour, erase or forge the lines around its own.
        """
        spoof = "\x1b[31mspoofed\x1b[0m\x07 and \x08\x08\x08gone"

        report = self.run_cycle(Talker("codex", block("BLOCKED", error=spoof)),
                                Talker("claude"))

        for control in ("\x1b", "\x07", "\x08"):
            with self.subTest(control=repr(control)):
                self.assertNotIn(control, report.reason)
                self.assertNotIn(control, report.explain())
        self.assertIn("spoofed", report.reason)

    def test_every_control_character_is_removed(self) -> None:
        """Removing the bytes is the guarantee. Recognising escape sequences
        would mean keeping a grammar in step with every terminal."""
        noisy = "".join(chr(code) for code in list(range(0, 32)) + [127])

        cleaned = rc.readable("before" + noisy + "after")

        self.assertEqual("before after", cleaned)

    def test_the_executors_own_output_is_cleaned_too(self) -> None:
        """`detail` is stderr from a CLI: the same trust, the same terminal."""
        class Rude(ScriptedAdapter):
            def dispatch(self, target, task, **kw):
                return ex.DispatchResult(
                    ex.DispatchOutcome.FAILED, self.name, target,
                    detail="\x1b[2Jcleared the screen")

        report = self.run_cycle(Rude("codex"), Talker("claude"))

        self.assertNotIn("\x1b", report.stopped_because)
        self.assertIn("cleared the screen", report.stopped_because)

    def test_a_very_long_reason_is_cut(self) -> None:
        """One line of a report, not a page of it."""
        report = self.run_cycle(Talker("codex", block("BLOCKED", error="x" * 4000)),
                                Talker("claude"))

        self.assertEqual(rc.REASON_LIMIT + 1, len(report.reason))
        self.assertTrue(report.reason.endswith("\u2026"))

    def test_a_reason_of_nothing_but_control_characters_is_no_reason(self) -> None:
        report = self.run_cycle(Talker("codex", block("BLOCKED", error="\x1b\x07")),
                                Talker("claude"))

        self.assertIsNone(report.reason)

    def test_the_prose_never_reaches_the_store(self) -> None:
        """The one thing this must not do. The store holds references and
        counts; a reason is the agent's prose about a run."""
        said = "the credential for owner/api expired at 09:00"
        self.run_cycle(Talker("codex", block("BLOCKED", summary=said)), Talker("claude"))

        written = " ".join(
            str(value) for row in self.rows() for value in row.values())

        self.assertNotIn(said, written)
        self.assertNotIn("credential", written)
        self.assertIn("BLOCKED", written)

    def test_the_reason_decides_nothing(self) -> None:
        """A stage that completed still completes, whatever it says beside it."""
        codex = Talker("codex", block("IMPLEMENTED", error="something went wrong"))

        report = self.run_cycle(codex, Talker("claude"))

        self.assertEqual(["implement", "review"], [s.role for s in report.stages])
        self.assertEqual(rc.APPROVED_END, report.status)


class Sequence(Talker):
    """Answers a different thing each time it is asked."""

    def __init__(self, name: str, answers: list[str]) -> None:
        super().__init__(name)
        self.answers = list(answers)

    def spoken(self, task: str) -> str:
        if "cc-resolve-comments" in task:
            return block("RESOLVED")
        return self.answers.pop(0) if self.answers else APPROVED


class Args:
    """What argparse would have produced, without the parser."""

    def __init__(self, **fields) -> None:
        self.repo = None
        self.task = "API-7"
        self.cwd = None
        self.config = None
        self.no_config = False
        self.__dict__.update(fields)


try:  # The runtime's one optional dependency, and only for reading a config.
    import yaml  # noqa: F401
    HAS_YAML = True
except ImportError:  # pragma: no cover - depends on the environment
    HAS_YAML = False


@unittest.skipUnless(HAS_YAML, "reading a configuration needs PyYAML")
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

    def test_a_selector_the_store_would_refuse_is_refused_first(self) -> None:
        """Otherwise a stage is dispatched, paid for, and its row cannot be
        written — the run happens and leaves no trace of having happened."""
        self.write("""
code_cycle:
  repository:
    selector: "bad selector"
""")
        with self.assertRaises(rc.CycleDriverError) as refused:
            rc.plan(Args(cwd=str(self.directory)))

        self.assertIn("not a reference", str(refused.exception))

    def test_a_credential_shaped_selector_never_reaches_a_prompt(self) -> None:
        self.write("""
code_cycle:
  repository:
    selector: "ghp_000000000000000000"
""")
        with self.assertRaises(rc.CycleDriverError) as refused:
            rc.plan(Args(cwd=str(self.directory)))

        self.assertIn("credential prefix", str(refused.exception))

    def test_an_explicit_repository_is_checked_the_same_way(self) -> None:
        with self.assertRaises(rc.CycleDriverError):
            rc.plan(Args(no_config=True, repo="not a repository"))

    def test_the_work_item_is_checked_too(self) -> None:
        """Same class, same cost: a task id the store refuses wastes the stage."""
        with self.assertRaises(rc.CycleDriverError):
            rc.plan(Args(no_config=True, repo="owner/api", task="API 7"))

    def test_nothing_is_dispatched_when_planning_refuses(self) -> None:
        self.write("""
code_cycle:
  repository:
    selector: "bad selector"
""")
        codex, claude = Talker("codex"), Talker("claude")

        with self.assertRaises(rc.CycleDriverError):
            repo, profiles = rc.plan(Args(cwd=str(self.directory)))
            rc.run_cycle(repo, "API-7", router.TaskSignals(), self.store,
                         profiles=profiles, registry=ex.Registry([codex, claude]),
                         availability={"codex": ex.Availability.READY,
                                       "claude": ex.Availability.READY})

        self.assertEqual([], codex.dispatched)
        self.assertEqual([], claude.dispatched)
        self.assertEqual([], self.store.rows("owner/api"))

    def test_a_section_that_is_not_a_mapping_is_a_stated_refusal(self) -> None:
        """Valid YAML, wrong shape. It used to reach a `.get` and traceback."""
        for body in ("code_cycle: not-a-mapping\n",
                     "code_cycle:\n  repository: nope\n",
                     "code_cycle:\n  profiles: nope\n"):
            with self.subTest(body=body):
                self.write(body)

                with self.assertRaises(rc.CycleDriverError) as refused:
                    rc.plan(Args(cwd=str(self.directory)))

                self.assertIn("must be a mapping", str(refused.exception))

    def test_a_malformed_profile_entry_is_a_stated_refusal(self) -> None:
        """Valid YAML, valid section, wrong entry. It used to reach a merge and
        raise TypeError past every handler the CLI has."""
        for body in ("code_cycle:\n  profiles:\n    cheap_coder: nope\n",
                     "code_cycle:\n  profiles:\n    cheap_coder:\n      primary: 3\n",
                     ("code_cycle:\n  profiles:\n    cheap_coder:\n"
                      "      primary: codex:openai/gpt-5.6-luna high\n"
                      "      fallback: [a, b]\n")):
            with self.subTest(body=body):
                self.write(body)

                with self.assertRaises(rc.CycleDriverError) as refused:
                    rc.plan(Args(cwd=str(self.directory), repo="owner/api"))

                self.assertIn("profile 'cheap_coder'", str(refused.exception))

    def test_a_declared_target_that_is_not_a_target_never_becomes_one(self) -> None:
        """`primary: 3` was accepted and carried as the integer 3."""
        self.write("code_cycle:\n  profiles:\n    cheap_coder:\n      primary: 3\n")

        with self.assertRaises(rc.CycleDriverError) as refused:
            rc.plan(Args(cwd=str(self.directory), repo="owner/api"))

        self.assertIn("not a target string", str(refused.exception))

    def test_asking_for_both_at_once_is_refused(self) -> None:
        with self.assertRaises(rc.CycleDriverError):
            rc.plan(Args(config=str(self.write("code_cycle: {}")),
                         no_config=True, repo="owner/api"))


if __name__ == "__main__":
    unittest.main()
