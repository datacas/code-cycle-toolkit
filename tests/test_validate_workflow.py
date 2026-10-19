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
            r"&& github\.event\.pull_request\.head\.repo\.fork && "
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
