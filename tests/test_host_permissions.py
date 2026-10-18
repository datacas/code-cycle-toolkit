"""The opt-in global host permissions: conservative, marked, reversible (#142)."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import host_permissions as hp  # noqa: E402
import stage_permissions as sp  # noqa: E402

USER_SETTINGS = {
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "rtk-rewrite"}]}]},
    "permissions": {"allow": ["Read", "Bash(git status:*)"], "deny": ["Bash(rm:*)"]},
    "model": "opus",
}
USER_RULES = 'prefix_rule(pattern=["npm", "ci"], decision="allow")\n'


class HostPermissionsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.paths = hp.Paths(base / "claude" / "settings.json",
                              base / "codex" / "rules" / "default.rules",
                              base / "state" / "code-cycle-toolkit" / "host-permissions.json")
        self.paths.claude_settings.parent.mkdir(parents=True)
        self.paths.claude_settings.write_text(json.dumps(USER_SETTINGS), encoding="utf-8")
        self.paths.codex_rules.parent.mkdir(parents=True)
        self.paths.codex_rules.write_text(USER_RULES, encoding="utf-8")
        self.manager = hp.HostPermissions(self.paths)

    def settings(self) -> dict:
        return json.loads(self.paths.claude_settings.read_text(encoding="utf-8"))

    def backups(self, path: Path) -> list[Path]:
        return sorted(path.parent.glob(f"{path.name}.code-cycle-backup-*"))


class ContentTests(unittest.TestCase):
    WRITES = ("push", "commit", "add", "checkout", "switch", "pull", "rm", "mv", "create",
              "edit", "comment", "review", "merge", "api", "run", "close")

    def test_the_global_allow_list_only_reads(self) -> None:
        for words in hp.READ_COMMANDS:
            with self.subTest(words=words):
                self.assertIn(words[0], {"git", "gh"})
                self.assertFalse(set(words[1:]) & set(self.WRITES))
        allowed = " ".join(hp.claude_entries()["allow"])
        for forbidden in ("python", "npm", "pytest", "unittest", "docker", "Bash(gh:*)",
                          "Bash(git:*)", "git push", "gh pr create", "gh api"):
            self.assertNotIn(forbidden, allowed)

    def test_every_allowed_read_is_in_every_stage_table_but_no_write_is(self) -> None:
        """The global set is a subset of what any stage reads, not their union."""
        policy = sp.StagePolicy(role="review", repo="owner/repo", change_request_id="7",
                                work_item_id="1")
        for words in hp.READ_COMMANDS:
            if words[:3] in (("gh", "issue", "list"), ("gh", "pr", "list")):
                continue  # issue review's lists
            with self.subTest(words=words):
                self.assertTrue(sp.check(" ".join(words), policy).allowed)

    def test_the_deny_list_covers_force_push_merge_and_ref_deletion(self) -> None:
        denied = hp.claude_entries()["deny"]
        for entry in ("Bash(git push --force:*)", "Bash(git push -f:*)",
                      "Bash(git push --force-with-lease:*)", "Bash(git push --delete:*)",
                      "Bash(gh pr merge:*)", "Bash(git branch -D:*)"):
            self.assertIn(entry, denied)
        block = hp.codex_block()
        self.assertTrue(block.startswith(hp.BEGIN_MARKER))
        self.assertTrue(block.rstrip().endswith(hp.END_MARKER))
        self.assertIn('prefix_rule(pattern=["gh", "pr", "merge"], decision="forbidden"', block)
        self.assertEqual(len(hp.READ_COMMANDS), block.count('decision="allow"'))


class ClaudeTests(HostPermissionsTestCase):
    def test_plan_writes_nothing(self) -> None:
        report = self.manager.claude(apply=True, write=False)
        self.assertTrue(report.added)
        self.assertEqual(USER_SETTINGS, self.settings())
        self.assertFalse(self.paths.registry.exists())

    def test_apply_adds_only_missing_entries_preserves_the_rest_and_backs_up(self) -> None:
        report = self.manager.claude(apply=True, write=True)
        settings = self.settings()
        self.assertEqual(USER_SETTINGS["hooks"], settings["hooks"])
        self.assertEqual("opus", settings["model"])
        self.assertEqual(["Read", "Bash(git status:*)"], settings["permissions"]["allow"][:2])
        self.assertIn("Bash(rm:*)", settings["permissions"]["deny"])
        self.assertIn("Bash(gh pr merge:*)", settings["permissions"]["deny"])
        # The user's own entry stays the user's: kept, not claimed.
        self.assertIn("permissions.allow Bash(git status:*)", report.kept)
        registry = json.loads(self.paths.registry.read_text(encoding="utf-8"))
        self.assertNotIn("Bash(git status:*)", registry["claude"]["entries"]["allow"])
        [backup] = self.backups(self.paths.claude_settings)
        self.assertEqual(USER_SETTINGS, json.loads(backup.read_text(encoding="utf-8")))
        self.assertEqual([], self.manager.validate_claude())

    def test_apply_is_idempotent(self) -> None:
        self.manager.claude(apply=True, write=True)
        first = self.paths.claude_settings.read_text(encoding="utf-8")
        report = self.manager.claude(apply=True, write=True)
        self.assertEqual([], report.added)
        self.assertFalse(report.written)
        self.assertEqual(first, self.paths.claude_settings.read_text(encoding="utf-8"))
        self.assertEqual(1, len(self.backups(self.paths.claude_settings)))

    def test_revert_removes_only_managed_entries_and_keeps_later_user_changes(self) -> None:
        self.manager.claude(apply=True, write=True)
        settings = self.settings()
        settings["permissions"]["allow"].append("Bash(make:*)")
        settings["theme"] = "dark"
        self.paths.claude_settings.write_text(json.dumps(settings), encoding="utf-8")

        report = self.manager.claude(apply=False, write=True)

        after = self.settings()
        self.assertEqual(["Read", "Bash(git status:*)", "Bash(make:*)"],
                         after["permissions"]["allow"])
        self.assertEqual(["Bash(rm:*)"], after["permissions"]["deny"])
        self.assertEqual("dark", after["theme"])
        self.assertTrue(report.removed)
        self.assertNotIn("claude", json.loads(self.paths.registry.read_text(encoding="utf-8")))
        again = self.manager.claude(apply=False, write=True)
        self.assertEqual(([], False), (again.removed, again.written))

    def test_a_hand_edited_entry_is_reported_and_never_restored_or_removed(self) -> None:
        self.manager.claude(apply=True, write=True)
        settings = self.settings()
        settings["permissions"]["deny"].remove("Bash(gh pr merge:*)")
        settings["permissions"]["deny"].append("Bash(gh pr merge --admin:*)")
        self.paths.claude_settings.write_text(json.dumps(settings), encoding="utf-8")

        report = self.manager.claude(apply=True, write=True)
        self.assertTrue(any("gh pr merge:*" in conflict for conflict in report.conflicts))
        self.assertNotIn("Bash(gh pr merge:*)", self.settings()["permissions"]["deny"])

        report = self.manager.claude(apply=False, write=True)
        self.assertTrue(any("gh pr merge:*" in conflict for conflict in report.conflicts))
        self.assertIn("Bash(gh pr merge --admin:*)", self.settings()["permissions"]["deny"])

    def test_unreadable_settings_change_nothing(self) -> None:
        self.paths.claude_settings.write_text("{broken", encoding="utf-8")
        with self.assertRaises(hp.HostPermissionError):
            self.manager.claude(apply=True, write=True)
        self.assertEqual("{broken", self.paths.claude_settings.read_text(encoding="utf-8"))
        self.paths.claude_settings.write_text('{"permissions": {"allow": "x"}}',
                                              encoding="utf-8")
        with self.assertRaises(hp.HostPermissionError):
            self.manager.claude(apply=True, write=True)

    def test_a_missing_settings_file_is_created(self) -> None:
        self.paths.claude_settings.unlink()
        report = self.manager.claude(apply=True, write=True)
        self.assertIsNone(report.backup)
        self.assertEqual(set(hp.claude_entries()["allow"]),
                         set(self.settings()["permissions"]["allow"]))


class CodexTests(HostPermissionsTestCase):
    def rules(self) -> str:
        return self.paths.codex_rules.read_text(encoding="utf-8")

    def test_apply_appends_one_marked_block_and_is_idempotent(self) -> None:
        report = self.manager.codex(apply=True, write=True)
        self.assertTrue(report.written)
        self.assertTrue(self.rules().startswith(USER_RULES))
        self.assertEqual(1, self.rules().count(hp.BEGIN_MARKER))
        self.assertEqual(1, len(self.backups(self.paths.codex_rules)))
        before = self.rules()
        again = self.manager.codex(apply=True, write=True)
        self.assertFalse(again.written)
        self.assertEqual(before, self.rules())

    def test_revert_keeps_rules_the_user_added_around_the_block(self) -> None:
        self.manager.codex(apply=True, write=True)
        self.paths.codex_rules.write_text(
            self.rules() + 'prefix_rule(pattern=["make"], decision="allow")\n', encoding="utf-8")
        self.manager.codex(apply=False, write=True)
        self.assertEqual(USER_RULES + 'prefix_rule(pattern=["make"], decision="allow")\n',
                         self.rules())
        self.assertNotIn("codex", json.loads(self.paths.registry.read_text(encoding="utf-8")))

    def test_a_hand_edited_block_is_a_conflict(self) -> None:
        self.manager.codex(apply=True, write=True)
        edited = self.rules().replace('decision="forbidden"', 'decision="allow"', 1)
        self.paths.codex_rules.write_text(edited, encoding="utf-8")
        for apply in (True, False):
            report = self.manager.codex(apply=apply, write=True)
            self.assertTrue(report.conflicts)
            self.assertFalse(report.written)
            self.assertEqual(edited, self.rules())

    def test_a_single_marker_is_a_conflict(self) -> None:
        self.paths.codex_rules.write_text(USER_RULES + hp.BEGIN_MARKER + "\n", encoding="utf-8")
        report = self.manager.codex(apply=True, write=True)
        self.assertTrue(report.conflicts)
        self.assertFalse(report.written)

    def test_a_missing_rules_file_is_created(self) -> None:
        self.paths.codex_rules.unlink()
        self.manager.codex(apply=True, write=True)
        self.assertEqual(hp.codex_block(), self.rules())


class CommandLineTests(HostPermissionsTestCase):
    def run_main(self, *argv) -> tuple[int, str]:
        stdout = io.StringIO()
        with mock.patch.object(hp.Paths, "default", return_value=self.paths), \
                redirect_stdout(stdout):
            try:
                code = hp.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code
        return code, stdout.getvalue()

    def test_writing_needs_confirmation(self) -> None:
        with mock.patch("sys.stderr", io.StringIO()):
            code, _ = self.run_main("apply")
        self.assertEqual(2, code)
        self.assertEqual(USER_SETTINGS, self.settings())
        code, output = self.run_main("plan")
        self.assertEqual(0, code)
        self.assertIn("+ permissions.deny Bash(gh pr merge:*)", output)
        self.assertEqual(USER_SETTINGS, self.settings())

    def test_apply_validate_and_revert_round_trip(self) -> None:
        self.assertEqual(0, self.run_main("apply", "--yes")[0])
        self.assertEqual(0, self.run_main("validate", "--host", "claude")[0])
        code, output = self.run_main("validate", "--host", "codex")
        self.assertIn("codex execpolicy check", output)
        self.assertEqual(0, self.run_main("revert", "--yes")[0])
        self.assertEqual(USER_SETTINGS, self.settings())
        self.assertEqual(USER_RULES, self.paths.codex_rules.read_text(encoding="utf-8"))

    def test_a_conflict_has_its_own_exit_status(self) -> None:
        self.run_main("apply", "--yes", "--host", "codex")
        self.paths.codex_rules.write_text(
            self.paths.codex_rules.read_text(encoding="utf-8").replace("git", "jj", 1),
            encoding="utf-8")
        code, output = self.run_main("revert", "--yes", "--host", "codex")
        self.assertEqual(3, code)
        self.assertIn("conflict", output)


if __name__ == "__main__":
    unittest.main()
