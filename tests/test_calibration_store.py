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


class PresentationIdTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.campaign = store.Campaign("CAL-2026-Q3", store_dir=Path(temporary.name))
        self.campaign.record_run(RUN_A, arm="reviewer_a")
        self.campaign.record_run(RUN_B, arm="reviewer_b")
        # Four from one arm and one from the other: the shape that leaked before.
        self.a = ["CAL-A001", "CAL-A002", "CAL-A003", "CAL-A004"]
        self.b = ["CAL-B001"]
        for c in self.a:
            self.campaign.record_candidate(c, RUN_A.id)
        for c in self.b:
            self.campaign.record_candidate(c, RUN_B.id)

    def test_ids_carry_no_arm_and_no_ordering(self) -> None:
        rows = self.campaign.mint_presentation_ids(self.a + self.b)

        ids = [r["display_id"] for r in rows]
        self.assertEqual(5, len(ids))
        self.assertEqual(len(ids), len(set(ids)))
        for display in ids:
            self.assertRegex(display, r"^F-[A-Z2-9]{4}$")
        # nothing in the row itself says which reviewer produced it
        self.assertEqual({"display_id", "candidate_id"}, set(rows[0]))

    def test_minting_is_stable_across_calls(self) -> None:
        first = {r["candidate_id"]: r["display_id"] for r in
                 self.campaign.mint_presentation_ids(self.a + self.b)}
        second = {r["candidate_id"]: r["display_id"] for r in
                  self.campaign.mint_presentation_ids(self.a + self.b)}

        self.assertEqual(first, second)

    def test_the_map_survives_a_resume(self) -> None:
        minted = {r["candidate_id"]: r["display_id"] for r in
                  self.campaign.mint_presentation_ids(self.a + self.b)}

        resumed = store.Campaign("CAL-2026-Q3", store_dir=self.campaign.store_dir)
        revealed = resumed.reveal_presentation()

        self.assertEqual(5, len(revealed))
        for candidate, display in minted.items():
            self.assertEqual(candidate, revealed[display]["candidate_id"])

    def test_reveal_maps_back_to_the_producing_arm(self) -> None:
        self.campaign.mint_presentation_ids(self.a + self.b)

        revealed = self.campaign.reveal_presentation()
        arms = sorted(v["arm"] for v in revealed.values())

        self.assertEqual(["reviewer_a"] * 4 + ["reviewer_b"], arms)

    def test_an_unknown_candidate_cannot_be_presented(self) -> None:
        with self.assertRaises(store.CalibrationError):
            self.campaign.mint_presentation_ids(["CAL-NOPE"])


class SampleCapabilityTests(unittest.TestCase):
    """A clean pair is not automatically a pair every metric can use."""

    HEAD = "0123456789abcdef0123456789abcdef01234567"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.campaign = store.Campaign("CAL-2026-Q3", store_dir=Path(temporary.name))
        self.campaign.record_run(RUN_A, arm="reviewer_a")
        self.campaign.record_run(RUN_B, arm="reviewer_b")
        self.campaign.record_candidate("CAL-A001", RUN_A.id)
        self.campaign.record_candidate("CAL-B001", RUN_B.id)
        self.key = self.campaign.open_pair(
            "4", head_sha=self.HEAD, target_relation="self_toolkit"
        )
        self.campaign.bind_published_id("CAL-A001", "REV-001")
        self.campaign.bind_published_id("CAL-B001", "REV-002")
        self.campaign.record_pair(
            "4",
            isolation="isolated",
            head_sha=self.HEAD,
            observed_head_shas={RUN_A.id: self.HEAD, RUN_B.id: self.HEAD},
            expected_review_run_ids=[RUN_A.id, RUN_B.id],
        )

    def triage_run(self, sha: str | None = None) -> contract.RunLine:
        return contract.RunLine(
            id="CCT-20260918-001",
            profile="cheap_coder",
            model=contract.ModelSpec("openai", "gpt-5.6-luna", "gpt-5.6-luna"),
            effort="high",
            triaged_sha=sha or self.HEAD,
        )

    def test_an_untriaged_pair_supports_coverage_but_not_acceptance(self) -> None:
        caps = self.campaign.capabilities(self.key)

        self.assertIsNone(caps["coverage"])
        self.assertIsNone(caps["overlap"])
        self.assertIn("no resolver triaged", caps["acceptance"])
        self.assertEqual([self.key], self.campaign.pairs_supporting("coverage"))
        self.assertEqual([], self.campaign.pairs_supporting("acceptance"))

    def test_a_fully_triaged_pair_supports_acceptance(self) -> None:
        self.campaign.record_triage(
            "4",
            head_sha=self.HEAD,
            run=self.triage_run(),
            dispositions={"REV-001": "valid", "REV-002": "incorrect"},
        )

        self.assertIsNone(self.campaign.capabilities(self.key)["acceptance"])
        self.assertEqual([self.key], self.campaign.pairs_supporting("acceptance"))

    def test_a_partially_triaged_pair_does_not_support_acceptance(self) -> None:
        self.campaign.record_triage(
            "4",
            head_sha=self.HEAD,
            run=self.triage_run(),
            dispositions={"REV-001": "valid"},
        )

        reason = self.campaign.capabilities(self.key)["acceptance"]
        self.assertIn("never triaged", reason)
        self.assertIn("REV-002", reason)

    def test_a_finding_left_as_untriaged_does_not_count(self) -> None:
        self.campaign.record_triage(
            "4",
            head_sha=self.HEAD,
            run=self.triage_run(),
            dispositions={"REV-001": "valid", "REV-002": "-"},
        )

        self.assertIn("left untriaged", self.campaign.capabilities(self.key)["acceptance"])

    def test_the_resolver_identity_is_recorded_beside_the_reviewers(self) -> None:
        self.campaign.record_triage(
            "4",
            head_sha=self.HEAD,
            run=self.triage_run(),
            dispositions={"REV-001": "valid", "REV-002": "incorrect"},
        )

        triage = self.campaign.pairs()[self.key]["triage"]
        self.assertEqual("cheap_coder", triage["profile"])
        self.assertEqual("gpt-5.6-luna", triage["model_resolved"])
        self.assertEqual(self.HEAD, triage["triaged_sha"])

    def test_triage_anchored_to_another_commit_is_refused(self) -> None:
        other = "89abcdef0123456789abcdef0123456789abcdef"

        with self.assertRaises(store.CalibrationError):
            self.campaign.record_triage(
                "4", head_sha=self.HEAD, run=self.triage_run(other),
                dispositions={"REV-001": "valid"},
            )

    def test_a_review_run_cannot_pose_as_the_resolver(self) -> None:
        with self.assertRaises(store.CalibrationError):
            self.campaign.record_triage(
                "4", head_sha=self.HEAD, run=RUN_A, dispositions={"REV-001": "valid"}
            )

    def test_dispositions_for_unpublished_findings_are_refused(self) -> None:
        with self.assertRaises(store.CalibrationError):
            self.campaign.record_triage(
                "4", head_sha=self.HEAD, run=self.triage_run(),
                dispositions={"REV-999": "valid"},
            )

    def test_an_unusable_pair_supports_nothing(self) -> None:
        other = "89abcdef0123456789abcdef0123456789abcdef"
        key = self.campaign.open_pair("5", head_sha=other)
        self.campaign.record_pair(
            "5", isolation="shared_concurrent", head_sha=other,
            observed_head_shas={RUN_A.id: other, RUN_B.id: other},
            expected_review_run_ids=[RUN_A.id, RUN_B.id],
        )

        caps = self.campaign.capabilities(key)

        self.assertTrue(all(v is not None for v in caps.values()))

    def test_the_target_relation_is_recorded(self) -> None:
        self.assertEqual("self_toolkit", self.campaign.pairs()[self.key]["target_relation"])

    def test_an_unknown_target_relation_is_rejected(self) -> None:
        with self.assertRaises(store.CalibrationError):
            self.campaign.open_pair("9", head_sha=self.HEAD, target_relation="whatever")


class PairIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.campaign = store.Campaign("CAL-2026-Q3", store_dir=Path(temporary.name))
        self.campaign.record_run(RUN_A, arm="reviewer_a")
        self.campaign.record_run(RUN_B, arm="reviewer_b")
        self.head = "0123456789abcdef0123456789abcdef01234567"
        self.moved = "89abcdef0123456789abcdef0123456789abcdef"
        self.key = store.Campaign.pair_key("123", self.head)

    def record(
        self,
        isolation: str,
        observed: dict[str, str] | None = None,
        expected: list[str] | None = None,
    ) -> bool:
        if observed is None:
            observed = {RUN_A.id: self.head, RUN_B.id: self.head}
        if expected is None:
            expected = [RUN_A.id, RUN_B.id]
        return self.campaign.record_pair(
            "123",
            isolation=isolation,
            head_sha=self.head,
            observed_head_shas=observed,
            expected_review_run_ids=expected,
        )

    def test_separate_worktrees_are_usable(self) -> None:
        self.assertTrue(self.record("isolated"))
        self.assertEqual([self.key], self.campaign.usable_pairs())

    def test_sequential_dispatch_is_usable(self) -> None:
        self.assertTrue(self.record("sequential"))

    def test_a_shared_concurrent_pair_is_recorded_but_excluded(self) -> None:
        self.assertFalse(self.record("shared_concurrent"))

        self.assertEqual([], self.campaign.usable_pairs())
        self.assertIn(self.key, self.campaign.pairs())

    def test_a_reviewer_that_moved_the_head_voids_the_pair(self) -> None:
        usable = self.record(
            "isolated", observed={RUN_A.id: self.head, RUN_B.id: self.moved}
        )

        self.assertFalse(usable)
        self.assertEqual([RUN_B.id], self.campaign.pairs()[self.key]["head_drifted_for"])

    def test_a_one_sided_review_is_not_a_pair(self) -> None:
        """Both reviewers found this independently on the first real campaign."""
        usable = self.record("isolated", observed={RUN_A.id: self.head})

        self.assertFalse(usable)
        self.assertEqual([], self.campaign.usable_pairs())
        self.assertIn("no head SHA was observed after", self.campaign.excluded_pairs()[self.key])
        self.assertIn(RUN_B.id, self.campaign.excluded_pairs()[self.key])

    def test_an_unattributable_observation_does_not_stand_in_for_a_reviewer(self) -> None:
        """Found while verifying the reported defect; no reviewer reported this one.

        One entry in the mapping used to be enough, even when it named a run that
        was never dispatched.
        """
        usable = self.record("isolated", observed={"CCR-NEVER-DISPATCHED": self.head})

        self.assertFalse(usable)
        reason = self.campaign.excluded_pairs()[self.key]
        self.assertIn("did not expect", reason)
        self.assertIn("no head SHA was observed after", reason)

    def test_an_expected_run_that_was_never_registered_is_refused(self) -> None:
        usable = self.record(
            "isolated",
            observed={RUN_A.id: self.head, "CCR-GHOST": self.head},
            expected=[RUN_A.id, "CCR-GHOST"],
        )

        self.assertFalse(usable)
        self.assertIn("not registered", self.campaign.excluded_pairs()[self.key])

    def test_a_pair_must_expect_at_least_one_run(self) -> None:
        with self.assertRaises(store.CalibrationError):
            self.record("isolated", expected=[])

    def test_a_second_campaign_on_the_same_change_request_is_its_own_pair(self) -> None:
        """Keying by change request alone would hide the second attempt.

        A later campaign at a new commit that crashes before `record_pair` must
        leave its own excluded row, not sit silently behind the usable row of
        the previous commit.
        """
        self.assertTrue(self.record("isolated"))

        second = store.Campaign.pair_key("123", self.moved)
        self.campaign.open_pair("123", head_sha=self.moved)

        self.assertNotEqual(self.key, second)
        self.assertEqual([self.key], self.campaign.usable_pairs())
        self.assertIn(second, self.campaign.excluded_pairs())
        self.assertIn("did not complete", self.campaign.excluded_pairs()[second])

    def test_an_unknown_isolation_mode_is_rejected(self) -> None:
        with self.assertRaises(store.CalibrationError):
            self.record("best_effort")

    def test_a_dispatch_that_never_finished_is_excluded_not_invisible(self) -> None:
        self.campaign.open_pair("123", head_sha=self.head)

        self.assertEqual([], self.campaign.usable_pairs())
        self.assertIn(self.key, self.campaign.excluded_pairs())
        self.assertIn("did not complete", self.campaign.excluded_pairs()[self.key])

    def test_opening_then_completing_a_pair_makes_it_usable(self) -> None:
        self.campaign.open_pair("123", head_sha=self.head)

        self.assertTrue(self.record("isolated"))
        self.assertEqual([self.key], self.campaign.usable_pairs())
        self.assertEqual({}, self.campaign.excluded_pairs())

    def test_reopening_never_discards_a_recorded_verdict(self) -> None:
        self.record("isolated")

        self.campaign.open_pair("123", head_sha=self.head)

        self.assertEqual([self.key], self.campaign.usable_pairs())

    def test_every_exclusion_states_its_reason(self) -> None:
        self.record("shared_concurrent", observed={RUN_A.id: self.moved})

        reason = self.campaign.excluded_pairs()[self.key]

        self.assertIn("not isolated", reason)
        self.assertIn("head moved", reason)

    def test_the_verdict_survives_a_resume(self) -> None:
        self.record("shared_concurrent")

        resumed = store.Campaign("CAL-2026-Q3", store_dir=self.campaign.store_dir)

        self.assertEqual([], resumed.usable_pairs())
        self.assertEqual("shared_concurrent", resumed.pairs()[self.key]["isolation"])


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
