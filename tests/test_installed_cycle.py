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
        self.assertEqual(implement["model_requested"], implement["model_resolved"])
        self.assertEqual("succeeded", implement["outcome"])

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

        self.assertEqual(0, result.returncode, result.stderr)
        implement = [row for row in self.rows() if row["role"] == "implement"]
        abandoned, fallback = implement[0], implement[1]

        self.assertEqual("codex", abandoned["executor"])
        self.assertEqual("blocked", abandoned["outcome"])
        self.assertEqual("operating_quota", abandoned["missing_capability"])

        self.assertEqual("claude", fallback["executor"])
        self.assertEqual(1, fallback["used_fallback"])
        self.assertEqual("succeeded", fallback["outcome"])

    def test_a_review_that_reports_no_verdict_stops_the_cycle(self) -> None:
        """An unknown verdict is recorded as unknown, never guessed from a
        successful exit."""
        result = self.run_cycle(FAKE_SILENT="claude")

        self.assertEqual(1, result.returncode)
        self.assertIn("no structured verdict", result.stdout)
        roles = [row["role"] for row in self.rows()]
        self.assertIn("review", roles)
        self.assertEqual("HUMAN_INTERVENTION", self.rows()[-1]["status"])

    def test_nothing_reached_the_database_outside_the_repository(self) -> None:
        """The store is host state, not a file the project carries."""
        self.run_cycle()

        self.assertTrue(self.database.is_file())
        self.assertEqual([], list(self.project.rglob("*.sqlite")))


if __name__ == "__main__":
    unittest.main()
