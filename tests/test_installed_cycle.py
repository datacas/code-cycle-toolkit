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
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FAKE_AGENT = ROOT / "tests" / "fakes" / "fake_agent.py"

sys.path.insert(0, str(ROOT / "tests"))
from test_support import isolate_host_environment  # noqa: E402


_restore_host_environment = None


def setUpModule() -> None:
    global _restore_host_environment
    _restore_host_environment = isolate_host_environment()


def tearDownModule() -> None:
    if _restore_host_environment is not None:
        _restore_host_environment()


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

    def command(self, *arguments: str, **environment) -> tuple[list[str], dict]:
        """The entrypoint exactly as a person installed it would start it."""
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            "CODEX_HOME": str(self.home / ".codex"),
            **environment,
        }
        return ([sys.executable, str(self.runtime / "run_cycle.py"),
                 "--repo", "owner/api", "--task", "API-7",
                 "--difficulty", "2", "--verifiability", "auto",
                 "--cwd", str(self.project),
                 "--database", str(self.database), *arguments], env)

    def run_cycle(self, *arguments: str, **environment) -> subprocess.CompletedProcess:
        argv, env = self.command(*arguments, **environment)
        return subprocess.run(argv, env=env, capture_output=True, text=True)

    def wait_for(self, path: Path, seconds: float = 30) -> None:
        deadline = time.monotonic() + seconds
        while not path.exists() or not path.read_text(encoding="utf-8").strip():
            if time.monotonic() > deadline:
                self.fail(f"{path} did not appear within {seconds}s")
            time.sleep(0.1)

    def rows(self, *, issue_review: bool = False) -> list[dict]:
        """The stored rows; the default issue review's rows only when asked."""
        return [row for row in self.all_rows()
                if issue_review or row["role"] != "issue_review"]

    def all_rows(self) -> list[dict]:
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

    def test_the_default_cycle_reviews_the_work_item_before_implementing(self) -> None:
        result = self.run_cycle()

        self.assertEqual(0, result.returncode, result.stderr)
        rows = self.rows(issue_review=True)
        self.assertEqual(
            ["issue_review", "issue_review", "implement", "implement",
             "review", "review", "coordinate"],
            [row["role"] for row in rows],
        )
        dispatch, verdict = rows[0], rows[1]
        self.assertEqual("reviewer", dispatch["profile"])
        self.assertEqual("succeeded", dispatch["outcome"])
        self.assertEqual("READY", verdict["status"])
        self.assertEqual("high", json.loads(verdict["payload"])["readiness_confidence"])
        self.assertEqual("dispatched", json.loads(rows[-1]["payload"])["issue_review"])

    def test_a_work_item_that_needs_refinement_is_not_implemented(self) -> None:
        result = self.run_cycle(FAKE_READINESS="NEEDS_REFINEMENT")

        self.assertEqual(1, result.returncode)
        rows = self.rows(issue_review=True)
        self.assertEqual(["issue_review", "issue_review", "coordinate"],
                         [row["role"] for row in rows])
        self.assertEqual("needs_refinement", json.loads(rows[-1]["payload"])["stop_reason"])
        self.assertIn("IR-001", result.stdout)

    def test_off_runs_the_original_cycle(self) -> None:
        result = self.run_cycle("--issue-review", "off")

        self.assertEqual(0, result.returncode, result.stderr)
        rows = self.rows(issue_review=True)
        self.assertEqual(
            ["implement", "implement", "review", "review", "coordinate"],
            [row["role"] for row in rows],
        )
        self.assertEqual("off", json.loads(rows[-1]["payload"])["issue_review"])

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
        self.assertEqual("gpt-6-sol", review["model_requested"])
        self.assertIsNone(review["model_resolved"])

    def test_agent_reported_test_evidence_survives_an_installed_cycle(self) -> None:
        result = self.run_cycle(FAKE_TEST_EVIDENCE="1")

        self.assertEqual(0, result.returncode, result.stderr)
        rows = self.rows()
        implement_verdict = next(row for row in rows
                                 if row["role"] == "implement" and row["status"])
        verdict = json.loads(implement_verdict["payload"])
        cycle = json.loads(rows[-1]["payload"])

        self.assertEqual(True, verdict["tests_passed"])
        self.assertEqual("agent_reported", verdict["tests_basis"])
        self.assertEqual("verified_with_reservations", verdict["verification"])
        self.assertEqual("agent_reported", cycle["tests_basis"])
        self.assertEqual("verified_with_reservations", cycle["verification"])

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
        # Off, so the implementation is the first stage the exhausted window
        # meets; the issue review would otherwise block on it first.
        result = self.run_cycle("--issue-review", "off", FAKE_QUOTA="codex")

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
        self.assertNotIn("\n  review ", result.stdout)

    def test_the_block_is_read_out_of_the_real_envelope(self) -> None:
        """Both fakes wrap their reply the way their CLI does, so a driver that
        read the envelope instead of the reply would fail here."""
        self.run_cycle()

        verdicts = [row["status"] for row in self.rows() if row["status"]]

        self.assertEqual(["IMPLEMENTED", "APPROVED", "READY_FOR_MANUAL_MERGE"],
                         verdicts)

    def test_a_stopped_cycle_records_where_it_stopped_and_stops_its_agent(self) -> None:
        """What a host does at its time limit, done to a real process."""
        pidfile = self.root / "agent.pid"
        argv, env = self.command("--issue-review", "off",
                                 FAKE_HANG="codex", FAKE_PIDFILE=str(pidfile))
        process = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.wait_for(pidfile)

        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=60)

        self.assertEqual(1, process.returncode, stderr)
        self.assertIn("interrupted by SIGTERM during implement", stdout)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text(encoding="utf-8")), 0)
        rows = self.rows()
        self.assertEqual(["interrupted"],
                         [row["outcome"] for row in rows if row["role"] == "implement"])
        self.assertEqual(["interrupted"],
                         [json.loads(row["payload"]).get("stop_reason")
                          for row in rows if row["role"] == "coordinate"])

    def test_a_detached_cycle_returns_at_once_and_runs_in_its_own_session(self) -> None:
        """The launch a host with a task time limit uses: nothing of it to kill."""
        pidfile = self.root / "agent.pid"
        argv, env = self.command("--issue-review", "off", "--detach",
                                 FAKE_HANG="codex", FAKE_PIDFILE=str(pidfile))
        started = time.monotonic()
        launched = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)

        self.assertEqual(0, launched.returncode, launched.stderr)
        self.assertLess(time.monotonic() - started, 20)
        lines = dict(line.split(": ", 1) for line in launched.stdout.splitlines())
        cycle_pid = int(lines["detached"].rsplit(" ", 1)[1])
        log = Path(lines["log"])
        self.assertEqual(self.database.parent / "status", log.parent)
        self.assertIn("cycle_status.py --line --since", lines["progress"])
        self.assertIn("--database", lines["progress"])

        self.wait_for(pidfile)
        self.assertNotEqual(os.getsid(0), os.getsid(cycle_pid))
        self.assertEqual(cycle_pid, os.getpgid(cycle_pid))
        os.kill(cycle_pid, signal.SIGTERM)
        deadline = time.monotonic() + 60
        while "recorded in" not in log.read_text(encoding="utf-8"):
            if time.monotonic() > deadline:
                self.fail(f"the detached cycle wrote no report: {log.read_text()}")
            time.sleep(0.1)

        self.assertIn("interrupted by SIGTERM during implement", log.read_text(encoding="utf-8"))
        self.assertEqual(["interrupted"],
                         [row["outcome"] for row in self.rows() if row["role"] == "implement"])

    def test_a_detached_cycle_is_refused_before_it_detaches(self) -> None:
        """A mistake is reported to the person launching, not to a log file."""
        argv, env = self.command("--detach", "--pr", "4")
        refused = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)

        self.assertEqual(2, refused.returncode)
        self.assertIn("--pr names an existing change request", refused.stderr)
        self.assertNotIn("detached:", refused.stdout)

    def test_an_interrupted_implementation_continues_from_its_checkout(self) -> None:
        refused = self.run_cycle("--continue", "--workspace", "current")
        self.assertEqual(2, refused.returncode)
        self.assertIn("nothing to continue", refused.stderr)

        (self.project / "partial.txt").write_text("half done\n", encoding="utf-8")
        continued = self.run_cycle("--continue", "--workspace", "current")

        self.assertEqual(0, continued.returncode, continued.stderr)
        closing = [json.loads(row["payload"]) for row in self.all_rows()
                   if row["role"] == "coordinate"]
        self.assertEqual([True], [payload.get("continued") for payload in closing])
        self.assertEqual([], self.all_rows_for("issue_review"))

    def all_rows_for(self, role: str) -> list[dict]:
        return [row for row in self.all_rows() if row["role"] == role]

    def test_nothing_reached_the_database_outside_the_repository(self) -> None:
        """The store is host state, not a file the project carries."""
        self.run_cycle()

        self.assertTrue(self.database.is_file())
        self.assertEqual([], list(self.project.rglob("*.sqlite")))


if __name__ == "__main__":
    unittest.main()
