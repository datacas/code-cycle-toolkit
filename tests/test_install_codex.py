"""Where a global Codex installation puts the skills, and what it cleans up.

Codex reads both `~/.agents/skills` and `~/.codex/skills`. Earlier installers
wrote to both, so every skill appeared twice. An installation now writes to
`~/.agents/skills` alone and removes this toolkit's own earlier copies from
`~/.codex/skills` only when `--force` asks it to.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install.sh"


@unittest.skipIf(os.name == "nt", "the Bash installer is covered on Windows by install.ps1")
@unittest.skipUnless(shutil.which("bash"), "bash is not available")
class GlobalCodexInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.agents = self.home / ".agents" / "skills"
        self.legacy = self.home / ".codex" / "skills"

    def install(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(INSTALLER), "--agent", "codex", "--scope", "global",
             "--no-runtime", *extra],
            capture_output=True, text=True, check=True,
            env={**os.environ, "HOME": str(self.home)},
        )

    def seed_legacy(self) -> None:
        for name in ("cc-verify", "other-skill"):
            (self.legacy / name).mkdir(parents=True)
            (self.legacy / name / "SKILL.md").write_text(name, encoding="utf-8")

    def test_skills_are_installed_to_agents_only(self) -> None:
        self.install()

        self.assertTrue((self.agents / "cc-verify" / "SKILL.md").is_file())
        self.assertFalse((self.legacy / "cc-verify").exists())

    def test_force_removes_the_toolkits_earlier_copies_only(self) -> None:
        self.seed_legacy()

        result = self.install("--force")

        self.assertFalse((self.legacy / "cc-verify").exists())
        self.assertTrue((self.legacy / "other-skill" / "SKILL.md").is_file())
        self.assertTrue((self.agents / "cc-verify" / "SKILL.md").is_file())
        self.assertIn(f"Removed duplicate {self.legacy / 'cc-verify'}", result.stdout)

    def test_without_force_the_duplicates_are_named_and_kept(self) -> None:
        self.seed_legacy()

        result = self.install()

        self.assertTrue((self.legacy / "cc-verify" / "SKILL.md").is_file())
        self.assertTrue((self.legacy / "other-skill" / "SKILL.md").is_file())
        self.assertIn(str(self.legacy / "cc-verify"), result.stderr)
        self.assertIn("rm -rf", result.stderr)
        self.assertIn("--force", result.stderr)
        self.assertNotIn("other-skill", result.stderr)

    def test_a_linked_legacy_directory_is_left_alone(self) -> None:
        """A link to `.agents/skills` would otherwise delete the new copy."""
        self.agents.mkdir(parents=True)
        self.legacy.parent.mkdir(parents=True)
        self.legacy.symlink_to(self.agents, target_is_directory=True)

        result = self.install("--force")

        self.assertTrue((self.agents / "cc-verify" / "SKILL.md").is_file())
        self.assertIn("it is a link", result.stderr)

    def test_a_linked_skill_entry_is_left_alone(self) -> None:
        outside = self.home / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("precious", encoding="utf-8")
        self.legacy.mkdir(parents=True)
        (self.legacy / "cc-verify").symlink_to(outside, target_is_directory=True)

        result = self.install("--force")

        self.assertTrue((self.legacy / "cc-verify").is_symlink())
        self.assertEqual("precious", (outside / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("it is a link", result.stderr)


if __name__ == "__main__":
    unittest.main()
