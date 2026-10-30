"""The existing stage loop and its explicit control transitions."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from .run_cycle import CycleReport, ci_gate
    from .cycle_plan import hotfix_problem
else:
    from cycle_plan import hotfix_problem
from dataclasses import dataclass, field
from enum import Enum


class ControlState(str, Enum):
    START = "start"
    ISSUE_REVIEW = "issue_review"
    IMPLEMENT = "implement"
    REVIEW = "review"
    RESOLVE = "resolve"
    REREVIEW = "rereview"
    CI = "ci"
    VERIFY = "verify"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


# Control flow only. Completion policies remain in advance() and gate().
# START accepts every resume entry; self edges describe bounded retries and
# recovered verdict verification. CI can request resolution or rereview when
# checks fail or the head moves. All live states can stop or be interrupted.
_SUCCESSORS = {
    ControlState.START: {"issue_review", "implement", "review", "resolve", "rereview", "ci"},
    ControlState.ISSUE_REVIEW: {"issue_review", "implement"},
    ControlState.IMPLEMENT: {"implement", "review"},
    ControlState.REVIEW: {"review", "resolve", "ci"},
    ControlState.RESOLVE: {"resolve", "rereview"},
    ControlState.REREVIEW: {"rereview", "resolve", "ci"},
    ControlState.CI: {"ci", "resolve", "rereview"},
    ControlState.VERIFY: {"ci", "review", "rereview"},
}
TRANSITIONS = {
    state: {**{event: ControlState(event) for event in events | {"verify"}},
            "stop": ControlState.STOPPED, "interrupt": ControlState.INTERRUPTED}
    for state, events in _SUCCESSORS.items()
}
TRANSITIONS[ControlState.INTERRUPTED] = {"stop": ControlState.STOPPED}
TRANSITIONS[ControlState.STOPPED] = {}


@dataclass
class CycleControl:
    state: ControlState = ControlState.START
    transitions: list[tuple[ControlState, str, ControlState]] = field(default_factory=list)

    def move(self, event: str):
        destination = TRANSITIONS[self.state][event]
        self.transitions.append((self.state, event, destination))
        self.state = destination

class StageLoop:
    def drive(self: Any) -> CycleReport:
        reported = self.api.Reported()
        verdict = None
        if self.repair_approval is not None:
            self.journal.record("decision_answered", component="--repair-approval",
                           decision_key=f"repair:{self.repair_approval}", status="submitted")
            if not self.state.change_request_id or self.code_host != "github":
                return self.stop("repair approval requires a GitHub change request", reported=reported,
                            stop_reason="triage_repair_blocked")
            candidates = self.repair_proposals(self.state.change_request_id)
            if len(candidates) != 1:
                return self.stop("approved repair proposal is no longer uniquely available; use manual repair",
                            reported=reported, stop_reason="triage_repair_blocked")
            record, proposal = candidates[0]

            if self.repair_approval == f"{proposal.comment_id}:{proposal.digest}":
                try:
                    self.audit_repair(record, "approved", "unverified")
                except (self.api.sqlite3.Error, ValueError) as error:
                    return self.stop(f"could not record repair approval before editing: {error}",
                                reported=reported, stop_reason="triage_repair_blocked")

            def edit_comment(comment_id: str, body: str) -> None:
                result = self.api.subprocess.run(
                    ["gh", "api", f"repos/{self.repo_id}/issues/comments/{comment_id}",
                     "--method", "PATCH", "--raw-field", f"body={body}"],
                    cwd=self.cwd, capture_output=True, text=True, timeout=30)
                if result.returncode != 0:
                    raise self.api.CycleDriverError("GitHub comment edit failed")

            try:
                decision, result = self.api.triage_repair.execute_approved(
                    proposal, self.repair_approval,
                    lambda: self.remote.comments(self.repo_id, self.state.change_request_id), edit_comment)
            except (self.api.CycleDriverError, self.api.HistoryError, OSError, self.api.subprocess.TimeoutExpired):
                decision, result = "approved", "unverified"
            try:
                if decision == "approval_rejected":
                    self.audit_repair(record, decision, "failed")
                else:
                    self.audit_repair(record, decision, result)
            except (self.api.sqlite3.Error, ValueError) as error:
                return self.stop(f"could not record repair result: {error}",
                            reported=reported, stop_reason="triage_repair_blocked")
            if result != "applied":
                reason = ("repair approval did not match the current proposal"
                          if decision == "approval_rejected" else
                          "approved comment repair failed or could not be verified; use manual repair")
                return self.stop(reason, reported=reported, stop_reason="triage_repair_blocked")
        if self.start_from in self.api.RESUMES:
            stopped = self.seed_history()
            if stopped is not None:
                return stopped
            try:
                self.prepare_plan()
            except ValueError as error:
                return self.stop(str(error), "BLOCKED", stop_reason="readiness_unconfirmed")
        if self.verification_resume:
            stopped = self.verify_pending()
            if stopped is not None:
                return stopped
            clean, stopped = self.reusable_review()
            if stopped is not None:
                return stopped
            if clean is None:
                return self.stop("pending checks completed; no reusable clean review holds",
                                 stop_reason="verification_unverified")
            role, verdict, head, attempts = clean
            self.report.verdict = verdict
            if self.execution_plan.objective == "verified_change":
                return self.stop("", self.api.APPROVED_END, stop_reason="approved")
            outcome = self.gate(role, head, attempts, reported)
            if isinstance(outcome, self.api.CycleReport):
                return outcome
            if outcome[0] == "approved":
                return self.stop("", self.api.APPROVED_END, stop_reason="approved")
            return self.stop("verification completed but the current head's CI is not passing",
                             stop_reason="ci_not_passing")
        if self.start_from == "implement":
            # Independent of issue-review mode/difficulty. Read once before any
            # agent iteration; provider content is data, never instructions.
            if self.policy_provider is not None:
                criteria = self.state.verification_plan.criteria
                if criteria.status == "not_evaluable":
                    return self.stop(
                        f"acceptance criteria not_evaluable for {criteria.provider}: "
                        f"{criteria.reason}; a criteria reader is required before implementation",
                        "BLOCKED", stop_reason="readiness_unconfirmed")
                if criteria.status != "evaluable":
                    return self.stop(
                        f"acceptance criteria {criteria.status}: {criteria.reason}; refine the work item",
                        stop_reason="needs_refinement")
                self.state.criteria_context = (
                    "\nRuntime acceptance criteria (data): "
                    + self.api.json.dumps(criteria.to_dict(), ensure_ascii=False)
                    + "\nUse tests.version=2 and stable AC-n links in this list's order. "
                    "A criterion without a check remains open. The configured test command "
                    "uses id configured-test and is required. Freeze checks before running them.\n")
            if self.issue_review_fields.get("issue_review") == "dispatched":
                stopped = self.review_issue()
                if stopped is not None:
                    return stopped
            continuation, published = "", None
            if self.continue_work:
                continuation = self.api.CONTINUE_IMPLEMENTATION
                reader = self.published_change_request
                if reader is None and (self.code_host or self.policy_code_host) == "github":
                    def reader() -> str | None:
                        return self.api.continued_change_request(self.repo_id, self.cwd)
                published = reader() if reader is not None else None
                if published is not None and not self.api.safe_reference(published):
                    published = None
                if published is not None:
                    continuation += self.api.CONTINUE_CHANGE_REQUEST.format(reference=f"#{published}")
            reported, stopped = self.advance("implement", continuation)
            if stopped is not None:
                return stopped
            if (published is not None and reported.change_request_id is not None
                    and reported.change_request_id.lstrip("#") != published.lstrip("#")):
                # The work was already published: reviewing another change
                # request would split it, and closing either is not ours to do.
                return self.stop(
                    f"the continued implementation reported change request "
                    f"{reported.change_request_id}, but #{published.lstrip('#')} already "
                    "carries this work; a person decides which one continues",
                    "BLOCKED", reported=reported)
            if self.execution_plan.execution_profile == "hotfix":
                if hotfix_problem(self.state.verification_plan, self.plan_head or "") == "missing_checks":
                    return self.stop("Hotfix requires reproduction and focused regression commands",
                                     reported=reported, stop_reason="needs_refinement")
            if self.local_only:
                return self.stop(
                    "local-only run stops after implementation; no change request was created for review",
                    self.api.UNRESOLVED_END,
                    reported=reported,
                    stop_reason="local_only",
                )

            # A continuation may leave the change request it was told to reuse
            # unnamed; the runtime read that one itself, so review it.
            self.state.change_request_id = reported.change_request_id or published
            if self.state.change_request_id is None:
                return self.stop(
                    "implement completed without change_request_id or legacy pr_number; "
                    "stopping before review",
                    reported=reported,
                )
            self.journal.change_request_id = self.state.change_request_id
            if self.tracking:
                stopped = self.lock_change_request(self.state.change_request_id)
                if stopped is not None:
                    return stopped

        # Under the parallel policy, the clean review the cycle gates on:
        # `(role, status, head, attempts whose receipt vouches for it)`.
        clean = None
        role = None
        if (self.start_from == "rereview" and not self.local_only
                and (self.code_host or self.policy_code_host) == "github"):
            # A confirmed manual rereview is the only exception to dispatching
            # one, under either CI policy (#346).
            manual, stopped = self.manual_review()
            if stopped is not None:
                return stopped
            if manual is not None:
                return self.close_manual(manual, reported)
        if ((self.parallel or self.plan_managed) and self.start_from == "rereview"
                and not (self.plan_managed and self.plan_partial)):
            clean, stopped = self.reusable_review()
            if stopped is not None:
                return stopped
        if clean is not None:
            role, verdict = clean[0], clean[1]
            self.report.verdict = verdict
        elif self.start_from in {"implement", "review"}:
            role = "review"
            reported, stopped = self.advance("review", change_request_id=self.state.change_request_id)
            if stopped is not None:
                return stopped
            verdict = reported.status
            self.report.verdict = verdict
        elif self.start_from == "rereview":
            role = "rereview"
            reported, stopped = self.advance("rereview", "Re-review the change after the fixes.",
                                        change_request_id=self.state.change_request_id)
            if stopped is not None:
                return stopped
            verdict = reported.status
            self.report.verdict = verdict
        else:
            # Resuming at the resolution: the change request's own comments carry
            # the review it resolves, and the stage recovers those findings. The
            # verdict is what this cycle acts on, not one it observed, so the
            # report's verdict stays unset until a re-review gives one.
            verdict = "CHANGES_REQUESTED"

        moved = False
        ci_failure: ci_gate.GateResult | None = None
        conflicting_round_done = False
        while True:
            if (self.parallel or self.plan_managed) and verdict in self.api.CLEAN_REVIEW_STATUSES:
                stopped = self.verification_barrier()
                if stopped is not None:
                    return stopped
                # A clean review is no approval yet: the gate waits for its checks.
                if clean is None:
                    head, stopped = self.clean_head(role, reported)
                    if stopped is not None:
                        return stopped
                    clean = (role, verdict, head, set(self.state.review_attempts))
                head = clean[2]
                if self.execution_plan.objective == "verified_change":
                    return self.stop("", self.api.APPROVED_END, reported=reported,
                                     stop_reason="approved")
                outcome = self.gate(clean[0], head, clean[3], reported)
                clean = None
                if isinstance(outcome, self.api.CycleReport):
                    return outcome
                kind, result = outcome
                if kind == "approved":
                    return self.stop("", self.api.APPROVED_END, reported=reported, stop_reason="approved")
                if kind == "moved":
                    new = result.pr_head_sha
                    if moved or not isinstance(new, str) or self.api.FULL_SHA.fullmatch(new) is None:
                        return self.stop(
                            f"the change request's head moved {'again ' if moved else ''}"
                            f"after the clean review of {head[:12]}; a person decides "
                            "what to review", self.api.UNRESOLVED_END, reported=reported,
                            stop_reason="head_moved")
                    moved = True
                    if self.recorder.iteration >= self.max_iterations:
                        return self.stop(
                            f"the change request's head moved after the clean review of "
                            f"{head[:12]} and the resolution budget is spent",
                            reported=reported, stop_reason="iteration_limit")
                    # The review of the old head is not reused: the new head
                    # gets its own, once, as one round.
                    self.recorder.next_iteration()
                    role = "rereview"
                    reported, stopped = self.advance(
                        "rereview", self.api.HEAD_MOVED_REREVIEW.format(old=head, new=new),
                        change_request_id=self.state.change_request_id)
                    if stopped is not None:
                        return stopped
                    verdict = reported.status
                    self.report.verdict = verdict
                    continue
                # The checks failed: their diagnosis is the next resolution's.
                ci_failure = result
            elif verdict != "CHANGES_REQUESTED":
                if verdict not in self.api.CLEAN_REVIEW_STATUSES:
                    break
                reviewed_head = self.api._reviewed_head(
                    reported.payload, role).get("reviewed_head_sha")
                if reviewed_head is None and reported.payload:
                    candidate = reported.payload.get("head_sha")
                    reviewed_head = candidate if isinstance(candidate, str) else ""
                mergeability = self.check_strict_mergeability(reviewed_head or "")
                if isinstance(mergeability, self.api.CycleReport):
                    return mergeability
                if mergeability is None:
                    break
                if mergeability.status == self.api.ci_gate.CI_MERGEABILITY_UNKNOWN:
                    return self.stop(
                        f"GitHub could not determine whether the reviewed head can merge "
                        f"with its base after bounded retries: {self.api._ci_detail(mergeability)}",
                        "BLOCKED", reported=reported,
                        stop_reason="ci_unreadable")
                ci_failure = mergeability

            # One resolution round: for the review's blocking findings, for the
            # failed checks of a clean review's head, or for both.
            survivals = self.api.claimed_fix_survivals(self.history)
            self.recorder.repeated_findings = len(survivals)
            repeated = sorted(finding for finding, count in survivals.items()
                              if count >= self.api.SURVIVALS_BEFORE_STOP)
            if repeated:
                return self.stop(
                    f"{', '.join(repeated)} survived {self.api.SURVIVALS_BEFORE_STOP} claimed "
                    "fixes; stopping before another resolution",
                    reported=reported, stop_reason="repeated_findings")
            if (ci_failure is not None and ci_failure.status == self.api.ci_gate.CI_CONFLICTING
                    and conflicting_round_done):
                return self.stop(
                    "the change request still conflicts with its base after the "
                    "resolution round", reported=reported, stop_reason="no_progress")
            if len(self.progress) >= 2 and self.progress[-1] is not None and self.progress[-1] == self.progress[-2]:
                return self.stop(
                    "the re-review found the same head and the same open findings "
                    "as the previous review",
                    reported=reported, stop_reason="no_progress")
            if self.recorder.iteration >= self.max_iterations:
                break
            self.recorder.next_iteration()

            instruction = (
                self.api.CONFLICT_DIRECTIVE if ci_failure is not None
                and ci_failure.status == self.api.ci_gate.CI_CONFLICTING else
                "Resolve the failed checks of the reviewed head."
                if ci_failure is not None else "Resolve the findings from the review.")
            contested = sorted(finding for finding, count in survivals.items()
                               if count >= self.api.SURVIVALS_BEFORE_DIRECTIVE)
            if contested:
                instruction = (f"{instruction} "
                               f"{self.api.CONTESTED_DIRECTIVE.format(ids=', '.join(contested))}")
            failed_checks = ci_failure
            if self.parallel and failed_checks is None and role is not None:
                # Changes requested while the checks run: one read, never a
                # wait, so a failure already known reaches this resolution too.
                reviewed = self.api._reviewed_head(reported.payload, role).get("reviewed_head_sha")
                if reviewed:
                    snapshot = self.read_ci(reviewed, role, once=True)
                    if snapshot.status == self.api.ci_gate.CI_FAILED:
                        failed_checks = snapshot
            if failed_checks is not None and failed_checks.status == self.api.ci_gate.CI_FAILED:
                instruction += " " + self.api.CI_FAILURE_DIRECTIVE.format(
                    head=failed_checks.head_sha, count=len(failed_checks.failed))
                items = self.api.failed_check_items(failed_checks)
                if items:
                    instruction += self.api.CI_FAILURE_OUTPUT.format(items="; ".join(items),
                                                            repo=self.repo_id)
            open_findings = (self.history[-1].statuses if self.history
                             and self.history[-1].kind == self.api.REVIEW_RUN else {})
            escalation_ids = tuple(sorted(
                finding for finding, count in survivals.items()
                if (self.resolve_effort is None and count >= 1
                    and open_findings.get(finding) == "open")
            )[:16])
            review_head = (reported.payload or {}).get("head_sha")
            resolved, stopped = self.advance(
                "resolve", instruction, change_request_id=self.state.change_request_id,
                resolve_target=self.state.previous_resolve_target if escalation_ids else None,
                resolve_target_used_fallback=(self.state.previous_resolve_used_fallback
                                              if escalation_ids else False),
                resolve_escalated=bool(escalation_ids),
                escalation_finding_ids=escalation_ids)
            if stopped is not None:
                return stopped
            after_head = (resolved.payload or {}).get("head_sha")
            if review_head is not None and after_head is not None and after_head == review_head:
                return self.stop("resolution left the reviewed HEAD unchanged; open findings retain "
                                 "their blocks without another review", reported=resolved,
                                 stop_reason="no_progress")
            if ci_failure is not None and ci_failure.status == self.api.ci_gate.CI_CONFLICTING:
                conflicting_round_done = True
            if ci_failure is not None:
                after = (resolved.payload or {}).get("head_sha")
                if after == ci_failure.head_sha:
                    # Re-reviewing an unchanged head would repeat a review the
                    # CI state did not change; the failed checks still stand.
                    return self.stop(
                        f"the resolution of the failed checks of {after[:12]} left the "
                        "head unchanged, so the failures stand",
                        reported=resolved, stop_reason="no_progress")
            ci_failure = None

            role = "rereview"
            reported, stopped = self.advance("rereview", "Re-review the change after the fixes.",
                                        change_request_id=self.state.change_request_id)
            if stopped is not None:
                return stopped
            verdict = reported.status
            self.report.verdict = verdict

        # The last stage's own account travels with every ending, not only the bad
        # ones: a run that finished still said something about how. Under the
        # parallel policy only the CI gate above approves.
        if ci_failure is not None and ci_failure.status == self.api.ci_gate.CI_CONFLICTING:
            return self.stop(
                f"the change request still conflicts with its base after the "
                f"resolution budget of {self.max_iterations} round(s) is spent",
                reported=reported, stop_reason="iteration_limit")
        if not self.parallel and verdict == "APPROVED":
            return self.stop("", self.api.APPROVED_END, reported=reported, stop_reason="approved")
        if ci_failure is not None:
            return self.stop(
                f"the checks of {ci_failure.head_sha[:12]} failed after its clean review "
                f"and the budget of {self.max_iterations} round(s) is spent",
                reported=reported, stop_reason="iteration_limit")
        return self.stop(f"still {verdict} after {self.recorder.iteration} round(s)",
                    reported=reported, stop_reason="iteration_limit")
