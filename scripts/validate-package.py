#!/usr/bin/env python3
"""Validate the public, cross-agent shape of Code Cycle Toolkit."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALLOWED_FRONTMATTER = {"name", "description", "license", "compatibility", "metadata"}
REQUIRED_SKILLS = {
    "cc-implement-issue",
    "cc-initial-review",
    "cc-resolve-comments",
    "cc-rereview",
    "cc-orchestrator",
    "cc-orca-orchestrator",
}
PRIVATE_PATTERNS = [
    re.compile(r"(?i)\bdatacas\b"),
    re.compile(r"(?i)\bdiari(?:-api)?\b"),
    re.compile(r"(?i)(?:/home/|/Users/|[A-Za-z]:\\Users\\)"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*[^\s`<>{}]+"),
]
PUBLIC_REPOSITORY_REFERENCE = re.compile(
    r"https://github\.com/[^/\s`<>{}]+/code-cycle-toolkit(?:\.git)?",
    re.IGNORECASE,
)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def parse_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("missing YAML frontmatter")

    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("unterminated YAML frontmatter")

    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line or line[0].isspace() or line.lstrip().startswith("-"):
            continue
        if ":" not in line:
            raise ValueError(f"invalid frontmatter line: {line}")
        key, value = line.split(":", 1)
        key = key.strip()
        if key not in ALLOWED_FRONTMATTER:
            raise ValueError(f"unsupported frontmatter field: {key}")
        values[key] = value.strip().strip("'\"")

    for required in ("name", "description"):
        if not values.get(required):
            raise ValueError(f"missing frontmatter field: {required}")
    return values


def validate_json(path: Path, expected_name: str) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if data.get("name") != expected_name:
        raise ValueError(f"manifest name must be {expected_name!r}")
    if not data.get("version"):
        raise ValueError("manifest is missing version")
    if not data.get("description"):
        raise ValueError("manifest is missing description")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    skills_root = root / "skills"
    errors: list[str] = []

    if not skills_root.is_dir():
        errors.append("skills directory is missing")
    else:
        found: set[str] = set()
        for skill_dir in sorted(path for path in skills_root.iterdir() if path.is_dir()):
            found.add(skill_dir.name)
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                errors.append(f"{skill_dir}: SKILL.md is missing")
                continue
            try:
                frontmatter = parse_frontmatter(skill_file)
                if frontmatter["name"] != skill_dir.name:
                    errors.append(f"{skill_file}: name does not match directory")
                if len(frontmatter["name"]) > 64 or not NAME_RE.fullmatch(frontmatter["name"]):
                    errors.append(f"{skill_file}: invalid skill name")
                if not 1 <= len(frontmatter["description"]) <= 1024:
                    errors.append(f"{skill_file}: description must be 1-1024 characters")
            except ValueError as exc:
                errors.append(f"{skill_file}: {exc}")

        missing = REQUIRED_SKILLS - found
        if missing:
            errors.append(f"missing skills: {', '.join(sorted(missing))}")

    for manifest in (root / ".claude-plugin/plugin.json", root / ".codex-plugin/plugin.json"):
        if not manifest.is_file():
            errors.append(f"missing manifest: {manifest.relative_to(root)}")
            continue
        try:
            validate_json(manifest, "code-cycle-toolkit")
        except ValueError as exc:
            errors.append(f"{manifest.relative_to(root)}: {exc}")

    if not (root / "opencode.jsonc").is_file():
        errors.append("missing opencode.jsonc")

    for path in root.rglob("*"):
        if path.is_file() and path.name.endswith(".Zone.Identifier"):
            errors.append(f"remove metadata sidecar before publishing: {path.relative_to(root)}")
        if path.is_file() and path.suffix.lower() in {".md", ".json", ".jsonc", ".yaml", ".yml", ".toml", ".sh", ".ps1", ".py"}:
            # This validator necessarily contains the patterns it scans for.
            if path.resolve() == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            # The public repository URL is expected release metadata, not a
            # private project reference.
            text = PUBLIC_REPOSITORY_REFERENCE.sub("", text)
            for pattern in PRIVATE_PATTERNS:
                if pattern.search(text):
                    errors.append(f"possible private data in {path.relative_to(root)}: {pattern.pattern}")

    if errors:
        for error in errors:
            fail(error)
        return 1

    print(f"VALID: {len(REQUIRED_SKILLS)} skills and cross-agent manifests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
