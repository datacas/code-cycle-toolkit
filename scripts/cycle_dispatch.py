"""One stage dispatch, evidence checks and bounded recovery."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from .run_cycle import CycleReport, Reported, StageOutcome, StagePolicy

class StageDispatch:
    def stage_policy(self: Any, role: str, request: str | None) -> StagePolicy:
        """The closed command table this stage receives (#142)."""
        base_ref = head_ref = None
        if self.policy_host == "github" and request:
            number = self.api.StagePolicy(role=role, change_request_id=request).pr_number
            if number:
                try:
                    pull = self.checks_provider.pull(self.repo_id, number)
                except self.api.ci_gate.CheckReadError:
                    # Failing closed here denies ref-sensitive Git commands;
                    # it does not prevent the stage from doing other work.
                    pass
                else:
                    if pull.state == "OPEN":
                        base_ref, head_ref = pull.base_ref, pull.head_ref
        return self.api.StagePolicy(
            role=role, repo=self.repo_id, change_request_id=request,
            work_item_id=self.task_id, base_branch=self.base_branch,
            base_ref=base_ref, head_ref=head_ref,
            test_command=self.allowed_test_command,
            verify_prefixes=tuple(self.allowed_verify_commands),
            code_host=self.policy_host, issue_provider=self.policy_provider,
            # The hook records its denials in the journal of this stage, the
            # next one the recorder numbers (#306).
            journal_path=str(self.telemetry.path), cycle_id=self.recorder.cycle_id,
            stage_seq=self.recorder.stage_seq + 1, checkout_branch=self.checkout_branch,
            **(self.user_tool_choices or {}),
        )

    def run(self: Any, role: str, instruction: str = "", *,
            change_request_id: str | None = None, escalated: bool = False,
            resolve_target=None, resolve_escalated: bool = False,
            resolve_target_used_fallback: bool = False,
            escalation_finding_ids: tuple[str, ...] = (),
            publishes: bool = True,
            ) -> tuple[StageOutcome, Reported, str | None, dict[str, Any]]:
        """Dispatch one stage; also say whether the evidence it was given changed.

        `publishes` is false for a dispatch that only re-reads a publication
        already recovered, so the outbox expects nothing from it.
        """
        self.control.move(role)
        resolve_start_head = (
            self.api._git_line(self.cwd, ["rev-parse", "HEAD"], 30)
            if role == "resolve" and self.cwd else None
        )
        stage_start_branch = self.api._git_line(self.cwd, ["branch", "--show-current"], 30) if self.cwd else None
        stage_start_clean = self.cwd is not None and self.api._clean_worktree(self.cwd)
        evidence = ""
        stage_kwargs = self.dispatch_kwargs
        artifact = None
        tampered = None
        # No base to measure against means no file, so no directory either.
        directory = (self.api.diff_directory(self.cwd)
                     if role in self.api.DIFF_ROLES and self.change_bases else None)
        try:
            review_base = None
            if role == "resolve" and self.ci_policy == "parallel" and not self.local_only and change_request_id:
                try:
                    review_base = self.api.resolve_delta_base(
                        self.remote.comments(self.repo_id, change_request_id), self.repo_id,
                        change_request_id, self.trusted_authors, self.cwd)
                except (self.api.CycleDriverError, self.api.ContractError, self.api.HistoryError):
                    # Missing review evidence widens the review; it never skips it.
                    pass
            if role in self.api.DIFF_ROLES:
                artifact = (self.api.write_diff_artifact(self.cwd, self.change_bases, directory)
                            if directory is not None else None)
                evidence = artifact.prompt() if artifact is not None else self.api.NO_DIFF_FILE
                if artifact is not None:
                    stage_kwargs = {**self.dispatch_kwargs, "read_dirs": (str(directory),)}
            if role in {"implement", "resolve"} and self.allowed_test_command:
                stage_kwargs = {
                    **stage_kwargs,
                    "claude_allowed_commands": (self.allowed_test_command,),
                }
            command_policy = self.stage_policy(role, change_request_id)
            stage_kwargs = {**stage_kwargs, "command_policy": command_policy}
            if change_request_id:
                self.journal.change_request_id = change_request_id
            # A resumed or re-dispatched stage sees what earlier runs did (#306).
            redispatched = any(stage.role == role for stage in self.report.stages)
            journal_context = (self.journal.summary()
                               if redispatched or self.journal.has_history() else "")
            verification_context = ""
            if (role in {"implement", "resolve", "rereview"}
                    and self.state.verification_plan is not None and self.state.verification_plan.checks):
                verification_context = (
                    "\nRuntime frozen verification checks (data): "
                    + self.api.json.dumps([check.to_dict() for check in self.state.verification_plan.checks],
                                 ensure_ascii=False)
                    + "\nReuse these identities, including executor/profile, when reporting "
                    "a result for an existing check, even when this stage's dispatch differs. "
                    "The identity names the check's owning execution context. "
                    "A check that needs another context requires a new ID; it does not "
                    "replace a required check. Additions and obligation promotions are allowed.\n")
            outcome = self.recorder.stage(
                role, self.api.compose(role, self.repo_id, self.task_id,
                              instruction + self.state.criteria_context + verification_context,
                              change_request_id=change_request_id,
                              local_only=self.local_only, evidence=evidence,
                              worktree_dir=self.worktree_dir,
                              workspace=self.workspace,
                              allowed_test_command=(
                                  self.allowed_test_command
                                  if role in {"implement", "resolve"} else None),
                              ci_policy=self.ci_policy, resolve_review_base=review_base,
                              command_policy=command_policy,
                              journal_context=journal_context),
                escalated=escalated,
                effort_override=(self.resolve_effort or "high") if role == "resolve" else None,
                target_override=resolve_target if role == "resolve" else None,
                target_override_used_fallback=(resolve_target_used_fallback
                                               if role == "resolve" else False),
                escalation_finding_ids=(escalation_finding_ids
                                         if resolve_escalated else ()),
                resolve_escalated=resolve_escalated,
                **stage_kwargs)
            if artifact is not None:
                tampered = artifact.changed()
        finally:
            # The diff lives for its stage only. A later stage writes its own,
            # from the head that stage will judge.
            if directory is not None:
                self.api._remove_workspace(str(directory))
        self.report.stages.append(outcome)
        attempt_id = (outcome.result.artifacts.get("dispatch_attempt_id")
                      if outcome.result is not None else None)
        if publishes:
            self.expect_publication(role, change_request_id, attempt_id)
        if (role == "resolve" and change_request_id and isinstance(attempt_id, str)
                and resolve_start_head and self.state.history_store is not None):
            try:
                self.state.history_store.record_attempt_start(
                    attempt_id, self.repo_id, change_request_id, role, resolve_start_head)
            except (self.api.sqlite3.Error, self.api.HistoryError):
                # Assisted repair requires this proof; absent storage stays manual.
                pass
        reported = self.api.read_structured_result(outcome.result)
        sandbox_startup_failures = (outcome.result.artifacts.get("sandbox_startup_failures", [])
                                    if outcome.result is not None else [])
        reported = self.api._with_sandbox_startup_failures(reported, sandbox_startup_failures)
        if (role in {"implement", "resolve"} and self.code_host == "github" and not self.local_only
                and self.cwd and reported.completes(role, self.ci_policy)
                and outcome.result is not None
                and self.registry.get(outcome.result.executor).requires_publication_preflight):
            request = self.api.Reported(payload={
                "change_request_id": reported.change_request_id or change_request_id
            }).telemetry_pull_request_id("github")
            expected = (reported.payload or {}).get("head_sha")
            try:
                if not request or not isinstance(expected, str) or not self.api.FULL_SHA.fullmatch(expected):
                    raise self.api.CycleDriverError("publication completion requires a change request and full head SHA")
                remote_head = self.api.probe_change_request(self.repo_id, request, self.cwd).head_sha
                local_head = self.api._git_line(self.cwd, ["rev-parse", "HEAD"], 30)
                if remote_head != expected or local_head != expected:
                    raise self.api.CycleDriverError(
                        f"upload unconfirmed: claimed {expected}, local {local_head}, PR {remote_head}; "
                        "retain the local commit and resume the publishing stage")
                transport = (outcome.result.artifacts if outcome.result else {}).get("publication_transport")
                if transport is not None and not any(
                        item.get("action") == "preflight" and item.get("status") == "ready"
                        for item in transport if isinstance(item, dict)):
                    raise self.api.CycleDriverError("publication preflight inside the stage sandbox was not confirmed")
            except self.api.CycleDriverError as error:
                reported = self.api.Reported(present=True, status="BLOCKED", payload={
                    **(reported.payload or {}), "status": "BLOCKED", "reason": str(error),
                    "publication_pending": True})
        if (role == "resolve" and change_request_id and self.cwd and resolve_start_head
                and stage_start_clean and stage_start_branch and self.state.history_store is not None
                and isinstance(attempt_id, str) and outcome.result is not None
                and outcome.result.process_exited and not outcome.result.asynchronous):
            end_head = self.api._git_line(self.cwd, ["rev-parse", "HEAD"], 30)
            if (end_head and end_head != resolve_start_head
                    and stage_start_branch == self.api._git_line(self.cwd, ["branch", "--show-current"], 30)
                    and self.api._clean_worktree(self.cwd)):
                try:
                    self.state.history_store.record_unpublished_head(
                        attempt_id, self.repo_id, change_request_id, self.cwd, stage_start_branch,
                        resolve_start_head, end_head)
                except (self.api.sqlite3.Error, self.api.HistoryError):
                    pass  # Missing attribution keeps resume conservative.
        if (outcome.succeeded and outcome.result is not None
                and not outcome.result.asynchronous and reported.status is None):
            result = outcome.result
            has_output = bool(result.agent_output.strip())
            code = "unreadable_result" if has_output else "no_error_report"
            detail = "no parseable structured stage result"
            pending = result.artifacts.get("background_command")
            if has_output and pending in {"confirmed", "suspected"}:
                detail = ("background_command_pending: verification command did not "
                          "complete before stage exit"
                          + (" (suspected)" if pending == "suspected" else ""))
            if has_output:
                detail += ": " + self.api.failure_excerpt(result.agent_output)
            outcome.result = self.api.replace(result, error_code=code,
                                     detail=self.api.failure_excerpt(detail))
            if outcome.rows:
                attempt_id = result.artifacts.get("dispatch_attempt_id")
                if attempt_id:
                    self.telemetry.update_dispatch_attempt(
                        attempt_id, {"error_code": code, "outcome": "failed", "lifecycle_state": "failed"},
                        update_id=f"update-{self.api.uuid.uuid4().hex}",
                        correction_reason="parser_correction",
                    )
        self.journal_result(role, outcome, reported, attempt_id)
        findings = self.api._findings(reported.payload, role)
        warnings = self.api._stage_warnings(outcome)
        blocked = self.api._blocked_commands(outcome)
        warnings += [f"blocked command: `{command}`" for command in blocked]
        notice = self.api._preapproved_notice(outcome)
        if notice and notice not in self.noticed:
            self.noticed.add(notice)
            warnings.append(notice)
        reported_status = reported.status if outcome.succeeded else None
        duration = self.status_writer.stage_finished(
            outcome.result, reported_status,
            findings=self.api._findings_line(role, findings), warnings=warnings,
            reviewed_head=self.api._reviewed_head(reported.payload, role).get("reviewed_head_sha"),
        )
        self.report.stage_details.append(self.api.StageReportDetails(
            round=self.recorder.iteration,
            status=reported_status,
            findings=findings,
            warnings=warnings,
            duration_seconds=duration if outcome.result is not None else None,
            blocked_commands=blocked,
        ))
        return outcome, reported, tampered, findings

    def unfinished(self: Any, outcome: StageOutcome, reported: Reported,
                   tampered: str | None) -> CycleReport | None:
        """Stop a stage whose dispatch did not finish here, before its report is read."""
        # Before anything the stage reported is read: its verdict rests on
        # evidence the runtime no longer vouches for.
        if tampered is not None:
            return self.stop(tampered, reported=reported)
        if self.consent is not None and self.consent.pending:
            # The sandbox stayed as it was; a person answers before any rerun.
            self.report.permission_requests = list(self.consent.pending)
            asked = self.consent.pending[0]
            return self.stop(f"the {asked.role} stage needs permission: {asked.grant.id} "
                        f"({asked.grant.resource})", reported=reported,
                        stop_reason="permission_required")
        if not outcome.succeeded:
            blocked_effort = (
                (outcome.result is not None
                 and outcome.result.failure_code == "unsupported_effort")
                or (outcome.decision is not None and outcome.decision.blocked
                    and any("does not support effort" in reason
                            for reason in outcome.decision.reasons))
            )
            return self.stop(self.api._why(outcome), reported=reported,
                        status="BLOCKED" if blocked_effort else self.api.UNRESOLVED_END,
                        stop_reason="dispatch_failed")
        if self.api._started_elsewhere(outcome):
            return self.stop(self.api._elsewhere(outcome), reported=reported)
        return None

    def audit_publication(self: Any, role: str, before, reported: Reported,
                          request: str | None) -> CycleReport | None:
        """Stop when a publication invariant failed during `role` (#261).

        The change request the stage was given, or for `implement` the one it
        reports, is read as well as those whose head is the cycle branch. A
        breach names what moved, never who moved it.
        """
        number = self.api._pull_number(request)
        if number is None and role == "implement":
            number = reported.telemetry_pull_request_id("github")
        try:
            result = self.publication_auditor.verify(before, change_request_id=number)
        except self.api.AuditUnreadable as error:
            self.report.publication_audit = {"status": "unreadable", "role": role}
            return self.stop(f"the publication audit could not read the code host after the "
                        f"{role} stage: {error}", "BLOCKED", reported=reported,
                        stop_reason="publication_unverified")
        evidence = {"role": role, **result.evidence()}
        self.report.publication_audit = evidence
        self.journal.record("stage_result", role=role, stage_seq=self.recorder.stage_seq,
                       component="publication_audit", status=result.status,
                       correlation={"cycle_branch": result.before.cycle_branch,
                                    "default_branch": result.before.default_branch,
                                    "breaches": len(result.breaches)})
        for breach in result.breaches:
            self.journal.record("failure", role=role, stage_seq=self.recorder.stage_seq,
                           component="publication_audit", status=breach.kind,
                           reason=breach.explain(),
                           correlation={"ref": breach.ref, "before": breach.before,
                                        "after": breach.after})
        if result.unverified:
            self.report.note(f"publication audit after {role}: unverified "
                        f"({', '.join(result.unverified)}); the rest was checked")
        if result.status != self.api.AUDIT_VIOLATED:
            return None
        for breach in result.breaches:
            self.report.note(f"  publication breach  {self.api.json.dumps(breach.evidence(), sort_keys=True)}")
        return self.stop(result.explain(role), reported=reported,
                    stop_reason="publication_violation")

    def review_issue(self: Any) -> CycleReport | None:
        """The readiness gate: only a confirmed READY lets `implement` run.

        A READY the runtime cannot confirm — low confidence, or a material
        uncertainty left open — is reviewed once more on the stronger profile.
        When that is not possible, or the second pass cannot confirm it either,
        the cycle stops for a person rather than implementing on a guess.
        """
        escalated = False
        while True:
            try:
                self.select_plan_stage(self.api.ISSUE_REVIEW_ROLE)
            except ValueError as error:
                return self.stop(str(error), "BLOCKED")
            outcome, reported, tampered, _ = self.run(
                self.api.ISSUE_REVIEW_ROLE,
                (self.api.ESCALATED_ISSUE_REVIEW if escalated else "") + self.plan_context(),
                escalated=escalated)
            stopped = self.unfinished(outcome, reported, tampered)
            if stopped is not None:
                return stopped
            readiness = self.api.assess_readiness(reported.payload, self.task_id)
            self.report.readiness = readiness
            if reported.status:
                self.record_plan_stage(self.api.ISSUE_REVIEW_ROLE, reported, outcome)
                self.recorder.record_verdict(self.api.ISSUE_REVIEW_ROLE, reported.status,
                                        **readiness.telemetry_fields())
            if readiness.gate == self.api.READINESS_CONTINUE:
                return None
            if readiness.gate == self.api.READINESS_STOP:
                if not reported.readable:
                    return self.stop(reported.explain(self.api.ISSUE_REVIEW_ROLE), reported=reported)
                if readiness.valid and readiness.status == "NEEDS_REFINEMENT":
                    return self.stop(readiness.explain(), reported=reported,
                                stop_reason="needs_refinement")
                return self.stop(readiness.explain(), reported=reported)
            stronger = self.api.ESCALATION_PROFILES[self.api.ISSUE_REVIEW_ROLE]
            if escalated or outcome.decision.profile == stronger:
                return self.stop(readiness.explain(), reported=reported,
                            stop_reason="readiness_unconfirmed")
            escalated = True

    def advance(self: Any, role: str, instruction: str = "", *,
                change_request_id: str | None = None,
                resolve_target=None, resolve_escalated: bool = False,
                resolve_target_used_fallback: bool = False,
                escalation_finding_ids: tuple[str, ...] = (),
                recover: bool = True) -> tuple[Reported, CycleReport | None]:
        """Run one stage and decide whether the cycle may continue past it.

        Every reason to stop is here rather than repeated per stage: a stop
        condition that has to be remembered four times is one that will be
        missing from the fourth.
        """
        if self.state.pending_recovery is not None:
            ids = [item["id"] for item in self.state.pending_recovery.get("finding_outcomes", [])
                   if item["status"] in {"resolved", "not_applicable"}]
            instruction += (
                f" Verify recovered attempt {self.state.pending_recovery['attempt_id']} at head "
                f"{self.state.pending_recovery['head_sha']}. Execute verification for every claimed "
                f"finding ID: {', '.join(ids) or '(none)'}. Report head_sha and explicit "
                "verified_findings outcomes (resolved, still_open, not_applicable). "
                "A reopened finding follows the normal resolution loop."
            )
        if recover:
            instruction += self.retry_directive(role, change_request_id)
        if (role in {"resolve", "rereview"} and self.code_host == "github"
                and change_request_id is not None and self.cwd is not None):
            try:
                target_head = self.api.probe_change_request(self.repo_id, change_request_id, self.cwd).head_sha
                unpublished = (self.state.history_store.unpublished_heads(
                    self.repo_id, change_request_id, self.cwd,
                    self.api._git_line(self.cwd, ["branch", "--show-current"], 30) or "", target_head or "")
                    if self.state.history_store is not None and role == "resolve" else None)
                if unpublished is None:
                    synchronized = self.api.synchronize_worktree(change_request_id, self.cwd, target_head=target_head)
                else:
                    synchronized = self.api.synchronize_worktree(
                        change_request_id, self.cwd, target_head=target_head, unpublished_heads=unpublished)
            except self.api.WorktreeSyncError as error:
                return self.api.Reported(), self.stop(str(error), "BLOCKED",
                                        stop_reason=error.stop_reason or "stage_not_completed")
            except self.api.CycleDriverError as error:
                return self.api.Reported(), self.stop(str(error), "BLOCKED")
            if synchronized is not None:
                old_head, new_head = synchronized
                self.report.note(f"worktree fast-forwarded: {old_head} -> {new_head}")
                self.journal.record(
                    "stage_result", role=role, stage_seq=self.recorder.stage_seq + 1,
                    component="runtime", status="worktree_synchronized",
                    reason="clean worktree fast-forwarded to the open pull request head",
                    correlation={"old_head_sha": old_head, "new_head_sha": new_head})
        try:
            self.select_plan_stage(role)
        except ValueError as error:
            return self.api.Reported(), self.stop(str(error), "BLOCKED")
        instruction += self.plan_context()
        audited = None
        if self.publication_auditor is not None and role in self.api.PUBLISHING_ROLES:
            try:
                audited = self.publication_auditor.capture(
                    checkout_branch=self.api._git_line(self.cwd, ["branch", "--show-current"], 30),
                    change_request_id=self.api._pull_number(change_request_id))
            except self.api.AuditUnreadable as error:
                return self.api.Reported(), self.stop(
                    f"the publication audit could not read the code host before the {role} "
                    f"stage, so nothing was dispatched: {error}", "BLOCKED",
                    stop_reason="publication_unverified")
        outcome, reported, tampered, findings = self.run(
            role, instruction, change_request_id=change_request_id,
            resolve_target=resolve_target,
            resolve_target_used_fallback=resolve_target_used_fallback,
            resolve_escalated=resolve_escalated,
            escalation_finding_ids=escalation_finding_ids,
            publishes=recover)
        if audited is not None:
            # Before anything the stage reported is read, whatever it reported:
            # a breach is never a `stage_not_completed` (#261, #364).
            stopped = self.audit_publication(role, audited, reported, change_request_id)
            if stopped is not None:
                return reported, stopped
        if role == "resolve" and outcome.decision is not None:
            self.state.previous_resolve_target = outcome.decision.target
            self.state.previous_resolve_used_fallback = outcome.decision.used_fallback
        result = outcome.result
        if role in {"review", "rereview"}:
            dispatched = (result.artifacts.get("dispatch_attempt_id")
                          if result is not None else None)
            # A re-dispatch for a recovered verdict keeps the recovered attempt.
            self.state.review_attempts = (set(self.state.review_attempts) if not recover else set()) | (
                {dispatched} if isinstance(dispatched, str) else set())
        if (recover and self.state.pending_recovery is None and tampered is None and reported.status is None
                and result is not None and not result.asynchronous
                and result.error_code in {"unreadable_result", "no_error_report"}
                and self.code_host == "github" and not self.local_only):
            attempt_id = result.artifacts.get("dispatch_attempt_id")
            try:
                if not attempt_id:
                    raise self.api.CycleDriverError("recovery requires a recorded dispatch attempt")
                if (role == "review" and self.code_host == "github"
                        and change_request_id is not None and self.cwd is not None):
                    target_head = self.api.probe_change_request(self.repo_id, change_request_id, self.cwd).head_sha
                    synchronized = self.api.synchronize_worktree(
                        change_request_id, self.cwd, target_head=target_head)
                    if synchronized is not None:
                        old_head, new_head = synchronized
                        self.report.note(f"worktree fast-forwarded for recovery: {old_head} -> {new_head}")
                        self.journal.record(
                            "stage_result", role=role, stage_seq=self.recorder.stage_seq + 1,
                            component="runtime", status="worktree_synchronized",
                            reason="clean worktree fast-forwarded before review recovery",
                            correlation={"old_head_sha": old_head, "new_head_sha": new_head})
                receipt = self.api.recovery_evidence(
                    self.repo_id, role, attempt_id, change_request_id=change_request_id,
                    cwd=self.cwd, trusted_authors=self.trusted_authors, awaiting_ci=self.parallel)
                self.telemetry.record_recovery(
                    attempt_id, change_request_id=receipt["change_request_id"],
                    head_sha=receipt["head_sha"], comment_id=receipt["comment_id"])
            except (self.api.CycleDriverError, self.api.TelemetryError) as error:
                if isinstance(error, self.api.WorktreeSyncError):
                    return reported, self.stop(f"{role} recovery rejected: {error}", "BLOCKED",
                                          reported=reported,
                                          stop_reason=error.stop_reason or "stage_not_completed")
                return reported, self.stop(f"{role} recovery rejected: {error}", reported=reported)
            self.state.pending_recovery = receipt
            self.report.stage_details[-1].status = "recovered_pending_verification"
            if role in {"review", "rereview"}:
                self.state.review_attempts = {receipt["attempt_id"]}
                return self.advance(role, "Obtain a structured verdict for the recovered review.",
                               change_request_id=receipt["change_request_id"], recover=False)
            if role == "resolve":
                self.history.append(self.api.RunStatuses(self.api.RESOLUTION_RUN, {
                    item["id"]: item["status"] for item in receipt.get("finding_outcomes", [])}))
            return self.api.Reported(present=True, status="recovered_pending_verification",
                            payload={"change_request_id": receipt["change_request_id"],
                                     "head_sha": receipt["head_sha"]}), None
        stopped = self.unfinished(outcome, reported, tampered)
        if stopped is not None:
            return reported, stopped
        if self.state.pending_recovery is not None:
            try:
                current_receipt = self.api.recovery_evidence(
                    self.repo_id, self.state.pending_recovery["stage"], self.state.pending_recovery["attempt_id"],
                    change_request_id=self.state.pending_recovery["change_request_id"],
                    cwd=self.cwd, trusted_authors=self.trusted_authors, awaiting_ci=self.parallel)
            except self.api.CycleDriverError as error:
                return reported, self.stop(f"recovered stage not completed: {error}", reported=reported)
            claims = {item["id"] for item in self.state.pending_recovery.get("finding_outcomes", [])
                      if item["status"] in {"resolved", "not_applicable"}}
            payload = reported.payload or {}
            verified = payload.get("verified_findings", [])
            verified_statuses = self.api._finding_statuses({"verified_findings": verified}, "rereview")
            verified_ids = {item["id"] for item in verified
                            if isinstance(item, dict) and isinstance(item.get("id"), str)} if isinstance(verified, list) else set()
            if (not reported.completes(role, self.ci_policy)
                    or current_receipt != self.state.pending_recovery
                    or payload.get("head_sha") != self.state.pending_recovery["head_sha"]
                    or payload.get("reviewed_head_sha", payload.get("head_sha")) != self.state.pending_recovery["head_sha"]
                    or self.api._git_line(self.cwd, ["rev-parse", "HEAD"], 30) != self.state.pending_recovery["head_sha"]
                    or (claims and (verified_statuses is None or not claims.issubset(verified_ids)))
                    or (reported.status in self.api.CLEAN_REVIEW_STATUSES and verified_statuses
                        and any(verified_statuses.get(finding_id) == "open" for finding_id in claims))):
                return reported, self.stop("recovered stage not completed: verifier did not confirm "
                                      "the recovered head and every claimed finding", reported=reported)
            self.state.pending_recovery = None
        # The dispatch and the work are different facts. This records what the
        # agent said it did; the row already says the call returned. The prose
        # beside it is not recorded: the store holds references and counts.
        if reported.status:
            test_payload = reported.payload
            if (self.state.verification_plan is None and self.allowed_test_command
                    and role in {"implement", "resolve", "rereview"}):
                # Resumes cannot recover criteria revisions until #329. Keep
                # them explicitly open and retain the project's obligation.
                self.state.verification_plan = self.api.verification_contract.VerificationPlan(
                    self.api.verification_contract.Criteria(
                        self.policy_provider or "unknown", "", "not_evaluable",
                        reason="runtime criteria revision unavailable"))
            if role in {"implement", "resolve", "rereview"} and self.state.verification_plan is not None:
                # Mandatory commands come from project policy, never the report.
                if self.allowed_test_command and not any(
                        check.id == "configured-test" for check in self.state.verification_plan.checks):
                    self.state.verification_plan = self.state.verification_plan.extend((
                        self.api.verification_contract.configured_check(
                            self.allowed_test_command,
                            outcome.result.executor if outcome.result else "unknown",
                            outcome.decision.profile if outcome.decision else "unknown"),))
                test_payload, self.state.verification_plan = self.api._normalize_test_payload(
                    test_payload, self.state.verification_plan)
            try:
                self.record_plan_stage(role, reported, outcome, test_payload)
            except ValueError as error:
                return reported, self.stop(str(error), "BLOCKED", reported=reported)
            pull_request_id = (
                reported.telemetry_pull_request_id(self.code_host)
                if role == "implement" else None
            )
            self.recorder.record_verdict(role, reported.status,
                                    **findings,
                                    **(self.api._forecast(reported.payload) if role == "implement" else {}),
                                    **(self.api._legacy_tests(test_payload, boundary_rule=(
                                        self.api.boundary_rule_fired(self.recorder.latest_change)))
                                       if self.state.verification_plan is not None else
                                       self.api._tests(test_payload, boundary_rule=(
                                           self.api.boundary_rule_fired(self.recorder.latest_change)))),
                                    **self.api._checks(reported.payload),
                                    **self.api._reviewed_head(reported.payload, role),
                                    **({"pull_request_id": pull_request_id}
                                       if pull_request_id else {}))
        if not reported.completes(role, self.ci_policy):
            because = reported.explain(role, self.ci_policy)
            if (role == "rereview" and not self.local_only
                    and (self.code_host or self.policy_code_host) == "github"):
                because += self.manual_hint(reported)
            return reported, self.stop(because,
                                  "BLOCKED" if (reported.payload or {}).get("publication_pending") is True
                                  else self.api.UNRESOLVED_END, reported=reported)
        if (role in {"review", "rereview"} and self.execution_plan.stage("security").selected
                and self.plan_managed and (reported.payload or {}).get("security_audit_ran") is not True):
            return reported, self.stop("the required security audit was omitted or unverified",
                                       reported=reported, stop_reason="verification_unverified")
        if recover and role in self.api.COMMENT_STAGES and change_request_id:
            stopped = self.after_publication(
                role, change_request_id,
                result.artifacts.get("dispatch_attempt_id") if result is not None else None,
                reported)
            if stopped is not None:
                return reported, stopped
        # Only a completed stage enters the history the ladder reads. The
        # cycle already stops on anything else, so an unreadable result can
        # neither reset nor advance a count.
        run_statuses = self.api._run_statuses(role, reported.payload)
        if run_statuses is not None:
            self.history.append(run_statuses)
        if role in {"review", "rereview"}:
            self.progress.append(self.api._progress_key(role, reported.payload))
        return reported, None
