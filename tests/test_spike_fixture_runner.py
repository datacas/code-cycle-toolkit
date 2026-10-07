import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


RUNNER = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "spikes"
    / "playwright-behavioral-capabilities-fixture"
    / "run.sh"
)


@unittest.skipUnless(os.name == "posix", "the spike runner is a Bash script")
class SpikeFixtureRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        for command in ("bash", "rm", "mkdir", "tee"):
            executable = shutil.which(command)
            if executable is None:
                self.skipTest(f"{command} is required to run the fixture")
            (self.bin_dir / command).symlink_to(executable)

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_fixture(self, name):
        env = {"PATH": str(self.bin_dir)}
        return subprocess.run(
            [str(RUNNER), name],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_runner_does_not_need_rtk_and_preserves_playwright_failure(self):
        npx = self.bin_dir / "npx"
        npx.write_text("#!/bin/sh\nexit 7\n")
        npx.chmod(0o755)

        result = self.run_fixture("without-rtk")

        self.assertEqual(result.returncode, 7)
        self.assertIn("without-rtk exit=7 json=no", (self.root / "out/exitcodes.txt").read_text())

    def test_runner_returns_command_not_found_when_npx_is_missing(self):
        result = self.run_fixture("without-npx")

        self.assertEqual(result.returncode, 127)
        self.assertIn("without-npx exit=127 json=no", (self.root / "out/exitcodes.txt").read_text())


if __name__ == "__main__":
    unittest.main()
