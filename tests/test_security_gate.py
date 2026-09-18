from __future__ import annotations

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
    def test_a_repository_rule_replaces_the_defaults(self) -> None:
        rule = gate.GateRule.from_config(
            {"code_cycle": {"security_review": {"always_when": {
                "paths": ["billing/**"], "files": ["secrets.yml"], "labels": ["money"]}}}}
        )

        self.assertEqual(("billing/**",), rule.paths)
        self.assertFalse(gate.evaluate(["auth/session.php"], rule=rule).required)
        self.assertTrue(gate.evaluate(["billing/charge.rb"], rule=rule).required)

    def test_an_absent_configuration_keeps_the_gate_on(self) -> None:
        """An unconfigured repository must not be the unsafe case."""
        for config in (None, {}, {"code_cycle": {}}):
            with self.subTest(config=config):
                rule = gate.GateRule.from_config(config)
                self.assertEqual(gate.DEFAULT_PATHS, rule.paths)
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
