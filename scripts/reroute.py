"""Conservative effect reconciliation before repeating a failed stage.

Snapshots are transient. Only the closed decision and reason reach telemetry.
An unreadable boundary is unknown, never evidence that nothing happened.

A publication is what the cycle's own code-host account created or edited
(#268): pull requests, comments and reviews. PR edits have no editor identity,
so edits to any existing in-scope PR stop conservatively. New foreign PRs,
other accounts' comments, reactions and timestamps do not stop a reroute.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess

from executors import _git_output, _remote_ref_fingerprint, _workspace_fingerprint


def _digest(*parts) -> str:
    return hashlib.sha256("\0".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()


def _authored_by(item: dict, login: str) -> bool:
    author = item.get("author") or item.get("user") or {}
    return str(author.get("login") or "").casefold() == login.casefold()


def _pulls(pulls: list) -> dict:
    return {pull["number"]: {"number": pull["number"], "headRefOid": pull.get("headRefOid"),
             "state": pull.get("state"),
             "text": _digest(pull.get("title"), pull.get("body"))}
            for pull in pulls}


def _own_comments(endpoint: str, pages: list, login: str) -> list:
    """Identity and content of this account's items; `--slurp` returns pages."""
    items = [item for page in pages for item in (page if isinstance(page, list) else [page])]
    return sorted(
        (endpoint, item["id"], item.get("state") or "", _digest(item.get("body")))
        for item in items if isinstance(item, dict) and _authored_by(item, login)
    )


def effect_snapshot(cwd: str | None, repo: str, *, publishes: bool,
                    change_request_id: str | None = None,
                    issue_repo: str | None = None, issue_id: str | None = None) -> dict:
    root = cwd or os.getcwd()
    if publishes and issue_id and not issue_id.isdecimal():
        raise ValueError("issue publication boundary unavailable")
    if publishes:
        origin = _git_output(root, "remote", "get-url", "origin")
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?", origin)
        if match is None or match.group(1).casefold() != repo.casefold():
            raise ValueError("code host publication boundary unavailable")
    workspace = _workspace_fingerprint(root)
    snapshot = {"workspace": workspace,
                "dirty": bool(_git_output(root, "status", "--porcelain",
                                          "--untracked-files=all")),
                "remote": _remote_ref_fingerprint(root)}
    if not publishes:
        return snapshot

    def gh(*args):
        result = subprocess.run(["gh", *args], cwd=root, capture_output=True,
                                text=True, timeout=30)
        if result.returncode:
            raise ValueError("publication evidence unavailable")
        return json.loads(result.stdout)

    # The account the cycle publishes with; without it nothing is attributable.
    login = gh("api", "user").get("login")
    if not isinstance(login, str) or not login:
        raise ValueError("publication identity unavailable")
    fields = "number,headRefOid,state,author,title,body"
    branch = _git_output(root, "branch", "--show-current")
    if change_request_id:
        pulls = [gh("pr", "view", str(change_request_id), "--repo", repo,
                    "--json", fields)]
    elif branch:
        pulls = gh("pr", "list", "--repo", repo, "--head", branch,
                   "--state", "all", "--json", fields)
    else:
        raise ValueError("publication scope unavailable")
    comments = []
    # Every pull on the scope is read, so the cycle's comments on a PR another
    # account opened still count.
    for pull in pulls:
        number = pull["number"]
        for endpoint in (f"repos/{repo}/issues/{number}/comments",
                         f"repos/{repo}/pulls/{number}/comments",
                         f"repos/{repo}/pulls/{number}/reviews"):
            comments.extend(_own_comments(endpoint, gh(
                "api", "--paginate", "--slurp", endpoint), login))
    # Implementation may comment on its issue before there is a PR.
    if issue_id and issue_id.isdecimal():
        endpoint = f"repos/{issue_repo or repo}/issues/{issue_id}/comments"
        comments.extend(_own_comments(endpoint, gh(
            "api", "--paginate", "--slurp", endpoint), login))
    observed_pulls = _pulls(pulls)
    snapshot.update(pulls=[observed_pulls[pull["number"]] for pull in pulls
                           if _authored_by(pull, login)],
                    observed_pulls=observed_pulls, comments=comments)
    return snapshot


def reconcile(before: dict | None, after: dict | None) -> tuple[str, str]:
    if before is None or after is None:
        return "stop", "evidence_unavailable"
    if before["dirty"] or after["dirty"]:
        return "stop", "dirty_workspace"
    if before["workspace"] != after["workspace"]:
        return "stop", "workspace_changed"
    if before["remote"] != after["remote"]:
        return "stop", "remote_changed"
    if before.get("pulls") != after.get("pulls"):
        return "stop", "publication_changed"
    # PR authorship cannot attribute later edits. Keep every pre-existing PR
    # protected, while allowing newly appearing PRs from another account.
    if any(after.get("observed_pulls", {}).get(number) != pull
           for number, pull in before.get("observed_pulls", {}).items()):
        return "stop", "publication_changed"
    if before.get("comments") != after.get("comments"):
        return "stop", "publication_changed"
    return "reroute", "no_effects"
