"""The runtime an installation gets, and whether it can actually run.

`install.sh` used to copy `skills/` alone, so `cc-orchestrator` could tell a
reader to drive every stage through `CycleRecorder` while no installation had
one. That is the failure mode this whole instrument exists to avoid, and no test
here could see it, because every other test imports from `scripts/` — the
checkout a real installation does not have.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
MANIFEST = SCRIPTS / "runtime.manifest"

#: Modules that deliberately stay in the checkout. Tooling for the package
#: itself, and campaign code no installed skill points anybody at.
NOT_RUNTIME = frozenset({
    "validate-package.py",
    "calibration_store.py",
})


def manifest_modules() -> list[str]:
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines
            if line.strip() and not line.strip().startswith("#")]


class ManifestTests(unittest.TestCase):
    def test_every_listed_module_exists(self) -> None:
        for module in manifest_modules():
            with self.subTest(module=module):
                self.assertTrue((SCRIPTS / module).is_file())

    def test_every_module_is_either_shipped_or_declared_not_shipped(self) -> None:
        """A new module cannot land without somebody deciding which it is."""
        listed = set(manifest_modules())

        for path in sorted(SCRIPTS.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertTrue(
                    path.name in listed or path.name in NOT_RUNTIME,
                    f"{path.name} is neither in runtime.manifest nor declared as tooling",
                )

    def test_the_runtime_can_import_itself(self) -> None:
        """A shipped module whose import is not shipped fails on first call.

        Installation would report success and the failure would surface later,
        somewhere else, as a missing row.
        """
        listed = set(manifest_modules())
        local = {path.stem for path in SCRIPTS.glob("*.py")}

        for module in sorted(listed):
            tree = ast.parse((SCRIPTS / module).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name in local:
                        with self.subTest(module=module, imports=name):
                            self.assertIn(f"{name}.py", listed)


@unittest.skipIf(os.name == "nt", "the Bash installer is covered on Windows by install.ps1")
@unittest.skipUnless(shutil.which("bash"), "bash is not available")
class InstalledRuntimeTests(unittest.TestCase):
    """What a clean installation can do, asked of the installation itself."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.install()

    def install(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPTS / "install.sh"), "--agent", "claude",
             "--scope", "project", "--project-dir", str(self.project), *extra],
            capture_output=True, text=True, check=True,
        )

    @property
    def runtime(self) -> Path:
        return self.project / ".code-cycle" / "runtime"

    def check(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "tests" / "installed_stage_check.py"),
             str(self.runtime), str(self.project / "telemetry.sqlite")],
            capture_output=True, text=True,
        )

    def test_a_clean_installation_runs_a_stage_and_records_it(self) -> None:
        """The claim `cc-orchestrator` makes, executed against an installation."""
        result = self.check()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("recorded 3 rows", result.stdout)

    def test_the_check_fails_when_the_runtime_is_incomplete(self) -> None:
        """Its own premise: it must be able to detect a missing module."""
        (self.runtime / "telemetry.py").unlink()

        result = self.check()

        self.assertNotEqual(0, result.returncode)

    def test_the_installed_directory_cannot_be_committed_by_accident(self) -> None:
        ignore = self.project / ".code-cycle" / ".gitignore"

        self.assertEqual("*", ignore.read_text(encoding="utf-8").strip())

    def test_the_runtime_can_be_declined(self) -> None:
        shutil.rmtree(self.project / ".code-cycle")
        self.install("--no-runtime", "--force")

        self.assertFalse(self.runtime.exists())

    def test_an_existing_ignore_file_is_left_exactly_as_it_was(self) -> None:
        """Those are somebody else's rules, in somebody else's repository."""
        ignore = self.project / ".code-cycle" / ".gitignore"
        ignore.write_text("keep-existing-rules\n", encoding="utf-8")

        self.install("--force")

        self.assertEqual("keep-existing-rules", ignore.read_text(encoding="utf-8").strip())

    def test_a_symlinked_ignore_file_is_refused_rather_than_followed(self) -> None:
        """Installing into a repository is not authority to truncate a file
        somewhere else on the disk that the repository happens to point at."""
        outside = self.project / "outside.txt"
        outside.write_text("precious\n", encoding="utf-8")
        ignore = self.project / ".code-cycle" / ".gitignore"
        ignore.unlink()
        ignore.symlink_to(outside)

        with self.assertRaises(subprocess.CalledProcessError) as refused:
            self.install("--force")

        self.assertIn("symlink", refused.exception.stderr)
        self.assertEqual("precious", outside.read_text(encoding="utf-8").strip())

    def test_force_replaces_a_symlinked_module_instead_of_writing_through_it(self) -> None:
        """--force asks to replace this toolkit's files, not to follow a link
        out of the installation directory."""
        outside = self.project / "outside.py"
        outside.write_text("precious\n", encoding="utf-8")
        module = self.runtime / "cycle.py"
        module.unlink()
        module.symlink_to(outside)

        self.install("--force")

        self.assertEqual("precious", outside.read_text(encoding="utf-8").strip())
        self.assertFalse(module.is_symlink())
        self.assertIn("CycleRecorder", module.read_text(encoding="utf-8"))

    def test_a_symlinked_runtime_directory_is_refused(self) -> None:
        elsewhere = self.project / "elsewhere"
        elsewhere.mkdir()
        shutil.rmtree(self.runtime)
        self.runtime.symlink_to(elsewhere, target_is_directory=True)

        with self.assertRaises(subprocess.CalledProcessError) as refused:
            self.install("--force")

        self.assertIn("symlink", refused.exception.stderr)
        self.assertEqual([], list(elsewhere.iterdir()))

    def test_reinstalling_over_a_runtime_needs_force(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()

        self.install("--force")

        self.assertTrue((self.runtime / "cycle.py").is_file())


if __name__ == "__main__":
    unittest.main()
