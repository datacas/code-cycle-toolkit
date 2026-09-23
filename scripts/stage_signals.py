"""What a stage's router could have known, observed before it chooses.

`router.TaskSignals` holds what a person declared about the work: how hard it
looks, how it can be verified, whether it is security sensitive. Those are
judgements. This module holds the other kind — facts the cycle can observe
before a stage is routed — and keeps the two apart, because a count read off a
diff and a difficulty somebody typed are not the same evidence and must never be
queried as if they were.

Three rules shape it.

**Before routing, never after.** A signal describes what was knowable when the
stage was routed. A diff does not exist before the first implementation, so an
`implement` stage carries no change signals at all; findings are the ones a
previous stage reported, never the ones the current stage will find.

**Unknown is not zero.** A value that could not be determined is `None` and is
omitted from the row. Zero means it was observed and was zero. A diff that could
not be read leaves every change signal out; an empty diff that could be read is
a diff of zero files.

**Only closed categories leave this module.** Paths are classified in memory
and discarded: what reaches telemetry is a count per language from a fixed
list, and a flag per area. No path, file name, diff line or free text is
returned, so a file name cannot become persistent text.

The path classifiers are heuristics. `touches_auth` means a changed path looks
like authentication code, not that the change affects authentication, and a
path that looks harmless proves nothing about the code in it.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, fields
from pathlib import PurePosixPath

#: Languages counted by changed file, in a fixed order. A file in none of them
#: counts under `other`. Adding one is a telemetry schema change: its field has
#: to be added to `telemetry.FIELD_SPECS` deliberately.
LANGUAGES: dict[str, frozenset[str]] = {
    "python": frozenset({".py", ".pyi"}),
    "javascript": frozenset({".js", ".jsx", ".mjs", ".cjs"}),
    "typescript": frozenset({".ts", ".tsx", ".mts", ".cts"}),
    "go": frozenset({".go"}),
    "rust": frozenset({".rs"}),
    "java": frozenset({".java", ".kt", ".kts"}),
    "csharp": frozenset({".cs"}),
    "ruby": frozenset({".rb"}),
    "php": frozenset({".php"}),
    "shell": frozenset({".sh", ".bash", ".ps1"}),
    "sql": frozenset({".sql"}),
    "markdown": frozenset({".md", ".mdx", ".rst"}),
}
LANGUAGE_FIELDS: tuple[str, ...] = tuple(
    f"changed_{name}_files" for name in (*LANGUAGES, "other")
)

#: Roles routed after a change exists. An `implement` stage is routed before
#: there is anything to observe, so it gets no change signals.
CHANGE_OBSERVED_ROLES = frozenset({
    "review", "rereview", "resolve", "security", "verify", "run",
})

#: Roles for which `resolution_round` means something.
RESOLUTION_ROLES = frozenset({"resolve", "rereview"})

_TEST_SEGMENTS = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing"})
_TEST_NAME = re.compile(
    r"(^test_.*\.py$|_test\.(py|go|rb)$|\.(test|spec)\.[cm]?[jt]sx?$"
    r"|Tests?\.(java|kt|cs)$|_spec\.rb$|Test\.php$)"
)
_DEPENDENCY_NAMES = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg", "pipfile", "pipfile.lock",
    "poetry.lock", "uv.lock", "package.json", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "go.mod", "go.sum", "cargo.toml", "cargo.lock", "gemfile",
    "gemfile.lock", "composer.json", "composer.lock", "pom.xml", "build.gradle",
    "build.gradle.kts", "packages.config", "directory.packages.props",
})
_DEPENDENCY_PATTERN = re.compile(r"^requirements.*\.(txt|in)$|\.csproj$")
_MIGRATION_SEGMENTS = frozenset({"migrations", "migration", "migrate", "alembic"})
_DATABASE_SEGMENTS = frozenset({"db", "database", "databases"})
_DATABASE_SUFFIXES = frozenset({".sql", ".prisma"})
#: Matched against whole words of a path, so `processor` is not `sso` and
#: `author` is not `auth`.
_AUTH_WORD = re.compile(
    r"^(auth(?!or)\w*|oauth\w*|login|logout|signin|signup|sso|sessions?"
    r"|permissions?|passwords?|passwd|credentials?|jwt|rbac|acls?)$"
)
_API_SEGMENTS = frozenset({
    "api", "apis", "routes", "controllers", "endpoints", "handlers", "graphql",
})
_API_PATTERN = re.compile(r"^(openapi|swagger)[^/]*\.(ya?ml|json)$|\.proto$")
_CI_PREFIXES = (".github/workflows/", ".circleci/", ".buildkite/", ".gitlab/ci/")
_CI_NAMES = frozenset({
    ".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml", ".travis.yml",
    "bitbucket-pipelines.yml",
})

#: An added line that defines a test, across the frameworks the classifiers
#: above recognise. A count of these is an estimate, and is named as one.
_TEST_DEFINITION = re.compile(
    r"^\+\s*(async\s+)?(def\s+test\w*\s*\(|func\s+Test\w*\s*\(|#\[test\]"
    r"|@Test\b|\[(Test|Fact|Theory)\]|(it|test)\s*\(\s*['\"`]"
    r"|public\s+function\s+test\w*\s*\()"
)


@dataclass(frozen=True)
class ChangeSignals:
    """Observed facts about a diff. `None` means not known, never zero.

    `changed_lines_estimate` and `test_count_estimate` are estimates by name:
    binary files have no line count, and a test definition is recognised by a
    pattern, not by running anything.
    """

    changed_files_count: int | None = None
    changed_lines_estimate: int | None = None
    has_tests: bool | None = None
    test_count_estimate: int | None = None
    touches_dependencies: bool | None = None
    touches_database: bool | None = None
    touches_auth: bool | None = None
    touches_api: bool | None = None
    touches_migrations: bool | None = None
    touches_ci: bool | None = None
    languages: tuple[tuple[str, int], ...] | None = None

    def telemetry_fields(self) -> dict:
        """The telemetry fields, with unknown ones left out rather than zeroed."""
        out = {}
        for spec in fields(self):
            if spec.name == "languages":
                continue
            value = getattr(self, spec.name)
            if value is not None:
                out[spec.name] = value
        if self.languages is not None:
            out.update(dict(self.languages))
        return out


def is_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (any(part.lower() in _TEST_SEGMENTS for part in parts[:-1])
            or bool(_TEST_NAME.search(parts[-1] if parts else "")))


def language_of(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    for name, suffixes in LANGUAGES.items():
        if suffix in suffixes:
            return name
    return "other"


def _areas(path: str) -> set[str]:
    lowered = path.lower()
    posix = PurePosixPath(lowered)
    name = posix.name
    directories = set(posix.parts[:-1])
    areas = set()
    if name in _DEPENDENCY_NAMES or _DEPENDENCY_PATTERN.search(name):
        areas.add("dependencies")
    if directories & _MIGRATION_SEGMENTS or "db/migrate/" in lowered:
        areas.add("migrations")
    if ("migrations" in areas or directories & _DATABASE_SEGMENTS
            or posix.suffix in _DATABASE_SUFFIXES):
        areas.add("database")
    if any(_AUTH_WORD.match(word) for word in re.split(r"[^a-z0-9]+", lowered)):
        areas.add("auth")
    if directories & _API_SEGMENTS or _API_PATTERN.search(name):
        areas.add("api")
    if lowered.startswith(_CI_PREFIXES) or name in _CI_NAMES:
        areas.add("ci")
    return areas


def parse_numstat(output: str) -> list[tuple[str, int | None]]:
    """Paths and line counts from `git diff --numstat -z --no-renames`.

    A binary file reports `-` and gets `None`: it has no lines to count.
    """
    changes = []
    for record in output.split("\0"):
        if not record.strip():
            continue
        added, deleted, path = record.lstrip("\n").split("\t", 2)
        lines = None if "-" in (added, deleted) else int(added) + int(deleted)
        changes.append((path, lines))
    return changes


def count_test_definitions(unified_diff: str) -> int:
    """Added test definitions in test files, from `git diff -U0` output."""
    count = 0
    in_test_file = False
    for line in unified_diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            in_test_file = target.startswith("b/") and is_test_path(target[2:])
            continue
        if in_test_file and _TEST_DEFINITION.match(line):
            count += 1
    return count


def derive_change_signals(numstat: str, unified_diff: str | None = None) -> ChangeSignals:
    """Classify a diff into closed categories. Deterministic for a given input."""
    changes = parse_numstat(numstat)
    paths = [path for path, _ in changes]
    areas: set[str] = set()
    for path in paths:
        areas |= _areas(path)
    languages = {name: 0 for name in LANGUAGE_FIELDS}
    for path in paths:
        languages[f"changed_{language_of(path)}_files"] += 1
    has_tests = any(is_test_path(path) for path in paths)
    return ChangeSignals(
        changed_files_count=len(changes),
        changed_lines_estimate=sum(lines or 0 for _, lines in changes),
        has_tests=has_tests,
        test_count_estimate=(
            count_test_definitions(unified_diff) if unified_diff is not None else None
        ),
        touches_dependencies="dependencies" in areas,
        touches_database="database" in areas,
        touches_auth="auth" in areas,
        touches_api="api" in areas,
        touches_migrations="migrations" in areas,
        touches_ci="ci" in areas,
        languages=tuple(languages.items()),
    )


def collect_change_signals(cwd: str | None, bases: tuple[str, ...],
                           *, timeout: int = 30) -> ChangeSignals | None:
    """Read the change against the first base that resolves, or nothing.

    Returns `None` when there is no readable diff — not a Git worktree, no base
    that resolves, Git missing — so the caller omits every change signal
    instead of recording an empty change it never observed. The diff text is
    read into memory, classified and dropped here.
    """
    for base in bases:
        spec = f"{base}...HEAD"
        numstat = _git(cwd, ["diff", "--numstat", "-z", "--no-renames", spec], timeout)
        if numstat is None:
            continue
        unified = _git(cwd, ["diff", "-U0", "--no-color", "--no-renames", spec], timeout)
        return derive_change_signals(numstat, unified)
    return None


def _git(cwd: str | None, args: list[str], timeout: int) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None
