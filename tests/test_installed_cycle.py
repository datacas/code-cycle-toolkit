"""A whole cycle, run the way a person would run it, from an installation.

Everything else in this suite imports from `scripts/` and builds its objects in
process. That proves the pieces work; it cannot prove that anything assembles
them. `CycleRecorder` existed for a release with no production caller, so the
guarantee it enforces — no dispatch without a row — applied to nothing.

This starts one process, with only an installed runtime on its path and fake
agent binaries on its PATH, and then opens the database to see what the run left
behind. No provider, no network, no credential: the agents are a script that
prints what a real one prints.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FAKE_AGENT = ROOT / "tests" / "fakes" / "fake_agent.py"


@unittest.skipIf(os.name == "nt", "the fake agents are POSIX executables")
@unittest.skipUnless(shutil.which("bash"), "bash is not available")
class InstalledCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.binaries = self.root / "bin"
        for directory in (self.home, self.project, self.binaries):
            directory.mkdir(parents=True)

        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)],
                       capture_output=True, text=True, check=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.project,
                       capture_output=True, text=True, check=True)
        subprocess.run(["git", "config", "user.name", "Cycle Test"], cwd=self.project, check=True)
        subprocess.run(["git", "config", "user.email", "cycle@example.test"], cwd=self.project, check=True)
        (self.project / "tracked.txt").write_text("ready\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.project, check=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=self.project,
                       capture_output=True, text=True, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote)],
                       cwd=self.project, check=True)

        self.install()
        self.make_agents()
        self.make_credentials()

    # ---- the environment a real run would have -------------------------------

    def install(self) -> None:
        subprocess.run(
            ["bash", str(ROOT / "scripts" / "install.sh"),
             "--agent", "claude", "--scope", "global"],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True, text=True, check=True,
        )

    def make_agents(self) -> None:
        for name in ("codex", "claude"):
            target = self.binaries / name
            shutil.copy(FAKE_AGENT, target)
            target.chmod(0o755)

    def make_credentials(self) -> None:
        (self.home / ".claude.json").write_text(
            json.dumps({"hasCompletedOnboarding": True}), encoding="utf-8")
        codex_home = self.home / ".codex"
        codex_home.mkdir(exist_ok=True)
        (codex_home / "auth.json").write_text("{}", encoding="utf-8")

    @property
    def runtime(self) -> Path:
        return self.home / ".code-cycle" / "runtime"

    @property
    def database(self) -> Path:
        return self.home / "state" / "telemetry.sqlite"

    def run_cycle(self, **environment) -> subprocess.CompletedProcess:
        """Start the entrypoint exactly as a person installed it would."""
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            "CODEX_HOME": str(self.home / ".codex"),
            **environment,
        }
        return subprocess.run(
            [sys.executable, str(self.runtime / "run_cycle.py"),
             "--repo", "owner/api", "--task", "API-7",
             "--difficulty", "2", "--verifiability", "auto",
             "--cwd", str(self.project),
             "--database", str(self.database)],
            env=env, capture_output=True, text=True,
        )

    def rows(self) -> list[dict]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute(
                "SELECT * FROM stages ORDER BY id")
            return [dict(row) for row in cursor.fetchall()]
        finally:
            connection.close()

    # ---- what a run leaves behind -------------------------------------------

    def test_a_cycle_run_from_the_installation_records_every_stage(self) -> None:
        result = self.run_cycle()

        self.assertEqual(0, result.returncode, result.stderr)
        rows = self.rows()
        self.assertEqual(
            ["implement", "implement", "review", "review", "coordinate"],
            [row["role"] for row in rows],
        )
        self.assertEqual("READY_FOR_MANUAL_MERGE", rows[-1]["status"])

    def test_the_rows_say_which_executor_and_model_actually_ran(self) -> None:
        self.run_cycle()

        implement = self.rows()[0]

        self.assertEqual("codex", implement["executor"])
        self.assertEqual("cheap_coder", implement["profile"])
        self.assertEqual("succeeded", implement["outcome"])
        # Codex reports no model at all, observed on a live run: the column is
        # empty rather than agreeing with the request, and silence is not
        # counted as agreement anywhere that reads it.
        self.assertIsNone(implement["model_resolved"])

        review = [row for row in self.rows()
                  if row["role"] == "review" and row["outcome"] == "succeeded"][0]
        self.assertEqual("codex", review["executor"])
        self.assertEqual("gpt-5.6-terra", review["model_requested"])
        self.assertIsNone(review["model_resolved"])

    def test_a_second_round_is_recorded_as_a_second_round(self) -> None:
        """The review asks for changes once, so the cycle resolves and re-reviews."""
        result = self.run_cycle(FAKE_ROUNDS=str(self.root / "rounds"))

        self.assertEqual(0, result.returncode, result.stderr)
        rows = self.rows()
        self.assertEqual(
            ["implement", "implement", "review", "review",
             "resolve", "resolve", "rereview", "rereview", "coordinate"],
            [row["role"] for row in rows],
        )
        self.assertEqual([0, 0, 0, 0, 1, 1, 1, 1, 1], [row["iteration"] for row in rows])
        self.assertEqual("CHANGES_REQUESTED", rows[3]["status"])
        self.assertEqual(2, rows[3]["findings_total"])
        self.assertEqual(1, rows[3]["findings_blocking"])
        self.assertEqual("APPROVED", rows[7]["status"])

    def test_an_exhausted_window_leaves_the_abandoned_attempt_in_the_record(self) -> None:
        """The scenario a fallback exists for, end to end and through SQLite.

        Codex reports an exhausted window on a real subprocess, the recorder
        reroutes once, and all three facts survive: the attempt that was
        abandoned, the decision to fall back, and what the fallback did.
        """
        result = self.run_cycle(FAKE_QUOTA="codex")

        self.assertEqual(1, result.returncode)
        implement = [row for row in self.rows() if row["role"] == "implement"]
        abandoned, fallback = implement[0], implement[1]

        self.assertEqual("codex", abandoned["executor"])
        self.assertEqual("blocked", abandoned["outcome"])
        self.assertEqual("operating_quota", abandoned["missing_capability"])

        self.assertEqual("claude", fallback["executor"])
        self.assertEqual(1, fallback["used_fallback"])
        self.assertEqual("succeeded", fallback["outcome"])
        self.assertEqual("HUMAN_INTERVENTION", self.rows()[-1]["status"])

    def test_a_review_that_reports_no_verdict_stops_the_cycle(self) -> None:
        """An unknown verdict is recorded as unknown, never guessed from a
        successful exit."""
        result = self.run_cycle(FAKE_SILENT_REVIEW="codex")

        self.assertEqual(1, result.returncode)
        self.assertIn("reported no structured result", result.stdout)
        roles = [row["role"] for row in self.rows()]
        self.assertIn("review", roles)
        self.assertEqual("HUMAN_INTERVENTION", self.rows()[-1]["status"])

    def test_a_blocked_implementation_is_recorded_and_stops_the_cycle(self) -> None:
        """The canary, as an installed run: the CLI exits 0, the agent reports
        BLOCKED, and no review must follow."""
        result = self.run_cycle(FAKE_BLOCKED="codex")

        self.assertEqual(1, result.returncode)
        rows = self.rows()
        self.assertEqual(["implement", "implement", "coordinate"],
                         [row["role"] for row in rows])
        self.assertEqual("succeeded", rows[0]["outcome"])
        self.assertEqual("BLOCKED", rows[1]["status"])
        self.assertEqual("HUMAN_INTERVENTION", rows[-1]["status"])
        self.assertNotIn("review", result.stdout)

    def test_the_block_is_read_out_of_the_real_envelope(self) -> None:
        """Both fakes wrap their reply the way their CLI does, so a driver that
        read the envelope instead of the reply would fail here."""
        self.run_cycle()

        verdicts = [row["status"] for row in self.rows() if row["status"]]

        self.assertEqual(["IMPLEMENTED", "APPROVED", "READY_FOR_MANUAL_MERGE"],
                         verdicts)

    def test_nothing_reached_the_database_outside_the_repository(self) -> None:
        """The store is host state, not a file the project carries."""
        self.run_cycle()

        self.assertTrue(self.database.is_file())
        self.assertEqual([], list(self.project.rglob("*.sqlite")))


if __name__ == "__main__":
    unittest.main()
