#!/usr/bin/env python3
"""Decide whether a change needs the full package validation.

`Validate package` runs two required checks on every push and pull request. A
change that only touches an explicit allowlist of paths cannot affect what those
checks validate, so each job classifies the change first and, for such a
change, runs only the privacy and metadata-sidecar scan plus the tests that read
an allowlisted file. Anything else, and any failure to classify, runs in full.

Subcommands:

  classify   read the event and the two commits from the environment and write
             `mode` (`fast` or `full`) and `tests` to $GITHUB_OUTPUT
  run-fast   run what the fast path still runs, for one job
  summary    record that a job ran in full
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import subprocess
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
TESTS_DIR = ROOT / "tests"
MAPPING_PATH = SCRIPTS_DIR / "fast-path-tests.json"

FAST = "fast"
FULL = "full"
SKIPPED_NON_CODE = "SKIPPED_NON_CODE"
TESTS_PASSED = "TESTS_PASSED"

# The explicit allowlist: the only paths a change may touch and still take the
# fast path. Everything else runs in full.
ALLOWED_FILES = frozenset({".code-cycle.yml"})
ALLOWED_PREFIXES = ("docs/", ".github/ISSUE_TEMPLATE/")
# Markdown at the repository root is allowlisted except the README, which the
# validator reads. Compared case-folded: a case-insensitive checkout would
# otherwise treat `readme.md` as the file the validator reads.
ROOT_MARKDOWN_EXCLUDED = frozenset({"readme.md"})
# Never skippable, whatever the allowlist says: a version bump, the release and
# validation workflows, and this classifier with its test mapping.
ALWAYS_FULL = frozenset({
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".github/workflows/release.yml",
    ".github/workflows/validate.yml",
    "scripts/fast_path.py",
    "scripts/fast-path-tests.json",
})
CHANGELOG = "CHANGELOG.md"
# `release.yml` cuts a release from the CHANGELOG's `## [x.y.z]` headings.
RELEASE_HEADING_CHANGE = re.compile(r"^[+-]##\s", re.MULTILINE)

# A mapping entry names a test module, optionally narrowed to a class or a test
# method: `test_x`, `test_x.Class`, `test_x.Class.test_y`.
MODULE_RE = re.compile(r"^test_[a-z0-9_]+(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LISTED_PATHS = 20

# A name tests use for a path that the guard cannot read from a literal.
PATH_ALIASES = {"CONFIG_NAME": ".code-cycle.yml"}

JOB_LABELS = {
    "validate": "Skills, manifests, and Bash installer",
    "powershell-installer": "PowerShell installer",
    "windows-shard": "Windows test shard",
}
FULL_VALIDATIONS = {
    "validate": (
        "package validator",
        "unit test suite",
        "skills CLI discovery",
        "manifest JSON validation",
        "Bash installer syntax and lint",
        "Bash installer scenarios",
    ),
    "powershell-installer": (
        "package validator (Windows Python)",
        "PowerShell installer scenarios",
    ),
    "windows-shard": ("unit tests of this shard",),
}
SKIPPED_VALIDATIONS = {
    "validate": (
        "package validator checks other than the scan",
        "unit tests not mapped to the change",
        "skills CLI discovery",
        "manifest JSON validation",
        "Bash installer syntax and lint",
        "Bash installer scenarios",
    ),
    "powershell-installer": (
        "package validator checks other than the scan",
        "PowerShell installer scenarios",
    ),
    "windows-shard": ("unit tests of this shard not mapped to the change",),
}


@dataclass(frozen=True)
class Classification:
    mode: str
    reason: str
    paths: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()


def is_allowlisted(path: str) -> bool:
    segments = path.split("/")
    if (
        not path
        or "\\" in path
        or any(segment in ("", ".", "..") for segment in segments)
    ):
        return False
    if path in ALWAYS_FULL:
        return False
    if path in ALLOWED_FILES or path.startswith(ALLOWED_PREFIXES):
        return True
    return (
        len(segments) == 1
        and path.endswith(".md")
        and path.casefold() not in ROOT_MARKDOWN_EXCLUDED
    )


def pattern_matches(pattern: str, path: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return path == pattern


def load_mapping(path: Path | None = None) -> dict[str, dict[str, list[str]]]:
    """Read the test mapping, or raise `ValueError` for anything malformed."""
    path = path or MAPPING_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, dict) or not groups:
        raise ValueError(f"{path.name}: expected a non-empty 'groups' object")
    for name, group in groups.items():
        if not isinstance(group, dict) or set(group) != {"paths", "tests"}:
            raise ValueError(f"{path.name}: group {name!r} needs 'paths' and 'tests'")
        for key in ("paths", "tests"):
            values = group[key]
            if (
                not isinstance(values, list)
                or not values
                or not all(isinstance(value, str) for value in values)
            ):
                raise ValueError(f"{path.name}: group {name!r} '{key}' must be a non-empty list of strings")
        for pattern in group["paths"]:
            probe = pattern[:-2] + "x" if pattern.endswith("/**") else pattern
            if not is_allowlisted(probe):
                raise ValueError(f"{path.name}: group {name!r} path {pattern!r} is not allowlisted")
        for module in group["tests"]:
            if not MODULE_RE.fullmatch(module):
                raise ValueError(f"{path.name}: group {name!r} test {module!r} is not a test module name")
    return groups


def mapped_tests(paths, mapping) -> tuple[str, ...]:
    tests: set[str] = set()
    for group in mapping.values():
        if any(pattern_matches(pattern, path) for pattern in group["paths"] for path in paths):
            tests.update(group["tests"])
    return tuple(sorted(tests))


# --- the guard: a static check that every test reading an allowlisted file is
# registered in the mapping -------------------------------------------------

def _mentions(expression: ast.AST, names: set[str]) -> bool:
    return any(
        isinstance(node, ast.Name) and (node.id in names or node.id == "__file__")
        for node in ast.walk(expression)
    )


def _has_division(expression: ast.AST) -> bool:
    return any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        for node in ast.walk(expression)
    )


def _root_names(tree: ast.AST) -> set[str]:
    """Names bound to the repository root, such as `ROOT = Path(__file__)...`."""
    names = {"ROOT"}
    assignments = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and not _has_division(node.value)
    ]
    changed = True
    while changed:
        changed = False
        for node in assignments:
            if not _mentions(node.value, names):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return names


def _flatten_division(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _flatten_division(node.left) + [node.right]
    return [node]


def _path_expressions(tree: ast.AST):
    """Yield (line, scope, operands) for each path built with `/`, `joinpath`,
    `os.path.join`, `glob` or `rglob`. The scope is the enclosing class and the
    outermost enclosing function, or None for each that is absent."""
    inner = {
        id(node.left)
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
    }

    def visit(node: ast.AST, scope: tuple[str | None, str | None]):
        if isinstance(node, ast.ClassDef):
            scope = (node.name, None)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = (scope[0], scope[1] or node.name)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if id(node) not in inner:
                yield node.lineno, scope, _flatten_division(node)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attribute = node.func.attr
            if attribute in ("joinpath", "glob", "rglob"):
                yield node.lineno, scope, _flatten_division(node.func.value) + list(node.args)
            elif attribute == "join" and ast.unparse(node.func.value).endswith("path"):
                yield node.lineno, scope, list(node.args)
        for child in ast.iter_child_nodes(node):
            yield from visit(child, scope)

    yield from visit(tree, (None, None))


def _registrations(module: str, scope: tuple[str | None, str | None]) -> set[str]:
    """The mapping entries that cover a statement in `scope` of `module`: the
    module, its class, or the test method itself. A helper that is not a test
    method is covered only by its class or module."""
    cls, function = scope
    entries = {module}
    is_test = function is not None and function.startswith("test")
    if cls is not None:
        entries.add(f"{module}.{cls}")
        if is_test:
            entries.add(f"{module}.{cls}.{function}")
    elif is_test:
        entries.add(f"{module}.{function}")
    return entries


def _segments(operand: ast.AST) -> list[str]:
    if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
        return [part for part in operand.value.split("/") if part]
    if isinstance(operand, ast.Attribute) and operand.attr in PATH_ALIASES:
        return [PATH_ALIASES[operand.attr]]
    return ["*"]


def _covers(pattern: str, probe: str) -> bool:
    if pattern.endswith("/**"):
        return probe.startswith(pattern[:-2])
    return pattern == probe or fnmatch.fnmatchcase(pattern, probe)


def guard_errors(tests_dir: Path = TESTS_DIR, mapping=None) -> list[str]:
    """Return every unregistered test dependency on an allowlisted path.

    A test module reads an allowlisted file when it builds a path from the
    repository root that is, or may be, allowlisted. The module must then be
    listed in the group covering that path. The check is static: a path built
    some other way is not seen, which is why the mapping is documented as an
    obligation and the unseen case is called out in CONTRIBUTING.md.
    """
    if mapping is None:
        mapping = load_mapping()
    errors: list[str] = []
    for group_name, group in mapping.items():
        for entry in group["tests"]:
            if not (tests_dir / f"{entry.split('.')[0]}.py").is_file():
                errors.append(f"group {group_name!r} lists {entry}, which is not a test module")
    for path in sorted(tests_dir.rglob("test_*.py")):
        module = path.stem
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, ValueError) as exc:
            errors.append(f"{path.name}: could not be read for the fast-path guard: {exc}")
            continue
        names = _root_names(tree)
        for line, scope, operands in _path_expressions(tree):
            if not _mentions(operands[0], names):
                continue
            segments = [segment for operand in operands[1:] for segment in _segments(operand)]
            if not segments:
                continue
            # A file, or a directory that holds files.
            probes = ["/".join(segments), "/".join(segments) + "/x"]
            if not any(is_allowlisted(probe) for probe in probes):
                continue
            covering = [
                group for group in mapping.values()
                if any(_covers(pattern, probe) for pattern in group["paths"] for probe in probes)
            ]
            accepted = _registrations(module, scope)
            if not any(accepted & set(group["tests"]) for group in covering):
                errors.append(
                    f"tests/{path.name}:{line} reads allowlisted path "
                    f"{probes[0]!r} but none of {sorted(accepted)} is registered "
                    f"for it in scripts/{MAPPING_PATH.name}"
                )
    return errors


# --- classification --------------------------------------------------------

def classify_paths(
    paths,
    release_headings_changed: bool = False,
    mapping=None,
    guard=None,
) -> Classification:
    paths = tuple(sorted(set(paths)))
    if not paths:
        return Classification(FULL, "the diff is empty")
    outside = tuple(path for path in paths if not is_allowlisted(path))
    if outside:
        return Classification(FULL, "the change touches paths outside the allowlist", outside)
    if CHANGELOG in paths and release_headings_changed:
        return Classification(FULL, "the change edits a release heading of CHANGELOG.md", (CHANGELOG,))
    try:
        if mapping is None:
            mapping = load_mapping()
        problems = guard_errors(mapping=mapping) if guard is None else guard()
    except (OSError, ValueError) as exc:
        return Classification(FULL, f"the test mapping could not be read: {exc}", paths)
    if problems:
        return Classification(FULL, f"the test-dependency guard failed: {problems[0]}", paths)
    return Classification(
        FAST, "the change only touches allowlisted paths", paths, mapped_tests(paths, mapping)
    )


def _git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode != 0:
        raise ValueError(f"git {arguments[0]} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


def changed_paths(event: str, base: str, head: str, cwd: Path = ROOT) -> tuple[list[str], bool]:
    """The paths a pull request or push changed, and whether the CHANGELOG's
    release headings changed. Raises `ValueError` when it cannot tell."""
    if event == "pull_request":
        span = f"{base}...{head}"
    elif event == "push":
        if set(base) == {"0"}:
            raise ValueError("the push has no previous commit")
        span = f"{base}..{head}"
    else:
        raise ValueError(f"the {event or 'unknown'} event always runs in full")
    if not SHA_RE.fullmatch(base) or not SHA_RE.fullmatch(head):
        raise ValueError("the base and head must be full commit SHAs")
    names = _git("diff", "--name-only", "--no-renames", "-z", span, cwd=cwd)
    paths = [name for name in names.split("\0") if name]
    headings = False
    if CHANGELOG in paths:
        patch = _git("diff", "--unified=0", "--no-renames", span, "--", CHANGELOG, cwd=cwd)
        headings = RELEASE_HEADING_CHANGE.search(patch) is not None
    return paths, headings


def decide(event: str, base: str, head: str, cwd: Path = ROOT) -> Classification:
    """Classify a change, failing closed: any error is a full validation."""
    try:
        paths, headings = changed_paths(event, base, head, cwd)
        return classify_paths(paths, headings)
    except Exception as exc:  # noqa: BLE001 - every failure must fail closed
        return Classification(FULL, f"classification failed: {exc}")


# --- reporting -------------------------------------------------------------

def _inline(text: str) -> str:
    return "".join(ch if ch.isprintable() and ch != "`" else "?" for ch in text)


def _listing(paths) -> list[str]:
    lines = [f"- `{_inline(path)}`" for path in paths[:LISTED_PATHS]]
    if len(paths) > LISTED_PATHS:
        lines.append(f"- … and {len(paths) - LISTED_PATHS} more")
    return lines


def render_classification(result: Classification) -> str:
    if result.mode == FAST:
        lines = ["## Change classification: non-code", "",
                 "Classified as `non_code`: " + result.reason + ".", ""]
        lines += ["Changed paths:", *_listing(result.paths), ""]
        lines.append(
            "Mapped tests: " + (", ".join(f"`{test}`" for test in result.tests) or "none apply")
        )
    else:
        lines = ["## Change classification: full validation", "",
                 "Classified as `full`: " + _inline(result.reason) + ".", ""]
        if result.paths:
            lines += ["Paths that caused it:", *_listing(result.paths)]
    return "\n".join(lines) + "\n"


def write_summary(text: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        print(text)
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def write_outputs(result: Classification) -> None:
    lines = f"mode={result.mode}\ntests={' '.join(result.tests)}\n"
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        print(lines, end="")
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(lines)


def _job_heading(job: str, shard: str | None) -> str:
    label = JOB_LABELS[job]
    return f"{label} ({shard})" if shard else label


def render_outcome(job: str, status: str, ran, skipped, shard: str | None = None) -> str:
    lines = [f"### {_job_heading(job, shard)}: `{status}`", "",
             "Validations run:", *[f"- {item}" for item in ran]]
    lines += ["", "Validations skipped:"]
    lines += [f"- {item}" for item in skipped] or ["- none"]
    return "\n".join(lines) + "\n"


# --- the fast path ---------------------------------------------------------

def _parse_tests(text: str) -> list[str]:
    entries = text.split()
    for entry in entries:
        if not MODULE_RE.fullmatch(entry) or not (TESTS_DIR / f"{entry.split('.')[0]}.py").is_file():
            raise ValueError(f"{entry!r} is not a test")
    return entries


def _run_tests(entries: list[str]) -> bool:
    sys.path.insert(0, str(TESTS_DIR))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for entry in entries:
        suite.addTests(loader.loadTestsFromName(entry))
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


def run_fast(job: str, scan: bool, shard: str | None) -> int:
    tests = _parse_tests(os.environ.get("FAST_PATH_TESTS", ""))
    if shard is not None:
        sys.path.insert(0, str(SCRIPTS_DIR))
        import run_windows_test_shard

        assigned = run_windows_test_shard.load_shards().get(shard)
        if assigned is None:
            raise ValueError(f"unknown Windows test shard {shard!r}")
        tests = [test for test in tests if test.split(".")[0] in assigned]

    ran: list[str] = []
    passed = True
    if scan:
        scanned = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "validate-package.py"), "--scan-only"],
            check=False,
        )
        passed = scanned.returncode == 0
        ran.append("privacy and metadata-sidecar scan")
    if tests:
        passed = _run_tests(tests) and passed
        ran.append("mapped tests: " + ", ".join(tests))
    else:
        ran.append("mapped tests: none apply")

    if passed:
        write_summary(render_outcome(job, SKIPPED_NON_CODE, ran, SKIPPED_VALIDATIONS[job], shard))
    else:
        write_summary(f"### {_job_heading(job, shard)}: `FAILED`\n\nA validation of the fast path failed.\n")
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decide whether a change needs the full package validation."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("classify")
    for name in ("run-fast", "summary"):
        sub = commands.add_parser(name)
        sub.add_argument("--job", required=True, choices=sorted(JOB_LABELS))
        sub.add_argument("--shard")
    commands.choices["run-fast"].add_argument("--scan", action="store_true")
    args = parser.parse_args()

    if args.command == "classify":
        result = decide(
            os.environ.get("GITHUB_EVENT_NAME", ""),
            os.environ.get("FAST_PATH_BASE", ""),
            os.environ.get("FAST_PATH_HEAD", ""),
        )
        write_outputs(result)
        write_summary(render_classification(result))
        return 0
    if args.command == "run-fast":
        return run_fast(args.job, args.scan, args.shard)
    write_summary(render_outcome(args.job, TESTS_PASSED, FULL_VALIDATIONS[args.job], (), args.shard))
    return 0


if __name__ == "__main__":
    sys.exit(main())
