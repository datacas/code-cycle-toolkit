#!/usr/bin/env python3
"""Validate the public, cross-agent shape of Code Cycle Toolkit."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALLOWED_FRONTMATTER = {"name", "description", "license", "compatibility", "metadata"}

CYCLE_SKILLS = {
    "cc-implement-issue",
    "cc-initial-review",
    "cc-resolve-comments",
    "cc-rereview",
    "cc-orchestrator",
    "cc-orca-orchestrator",
}
SUPPORT_SKILLS = {
    "cc-pr-review",
    "cc-code-review",
    "cc-security-review",
    "cc-verify",
    "cc-run",
}
REQUIRED_SKILLS = CYCLE_SKILLS | SUPPORT_SKILLS

# The three review-cycle skills carry byte-identical copies of the sections that
# define the state-recovery contract. Skills are installed as independent
# directories, so the duplication is deliberate; this check catches the drift it
# would otherwise hide.
REVIEW_CYCLE_SKILLS = ("cc-initial-review", "cc-rereview", "cc-resolve-comments")
SHARED_REVIEW_SECTIONS = (
    "### The `ORCHESTRATION_RESULT` block is opt-in",
    "### Where the block goes",
    "### The PR comment is the machine-readable record",
)

# Every skill repeats these two sections verbatim, for the same reason.
SHARED_ALL_SECTIONS = ("## Repository conventions",)
# cc-run publishes no GitHub artefact, so it states the language rule in its own
# terms instead of the shared one.
SHARED_PUBLISHING_SECTIONS = ("## Output language",)
LANGUAGE_EXEMPT = {"cc-run"}

PRIVATE_PATTERNS = [
    re.compile(r"(?i)\bdatacas\b"),
    re.compile(r"(?i)\bdiari(?:-api)?\b"),
    re.compile(r"(?i)(?:/home/|/Users/|[A-Za-z]:\\Users\\)"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*[^\s`<>{}]+"),
]
PUBLIC_REPOSITORY_REFERENCE = re.compile(
    r"(?:https://github\.com/)?[^/\s`<>{}]+/code-cycle-toolkit(?:\.git)?",
    re.IGNORECASE,
)
SCANNED_SUFFIXES = {".md", ".json", ".jsonc", ".yaml", ".yml", ".toml", ".sh", ".ps1", ".py"}

# Known fixed-language output from earlier versions. Static validation cannot
# identify every natural language, but it can prevent these regressions.
FIXED_LANGUAGE_MARKERS = re.compile(
    r"(?i)\b(?:auditor[ií]a|verificaci[oó]n|resuelto|hallazgo|"
    r"entorno no disponible|cambio no sensible|no verificado por ejecuci[oó]n)\b"
)
LITERAL_OUTPUT_DIRECTIVE = re.compile(
    r"(?i)\busing this (?:sentence|exact wording)\b"
)
# The one deliberate exception: the opt-in trigger lists quote Spanish requests a
# user may type, so the skill recognises them.
SPANISH_TRIGGER_CONTEXT = re.compile(
    r'"con ORCHESTRATION_RESULT".{0,200}?"add the JSON block"', re.DOTALL
)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments that live outside string literals."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


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


def extract_section(text: str, heading: str) -> str | None:
    """Return one Markdown section, from its heading to the next same-or-higher one."""
    start = text.find(heading)
    if start == -1:
        return None
    level = len(heading) - len(heading.lstrip("#"))
    cursor = start + len(heading)
    while True:
        nxt = text.find("\n#", cursor)
        if nxt == -1:
            return text[start:]
        candidate = text[nxt + 1 :]
        hashes = len(candidate) - len(candidate.lstrip("#"))
        if hashes <= level:
            return text[start : nxt + 1]
        cursor = nxt + 1


def validate_manifest(path: Path, expected_name: str) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if data.get("name") != expected_name:
        raise ValueError(f"manifest name must be {expected_name!r}")
    version = data.get("version")
    if not version:
        raise ValueError("manifest is missing version")
    if not data.get("description"):
        raise ValueError("manifest is missing description")
    return str(version)


def check_shared_sections(
    root: Path, skills: tuple[str, ...] | list[str], headings: tuple[str, ...], errors: list[str]
) -> None:
    for heading in headings:
        seen: dict[str, str] = {}
        for skill in skills:
            path = root / "skills" / skill / "SKILL.md"
            if not path.is_file():
                continue
            section = extract_section(path.read_text(encoding="utf-8"), heading)
            if section is None:
                errors.append(f"skills/{skill}/SKILL.md: missing shared section {heading!r}")
                continue
            seen[skill] = section.strip()
        if len(set(seen.values())) > 1:
            reference = skills[0]
            drifted = [s for s, body in seen.items() if body != seen.get(reference)]
            errors.append(
                f"shared section {heading!r} has drifted between skills: "
                f"{', '.join(sorted(drifted))} differ from {reference}"
            )


def validate_package(root: Path) -> list[str]:
    root = root.resolve()
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

            body = SPANISH_TRIGGER_CONTEXT.sub("", skill_file.read_text(encoding="utf-8"))
            match = FIXED_LANGUAGE_MARKERS.search(body)
            if match:
                errors.append(
                    f"skills/{skill_dir.name}/SKILL.md: known fixed-language output "
                    f"({match.group(0)!r}); published text must follow the target repository"
                )
            directive = LITERAL_OUTPUT_DIRECTIVE.search(body)
            if directive:
                errors.append(
                    f"skills/{skill_dir.name}/SKILL.md: literal output directive "
                    f"({directive.group(0)!r}); require natural wording in the selected language"
                )

        missing = REQUIRED_SKILLS - found
        if missing:
            errors.append(f"missing skills: {', '.join(sorted(missing))}")
        unexpected = found - REQUIRED_SKILLS
        if unexpected:
            errors.append(
                f"skills not declared in the validator: {', '.join(sorted(unexpected))}"
            )

        check_shared_sections(root, sorted(REQUIRED_SKILLS), SHARED_ALL_SECTIONS, errors)
        check_shared_sections(
            root, sorted(REQUIRED_SKILLS - LANGUAGE_EXEMPT), SHARED_PUBLISHING_SECTIONS, errors
        )
        check_shared_sections(root, REVIEW_CYCLE_SKILLS, SHARED_REVIEW_SECTIONS, errors)

    versions: dict[str, str] = {}
    for manifest in (root / ".claude-plugin/plugin.json", root / ".codex-plugin/plugin.json"):
        if not manifest.is_file():
            errors.append(f"missing manifest: {manifest.relative_to(root)}")
            continue
        try:
            versions[str(manifest.relative_to(root))] = validate_manifest(
                manifest, "code-cycle-toolkit"
            )
        except ValueError as exc:
            errors.append(f"{manifest.relative_to(root)}: {exc}")
    if len(set(versions.values())) > 1:
        errors.append(
            "manifest versions disagree: "
            + ", ".join(f"{name}={version}" for name, version in sorted(versions.items()))
        )

    opencode = root / "opencode.jsonc"
    if not opencode.is_file():
        errors.append("missing opencode.jsonc")
    else:
        try:
            json.loads(strip_jsonc(opencode.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            errors.append(f"opencode.jsonc: invalid JSONC: {exc}")

    readme = root / "README.md"
    if not readme.is_file():
        errors.append("missing README.md")
    else:
        readme_text = readme.read_text(encoding="utf-8")
        undocumented = sorted(skill for skill in REQUIRED_SKILLS if f"`{skill}`" not in readme_text)
        if undocumented:
            errors.append(f"skills missing from README.md: {', '.join(undocumented)}")

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(".Zone.Identifier"):
            errors.append(f"remove metadata sidecar before publishing: {path.relative_to(root)}")
            continue
        if path.suffix.lower() not in SCANNED_SUFFIXES:
            continue
        # This validator necessarily contains the patterns it scans for.
        if path.resolve() == (root / "scripts" / "validate-package.py").resolve():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # The public repository URL is expected release metadata, not a private
        # project reference.
        text = PUBLIC_REPOSITORY_REFERENCE.sub("", text)
        for pattern in PRIVATE_PATTERNS:
            if pattern.search(text):
                errors.append(f"possible private data in {path.relative_to(root)}: {pattern.pattern}")

    return errors


def main() -> int:
    errors = validate_package(Path(__file__).resolve().parent.parent)
    if errors:
        for error in errors:
            fail(error)
        return 1

    print(
        f"VALID: {len(REQUIRED_SKILLS)} skills "
        f"({len(CYCLE_SKILLS)} cycle, {len(SUPPORT_SKILLS)} supporting), "
        "shared sections in sync, cross-agent manifests aligned"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
