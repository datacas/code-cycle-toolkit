from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import triage_repair  # noqa: E402
import run_cycle  # noqa: E402
from cycle_history import CycleHistoryStore  # noqa: E402
from telemetry import Telemetry  # noqa: E402


REPO = "datacas/code-cycle-toolkit"
REQUEST = "298"
START_SHA = "0123456789abcdef0123456789abcdef01234567"
HEAD_SHA = "89abcdef0123456789abcdef0123456789abcdef"
BASE = ("#### [CCT-20261021-001] · cheap_coder · "
        "openai/luna→? · high · schema:2")


def comment(line: str) -> str:
    receipt = {
        "schema": 2, "repo": REPO, "change_request_id": REQUEST,
        "stage": "resolve", "attempt_id": "attempt-abc123",
        "head_sha": HEAD_SHA, "status": "RESOLVED", "finding_outcomes": [],
    }
    return f"Resolve report\n\n{line}\n\n<!-- code-cycle-stage {json.dumps(receipt)} -->\n"


class ProposalTests(unittest.TestCase):
    def test_missing_anchor_requires_recorded_start_head(self) -> None:
        body = comment(BASE)
        self.assertIsNone(triage_repair.propose_with_start(REPO, "55", body, None))

        proposal = triage_repair.propose_with_start(REPO, "55", body, START_SHA)

        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertIn(f"triaged:{START_SHA}", proposal.repaired_line)
        self.assertEqual(START_SHA, proposal.sha)
        self.assertEqual("recorded resolve start head", proposal.evidence)

    def test_single_extra_leading_resolve_token_is_repairable_without_sha(self) -> None:
        anchored = BASE.replace(" · schema:2", f" · triaged:{START_SHA} · schema:2")
        body = comment(anchored.replace(" · cheap_coder", " · resolve · cheap_coder"))

        proposal = triage_repair.propose_with_start(REPO, "55", body, None)

        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(anchored, proposal.repaired_line)
        self.assertIsNone(proposal.sha)

    def test_known_pair_requires_start_head_and_removes_only_extra_token(self) -> None:
        malformed = BASE.replace(" · cheap_coder", " · resolve · cheap_coder")
        body = comment(malformed)

        proposal = triage_repair.propose_with_start(REPO, "55", body, START_SHA)

        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(
            BASE.replace(" · schema:2", f" · triaged:{START_SHA} · schema:2"),
            proposal.repaired_line)

    def test_unknown_or_multiple_malformations_remain_manual(self) -> None:
        unknown = comment(BASE + " · mystery:token")
        duplicate = comment(BASE.replace(" · cheap_coder", " · resolve · resolve · cheap_coder"))

        self.assertIsNone(triage_repair.propose_with_start(REPO, "55", unknown, START_SHA))
        self.assertIsNone(triage_repair.propose_with_start(REPO, "55", duplicate, START_SHA))


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proposal = triage_repair.propose_with_start(
            REPO, "55", comment(BASE), START_SHA)
        assert self.proposal is not None

    def test_approval_is_content_bound_and_verified(self) -> None:
        remote = [SimpleNamespace(id="55", body=self.proposal.original)]
        edits = []

        def edit(identifier: str, body: str) -> None:
            edits.append((identifier, body))
            remote[0].body = body

        decision, result = triage_repair.execute_approved(
            self.proposal, f"55:{self.proposal.digest}", lambda: remote, edit)

        self.assertEqual(("approved", "applied"), (decision, result))
        self.assertEqual([("55", self.proposal.repaired)], edits)

    def test_changed_comment_aborts_before_edit(self) -> None:
        remote = [SimpleNamespace(id="55", body=self.proposal.original + " concurrent edit")]
        edits = []

        decision, result = triage_repair.execute_approved(
            self.proposal, f"55:{self.proposal.digest}", lambda: remote,
            lambda identifier, body: edits.append((identifier, body)))

        self.assertEqual(("approved", "failed"), (decision, result))
        self.assertEqual([], edits)

    def test_wrong_digest_is_rejected_and_uncertain_edit_is_not_retried(self) -> None:
        remote = [SimpleNamespace(id="55", body=self.proposal.original)]
        edits = []
        decision, result = triage_repair.execute_approved(
            self.proposal, f"55:{'0' * 64}", lambda: remote,
            lambda identifier, body: edits.append((identifier, body)))
        self.assertEqual(("approval_rejected", "failed"), (decision, result))
        self.assertEqual([], edits)

        def uncertain_edit(identifier: str, body: str) -> None:
            edits.append((identifier, body))
            raise OSError("response lost")

        decision, result = triage_repair.execute_approved(
            self.proposal, f"55:{self.proposal.digest}", lambda: remote, uncertain_edit)
        self.assertEqual(("approved", "failed"), (decision, result))
        self.assertEqual(1, len(edits))


class PersistenceTests(unittest.TestCase):
    def test_missing_or_unmatched_attempt_history_stays_on_manual_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CycleHistoryStore(Path(directory) / "history.sqlite")

            self.assertIsNone(run_cycle._safe_repair_start_head(
                store, "attempt-abc123", REPO, REQUEST))
            store.record_attempt_start(
                "attempt-abc123", REPO, REQUEST, "resolve", START_SHA)
            self.assertIsNone(run_cycle._safe_repair_start_head(
                store, "invalid", REPO, REQUEST))
            with mock.patch.object(
                    store, "attempt_start_head",
                    side_effect=sqlite3.OperationalError("history unavailable")):
                self.assertIsNone(run_cycle._safe_repair_start_head(
                    store, "attempt-abc123", REPO, REQUEST))

    def test_attempt_start_head_is_bound_to_exact_request_and_resolve_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CycleHistoryStore(Path(directory) / "history.sqlite")

            store.record_attempt_start("attempt-abc123", REPO, REQUEST, "resolve", START_SHA)

            self.assertEqual(START_SHA,
                             store.attempt_start_head("attempt-abc123", REPO, REQUEST))
            self.assertIsNone(store.attempt_start_head("attempt-abc123", REPO, "299"))

    def test_audit_keeps_digests_and_references_without_comment_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Telemetry(Path(directory) / "telemetry.sqlite")
            original = "private comment body"
            repaired = "corrected comment body"
            store.record_triage_repair(
                repo=REPO, pull_number=REQUEST, comment_id="55",
                original_sha256=hashlib.sha256(original.encode()).hexdigest(),
                repaired_sha256=hashlib.sha256(repaired.encode()).hexdigest(),
                proposal_sha256="a" * 64, triaged_sha=START_SHA,
                repair_type="recorded resolve start head", decision="approved",
                result="applied")

            with sqlite3.connect(store.path) as connection:
                row = connection.execute(
                    "SELECT repo, pull_number, comment_id, original_sha256, "
                    "repaired_sha256, proposal_sha256, triaged_sha, decision, result "
                    "FROM triage_repair_audit").fetchone()

            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual((REPO, REQUEST, "55"), row[:3])
            self.assertNotIn(original, repr(row))
            self.assertNotIn(repaired, repr(row))


if __name__ == "__main__":
    unittest.main()
