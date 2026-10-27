#!/usr/bin/env python3
"""Record a rereview that ran outside the runtime against one pull request head.

A person who reviewed a change request by hand, because no stage could, uses
`record` to attach that rereview to the cycle (#346). It publishes one comment
on the pull request with a schema-2 receipt marked `provenance: manual`:

- the operator is the GitHub account `gh` is authenticated as, read from the
  code host, never a value typed on the command line;
- `--confirm-head` must be the full SHA of the pull request's current head;
- every check is declared by the operator, with its result and a link to its
  evidence, and is never presented as a check the runtime ran;
- recording the same rereview again publishes nothing and edits nothing.

A recorded rereview is not an approval. `run_cycle.py --from rereview` closes
the cycle on it only when it is clean, confirmed and still on the current
head, and only after the required checks of that head pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys

try:
    from gh_environment import apply_gh_environment_defaults
except ModuleNotFoundError:
    from .gh_environment import apply_gh_environment_defaults
from review_contract import (
    MANUAL_CHECK_RESULTS,
    MANUAL_PROVENANCE,
    MANUAL_STATUSES,
    FINDING_STATUSES,
    ContractError,
    format_stage_evidence,
    stage_evidence,
)

FULL_SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
FINDING = re.compile(r"(REV-[0-9]{3,})=([a-z_]+)")


class ManualRereviewError(RuntimeError):
    """The rereview cannot be recorded; nothing was published."""


def parse_check(text: str) -> dict:
    """`RESULT:NAME:EVIDENCE`, such as `pass:python3 -m unittest:https://...`."""
    parts = text.split(":", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise ManualRereviewError(
            f"--check {text!r} is not RESULT:NAME:EVIDENCE")
    result, name, evidence = (part.strip() for part in parts)
    if result not in MANUAL_CHECK_RESULTS:
        raise ManualRereviewError(
            f"--check result {result!r} is not one of {', '.join(MANUAL_CHECK_RESULTS)}")
    return {"name": name, "result": result, "evidence": evidence}


def parse_finding(text: str) -> dict:
    """`REV-001=resolved`: an outcome the operator verified for a published finding."""
    match = FINDING.fullmatch(text.strip())
    if match is None or match.group(2) not in FINDING_STATUSES:
        raise ManualRereviewError(
            f"--finding {text!r} is not REV-NNN={'|'.join(FINDING_STATUSES)}")
    return {"id": match.group(1), "status": match.group(2)}


def build_receipt(repo: str, number: str, head: str, operator: str, status: str,
                  checks: list[dict], findings: list[dict]) -> dict:
    """The manual receipt; its attempt ID is derived from what it records.

    The same rereview always yields the same attempt ID, which is what makes
    recording it twice a no-op, and a different one never reuses an ID.
    """
    content = {"repo": repo, "change_request_id": number, "head_sha": head,
               "operator": operator, "status": status, "checks": checks,
               "finding_outcomes": findings}
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    return {
        "schema": 2, "repo": repo, "change_request_id": number, "stage": "rereview",
        "attempt_id": f"manual-{digest}", "head_sha": head, "status": status,
        "finding_outcomes": findings, "provenance": MANUAL_PROVENANCE,
        "operator": operator, "confirmed_head": head, "checks": checks,
    }


def comment_body(receipt: dict) -> str:
    """The published record: plain about what it is, then its receipt."""
    lines = [
        "### Manual rereview record",
        "",
        f"A rereview of `{receipt['head_sha']}` ran outside the Code Cycle runtime. "
        "This comment records it; it is not a review the runtime dispatched, and "
        "recording it is not an approval.",
        "",
        f"- Provenance: `{MANUAL_PROVENANCE}`",
        f"- Operator: `{receipt['operator']}`, the authenticated account that "
        "published this comment",
        f"- Head: `{receipt['head_sha']}`, confirmed with `--confirm-head`",
        f"- Verdict: `{receipt['status']}`",
        "- Checks declared by the operator, not run by the runtime:",
    ]
    lines.extend(f"  - `{item['result']}` {item['name']}: {item['evidence']}"
                 for item in receipt["checks"])
    if receipt["finding_outcomes"]:
        lines.append("- Finding outcomes: " + ", ".join(
            f"{item['id']} `{item['status']}`" for item in receipt["finding_outcomes"]))
    lines.extend(["", format_stage_evidence(receipt)])
    return "\n".join(lines)


def _gh(runner, args: list[str], *, cwd: str | None = None):
    try:
        result = runner(["gh", *args], cwd=cwd, capture_output=True, text=True,
                        timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise ManualRereviewError(f"gh {args[0]} could not run: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise ManualRereviewError(
            f"gh {' '.join(args[:2])} exited {result.returncode}"
            + (f": {detail[-1][:200]}" if detail else ""))
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise ManualRereviewError(f"gh {' '.join(args[:2])} returned no JSON") from error


def record(repo: str, number: str, *, status: str, confirm_head: str,
           checks: list[dict], findings: list[dict], runner=subprocess.run,
           cwd: str | None = None, dry_run: bool = False) -> dict:
    """Publish the manual rereview of the current head once, or say why not."""
    if REPOSITORY.fullmatch(repo) is None:
        raise ManualRereviewError(f"--repo {repo!r} is not owner/name")
    if not number.isdigit():
        raise ManualRereviewError(f"--pr {number!r} is not a pull request number")
    if FULL_SHA.fullmatch(confirm_head) is None:
        raise ManualRereviewError("--confirm-head must be the full 40-character head SHA")
    if status not in MANUAL_STATUSES:
        raise ManualRereviewError(f"--status must be one of {', '.join(MANUAL_STATUSES)}")
    if not checks:
        raise ManualRereviewError("declare at least one --check the rereview ran")
    user = _gh(runner, ["api", "user"], cwd=cwd)
    operator = user.get("login") if isinstance(user, dict) else None
    if not isinstance(operator, str) or not operator.strip():
        raise ManualRereviewError("the authenticated GitHub account has no login")
    pull = _gh(runner, ["pr", "view", number, "--repo", repo,
                        "--json", "number,state,headRefOid"], cwd=cwd)
    if not isinstance(pull, dict) or str(pull.get("number")) != number:
        raise ManualRereviewError(f"pull request {number} could not be read")
    if pull.get("state") != "OPEN":
        raise ManualRereviewError(f"pull request {number} is not open")
    head = pull.get("headRefOid")
    if head != confirm_head:
        raise ManualRereviewError(
            f"--confirm-head {confirm_head[:12]} is not the current head "
            f"{str(head)[:12]} of pull request {number}; review the current head "
            "and confirm that one")
    receipt = build_receipt(repo, number, head, operator, status, checks, findings)
    try:
        body = comment_body(receipt)
    except ContractError as error:
        raise ManualRereviewError(f"the receipt cannot be published: {error}") from error
    pages = _gh(runner, ["api", "--paginate", "--slurp",
                         f"repos/{repo}/issues/{number}/comments"], cwd=cwd)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ManualRereviewError("the pull request comments could not be read")
    for comment in (item for page in pages for item in page):
        if not isinstance(comment, dict):
            continue
        author = (comment.get("user") or {}).get("login")
        if not isinstance(author, str) or author.casefold() != operator.casefold():
            continue
        try:
            found = stage_evidence(comment.get("body") or "")
        except ContractError:
            continue
        if any(item["attempt_id"] == receipt["attempt_id"] for item in found):
            return {"status": "already_recorded", "attempt_id": receipt["attempt_id"],
                    "head_sha": head, "operator": operator,
                    "comment_url": comment.get("html_url")}
    if dry_run:
        return {"status": "dry_run", "attempt_id": receipt["attempt_id"],
                "head_sha": head, "operator": operator, "body": body}
    published = _gh(runner, ["api", f"repos/{repo}/issues/{number}/comments",
                             "--method", "POST", "--raw-field", f"body={body}"], cwd=cwd)
    return {"status": "recorded", "attempt_id": receipt["attempt_id"], "head_sha": head,
            "operator": operator,
            "comment_url": published.get("html_url") if isinstance(published, dict) else None}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manual_rereview", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="record a rereview that ran outside the runtime")
    rec.add_argument("--repo", required=True, help="GitHub owner/name")
    rec.add_argument("--pr", required=True, help="pull request number")
    rec.add_argument("--status", required=True, choices=MANUAL_STATUSES,
                     help="the verdict of the manual rereview")
    rec.add_argument("--confirm-head", required=True, metavar="SHA",
                     help="full SHA of the pull request's current head, the one reviewed")
    rec.add_argument("--check", action="append", default=[], required=True,
                     metavar="RESULT:NAME:EVIDENCE",
                     help=("a check the rereview ran, its result "
                           f"({'|'.join(MANUAL_CHECK_RESULTS)}) and a link to its evidence; "
                           "repeatable"))
    rec.add_argument("--finding", action="append", default=[], metavar="REV-NNN=STATUS",
                     help="an outcome the rereview verified for a published finding; repeatable")
    rec.add_argument("--dry-run", action="store_true",
                     help="print the comment that would be published, publish nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    apply_gh_environment_defaults()
    args = _parser().parse_args(argv)
    try:
        result = record(
            args.repo, args.pr, status=args.status, confirm_head=args.confirm_head,
            checks=[parse_check(item) for item in args.check],
            findings=[parse_finding(item) for item in args.finding],
            dry_run=args.dry_run)
    except ManualRereviewError as error:
        print(f"manual_rereview: {error}; nothing was published", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result["status"] in {"recorded", "already_recorded"}:
        print(f"next: run_cycle.py --task <work item> --from rereview --pr {args.pr} "
              "closes the cycle on this rereview once the required checks of its head "
              "pass", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
