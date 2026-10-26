"""GitHub upload from the stage's own execution environment, never the host.

The CLI is deliberately narrow: origin must identify the authorized repository,
and only the current non-base branch can be uploaded. HTTPS recovery changes no
remote, credential store or persistent Git configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable


REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


def safe_error(value: str) -> str:
    value = re.sub(r"https?://[^\s/@]+(?::[^\s/@]+)?@", "https://[redacted]@", value)
    value = re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[redacted]", value)
    value = re.sub(r"(?i)((?:bearer|basic)\s+)[A-Za-z0-9._~+/-]+=*", r"\1[redacted]", value)
    return " ".join(value.split())[:1200]


def github_url(url: str, repo: str) -> bool:
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?", url)
    return match is not None and match[1].casefold() == repo.casefold()


def transport(action: str, repo: str, base: str, *, cwd: str | None = None,
              run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> dict[str, Any]:
    """Preflight or publish; an exit-zero push AND matching remote ref is required."""
    evidence: dict[str, Any] = {"action": action, "repo": repo, "status": "failed", "attempts": []}

    def command(argv: list[str], *, required: bool = True):
        try:
            result = run(argv, cwd=cwd, capture_output=True, text=True, timeout=60,
                         stdin=subprocess.DEVNULL,
                         env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
        except subprocess.TimeoutExpired as error:
            partial = error.stderr or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            result = subprocess.CompletedProcess(
                argv, 124, "", partial + "\npublication command timed out after 60s")
        except OSError as error:
            result = subprocess.CompletedProcess(argv, 127, "", safe_error(str(error)))
        evidence["attempts"].append({"command": argv, "exit_code": result.returncode,
                                     "error": safe_error(result.stderr or "")})
        if required and result.returncode:
            raise ValueError(f"{argv[0]} {argv[-1]} failed (exit {result.returncode})")
        return result

    try:
        if action not in {"preflight", "push"} or not REPOSITORY.fullmatch(repo):
            raise ValueError("unsupported publication scope")
        branch = command(["git", "branch", "--show-current"]).stdout.strip()
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
                or ".." in branch or branch.startswith("refs/")
                or branch in {base, "main", "master"}):
            raise ValueError("publication requires the current non-base working branch")
        origin = command(["git", "remote", "get-url", "--push", "--all", "origin"]).stdout.splitlines()
        if len(origin) != 1 or not github_url(origin[0], repo):
            raise ValueError("origin push URL is not the explicitly authorized GitHub repository")
        https = f"https://github.com/{repo}.git"
        # Detect insteadOf rewrites before handing credentials to Git.
        effective = command(["git", "ls-remote", "--get-url", https]).stdout.strip()
        if effective != https:
            raise ValueError("Git URL rewriting changes the authorized HTTPS destination")
        command(["gh", "auth", "status"])
        permission = command(["gh", "repo", "view", repo, "--json", "viewerPermission"])
        if json.loads(permission.stdout).get("viewerPermission") not in {"WRITE", "MAINTAIN", "ADMIN"}:
            raise ValueError("GitHub account lacks push permission")
        head = command(["git", "rev-parse", "HEAD"]).stdout.strip()
        if not SHA.fullmatch(head):
            raise ValueError("publication requires a full commit SHA")
        evidence.update(head_sha=head, branch=branch, transport="https")
        credential = ["git", "-c", "credential.helper=", "-c",
                      "credential.helper=!gh auth git-credential", "-c", "core.askPass=",
                      "-c", f"remote.code-cycle-publication.url={https}",
                      "-c", f"remote.code-cycle-publication.pushurl={https}",
                      "-c", "remote.code-cycle-publication.mirror=false"]
        ref = f"refs/heads/{branch}"
        options = ["--no-follow-tags", "--recurse-submodules=no"]
        if action == "preflight":
            command([*credential, "push", "--dry-run", *options, "code-cycle-publication",
                     f"{head}:refs/heads/cc-cycle-preflight"])
            evidence["status"] = "ready"
        else:
            # Preserve the original transport failure, then recover in this same
            # sandbox. No runtime host process ever calls this upload function.
            original = command(["git", "push", *options, "origin", f"{head}:{ref}"], required=False)
            if original.returncode:
                command([*credential, "push", *options, "code-cycle-publication", f"{head}:{ref}"])
            else:
                evidence["transport"] = "ssh" if origin[0].startswith(("git@", "ssh://")) else "https"
            remote = command([*credential, "ls-remote", "--refs", https, ref]).stdout.splitlines()
            observed = [line.split()[0] for line in remote if line.split()[1:] == [ref]]
            evidence["remote_head_sha"] = observed[0] if len(observed) == 1 else None
            if observed != [head]:
                raise ValueError("push did not update the remote branch to the expected commit")
            evidence["status"] = "published"
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError) as error:
        evidence["error"] = safe_error(str(error))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "push"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    args = parser.parse_args(argv)
    result = transport(args.action, args.repo, args.base)
    # A stable, per-dispatch log preserves both the failed original push and
    # recovery evidence. It is read by the runtime before TMPDIR is removed.
    temporary = os.environ.get("TMPDIR")
    if temporary:
        with (Path(temporary) / "publication.log").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result) + "\n")
    print(json.dumps(result))
    return 0 if result["status"] in {"ready", "published"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
