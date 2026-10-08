#!/usr/bin/env python3
"""A CLI agent fake for driving a whole cycle without providers.

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
    FAKE_SILENT_REVIEW=codex that agent omits only an initial-review result
    FAKE_BLOCKED=codex     that agent exits 0 reporting BLOCKED, as one did
    FAKE_READINESS=<status> the issue review's status (default READY); the
                           other variables leave the issue review alone
    FAKE_HANG=codex        that agent never finishes an implementation, as a
                           long one looks to a host that stops it; with
    FAKE_PIDFILE=<path>    it first writes its process ID there

It writes each CLI's real envelope — Codex NDJSON with `item.completed`, Claude
one JSON object with `result` — because that is what a canary found the driver
could not read. A fake that printed the block as plain text passed every test
while the real thing did not work.

It executes only the runtime's explicit Git metadata write-probe command; all
agent prompts still receive scripted answers without running their contents.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
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


def speak(model: str, message: str) -> None:
    """Print the message the way this agent's real CLI prints one.

    Codex emits NDJSON events and puts the reply in an `agent_message` item; it
    reports no model anywhere, which is what a live run showed. Claude emits
    verbose JSONL events and puts the reply in its final `result` event.
    """
    if NAME == "claude":
        print(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": message}]},
        }))
        print(json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "modelUsage": {model: {"inputTokens": 1, "outputTokens": 1}},
            "result": message,
        }))
        return
    print(json.dumps({"type": "thread.started", "thread_id": "0" * 8}))
    print(json.dumps({"type": "item.completed",
                      "item": {"id": "item_0", "type": "agent_message",
                               "text": message}}))
    print(json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}))


def result_for_role(role: str, status: str = "CHANGES_REQUESTED") -> dict:
    """Return the structured payload this fake stage emits for a role."""
    skill = {
        "implement": "cc-implement-issue",
        "review": "cc-initial-review",
        "rereview": "cc-rereview",
        "resolve": "cc-resolve-comments",
    }.get(role, "fake")
    payload = {"skill": skill, "status": status}
    if role == "implement" and status == "IMPLEMENTED":
        payload["change_request_id"] = "4"
        if os.environ.get("FAKE_TEST_EVIDENCE") == "1":
            payload["tests"] = {
                "ran": True,
                "passed": True,
                "conclusion": "verified_with_reservations",
                "evidence": [
                    {"level": "test", "command": "python -m unittest",
                     "exit_code": 0, "executed": 12},
                ],
            }
    if role == "review" and status != "BLOCKED":
        findings = ([
            {"id": "REV-001", "severity": "high", "status": "open",
             "blocks_approval": True},
            {"id": "REV-002", "severity": "low", "status": "open",
             "blocks_approval": False},
        ] if status == "CHANGES_REQUESTED" else [])
        payload["findings"] = findings
        payload["blocking_findings"] = [item["id"] for item in findings
                                         if item["blocks_approval"]]
    elif role == "rereview" and status != "BLOCKED":
        payload["verified_findings"] = [
            {"id": "REV-001", "severity": "high", "blocks_approval": True,
             "status": "resolved", "disposition": "valid"},
            {"id": "REV-002", "severity": "low", "blocks_approval": False,
             "status": "resolved", "disposition": "valid"},
        ]
        payload["new_findings"] = []
        payload["blocking_findings"] = []
    elif role == "resolve" and status != "BLOCKED":
        payload["finding_outcomes"] = [
            {"id": "REV-001", "disposition": "valid", "status": "resolved"},
            {"id": "REV-002", "disposition": "valid", "status": "resolved"},
        ]
        payload["resolved_findings"] = [
            {"id": "REV-001", "status": "resolved"},
            {"id": "REV-002", "status": "resolved"},
        ]
        payload["unresolved_findings"] = []
    return payload


def readiness_result() -> dict:
    """An issue review's result: READY unless the environment says otherwise."""
    status = os.environ.get("FAKE_READINESS", "READY")
    payload = {
        "skill": "cc-issue-review", "status": status, "issue_id": "API-7", "confidence": "high",
        "dimensions": ["applicability", "acceptance_verification"],
        "findings": [], "uncertainties": [],
    }
    if status == "NEEDS_REFINEMENT":
        payload["findings"] = [{
            "id": "IR-001", "dimension": "acceptance_verification",
            "severity": "high", "blocks_readiness": True,
            "evidence": [{"kind": "work_item", "ref": "API-7"}],
            "summary": "The acceptance criteria name no observable result.",
            "proposed_change": "State the observable result each criterion checks.",
        }]
    return payload


def main(argv: list[str]) -> int:
    if argv and argv[0] == "sandbox":
        try:
            command = argv[argv.index("--") + 1:]
        except (ValueError, IndexError):
            return 2
        filesystem = next((
            argv[index + 1].split("=", 1)[1]
            for index, value in enumerate(argv[:-1])
            if value == "-c" and argv[index + 1].startswith(
                "permissions.code_cycle_publish_write.filesystem=",
            )
        ), "")
        entries = {
            json.loads(path): json.loads(access)
            for path, access in re.findall(
                r'("(?:\\.|[^"])*")=("(?:\\.|[^"])*")', filesystem,
            )
        }
        if not entries:
            return 2
        common = next(iter(entries))
        worktree = next((
            path for path, access in entries.items()
            if access == "write"
            and path.startswith(os.path.join(common, "worktrees") + os.sep)
        ), None)
        required_readonly = [
            os.path.join(common, name)
            for name in ("config", "config.lock", "config.worktree",
                         "config.worktree.lock", "hooks", "worktrees")
        ]
        required_denied = []
        if worktree is not None:
            workspace = flag(argv, "--cd")
            git_pointer = os.path.join(workspace, ".git") if workspace else None
            if git_pointer and git_pointer != common:
                required_denied.append(git_pointer)
            main_state = (
                "HEAD", "index", "ORIG_HEAD", "FETCH_HEAD", "COMMIT_EDITMSG",
                "MERGE_HEAD", "MERGE_MSG", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                "REBASE_HEAD", "AUTO_MERGE", "BISECT_START", "BISECT_LOG",
                "BISECT_NAMES", "BISECT_EXPECTED_REV", "MERGE_AUTOSTASH",
                "NOTES_MERGE_PARTIAL", "NOTES_MERGE_REF", "SQUASH_MSG", "logs/HEAD",
            )
            required_denied.extend(
                os.path.join(common, name) for name in main_state
            )
            required_denied.extend(
                os.path.join(common, name + ".lock") for name in main_state
            )
            required_denied.extend(os.path.join(common, name) for name in (
                "sequencer", "rebase-apply", "rebase-merge", "bisect",
                "refs/bisect", "refs/worktree", "refs/rewritten",
                "logs/refs/bisect", "logs/refs/worktree", "logs/refs/rewritten",
            ))
            required_readonly.extend(os.path.join(worktree, name) for name in (
                "commondir", "gitdir", "config.worktree",
                "config.worktree.lock",
            ))
            if entries.get(common) != "write":
                return 2
        else:
            required_readonly.append(os.path.join(common, "worktrees"))
        if any(entries.get(path) != "read" for path in required_readonly):
            return 2
        if any(entries.get(path) != "deny" for path in required_denied):
            return 2

        def writable(path: Path) -> bool:
            matches = [
                (len(root), access)
                for root, access in entries.items()
                if str(path) == root or str(path).startswith(root + os.sep)
            ]
            return bool(matches) and max(matches)[1] == "write"

        required_writes = [
            Path(common) / name
            for name in ("objects", "refs", "logs", "packed-refs", "packed-refs.lock")
        ]
        if worktree is not None:
            required_writes.append(Path(worktree))
        if any(not writable(path) for path in required_writes):
            return 2

        script = command[-1]
        marker_pairs = re.findall(
            r'Path\(("(?:\\.|[^"])*\.code-cycle-write-probe-[^"]*")\)'
            r'\.write_text\(("(?:\\.|[^"])*")\)',
            script,
        )
        if not marker_pairs:
            return 2
        for path_text, token_text in marker_pairs:
            path = Path(json.loads(path_text))
            if not writable(path):
                return 2
            token = json.loads(token_text)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(token)
        return 0

    if "--version" in argv:
        # Exercise the current Codex permission-profile path in installed-cycle
        # tests, without installing either real agent.
        print(f"{NAME} 0.138.0-fake")
        return 0

    model = flag(argv, "-m", "--model") or "unknown"
    prompt = prompt_of(argv)
    if os.environ.get("FAKE_QUOTA") == NAME:
        print(json.dumps({"type": "error", "started": False,
                          "message": "usage limit reached for this window"}))
        print("usage limit reached for this window", file=sys.stderr)
        return 1

    if os.environ.get("FAKE_CAPACITY") == NAME:
        print(json.dumps({"type": "error", "started": False,
                          "codex_error_info": "server_overloaded",
                          "message": "Selected model is at capacity"}))
        return 1

    said = [f"prompt received: {prompt}"]

    if os.environ.get("FAKE_HANG") == NAME and "cc-implement-issue" in prompt:
        pidfile = os.environ.get("FAKE_PIDFILE")
        if pidfile:
            with open(pidfile, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
        time.sleep(300)
        return 1

    if (os.environ.get("FAKE_SILENT") == NAME
            or (os.environ.get("FAKE_SILENT_REVIEW") == NAME
                and "cc-initial-review" in prompt)):
        speak(model, said[0])
        return 0

    if "cc-issue-review" in prompt:
        said += [BEGIN, json.dumps(readiness_result()), END]
        speak(model, "\n".join(said))
        return 0

    status = "BLOCKED" if os.environ.get("FAKE_BLOCKED") == NAME else status_for(prompt)
    selected = next(((skill, role) for skill, role in (
        ("cc-implement-issue", "implement"),
        ("cc-initial-review", "review"),
        ("cc-rereview", "rereview"),
        ("cc-resolve-comments", "resolve"),
    ) if skill in prompt), ("fake", None))
    _, role = selected
    payload = result_for_role(role or "unknown", status)
    said += [BEGIN, json.dumps(payload), END]
    speak(model, "\n".join(said))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
