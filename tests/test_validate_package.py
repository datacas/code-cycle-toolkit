from __future__ import annotations

import importlib.util
import json
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


if __name__ == "__main__":
    unittest.main()
