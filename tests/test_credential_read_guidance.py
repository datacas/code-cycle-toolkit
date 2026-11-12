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
            "runtime issue snapshot",
            "fallback for a stage without the",
            "exposes the whole gh store to every",
            "[#397]",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)

        self.assertRegex(
            section,
            re.compile(r"do not grant cycle, repository,\s+or forever consent", re.IGNORECASE),
        )

    def test_the_issue_review_skill_does_not_ask_the_stage_to_hash_the_snapshot(self) -> None:
        # The issue-review command table has no hashing command, and the
        # runtime verifies the file's hash after the stage (#397 review).
        contents = (ROOT / "skills/cc-issue-review/SKILL.md").read_text()
        section = contents.split("### Runtime issue snapshot", 1)[1].split("\n### ", 1)[0]
        flat = " ".join(section.split())

        self.assertNotIn("Confirm the file's size and SHA-256", flat)
        self.assertIn("Do not recompute its SHA-256", flat)
        self.assertIn("the runtime itself verifies the file's hash after the stage", flat)

    def test_supporting_docs_link_to_the_troubleshooting_entry(self) -> None:
        anchor = "review-cycle.md#github-credential-store-denied-during-issue-review"
        for path in ("docs/dependency-preparation.md", "docs/host-permissions.md"):
            with self.subTest(path=path):
                self.assertIn(anchor, (ROOT / path).read_text())

    def test_parallel_review_receipt_precedes_the_ci_gate_and_no_progress_guard_is_documented(self) -> None:
        docs = (ROOT / "docs/review-cycle.md").read_text()
        order = " ".join(docs.split("**Order under `parallel`:**", 1)[1].split())
        gate = docs.index("### The CI gate")

        self.assertLess(docs.index("**Order under `parallel`:**"), gate)
        for phrase in (
            "complete and validate the review receipt first",
            "run the CI gate once for that reviewed SHA",
            "A failed or interrupted review is not completed",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, order)

    def test_runtime_review_guard_and_resolution_resume_are_documented(self) -> None:
        docs = (ROOT / "docs/review-cycle.md").read_text()
        guard = " ".join(docs.split("Before any runtime-initiated review", 1)[1]
                          .split("**Order under `parallel`:**", 1)[0].split())

        self.assertIn("latest valid, trusted, completed review receipt", guard)
        self.assertIn("record a `no_progress` stop", guard)
        self.assertIn("A later resolution receipt permits the automatic rereview", guard)
        self.assertIn("An explicit `--from review` or `--from rereview`", guard)

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
                # The snapshot is the default path; these remedies are its fallback.
                self.assertIn("runtime issue snapshot", guidance)
                self.assertIn("fallback", guidance.lower())
                self.assertIn("whole gh store", guidance)
                self.assertNotIn("interim", guidance.lower())
                self.assertIn("[#397]", guidance)
                self.assertRegex(
                    guidance,
                    re.compile(r"Never offer cycle, repository, or forever consent for credential reads", re.IGNORECASE),
                )


if __name__ == "__main__":
    unittest.main()
