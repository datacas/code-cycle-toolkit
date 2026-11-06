"""The agent capability parity rule and its audit stay complete (#450)."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "docs" / "capability-parity-audit.md"
OUTCOMES = ("real limit", "role contract", "toolkit veto")
SENSITIVE = (
    "Force-push",
    "Pushes to the base branch",
    "Ref deletion",
    "Credential reads",
    "Git config edits",
    "Publication guards",
    "Forever-grants for review",
)


def section(text: str, heading: str) -> str:
    marker = f"\n## {heading}\n"
    if marker not in text:
        raise AssertionError(f"missing section {heading!r}")
    return text.split(marker, 1)[1].split("\n## ", 1)[0]


def rows(block: str) -> list[list[str]]:
    """The body rows of the first Markdown table in `block`, cell by cell."""
    found: list[list[str]] = []
    for line in block.splitlines():
        if not line.startswith("|"):
            if found:
                break
            continue
        # A `\|` inside a cell is a literal pipe, not a column boundary.
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        found.append(cells)
    return [row for row in found[2:]]


class CapabilityParityAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.audit = AUDIT.read_text(encoding="utf-8")
        cls.inventory = {row[0]: row for row in rows(section(cls.audit, "Inventory"))}
        cls.kept = {row[0]: row for row in rows(section(cls.audit, "Kept restrictions"))}
        cls.vetoes = {row[0]: row for row in
                      rows(section(cls.audit, "Vetoes: conversion and parity baseline"))}
        cls.children = {row[0]: row for row in rows(section(cls.audit, "Child issues"))}

    def test_the_rule_is_mandatory_in_agents_and_its_owning_page(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        page = section((ROOT / "docs" / "host-permissions.md").read_text(encoding="utf-8"),
                       "Agent capability parity")
        for text in (agents, page):
            with self.subTest(text=text[:40]):
                self.assertRegex(text, r"mandatory for every new implementation and every review")
                self.assertIn("finding", text)
        self.assertIn("capability-parity-audit.md", page)
        self.assertIn("docs/capability-parity-audit.md", agents)

    def test_every_restriction_has_source_role_incident_and_one_outcome(self) -> None:
        self.assertGreaterEqual(len(self.inventory), 30)
        for identifier, row in self.inventory.items():
            with self.subTest(row=identifier):
                self.assertRegex(identifier, r"PA-\d{2}\Z")
                self.assertEqual(len(row), 7)
                _, restriction, source, role, incident, outcome, _ = row
                for cell in (restriction, source, role, incident):
                    self.assertTrue(cell and cell != "—")
                self.assertIn(outcome, OUTCOMES)

    def test_every_kept_restriction_states_its_rationale(self) -> None:
        kept = {key for key, row in self.inventory.items() if row[5] != "toolkit veto"}
        self.assertEqual(kept, set(self.kept))
        for identifier, row in self.kept.items():
            with self.subTest(row=identifier):
                self.assertGreater(len(row[1]), 40)

    def test_every_veto_links_an_issue_or_a_child_issue(self) -> None:
        linked_children: set[str] = set()
        for identifier, row in self.inventory.items():
            if row[5] != "toolkit veto":
                continue
            with self.subTest(row=identifier):
                # A draft is not an issue: every veto links a created one.
                self.assertNotRegex(row[6], r"CI-\d")
                links = re.findall(r"#\d+", row[6])
                self.assertTrue(links)
                for child in (link for link in links if link in self.children):
                    linked_children.add(child)
                    self.assertIn(identifier, self.children[child][5])
        self.assertEqual(linked_children, set(self.children))
        for child, row in self.children.items():
            for identifier in row[5].split(", "):
                with self.subTest(child=child, row=identifier):
                    self.assertEqual(self.inventory[identifier][5], "toolkit veto")
                    self.assertIn(child, re.findall(r"#\d+", self.inventory[identifier][6]))

    def test_every_child_issue_is_created_with_one_milestone_and_one_priority(self) -> None:
        self.assertTrue(self.children)
        for identifier, row in self.children.items():
            with self.subTest(child=identifier):
                self.assertRegex(identifier, r"\A#\d+\Z")
                self.assertRegex(row[1], r"\ACI-\d+\Z")
                self.assertTrue(row[2])
                self.assertRegex(row[3], r"\AM\d+ — ")
                self.assertRegex(row[4], r"\AP[0-3]\Z")
                self.assertTrue(row[6])
        drafts_left = re.findall(r"CI-\d+", self.audit.replace(section(self.audit, "Child issues"), ""))
        self.assertEqual(drafts_left, [])

    def test_the_roadmap_lists_every_child_issue(self) -> None:
        roadmap = (ROOT / "docs" / "ROADMAP_V2.md").read_text(encoding="utf-8").splitlines()
        for identifier, row in self.children.items():
            with self.subTest(child=identifier):
                milestone = next((line for line in roadmap if line.startswith(f"| [{row[3]}]")), None)
                self.assertIsNotNone(milestone, f"no milestone row for {row[3]!r}")
                self.assertIn(f"{identifier} ({row[4]}", milestone or "")

    def test_every_veto_names_a_claude_and_a_codex_baseline(self) -> None:
        vetoes = {key for key, row in self.inventory.items() if row[5] == "toolkit veto"}
        self.assertEqual(vetoes, set(self.vetoes))
        for identifier, row in self.vetoes.items():
            with self.subTest(row=identifier):
                _, conversion, claude, codex = row
                self.assertTrue(conversion)
                self.assertIn("claude", claude.lower())
                self.assertIn("codex", codex.lower())

    def test_sensitive_operations_have_a_classified_row(self) -> None:
        sensitive = rows(section(self.audit, "Sensitive operations"))
        names = [row[0] for row in sensitive]
        for operation in SENSITIVE:
            with self.subTest(operation=operation):
                matching = [row for row in sensitive if row[0].startswith(operation)]
                self.assertTrue(matching, names)
                for row in matching:
                    self.assertIn(row[1], self.inventory)
                    self.assertIn(row[2], OUTCOMES)
                    self.assertEqual(row[2], self.inventory[row[1]][5])
                    self.assertTrue(row[3])

    def test_a_review_reports_a_new_toolkit_only_veto(self) -> None:
        skill = (ROOT / "skills" / "cc-pr-review" / "SKILL.md").read_text(encoding="utf-8")
        self.assertRegex(skill, r"breaks a stated review rule is a finding")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("A review treats a new toolkit-only veto as a finding.", agents)
        self.assertIn("is a review finding", section(self.audit, "Reviews")
                      + section((ROOT / "docs" / "host-permissions.md").read_text(encoding="utf-8"),
                                "Agent capability parity"))


if __name__ == "__main__":
    unittest.main()
