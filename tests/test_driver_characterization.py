"""Observable driver contracts pinned before extracting its implementation."""
import contextlib
import io
import unittest

from test_run_cycle import (
    RunCycleTestCase, Talker, Sequence, Starter, Interrupter, block, rc,
)
from cycle_loop import CycleControl, ControlState


class DriverCharacterizationTests(RunCycleTestCase):
    def test_control_table_covers_retry_resume_gate_and_interruption(self):
        paths = [
            ["issue_review", "issue_review", "implement", "review", "resolve", "rereview", "ci", "stop"],
            ["review", "review", "ci", "rereview", "ci", "resolve", "rereview", "stop"],
            ["resolve", "resolve", "rereview", "interrupt", "stop"],
            ["rereview", "ci", "stop"],
        ]
        for path in paths:
            with self.subTest(path=path):
                control = CycleControl()
                for event in path:
                    control.move(event)
                self.assertEqual(ControlState.STOPPED, control.state)
                self.assertEqual(path, [event for _, event, _ in control.transitions])

    def test_stage_attempt_sequences_and_endings(self):
        cases = [
            ({}, Talker("codex"), Talker("claude"),
             ["implement", "review"], rc.APPROVED_END),
            ({}, Talker("codex"), Sequence("claude", [block("CHANGES_REQUESTED"), block("APPROVED")]),
             ["implement", "review", "resolve", "rereview"], rc.APPROVED_END),
            ({"start_from": "resolve", "change_request_id": "74"},
             Talker("codex"), Talker("claude"), ["resolve", "rereview"], rc.APPROVED_END),
            ({}, Starter("codex"), Talker("claude"), ["implement"], rc.UNRESOLVED_END),
            ({}, Interrupter("codex"), Talker("claude"), ["implement"], rc.UNRESOLVED_END),
            ({}, Talker("codex", block("BLOCKED")), Talker("claude"),
             ["implement"], rc.UNRESOLVED_END),
        ]
        for index, (options, coder, reviewer, roles, ending) in enumerate(cases):
            with self.subTest(index=index):
                before = len(self.rows())
                report = self.run_cycle(coder, reviewer, **options)
                dispatches = [r for r in self.rows()[before:]
                              if r["payload"].get("record_kind") == "dispatch"]
                self.assertEqual(roles, [s.role for s in report.stages])
                self.assertEqual(roles, [r["role"] for r in dispatches])
                self.assertEqual(ending, report.status)

    def test_cli_parse_errors_keep_exit_two(self):
        for flags, message in [
            (["--progress-interval", "0"], "--progress-interval must be greater than zero"),
            (["--json"], "--json needs --dry-run"),
            (["--dry-run", "--detach"], "--dry-run runs nothing to detach"),
            (["--from", "resolve"], "--from resolve requires --pr"),
        ]:
            with self.subTest(flags=flags):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                    rc.main(["--repo", "owner/api", "--task", "API-7", "--no-config", *flags])
                self.assertEqual(2, error.exception.code)
                self.assertIn(message, stderr.getvalue())

    def test_report_explain_text(self):
        report = rc.CycleReport("owner/api", "API-7", stopped_because="fixture stop",
                                reason="agent account")
        report.note("  fixture note")
        self.assertEqual(
            "owner/api API-7: HUMAN_INTERVENTION\n  fixture note\n"
            "  stopped: fixture stop\n  reason:  agent account", report.explain())


if __name__ == "__main__":
    unittest.main()
