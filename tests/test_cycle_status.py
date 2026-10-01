from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cycle_status  # noqa: E402


class CycleStatusTests(unittest.TestCase):
    def test_progress_line_uses_snapshot_time_and_reports_cycle_end(self) -> None:
        updated_at = "2026-09-30T12:34:00.123456+00:00"
        local_time = cycle_status._parse_timestamp(updated_at).astimezone().strftime("%H:%M")
        line = cycle_status.format_progress_line({
            "updated_at": updated_at,
            "finished": True,
            "status": "READY_FOR_MANUAL_MERGE",
            "stage": {"role": "initial-review", "finished": True},
        })

        self.assertEqual(
            f"✅ 🔍 [{local_time}] cycle done · READY_FOR_MANUAL_MERGE · "
            "cwd unknown · repo unknown · branch unknown · unknown",
            line,
        )

    def test_progress_line_has_the_fixed_fields_on_one_line(self) -> None:
        line = cycle_status.format_progress_line({
            "workspace": {
                "cwd": "/tmp/asgard-issue230",
                "repo_root": "/srv/asgard",
                "branch": "fix/230-audittable-recovery",
                "kind": "linked worktree",
                "temporary": True,
            },
            "stage": {
                "role": "implement",
                "executor": "codex",
                "provider": "openai",
                "model": "gpt-6-luna",
                "effort": "max",
                "elapsed_seconds": 720,
                "activity": "running the\ntest suite",
                "finished": False,
            },
        })

        self.assertRegex(
            line,
            r"^🛠️ \[\d{2}:\d{2}\] implement · codex openai/gpt-6-luna max · "
            r"12m00s · cwd /tmp/asgard-issue230 · repo /srv/asgard · "
            r"branch fix/230-audittable-recovery · temporary linked worktree · "
            r"running the test suite$",
        )
        self.assertNotIn("\n", line)

        done = cycle_status.format_progress_line({
            "stage": {
                "role": "implement",
                "executor": "codex",
                "provider": "openai",
                "model": "gpt-6-luna",
                "effort": "max",
                "duration_seconds": 720,
                "activity": "opening the pull request",
                "status": "IMPLEMENTED",
                "finished": True,
            },
        })
        self.assertIn("✅ 🛠️", done)
        self.assertIn("implement done", done)
        self.assertIn("IMPLEMENTED: opening the pull request", done)
        self.assertNotIn("\n", done)

    def test_result_icons_distinguish_review_changes_and_errors(self) -> None:
        review = cycle_status.format_progress_line({
            "updated_at": "2026-09-30T12:34:00Z",
            "stage": {
                "role": "review",
                "status": "CHANGES_REQUESTED",
                "outcome": "succeeded",
                "finished": True,
            },
        })
        failed = cycle_status.format_progress_line({
            "updated_at": "2026-09-30T12:34:00Z",
            "finished": True,
            "status": "FAILED",
            "stage": {"role": "implement", "finished": True},
        })

        self.assertIn("⚠️ 🔍", review)
        self.assertIn("❌ 🛠️", failed)

    def test_heartbeat_schedule_switches_at_fifteen_minutes(self) -> None:
        cases = (
            (0, 120),
            (120, 120),
            (14 * 60, 60),
            (15 * 60 - 1, 1),
            (15 * 60, 300),
            (20 * 60 - 1, 1),
            (20 * 60, 300),
        )
        for elapsed, expected in cases:
            with self.subTest(elapsed=elapsed):
                self.assertEqual(
                    expected,
                    cycle_status._next_heartbeat_delay(elapsed, 120),
                )

    def test_workspace_snapshot_identifies_git_location_and_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            subprocess.run(["git", "-C", str(root), "init"], check=True,
                           capture_output=True, text=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"],
                           check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email",
                            "test@example.com"], check=True, capture_output=True, text=True)
            (root / "README.md").write_text("test\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "README.md"],
                           check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"],
                           check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(root), "branch", "-M", "issue-137"],
                           check=True, capture_output=True, text=True)

            writer = cycle_status.CycleStatusWriter(
                Path(temporary) / "telemetry.sqlite", "workspace-test", "owner/repo",
                "ISSUE-137", workspace=root,
            )
            current = writer._status["workspace"]
            self.assertEqual(str(root.resolve()), current["repo_root"])
            self.assertEqual("issue-137", current["branch"])
            self.assertEqual("regular checkout", current["kind"])
            self.assertTrue(current["temporary"])
            nested = root / "scripts"
            nested.mkdir()
            self.assertEqual(
                "regular checkout", cycle_status._workspace_snapshot(nested)["kind"]
            )

            subprocess.run(["git", "-C", str(root), "switch", "-c", "feature"],
                           check=True, capture_output=True, text=True)
            self.assertTrue(writer._refresh_workspace(force=True))
            self.assertEqual("feature", writer._status["workspace"]["branch"])
            self.assertIn("branch feature", cycle_status.format_progress_line(writer._status))

            outside = Path(temporary) / "outside-git"
            outside.mkdir()
            untracked = cycle_status._workspace_snapshot(outside)
            self.assertEqual("outside Git", untracked["kind"])
            self.assertEqual("unknown", untracked["branch"])

    def test_workspace_snapshot_identifies_linked_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            subprocess.run(["git", "-C", str(root), "init"], check=True,
                           capture_output=True, text=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"],
                           check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email",
                            "test@example.com"], check=True, capture_output=True, text=True)
            (root / "README.md").write_text("test\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "README.md"],
                           check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"],
                           check=True, capture_output=True, text=True)
            worktree = Path(temporary) / "linked-worktree"
            subprocess.run(["git", "-C", str(root), "worktree", "add", "-b",
                            "feature", str(worktree)], check=True,
                           capture_output=True, text=True)

            snapshot = cycle_status._workspace_snapshot(worktree)

            self.assertEqual("linked worktree", snapshot["kind"])
            self.assertEqual("feature", snapshot["branch"])
            self.assertTrue(snapshot["temporary"])

    def test_line_mode_only_prints_statuses_newer_than_since(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            statuses = [
                {
                    "cycle_id": "old",
                    "updated_at": "2026-09-30T11:00:00.000000+00:00",
                    "stage": {"role": "old-stage", "executor": "codex",
                              "provider": "openai", "model": "old-model",
                              "effort": "low", "elapsed_seconds": 60,
                              "activity": "old activity", "finished": False},
                },
                {
                    "cycle_id": "new",
                    "updated_at": "2026-09-30T12:00:00.200000+00:00",
                    "stage": {"role": "new-stage", "executor": "claude",
                              "provider": "anthropic", "model": "new-model",
                              "effort": "high", "elapsed_seconds": 60,
                              "activity": "new activity", "finished": False},
                },
            ]
            for status in statuses:
                (directory / f"{status['cycle_id']}.json").write_text(
                    json.dumps(status), encoding="utf-8"
                )
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "cycle_status.py"),
                 "--line", "--since", "2026-09-30T12:00:00.100000Z",
                 "--status-dir", str(directory)],
                capture_output=True, text=True, check=False,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(1, len(lines))
        self.assertIn("new-stage", lines[0])
        self.assertNotIn("old-stage", result.stdout)

    def test_since_requires_timezone_aware_iso_timestamp(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
            cycle_status.main(["--line", "--since", "2026-09-30T11:30:00"])
        self.assertIn("timestamp must include a timezone", output.getvalue())

    def test_heartbeat_suppresses_unchanged_progress_but_reports_activity_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            writer = cycle_status.CycleStatusWriter(
                Path(temporary) / "telemetry.sqlite", "heartbeat-test", "owner/repo", "ISSUE-7",
                progress_interval=10, verbose=True, stream=output,
            )

            class Target:
                executor = "codex"
                provider = "openai"
                model = "gpt-6-luna"
                effort = "max"

            class Decision:
                target = Target()
                profile = "deep_coder"
                used_fallback = False

            writer.stage_started("implement", Decision())
            writer._stage_started = 10.0
            waits = iter((None, "tool", "activity", "stop"))

            def wait(_timeout: float) -> bool:
                tick = next(waits)
                if tick == "tool":
                    writer.activity(tool=True)
                elif tick == "activity":
                    writer.activity(text="reading source")
                return tick == "stop"

            with patch.object(writer._stop, "wait", side_effect=wait), \
                    patch.object(cycle_status.time, "monotonic", return_value=10.1):
                writer._heartbeat()

            lines = output.getvalue().splitlines()
            progress = [line for line in lines if "implement ·" in line]
            self.assertEqual(3, len(progress))  # start plus the two changed heartbeats
            self.assertIn("1 tools", progress[1])
            self.assertIn("1 tools", progress[2])
            self.assertIn("reading source", progress[-1])
            self.assertFalse(any("└" in line for line in lines))

    def test_finished_status_keeps_usage_summary(self) -> None:
        rendered = cycle_status.format_status({
            "cycle_id": "usage-test",
            "finished": True,
            "status": "APPROVED",
            "stage": {
                "role": "review",
                "finished": True,
                "status": "APPROVED",
                "usage": "in 10k (25% cached) / out 1k",
                "workspace": {},
            },
        })

        self.assertIn("in 10k (25% cached) / out 1k", rendered)

    def test_status_is_written_atomically_and_reader_shows_start_and_finish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "telemetry.sqlite"
            done_output = io.StringIO()
            writer = cycle_status.CycleStatusWriter(
                database, "cycle-test", "owner/repo", "ISSUE-7",
                progress_interval=10, verbose=True, stream=done_output,
            )
            writer.start()
            initial = json.loads(writer.path.read_text(encoding="utf-8"))
            self.assertFalse(initial["finished"])
            self.assertEqual("RUNNING", initial["status"])
            self.assertRegex(
                initial["updated_at"],
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}[+-]\d{2}:\d{2}$",
            )

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

            writer.stage_finished(
                Result(), "IMPLEMENTED",
                findings="findings: 1 open (1 high)",
                warnings=["review clone left a warning"],
            )
            writer.finish("READY_FOR_MANUAL_MERGE")
            completed = json.loads(writer.path.read_text(encoding="utf-8"))
            self.assertTrue(completed["finished"])
            self.assertTrue(completed["stage"]["finished"])
            self.assertEqual("IMPLEMENTED", completed["stage"]["status"])
            self.assertEqual("findings: 1 open (1 high)", completed["stage"]["findings"])
            self.assertEqual(["review clone left a warning"], completed["stage"]["warnings"])
            self.assertIn("findings: 1 open (1 high)", done_output.getvalue())
            self.assertIn("warning: review clone left a warning", done_output.getvalue())

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
