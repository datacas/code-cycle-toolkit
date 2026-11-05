import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = (
    "skills/cc-issue-review/SKILL.md",
    "skills/cc-orchestrator/SKILL.md",
    "skills/cc-orca-orchestrator/SKILL.md",
)
REMEDIES = (
    'code_cycle.user_choices.credential_reads: ["~/.config/gh"]',
    "one-run configuration copy",
    "Skip `issue_review`",
)


class CredentialReadGuidanceTests(unittest.TestCase):
    def test_issue_review_docs_explain_the_denial_and_narrow_fix(self) -> None:
        docs = (ROOT / "docs/review-cycle.md").read_text()
        section = docs.split("### GitHub credential store denied during issue review", 1)[1]
        section = section.split("\n### ", 1)[0]

        for phrase in (
            "`environment_restricted`",
            "`~/.config/gh`",
            "policy denial",
            "Narrow fix",
            'code_cycle.user_choices.credential_reads: ["~/.config/gh"]',
            "The user applies any configuration change; the agent does not",
            "interim guidance",
            "[#397]",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)

        self.assertRegex(
            section,
            re.compile(r"do not grant cycle, repository,\s+or forever consent", re.IGNORECASE),
        )

    def test_supporting_docs_link_to_the_troubleshooting_entry(self) -> None:
        anchor = "review-cycle.md#github-credential-store-denied-during-issue-review"
        for path in ("docs/dependency-preparation.md", "docs/host-permissions.md"):
            with self.subTest(path=path):
                self.assertIn(anchor, (ROOT / path).read_text())

    def test_all_three_skills_name_signal_and_order_user_remedies(self) -> None:
        for path in SKILLS:
            with self.subTest(path=path):
                contents = (ROOT / path).read_text()
                if path.endswith("cc-issue-review/SKILL.md"):
                    guidance = contents.split("### GitHub credential-store denial", 1)[1]
                    guidance = guidance.split("\n## ", 1)[0]
                else:
                    guidance = contents.split("When `issue_review` returns", 1)[1]
                    guidance = guidance.split("\n## ", 1)[0]

                self.assertIn("`environment_restricted`", guidance)
                self.assertIn("`~/.config/gh`", guidance)
                positions = [guidance.index(f"({letter})") for letter in "abc"]
                self.assertEqual(positions, sorted(positions))
                for remedy in REMEDIES:
                    self.assertIn(remedy, guidance)
                self.assertIn("the user", guidance.lower())
                self.assertIn("agent", guidance.lower())
                self.assertIn("interim", guidance.lower())
                self.assertIn("[#397]", guidance)
                self.assertRegex(
                    guidance,
                    re.compile(r"Never offer cycle, repository, or forever consent for credential reads", re.IGNORECASE),
                )


if __name__ == "__main__":
    unittest.main()
