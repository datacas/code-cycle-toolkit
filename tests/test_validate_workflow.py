from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"


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


if __name__ == "__main__":
    unittest.main()
