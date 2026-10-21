#!/usr/bin/env python3
"""Validate a staged resolver comment with the shared review contract."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_contract


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate all machine-readable lines in a staged comment.")
    parser.add_argument("comment", type=Path, help="path to the staged comment")
    args = parser.parse_args()
    try:
        body = args.comment.read_text(encoding="utf-8")
        review_contract.validate_triage_comment(body)
    except (OSError, review_contract.ContractError) as error:
        print(f"{args.comment}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
