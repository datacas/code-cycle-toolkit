from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
VALIDATOR_PATH = ROOT / "scripts" / "validate-package.py"
SPEC = importlib.util.spec_from_file_location("validate_package", VALIDATOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load validator from {VALIDATOR_PATH}")
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


class ValidatePackageTests(unittest.TestCase):
    def copy_package(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "package"
        shutil.copytree(
            ROOT,
            target,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        return target

    def assert_error_contains(self, errors: list[str], expected: str) -> None:
        self.assertTrue(
            any(expected in error for error in errors),
            f"Expected an error containing {expected!r}, got: {errors}",
        )

    def test_current_package_is_valid(self) -> None:
        self.assertEqual([], VALIDATOR.validate_package(ROOT))

    def test_rejects_shared_section_drift(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-code-review" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace(
                "Read the repository's own instructions when they exist",
                "Read repository instructions when they exist",
                1,
            ),
            encoding="utf-8",
        )

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "has drifted between skills")

    @unittest.skipIf(
        os.name == "nt",
        "On NTFS a colon opens an alternate data stream instead of creating a file, "
        "so this sidecar cannot exist as a listable entry on Windows. It appears as "
        "a real file only once the tree reaches a filesystem without ADS, which is "
        "where the validator has to catch it.",
    )
    def test_rejects_a_windows_zone_identifier_sidecar(self) -> None:
        package = self.copy_package()
        # The real shape seen in WSL: a colon, so neither the suffix filter nor
        # a `.Zone.Identifier` ending catches it.
        sidecar = package / "docs" / "guide.md:Zone.Identifier"
        sidecar.write_text("[ZoneTransfer]\nZoneId=3\n", encoding="utf-8")
        self.assertTrue(
            sidecar.is_file(),
            "the sidecar was not created as a listable file; the test premise is gone",
        )

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "remove metadata sidecar before publishing")

    def test_rejects_a_dot_separated_zone_identifier_sidecar(self) -> None:
        package = self.copy_package()
        (package / "docs" / "guide.md.Zone.Identifier").write_text(
            "[ZoneTransfer]\nZoneId=3\n", encoding="utf-8"
        )

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "remove metadata sidecar before publishing")

    def test_rejects_a_documented_header_the_parser_cannot_read(self) -> None:
        package = self.copy_package()
        for skill in ("cc-initial-review", "cc-rereview", "cc-resolve-comments"):
            path = package / "skills" / skill / "SKILL.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "· resolved · valid · blocks:yes",
                    "· resolved · probably · blocks:yes",
                ),
                encoding="utf-8",
            )

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "contract example does not parse")

    def test_rejects_dropping_a_documented_line_kind(self) -> None:
        package = self.copy_package()
        for skill in ("cc-initial-review", "cc-rereview", "cc-resolve-comments"):
            path = package / "skills" / skill / "SKILL.md"
            text = path.read_text(encoding="utf-8")
            start = text.index("A triage run line records")
            end = text.index("Every finding the comment publishes", start)
            path.write_text(text[:start] + text[end:], encoding="utf-8")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "no longer documents the triage line kind")

    def test_rejects_known_fixed_language_output(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-initial-review" / "SKILL.md"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\nRecord `No verificado por ejecución` in the published comment.\n",
            encoding="utf-8",
        )

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "known fixed-language output")

    def test_rejects_literal_output_directive(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-rereview" / "SKILL.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\nPublish using this sentence:\n",
            encoding="utf-8",
        )

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "literal output directive")

    def test_rejects_manifest_version_mismatch(self) -> None:
        package = self.copy_package()
        path = package / ".codex-plugin" / "plugin.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["version"] = "999.0.0"
        path.write_text(json.dumps(manifest), encoding="utf-8")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "manifest versions disagree")

    def test_rejects_invalid_jsonc(self) -> None:
        package = self.copy_package()
        (package / "opencode.jsonc").write_text("{ invalid }", encoding="utf-8")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "invalid JSONC")

    def test_rejects_skill_missing_from_readme(self) -> None:
        package = self.copy_package()
        path = package / "README.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("`cc-run`", "runtime helper"),
            encoding="utf-8",
        )

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "skills missing from README.md: cc-run")

    def test_rejects_missing_required_skill(self) -> None:
        package = self.copy_package()
        shutil.rmtree(package / "skills" / "cc-verify")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "missing skills: cc-verify")

    def test_rejects_unregistered_skill(self) -> None:
        package = self.copy_package()
        source = package / "skills" / "cc-run"
        target = package / "skills" / "cc-extra"
        shutil.copytree(source, target)
        skill_file = target / "SKILL.md"
        skill_file.write_text(
            skill_file.read_text(encoding="utf-8").replace(
                "name: cc-run", "name: cc-extra", 1
            ),
            encoding="utf-8",
        )

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "skills not declared in the validator: cc-extra")

    def test_rejects_missing_claude_codex_adapter_reference(self) -> None:
        package = self.copy_package()
        reference = (
            package
            / "skills"
            / "cc-orchestrator"
            / "references"
            / "codex-plugin-cc.md"
        )
        reference.unlink()

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "missing orchestrator reference")

    def test_rejects_unrouted_claude_codex_adapter_reference(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-orchestrator" / "SKILL.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "references/codex-plugin-cc.md",
                "references/missing-adapter.md",
            ),
            encoding="utf-8",
        )

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(
            errors, "cc-orchestrator does not route Claude-to-Codex mode"
        )

    def edit_implement_skill(self, old: str, new: str) -> list[str]:
        package = self.copy_package()
        path = package / "skills" / "cc-implement-issue" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return VALIDATOR.validate_package(package)

    def test_rejects_an_implement_result_example_that_is_not_json(self) -> None:
        errors = self.edit_implement_skill(
            '"related_items": ["130", "131"]', '"related_items": ["130", "131"],'
        )

        self.assert_error_contains(errors, "result example is not strict JSON")

    def test_rejects_an_implement_result_example_without_diagnosis(self) -> None:
        errors = self.edit_implement_skill('"diagnosis": {', '"diagnosed": {')

        self.assert_error_contains(errors, "result example has no `diagnosis` object")

    def test_rejects_a_diagnosis_decision_the_section_does_not_define(self) -> None:
        errors = self.edit_implement_skill(
            '"decision": "implement_root_fix"', '"decision": "patch_symptom"'
        )

        self.assert_error_contains(errors, "`diagnosis.decision` is not a known token")

    def test_rejects_dropping_a_diagnosis_decision(self) -> None:
        errors = self.edit_implement_skill(
            "| `stop_duplicate` |", "| `stop` |"
        )

        self.assert_error_contains(errors, "does not define `stop_duplicate`")

    def test_rejects_dropping_the_non_reproduced_outcome(self) -> None:
        errors = self.edit_implement_skill("| `needs_evidence` |", "| `ask` |")

        self.assert_error_contains(errors, "does not define `needs_evidence`")

    def test_rejects_a_reproduction_flag_that_is_not_boolean_or_null(self) -> None:
        errors = self.edit_implement_skill('"reproduced": true', '"reproduced": "unknown"')

        self.assert_error_contains(errors, "`diagnosis.reproduced` must be true, false, or null")

    def test_rejects_related_items_that_are_not_a_list(self) -> None:
        errors = self.edit_implement_skill(
            '"related_items": ["130", "131"]', '"related_items": "130"'
        )

        self.assert_error_contains(errors, "`diagnosis.related_items` must be a list")


class DiagnosisShapeTests(unittest.TestCase):
    """Every diagnosis the skill allows, not only the one its example prints."""

    NOT_REPRODUCED = {
        "classification": "not_reproduced", "decision": "needs_evidence",
        "related_search": "basic", "reproduced": False,
        "cause_matches_issue": None, "related_items": [],
    }

    def test_a_defect_that_could_not_be_reproduced_has_a_valid_diagnosis(self) -> None:
        self.assertEqual([], VALIDATOR.diagnosis_errors(self.NOT_REPRODUCED))

    def test_a_non_reproduced_defect_cannot_claim_a_known_cause(self) -> None:
        for changed in ({"reproduced": True}, {"cause_matches_issue": False}):
            with self.subTest(changed=changed):
                problems = VALIDATOR.diagnosis_errors({**self.NOT_REPRODUCED, **changed})

                self.assertTrue(any("`not_reproduced` requires" in p for p in problems))

    def test_an_integer_is_not_a_boolean(self) -> None:
        problems = VALIDATOR.diagnosis_errors({**self.NOT_REPRODUCED, "reproduced": 0})

        self.assertTrue(any("must be true, false, or null" in p for p in problems))

    def test_related_items_hold_identifiers_only(self) -> None:
        for items in (["130", 131], [""], [{"id": "130"}]):
            with self.subTest(items=items):
                problems = VALIDATOR.diagnosis_errors(
                    {**self.NOT_REPRODUCED, "related_items": items}
                )

                self.assertTrue(any("related_items" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
