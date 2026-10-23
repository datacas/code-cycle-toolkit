from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fast_path  # noqa: E402


def git(directory: Path, *arguments: str) -> str:
    # Identity is set per command: a CI runner has no global one.
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *arguments],
        cwd=directory, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


class AllowlistTests(unittest.TestCase):
    def test_allowlisted_paths(self) -> None:
        for path in (
            ".code-cycle.yml",
            "CONTRIBUTING.md",
            "AGENTS.md",
            "SECURITY.md",
            "CHANGELOG.md",
            "docs/configuration.md",
            "docs/research/evidence/x/audit.py",
            "docs/Plan de instrumentación y routing.md",
            ".github/ISSUE_TEMPLATE/bug.yml",
        ):
            with self.subTest(path=path):
                self.assertTrue(fast_path.is_allowlisted(path))

    def test_everything_else_is_not(self) -> None:
        for path in (
            "README.md",
            "readme.md",
            "skills/cc-verify/SKILL.md",
            "skills/cc-orchestrator/references/codex-plugin-cc.md",
            "scripts/run_cycle.py",
            "scripts/fast_path.py",
            "scripts/fast-path-tests.json",
            "tests/test_fast_path.py",
            "tests/fixtures/issue-review/README.md",
            ".claude-plugin/plugin.json",
            ".codex-plugin/plugin.json",
            ".github/workflows/validate.yml",
            ".github/workflows/release.yml",
            ".github/workflows/other.yml",
            ".github/CODEOWNERS",
            "opencode.jsonc",
            "notes/extra.md",
            "docs",
            "docs/",
            "docs/../scripts/run_cycle.py",
            "docs/./x.md",
            "docs\\x.md",
            "",
        ):
            with self.subTest(path=path):
                self.assertFalse(fast_path.is_allowlisted(path))

    def test_the_allowlist_never_reaches_what_the_validator_reads(self) -> None:
        validator = (ROOT / "scripts" / "validate-package.py").read_text(encoding="utf-8")
        for read in ('"skills"', '"README.md"', '"opencode.jsonc"', '".claude-plugin/plugin.json"'):
            self.assertIn(read, validator)
        for path in ("README.md", "opencode.jsonc", "skills/cc-verify/SKILL.md",
                     "scripts/run_cycle.py", ".claude-plugin/plugin.json"):
            self.assertFalse(fast_path.is_allowlisted(path), path)


class ClassifyPathsTests(unittest.TestCase):
    def test_allowlisted_only_takes_the_fast_path(self) -> None:
        result = fast_path.classify_paths([".code-cycle.yml", "docs/workflows.md"])

        self.assertEqual(fast_path.FAST, result.mode)
        self.assertEqual((".code-cycle.yml", "docs/workflows.md"), result.paths)

    def test_mixed_change_runs_in_full_and_names_the_cause(self) -> None:
        result = fast_path.classify_paths(["docs/workflows.md", "scripts/run_cycle.py"])

        self.assertEqual(fast_path.FULL, result.mode)
        self.assertEqual(("scripts/run_cycle.py",), result.paths)

    def test_workflow_changes_run_in_full(self) -> None:
        for path in (".github/workflows/validate.yml", ".github/workflows/release.yml",
                     "scripts/fast_path.py", "scripts/fast-path-tests.json"):
            with self.subTest(path=path):
                result = fast_path.classify_paths(["docs/workflows.md", path])
                self.assertEqual(fast_path.FULL, result.mode)
                self.assertEqual((path,), result.paths)

    def test_manifest_changes_run_in_full(self) -> None:
        for path in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
            with self.subTest(path=path):
                self.assertEqual(fast_path.FULL, fast_path.classify_paths([path]).mode)

    def test_an_empty_diff_runs_in_full(self) -> None:
        result = fast_path.classify_paths([])

        self.assertEqual(fast_path.FULL, result.mode)
        self.assertIn("empty", result.reason)

    def test_a_changelog_edit_outside_the_release_headings_is_allowlisted(self) -> None:
        self.assertEqual(
            fast_path.FAST, fast_path.classify_paths(["CHANGELOG.md"], release_headings_changed=False).mode
        )

    def test_a_changelog_release_heading_runs_in_full(self) -> None:
        result = fast_path.classify_paths(["CHANGELOG.md"], release_headings_changed=True)

        self.assertEqual(fast_path.FULL, result.mode)
        self.assertEqual(("CHANGELOG.md",), result.paths)

    def test_each_path_group_triggers_its_mapped_tests(self) -> None:
        mapping = fast_path.load_mapping()
        expected = {
            ".code-cycle.yml": "test_run_cycle",
            "docs/telemetry.md": "test_permission_view",
            "docs/configuration.md": "test_validate_package",
            "docs/spikes/playwright-behavioral-capabilities-fixture/run.sh": "test_spike_fixture_runner",
        }
        for path, module in expected.items():
            with self.subTest(path=path):
                result = fast_path.classify_paths([path], mapping=mapping)
                self.assertEqual(fast_path.FAST, result.mode, result.reason)
                self.assertTrue(result.tests)
                self.assertIn(module, {test.split(".")[0] for test in result.tests})
                self.assertEqual(
                    {test.split(".")[0] for test in result.tests},
                    {test.split(".")[0] for group in mapping.values()
                     if path in group["paths"] or any(
                         fast_path.pattern_matches(p, path) for p in group["paths"])
                     for test in group["tests"]},
                )

    def test_every_mapped_test_exists(self) -> None:
        sys.path.insert(0, str(ROOT / "tests"))
        loader = unittest.TestLoader()
        for group in fast_path.load_mapping().values():
            for entry in group["tests"]:
                with self.subTest(entry=entry):
                    suite = loader.loadTestsFromName(entry)
                    self.assertGreater(suite.countTestCases(), 0)
        self.assertEqual([], loader.errors)

    def test_a_change_outside_the_groups_maps_no_tests(self) -> None:
        self.assertEqual((), fast_path.classify_paths(["docs/workflows.md"]).tests)

    def test_a_failing_guard_runs_in_full(self) -> None:
        result = fast_path.classify_paths(["docs/workflows.md"], guard=lambda: ["unregistered"])

        self.assertEqual(fast_path.FULL, result.mode)
        self.assertIn("guard failed", result.reason)

    def test_an_unreadable_mapping_runs_in_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "mapping.json"
            bad.write_text("{", encoding="utf-8")
            original = fast_path.MAPPING_PATH
            fast_path.MAPPING_PATH = bad
            try:
                result = fast_path.classify_paths(["docs/workflows.md"], guard=lambda: [])
            finally:
                fast_path.MAPPING_PATH = original

        self.assertEqual(fast_path.FULL, result.mode)


class MappingTests(unittest.TestCase):
    def write(self, directory: str, data: object) -> Path:
        path = Path(directory) / "mapping.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_the_shipped_mapping_loads(self) -> None:
        self.assertTrue(fast_path.load_mapping())

    def test_rejects_malformed_mappings(self) -> None:
        cases = {
            "no groups": {},
            "extra key": {"groups": {"a": {"paths": ["docs/a.md"], "tests": ["test_a"], "x": 1}}},
            "empty tests": {"groups": {"a": {"paths": ["docs/a.md"], "tests": []}}},
            "path outside the allowlist": {"groups": {"a": {"paths": ["scripts/a.py"], "tests": ["test_a"]}}},
            "not a module name": {"groups": {"a": {"paths": ["docs/a.md"], "tests": ["../evil"]}}},
        }
        for label, data in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    fast_path.load_mapping(self.write(directory, data))


class GuardTests(unittest.TestCase):
    MAPPING = {
        "telemetry": {"paths": ["docs/telemetry.md"], "tests": ["test_registered"]},
        "spikes": {"paths": ["docs/spikes/**"], "tests": ["test_registered"]},
        "config": {"paths": [".code-cycle.yml"], "tests": ["test_registered"]},
    }

    def guard(self, **sources: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            tests = Path(directory)
            for name in ("test_registered", *sources):
                (tests / f"{name}.py").write_text(sources.get(name, "X = 1\n"), encoding="utf-8")
            return fast_path.guard_errors(tests, self.MAPPING)

    def test_the_repository_has_no_unregistered_dependency(self) -> None:
        self.assertEqual([], fast_path.guard_errors())

    def test_a_registered_dependency_passes(self) -> None:
        source = (
            "from pathlib import Path\n"
            "ROOT = Path(__file__).resolve().parent.parent\n"
            "TEXT = (ROOT / 'docs' / 'telemetry.md').read_text()\n"
        )

        self.assertEqual([], self.guard(test_registered=source))

    def test_an_unregistered_new_dependency_is_reported(self) -> None:
        source = (
            "from pathlib import Path\n"
            "ROOT = Path(__file__).resolve().parent.parent\n"
            "TEXT = (ROOT / 'docs' / 'new-page.md').read_text()\n"
        )

        errors = self.guard(test_new=source)

        self.assertEqual(1, len(errors))
        self.assertIn("tests/test_new.py:3", errors[0])
        self.assertIn("docs/new-page.md", errors[0])

    def test_a_module_registered_for_another_path_is_still_reported(self) -> None:
        source = (
            "from pathlib import Path\n"
            "ROOT = Path(__file__).resolve().parent.parent\n"
            "TEXT = (ROOT / 'docs' / 'telemetry.md').read_text()\n"
        )

        self.assertEqual(1, len(self.guard(test_other=source)))

    def test_every_way_of_building_a_path_is_seen(self) -> None:
        for expression in (
            "ROOT / '.code-cycle.yml'",
            "ROOT.joinpath('docs', 'a.md')",
            "os.path.join(ROOT, 'docs', 'a.md')",
            "ROOT / 'docs' / name",
            "ROOT / 'docs'",
            "ROOT.glob('*.md')",
            "(Path(__file__).resolve().parents[1] / 'CONTRIBUTING.md')",
            "ROOT / rc.CONFIG_NAME",
        ):
            with self.subTest(expression=expression):
                source = (
                    "import os\nfrom pathlib import Path\n"
                    "ROOT = Path(__file__).resolve().parent.parent\n"
                    f"VALUE = {expression}\n"
                )
                self.assertEqual(1, len(self.guard(test_new=source)))

    def test_paths_that_cannot_be_allowlisted_or_are_not_from_the_root_are_ignored(self) -> None:
        source = (
            "from pathlib import Path\nimport tempfile\n"
            "ROOT = Path(__file__).resolve().parent.parent\n"
            "A = ROOT / 'skills' / 'x' / 'SKILL.md'\n"
            "B = ROOT / 'README.md'\n"
            "C = Path(tempfile.mkdtemp()) / '.code-cycle.yml'\n"
            "D = ROOT / 'tests' / 'fixtures' / 'a.md'\n"
        )

        self.assertEqual([], self.guard(test_new=source))

    def test_a_group_that_lists_a_missing_module_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            errors = fast_path.guard_errors(
                Path(directory), {"a": {"paths": ["docs/a.md"], "tests": ["test_missing"]}}
            )

        self.assertTrue(any("test_missing" in error for error in errors))

    def test_an_unparseable_test_module_is_reported(self) -> None:
        self.assertTrue(self.guard(test_broken="def (:\n"))


class RepositoryDiffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.directory.name)
        git(self.repo, "init", "-q", "-b", "main")
        self.commit({"README.md": "base\n", "docs/a.md": "a\n", "CHANGELOG.md": "# Changelog\n\n## [1.0.0]\n\n- x\n"})
        self.base = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def commit(self, files: dict[str, str]) -> str:
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")
        return git(self.repo, "rev-parse", "HEAD")

    def decide(self, event: str, head: str) -> fast_path.Classification:
        return fast_path.decide(event, self.base, head, self.repo)

    def test_a_pull_request_that_only_edits_documentation(self) -> None:
        head = self.commit({"docs/a.md": "changed\n"})

        for event in ("pull_request", "push"):
            with self.subTest(event=event):
                result = self.decide(event, head)
                self.assertEqual(fast_path.FAST, result.mode, result.reason)
                self.assertEqual(("docs/a.md",), result.paths)

    def test_a_mixed_change_runs_in_full(self) -> None:
        head = self.commit({"docs/a.md": "changed\n", "scripts/x.py": "pass\n"})

        result = self.decide("pull_request", head)

        self.assertEqual(fast_path.FULL, result.mode)
        self.assertEqual(("scripts/x.py",), result.paths)

    def test_a_deleted_or_renamed_file_counts_both_paths(self) -> None:
        (self.repo / "scripts").mkdir()
        git(self.repo, "mv", "docs/a.md", "scripts/a.md")
        git(self.repo, "commit", "-q", "-m", "move")
        head = git(self.repo, "rev-parse", "HEAD")

        result = self.decide("pull_request", head)

        self.assertEqual(fast_path.FULL, result.mode)
        self.assertEqual(("scripts/a.md",), result.paths)

    def test_a_pull_request_compares_with_the_merge_base(self) -> None:
        main = self.commit({"scripts/main_only.py": "pass\n"})
        git(self.repo, "checkout", "-q", "-b", "topic", self.base)
        head = self.commit({"docs/a.md": "topic\n"})

        pull_request = fast_path.decide("pull_request", main, head, self.repo)
        push = fast_path.decide("push", main, head, self.repo)

        self.assertEqual(fast_path.FAST, pull_request.mode, pull_request.reason)
        self.assertEqual(fast_path.FULL, push.mode)

    def test_changelog_release_headings_run_in_full(self) -> None:
        notes = self.commit({"CHANGELOG.md": "# Changelog\n\n## [1.0.0]\n\n- x\n- y\n"})
        self.assertEqual(fast_path.FAST, self.decide("push", notes).mode)

        release = self.commit({"CHANGELOG.md": "# Changelog\n\n## [1.1.0]\n\n- z\n\n## [1.0.0]\n\n- x\n- y\n"})
        result = self.decide("push", release)

        self.assertEqual(fast_path.FULL, result.mode)
        self.assertIn("release heading", result.reason)

    def test_an_empty_diff_runs_in_full(self) -> None:
        for event in ("pull_request", "push"):
            with self.subTest(event=event):
                result = self.decide(event, self.base)
                self.assertEqual(fast_path.FULL, result.mode)
                self.assertIn("empty", result.reason)

    def test_classification_fails_closed(self) -> None:
        head = self.commit({"docs/a.md": "changed\n"})
        cases = {
            "unknown event": ("workflow_dispatch", self.base, head),
            "no event": ("", self.base, head),
            "no previous commit on a push": ("push", "0" * 40, head),
            "missing base": ("pull_request", "", head),
            "abbreviated head": ("pull_request", self.base, head[:7]),
            "option as a commit": ("pull_request", "--output=x", head),
            "commit that does not exist": ("pull_request", "1" * 40, head),
        }
        for label, (event, base, tip) in cases.items():
            with self.subTest(label):
                result = fast_path.decide(event, base, tip, self.repo)
                self.assertEqual(fast_path.FULL, result.mode)

    def test_a_directory_that_is_not_a_repository_runs_in_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = fast_path.decide("pull_request", self.base, self.base, Path(directory))

        self.assertEqual(fast_path.FULL, result.mode)


class ReportTests(unittest.TestCase):
    def test_the_classification_summary_names_the_class_and_the_paths(self) -> None:
        fast = fast_path.render_classification(
            fast_path.Classification(fast_path.FAST, "r", ("docs/a.md",), ("test_x",))
        )
        full = fast_path.render_classification(
            fast_path.Classification(fast_path.FULL, "outside", ("scripts/`x`.py",))
        )

        self.assertIn("`non_code`", fast)
        self.assertIn("`docs/a.md`", fast)
        self.assertIn("`test_x`", fast)
        self.assertIn("`full`", full)
        self.assertIn("`scripts/?x?.py`", full)

    def test_the_outcome_distinguishes_skipped_from_run_tests(self) -> None:
        skipped = fast_path.render_outcome(
            "windows-shard", fast_path.SKIPPED_NON_CODE, ["mapped tests: none apply"],
            fast_path.SKIPPED_VALIDATIONS["windows-shard"], "windows-2",
        )
        passed = fast_path.render_outcome(
            "windows-shard", fast_path.TESTS_PASSED, fast_path.FULL_VALIDATIONS["windows-shard"], (),
            "windows-2",
        )

        self.assertIn("`SKIPPED_NON_CODE`", skipped)
        self.assertNotIn("TESTS_PASSED", skipped)
        self.assertIn("Validations skipped:\n- unit tests of this shard not mapped", skipped)
        self.assertIn("Windows test shard (windows-2): `TESTS_PASSED`", passed)
        self.assertIn("Validations skipped:\n- none", passed)

    def test_every_job_has_a_label_and_validation_lists(self) -> None:
        self.assertEqual(set(fast_path.JOB_LABELS), set(fast_path.FULL_VALIDATIONS))
        self.assertEqual(set(fast_path.JOB_LABELS), set(fast_path.SKIPPED_VALIDATIONS))


class CommandTests(unittest.TestCase):
    def run_script(self, *arguments: str, **environment: str) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GITHUB_", "FAST_PATH_"))}
        env.update(environment)
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "fast_path.py"), *arguments],
            capture_output=True, text=True, env=env, cwd=ROOT,
        )

    def test_classify_fails_closed_and_still_exits_successfully(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            summary = Path(directory) / "summary.md"

            result = self.run_script(
                "classify", GITHUB_EVENT_NAME="schedule",
                GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(summary),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("mode=full\n", output.read_text(encoding="utf-8"))
            self.assertIn("`full`", summary.read_text(encoding="utf-8"))

    def test_run_fast_rejects_a_test_name_that_is_not_a_module(self) -> None:
        result = self.run_script("run-fast", "--job", "validate", FAST_PATH_TESTS="../../etc/passwd")

        self.assertNotEqual(0, result.returncode)

    def test_run_fast_runs_the_scan_and_the_mapped_tests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"

            result = self.run_script(
                "run-fast", "--job", "validate", "--scan",
                FAST_PATH_TESTS="test_spike_fixture_runner", GITHUB_STEP_SUMMARY=str(summary),
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            text = summary.read_text(encoding="utf-8")
            self.assertIn("`SKIPPED_NON_CODE`", text)
            self.assertIn("privacy and metadata-sidecar scan", text)
            self.assertIn("mapped tests: test_spike_fixture_runner", text)

    def test_run_fast_keeps_only_the_tests_of_the_shard(self) -> None:
        manifest = json.loads((ROOT / "scripts" / "windows-test-shards.json").read_text(encoding="utf-8"))
        shard = next(name for name, modules in manifest["shards"].items()
                     if "test_spike_fixture_runner" not in modules)
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"

            result = self.run_script(
                "run-fast", "--job", "windows-shard", "--shard", shard,
                FAST_PATH_TESTS="test_spike_fixture_runner", GITHUB_STEP_SUMMARY=str(summary),
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("mapped tests: none apply", summary.read_text(encoding="utf-8"))

    def test_summary_records_a_full_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"

            result = self.run_script("summary", "--job", "validate", GITHUB_STEP_SUMMARY=str(summary))

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("`TESTS_PASSED`", summary.read_text(encoding="utf-8"))


class ScanOnlyTests(unittest.TestCase):
    def test_the_scan_is_reusable_and_reports_what_the_validator_reports(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "validate_package_for_scan", ROOT / "scripts" / "validate-package.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "docs").mkdir()
            # Built from parts: the scan this test exercises also scans this file.
            (package / "docs" / "guide.md").write_text(
                "see " + "/ho" + "me/someone/project\n", encoding="utf-8"
            )
            (package / "docs" / "guide.md.Zone.Identifier").write_text("x", encoding="utf-8")

            errors = module.scan_for_private_data(package)

        normalized_errors = [error.replace("\\", "/") for error in errors]
        self.assertEqual(2, len(errors), errors)
        self.assertTrue(
            any("possible private data in docs/guide.md" in error for error in normalized_errors)
        )
        self.assertTrue(any("metadata sidecar" in error for error in normalized_errors))
        self.assertEqual([], module.scan_for_private_data(ROOT))


if __name__ == "__main__":
    unittest.main()
