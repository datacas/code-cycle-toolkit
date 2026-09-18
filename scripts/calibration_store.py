"""Campaign store for blind paired-review calibration.

During a paired campaign two reviewers judge the same commit and their findings
are merged and shuffled before triage, so the resolver cannot see who wrote
what. The map from opaque candidate IDs back to review runs therefore cannot
live in the comment while the campaign is running — and it is the one piece of
campaign data that cannot be reconstructed afterwards.

So it lives here: a working store outside every repository, written atomically
so an interrupted campaign can be resumed. When a paired change request is
finished — after the blind triage, the frozen dispositions and the human
matching — the attribution is published into the durable comment and the store
has done its job.

This file never enters Git, and it holds identifiers only: no tokens, no diff
content, no repository data.

The published block is a calibration artefact, not part of the permanent shared
skill contract. The campaign ends; the contract does not.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

from review_contract import (
    CANDIDATE_PREFIX,
    REVIEW_RUN_PREFIX,
    SCHEMA_VERSION,
    SEPARATOR,
    ModelSpec,
    RunLine,
    format_run_line,
)

STORE_DIRNAME = "calibration"
APP_DIRNAME = "code-cycle-toolkit"

# How the two reviewers were kept from interfering. `shared_concurrent` exists
# only so a pair dispatched that way can be recorded and then excluded; it is
# never a mode to dispatch in.
ISOLATION_MODES = ("isolated", "sequential", "shared_concurrent")
USABLE_ISOLATION_MODES = ("isolated", "sequential")

# Whose code the reviewers were judging. A toolkit reviewing itself cannot
# separate "this reviewer is better" from "this reviewer wrote that code", so the
# relation is recorded rather than corrected for.
TARGET_RELATIONS = ("self_toolkit", "external_project")

_CAMPAIGN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CalibrationError(ValueError):
    """The campaign store was asked for something it must not do."""


def default_store_dir() -> Path:
    """Locate the host-local store, matching the provider-health convention.

    Unix-like hosts use `~/.config/code-cycle-toolkit/`; Windows uses the
    user's application-data directory.
    """
    override = os.environ.get("CODE_CYCLE_HOME")
    if override:
        base = Path(override)
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) / APP_DIRNAME if appdata else Path.home() / APP_DIRNAME
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        root = Path(xdg) if xdg else Path.home() / ".config"
        base = root / APP_DIRNAME
    return base / STORE_DIRNAME


@dataclass(frozen=True)
class Candidate:
    """One reviewer's finding, under an opaque ID, before attribution."""

    candidate_id: str
    review_run_id: str
    arm: str

    def __post_init__(self) -> None:
        if not self.candidate_id.startswith(CANDIDATE_PREFIX):
            raise CalibrationError(
                f"candidate id must start with {CANDIDATE_PREFIX!r}: {self.candidate_id!r}"
            )
        if not self.review_run_id.startswith(REVIEW_RUN_PREFIX):
            raise CalibrationError(
                f"review run id must start with {REVIEW_RUN_PREFIX!r}: {self.review_run_id!r}"
            )


class Campaign:
    """The working attribution store for one calibration campaign.

    Every mutation is persisted immediately and atomically, because the point of
    this file is to survive an interruption partway through a change request.
    """

    def __init__(self, campaign_id: str, store_dir: Path | None = None) -> None:
        if not _CAMPAIGN_RE.match(campaign_id):
            raise CalibrationError(f"unsafe campaign id: {campaign_id!r}")
        self.campaign_id = campaign_id
        self.store_dir = Path(store_dir) if store_dir is not None else default_store_dir()
        self.path = self.store_dir / f"{campaign_id}.json"
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.is_file():
            return {
                "schema_version": SCHEMA_VERSION,
                "campaign_id": self.campaign_id,
                "runs": {},
                "candidates": {},
            }
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("runs", {})
        data.setdefault("candidates", {})
        return data

    def _save(self) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.store_dir, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self._data, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @property
    def schema_version(self) -> int:
        return int(self._data.get("schema_version", SCHEMA_VERSION))

    def record_run(self, run: RunLine, arm: str) -> None:
        """Register a review run and which experimental arm produced it."""
        if run.kind != "review":
            raise CalibrationError(f"only review runs are attributable: {run.id!r}")
        self._data["runs"][run.id] = {
            "arm": arm,
            "profile": run.profile,
            "provider": run.model.provider,
            "model_requested": run.model.requested,
            "model_resolved": run.model.resolved,
            "effort": run.effort,
            "schema_version": run.schema_version,
        }
        self._save()

    def record_candidate(self, candidate_id: str, review_run_id: str) -> Candidate:
        """Bind an opaque candidate ID to the run that produced it."""
        if review_run_id not in self._data["runs"]:
            raise CalibrationError(f"unknown review run: {review_run_id!r}")
        existing = self._data["candidates"].get(candidate_id)
        if existing is not None and existing != review_run_id:
            raise CalibrationError(
                f"{candidate_id} is already bound to {existing}; "
                "attribution is never silently rebound"
            )
        self._data["candidates"][candidate_id] = review_run_id
        self._save()
        return Candidate(
            candidate_id=candidate_id,
            review_run_id=review_run_id,
            arm=self._data["runs"][review_run_id]["arm"],
        )

    def mint_presentation_ids(self, candidate_ids: list[str] | tuple[str, ...]) -> list[dict]:
        """Give each finding an opaque id and return them in shuffled order.

        The human who matches root causes must not be able to tell which reviewer
        wrote what. Two labelled lists fail at this even when the labels are
        meaningless, because the count per label attributes them: four findings
        under one label and one under the other identifies both arms to anyone
        who knows how many each produced.

        So there are no labels and no groups. One shuffled list of opaque ids,
        minted here rather than by the reviewers, and the map back to each run
        stays in this store until the matching is closed.
        """
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alike characters
        presentation = self._data.setdefault("presentation", {})
        rows = []
        for candidate_id in candidate_ids:
            if candidate_id not in self._data["candidates"]:
                raise CalibrationError(f"unknown candidate: {candidate_id!r}")
            existing = next(
                (k for k, v in presentation.items() if v == candidate_id), None
            )
            if existing is None:
                while True:
                    existing = "F-" + "".join(
                        secrets.choice(alphabet) for _ in range(4)
                    )
                    if existing not in presentation:
                        break
                presentation[existing] = candidate_id
            rows.append({"display_id": existing, "candidate_id": candidate_id})
        secrets.SystemRandom().shuffle(rows)
        self._save()
        return rows

    def reveal_presentation(self) -> dict[str, dict[str, str]]:
        """Resolve opaque ids back to reviewer runs. Call only after matching."""
        out = {}
        for display_id, candidate_id in sorted(
            self._data.get("presentation", {}).items()
        ):
            run_id = self._data["candidates"][candidate_id]
            out[display_id] = {
                "candidate_id": candidate_id,
                "review_run_id": run_id,
                "arm": self._data["runs"][run_id]["arm"],
            }
        return out

    def bind_published_id(self, candidate_id: str, finding_id: str) -> None:
        """Record the public `REV-xxx` a candidate received once merged."""
        if candidate_id not in self._data["candidates"]:
            raise CalibrationError(f"unknown candidate: {candidate_id!r}")
        published = self._data.setdefault("published", {})
        existing = published.get(finding_id)
        if existing is not None and existing != candidate_id:
            raise CalibrationError(
                f"{finding_id} is already bound to {existing}; "
                "attribution is never silently rebound"
            )
        published[finding_id] = candidate_id
        self._save()

    @staticmethod
    def pair_key(change_request_id: str, head_sha: str) -> str:
        """Identify a pair by the change request *and* the commit it reviewed.

        Keying by change request alone loses a campaign: a second pair on the
        same change request at a new commit would collide with the first, and
        because an existing row is never overwritten, a later dispatch that
        crashed would leave no row at all — behind a row still marked usable
        from the previous commit. That is precisely the guarantee `open_pair`
        exists to provide, so the commit belongs in the identity.
        """
        return f"{change_request_id}@{head_sha[:7]}"

    def open_pair(
        self,
        change_request_id: str,
        *,
        head_sha: str,
        target_relation: str = "external_project",
    ) -> str:
        """Register a paired dispatch before the reviewers run.

        A pair starts excluded and earns its way in. Recording only on success
        would make a crashed or abandoned run invisible rather than excluded,
        and an absent row reads as "never attempted", which is a different
        claim from "attempted and unusable".
        """
        key = self.pair_key(change_request_id, head_sha)
        pairs = self._data.setdefault("pairs", {})
        if key in pairs:
            return key
        if target_relation not in TARGET_RELATIONS:
            raise CalibrationError(
                f"target_relation must be one of {sorted(TARGET_RELATIONS)}: {target_relation!r}"
            )
        pairs[key] = {
            "state": "dispatched",
            "pair_key": key,
            "change_request_id": change_request_id,
            "target_relation": target_relation,
            "isolation": None,
            "head_sha": head_sha,
            "observed_head_shas": {},
            "head_drifted_for": [],
            "usable": False,
            "excluded_because": "dispatch did not complete",
        }
        self._save()
        return key

    def record_pair(
        self,
        change_request_id: str,
        *,
        isolation: str,
        head_sha: str,
        observed_head_shas: dict[str, str],
        expected_review_run_ids: list[str] | set[str] | tuple[str, ...],
    ) -> bool:
        """Record how one paired change request was dispatched, and whether it counts.

        A pair is usable only when the two reviewers could not interfere: each in
        its own worktree, or one strictly after the other. Two reviewers sharing a
        worktree concurrently is not a degraded pair, it is not a pair at all.

        The observed set must match the expected set exactly. Checking merely that
        some observation exists is not enough: a pair where one reviewer never
        reported is a one-sided review, and counting it would put a sample with a
        missing arm next to complete ones, indistinguishable. An unknown run id
        fails for the same reason — an observation nobody can attribute proves
        nothing about the reviewers that were supposed to run.

        Each observed SHA must still be the one the reviewers were given. A
        reviewer that moved the branch reviewed something its counterpart did not.

        Every reason is stored rather than merely enforced: an analysis that cannot
        tell a contaminated pair from a clean one cannot drop it either.
        """
        if isolation not in ISOLATION_MODES:
            raise CalibrationError(
                f"isolation must be one of {sorted(ISOLATION_MODES)}: {isolation!r}"
            )
        expected = set(expected_review_run_ids)
        if not expected:
            raise CalibrationError("a pair must expect at least one review run")

        observed = set(observed_head_shas)
        known = set(self._data["runs"])
        reasons = []

        if isolation not in USABLE_ISOLATION_MODES:
            reasons.append(f"reviewers were not isolated ({isolation})")

        unregistered = sorted(expected - known)
        if unregistered:
            reasons.append("expected runs are not registered: " + ", ".join(unregistered))

        missing = sorted(expected - observed)
        if missing:
            reasons.append("no head SHA was observed after " + ", ".join(missing))

        unexpected = sorted(observed - expected)
        if unexpected:
            reasons.append("observations from runs this pair did not expect: " + ", ".join(unexpected))

        drifted = sorted(
            run_id for run_id, seen in observed_head_shas.items() if seen != head_sha
        )
        if drifted:
            reasons.append("head moved during " + ", ".join(drifted))

        key = self.pair_key(change_request_id, head_sha)
        pairs = self._data.setdefault("pairs", {})
        # Keep what open_pair recorded before the reviewers ran.
        opened = pairs.get(key, {})
        pairs[key] = {
            "state": "complete",
            "pair_key": key,
            "change_request_id": change_request_id,
            "target_relation": opened.get("target_relation", "external_project"),
            "isolation": isolation,
            "head_sha": head_sha,
            "expected_review_run_ids": sorted(expected),
            "observed_head_shas": dict(observed_head_shas),
            "head_drifted_for": drifted,
            "usable": not reasons,
            "excluded_because": "; ".join(reasons) if reasons else None,
        }
        self._save()
        return not reasons

    def record_triage(
        self,
        change_request_id: str,
        *,
        head_sha: str,
        run: RunLine,
        dispositions: dict[str, str],
    ) -> None:
        """Register the resolver that triaged a pair, and what it decided.

        The resolver is part of the instrument, not a neutral observer: it is the
        one deciding whether each reviewer was right, so its identity belongs in
        the record beside the reviewers'. Keep it fixed across campaigns — moving
        the scorer and the candidates at the same time makes neither measurable.
        """
        if run.kind != "triage":
            raise CalibrationError(f"a triage run is required, got {run.id!r}")
        if run.triaged_sha != head_sha:
            raise CalibrationError(
                f"triage anchored to {run.triaged_sha}, not to the pair's head {head_sha}"
            )
        unknown = sorted(set(dispositions) - set(self._data.get("published", {})))
        if unknown:
            raise CalibrationError(
                "dispositions for findings this pair never published: " + ", ".join(unknown)
            )
        key = self.pair_key(change_request_id, head_sha)
        pair = self._data.get("pairs", {}).get(key)
        if pair is None:
            raise CalibrationError(f"unknown pair: {key!r}")
        pair["triage"] = {
            "run_id": run.id,
            "profile": run.profile,
            "provider": run.model.provider,
            "model_requested": run.model.requested,
            "model_resolved": run.model.resolved,
            "effort": run.effort,
            "triaged_sha": run.triaged_sha,
            "dispositions": dict(dispositions),
        }
        self._save()

    def capabilities(self, pair_key: str) -> dict[str, str | None]:
        """Say which metrics a pair can actually support, and why not otherwise.

        `usable_pairs()` answers one question: was this pair collected cleanly.
        That is not the same as "this pair can feed any metric". A clean pair
        whose findings were never triaged supports coverage and overlap but not
        acceptance, and treating the two as one concept would let a sample claim
        information it never produced.
        """
        pair = self._data.get("pairs", {}).get(pair_key)
        if pair is None:
            raise CalibrationError(f"unknown pair: {pair_key!r}")
        if not pair.get("usable"):
            blocked = pair.get("excluded_because") or "pair is not usable"
            return {k: blocked for k in ("coverage", "overlap", "acceptance")}

        out: dict[str, str | None] = {"coverage": None, "overlap": None}
        triage = pair.get("triage")
        published = self._data.get("published", {})
        if not triage:
            out["acceptance"] = "no resolver triaged this pair; dispositions are '-'"
        else:
            missing = sorted(set(published) - set(triage["dispositions"]))
            untriaged = sorted(
                f for f, d in triage["dispositions"].items() if d == "-"
            )
            if missing:
                out["acceptance"] = "findings never triaged: " + ", ".join(missing)
            elif untriaged:
                out["acceptance"] = "findings left untriaged: " + ", ".join(untriaged)
            else:
                out["acceptance"] = None
        return out

    def pairs_supporting(self, metric: str) -> list[str]:
        """Pairs a given metric may legitimately be computed from."""
        return sorted(
            key for key in self.usable_pairs() if self.capabilities(key).get(metric) is None
        )

    def usable_pairs(self) -> list[str]:
        """The only definition of a valid sample. Read nothing else to build one.

        Later calibration consumes this list and never the raw rows, so that
        `shared_concurrent`, a head that moved, and a dispatch that never
        finished cannot be reinterpreted downstream. One definition of a clean
        pair, in one place.
        """
        return sorted(
            key for key, pair in self._data.get("pairs", {}).items() if pair["usable"]
        )

    def excluded_pairs(self) -> dict[str, str]:
        """Pairs kept out of the sample, with the reason for each.

        Exclusions are reported, not silently dropped: a campaign that discards
        half its pairs is saying something about the dispatch, not about the
        reviewers.
        """
        return {
            key: pair["excluded_because"] or "unknown"
            for key, pair in sorted(self._data.get("pairs", {}).items())
            if not pair["usable"]
        }

    def pairs(self) -> dict[str, dict]:
        """Every recorded pair, for diagnosis. Not the sample gate.

        Use `usable_pairs()` to decide what enters an analysis.
        """
        return json.loads(json.dumps(self._data.get("pairs", {})))

    def resolve(self, candidate_id: str) -> Candidate:
        run_id = self._data["candidates"].get(candidate_id)
        if run_id is None:
            raise CalibrationError(f"unknown candidate: {candidate_id!r}")
        return Candidate(
            candidate_id=candidate_id,
            review_run_id=run_id,
            arm=self._data["runs"][run_id]["arm"],
        )

    def runs(self) -> dict[str, dict]:
        return dict(self._data["runs"])

    def candidates(self) -> dict[str, str]:
        return dict(self._data["candidates"])

    def published(self) -> dict[str, str]:
        return dict(self._data.get("published", {}))

    def run_lines(self) -> list[RunLine]:
        lines = []
        for run_id, run in sorted(self._data["runs"].items()):
            lines.append(
                RunLine(
                    id=run_id,
                    profile=run["profile"],
                    model=ModelSpec(
                        provider=run["provider"],
                        requested=run["model_requested"],
                        resolved=run["model_resolved"],
                    ),
                    effort=run["effort"],
                    schema_version=int(run.get("schema_version", SCHEMA_VERSION)),
                )
            )
        return lines

    def attribution_block(self, *, level: int = 4) -> str:
        """Render the durable attribution block for a finished paired change request.

        Publish it only after the blind triage, the frozen dispositions and the
        human matching are done. Publishing the identities earlier would tell
        the resolver, and then the matcher, who wrote each finding.
        """
        published = self.published()
        if not published:
            raise CalibrationError(
                "no finding is bound to a candidate yet; "
                "publish attribution only after the human matching"
            )
        heading = (
            f"{'#' * level} [{self.campaign_id}] {SEPARATOR} attribution "
            f"{SEPARATOR} schema:{self.schema_version}"
        )
        lines = [heading, ""]
        lines.extend(format_run_line(run, level=level + 1) for run in self.run_lines())
        lines.append("")
        for finding_id, candidate_id in sorted(published.items()):
            run_id = self._data["candidates"][candidate_id]
            lines.append(f"- {finding_id} ← {run_id}")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return json.loads(json.dumps(self._data))


def describe_candidate(candidate: Candidate) -> dict:
    return asdict(candidate)
