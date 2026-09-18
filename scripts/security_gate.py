"""Deterministic trigger for the security audit.

Deciding whether a change needs a security review used to be a judgement call
made by a coordinator reading the diff. That makes the cheapest component in the
cycle the gatekeeper of its most expensive check, and a false negative there
happens before any careful model ever sees the change. Paying for a better
gatekeeper does not fix it: it is still an unverified semantic judgement.

So the decision becomes a union:

    security_required = deterministic_rule OR reviewer_requests_security

A model can *add* a security review. It can never remove one the rule fired.
A false negative then requires both to fail at once, and the rule costs nothing
to run.

The rule is deliberately dumb. It matches paths, filenames and labels, and it
does not try to understand the change — understanding is what the reviewer is
for, and the point here is to have one detector that cannot be talked out of it.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

# Sensible defaults for a repository that has not declared its own. They are
# deliberately broad: a security audit that was not needed costs credits, and a
# missed one costs a vulnerability. They are also not a taxonomy — a project
# states its own and those replace these entirely.
DEFAULT_PATHS = (
    "auth/**",
    "**/auth/**",
    "middleware/**",
    "**/middleware/**",
    "migrations/**",
    "**/migrations/**",
    "routes/**",
    "**/routes/**",
    "**/Policies/**",
    "**/policies/**",
)

DEFAULT_FILES = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "composer.lock",
    "requirements.txt",
    "poetry.lock",
    "Gemfile.lock",
    "go.sum",
    "Cargo.lock",
    "Dockerfile",
    "docker-compose*.yml",
    "*.env.example",
)

# Matched by meaning, not by exact string: every project spells them differently.
DEFAULT_LABEL_TERMS = (
    "security",
    "auth",
    "authz",
    "authentication",
    "authorization",
    "permission",
    "privacy",
    "data",
    "secret",
    "credential",
    "dependencies",
)


class SecurityGateError(ValueError):
    """The gate was given a rule it cannot evaluate."""


@dataclass(frozen=True)
class GateDecision:
    """Why the audit is or is not required, in terms a human can check."""

    required: bool
    matched_paths: tuple[str, ...] = ()
    matched_files: tuple[str, ...] = ()
    matched_labels: tuple[str, ...] = ()
    reviewer_requested: bool = False

    @property
    def deterministic(self) -> bool:
        """True when the rule fired, regardless of what any model thought."""
        return bool(self.matched_paths or self.matched_files or self.matched_labels)

    def reason(self) -> str:
        if not self.required:
            return "no rule matched and no reviewer asked for an audit"
        parts = []
        if self.matched_paths:
            parts.append("paths: " + ", ".join(self.matched_paths))
        if self.matched_files:
            parts.append("files: " + ", ".join(self.matched_files))
        if self.matched_labels:
            parts.append("labels: " + ", ".join(self.matched_labels))
        if self.reviewer_requested:
            parts.append("the reviewer asked for one")
        return "; ".join(parts)


@dataclass(frozen=True)
class GateRule:
    """One repository's `security_review.always_when` block."""

    paths: tuple[str, ...] = DEFAULT_PATHS
    files: tuple[str, ...] = DEFAULT_FILES
    label_terms: tuple[str, ...] = DEFAULT_LABEL_TERMS

    @classmethod
    def from_config(cls, config: dict | None) -> "GateRule":
        """Read the rule from parsed `.code-cycle.yml`, falling back to defaults.

        A repository that declares nothing gets the defaults rather than an
        empty rule: an absent configuration must not silently disable the gate,
        which would make the safe-looking case the unsafe one.
        """
        if not config:
            return cls()
        block = (
            config.get("code_cycle", {})
            .get("security_review", {})
            .get("always_when", {})
        )
        if not isinstance(block, dict):
            raise SecurityGateError(
                f"security_review.always_when must be a mapping, got {type(block).__name__}"
            )
        return cls(
            paths=tuple(block.get("paths") or DEFAULT_PATHS),
            files=tuple(block.get("files") or DEFAULT_FILES),
            label_terms=tuple(block.get("labels") or DEFAULT_LABEL_TERMS),
        )


def _matches_path(path: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        # `auth/**` should also match a nested `src/auth/token.php`, because a
        # repository's layout is not the gate's business.
        if pattern.endswith("/**") and re.search(
            rf"(^|/){re.escape(pattern[:-3])}/", path
        ):
            return True
    return False


def evaluate(
    changed_paths,
    labels=(),
    *,
    rule: GateRule | None = None,
    reviewer_requested: bool = False,
) -> GateDecision:
    """Decide whether the security audit runs.

    `reviewer_requested` is the model's opinion. It is ORed in, never ANDed:
    the model may add an audit and may not remove one.
    """
    rule = rule or GateRule()
    paths = [p for p in changed_paths if p]

    matched_paths = tuple(sorted({p for p in paths if _matches_path(p, rule.paths)}))
    matched_files = tuple(
        sorted(
            {
                p
                for p in paths
                if any(fnmatch.fnmatch(p.rsplit("/", 1)[-1], f) for f in rule.files)
            }
            - set(matched_paths)
        )
    )
    lowered = [(l or "").lower() for l in labels]
    matched_labels = tuple(
        sorted({l for l in lowered if any(term in l for term in rule.label_terms)})
    )

    fired = bool(matched_paths or matched_files or matched_labels)
    return GateDecision(
        required=fired or bool(reviewer_requested),
        matched_paths=matched_paths,
        matched_files=matched_files,
        matched_labels=matched_labels,
        reviewer_requested=bool(reviewer_requested),
    )
