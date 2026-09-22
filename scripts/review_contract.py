"""Reference parser for the Code Cycle machine-readable review record.

The published change-request comment is the durable record. `ORCHESTRATION_RESULT`
is an optional mirror, so every field an analysis needs must survive in the
comment alone. This module is the executable definition of that comment
contract: the skills state it in prose, and this parser is what reads it back.

It is deliberately dependency-free and side-effect-free. It does not call a
code host, choose a model, or score anything; it parses, formats, and checks
the invariants the contract promises.

Three line kinds carry the record:

    #### [CCR-20260918-001] · senior_reviewer · anthropic/sonnet-5→sonnet-5 · high · schema:1
    #### [CCT-20260918-001] · cheap_coder · openai/luna-high→luna-high · high · triaged:<sha> · schema:1
    #### [REV-004] · medium · resolved · valid · blocks:yes — Short title

A review run line is emitted by the skill that opens a review run. A triage run
line is emitted by the skill that freezes dispositions, and anchors them to the
commit every finding was judged against. A finding header is emitted for every
published finding, new or previous.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

SCHEMA_VERSION = 1

SEPARATOR = "·"
ARROW = "→"
TITLE_DASH = "—"

SEVERITIES = ("critical", "high", "medium", "low")
FINDING_STATUSES = ("open", "resolved", "not_applicable")
DISPOSITIONS = (
    "valid",
    "debatable",
    "incorrect",
    "obsolete",
    "needs_clarification",
)

# A finding published by a reviewer has not been triaged yet, and a legacy
# four-token header predates the disposition token. Both read as untriaged.
UNTRIAGED = "-"

REVIEW_RUN_PREFIX = "CCR-"
TRIAGE_RUN_PREFIX = "CCT-"
FINDING_PREFIX = "REV-"
CANDIDATE_PREFIX = "CAL-"

UNKNOWN_MODEL = "?"

_HEADER_RE = re.compile(r"^#{1,6}\s*\[([A-Z]+-[A-Za-z0-9-]+)\]\s*(.*)$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SCHEMA_RE = re.compile(r"^schema:(\d+)$")
_TRIAGED_RE = re.compile(r"^triaged:(.+)$")
_BLOCKS_RE = re.compile(r"^blocks:(yes|no)$")


class ContractError(ValueError):
    """A line claims to follow the contract but violates it."""


class FindingCollisionError(ContractError):
    """One stable finding ID was reused for a different finding."""


@dataclass(frozen=True)
class ModelSpec:
    """`provider/requested→resolved`, with both sides always written.

    `resolved is None` means the host did not report which model actually ran.
    That is written `?`, and it is information: it says "we could not know",
    which is not the same claim as "it did not change".
    """

    provider: str
    requested: str
    resolved: str | None

    def __str__(self) -> str:
        resolved = UNKNOWN_MODEL if self.resolved is None else self.resolved
        return f"{self.provider}/{self.requested}{ARROW}{resolved}"

    @property
    def drifted(self) -> bool:
        """True only when the host reported a model other than the requested one."""
        return self.resolved is not None and self.resolved != self.requested


@dataclass(frozen=True)
class RunLine:
    """A review run or a triage run.

    `triaged_sha` is set for triage runs only, and is the commit every
    disposition in that run was judged against.
    """

    id: str
    profile: str
    model: ModelSpec
    effort: str
    schema_version: int = SCHEMA_VERSION
    triaged_sha: str | None = None

    @property
    def kind(self) -> str:
        return "triage" if self.id.startswith(TRIAGE_RUN_PREFIX) else "review"


@dataclass(frozen=True)
class Finding:
    """One published finding header.

    `status` is what happened to the code. `disposition` is what the first
    resolver thought of the finding. They are orthogonal and both are needed.
    """

    id: str
    severity: str
    status: str
    blocks_approval: bool
    title: str = ""
    disposition: str = UNTRIAGED
    legacy: bool = False

    @property
    def triaged(self) -> bool:
        return self.disposition != UNTRIAGED


@dataclass(frozen=True)
class ProviderComment:
    """One provider comment with identity metadata kept separate from its body."""

    author: str
    body: str


@dataclass
class ReviewRecord:
    """Everything the contract lets a later run recover from trusted comments."""

    runs: list[RunLine] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    attribution: dict[str, str] = field(default_factory=dict)
    recovery_notes: list[str] = field(default_factory=list)

    @property
    def review_runs(self) -> list[RunLine]:
        return [run for run in self.runs if run.kind == "review"]

    @property
    def triage_runs(self) -> list[RunLine]:
        return [run for run in self.runs if run.kind == "triage"]

    def finding(self, finding_id: str) -> Finding | None:
        for item in self.findings:
            if item.id == finding_id:
                return item
        return None


def _split_tokens(rest: str) -> list[str]:
    return [token.strip() for token in rest.split(SEPARATOR) if token.strip()]


def parse_model_spec(token: str) -> ModelSpec:
    """Parse `provider/requested→resolved`.

    The arrow is mandatory on both sides, including when they match. A constant
    shape is cheaper to parse than an optional arrow, and it turns "no drift"
    into an observable statement instead of an absence.
    """
    if ARROW not in token:
        raise ContractError(
            f"model spec must always write both sides of {ARROW!r}: {token!r}"
        )
    left, _, resolved = token.partition(ARROW)
    if "/" not in left:
        raise ContractError(f"model spec must be provider/model: {token!r}")
    provider, _, requested = left.partition("/")
    provider, requested, resolved = provider.strip(), requested.strip(), resolved.strip()
    if not provider or not requested or not resolved:
        raise ContractError(f"model spec has an empty component: {token!r}")
    return ModelSpec(
        provider=provider,
        requested=requested,
        resolved=None if resolved == UNKNOWN_MODEL else resolved,
    )


def parse_run_line(line: str) -> RunLine | None:
    """Parse a review or triage run line, or return None when it is not one."""
    match = _HEADER_RE.match(line.strip())
    if match is None:
        return None
    run_id = match.group(1)
    if not run_id.startswith((REVIEW_RUN_PREFIX, TRIAGE_RUN_PREFIX)):
        return None

    tokens = _split_tokens(match.group(2))
    if len(tokens) < 4:
        raise ContractError(f"run line needs profile, model, effort, schema: {line!r}")

    profile, model_token, effort = tokens[0], tokens[1], tokens[2]
    triaged_sha: str | None = None
    schema_version: int | None = None

    for token in tokens[3:]:
        schema_match = _SCHEMA_RE.match(token)
        if schema_match is not None:
            schema_version = int(schema_match.group(1))
            continue
        triaged_match = _TRIAGED_RE.match(token)
        if triaged_match is not None:
            triaged_sha = triaged_match.group(1).strip()
            if not _SHA_RE.match(triaged_sha):
                raise ContractError(f"triaged: needs a commit SHA: {token!r}")
            continue
        raise ContractError(f"unknown token in run line: {token!r}")

    if schema_version is None:
        raise ContractError(f"run line is missing schema:<n>: {line!r}")

    run = RunLine(
        id=run_id,
        profile=profile,
        model=parse_model_spec(model_token),
        effort=effort,
        schema_version=schema_version,
        triaged_sha=triaged_sha,
    )
    if run.kind == "triage" and run.triaged_sha is None:
        raise ContractError(f"a triage run line must carry triaged:<sha>: {line!r}")
    if run.kind == "review" and run.triaged_sha is not None:
        raise ContractError(f"a review run line must not carry triaged: {line!r}")
    return run


def format_run_line(run: RunLine, level: int = 4) -> str:
    parts = [f"[{run.id}]", run.profile, str(run.model), run.effort]
    if run.triaged_sha is not None:
        parts.append(f"triaged:{run.triaged_sha}")
    parts.append(f"schema:{run.schema_version}")
    return f"{'#' * level} " + f" {SEPARATOR} ".join(parts)


def parse_finding_header(line: str) -> Finding | None:
    """Parse a finding header in either the legacy or the current form.

    Legacy, four tokens:  severity, status, blocks
    Current, five tokens: severity, status, disposition, blocks

    A legacy header reads as `disposition = -`. Live change requests opened
    before this contract must keep working; a header is never rejected merely
    for predating the disposition token.
    """
    match = _HEADER_RE.match(line.strip())
    if match is None:
        return None
    finding_id = match.group(1)
    if not finding_id.startswith(FINDING_PREFIX):
        return None

    rest = match.group(2)
    title = ""
    separator = f" {TITLE_DASH} "
    if separator in rest:
        rest, _, title = rest.partition(separator)
    tokens = _split_tokens(rest)

    if len(tokens) == 3:
        severity, status, blocks = tokens
        disposition, legacy = UNTRIAGED, True
    elif len(tokens) == 4:
        severity, status, disposition, blocks = tokens
        legacy = False
    else:
        raise ContractError(
            f"finding header needs 3 legacy or 4 current tokens, got {len(tokens)}: {line!r}"
        )

    if severity not in SEVERITIES:
        raise ContractError(f"unknown severity {severity!r} in {line!r}")
    if status not in FINDING_STATUSES:
        raise ContractError(f"unknown finding status {status!r} in {line!r}")
    if disposition != UNTRIAGED and disposition not in DISPOSITIONS:
        raise ContractError(f"unknown disposition {disposition!r} in {line!r}")
    blocks_match = _BLOCKS_RE.match(blocks)
    if blocks_match is None:
        raise ContractError(f"expected blocks:yes or blocks:no in {line!r}")

    return Finding(
        id=finding_id,
        severity=severity,
        status=status,
        blocks_approval=blocks_match.group(1) == "yes",
        title=title.strip(),
        disposition=disposition,
        legacy=legacy,
    )


def format_finding_header(finding: Finding, level: int = 4) -> str:
    """Format a finding header in the current five-token form.

    Republishing a legacy finding in the current form is lossless: its
    disposition is written `-`, which is what the legacy header already meant.
    """
    parts = [
        f"[{finding.id}]",
        finding.severity,
        finding.status,
        finding.disposition,
        "blocks:yes" if finding.blocks_approval else "blocks:no",
    ]
    header = f"{'#' * level} " + f" {SEPARATOR} ".join(parts)
    if finding.title:
        header = f"{header} {TITLE_DASH} {finding.title}"
    return header


def parse_attribution_block(text: str) -> dict[str, str]:
    """Parse the calibration attribution block, if the comment carries one.

    The block maps published findings to the review run that produced them. It
    exists only for a paired calibration campaign, is published after the human
    matching, and is not part of the permanent shared contract.
    """
    mapping: dict[str, str] = {}
    pattern = re.compile(
        rf"^[-*]\s*({FINDING_PREFIX}[A-Za-z0-9-]+|{CANDIDATE_PREFIX}[A-Za-z0-9-]+)"
        rf"\s*(?:←|<-)\s*({REVIEW_RUN_PREFIX}[A-Za-z0-9-]+)\s*$"
    )
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match is not None:
            mapping[match.group(1)] = match.group(2)
    return mapping


def parse_comment(text: str) -> ReviewRecord:
    """Read a published change-request comment into a record.

    Prose around the contract lines is ignored, in any language: only the
    language-neutral tokens are read.
    """
    record = ReviewRecord()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        run = parse_run_line(stripped)
        if run is not None:
            record.runs.append(run)
            continue
        finding = parse_finding_header(stripped)
        if finding is not None:
            record.findings.append(finding)
    record.attribution = parse_attribution_block(text)
    return record


def merge_disposition(previous: str | None, incoming: str) -> str:
    """Return the disposition that survives.

    The first resolver that triages a finding assigns its disposition, and it
    is frozen from then on. A later pass judges code that has already been
    fixed, so a second opinion would measure the fix rather than whether the
    finding was real when it was written.
    """
    if previous and previous != UNTRIAGED:
        return previous
    return incoming


def merge_finding(previous: Finding | None, incoming: Finding) -> Finding:
    """Carry a finding forward into a republished comment.

    `status` moves with the code. `disposition` does not.
    """
    if previous is None:
        return incoming
    return replace(
        incoming,
        severity=previous.severity,
        title=previous.title or incoming.title,
        disposition=merge_disposition(previous.disposition, incoming.disposition),
    )


def _finding_identity_disagreements(previous: Finding, incoming: Finding) -> list[str]:
    """Describe mutable-header disagreements without rewriting first publication.

    A review can legitimately re-score or reword a finding. Those differences
    are audit data, not evidence by themselves that an ID was reused for a
    different finding. The recovered record keeps the first severity, title,
    and disposition while allowing status and approval blocking to advance.
    """
    disagreements: list[str] = []
    if previous.severity != incoming.severity:
        disagreements.append(
            f"severity {previous.severity!r} was republished as {incoming.severity!r}"
        )
    if (
        previous.title
        and incoming.title
        and previous.title.casefold() != incoming.title.casefold()
    ):
        disagreements.append(
            f"title {previous.title!r} was republished as {incoming.title!r}"
        )
    if (
        previous.triaged
        and incoming.triaged
        and previous.disposition != incoming.disposition
    ):
        disagreements.append(
            "disposition "
            f"{previous.disposition!r} was republished as {incoming.disposition!r}"
        )
    return disagreements


def _same_comment_collision(previous: Finding, incoming: Finding) -> bool:
    """Whether one trusted snapshot assigns an ID to incompatible findings."""
    return bool(
        previous.severity != incoming.severity
        or (
            previous.title
            and incoming.title
            and previous.title.casefold() != incoming.title.casefold()
        )
    )


def recover_comment_history(
    comments: Iterable[ProviderComment], *, trusted_authors: Iterable[str]
) -> ReviewRecord:
    """Fold complete change-request comments, oldest first, into one record.

    A comment is a partial update, not a replacement snapshot. Omitting a
    finding therefore leaves its recovered state intact; a later header for the
    same ID may advance its status while preserving its first published fields.
    The caller must pass provider author metadata and the configured review
    identities; bodies from every other author are ignored and reported.
    """
    trusted = frozenset(trusted_authors)
    if not trusted:
        raise ContractError("history recovery needs at least one trusted author")

    recovered = ReviewRecord()
    findings: dict[str, Finding] = {}
    positions: dict[str, int] = {}
    runs: set[str] = set()

    for number, comment in enumerate(comments, start=1):
        if comment.author not in trusted:
            recovered.recovery_notes.append(
                f"comment {number} from {comment.author!r} ignored: author is not trusted"
            )
            continue
        try:
            record = parse_comment(comment.body)
        except ContractError as error:
            recovered.recovery_notes.append(
                f"comment {number} from {comment.author!r} skipped: {error}"
            )
            continue
        for run in record.runs:
            if run.id not in runs:
                recovered.runs.append(run)
                runs.add(run.id)
        in_comment: dict[str, Finding] = {}
        for incoming in record.findings:
            snapshot = in_comment.get(incoming.id)
            if snapshot is not None and _same_comment_collision(snapshot, incoming):
                raise FindingCollisionError(
                    f"{incoming.id} identifies incompatible findings in comment {number}"
                )
            in_comment[incoming.id] = incoming
            previous = findings.get(incoming.id)
            if previous is not None:
                for disagreement in _finding_identity_disagreements(previous, incoming):
                    recovered.recovery_notes.append(f"{incoming.id}: {disagreement}")
            merged = merge_finding(previous, incoming)
            findings[incoming.id] = merged
            if previous is None:
                positions[incoming.id] = len(recovered.findings)
                recovered.findings.append(merged)
            else:
                recovered.findings[positions[incoming.id]] = merged
        for finding_id, run_id in record.attribution.items():
            recovered.attribution.setdefault(finding_id, run_id)

    return recovered


def next_finding_id(record: ReviewRecord | Iterable[Finding]) -> str:
    """Allocate after the highest numeric REV-ID recovered from all history."""
    findings = record.findings if isinstance(record, ReviewRecord) else record
    numeric_ids = [
        int(match.group(1))
        for item in findings
        if (match := re.fullmatch(rf"{FINDING_PREFIX}(\d+)", item.id)) is not None
    ]
    next_number = max(numeric_ids, default=0) + 1
    return f"{FINDING_PREFIX}{next_number:03d}"


def disposition_conflicts(
    previous: list[Finding] | ReviewRecord, incoming: list[Finding] | ReviewRecord
) -> list[str]:
    """Report attempts to overwrite a frozen disposition.

    A conflict is audit data, not a reason to rewrite history: the caller keeps
    the frozen value and records that a later pass disagreed.
    """
    before = previous.findings if isinstance(previous, ReviewRecord) else previous
    after = incoming.findings if isinstance(incoming, ReviewRecord) else incoming
    known = {item.id: item.disposition for item in before}
    conflicts = []
    for item in after:
        frozen = known.get(item.id)
        if frozen and frozen != UNTRIAGED and item.triaged and item.disposition != frozen:
            conflicts.append(
                f"{item.id}: disposition {frozen!r} is frozen, "
                f"republished as {item.disposition!r}"
            )
    return conflicts


def verify_triage_freeze(
    record: ReviewRecord, *, pre_edit_sha: str | None = None
) -> list[str]:
    """Check that dispositions were frozen against one commit before editing.

    This is the mechanism behind "classify everything first, then edit". The
    triage run line anchors the whole triage to the commit it judged, so a
    record where the anchor is the post-fix HEAD, or where some findings were
    never classified, is detectable from the comment alone rather than trusted.
    """
    problems: list[str] = []
    triage_runs = record.triage_runs
    if not triage_runs:
        problems.append("no triage run line: dispositions are not anchored to a commit")
        return problems
    if len({run.triaged_sha for run in triage_runs}) > 1:
        problems.append(
            "findings were triaged against more than one commit in the same record"
        )
    anchor = triage_runs[-1].triaged_sha
    if pre_edit_sha is not None and anchor not in (pre_edit_sha, pre_edit_sha[: len(anchor or "")]):
        problems.append(
            f"triage is anchored to {anchor}, not to the pre-edit commit {pre_edit_sha}"
        )
    untriaged = [item.id for item in record.findings if not item.triaged]
    if untriaged:
        problems.append(
            "these findings reached the edit step without a disposition: "
            + ", ".join(untriaged)
        )
    return problems


def finding_outcomes(record: ReviewRecord) -> list[dict[str, str]]:
    """Build the optional `ORCHESTRATION_RESULT` mirror from the durable record.

    The mirror is derived from the comment, never the other way round.
    """
    return [
        {"id": item.id, "disposition": item.disposition, "status": item.status}
        for item in record.findings
    ]
