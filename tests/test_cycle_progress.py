from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import executors as ex  # noqa: E402
import router  # noqa: E402
import run_cycle as rc  # noqa: E402
import telemetry as tm  # noqa: E402
from test_run_cycle import APPROVED, COMPLETED, StreamingNative  # noqa: E402


class CycleProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = tm.Telemetry(Path(self.temporary.name) / "telemetry.sqlite")
        self.implementer = StreamingNative("codex", COMPLETED["cc-implement-issue"])
        self.reviewer = StreamingNative("claude", APPROVED)
        self.profiles = router.load_profiles({"code_cycle": {
            "profiles": {"reviewer": {
                "primary": "claude:anthropic/claude-sonnet-5 high",
            }},
        }})

    def run_cycle(self, **options):
        return rc.run_cycle(
            "owner/api", "API-7", router.TaskSignals(), self.store,
            registry=ex.Registry([self.implementer, self.reviewer]),
            availability={"codex": ex.Availability.READY,
                          "claude": ex.Availability.READY},
            profiles=self.profiles,
            **options,
        )

    def test_verbose_shows_start_activity_periodic_progress_and_end(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report = self.run_cycle(verbose=True, progress_interval=0.01)

        rendered = output.getvalue()
        self.assertEqual("READY_FOR_MANUAL_MERGE", report.status)
        self.assertIn("implement ·", rendered)
        self.assertIn("1 tools", rendered)
        self.assertIn("The scripted executor is working", rendered)
        self.assertIn("implement done · succeeded · IMPLEMENTED", rendered)
        self.assertIn("review done · succeeded · APPROVED", rendered)

    def test_default_output_stays_silent_and_status_is_finished(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report = self.run_cycle(progress_interval=0.01)

        self.assertEqual("READY_FOR_MANUAL_MERGE", report.status)
        self.assertEqual("", output.getvalue())
        paths = list((self.store.path.parent / "status").glob("*.json"))
        self.assertEqual(1, len(paths))
        status = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertTrue(status["finished"])
        self.assertTrue(status["stage"]["finished"])
        self.assertEqual("APPROVED", status["stage"]["status"])

    def test_cli_passes_verbose_options_and_keeps_defaults_opt_in(self) -> None:
        report = rc.CycleReport(
            repo_id="owner/api", task_id="API-7",
            status=rc.APPROVED_END, verdict="APPROVED",
        )
        with patch.object(rc, "run_cycle", return_value=report) as run:
            result = rc.main([
                "--repo", "owner/api", "--task", "API-7", "--no-config",
                "--database", str(Path(self.temporary.name) / "cli.sqlite"),
                "--verbose", "--progress-interval", "12",
            ])

        self.assertEqual(0, result)
        self.assertTrue(run.call_args.kwargs["verbose"])
        self.assertEqual(12, run.call_args.kwargs["progress_interval"])


if __name__ == "__main__":
    unittest.main()
