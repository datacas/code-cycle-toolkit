from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"
sys.path.insert(0, str(ROOT / "scripts"))

from run_windows_test_shard import validate_assignments


class ValidateWorkflowTests(unittest.TestCase):
    def test_validation_triggers_and_concurrency_preserve_required_runs(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        triggers = re.search(
            r"(?ms)^on:\n(?P<body>.*?)(?=^[^\s#][^\n]*:|\Z)", workflow
        )
        self.assertIsNotNone(triggers, "workflow must declare top-level triggers")
        trigger_block = triggers.group("body")
        self.assertRegex(trigger_block, r"(?m)^  push:\n    branches: \[main\]$")
        self.assertRegex(trigger_block, r"(?m)^  pull_request:\s*$")
        self.assertRegex(trigger_block, r"(?m)^  workflow_dispatch:\s*$")

        self.assertRegex(
            workflow,
            r"(?m)^  group: validate-\$\{\{ github\.event_name == '"
            r"pull_request' && format\('pr-\{0\}', "
            r"github\.event\.pull_request\.number\) \|\| "
            r"format\('\{0\}-\{1\}', github\.event_name, github\.sha\) \}\}$",
        )
        self.assertRegex(
            workflow,
            r"(?m)^  cancel-in-progress: \$\{\{ github\.event_name == '"
            r"pull_request' \}\}$",
        )

    def test_fork_pr_runner_routing_preserves_required_check(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        job = re.search(
            r"(?ms)^  validate:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)",
            workflow,
        )
        self.assertIsNotNone(job, "workflow must declare the required Linux job")
        body = job.group("body")
        self.assertRegex(
            body,
            r"(?m)^    name: Skills, manifests, and Bash installer$",
        )
        self.assertRegex(
            body,
            r"(?m)^    runs-on: \$\{\{ github\.event_name == 'pull_request' "
            r"&& github\.event\.pull_request\.head\.repo\.full_name != "
            r"github\.repository && "
            r"'ubuntu-latest' \|\| fromJSON\('\[\"self-hosted\","
            r"\"Linux\",\"X64\"\]'\) \}\}$",
        )
        self.assertNotRegex(body, r"(?m)^    if:")

    def test_windows_test_shards_feed_the_required_aggregating_job(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertRegex(
            workflow,
            r"(?ms)^  windows-test-shards:\n.*?^    name: Windows test shard "
            r"\(\$\{\{ matrix\.shard \}\}\).*?^      fail-fast: false",
        )
        matrix = re.search(
            r"(?ms)^  windows-test-shards:.*?^        shard: "
            r"\[(?P<names>[^\]]+)\]",
            workflow,
        )
        self.assertIsNotNone(matrix)
        manifest = json.loads(
            (ROOT / "scripts" / "windows-test-shards.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            list(manifest["shards"]),
            [name.strip() for name in matrix.group("names").split(",")],
        )
        self.assertRegex(
            workflow,
            r"(?ms)^  powershell-installer:\n"
            r"    name: PowerShell installer\n"
            r"    runs-on: windows-latest\n"
            r"    needs: windows-test-shards\n"
            r"    if: \$\{\{ always\(\) \}\}.*?^      - name: Require all Windows test shards to pass",
        )
        self.assertNotIn("pytest-xdist", workflow)
        self.assertNotIn("pytest -n", workflow)
        self.assertRegex(
            workflow,
            r"(?m)^        if: \$\{\{ always\(\) && "
            r"needs\.windows-test-shards\.result != 'success' \}\}$",
        )

    def job_steps(self, job: str) -> dict[str, str]:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        body = re.search(
            rf"(?ms)^  {re.escape(job)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", workflow
        )
        self.assertIsNotNone(body, f"workflow must declare {job}")
        steps = re.split(r"(?m)^      - name: ", body.group("body"))[1:]
        return {step.split("\n", 1)[0]: step for step in steps}

    def test_every_job_classifies_the_change_and_fails_closed(self) -> None:
        # Steps that run whatever the classification: the classification itself,
        # setup it depends on, and the dependency install the fast path shares.
        always = {
            "Check out repository",
            "Set up Python",
            "Classify the change",
            "Install the configuration parser",
            "Install the Windows test dependencies",
            "Require all Windows test shards to pass",
        }
        for job in ("validate", "windows-test-shards", "powershell-installer"):
            steps = self.job_steps(job)
            with self.subTest(job=job):
                classify = steps["Classify the change"]
                self.assertIn("id: classify", classify)
                self.assertIn("continue-on-error: true", classify)
                self.assertIn("scripts/fast_path.py classify", classify)
                self.assertIn("github.event.pull_request.base.sha || github.event.before", classify)
                self.assertIn("github.event.pull_request.head.sha || github.sha", classify)
                self.assertIn("fetch-depth: 0", steps["Check out repository"])
                for name, step in steps.items():
                    if name in always:
                        continue
                    # `!= 'fast'`, never `== 'full'`: an unset output runs in full.
                    self.assertRegex(
                        step,
                        r"(?m)^        if: \$\{\{ steps\.classify\.outputs\.mode (!=|==) 'fast' \}\}$",
                        name,
                    )
                self.assertNotIn("== 'full'", WORKFLOW.read_text(encoding="utf-8"))

    def test_the_fast_path_runs_what_the_decisions_require(self) -> None:
        validate = self.job_steps("validate")["Run the fast path for a non-code change"]
        shard = self.job_steps("windows-test-shards")["Run the fast path for a non-code change"]
        installer = self.job_steps("powershell-installer")["Run the fast path for a non-code change"]

        self.assertIn("run-fast --job validate --scan", validate)
        self.assertIn("run-fast --job windows-shard --shard ${{ matrix.shard }}", shard)
        self.assertNotIn("--scan", shard)
        self.assertIn("run-fast --job powershell-installer --scan", installer)
        for step in (validate, shard):
            self.assertIn("FAST_PATH_TESTS: ${{ steps.classify.outputs.tests }}", step)
            self.assertIn("steps.classify.outputs.mode == 'fast'", step)

    def test_the_expensive_validation_is_skipped_only_on_the_fast_path(self) -> None:
        for job, names in {
            "validate": ("Validate skills and package metadata", "Check Bash installer syntax",
                         "Discover every skill through the recommended CLI"),
            "windows-test-shards": ("Validate shard assignments and run tests",),
            "powershell-installer": ("Validate package with Windows Python",
                                     "Test project installation for all hosts"),
        }.items():
            steps = self.job_steps(job)
            for name in names:
                with self.subTest(job=job, step=name):
                    self.assertIn("steps.classify.outputs.mode != 'fast'", steps[name])

    def test_full_runs_record_that_the_tests_passed(self) -> None:
        for job in ("validate", "windows-test-shards", "powershell-installer"):
            with self.subTest(job=job):
                step = self.job_steps(job)["Record the validation outcome"]
                self.assertIn("scripts/fast_path.py summary --job", step)
                self.assertIn("steps.classify.outputs.mode != 'fast'", step)

    def test_windows_shard_manifest_covers_each_test_module_once(self) -> None:
        manifest = json.loads(
            (ROOT / "scripts" / "windows-test-shards.json").read_text(
                encoding="utf-8"
            )
        )
        expected = {
            path.relative_to(ROOT / "tests")
            .with_suffix("")
            .as_posix()
            .replace("/", ".")
            for path in (ROOT / "tests").rglob("test_*.py")
        }

        validate_assignments(manifest["shards"], expected)

    def test_windows_shard_validation_rejects_missing_and_duplicate_modules(self) -> None:
        with self.assertRaisesRegex(ValueError, "not assigned to a shard"):
            validate_assignments({"one": ["test_a"]}, {"test_a", "test_b"})

        with self.assertRaisesRegex(ValueError, "more than one shard"):
            validate_assignments(
                {"one": ["test_a"], "two": ["test_a"]}, {"test_a"}
            )


if __name__ == "__main__":
    unittest.main()
