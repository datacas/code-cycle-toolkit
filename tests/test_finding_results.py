from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import run_cycle as cycle  # noqa: E402


EXPECTED = {
    "review": {
        "findings_total": 1,
        "findings_blocking": 1,
        "findings_critical": 0,
        "findings_high": 1,
        "findings_medium": 0,
        "findings_low": 0,
    },
    "rereview": {
        "findings_total": 1,
        "findings_blocking": 1,
        "findings_critical": 0,
        "findings_high": 0,
        "findings_medium": 1,
        "findings_low": 0,
    },
    "resolve": {
        "findings_total": 1,
        "findings_blocking": 1,
        "findings_critical": 0,
        "findings_high": 0,
        "findings_medium": 1,
        "findings_low": 0,
    },
}


def documented_result(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^ORCHESTRATION_RESULT\s*$\s*(?=\{)", text):
        try:
            result, _ = decoder.raw_decode(text[match.end():].lstrip())
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict) and result.get("skill"):
            return result
    raise AssertionError(f"no JSON ORCHESTRATION_RESULT example in {path}")


class DocumentedFindingResultTests(unittest.TestCase):
    def test_documented_result_examples_match_the_runtime_parser(self) -> None:
        examples = (
            ("review", "skills/cc-initial-review/SKILL.md"),
            ("rereview", "skills/cc-rereview/SKILL.md"),
            ("resolve", "skills/cc-resolve-comments/SKILL.md"),
        )
        for role, relative_path in examples:
            with self.subTest(role=role):
                payload = documented_result(ROOT / relative_path)
                self.assertEqual(EXPECTED[role], cycle._findings(payload, role))
                if role == "rereview":
                    self.assertTrue(all(
                        {"severity", "blocks_approval"} <= finding.keys()
                        for finding in payload["verified_findings"]
                    ))

    def test_only_open_findings_count_during_reviews(self) -> None:
        payload = {
            "findings": [
                {"status": "open", "severity": "high", "blocks_approval": True},
                {"status": "resolved", "severity": "low", "blocks_approval": False},
            ],
            "blocking_findings": [],
        }

        result = cycle._findings(payload, "review")

        self.assertEqual(1, result["findings_total"])
        self.assertEqual(0, result["findings_blocking"])
        self.assertEqual(1, result["findings_high"])

    def test_unknown_severity_omits_the_entire_breakdown(self) -> None:
        payload = {"findings": [{"status": "open", "blocks_approval": False}]}

        result = cycle._findings(payload, "review")

        self.assertEqual(1, result["findings_total"])
        self.assertEqual(0, result["findings_blocking"])
        self.assertFalse(any(key.startswith("findings_") and key not in {
            "findings_total", "findings_blocking",
        } for key in result))

    def test_rereview_combines_open_verified_and_new_findings(self) -> None:
        payload = {
            "verified_findings": [
                {"status": "still_open", "severity": "high", "blocks_approval": True},
                {"status": "not_applicable", "severity": "low", "blocks_approval": False},
            ],
            "new_findings": [
                {"status": "open", "severity": "low", "blocks_approval": False},
            ],
            "blocking_findings": ["REV-001"],
        }

        result = cycle._findings(payload, "rereview")

        self.assertEqual(2, result["findings_total"])
        self.assertEqual(1, result["findings_blocking"])
        self.assertEqual(1, result["findings_high"])
        self.assertEqual(1, result["findings_low"])


class CapturedFindingResultTests(unittest.TestCase):
    def test_redacted_runtime_captures_cover_roles_and_executors(self) -> None:
        fixtures = sorted((ROOT / "tests/fixtures/result-contracts").glob("*.json"))
        captures = [json.loads(path.read_text(encoding="utf-8")) for path in fixtures]
        self.assertEqual({"implement", "review", "resolve", "rereview"},
                         {item["capture"]["role"] for item in captures})
        self.assertEqual({"codex", "claude"},
                         {item["capture"]["executor"] for item in captures})

        by_role = {item["capture"]["role"]: item["result"] for item in captures}
        self.assertEqual({}, cycle._findings(by_role["implement"], "implement"))
        self.assertEqual({
            "findings_total": 1, "findings_blocking": 1,
            "findings_critical": 0, "findings_high": 1,
            "findings_medium": 0, "findings_low": 0,
        }, cycle._findings(by_role["review"], "review"))
        self.assertEqual({
            "findings_total": 1, "findings_blocking": 1,
            "findings_critical": 0, "findings_high": 0,
            "findings_medium": 1, "findings_low": 0,
        }, cycle._findings(by_role["resolve"], "resolve"))
        rereview = cycle._findings(by_role["rereview"], "rereview")
        self.assertEqual((2, 1), (rereview["findings_total"],
                                  rereview["findings_blocking"]))
        self.assertFalse(any(key.startswith("findings_") and key not in {
            "findings_total", "findings_blocking",
        } for key in rereview))


if __name__ == "__main__":
    unittest.main()
