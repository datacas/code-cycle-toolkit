#!/usr/bin/env python3
"""Validate the public, cross-agent shape of Code Cycle Toolkit."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_contract  # noqa: E402


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
    "cc-provider-bootstrap",
    "cc-pr-review",
    "cc-code-review",
    "cc-security-review",
    "cc-verify",
    "cc-run",
    "cc-stats",
    "cc-profile-config",
}
REQUIRED_SKILLS = CYCLE_SKILLS | SUPPORT_SKILLS
CLAUDE_CODEX_REFERENCE = Path(
    "skills/cc-orchestrator/references/codex-plugin-cc.md"
)

# The three review-cycle skills carry byte-identical copies of the sections that
# define the state-recovery contract. Skills are installed as independent
# directories, so the duplication is deliberate; this check catches the drift it
# would otherwise hide.
REVIEW_CYCLE_SKILLS = ("cc-initial-review", "cc-rereview", "cc-resolve-comments")
SHARED_REVIEW_SECTIONS = (
    "### The `ORCHESTRATION_RESULT` block is opt-in",
    "### Where the block goes",
    "### The change-request comment is the machine-readable record",
)

# The two skills that push a head wait for its checks by the same rule.
HEAD_PUSHING_SKILLS = ("cc-implement-issue", "cc-resolve-comments")
SHARED_HEAD_SECTIONS = ("## Checks of the pushed head",)

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


def redact_public_repository_owner_trust(text: str) -> str:
    """Hide only a public repository owner repeated as a trusted author."""
    owner_match = re.search(
        r"(?m)^\s*selector:\s*([^/\s`<>{}]+)/code-cycle-toolkit(?:\.git)?\s*$",
        text,
    )
    if owner_match is None:
        return text
    owner = owner_match.group(1)
    lines = text.splitlines(keepends=True)
    trusted_indent: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped == "trusted_authors:":
            trusted_indent = indent
            continue
        if trusted_indent is None:
            continue
        if stripped and indent <= trusted_indent:
            trusted_indent = None
            continue
        if stripped == f"- {owner}":
            lines[index] = f"{' ' * indent}- <public repository owner>\n"
    return "".join(lines)

# Windows alternate data streams surface in WSL and on copied trees as a sibling
# file, usually `<name>.md:Zone.Identifier`. The colon means it carries neither a
# recognised suffix nor a `.Zone.Identifier` ending, so it slipped past both the
# suffix filter and the earlier exact-suffix check. Match the marker wherever it
# appears in the name.
ZONE_IDENTIFIER_RE = re.compile(r"(?i)[.:]Zone\.Identifier$")

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


RECORD_SECTION = "### The change-request comment is the machine-readable record"
FENCE_RE = re.compile(r"```text\n(.*?)\n```", re.DOTALL)


def check_record_contract(root: Path, errors: list[str]) -> None:
    """Keep the published contract and its reference parser from drifting apart.

    The shared section states the comment contract in prose; `review_contract`
    is what reads it back. Every example the section prints must parse, or the
    skills are promising a shape nothing can recover.
    """
    path = root / "skills" / "cc-initial-review" / "SKILL.md"
    if not path.is_file():
        return
    section = extract_section(path.read_text(encoding="utf-8"), RECORD_SECTION)
    if section is None:
        errors.append(f"missing shared section {RECORD_SECTION!r}")
        return

    examples = [block.strip() for block in FENCE_RE.findall(section)]
    if not examples:
        errors.append(f"{RECORD_SECTION!r} prints no contract example to check")
        return

    seen: set[str] = set()
    for example in examples:
        try:
            run = review_contract.parse_run_line(example)
            finding = review_contract.parse_finding_header(example)
        except review_contract.ContractError as exc:
            errors.append(f"contract example does not parse: {example!r}: {exc}")
            continue
        if run is not None:
            seen.add(run.kind)
        elif finding is not None:
            seen.add("finding")
        else:
            errors.append(f"contract example matches no known line kind: {example!r}")

    for kind in ("review", "triage", "finding"):
        if kind not in seen:
            errors.append(
                f"{RECORD_SECTION!r} no longer documents the {kind} line kind"
            )


IMPLEMENT_SKILL = "cc-implement-issue"
DIAGNOSIS_SECTION = "## Diagnose before editing"
RESULT_SECTION = "## Structured result"
RESULT_BLOCK_RE = re.compile(
    r"ORCHESTRATION_RESULT\n(.*?)\nEND_ORCHESTRATION_RESULT", re.DOTALL
)
DIAGNOSIS_CLASSIFICATIONS = (
    "isolated_defect", "shared_cause", "duplicate", "superseded",
    "already_resolved", "feature_request", "cause_mismatch", "not_reproduced",
)
DIAGNOSIS_DECISIONS = (
    "implement", "implement_root_fix", "do_not_implement_in_isolation",
    "stop_duplicate", "needs_scope_decision", "needs_evidence",
)
DIAGNOSIS_KEYS = {
    "classification", "decision", "related_search", "reproduced",
    "cause_matches_issue", "related_items",
}
WORK_UNIT_KEYS = {"id", "commit_sha", "rollback"}
WORK_UNIT_ROLLBACKS = ("independent", "dependent", "irreversible")
WORK_UNIT_ID_RE = re.compile(r"WU-[1-9][0-9]*")
COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}")


def diagnosis_errors(diagnosis: object) -> list[str]:
    """What is wrong with one `diagnosis` object, as the skill defines it."""
    if not isinstance(diagnosis, dict):
        return ["`diagnosis` is not an object"]
    problems: list[str] = []
    if set(diagnosis) != DIAGNOSIS_KEYS:
        problems.append(
            f"`diagnosis` keys {sorted(diagnosis)} differ from {sorted(DIAGNOSIS_KEYS)}"
        )
    if diagnosis.get("classification") not in DIAGNOSIS_CLASSIFICATIONS:
        problems.append("`diagnosis.classification` is not a known token")
    if diagnosis.get("decision") not in DIAGNOSIS_DECISIONS:
        problems.append("`diagnosis.decision` is not a known token")
    if diagnosis.get("related_search") not in ("basic", "widened"):
        problems.append("`diagnosis.related_search` must be basic or widened")
    for key in ("reproduced", "cause_matches_issue"):
        # Compared by type: `1 in (True, False, None)` is true in Python.
        if key in diagnosis and not (
            diagnosis[key] is None or isinstance(diagnosis[key], bool)
        ):
            problems.append(f"`diagnosis.{key}` must be true, false, or null")
    items = diagnosis.get("related_items")
    if not isinstance(items, list) or not all(
        isinstance(item, str) and item for item in items
    ):
        problems.append("`diagnosis.related_items` must be a list of identifiers")
    if diagnosis.get("classification") == "not_reproduced" and (
        diagnosis.get("reproduced") is not False
        or diagnosis.get("cause_matches_issue") is not None
    ):
        problems.append(
            "`not_reproduced` requires `reproduced: false` and `cause_matches_issue: null`"
        )
    return problems


def work_units_errors(work_units: object) -> list[str]:
    """Validate the token-only work-unit result contract."""
    if not isinstance(work_units, list) or not work_units:
        return ["`work_units` must be a non-empty list"]

    problems: list[str] = []
    seen_ids: set[str] = set()
    for index, unit in enumerate(work_units):
        where = f"`work_units[{index}]`"
        if not isinstance(unit, dict):
            problems.append(f"{where} must be an object")
            continue
        if set(unit) != WORK_UNIT_KEYS:
            problems.append(
                f"{where} keys {sorted(unit)} differ from {sorted(WORK_UNIT_KEYS)}"
            )

        unit_id = unit.get("id")
        if not isinstance(unit_id, str) or not WORK_UNIT_ID_RE.fullmatch(unit_id):
            problems.append(f"`work_units[{index}].id` must be a WU-N token")
        elif unit_id in seen_ids:
            problems.append(f"`work_units[{index}].id` duplicates `{unit_id}`")
        else:
            seen_ids.add(unit_id)

        commit_sha = unit.get("commit_sha")
        if commit_sha is not None and (
            not isinstance(commit_sha, str)
            or not COMMIT_SHA_RE.fullmatch(commit_sha)
        ):
            problems.append(f"`work_units[{index}].commit_sha` must be a full SHA or null")

        if unit.get("rollback") not in WORK_UNIT_ROLLBACKS:
            problems.append(f"`work_units[{index}].rollback` is not a known token")

    return problems


def check_implement_contract(root: Path, errors: list[str]) -> None:
    """Keep the implement stage's diagnosis, work units, and result example consistent.

    The diagnosis section defines the tokens; the result example is what a
    consumer copies. Both must name the same closed vocabulary, and the example
    must be strict JSON, or the skill promises a shape nothing can read.
    """
    path = root / "skills" / IMPLEMENT_SKILL / "SKILL.md"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    where = f"skills/{IMPLEMENT_SKILL}/SKILL.md"

    diagnosis = extract_section(text, DIAGNOSIS_SECTION)
    if diagnosis is None:
        errors.append(f"{where}: missing section {DIAGNOSIS_SECTION!r}")
    else:
        for token in DIAGNOSIS_CLASSIFICATIONS:
            if f"`{token}`" not in diagnosis:
                errors.append(f"{where}: {DIAGNOSIS_SECTION!r} does not define `{token}`")
        # A decision is defined by its row in the decision table; a mention in
        # the prose around it does not say when it applies or what happens.
        for token in DIAGNOSIS_DECISIONS:
            if f"\n| `{token}` |" not in diagnosis:
                errors.append(f"{where}: {DIAGNOSIS_SECTION!r} does not define `{token}`")
        for depth in ("**Basic, always.**", "**Widened, when any signal is present.**"):
            if depth not in diagnosis:
                errors.append(f"{where}: {DIAGNOSIS_SECTION!r} lost the search depth {depth}")

    section = extract_section(text, RESULT_SECTION)
    blocks = RESULT_BLOCK_RE.findall(section or "")
    if not blocks:
        errors.append(f"{where}: {RESULT_SECTION!r} prints no ORCHESTRATION_RESULT example")
        return
    try:
        example = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        errors.append(f"{where}: result example is not strict JSON: {exc}")
        return
    shown = example.get("diagnosis")
    if not isinstance(shown, dict):
        errors.append(f"{where}: result example has no `diagnosis` object")
        return
    errors.extend(f"{where}: {problem}" for problem in diagnosis_errors(shown))
    errors.extend(
        f"{where}: {problem}"
        for problem in work_units_errors(example.get("work_units"))
    )


ORCHESTRATOR_SKILLS = ("cc-orchestrator", "cc-orca-orchestrator")
LADDER_SECTION = "### Repeated-findings ladder"
#: What each orchestrator's ladder must state, in the words that define it:
#: the claim, the rejection, both rungs, and where the definition lives.
LADDER_PHRASES = (
    "survives a claimed fix",
    "`resolved` or `not_applicable`",
    "`open` or `still_open`",
    "never the disposition",
    "**First survival**",
    "**Second survival**",
    "`HUMAN_INTERVENTION` before",
    "`PARTIALLY_RESOLVED`",
    "`review_contract.claimed_fix_survivals`",
)


def check_repeated_findings_ladder(root: Path, errors: list[str]) -> None:
    """Both orchestrators must state the same ladder the runtime applies.

    The definition lives in `review_contract.claimed_fix_survivals`; the
    orchestrators apply it in prose. A section that lost a rung or the
    definition of a claim would let the two drift without anything failing.
    """
    for skill in ORCHESTRATOR_SKILLS:
        path = root / "skills" / skill / "SKILL.md"
        if not path.is_file():
            continue
        where = f"skills/{skill}/SKILL.md"
        section = extract_section(path.read_text(encoding="utf-8"), LADDER_SECTION)
        if section is None:
            errors.append(f"{where}: missing section {LADDER_SECTION!r}")
            continue
        flattened = " ".join(section.split())
        for phrase in LADDER_PHRASES:
            if phrase not in flattened:
                errors.append(f"{where}: {LADDER_SECTION!r} does not state {phrase!r}")


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
        check_shared_sections(root, HEAD_PUSHING_SKILLS, SHARED_HEAD_SECTIONS, errors)
        check_record_contract(root, errors)
        check_implement_contract(root, errors)
        check_repeated_findings_ladder(root, errors)

        adapter_reference = root / CLAUDE_CODEX_REFERENCE
        orchestrator_path = root / "skills" / "cc-orchestrator" / "SKILL.md"
        if not adapter_reference.is_file():
            errors.append(f"missing orchestrator reference: {CLAUDE_CODEX_REFERENCE}")
        elif orchestrator_path.is_file():
            orchestrator_text = orchestrator_path.read_text(encoding="utf-8")
            relative_link = "references/codex-plugin-cc.md"
            if relative_link not in orchestrator_text:
                errors.append(
                    "cc-orchestrator does not route Claude-to-Codex mode to "
                    f"{relative_link}"
                )

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
        adapter_doc_link = CLAUDE_CODEX_REFERENCE.as_posix()
        if adapter_doc_link not in readme_text:
            errors.append(
                "README.md does not link the Claude-to-Codex adapter contract: "
                f"{adapter_doc_link}"
            )

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if ZONE_IDENTIFIER_RE.search(path.name):
            errors.append(f"remove metadata sidecar before publishing: {path.relative_to(root)}")
            continue
        if path.suffix.lower() not in SCANNED_SUFFIXES:
            continue
        # This validator necessarily contains the patterns it scans for.
        if path.resolve() == (root / "scripts" / "validate-package.py").resolve():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.name == ".code-cycle.yml":
            text = redact_public_repository_owner_trust(text)
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
