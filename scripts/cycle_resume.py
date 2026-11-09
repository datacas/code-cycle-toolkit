"""Resume a finished publishing stage without a manual `--from` and `--pr` (#464).

A stage that did its work, pushed its branch and opened its change request can
still end the cycle: a refusal, an interruption or a read failure stopped it
before the next stage. The cycle journal then holds a *resume point*, written by
the runtime when the stage's own structured result proved the work done. Running
the same work item again reads that point and starts from the next stage:
`implement` resumes at `review` and `resolve` at `rereview`.

The point is never trusted by itself. It is used only when

- the earlier run recorded its end and no later stage started;
- the persisted state is consistent: a publishing role, a full head, the change
  request number and the stage that follows the role;
- the change request is open and its head is still the recorded head.

Anything else is refused with the mismatch named, never guessed around, and the
explicit `--from <stage> --pr <n>` stays available. A pull request existing is
not completion: the point is written only from a result that claims, or proves,
the work done (`command_permissions.inferred_completion`).

Reading the journal never widens a permission and never changes a decision.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import cycle_journal

#: The stage a resume starts at, by the publishing role that finished.
NEXT_STAGE = {"implement": "review", "resolve": "rereview"}
#: The journal component of a resume point and of a recorded automatic resume.
POINT = "resume_point"
RESUMED = "auto_resume"
FULL_SHA = re.compile(r"[0-9a-f]{40}")


class ResumeRefused(ValueError):
    """The recorded resume point cannot be used; the message names why."""


@dataclass(frozen=True)
class AutoResume:
    """What the rerun does instead of starting the work item over."""

    start_from: str
    change_request_id: str
    head_sha: str
    blocked_role: str
    cycle_id: str
    cause: str
    status: str
    checks: tuple[str, ...]
    point: str = ""

    def notice(self) -> str:
        return (f"  auto-resume  the {self.blocked_role} stage of {self.cycle_id} left change "
                f"request #{self.change_request_id} at {self.head_sha[:12]} ({self.status}: "
                f"{self.cause}); starting from {self.start_from}. Checks: "
                + "; ".join(self.checks))


def work_done(payload: dict[str, Any], role: str) -> bool:
    """Whether the stage's structured result claims or proves its work done.

    It needs a full head and a change request, no pending publication, no
    environment blocker, no essential refused operation and no failed check, and
    then either a completion status valid for the role (`status` or the agent's
    `completed_status`) or the structured evidence `inferred_completion` reads.
    """
    from command_permissions import inferred_completion
    from execution_plan import completion_statuses
    if role not in NEXT_STAGE or not isinstance(payload, dict):
        return False
    head = payload.get("head_sha")
    if not isinstance(head, str) or not FULL_SHA.fullmatch(head):
        return False
    if not (payload.get("change_request_id") or payload.get("pr_number")):
        return False
    if (payload.get("publication_pending") is True or payload.get("environment_blocker")
            or payload.get("essential_permission")):
        return False
    tests = payload.get("tests")
    checks = tests.get("checks") if isinstance(tests, dict) else None
    if any(isinstance(check, dict) and check.get("result") == "fail" for check in checks or []):
        return False
    completed = completion_statuses().get(role, frozenset())
    return (payload.get("status") in completed or payload.get("completed_status") in completed
            or inferred_completion(payload, role, completed) is not None)


def pull_number(value: object) -> str | None:
    match = re.fullmatch(r"(?:.*/pull/)?#?([1-9][0-9]{0,9})/?", str(value or ""))
    return match.group(1) if match else None


def record_resume_point(journal: Any, role: str, payload: dict[str, Any], *, cause: str,
                        status: str, branch: str | None = None) -> bool:
    """Write the resume point of a publishing stage whose work is proven done."""
    if not work_done(payload, role):
        return False
    number = pull_number(payload.get("change_request_id") or payload.get("pr_number"))
    if number is None:
        return False
    return journal.record(
        "stage_result", role=role, component=POINT, status=status, reason=cause,
        change_request_id=number,
        correlation={"head_sha": payload["head_sha"], "next_stage": NEXT_STAGE[role],
                     "branch": branch})


def read_pull_with_gh(repo_id: str) -> Callable[[str], dict[str, Any]]:
    """A reader of the change request's state through `gh`, for `auto_resume`."""
    def read(number: str) -> dict[str, Any]:
        try:
            done = subprocess.run(
                ["gh", "pr", "view", number, "--repo", repo_id, "--json",
                 "state,headRefOid,headRefName"],
                capture_output=True, text=True, check=False, timeout=60,
                stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ResumeRefused(f"could not read change request #{number}: {error}") from error
        if done.returncode != 0:
            raise ResumeRefused(f"could not read change request #{number} in {repo_id}")
        try:
            pull = json.loads(done.stdout)
        except ValueError as error:
            raise ResumeRefused(f"change request #{number} could not be read") from error
        return pull if isinstance(pull, dict) else {}
    return read


def auto_resume(database: Any, repo_id: str, task_id: str, *,
                read_pull: Callable[[str], dict[str, Any]]) -> AutoResume | None:
    """The resume the journal and the code host both support, or None.

    None means there is nothing to resume and the work item starts as before,
    including when the history cannot be read. `ResumeRefused` means a resume
    point exists and cannot be honoured; its message says what did not match.
    """
    try:
        rows = cycle_journal.events(
            database, repo_id, task_id=task_id,
            kinds=("stage_start", "stage_end", "stage_result", "stop"))
    except Exception:
        return None
    index = max((position for position, row in enumerate(rows)
                 if row["kind"] == "stage_result" and row.get("component") == POINT), default=None)
    if index is None:
        return None
    point, later = rows[index], rows[index + 1:]
    if any(row["kind"] in {"stage_start", "stage_end"} for row in later):
        return None  # a stage began after it: nothing is left to resume at that point
    # The run that ended must have recorded its stop after the newest resume of
    # this point, or a second rerun could start beside one still running.
    resumed = max((position for position, row in enumerate(later)
                   if row.get("component") == RESUMED), default=-1)
    if not any(row["kind"] == "stop" for row in later[resumed + 1:]):
        raise ResumeRefused(
            f"the earlier run {later[resumed]['cycle_id'] if resumed >= 0 else point['cycle_id']} "
            "recorded no stop; it may still be running")
    facts = point.get("correlation") or {}
    role, head, number = point.get("role"), facts.get("head_sha"), point.get("change_request_id")
    if (role not in NEXT_STAGE or not isinstance(head, str) or not FULL_SHA.fullmatch(head)
            or not isinstance(number, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", number)
            or facts.get("next_stage") != NEXT_STAGE[role]):
        raise ResumeRefused("the persisted resume point is inconsistent: "
                            f"role={role!r}, head={head!r}, change request={number!r}, "
                            f"next stage={facts.get('next_stage')!r}")
    pull = read_pull(number)
    state = str(pull.get("state") or "").upper()
    if state != "OPEN":
        raise ResumeRefused(f"change request #{number} is {state or 'in an unknown state'}, "
                            "not open")
    observed = pull.get("headRefOid")
    if observed != head:
        raise ResumeRefused(
            f"change request #{number} is at {str(observed)[:12]}, not at the recorded "
            f"{head[:12]}; check what moved it and resume with --from {NEXT_STAGE[role]} "
            f"--pr {number} if that is intended")
    branch = facts.get("branch")
    if branch and pull.get("headRefName") not in {None, branch}:
        raise ResumeRefused(f"change request #{number} is on {pull.get('headRefName')}, not on "
                            f"the recorded branch {branch}")
    return AutoResume(
        start_from=NEXT_STAGE[role], change_request_id=number, head_sha=head,
        blocked_role=role, cycle_id=point["cycle_id"], status=str(point.get("status")),
        point=str(point["event_id"]),
        cause=str(point.get("reason") or "the stage's work was proven done"),
        checks=(f"journal resume point {point['event_id']} of {point['cycle_id']}",
                "earlier run recorded its stop",
                f"change request #{number} is open at the recorded head {head[:12]}"))


def record_auto_resume(database: Any, repo_id: str, task_id: str, resume: AutoResume) -> bool:
    """Record the resume, the block reason, the recovery stage and the checks made.

    Written under the cycle that left the point, so the work item's journal
    reads in order: the stop, then the resume that answered it. A write failure
    never changes the decision.
    """
    try:
        cycle_journal.record_event(
            database, repo_id=repo_id, task_id=task_id, cycle_id=resume.cycle_id,
            kind="stage_result", role=resume.blocked_role, component=RESUMED,
            status="resumed", reason=resume.cause, change_request_id=resume.change_request_id,
            detail="; ".join(resume.checks),
            correlation={"head_sha": resume.head_sha, "recovery_stage": resume.start_from,
                         "original_status": resume.status, "point": resume.point})
    except Exception:
        return False
    return True
