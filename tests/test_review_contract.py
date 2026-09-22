from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import review_contract as contract  # noqa: E402


LEGACY_HEADER = "#### [REV-004] · medium · open · blocks:yes — Short title"
CURRENT_HEADER = (
    "#### [REV-004] · medium · resolved · valid · blocks:yes — Short title"
)
REVIEW_RUN = (
    "#### [CCR-20260918-001] · senior_reviewer · "
    "anthropic/sonnet-5→sonnet-5 · high · schema:1"
)
PRE_EDIT_SHA = "0123456789abcdef0123456789abcdef01234567"
POST_EDIT_SHA = "89abcdef0123456789abcdef0123456789abcdef"
TRIAGE_RUN = (
    "#### [CCT-20260918-001] · cheap_coder · openai/luna-high→luna-high"
    f" · high · triaged:{PRE_EDIT_SHA} · schema:1"
)


class LegacyFindingHeaderTests(unittest.TestCase):
    def test_legacy_header_still_parses(self) -> None:
        finding = contract.parse_finding_header(LEGACY_HEADER)

        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual("REV-004", finding.id)
        self.assertEqual("medium", finding.severity)
        self.assertEqual("open", finding.status)
        self.assertTrue(finding.blocks_approval)
        self.assertEqual("Short title", finding.title)

    def test_legacy_header_means_untriaged(self) -> None:
        finding = contract.parse_finding_header(LEGACY_HEADER)

        assert finding is not None
        self.assertEqual(contract.UNTRIAGED, finding.disposition)
        self.assertFalse(finding.triaged)
        self.assertTrue(finding.legacy)

    def test_legacy_change_request_is_not_blocked_by_the_migration(self) -> None:
        comment = "\n".join(
            [
                "## Revisión",
                "",
                LEGACY_HEADER,
                "Algo que arreglar.",
                "#### [REV-005] · low · resolved · blocks:no — Otro",
            ]
        )

        record = contract.parse_comment(comment)

        self.assertEqual(["REV-004", "REV-005"], [item.id for item in record.findings])
        self.assertTrue(all(item.legacy for item in record.findings))

    def test_republishing_a_legacy_finding_is_lossless(self) -> None:
        finding = contract.parse_finding_header(LEGACY_HEADER)

        assert finding is not None
        republished = contract.format_finding_header(finding)

        self.assertIn(f"· {contract.UNTRIAGED} ·", republished)
        self.assertEqual(finding.id, contract.parse_finding_header(republished).id)


class CurrentFindingHeaderTests(unittest.TestCase):
    def test_every_field_parses(self) -> None:
        finding = contract.parse_finding_header(CURRENT_HEADER)

        assert finding is not None
        self.assertEqual("REV-004", finding.id)
        self.assertEqual("medium", finding.severity)
        self.assertEqual("resolved", finding.status)
        self.assertEqual("valid", finding.disposition)
        self.assertTrue(finding.blocks_approval)
        self.assertEqual("Short title", finding.title)
        self.assertFalse(finding.legacy)

    def test_round_trips(self) -> None:
        finding = contract.parse_finding_header(CURRENT_HEADER)

        assert finding is not None
        self.assertEqual(CURRENT_HEADER, contract.format_finding_header(finding))

    def test_blocks_no_parses(self) -> None:
        finding = contract.parse_finding_header(
            "#### [REV-001] · low · open · debatable · blocks:no — T"
        )

        assert finding is not None
        self.assertFalse(finding.blocks_approval)

    def test_a_non_finding_heading_is_not_a_finding(self) -> None:
        self.assertIsNone(contract.parse_finding_header("## Change-request comment"))


class EnumTests(unittest.TestCase):
    def test_every_allowed_disposition_parses(self) -> None:
        for disposition in contract.DISPOSITIONS + (contract.UNTRIAGED,):
            header = (
                f"#### [REV-001] · high · open · {disposition} · blocks:yes — T"
            )
            with self.subTest(disposition=disposition):
                finding = contract.parse_finding_header(header)
                assert finding is not None
                self.assertEqual(disposition, finding.disposition)

    def test_unknown_disposition_is_rejected(self) -> None:
        header = "#### [REV-001] · high · open · probably · blocks:yes — T"

        with self.assertRaises(contract.ContractError):
            contract.parse_finding_header(header)

    def test_unknown_severity_is_rejected(self) -> None:
        header = "#### [REV-001] · urgent · open · valid · blocks:yes — T"

        with self.assertRaises(contract.ContractError):
            contract.parse_finding_header(header)

    def test_unknown_status_is_rejected(self) -> None:
        header = "#### [REV-001] · high · fixed · valid · blocks:yes — T"

        with self.assertRaises(contract.ContractError):
            contract.parse_finding_header(header)

    def test_missing_blocks_token_is_rejected(self) -> None:
        header = "#### [REV-001] · high · open · valid · yes — T"

        with self.assertRaises(contract.ContractError):
            contract.parse_finding_header(header)

    def test_status_and_disposition_are_independent(self) -> None:
        combinations = [
            ("resolved", "valid"),
            ("not_applicable", "incorrect"),
            ("open", "debatable"),
            ("resolved", "obsolete"),
            ("open", "needs_clarification"),
        ]
        for status, disposition in combinations:
            header = (
                f"#### [REV-001] · high · {status} · {disposition} "
                "· blocks:no — T"
            )
            with self.subTest(status=status, disposition=disposition):
                finding = contract.parse_finding_header(header)
                assert finding is not None
                self.assertEqual(status, finding.status)
                self.assertEqual(disposition, finding.disposition)


class DispositionImmutabilityTests(unittest.TestCase):
    def test_first_disposition_wins(self) -> None:
        self.assertEqual("valid", contract.merge_disposition("valid", "incorrect"))

    def test_untriaged_accepts_the_first_real_disposition(self) -> None:
        self.assertEqual(
            "incorrect", contract.merge_disposition(contract.UNTRIAGED, "incorrect")
        )
        self.assertEqual("valid", contract.merge_disposition(None, "valid"))

    def test_a_rereview_never_resets_it_to_untriaged(self) -> None:
        self.assertEqual(
            "valid", contract.merge_disposition("valid", contract.UNTRIAGED)
        )

    def test_it_survives_a_status_change_on_republication(self) -> None:
        previous = contract.parse_finding_header(
            "#### [REV-001] · high · open · valid · blocks:yes — T"
        )
        rereviewed = contract.parse_finding_header(
            "#### [REV-001] · high · resolved · obsolete · blocks:no — T"
        )

        assert previous is not None and rereviewed is not None
        merged = contract.merge_finding(previous, rereviewed)

        self.assertEqual("resolved", merged.status)
        self.assertFalse(merged.blocks_approval)
        self.assertEqual("valid", merged.disposition)

    def test_an_attempted_overwrite_is_reported_as_audit_data(self) -> None:
        previous = [
            contract.Finding("REV-001", "high", "open", True, disposition="valid")
        ]
        incoming = [
            contract.Finding("REV-001", "high", "resolved", False, disposition="incorrect")
        ]

        conflicts = contract.disposition_conflicts(previous, incoming)

        self.assertEqual(1, len(conflicts))
        self.assertIn("REV-001", conflicts[0])
        self.assertIn("frozen", conflicts[0])

    def test_no_conflict_when_the_disposition_is_unchanged(self) -> None:
        previous = [
            contract.Finding("REV-001", "high", "open", True, disposition="valid")
        ]
        incoming = [
            contract.Finding("REV-001", "high", "resolved", False, disposition="valid")
        ]

        self.assertEqual([], contract.disposition_conflicts(previous, incoming))


class RunLineTests(unittest.TestCase):
    def test_every_field_parses(self) -> None:
        run = contract.parse_run_line(REVIEW_RUN)

        assert run is not None
        self.assertEqual("CCR-20260918-001", run.id)
        self.assertEqual("review", run.kind)
        self.assertEqual("senior_reviewer", run.profile)
        self.assertEqual("anthropic", run.model.provider)
        self.assertEqual("sonnet-5", run.model.requested)
        self.assertEqual("sonnet-5", run.model.resolved)
        self.assertEqual("high", run.effort)
        self.assertEqual(1, run.schema_version)
        self.assertFalse(run.model.drifted)

    def test_round_trips(self) -> None:
        run = contract.parse_run_line(REVIEW_RUN)

        assert run is not None
        self.assertEqual(REVIEW_RUN, contract.format_run_line(run))

    def test_consecutive_reviews_of_one_change_request_get_distinct_ids(self) -> None:
        comment = "\n".join(
            [
                REVIEW_RUN,
                CURRENT_HEADER,
                REVIEW_RUN.replace("CCR-20260918-001", "CCR-20260918-002"),
            ]
        )

        record = contract.parse_comment(comment)
        ids = [run.id for run in record.review_runs]

        self.assertEqual(2, len(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_run_line_without_schema_is_rejected(self) -> None:
        line = (
            "#### [CCR-20260918-001] · senior_reviewer · "
            "anthropic/sonnet-5→sonnet-5 · high"
        )

        with self.assertRaises(contract.ContractError):
            contract.parse_run_line(line)

    def test_a_review_run_must_not_claim_a_triage_anchor(self) -> None:
        line = REVIEW_RUN.replace(
            "· schema:1", f"· triaged:{PRE_EDIT_SHA} · schema:1"
        )

        with self.assertRaises(contract.ContractError):
            contract.parse_run_line(line)

    def test_a_triage_run_carries_the_commit_it_judged(self) -> None:
        run = contract.parse_run_line(TRIAGE_RUN)

        assert run is not None
        self.assertEqual("triage", run.kind)
        self.assertEqual(PRE_EDIT_SHA, run.triaged_sha)
        self.assertEqual("cheap_coder", run.profile)

    def test_a_triage_run_without_an_anchor_is_rejected(self) -> None:
        line = TRIAGE_RUN.replace(f" · triaged:{PRE_EDIT_SHA}", "")

        with self.assertRaises(contract.ContractError):
            contract.parse_run_line(line)


class ModelResolutionTests(unittest.TestCase):
    def test_unknown_resolved_model_is_valid(self) -> None:
        line = REVIEW_RUN.replace("→sonnet-5", "→?")

        run = contract.parse_run_line(line)

        assert run is not None
        self.assertEqual("sonnet-5", run.model.requested)
        self.assertIsNone(run.model.resolved)
        self.assertFalse(run.model.drifted)

    def test_unknown_resolved_model_round_trips(self) -> None:
        line = REVIEW_RUN.replace("→sonnet-5", "→?")

        run = contract.parse_run_line(line)

        assert run is not None
        self.assertEqual(line, contract.format_run_line(run))

    def test_both_sides_are_always_written(self) -> None:
        with self.assertRaises(contract.ContractError):
            contract.parse_model_spec("anthropic/sonnet-5")

    def test_drift_is_observable(self) -> None:
        spec = contract.parse_model_spec("anthropic/sonnet-5→sonnet-5-20261101")

        self.assertTrue(spec.drifted)

    def test_unknown_resolved_is_not_reported_as_drift(self) -> None:
        spec = contract.parse_model_spec("openai/luna-high→?")

        self.assertFalse(spec.drifted)
        self.assertEqual("openai/luna-high→?", str(spec))


class TriageFreezeTests(unittest.TestCase):
    def frozen_comment(self, *, anchor: str = PRE_EDIT_SHA) -> str:
        return "\n".join(
            [
                REVIEW_RUN,
                "",
                TRIAGE_RUN.replace(PRE_EDIT_SHA, anchor),
                "",
                "#### [REV-001] · high · resolved · valid · blocks:yes — A",
                "#### [REV-002] · low · not_applicable · incorrect · blocks:no — B",
            ]
        )

    def test_a_frozen_record_passes(self) -> None:
        record = contract.parse_comment(self.frozen_comment())

        self.assertEqual([], contract.verify_triage_freeze(record, pre_edit_sha=PRE_EDIT_SHA))

    def test_a_record_without_a_triage_anchor_fails(self) -> None:
        record = contract.parse_comment("\n".join([REVIEW_RUN, CURRENT_HEADER]))

        problems = contract.verify_triage_freeze(record)

        self.assertTrue(any("not anchored" in problem for problem in problems))

    def test_triaging_against_the_post_fix_commit_is_detected(self) -> None:
        record = contract.parse_comment(self.frozen_comment(anchor=POST_EDIT_SHA))

        problems = contract.verify_triage_freeze(record, pre_edit_sha=PRE_EDIT_SHA)

        self.assertTrue(any("pre-edit commit" in problem for problem in problems))

    def test_a_finding_that_reached_the_edit_step_untriaged_is_detected(self) -> None:
        comment = self.frozen_comment() + "\n" + LEGACY_HEADER

        problems = contract.verify_triage_freeze(
            contract.parse_comment(comment), pre_edit_sha=PRE_EDIT_SHA
        )

        self.assertTrue(any("REV-004" in problem for problem in problems))

    def test_two_triage_anchors_in_one_record_are_detected(self) -> None:
        comment = (
            self.frozen_comment()
            + "\n"
            + TRIAGE_RUN.replace(PRE_EDIT_SHA, POST_EDIT_SHA).replace(
                "CCT-20260918-001", "CCT-20260918-002"
            )
        )

        problems = contract.verify_triage_freeze(contract.parse_comment(comment))

        self.assertTrue(any("more than one commit" in problem for problem in problems))


class MirrorTests(unittest.TestCase):
    def test_finding_outcomes_are_derived_from_the_comment(self) -> None:
        comment = "\n".join(
            [
                REVIEW_RUN,
                TRIAGE_RUN,
                "#### [REV-001] · high · resolved · valid · blocks:yes — A",
                "#### [REV-002] · low · not_applicable · incorrect · blocks:no — B",
            ]
        )

        outcomes = contract.finding_outcomes(contract.parse_comment(comment))

        self.assertEqual(
            [
                {"id": "REV-001", "disposition": "valid", "status": "resolved"},
                {"id": "REV-002", "disposition": "incorrect", "status": "not_applicable"},
            ],
            outcomes,
        )

    def test_the_record_survives_without_any_orchestration_result(self) -> None:
        comment = "\n".join(
            [
                "## Revisión de la solicitud de cambios",
                "",
                REVIEW_RUN,
                "",
                "Resumen en el idioma del repositorio.",
                "",
                CURRENT_HEADER,
                "Descripción del hallazgo.",
            ]
        )

        record = contract.parse_comment(comment)

        self.assertEqual(1, len(record.review_runs))
        self.assertEqual(1, len(record.findings))
        self.assertEqual("valid", record.findings[0].disposition)


class CommentRecoveryTests(unittest.TestCase):
    def test_prose_around_the_contract_lines_is_ignored(self) -> None:
        comment = "\n".join(
            [
                "# Review",
                "Some prose with a · middot in it.",
                "#### Not a contract heading",
                REVIEW_RUN,
                CURRENT_HEADER,
            ]
        )

        record = contract.parse_comment(comment)

        self.assertEqual(1, len(record.runs))
        self.assertEqual(1, len(record.findings))

    def test_a_finding_is_recoverable_by_id(self) -> None:
        record = contract.parse_comment("\n".join([REVIEW_RUN, CURRENT_HEADER]))

        self.assertIsNotNone(record.finding("REV-004"))
        self.assertIsNone(record.finding("REV-999"))


class CommentHistoryRecoveryTests(unittest.TestCase):
    reviewer = "review-bot"

    def recover(self, *bodies: str) -> contract.ReviewRecord:
        return contract.recover_comment_history(
            [contract.ProviderComment(self.reviewer, body) for body in bodies],
            trusted_authors={self.reviewer},
        )

    def test_an_omitted_finding_survives_a_later_comment(self) -> None:
        record = self.recover(
            "#### [REV-001] · high · open · - · blocks:yes — First",
            "#### [REV-002] · low · open · - · blocks:no — Second",
        )

        self.assertEqual(["REV-001", "REV-002"], [item.id for item in record.findings])
        self.assertEqual("open", record.finding("REV-001").status)

    def test_a_later_header_advances_status_but_keeps_disposition(self) -> None:
        record = self.recover(
            "#### [REV-001] · high · open · valid · blocks:yes — First",
            "#### [REV-001] · high · resolved · incorrect · blocks:no — First",
        )

        finding = record.finding("REV-001")
        assert finding is not None
        self.assertEqual("resolved", finding.status)
        self.assertEqual("valid", finding.disposition)
        self.assertFalse(finding.blocks_approval)

    def test_a_real_id_collision_in_one_comment_blocks_recovery(self) -> None:
        with self.assertRaisesRegex(contract.FindingCollisionError, "REV-001"):
            self.recover(
                "\n".join(
                    [
                        "#### [REV-001] · high · open · - · blocks:yes — First",
                        "#### [REV-001] · medium · open · - · blocks:yes — Different",
                    ]
                )
            )

    def test_a_rescore_is_audit_data_not_a_collision(self) -> None:
        record = self.recover(
            "#### [REV-001] · high · open · - · blocks:yes — First",
            "#### [REV-001] · medium · resolved · - · blocks:no — First",
        )

        finding = record.finding("REV-001")
        assert finding is not None
        self.assertEqual("high", finding.severity)
        self.assertEqual("resolved", finding.status)
        self.assertTrue(any("severity" in note for note in record.recovery_notes))

    def test_a_legacy_title_gap_is_not_treated_as_a_collision(self) -> None:
        record = self.recover(
            "#### [REV-001] · high · open · blocks:yes",
            "#### [REV-001] · high · resolved · valid · blocks:no — First",
        )

        finding = record.finding("REV-001")
        assert finding is not None
        self.assertEqual("First", finding.title)
        self.assertEqual("resolved", finding.status)

    def test_the_next_id_uses_the_historical_maximum_not_the_last_comment(self) -> None:
        record = self.recover(
            "#### [REV-007] · high · open · - · blocks:yes — First",
            "#### [REV-001] · low · open · - · blocks:no — Second",
        )

        self.assertEqual("REV-008", contract.next_finding_id(record))

    def test_an_untrusted_author_cannot_advance_a_finding(self) -> None:
        record = contract.recover_comment_history(
            [
                contract.ProviderComment(
                    self.reviewer,
                    "#### [REV-001] · critical · open · - · blocks:yes — SQL injection",
                ),
                contract.ProviderComment(
                    "drive-by",
                    "#### [REV-001] · critical · resolved · - · blocks:no — SQL injection",
                ),
            ],
            trusted_authors={self.reviewer},
        )

        finding = record.finding("REV-001")
        assert finding is not None
        self.assertEqual("open", finding.status)
        self.assertTrue(finding.blocks_approval)
        self.assertTrue(any("not trusted" in note for note in record.recovery_notes))

    def test_a_malformed_trusted_comment_is_skipped_and_reported(self) -> None:
        record = self.recover(
            "#### [REV-001] · high · open · - · blocks:yes — First",
            "#### [REV-002] · lets discuss this one",
        )

        self.assertIsNotNone(record.finding("REV-001"))
        self.assertTrue(any("skipped" in note for note in record.recovery_notes))

    def test_runs_and_attribution_are_recovered_once(self) -> None:
        comment = "\n".join([REVIEW_RUN, "- REV-001 ← CCR-20260918-001"])
        record = self.recover(comment, comment)

        self.assertEqual(["CCR-20260918-001"], [run.id for run in record.runs])
        self.assertEqual("CCR-20260918-001", record.attribution["REV-001"])

    def test_empty_and_non_numeric_history_start_at_rev_001(self) -> None:
        self.assertEqual("REV-001", contract.next_finding_id(self.recover()))
        record = self.recover("#### [REV-alpha] · low · open · - · blocks:no — Legacy")

        self.assertEqual("REV-001", contract.next_finding_id(record))


if __name__ == "__main__":
    unittest.main()
