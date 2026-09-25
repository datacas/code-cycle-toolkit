from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import run_cycle as cycle  # noqa: E402
sys.path.insert(0, str(ROOT / "tests/fakes"))
import fake_agent  # noqa: E402


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


def key_paths(value: object, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    paths = set()
    if isinstance(value, dict):
        for key, child in value.items():
            path = prefix + (key,)
            paths.add(path)
            paths.update(key_paths(child, path))
    elif isinstance(value, list):
        for child in value:
            paths.update(key_paths(child, prefix))
    return paths


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
            "blocking_findings": ["REV-1"],
        }

        result = cycle._findings(payload, "review")

        self.assertEqual(1, result["findings_total"])
        self.assertEqual(1, result["findings_blocking"])
        self.assertEqual(1, result["findings_high"])

    def test_conflicting_blocking_signals_make_counts_unknown(self) -> None:
        result = cycle._findings({
            "findings": [
                {"id": "REV-001", "status": "open", "severity": "high",
                 "blocks_approval": True},
            ],
            "blocking_findings": [],
        }, "review")
        self.assertEqual({}, result)

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
        # The captured rereview repeats an ID with conflicting statuses, so
        # its finding counts are intentionally unknown after duplicate checks.
        self.assertEqual({}, cycle._findings(by_role["rereview"], "rereview"))
        for item in captures:
            with self.subTest(provenance=item["capture"]["role"]):
                self.assertTrue({"host", "model", "skill_commit"} <=
                                item["capture"].keys())

        expected_statuses = {
            "implement": "IMPLEMENTED",
            "review": "CHANGES_REQUESTED",
            "resolve": "RESOLVED",
            "rereview": "BLOCKED",
        }
        expected_tests = {
            "implement": {"tests_passed": True},
            "review": {},
            "resolve": {"tests_passed": True},
            "rereview": {},
        }
        for role, payload in by_role.items():
            with self.subTest(role=role):
                self.assertEqual(expected_statuses[role], cycle._status_of(payload))
                self.assertEqual(expected_tests[role], cycle._tests(payload))

    def test_fake_payload_keys_are_covered_by_fixture_or_documented_contract(self) -> None:
        fixtures = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (ROOT / "tests/fixtures/result-contracts").glob("*.json")
        ]
        fixture_by_role = {
            item["capture"]["role"]: item["result"] for item in fixtures
        }
        examples = {
            "review": documented_result(ROOT / "skills/cc-initial-review/SKILL.md"),
            "rereview": documented_result(ROOT / "skills/cc-rereview/SKILL.md"),
            "resolve": documented_result(ROOT / "skills/cc-resolve-comments/SKILL.md"),
        }
        for role in ("implement", "review", "resolve", "rereview"):
            allowed = key_paths(fixture_by_role[role])
            if role in examples:
                allowed.update(key_paths(examples[role]))
            emitted = fake_agent.result_for_role(
                role, "IMPLEMENTED" if role == "implement" else "CHANGES_REQUESTED")
            with self.subTest(role=role):
                self.assertLessEqual(key_paths(emitted), allowed)

    def test_missing_or_conflicting_finding_statuses_are_unknown(self) -> None:
        self.assertEqual({}, cycle._findings({
            "findings": [{"id": "REV-001", "severity": "high"}],
        }, "review"))
        self.assertEqual({}, cycle._findings({
            "new_findings": [{"id": "REV-001", "status": "open"}],
            "verified_findings": [
                {"id": "REV-001", "status": "still_open", "severity": "high"},
                {"id": "REV-001", "status": "resolved", "severity": "high"},
            ],
        }, "rereview"))

    def test_identical_duplicate_verified_findings_count_once(self) -> None:
        result = cycle._findings({
            "verified_findings": [
                {"id": "REV-001", "status": "still_open", "severity": "high",
                 "blocks_approval": True},
                {"id": "REV-001", "status": "still_open", "severity": "high",
                 "blocks_approval": True},
            ],
        }, "rereview")
        self.assertEqual(1, result["findings_total"])

    def test_duplicate_blocking_ids_count_once(self) -> None:
        result = cycle._findings({
            "findings": [
                {"id": "REV-001", "status": "open", "severity": "high",
                 "blocks_approval": True},
            ],
            "blocking_findings": ["REV-001", "REV-001"],
        }, "review")
        self.assertEqual(1, result["findings_total"])
        self.assertEqual(1, result["findings_blocking"])

    def test_malformed_resolve_entry_is_unknown_with_or_without_blocker_list(self) -> None:
        for blocking in (None, ["REV-1"]):
            payload = {"unresolved_findings": ["REV-1"]}
            if blocking is not None:
                payload["blocking_findings"] = blocking
            with self.subTest(blocking=blocking):
                self.assertEqual({}, cycle._findings(payload, "resolve"))


if __name__ == "__main__":
    unittest.main()
