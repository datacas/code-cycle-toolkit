#!/usr/bin/env python3
"""A CLI agent that runs nothing, for driving a whole cycle without providers.

Installed under both `codex` and `claude` on a temporary PATH. It answers
`--version`, reports the model it was asked for the way the real binary does,
echoes the prompt it received — which is how the real ones behave and is what
puts a decoy `ORCHESTRATION_RESULT` in the stream — and emits the structured
block the driver reads the verdict from.

Its answers are steered by the environment, so one binary covers a healthy run,
an exhausted window, and a review that changes its mind on the second round:

    FAKE_QUOTA=codex       that agent reports an exhausted window and exits 1
    FAKE_ROUNDS=<path>     a counter file; the first review asks for changes
    FAKE_SILENT=claude     that agent emits no structured block at all
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

NAME = Path(sys.argv[0]).name
BEGIN, END = "ORCHESTRATION_RESULT", "END_ORCHESTRATION_RESULT"

SKILL_STATUS = {
    "cc-implement-issue": "IMPLEMENTED",
    "cc-resolve-comments": "RESOLVED",
}


def flag(argv: list[str], *names: str) -> str | None:
    for name in names:
        if name in argv:
            index = argv.index(name) + 1
            if index < len(argv):
                return argv[index]
    return None


def prompt_of(argv: list[str]) -> str:
    return flag(argv, "-p") or argv[-1]


def review_status(prompt: str) -> str:
    """Asks for changes once, then approves — a cycle with a real second round."""
    counter = os.environ.get("FAKE_ROUNDS")
    if "cc-rereview" in prompt or not counter:
        return "APPROVED"
    path = Path(counter)
    seen = int(path.read_text(encoding="utf-8") or "0") if path.exists() else 0
    path.write_text(str(seen + 1), encoding="utf-8")
    return "CHANGES_REQUESTED" if seen == 0 else "APPROVED"


def status_for(prompt: str) -> str:
    for skill, status in SKILL_STATUS.items():
        if skill in prompt:
            return status
    return review_status(prompt)


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(f"{NAME} 0.0.0-fake")
        return 0

    if os.environ.get("FAKE_QUOTA") == NAME:
        print("usage limit reached for this window", file=sys.stderr)
        return 1

    model = flag(argv, "-m", "--model") or "unknown"
    prompt = prompt_of(argv)

    # The real binaries report the resolved model differently, and the adapters
    # read each one its own way.
    if NAME == "claude":
        print(json.dumps({"modelUsage": {model: {"inputTokens": 1}}}))
    else:
        print(json.dumps({"model": model}))

    print(f"prompt received: {prompt}")

    if os.environ.get("FAKE_SILENT") == NAME:
        return 0

    status = status_for(prompt)
    payload = {"skill": "fake", "status": status}
    if status == "CHANGES_REQUESTED":
        payload["unresolved_findings"] = [
            {"id": "REV-001", "severity": "high", "status": "open",
             "blocks_approval": True},
            {"id": "REV-002", "severity": "low", "status": "open",
             "blocks_approval": False},
        ]
    print(BEGIN)
    print(json.dumps(payload))
    print(END)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
