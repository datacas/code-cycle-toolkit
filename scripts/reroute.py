"""Conservative effect reconciliation before repeating a failed stage.

Snapshots are transient. Only the closed decision and reason reach telemetry.
An unreadable boundary is unknown, never evidence that nothing happened.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from executors import _git_output, _remote_ref_fingerprint, _workspace_fingerprint


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

    branch = _git_output(root, "branch", "--show-current")
    if change_request_id:
        pulls = [gh("pr", "view", str(change_request_id), "--repo", repo,
                    "--json", "number,headRefOid,state")]
    elif branch:
        pulls = gh("pr", "list", "--repo", repo, "--head", branch,
                   "--state", "all", "--json", "number,headRefOid,state")
    else:
        raise ValueError("publication scope unavailable")
    comments = []
    for pull in pulls:
        number = pull["number"]
        for endpoint in (f"issues/{number}/comments", f"pulls/{number}/comments",
                         f"pulls/{number}/reviews"):
            comments.append(gh("api", "--paginate", "--slurp",
                               f"repos/{repo}/{endpoint}"))
    # Implementation may comment on its issue before there is a PR.
    if issue_id and issue_id.isdecimal():
        comments.append(gh("api", "--paginate", "--slurp",
                           f"repos/{issue_repo or repo}/issues/{issue_id}/comments"))
    snapshot.update(pulls=pulls, comments=comments)
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
    if before.get("comments") != after.get("comments"):
        return "stop", "publication_changed"
    return "reroute", "no_effects"
