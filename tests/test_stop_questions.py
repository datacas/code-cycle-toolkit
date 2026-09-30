"""The questions a stopped stage asks, one at a time.

A stop that needs decisions is only quick to answer if every question has a
bounded prompt, a few options with the recommendation first, and says what it
settles. These tests pin that shape and the asking order.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import stop_questions as sq  # noqa: E402


def question(**fields) -> dict:
    value = {
        "id": "Q-001",
        "prompt": "The criterion names no observable result. What should it check?",
        "options": ["The exit status", "The printed report"],
        "recommended": "The exit status",
        "blocks": ["IR-001"],
    }
    value.update(fields)
    return value


class ShapeTests(unittest.TestCase):
    def test_a_documented_question_is_valid(self) -> None:
        self.assertEqual([], sq.questions_errors([question()]))
        self.assertEqual([], sq.questions_errors([question(recommended=None)]))
        self.assertEqual([], sq.questions_errors([]))

    def test_options_are_two_to_four_distinct_texts(self) -> None:
        for options in (["only one"], ["a", "b", "c", "d", "e"], ["a", "a"],
                        ["a", ""], "a or b", ["a", 1]):
            with self.subTest(options=options):
                self.assertTrue(sq.questions_errors([question(
                    options=options, recommended=None)]))

    def test_the_recommendation_is_the_first_option(self) -> None:
        problems = sq.questions_errors([question(recommended="The printed report")])
        self.assertEqual(["`questions[0]`.recommended must be null or the first option"],
                         problems)
        self.assertTrue(sq.questions_errors([question(recommended="Something else")]))

    def test_every_key_is_required_and_nothing_else_is_accepted(self) -> None:
        missing = question()
        del missing["blocks"]
        self.assertTrue(sq.questions_errors([missing]))
        self.assertTrue(sq.questions_errors([question(context="extra")]))

    def test_identifiers_are_q_numbers_and_unique(self) -> None:
        self.assertTrue(sq.questions_errors([question(id="IR-001")]))
        self.assertTrue(sq.questions_errors([question(), question()]))

    def test_a_question_says_what_it_settles(self) -> None:
        for blocks in ([], ["IR-001", "IR-001"], "IR-001", [""]):
            with self.subTest(blocks=blocks):
                self.assertTrue(sq.questions_errors([question(blocks=blocks)]))
        self.assertEqual([], sq.questions_errors([question()], frozenset({"IR-001"})))
        self.assertEqual(
            ["`questions[0]`.blocks names what the result does not report"],
            sq.questions_errors([question()], frozenset({"IU-001"})))

    def test_a_prompt_is_bounded_text(self) -> None:
        for prompt in ("", "  ", "x" * (sq.MAX_PROMPT_LENGTH + 1), None):
            with self.subTest(prompt=prompt):
                self.assertTrue(sq.questions_errors([question(prompt=prompt)]))

    def test_a_list_that_is_not_a_bounded_list_is_refused(self) -> None:
        self.assertTrue(sq.questions_errors({"Q-001": question()}))
        self.assertTrue(sq.questions_errors(
            [question(id=f"Q-{n:03d}") for n in range(sq.MAX_QUESTIONS + 1)]))
        self.assertTrue(sq.questions_errors(["Q-001"]))


class OrderTests(unittest.TestCase):
    def test_the_question_that_settles_the_most_is_asked_first(self) -> None:
        ordered = sq.ask_order([
            question(id="Q-001", blocks=["IR-001"]),
            question(id="Q-002", blocks=["IR-002", "IU-001"]),
            question(id="Q-003", blocks=["IR-003"]),
        ])
        self.assertEqual(["Q-002", "Q-001", "Q-003"], [item["id"] for item in ordered])


if __name__ == "__main__":
    unittest.main()
