from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cycle_status  # noqa: E402


class CycleStatusTests(unittest.TestCase):
    def test_status_is_written_atomically_and_reader_shows_start_and_finish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "telemetry.sqlite"
            writer = cycle_status.CycleStatusWriter(
                database, "cycle-test", "owner/repo", "ISSUE-7",
                progress_interval=10,
            )
            writer.start()
            initial = json.loads(writer.path.read_text(encoding="utf-8"))
            self.assertFalse(initial["finished"])
            self.assertEqual("RUNNING", initial["status"])

            class Target:
                executor = "codex"
                provider = "openai"
                model = "gpt-6-luna"
                effort = "max"

            class Decision:
                target = Target()
                profile = "deep_coder"
                used_fallback = True

            writer.stage_started("implement", Decision())
            writer.activity(text="working\x00 on issue", tool=True)
            active = json.loads(writer.path.read_text(encoding="utf-8"))
            self.assertEqual("working on issue", active["stage"]["activity"])
            self.assertEqual(1, active["stage"]["tool_count"])
            self.assertIn("openai/gpt-6-luna", cycle_status.format_status(active))
            self.assertIn("fallback", cycle_status.format_status(active))
            active_output = io.StringIO()
            with contextlib.redirect_stdout(active_output):
                cycle_status.main(["--status-dir", str(writer.path.parent)])
            self.assertIn("cycle-test", active_output.getvalue())
            self.assertNotIn("cycle finished", active_output.getvalue())

            class Result:
                class Outcome:
                    value = "succeeded"
                outcome = Outcome()

            writer.stage_finished(Result(), "IMPLEMENTED")
            writer.finish("READY_FOR_MANUAL_MERGE")
            completed = json.loads(writer.path.read_text(encoding="utf-8"))
            self.assertTrue(completed["finished"])
            self.assertTrue(completed["stage"]["finished"])
            self.assertEqual("IMPLEMENTED", completed["stage"]["status"])

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = cycle_status.main(["--status-dir", str(writer.path.parent)])
            self.assertEqual(0, result)
            self.assertIn("cycle finished · READY_FOR_MANUAL_MERGE", output.getvalue())

    def test_activity_is_single_line_and_bounded(self) -> None:
        text = cycle_status._clean_activity("first\nsecond\x00")
        self.assertEqual("first second", text)
        self.assertLessEqual(len(cycle_status._clean_activity("x" * 1000)),
                             cycle_status.ACTIVITY_LIMIT)

    def test_usage_summary_reads_codex_and_claude_final_events(self) -> None:
        class Result:
            def __init__(self, stdout):
                self.artifacts = {"stdout": stdout}

        codex = Result(json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 3_100_000, "cached_input_tokens": 2_945_000,
            "output_tokens": 61_000,
        }}))
        claude = Result(json.dumps({"type": "result", "modelUsage": {
            "claude-sonnet-5": {"inputTokens": 10, "cacheReadInputTokens": 90,
                                "outputTokens": 2_000},
        }}))

        self.assertEqual("in 3.1M (95% cached) / out 61k",
                         cycle_status._usage_summary(codex))
        self.assertEqual("in 100 (90% cached) / out 2k",
                         cycle_status._usage_summary(claude))


if __name__ == "__main__":
    unittest.main()
