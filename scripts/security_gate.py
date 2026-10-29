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

# Sensible defaults for a repository that has not declared its own. These
# remain replaceable; MANDATORY_* below is always combined with a project rule.
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

# The canonical non-reducible minimum for every repository. The documentation
# mirrors these constants and tests enforce that it stays in sync. Review
# skills may widen this rule, but cannot narrow it.
MANDATORY_PATHS = (
    ".github/workflows/**",
    ".github/actions/**",
    ".claude/**",
    ".codex/**",
    ".agents/**",
)

MANDATORY_FILES = (
    ".gitlab-ci.yml",
    "bitbucket-pipelines.yml",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    ".code-cycle.yml",
    "AGENTS.md",
    "CLAUDE.md",
    "opencode.json*",
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
    """A repository rule combined with the non-reducible security minimum."""

    paths: tuple[str, ...] = tuple(dict.fromkeys((*DEFAULT_PATHS, *MANDATORY_PATHS)))
    files: tuple[str, ...] = tuple(dict.fromkeys((*DEFAULT_FILES, *MANDATORY_FILES)))
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
            paths=tuple(dict.fromkeys((*block.get("paths", ()), *MANDATORY_PATHS)))
            if block.get("paths")
            else tuple(dict.fromkeys((*DEFAULT_PATHS, *MANDATORY_PATHS))),
            files=tuple(dict.fromkeys((*block.get("files", ()), *MANDATORY_FILES)))
            if block.get("files")
            else tuple(dict.fromkeys((*DEFAULT_FILES, *MANDATORY_FILES))),
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
    paths_rule = tuple(dict.fromkeys((*rule.paths, *MANDATORY_PATHS)))
    files_rule = tuple(dict.fromkeys((*rule.files, *MANDATORY_FILES)))
    paths = [p for p in changed_paths if p]

    matched_paths = tuple(sorted({p for p in paths if _matches_path(p, paths_rule)}))
    matched_files = tuple(
        sorted(
            {
                p
                for p in paths
                if any(fnmatch.fnmatch(p.rsplit("/", 1)[-1], f) for f in files_rule)
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
