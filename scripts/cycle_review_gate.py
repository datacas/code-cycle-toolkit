"""Review reuse and CI/manual completion gates."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from .run_cycle import CycleReport, Reported, ci_gate
from review_contract import RUNTIME_PROVENANCE

class ReviewGate:
    def pull_number(self: Any) -> str | None:
        return self.api._pull_number(self.state.change_request_id)

    def read_ci(self: Any, head: str, role: str | None, *, once: bool) -> ci_gate.GateResult:
        """One read, or the one bounded wait, of the CI gate: recorded and shown.

        No stage runs while it waits and nothing else polls: under the parallel
        policy this is the cycle's only wait on the code host's checks.
        """
        self.control.move("ci")
        number = self.pull_number()
        if number is None:
            result = self.api.ci_gate.GateResult(
                status=self.api.ci_gate.CI_UNREADABLE, head_sha=head,
                reason="the change request names no GitHub pull request number")
        else:
            timing = {}
            if self.ci_clock is not None:
                timing["clock"] = self.ci_clock
            if self.ci_sleep is not None:
                timing["sleep"] = self.ci_sleep
            result = self.api.ci_gate.wait_for_checks(
                self.checks_provider, self.repo_id, number, head, timeout=self.ci_timeout,
                poll_interval=self.ci_poll_interval, once=once,
                on_poll=lambda observed: self.status_writer.ci_observed(
                    observed, waiting=not once and observed.status == self.api.ci_gate.CI_PENDING),
                **timing)
        self.recorder.record_ci(result, mode="once" if once else "wait", role=role)
        self.status_writer.ci_observed(result, waiting=False)
        self.report.note(self.api._ci_line(result, once=once))
        return result

    def receipt_problem(self: Any, role: str, head: str, attempts: set[str],
                        provenance: str = RUNTIME_PROVENANCE) -> str | None:
        """Why no trusted receipt on the change request vouches for a clean review.

        The PR comment is the record of a clean review awaiting its checks, so
        approval never rests on the structured result alone. A trusted receipt
        of one of `attempts` must name this stage, this head, a clean status
        and `provenance`; none of their receipts may say otherwise; and nothing
        may have been published after them.
        """
        number = self.pull_number()
        trusted = self.api.trusted_logins(self.trusted_authors)
        if not trusted:
            return "review.trusted_authors is empty, so no receipt can be authenticated"
        if number is None or not attempts:
            return "the review has no pull request number or recorded attempt to look for"
        try:
            comments = self.remote.comments(self.repo_id, number)
        except self.api.CycleDriverError as error:
            return f"the comments of change request {number} could not be read: {error}"
        receipts = []
        for comment in comments:
            author = (comment.author.strip().casefold()
                      if isinstance(comment.author, str) else None)
            if author not in trusted:
                continue
            try:
                found = self.api.stage_evidence(comment.body)
            except self.api.ContractError:
                if any(attempt in comment.body for attempt in attempts):
                    return "its receipt cannot be read"
                continue
            receipts.extend(receipt for receipt in found
                            if receipt["repo"] == self.repo_id
                            and receipt["change_request_id"] == number)
        mine = [receipt for receipt in receipts if receipt["attempt_id"] in attempts]
        if not mine:
            return "no trusted comment carries its receipt"
        if any(receipt["stage"] != role or receipt["head_sha"] != head
               or receipt["status"] not in self.api.CLEAN_REVIEW_STATUSES for receipt in mine):
            return "its receipt names another stage, head or status"
        if any(self.api.receipt_provenance(receipt) != provenance for receipt in mine):
            return f"its receipt is not a {provenance} receipt"
        if receipts[-1]["attempt_id"] not in attempts:
            return "another stage published after it"
        return None

    def clean_head(self: Any, role: str, reported: Reported) -> tuple[str | None, CycleReport | None]:
        """The head a clean review completed on, once its result and receipt hold."""
        problem = self.api.clean_review_problem(reported.payload, role)
        if problem is not None:
            return None, self.stop(
                f"the {role} reported {reported.status} without a valid completed "
                f"clean review: {problem}", reported=reported)
        head = reported.payload["reviewed_head_sha"]
        problem = self.receipt_problem(role, head, self.state.review_attempts)
        if problem is not None:
            return None, self.stop(
                f"the {role} reported {reported.status} for {head[:12]}, but {problem}; "
                "the receipt on the change request is the record of a clean review",
                "BLOCKED", reported=reported)
        return head, None

    def gate(self: Any, role: str, head: str, attempts: set[str], reported: Reported,
             provenance: str = RUNTIME_PROVENANCE):
        """Wait for the checks of a clean review's head; approve only on a pass.

        Returns `("approved" | "moved" | "failed", result)`, or the report of a
        stop. The final barrier is all of: every required check of `head`
        passed while it was still the change request's head, and the clean
        review's receipt still holds with nothing published after it.
        """
        result = self.read_ci(head, role, once=False)
        number = self.pull_number() or self.state.change_request_id
        if result.status == self.api.ci_gate.CI_PASSED:
            problem = self.receipt_problem(role, head, attempts, provenance)
            if problem is not None:
                return self.stop(
                    f"the checks of {head[:12]} passed, but the clean {role}'s receipt "
                    f"no longer holds: {problem}", "BLOCKED", reported=reported)
            return "approved", result
        if result.status == self.api.ci_gate.CI_HEAD_MOVED:
            return "moved", result
        if result.status == self.api.ci_gate.CI_CONFLICTING:
            return "conflicting", result
        if result.status == self.api.ci_gate.CI_MERGEABILITY_UNKNOWN:
            return self.stop(
                f"GitHub could not determine whether the change request at {head[:12]} "
                f"can merge with its base after bounded retries: {self.api._ci_detail(result)}",
                "BLOCKED", reported=reported, stop_reason="ci_unreadable")
        if result.status == self.api.ci_gate.CI_FAILED:
            return "failed", result
        detail = self.api._ci_detail(result)
        if result.status == self.api.ci_gate.CI_TIMEOUT:
            return self.stop(
                f"the {role} of {head[:12]} is clean and stays valid for that head, but "
                f"its checks had not all passed after {self.api._duration(self.ci_timeout)}: {detail}; "
                f"resume with --from rereview --pr {number} --ci-policy parallel to keep "
                "waiting without another review",
                "BLOCKED", reported=reported, stop_reason="ci_timeout")
        if result.status in {self.api.ci_gate.CI_SKIPPED, self.api.ci_gate.NO_CI_CHECKS_FOUND}:
            return self.stop(
                f"the {role} of {head[:12]} is clean, but its checks cannot pass "
                f"({result.status}): {detail}; approval needs every required check "
                "to succeed on that head",
                self.api.UNRESOLVED_END, reported=reported, stop_reason="ci_not_passing")
        return self.stop(f"the checks of {head[:12]} could not be read: {detail}",
                    "BLOCKED", reported=reported, stop_reason="ci_unreadable")

    def check_strict_mergeability(self: Any, head: str) -> ci_gate.GateResult | CycleReport | None:
        """Check GitHub mergeability before a sequential-policy approval."""
        if self.code_host != "github":
            return None
        number = self.pull_number()
        if number is None:
            return self.stop("the reviewed change request has no GitHub pull request number",
                        "BLOCKED", stop_reason="ci_unreadable")
        for attempt in range(3):
            try:
                pull = self.checks_provider.pull(self.repo_id, number)
            except self.api.ci_gate.CheckReadError as error:
                return self.stop(f"the change request mergeability could not be read: {error}",
                            "BLOCKED", stop_reason="ci_unreadable")
            if pull.state != "OPEN":
                return self.stop(f"the change request is {pull.state.lower()}, not open",
                            "BLOCKED", stop_reason="ci_unreadable")
            if pull.head_sha != head:
                return self.stop(
                    f"the change request head moved from {head[:12]} to "
                    f"{pull.head_sha[:12]} before mergeability was checked",
                    "BLOCKED", stop_reason="head_moved")
            if pull.mergeable is True:
                return None
            if pull.mergeable is False and pull.mergeable_state == "dirty":
                return self.api.ci_gate.GateResult(
                    status=self.api.ci_gate.CI_CONFLICTING, head_sha=head, pr_head_sha=head,
                    reason="the change request conflicts with its base branch")
            if attempt < 2:
                (self.ci_sleep or time.sleep)(min(self.ci_poll_interval, 5))
        return self.api.ci_gate.GateResult(
            status=self.api.ci_gate.CI_MERGEABILITY_UNKNOWN, head_sha=head, pr_head_sha=head,
            reason="GitHub could not determine whether the change request is mergeable")

    def reusable_review(self: Any):
        """The clean review a resumed change request already has, when it holds.

        The receipt on the change request is the record: when the latest
        trusted receipt is a clean review or re-review of the current head and
        the published findings block nothing, the review is not run again and
        the cycle only waits for its checks. Anything else reviews the head.
        Returns `((role, status, head, attempts) | None, stop | None)`.
        """
        number = self.pull_number()
        if number is None:
            return None, self.stop(
                f"the parallel CI policy needs a GitHub pull request number, not "
                f"{self.state.change_request_id}", "BLOCKED", stop_reason="ci_unreadable")
        try:
            comments = self.remote.comments(self.repo_id, number)
            pull = self.checks_provider.pull(self.repo_id, number)
        except (self.api.CycleDriverError, self.api.ci_gate.CheckReadError) as error:
            return None, self.stop(
                f"whether change request {number} already has a clean review could "
                f"not be read: {error}", "BLOCKED", stop_reason="ci_unreadable")
        if pull.state != "OPEN":
            return None, self.stop(
                f"change request {number} is {pull.state.lower()}, not open", "BLOCKED")
        self.recorder.review_reused = False
        try:
            receipts = [receipt for receipt in self.api.trusted_receipts(
                            comments, trusted_authors=self.trusted_authors)
                        if receipt["repo"] == self.repo_id
                        and receipt["change_request_id"] == number]
            published = self.api.recover_comment_history(comments, trusted_authors=self.trusted_authors)
        except self.api.ContractError as error:
            self.report.note(f"  resume   no clean review reused: {self.api.readable(str(error), 200)}")
            return None, None
        latest = receipts[-1] if receipts else None
        # A manual rereview is never reused for being clean and trusted; only
        # `manual_review` accepts one, on its own conditions.
        if (latest is None or latest["stage"] not in {"review", "rereview"}
                or latest["status"] not in self.api.CLEAN_REVIEW_STATUSES
                or self.api.receipt_provenance(latest) != self.api.RUNTIME_PROVENANCE):
            return None, None
        if latest["head_sha"] != pull.head_sha:
            self.report.note(
                f"  resume   the clean {latest['stage']} of {latest['head_sha'][:12]} "
                f"does not hold for the current head {self.api.readable(pull.head_sha, 12)}; "
                "reviewing the current head")
            return None, None
        if any(finding.status == "open" and finding.blocks_approval
               for finding in published.findings):
            self.report.note("  resume   the published findings still block approval; "
                        "reviewing again")
            return None, None
        self.recorder.review_reused = True
        self.report.note(
            f"  resume   reused the clean {latest['stage']} of {latest['head_sha'][:12]} "
            "from its receipt; no review dispatched")
        return (latest["stage"], latest["status"], latest["head_sha"],
                {latest["attempt_id"]}), None

    def manual_step(self: Any, number: str, head: str) -> str:
        """The command that records a manual rereview of `head` (#346)."""
        return (f"python3 manual_rereview.py record --repo {self.repo_id} --pr {number} "
                f"--status APPROVED --confirm-head {head} "
                "--check RESULT:NAME:EVIDENCE")

    def manual_hint(self: Any, reported: Reported) -> str:
        """What closes a cycle whose rereview could not complete here (#346)."""
        number = self.pull_number()
        if number is None:
            return ""
        head = (reported.payload or {}).get("head_sha")
        if not isinstance(head, str) or self.api.FULL_SHA.fullmatch(head) is None:
            head = "<current head SHA>"
        return ("; a rereview done outside the runtime has no receipt until it is "
                f"recorded with `{self.manual_step(number, head)}`, then resume with "
                f"--from rereview --pr {number}")

    def manual_review(self: Any):
        """The confirmed manual rereview a resume closes on, when it holds (#346).

        Considered only when the latest trusted receipt of the change request
        is a manual one; anything else returns `(None, None)` and the resume
        goes on as before. It is accepted only when the account that published
        it is the operator it names, it is a clean rereview confirmed for the
        change request's current head, nothing readable was published after
        it, and every published finding that still blocks approval is one it
        verified. Otherwise the cycle stops for a person with the missing
        step: a manual rereview is never replaced by a runtime one behind the
        operator's back, nor upgraded to one.
        Returns `((role, status, head, attempts) | None, stop | None)`.
        """
        number = self.pull_number()
        trusted = self.api.trusted_logins(self.trusted_authors)
        if number is None or not trusted:
            return None, None
        try:
            comments = self.remote.comments(self.repo_id, number)
        except (self.api.CycleDriverError, self.api.HistoryError):
            return None, None
        latest, publisher = None, None
        for comment in comments:
            author = (comment.author.strip().casefold()
                      if isinstance(comment.author, str) else None)
            if author not in trusted:
                continue
            try:
                found = self.api.stage_evidence(comment.body)
            except self.api.ContractError:
                # Unreadable, so it cannot be ruled out as the latest.
                latest, publisher = None, None
                continue
            for receipt in found:
                if receipt["repo"] == self.repo_id and receipt["change_request_id"] == number:
                    latest, publisher = receipt, author
        if latest is None or self.api.receipt_provenance(latest) != self.api.MANUAL_PROVENANCE:
            return None, None
        try:
            pull = self.checks_provider.pull(self.repo_id, number)
        except self.api.ci_gate.CheckReadError as error:
            return None, self.stop(
                f"the manual rereview of change request {number} could not be "
                f"checked against its head: {error}", "BLOCKED",
                stop_reason="ci_unreadable")
        if pull.state != "OPEN":
            return None, self.stop(
                f"change request {number} is {pull.state.lower()}, not open", "BLOCKED")
        reviewed = latest["head_sha"]
        step = self.manual_step(number, pull.head_sha)

        def rejected(problem: str) -> tuple[None, CycleReport]:
            return None, self.stop(
                f"the latest receipt of change request {number} is a manual rereview "
                f"of {reviewed[:12]}, but {problem}; a manual rereview is never "
                "upgraded to a runtime one", self.api.UNRESOLVED_END,
                stop_reason="manual_rereview_rejected")

        if latest["operator"].strip().casefold() != publisher:
            return rejected(
                f"it names operator {self.api.readable(latest['operator'], 40)!r} while "
                f"{self.api.readable(publisher or '', 40)!r} published it")
        if reviewed != pull.head_sha:
            return rejected(
                f"the current head is {self.api.readable(pull.head_sha, 12)}: a new head "
                "invalidates the confirmation. Review the current head and record "
                f"it with `{step}`, or resume with --from review to have the "
                "runtime review it")
        if latest["status"] not in self.api.CLEAN_REVIEW_STATUSES:
            return rejected(
                f"it requested changes ({latest['status']}). Resolve them with "
                f"--from resolve --pr {number}, then rereview the new head")
        try:
            published = self.api.recover_comment_history(comments, trusted_authors=self.trusted_authors)
        except self.api.ContractError as error:
            return rejected(f"the published review history cannot be read: "
                            f"{self.api.readable(str(error), 200)}")
        verified = {item["id"] for item in latest.get("finding_outcomes", [])
                    if item["status"] in {"resolved", "not_applicable"}}
        blocking = sorted(finding.id for finding in published.findings
                          if finding.status == "open" and finding.blocks_approval
                          and finding.id not in verified)
        if blocking:
            return rejected(
                f"published findings still block approval and it verified none of "
                f"them: {', '.join(blocking)}. Record their outcomes with "
                "--finding REV-NNN=resolved in a new manual record")
        self.report.review_provenance = self.api.MANUAL_PROVENANCE
        self.recorder.review_provenance = self.api.MANUAL_PROVENANCE
        declared = "; ".join(
            f"{self.api.readable(item['name'], 60)} {item['result']}" for item in latest["checks"])
        self.report.note(
            f"  resume   accepted the manual rereview of {reviewed[:12]} by "
            f"{self.api.readable(latest['operator'], 40)} ({latest['attempt_id']}); checks "
            f"declared by the operator, not run by the runtime: {declared}")
        self.journal.record(
            "stage_result", role="rereview", stage_seq=self.recorder.stage_seq,
            component="runtime", status="manual_rereview_accepted",
            reason="the confirmed manual rereview of the current head is the review "
                   "the cycle closes on, after its required checks pass",
            correlation={"head_sha": reviewed, "attempt_id": latest["attempt_id"],
                         "review_provenance": self.api.MANUAL_PROVENANCE})
        return ("rereview", latest["status"], reviewed, {latest["attempt_id"]}), None

    def close_manual(self: Any, clean, reported: Reported) -> CycleReport:
        """Approve a confirmed manual rereview only once its head's checks pass.

        The same CI gate as a runtime review, under either CI policy: a manual
        rereview never bypasses a required check, a conflict, or a later
        publication. What it cannot approve goes to a person, never to a
        runtime resolution it did not ask for.
        """
        role, verdict, head, attempts = clean
        self.report.verdict = verdict
        number = self.pull_number() or self.state.change_request_id
        outcome = self.gate(role, head, attempts, reported, provenance=self.api.MANUAL_PROVENANCE)
        if isinstance(outcome, self.api.CycleReport):
            return outcome
        kind, result = outcome
        if kind == "approved":
            return self.stop("", self.api.APPROVED_END, reported=reported, stop_reason="approved")
        if kind == "moved":
            new = self.api.readable(result.pr_head_sha or "", 40)
            return self.stop(
                f"the change request's head moved after the manual rereview of "
                f"{head[:12]}, which invalidates its confirmation; review the new "
                f"head and record it with `{self.manual_step(number, new)}`",
                self.api.UNRESOLVED_END, reported=reported, stop_reason="head_moved")
        detail = self.api._ci_detail(result)
        if kind == "conflicting":
            detail = "the change request conflicts with its base branch"
        return self.stop(
            f"the manual rereview of {head[:12]} is clean, but {detail}; a manual "
            f"rereview never bypasses the required checks: resolve with --from "
            f"resolve --pr {number}", self.api.UNRESOLVED_END, reported=reported,
            stop_reason="ci_not_passing")
