"""The decisions a stopped stage needs from a person, as questions to ask.

A stage that stops because someone has to decide something used to end with
everything it knew: findings, evidence, and proposed edits in one block. The
person had to read all of it before answering any of it, and then answered in
prose. This module is the part of the other way the runtime trusts: a closed
`questions` list a stage returns, validated here, and asked one at a time in
the order `ask_order` gives.

Three decisions are worth stating.

**The recommendation comes first.** `recommended` is either `null` or the first
option, so a host that shows options in order always shows it first, and one
that cannot mark a recommendation still leads with it.

**A question says what it blocks.** `blocks` names the findings, uncertainties,
or stages the answer settles. The question that settles the most is asked
first; ties keep the stage's own order.

**Options never close the answer.** A free-text answer is always available, so
two to four options is a suggestion, not a menu the person is held to. That is
a rule for whoever asks, and it is why no option has to mean "other".
"""

from __future__ import annotations

import re

#: What each question carries, and nothing else.
QUESTION_KEYS = frozenset({"id", "prompt", "options", "recommended", "blocks"})
QUESTION_ID = re.compile(r"Q-[0-9]{3}")

MIN_OPTIONS, MAX_OPTIONS = 2, 4
#: The most questions one stop may ask before the list is treated as malformed.
MAX_QUESTIONS = 20
MAX_PROMPT_LENGTH = 600
MAX_OPTION_LENGTH = 200
MAX_REFERENCE_LENGTH = 200


def _text(value: object, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def questions_errors(questions: object, known: frozenset[str] | None = None) -> list[str]:
    """What is wrong with one `questions` list. Empty means the shape holds.

    With `known`, every `blocks` entry must name one of those identifiers: a
    question that settles nothing the result reports is not one to ask.
    """
    if not isinstance(questions, list) or len(questions) > MAX_QUESTIONS:
        return [f"`questions` must be a list of at most {MAX_QUESTIONS}"]
    problems: list[str] = []
    seen: set[str] = set()
    for index, question in enumerate(questions):
        where = f"`questions[{index}]`"
        if not isinstance(question, dict):
            problems.append(f"{where} must be an object")
            continue
        if set(question) != QUESTION_KEYS:
            problems.append(
                f"{where} keys {sorted(map(str, question))} differ from "
                f"{sorted(QUESTION_KEYS)}")
        identifier = question.get("id")
        if not isinstance(identifier, str) or QUESTION_ID.fullmatch(identifier) is None:
            problems.append(f"{where}.id must be a Q-NNN identifier")
        elif identifier in seen:
            problems.append(f"{where}.id duplicates {identifier}")
        else:
            seen.add(identifier)
        if not _text(question.get("prompt"), MAX_PROMPT_LENGTH):
            problems.append(f"{where}.prompt must be text of at most "
                            f"{MAX_PROMPT_LENGTH} characters")
        options = question.get("options")
        if (not isinstance(options, list)
                or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS
                or not all(_text(option, MAX_OPTION_LENGTH) for option in options)
                or len(set(options)) != len(options)):
            problems.append(f"{where}.options must be {MIN_OPTIONS} to {MAX_OPTIONS} "
                            "distinct texts")
            options = None
        recommended = question.get("recommended")
        if recommended is not None and (options is None or recommended != options[0]):
            problems.append(f"{where}.recommended must be null or the first option")
        blocks = question.get("blocks")
        if (not isinstance(blocks, list) or not blocks
                or not all(_text(ref, MAX_REFERENCE_LENGTH) for ref in blocks)
                or len(set(blocks)) != len(blocks)):
            problems.append(f"{where}.blocks must be a non-empty list of distinct references")
        elif known is not None:
            unknown = [ref for ref in blocks if ref not in known]
            if unknown:
                problems.append(f"{where}.blocks names what the result does not report")
    return problems


def ask_order(questions: list[dict] | tuple[dict, ...]) -> list[dict]:
    """The questions in asking order: the one that settles the most first."""
    return sorted(questions, key=lambda question: -len(question["blocks"]))
