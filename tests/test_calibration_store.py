from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import calibration_store as store  # noqa: E402
import review_contract as contract  # noqa: E402


def run_line(run_id: str, profile: str, provider: str, model: str) -> contract.RunLine:
    return contract.RunLine(
        id=run_id,
        profile=profile,
        model=contract.ModelSpec(provider=provider, requested=model, resolved=model),
        effort="high",
    )


RUN_A = run_line("CCR-20260918-001", "reviewer_a", "anthropic", "sonnet-5")
RUN_B = run_line("CCR-20260918-002", "reviewer_b", "openai", "terra-high")


class CampaignStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store_dir = Path(temporary.name)

    def campaign(self, campaign_id: str = "CAL-2026-Q3") -> store.Campaign:
        return store.Campaign(campaign_id, store_dir=self.store_dir)

    def seeded(self) -> store.Campaign:
        campaign = self.campaign()
        campaign.record_run(RUN_A, arm="reviewer_a")
        campaign.record_run(RUN_B, arm="reviewer_b")
        campaign.record_candidate("CAL-001", RUN_A.id)
        campaign.record_candidate("CAL-002", RUN_B.id)
        return campaign

    def test_it_lives_outside_the_repository(self) -> None:
        campaign = self.seeded()

        self.assertFalse(str(campaign.path).startswith(str(ROOT)))
        self.assertTrue(campaign.path.is_file())

    def test_attribution_survives_an_interrupted_campaign(self) -> None:
        self.seeded()

        resumed = self.campaign()

        self.assertEqual("reviewer_a", resumed.resolve("CAL-001").arm)
        self.assertEqual(RUN_B.id, resumed.resolve("CAL-002").review_run_id)

    def test_a_partial_change_request_can_be_continued(self) -> None:
        campaign = self.seeded()
        campaign.bind_published_id("CAL-001", "REV-001")

        resumed = self.campaign()
        resumed.bind_published_id("CAL-002", "REV-002")

        self.assertEqual({"REV-001": "CAL-001", "REV-002": "CAL-002"}, resumed.published())

    def test_attribution_is_never_silently_rebound(self) -> None:
        campaign = self.seeded()

        with self.assertRaises(store.CalibrationError):
            campaign.record_candidate("CAL-001", RUN_B.id)

        campaign.bind_published_id("CAL-001", "REV-001")
        with self.assertRaises(store.CalibrationError):
            campaign.bind_published_id("CAL-002", "REV-001")

    def test_rebinding_the_same_value_is_not_an_error(self) -> None:
        campaign = self.seeded()

        campaign.record_candidate("CAL-001", RUN_A.id)

        self.assertEqual(RUN_A.id, campaign.resolve("CAL-001").review_run_id)

    def test_an_unknown_run_cannot_receive_candidates(self) -> None:
        campaign = self.campaign()

        with self.assertRaises(store.CalibrationError):
            campaign.record_candidate("CAL-001", "CCR-does-not-exist")

    def test_only_review_runs_are_attributable(self) -> None:
        campaign = self.campaign()
        triage = contract.RunLine(
            id="CCT-20260918-001",
            profile="cheap_coder",
            model=contract.ModelSpec("openai", "luna-high", "luna-high"),
            effort="high",
            triaged_sha="0123456789abcdef0123456789abcdef01234567",
        )

        with self.assertRaises(store.CalibrationError):
            campaign.record_run(triage, arm="resolver")

    def test_opaque_candidate_ids_are_enforced(self) -> None:
        campaign = self.seeded()

        with self.assertRaises(store.CalibrationError):
            campaign.resolve("REV-001")

    def test_an_unsafe_campaign_id_is_rejected(self) -> None:
        for unsafe in ("../escape", "with/slash", "", "a" * 200):
            with self.subTest(campaign_id=unsafe):
                with self.assertRaises(store.CalibrationError):
                    self.campaign(unsafe)

    def test_it_stores_identifiers_only(self) -> None:
        campaign = self.seeded()
        campaign.bind_published_id("CAL-001", "REV-001")

        raw = campaign.path.read_text(encoding="utf-8")

        self.assertIn("CCR-20260918-001", raw)
        self.assertNotIn("diff", raw)
        self.assertNotIn("token", raw)


class PairIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.campaign = store.Campaign("CAL-2026-Q3", store_dir=Path(temporary.name))
        self.campaign.record_run(RUN_A, arm="reviewer_a")
        self.campaign.record_run(RUN_B, arm="reviewer_b")
        self.head = "0123456789abcdef0123456789abcdef01234567"
        self.moved = "89abcdef0123456789abcdef0123456789abcdef"

    def record(self, isolation: str, observed: dict[str, str] | None = None) -> bool:
        if observed is None:
            observed = {RUN_A.id: self.head, RUN_B.id: self.head}
        return self.campaign.record_pair(
            "123", isolation=isolation, head_sha=self.head, observed_head_shas=observed
        )

    def test_separate_worktrees_are_usable(self) -> None:
        self.assertTrue(self.record("isolated"))
        self.assertEqual(["123"], self.campaign.usable_pairs())

    def test_sequential_dispatch_is_usable(self) -> None:
        self.assertTrue(self.record("sequential"))

    def test_a_shared_concurrent_pair_is_recorded_but_excluded(self) -> None:
        self.assertFalse(self.record("shared_concurrent"))

        self.assertEqual([], self.campaign.usable_pairs())
        self.assertIn("123", self.campaign.pairs())

    def test_a_reviewer_that_moved_the_head_voids_the_pair(self) -> None:
        usable = self.record(
            "isolated", observed={RUN_A.id: self.head, RUN_B.id: self.moved}
        )

        self.assertFalse(usable)
        self.assertEqual([RUN_B.id], self.campaign.pairs()["123"]["head_drifted_for"])

    def test_an_unknown_isolation_mode_is_rejected(self) -> None:
        with self.assertRaises(store.CalibrationError):
            self.record("best_effort")

    def test_a_dispatch_that_never_finished_is_excluded_not_invisible(self) -> None:
        self.campaign.open_pair("123", head_sha=self.head)

        self.assertEqual([], self.campaign.usable_pairs())
        self.assertIn("123", self.campaign.excluded_pairs())
        self.assertIn("did not complete", self.campaign.excluded_pairs()["123"])

    def test_opening_then_completing_a_pair_makes_it_usable(self) -> None:
        self.campaign.open_pair("123", head_sha=self.head)

        self.assertTrue(self.record("isolated"))
        self.assertEqual(["123"], self.campaign.usable_pairs())
        self.assertEqual({}, self.campaign.excluded_pairs())

    def test_reopening_never_discards_a_recorded_verdict(self) -> None:
        self.record("isolated")

        self.campaign.open_pair("123", head_sha=self.head)

        self.assertEqual(["123"], self.campaign.usable_pairs())

    def test_every_exclusion_states_its_reason(self) -> None:
        self.record("shared_concurrent", observed={RUN_A.id: self.moved})

        reason = self.campaign.excluded_pairs()["123"]

        self.assertIn("not isolated", reason)
        self.assertIn("head moved", reason)

    def test_the_verdict_survives_a_resume(self) -> None:
        self.record("shared_concurrent")

        resumed = store.Campaign("CAL-2026-Q3", store_dir=self.campaign.store_dir)

        self.assertEqual([], resumed.usable_pairs())
        self.assertEqual("shared_concurrent", resumed.pairs()["123"]["isolation"])


class AttributionBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.campaign = store.Campaign("CAL-2026-Q3", store_dir=Path(temporary.name))
        self.campaign.record_run(RUN_A, arm="reviewer_a")
        self.campaign.record_run(RUN_B, arm="reviewer_b")
        self.campaign.record_candidate("CAL-001", RUN_A.id)
        self.campaign.record_candidate("CAL-002", RUN_B.id)

    def test_it_cannot_be_published_before_the_human_matching(self) -> None:
        with self.assertRaises(store.CalibrationError):
            self.campaign.attribution_block()

    def test_it_carries_both_run_lines_and_the_finding_map(self) -> None:
        self.campaign.bind_published_id("CAL-001", "REV-001")
        self.campaign.bind_published_id("CAL-002", "REV-002")

        block = self.campaign.attribution_block()

        self.assertIn("[CAL-2026-Q3]", block)
        self.assertIn("attribution", block)
        self.assertIn(contract.format_run_line(RUN_A, level=5), block)
        self.assertIn(contract.format_run_line(RUN_B, level=5), block)
        self.assertIn("- REV-001 ← CCR-20260918-001", block)
        self.assertIn("- REV-002 ← CCR-20260918-002", block)

    def test_the_published_block_parses_back_to_the_finding_map(self) -> None:
        self.campaign.bind_published_id("CAL-001", "REV-001")
        self.campaign.bind_published_id("CAL-002", "REV-002")

        mapping = contract.parse_attribution_block(self.campaign.attribution_block())

        self.assertEqual(
            {"REV-001": "CCR-20260918-001", "REV-002": "CCR-20260918-002"}, mapping
        )

    def test_a_paired_comment_exposes_two_review_runs(self) -> None:
        self.campaign.bind_published_id("CAL-001", "REV-001")
        self.campaign.bind_published_id("CAL-002", "REV-002")
        comment = "\n".join(
            [
                "## Revisión pareada",
                "#### [REV-001] · high · resolved · valid · blocks:yes — A",
                "#### [REV-002] · low · open · incorrect · blocks:no — B",
                "",
                self.campaign.attribution_block(),
            ]
        )

        record = contract.parse_comment(comment)

        self.assertEqual(2, len(record.review_runs))
        self.assertEqual("CCR-20260918-001", record.attribution["REV-001"])
        self.assertEqual("CCR-20260918-002", record.attribution["REV-002"])


if __name__ == "__main__":
    unittest.main()
