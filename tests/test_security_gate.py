from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import security_gate as gate  # noqa: E402


class DeterministicTriggerTests(unittest.TestCase):
    def test_a_policy_change_requires_an_audit(self) -> None:
        decision = gate.evaluate(["app/Policies/EntryPolicy.php", "README.md"])

        self.assertTrue(decision.required)
        self.assertTrue(decision.deterministic)
        self.assertIn("app/Policies/EntryPolicy.php", decision.matched_paths)

    def test_ci_secret_and_agent_configuration_changes_require_an_audit(self) -> None:
        cases = {
            ".github/workflows/ci.yml": "matched_paths",
            ".env.local": "matched_files",
            "deploy/key.pem": "matched_files",
            ".code-cycle.yml": "matched_files",
            "AGENTS.md": "matched_files",
        }
        for path, match_field in cases.items():
            with self.subTest(path=path):
                decision = gate.evaluate([path])
                self.assertTrue(decision.required)
                self.assertIn(path, getattr(decision, match_field))

    def test_dotenv_example_overlap_is_reported_once(self) -> None:
        decision = gate.evaluate([".env.example"])

        self.assertTrue(decision.required)
        self.assertEqual((".env.example",), decision.matched_files)

    def test_a_migration_requires_an_audit(self) -> None:
        self.assertTrue(gate.evaluate(["database/migrations/2026_01_01_add.php"]).required)

    def test_a_nested_auth_directory_still_matches(self) -> None:
        decision = gate.evaluate(["src/modules/auth/token.ts"])

        self.assertTrue(decision.required)
        self.assertIn("src/modules/auth/token.ts", decision.matched_paths)

    def test_a_lockfile_requires_an_audit(self) -> None:
        decision = gate.evaluate(["composer.lock"])

        self.assertTrue(decision.required)
        self.assertEqual(("composer.lock",), decision.matched_files)

    def test_a_label_requires_an_audit_by_meaning_not_exact_string(self) -> None:
        for label in ("type:security", "area:auth", "Privacy", "deps/dependencies"):
            with self.subTest(label=label):
                self.assertTrue(gate.evaluate(["README.md"], [label]).required)

    def test_an_ordinary_change_does_not(self) -> None:
        decision = gate.evaluate(
            ["docs/guide.md", "src/ui/Button.vue"], ["type:feature"]
        )

        self.assertFalse(decision.required)
        self.assertFalse(decision.deterministic)
        self.assertIn("no rule matched", decision.reason())


class ModelCanAddNeverRemoveTests(unittest.TestCase):
    """The union is the whole point of the gate."""

    def test_the_reviewer_can_add_an_audit_the_rule_did_not_trigger(self) -> None:
        decision = gate.evaluate(["src/ui/Button.vue"], reviewer_requested=True)

        self.assertTrue(decision.required)
        self.assertFalse(decision.deterministic)
        self.assertIn("the reviewer asked for one", decision.reason())

    def test_the_reviewer_cannot_remove_one_the_rule_triggered(self) -> None:
        decision = gate.evaluate(["auth/session.php"], reviewer_requested=False)

        self.assertTrue(decision.required)
        self.assertTrue(decision.deterministic)

    def test_a_false_negative_needs_both_to_fail(self) -> None:
        # Neither the rule nor the model flags it: the only way to skip.
        self.assertFalse(gate.evaluate(["src/ui/Button.vue"], reviewer_requested=False).required)


class ConfigurationTests(unittest.TestCase):
    def test_documented_mandatory_minimum_matches_the_reference_constants(self) -> None:
        documentation = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
        section = documentation.split("The mandatory minimum is the shared baseline", 1)[1]
        section = section.split("\n### ", 1)[0]
        rows = [line for line in section.splitlines() if line.startswith("|")]
        documented = {pattern for row in rows for pattern in re.findall(r"`([^`]+)`", row)}

        self.assertEqual(
            set(gate.MANDATORY_PATHS + gate.MANDATORY_FILES),
            documented,
        )

    def test_a_repository_rule_replaces_defaults_but_keeps_the_minimum(self) -> None:
        rule = gate.GateRule.from_config(
            {"code_cycle": {"security_review": {"always_when": {
                "paths": ["billing/**"], "files": ["secrets.yml"], "labels": ["money"]}}}}
        )

        self.assertIn("billing/**", rule.paths)
        self.assertIn(".github/workflows/**", rule.paths)
        self.assertFalse(gate.evaluate(["auth/session.php"], rule=rule).required)
        self.assertTrue(gate.evaluate(["billing/charge.rb"], rule=rule).required)

    def test_a_project_rule_cannot_disable_any_mandatory_group(self) -> None:
        rule = gate.GateRule.from_config(
            {"code_cycle": {"security_review": {"always_when": {
                "paths": ["billing/**"], "files": ["secrets.yml"], "labels": ["money"]}}}}
        )

        for path in (".github/workflows/ci.yml", ".env.local", ".code-cycle.yml"):
            with self.subTest(path=path):
                decision = gate.evaluate([path], rule=rule)
                self.assertTrue(decision.required)
                matches = (*decision.matched_paths, *decision.matched_files)
                self.assertEqual(1, matches.count(path))

    def test_direct_rule_construction_cannot_bypass_the_minimum(self) -> None:
        rule = gate.GateRule(paths=("billing/**",), files=("secrets.yml",))

        self.assertTrue(gate.evaluate([".github/workflows/ci.yml"], rule=rule).required)
        self.assertTrue(gate.evaluate([".env.local"], rule=rule).required)

    def test_overlapping_project_and_mandatory_matches_are_reported_once(self) -> None:
        rule = gate.GateRule.from_config(
            {"code_cycle": {"security_review": {"always_when": {
                "paths": [".github/workflows/**"], "files": [".env.*"]}}}}
        )

        decision = gate.evaluate(
            [".github/workflows/ci.yml", ".env.local"], rule=rule
        )

        self.assertEqual((".github/workflows/ci.yml",), decision.matched_paths)
        self.assertEqual((".env.local",), decision.matched_files)

    def test_an_absent_configuration_keeps_the_gate_on(self) -> None:
        """An unconfigured repository must not be the unsafe case."""
        for config in (None, {}, {"code_cycle": {}}):
            with self.subTest(config=config):
                rule = gate.GateRule.from_config(config)
                self.assertTrue(set(gate.DEFAULT_PATHS).issubset(rule.paths))
                self.assertTrue(set(gate.MANDATORY_PATHS).issubset(rule.paths))
                self.assertTrue(set(gate.DEFAULT_FILES).issubset(rule.files))
                self.assertTrue(set(gate.MANDATORY_FILES).issubset(rule.files))
                self.assertTrue(gate.evaluate(["auth/session.php"], rule=rule).required)

    def test_an_empty_block_also_keeps_the_defaults(self) -> None:
        rule = gate.GateRule.from_config(
            {"code_cycle": {"security_review": {"always_when": {}}}}
        )

        self.assertTrue(gate.evaluate(["middleware/cors.js"], rule=rule).required)

    def test_a_malformed_rule_is_rejected_rather_than_ignored(self) -> None:
        with self.assertRaises(gate.SecurityGateError):
            gate.GateRule.from_config(
                {"code_cycle": {"security_review": {"always_when": ["auth/**"]}}}
            )


class ReasonTests(unittest.TestCase):
    def test_the_reason_names_what_matched(self) -> None:
        decision = gate.evaluate(
            ["auth/session.php", "composer.lock"], ["area:privacy"], reviewer_requested=True
        )
        reason = decision.reason()

        self.assertIn("auth/session.php", reason)
        self.assertIn("composer.lock", reason)
        self.assertIn("area:privacy", reason)
        self.assertIn("the reviewer asked for one", reason)

    def test_a_path_match_is_not_also_counted_as_a_file_match(self) -> None:
        decision = gate.evaluate(["migrations/2026_add.sql"])

        self.assertEqual((), decision.matched_files)
        self.assertEqual(("migrations/2026_add.sql",), decision.matched_paths)


class RealCampaignIssuesTests(unittest.TestCase):
    """The changes this gate was introduced ahead of."""

    def test_account_deletion_and_sessions_trigger(self) -> None:
        cases = {
            "private avatar upload": ["app/Http/Controllers/ProfileController.php",
                                      "database/migrations/2026_add_birthdate.php"],
            "session revocation": ["app/Modules/Auth/SessionService.php",
                                   "routes/api.php"],
            "last effective admin": ["app/Modules/Privacy/PrivacyService.php",
                                     "app/Policies/DiaryPolicy.php"],
        }
        for name, paths in cases.items():
            with self.subTest(change=name):
                self.assertTrue(gate.evaluate(paths).required)


if __name__ == "__main__":
    unittest.main()
