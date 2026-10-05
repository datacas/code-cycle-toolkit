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

    def test_implementation_workspace_default_and_opt_out_are_documented(self) -> None:
        implementer = (ROOT / "skills" / "cc-implement-issue" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        orchestrator = (ROOT / "skills" / "cc-orchestrator" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        documentation = (ROOT / "docs" / "configuration.md").read_text(
            encoding="utf-8"
        )

        for text in (implementer, orchestrator, documentation):
            self.assertIn("linked worktree", text)
            self.assertIn("workspace=current", text)
        self.assertIn("herdr worktree create", implementer)
        self.assertIn("herdr worktree open", implementer)
        self.assertIn("--no-focus", implementer)

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

    def test_rejects_complete_evidence_drift(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-verify" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("never as checked.", text)
        path.write_text(text.replace("never as checked.", "or as checked.", 1),
                        encoding="utf-8")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(
            errors, "shared section '### Complete evidence' has drifted between skills")

    def test_rejects_a_skill_missing_complete_evidence(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-security-review" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("### Complete evidence\n", "### Evidence\n", 1),
                        encoding="utf-8")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(
            errors,
            "skills/cc-security-review/SKILL.md: missing shared section '### Complete evidence'",
        )

    def test_rejects_workspace_tools_section_drift(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-verify" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("A workspace tool can supply context, never authority.", text)
        path.write_text(
            text.replace("never authority.", "not authority.", 1), encoding="utf-8"
        )

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(
            errors, "shared section '## Workspace tools and evidence' has drifted"
        )

    def test_rejects_user_visible_progress_section_drift(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-orca-orchestrator" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("heartbeat every 1 minute through minute 5", text)
        path.write_text(text.replace("1 minute", "2 minutes", 1), encoding="utf-8")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(
            errors, "shared section '## User-visible progress' has drifted"
        )

    def edit_every_asking_copy(self, package: Path, old: str, new: str) -> None:
        for skill in VALIDATOR.ASKING_SKILLS:
            path = package / "skills" / skill / "SKILL.md"
            text = path.read_text(encoding="utf-8")
            self.assertIn(old, text, skill)
            path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def test_rejects_asking_the_user_section_drift(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-rereview" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("**one at a time**", "**all together**", 1),
                        encoding="utf-8")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(
            errors, "shared section '## Asking the user' has drifted")

    def test_rejects_a_stage_that_stops_without_the_asking_rule(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-provider-bootstrap" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("## Asking the user\n", "## Questions\n", 1),
                        encoding="utf-8")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(
            errors,
            "skills/cc-provider-bootstrap/SKILL.md: missing shared section "
            "'## Asking the user'")

    def test_rejects_an_asking_rule_that_allows_a_wall_of_text(self) -> None:
        package = self.copy_package()
        self.edit_every_asking_copy(
            package, "Never ask with a wall of\n   text.", "Keep it short.")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "does not state 'Never ask with a wall of text'")

    def test_rejects_a_result_that_forbids_the_prose_of_questions(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-rereview" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("The one\nexception is `questions`", text)
        path.write_text(text.replace("The one\nexception is `questions`",
                                     "No\nexception is `questions`", 1), encoding="utf-8")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(
            errors, "skills/cc-rereview/SKILL.md: its result allows identifiers and "
            "status only, with no exception for `questions`")

    def test_rejects_a_questions_example_the_runtime_refuses(self) -> None:
        package = self.copy_package()
        self.edit_every_asking_copy(
            package, '"recommended": "The command\'s exit status"',
            '"recommended": "Both"')

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(
            errors, "`questions` example: `questions[0]`.recommended must be null "
            "or the first option")

    def test_rejects_an_issue_review_question_that_settles_nothing_reported(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-issue-review" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn('"blocks": ["IR-001", "IU-001"]\n    }\n  ],\n  "summary"', text)
        path.write_text(text.replace(
            '"blocks": ["IR-001", "IU-001"]\n    }\n  ],\n  "summary"',
            '"blocks": ["IR-009"]\n    }\n  ],\n  "summary"', 1), encoding="utf-8")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(
            errors, "result example: `questions[0]`.blocks names what the result "
            "does not report")

    def test_rejects_a_skill_missing_workspace_tools_section(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-orchestrator" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("## Workspace tools and evidence\n", "## Tools\n", 1),
            encoding="utf-8",
        )

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(
            errors,
            "skills/cc-orchestrator/SKILL.md: missing shared section '## Workspace tools and evidence'",
        )

    def test_rejects_a_product_name_in_workspace_tools_section(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-orchestrator" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("Semantic navigation", text)
        path.write_text(
            text.replace("Semantic navigation", "Context7 semantic navigation", 1),
            encoding="utf-8",
        )

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(
            errors, "product name 'Context7' in shared workspace-tools section"
        )

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

    def test_rejects_a_description_that_is_not_valid_yaml(self) -> None:
        """#110: `It is read-only: it never ...` made the skills CLI skip a skill."""
        for fragment in ("It is read-only: it never", "read-only #1 rule"):
            with self.subTest(fragment=fragment):
                errors = self.edit_issue_review_skill(
                    "It is read-only and never", fragment)

                self.assert_error_contains(errors, "is not valid YAML")

    def test_a_quoted_description_may_contain_a_colon(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-issue-review" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        start = text.index("description: ") + len("description: ")
        end = text.index("\n", start)
        quoted = '"' + text[start:end].replace("It is read-only and never",
                                                "It is read-only: it never") + '"'
        path.write_text(text[:start] + quoted + text[end:], encoding="utf-8")

        self.assertEqual([], VALIDATOR.validate_package(package))

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

    def edit_issue_review_skill(self, old: str, new: str) -> list[str]:
        package = self.copy_package()
        path = package / "skills" / "cc-issue-review" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return VALIDATOR.validate_package(package)

    def test_rejects_an_issue_review_example_the_runtime_would_refuse(self) -> None:
        errors = self.edit_issue_review_skill('"id": "IR-001"', '"id": "REV-001"')

        self.assert_error_contains(errors, "must be an IR-NNN identifier")

    def test_rejects_an_issue_review_example_with_a_review_disposition(self) -> None:
        errors = self.edit_issue_review_skill(
            '"blocks_readiness": true,', '"blocks_readiness": true, "disposition": "valid",')

        self.assert_error_contains(errors, "carries keys outside the contract: disposition")

    def test_rejects_dropping_an_issue_review_dimension(self) -> None:
        errors = self.edit_issue_review_skill("| `operations` |", "| operations |")

        self.assert_error_contains(errors, "does not define `operations`")

    def test_rejects_dropping_an_issue_review_outcome(self) -> None:
        errors = self.edit_issue_review_skill("| `NEEDS_REFINEMENT` |", "| NEEDS_REFINEMENT |")

        self.assert_error_contains(errors, "does not define `NEEDS_REFINEMENT`")

    def test_rejects_an_orchestrator_without_the_repeated_findings_ladder(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-orca-orchestrator" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("### Repeated-findings ladder", "### Ladder", 1),
                        encoding="utf-8")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(
            errors, "skills/cc-orca-orchestrator/SKILL.md: missing section "
                    "'### Repeated-findings ladder'")

    def test_rejects_a_ladder_that_lost_its_second_rung(self) -> None:
        package = self.copy_package()
        path = package / "skills" / "cc-orchestrator" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("**Second survival**", "**Later**", 1),
                        encoding="utf-8")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "does not state '**Second survival**'")

    def edit_orchestrator(self, skill: str, old: str, new: str) -> list[str]:
        package = self.copy_package()
        path = package / "skills" / skill / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return VALIDATOR.validate_package(package)

    def test_rejects_an_orchestrator_without_the_issue_review_gate(self) -> None:
        errors = self.edit_orchestrator(
            "cc-orchestrator", "## Issue review before implementation", "## Readiness")

        self.assert_error_contains(
            errors, "skills/cc-orchestrator/SKILL.md: missing shared section "
                    "'## Issue review before implementation'")

    def test_rejects_issue_review_gates_that_disagree(self) -> None:
        errors = self.edit_orchestrator(
            "cc-orca-orchestrator", "escalates once:", "escalates twice:")

        self.assert_error_contains(
            errors, "'## Issue review before implementation' has drifted between skills")
        self.assert_error_contains(
            errors, "skills/cc-orca-orchestrator/SKILL.md: '## Issue review before "
                    "implementation' does not state 'escalates once'")

    def test_rejects_an_issue_review_gate_that_lost_the_off_setting(self) -> None:
        package = self.copy_package()
        for skill in VALIDATOR.ORCHESTRATOR_SKILLS:
            path = package / "skills" / skill / "SKILL.md"
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("`issue_review=auto|off`", "`issue_review`", 1),
                            encoding="utf-8")

        errors = VALIDATOR.validate_package(package)

        self.assert_error_contains(errors, "does not state '`issue_review=auto|off`'")

    def test_rejects_an_orca_issue_review_worker_without_a_read_only_workspace(self) -> None:
        errors = self.edit_orchestrator(
            "cc-orca-orchestrator", "isolated review workspace", "review workspace")

        self.assert_error_contains(
            errors, "'### Issue review worker' does not state 'isolated review workspace'")

    def test_rejects_an_orchestrator_that_does_not_detach_the_runtime(self) -> None:
        errors = self.edit_orchestrator(
            "cc-orchestrator", "always start the runtime with\n`--detach`",
            "start the runtime")

        self.assert_error_contains(
            errors, "'## Routing' does not state "
                    "'always start the runtime with `--detach`'")

    def test_rejects_fixed_modes_without_a_distinct_senior_reviewer_stop(self) -> None:
        errors = self.edit_orchestrator(
            "cc-orchestrator",
            "Neither mode\n   assigns a distinct `senior_reviewer`",
            "The modes assign a reviewer",
        )

        self.assert_error_contains(
            errors, "'## Workflow' does not state "
                    "'Neither mode assigns a distinct `senior_reviewer`'")

    def test_rejects_an_implement_result_example_that_is_not_json(self) -> None:
        errors = self.edit_implement_skill(
            '"related_items": ["130", "131"]', '"related_items": ["130", "131"],'
        )

        self.assert_error_contains(errors, "result example is not strict JSON")

    def test_rejects_an_implement_result_example_without_diagnosis(self) -> None:
        errors = self.edit_implement_skill('"diagnosis": {', '"diagnosed": {')

        self.assert_error_contains(errors, "result example has no `diagnosis` object")

    def test_rejects_an_implement_result_example_without_work_units(self) -> None:
        errors = self.edit_implement_skill('"work_units": [', '"units": [')

        self.assert_error_contains(errors, "`work_units` must be a non-empty list")

    def test_rejects_an_unknown_work_unit_rollback_token(self) -> None:
        errors = self.edit_implement_skill(
            '"rollback": "independent"', '"rollback": "sometimes"'
        )

        self.assert_error_contains(errors, "`work_units[0].rollback` is not a known token")

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


class WorkUnitShapeTests(unittest.TestCase):
    """The structured result carries tokens and commit references only."""

    def test_accepts_each_rollback_boundary(self) -> None:
        for rollback in ("independent", "dependent", "irreversible"):
            with self.subTest(rollback=rollback):
                units = [{
                    "id": "WU-1",
                    "commit_sha": "89abcdef0123456789abcdef0123456789abcdef",
                    "rollback": rollback,
                }]

                self.assertEqual([], VALIDATOR.work_units_errors(units))

    def test_accepts_null_sha_for_a_required_single_commit(self) -> None:
        units = [{"id": "WU-1", "commit_sha": None, "rollback": "independent"}]

        self.assertEqual([], VALIDATOR.work_units_errors(units))

    def test_rejects_a_truncated_commit_sha(self) -> None:
        units = [{"id": "WU-1", "commit_sha": "89abcdef", "rollback": "independent"}]

        problems = VALIDATOR.work_units_errors(units)

        self.assertTrue(any("must be a full SHA or null" in problem for problem in problems))

    def test_rejects_duplicate_ids_and_non_token_fields(self) -> None:
        units = [
            {"id": "WU-1", "commit_sha": None, "rollback": "independent"},
            {
                "id": "WU-1",
                "commit_sha": None,
                "rollback": "dependent",
                "description": "free text",
            },
        ]

        problems = VALIDATOR.work_units_errors(units)

        self.assertTrue(any("duplicates `WU-1`" in problem for problem in problems))
        self.assertTrue(any("keys" in problem for problem in problems))


if __name__ == "__main__":
    unittest.main()
