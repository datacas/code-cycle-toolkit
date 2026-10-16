from __future__ import annotations

import argparse
import collections
import importlib
import json
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"
SHARDS_PATH = Path(__file__).with_name("windows-test-shards.json")


def validate_assignments(
    shards: dict[str, list[str]], expected_modules: set[str]
) -> None:
    assigned = [module for modules in shards.values() for module in modules]
    counts = collections.Counter(assigned)
    duplicates = sorted(module for module, count in counts.items() if count > 1)
    missing = sorted(expected_modules - counts.keys())
    unexpected = sorted(counts.keys() - expected_modules)

    problems = []
    if duplicates:
        problems.append(f"assigned to more than one shard: {', '.join(duplicates)}")
    if missing:
        problems.append(f"not assigned to a shard: {', '.join(missing)}")
    if unexpected:
        problems.append(f"not test modules: {', '.join(unexpected)}")
    if not shards or any(not modules for modules in shards.values()):
        problems.append("every shard must have at least one test module")
    if problems:
        raise ValueError("Invalid Windows test shard assignments: " + "; ".join(problems))


def load_shards() -> dict[str, list[str]]:
    data: dict[str, Any] = json.loads(SHARDS_PATH.read_text(encoding="utf-8"))
    shards = data.get("shards")
    if not isinstance(shards, dict) or not all(
        isinstance(name, str)
        and isinstance(modules, list)
        and all(isinstance(module, str) for module in modules)
        for name, modules in shards.items()
    ):
        raise ValueError(f"Invalid shard manifest format: {SHARDS_PATH}")

    expected_modules = {
        path.relative_to(TESTS_DIR).with_suffix("").as_posix().replace("/", ".")
        for path in TESTS_DIR.rglob("test_*.py")
        if path.is_file()
    }
    validate_assignments(shards, expected_modules)
    return shards


def run_shard(shard_name: str, shards: dict[str, list[str]]) -> bool:
    modules = shards.get(shard_name)
    if modules is None:
        raise ValueError(
            f"Unknown Windows test shard {shard_name!r}; expected one of: "
            f"{', '.join(shards)}"
        )

    sys.path.insert(0, str(TESTS_DIR))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for module_name in modules:
        suite.addTests(loader.loadTestsFromModule(importlib.import_module(module_name)))

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("shard", help="name of the Windows test shard to run")
    args = parser.parse_args()

    try:
        shards = load_shards()
        return 0 if run_shard(args.shard, shards) else 1
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
