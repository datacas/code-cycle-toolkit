"""Cycle lifetime, shared state and reporting."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from .cycle_journal import events as journal_events
    from .plan_service import reconcile_plan
    from .orca_plan import reconcile_outcome
    from .run_cycle import CycleHistoryStore, CycleInterrupted, CycleReport, Reported, RunStatuses
    from . import verification_plan as verification_contract


if TYPE_CHECKING:
    from .cycle_state import SessionState
    from .cycle_dispatch import StageDispatch
    from .cycle_publication import PublicationHistory
    from .cycle_review_gate import ReviewGate
    from .cycle_loop import CycleControl, StageLoop
    from .workspace_setup import commit_setup, own_file
    from .cycle_plan import PlanRuntime
else:
    from cycle_journal import events as journal_events
    from plan_service import reconcile_plan
    from orca_plan import reconcile_outcome
    from cycle_state import SessionState
    from cycle_dispatch import StageDispatch
    from cycle_publication import PublicationHistory
    from cycle_review_gate import ReviewGate
    from cycle_loop import CycleControl, StageLoop
    from workspace_setup import commit_setup, own_file
    from cycle_plan import PlanRuntime

class CycleSession(PlanRuntime, StageDispatch, PublicationHistory, ReviewGate, StageLoop):
    def __init__(self, api: Any, options: dict[str, Any]):
        # The facade supplies dependencies so existing integrations keep
        # the same public names and injection points.
        self.api = api
        self.orca_context = options.get("orca_context")
        self.orca_action = options.get("orca_action", "launch")
        self.state = SessionState()
        self.control = CycleControl()
        self.repo_id = options["repo_id"]
        self.task_id = options["task_id"]
        self.signals = options["signals"]
        self.telemetry = options["telemetry"]
        self.profiles = options["profiles"]
        self.registry = options["registry"]
        self.availability = options["availability"]
        self.mode = options["mode"]
        self.routing_strategy = options["routing_strategy"]
        self.policy = options["policy"]
        self.max_iterations = options["max_iterations"]
        self.cwd = options["cwd"]
        self.timeout = options["timeout"]
        self.verification_answers = options.get("verification_answers") or {}
        if any(value not in {"accept", "outside"} for value in self.verification_answers.values()):
            raise ValueError("verification answers must be accept or outside")
        self.local_only = options["local_only"]
        self.change_bases = options["change_bases"]
        self.verification_available = options["verification_available"]
        self.jev = options["jev"]
        self.shadow = options["shadow"]
        self.verbose = options["verbose"]
        self.progress_interval = options["progress_interval"]
        self.start_from = options["start_from"]
        self.state.change_request_id = options["change_request_id"]
        self.issue_review = options["issue_review"]
        self.continue_work = options["continue_work"]
        self.worktree_dir = options["worktree_dir"]
        self.workspace = options["workspace"]
        self.work_item_provider = options["work_item_provider"]
        self.work_item_repository = options["work_item_repository"]
        self.code_host = options["code_host"]
        self.trusted_authors = options["trusted_authors"]
        self.resolve_effort = options["resolve_effort"]
        self.comment_reader = options["comment_reader"]
        self.allowed_test_command = options["allowed_test_command"]
        self.allowed_verify_commands = options["allowed_verify_commands"]
        self.user_tool_choices = options["user_tool_choices"]
        self.base_branch = options["base_branch"]
        self.policy_code_host = options["policy_code_host"]
        self.history_remote = options["history_remote"]
        self.ci_policy = options["ci_policy"]
        self.ci_timeout = options["ci_timeout"]
        self.consent = options["consent"]
        self.ci_checks = options["ci_checks"]
        self.ci_poll_interval = options["ci_poll_interval"]
        self.ci_clock = options["ci_clock"]
        self.ci_sleep = options["ci_sleep"]
        self.repair_approval = options["repair_approval"]
        self.published_change_request = options["published_change_request"]
        self.publication_auditor = options["publication_auditor"]
        self.publication_observer = options["publication_observer"]
        self.criteria_reader = options["criteria_reader"]
        self.issue_snapshot_reader = options.get("issue_snapshot_reader")
        self.api.validate_start(self.start_from, self.state.change_request_id, local_only=self.local_only,
                       continue_work=self.continue_work)
        self.api.check_ci_policy(self.ci_policy, self.code_host)
        self.issue_review = self.api.IssueReviewMode(self.issue_review)
        if self.shadow is None and self.jev is not None and self.jev.enabled:
            self.shadow = self.api.JevShadow(self.jev)
        if self.local_only:
            self.api.validate_local_only_cwd(self.cwd)
        self.registry = self.registry or self.api.Registry()
        self.policy = self.policy or self.api.ReadinessPolicy.for_mode(self.mode)
        self.probes = self.registry.probe_all()
        if self.availability is None:
            self.availability = self.registry.availability(self.policy, self.probes)
        if (self.consent is not None and self.consent.store is not None
                and (self.continue_work or self.start_from != "implement")):
            try:
                self.consent.cycle_id = self.consent.store.cycle_for(self.repo_id, self.task_id)
            except self.api.GrantError:
                pass
        self.recorder = self.api.CycleRecorder(
            self.telemetry, self.repo_id, self.task_id, self.signals,
            availability=self.availability, registry=self.registry, mode=self.mode, policy=self.policy,
            probes=self.probes, profiles=self.profiles, local_only=self.local_only,
            continue_work=self.continue_work,
            started_from=self.start_from,
            routing_strategy=self.routing_strategy,
            change_observer=(
                (lambda: self.api.collect_change_signals(self.cwd, self.change_bases))
                if self.change_bases else None
            ),
            verification_available=self.verification_available,
            work_item_provider=self.work_item_provider,
            work_item_repository=self.work_item_repository,
            shadow=self.shadow,
            skills_by_role=self.api.SKILL_FOR_ROLE,
            change_request_id=self.state.change_request_id,
            resolve_effort=self.resolve_effort,
            ci_policy=self.ci_policy,
            consent=self.consent,
            publication_observer=self.publication_observer,
        )
        self.journal = self.api.Journal(self.telemetry.path, self.repo_id, self.task_id, self.recorder.cycle_id,
                          change_request_id=self.state.change_request_id)
        self.recorder.journal = self.journal
        self.previous_consent_observer = self.consent.observer if self.consent is not None else None
        if self.consent is not None:
            self.consent.observer = self.journal.consent_event
            # Read before this run asks anything: a `--grant` answer is stored only
            # for a scope an earlier run of the work item showed the person.
            self.consent.asked = self.consent.asked | self.api.asked_scopes(self.telemetry.path, self.repo_id, self.task_id)
            for self.grant_id, self.answer in self.consent.answers.items():
                self.journal.record("decision_answered", component="--grant",
                               decision_key=f"grant:{self.grant_id}", status=self.answer)
        self.checkout_branch = self.api._git_line(self.cwd, ["branch", "--show-current"], 30)
        self.consent_scopes = None
        if self.consent is not None and self.consent.store is not None:
            try:
                self.consent_scopes = self.consent.store.allowed_scopes(self.repo_id)
            except self.api.GrantError:
                pass
        self.journal.reconcile_permissions(consents=self.consent_scopes, user_choices=self.user_tool_choices,
                                      branch=self.checkout_branch)
        self.status_writer = self.api.CycleStatusWriter(
            self.telemetry.path, self.recorder.cycle_id, self.repo_id, self.task_id,
            progress_interval=self.progress_interval, verbose=self.verbose,
            workspace=self.cwd or self.api.Path.cwd(),
        )
        self.recorder.stage_started = self.progress_stage_started
        self.recorder.on_dispatch_started = commit_setup
        self.recorder.on_progress = self.status_writer.activity
        self.recorder.on_workspace = self.status_writer.workspace_observed
        own_file(self.status_writer.path, "cycle status before first dispatch")
        self.status_writer.start()
        self.report = self.api.CycleReport(repo_id=self.repo_id, task_id=self.task_id)
        self.noticed: set[str] = set()
        self.dispatch_kwargs = {}
        if self.cwd:
            self.dispatch_kwargs["cwd"] = self.cwd
            self.common_directory = self.api._git_common_directory(self.cwd)
            if self.common_directory:
                self.worktree_directory = self.api._git_worktree_directory(self.cwd)
                self.dispatch_kwargs["writable_dirs"] = tuple(dict.fromkeys(
                    directory for directory in (self.common_directory, self.worktree_directory)
                    if directory
                ))
        if self.timeout is not None:
            self.dispatch_kwargs["timeout"] = self.timeout
        self.state.previous_resolve_target = None
        self.state.previous_resolve_used_fallback = False
        self.policy_host = self.code_host or self.policy_code_host
        self.policy_provider = self.work_item_provider or (
            "github" if self.policy_host == "github" and str(self.task_id).isdecimal() else None)
        self.state.verification_plan = None
        self.state.criteria_context = ""
        if self.history_remote is not None:
            self.remote, self.tracking = self.history_remote, True
        elif self.comment_reader is not None:
            self.remote, self.tracking = self.api.ReaderRemote(self.comment_reader, self.cwd), False
        elif self.code_host == "github":
            self.remote, self.tracking = self.api.GitHubHistoryRemote(self.cwd), True
        else:
            self.remote, self.tracking = self.api.ReaderRemote(self.api.read_change_request_comments, self.cwd), False
        self.tracking = self.tracking and not self.local_only
        self.state.history_store = self.api.CycleHistoryStore(self.telemetry.path)
        self.state.history_lock = None
        self.state.pending_recovery = None
        self.state.review_attempts = set()
        self.parallel = self.ci_policy == "parallel" and not self.local_only
        self.history: list[RunStatuses] = []
        self.progress: list[tuple[str, frozenset[str]] | None] = []
        self.issue_review_fields = {}
        if self.continue_work:
            # The interrupted run already passed, or skipped, the readiness gate.
            self.issue_review_fields["continued"] = True
        elif self.start_from == "implement":
            self.issue_review_fields["issue_review"] = self.api.issue_review_decision(
                self.issue_review, self.signals)
        self.checks_provider = self.ci_checks if self.ci_checks is not None else self.api.ci_gate.GitHubChecks(self.cwd)
        self.initialize_plan(options.get("execution_plan"), options.get("security_rule"))

    def progress_stage_started(self, role, decision) -> None:
        iteration = self.recorder.iteration if role in {"review", "resolve", "rereview"} else None
        self.status_writer.stage_started(role, decision, iteration=iteration)

    def journal_result(self, role, outcome, reported, attempt_id) -> None:
        payload = reported.payload or {}
        # A stage's report cannot redirect attribution or retention. The driver
        # owns this reference and accepts a new implementation's validated ID
        # only after its existing continuation and publication checks below.
        self.journal.change_request_id = self.state.change_request_id
        self.journal.record("stage_result", role=role, stage_seq=self.recorder.stage_seq,
                       attempt_id=attempt_id, component="runtime", status=reported.status,
                       detail=reported.reason, correlation={"head_sha": payload.get("head_sha")})
        transport = (outcome.result.artifacts if outcome.result else {}).get("publication_transport", [])
        for event in transport if isinstance(transport, list) else []:
            if not isinstance(event, dict):
                continue
            attempts = event.get("attempts", [])
            for attempt in attempts if isinstance(attempts, list) else []:
                if not isinstance(attempt, dict):
                    continue
                command = attempt.get("command")
                if not isinstance(command, list) or not all(isinstance(word, str) for word in command):
                    continue
                self.journal.record(
                    "stage_result", role=role, stage_seq=self.recorder.stage_seq,
                    attempt_id=attempt_id, component="publication_transport",
                    status=event.get("status"), command=self.api.shlex.join(command),
                    detail=attempt.get("error"),
                    correlation={"head_sha": event.get("head_sha"),
                                 "remote_head_sha": event.get("remote_head_sha"),
                                 "exit_code": attempt.get("exit_code")})
        # The consent record the runtime wrote into the result is removed before
        # the role's contract judges it; the cycle record keeps it here.
        outcomes = reported.runtime.get("permission_outcomes")
        for item in outcomes if isinstance(outcomes, list) else []:
            if not isinstance(item, dict):
                continue
            checks = item.get("checks")
            self.journal.record(
                "stage_result", role=role, stage_seq=self.recorder.stage_seq,
                attempt_id=attempt_id, component="permission_outcome",
                status=item.get("answer") if isinstance(item.get("answer"), str) else None,
                command=item.get("command") if isinstance(item.get("command"), str) else None,
                correlation={"grant": item.get("grant") if isinstance(item.get("grant"), str) else None,
                             "essential": str(item.get("essential")).lower(),
                             "class": item.get("class") if isinstance(item.get("class"), str) else None,
                             "rule": item.get("rule") if isinstance(item.get("rule"), str) else None,
                             "operation": item.get("operation")
                             if isinstance(item.get("operation"), str) else None,
                             "checks": ",".join(str(check) for check in checks)
                             if isinstance(checks, list) and checks else None})
            self.note_declined_check(item, payload.get("head_sha"))
        questions = payload.get("questions")
        if isinstance(questions, list):
            for question in questions:
                if isinstance(question, dict):
                    self.journal.record("decision_asked", role=role, stage_seq=self.recorder.stage_seq,
                                   attempt_id=attempt_id, component="stage_result",
                                   decision_key=f"{attempt_id}:{question.get('id')}",
                                   status="unanswered", reason=question.get("prompt"),
                                   detail=self.api.json.dumps(question.get("options")))

    #: Answers that let a configured check run.
    _CHECK_ALLOWS = frozenset({"cycle", "persistent", "forever"})

    def note_declined_check(self, outcome: dict[str, Any], head_sha: Any) -> None:
        """Keep a configured check the person declined as pending verification (#398).

        The runtime's own permission outcome decides, whatever the agent put in
        its tests: a denied, unanswered or unidentified configured check stays
        `unverified` to the end of the cycle, and a later allow clears it.
        """
        check = outcome.get("check")
        answer = outcome.get("answer")
        if not isinstance(check, str) or not check:
            return
        if answer in self._CHECK_ALLOWS:
            self.state.declined_checks.pop(check, None)
            return
        if answer in {None, "pending"} and not outcome.get("executor_hook"):
            # A refusal still unanswered is asked before the stage reruns, and
            # an unanswered question stops the cycle as permission_required.
            # Only Codex's pre-dispatch question runs on without its answer.
            return
        grant = outcome.get("grant") if isinstance(outcome.get("grant"), str) else None
        self.state.declined_checks[check] = {
            "check": check, "command": outcome.get("command"),
            "head_sha": head_sha if isinstance(head_sha, str) else None,
            "cause": f"permission {grant or 'unknown'}: {answer or 'pending'}",
            "decision": "declined", "grant": grant,
            **({"config_sha256": outcome["config_sha256"]}
               if isinstance(outcome.get("config_sha256"), str) else {})}

    def declined_summary(self) -> str:
        """The declined configured checks and how to authorize them and resume."""
        parts = []
        for item in self.state.declined_checks.values():
            grant = item.get("grant")
            how = (f"to authorize it, revoke a stored denial with `consent_control.py revoke "
                   f"--repo {self.repo_id} --executor claude|codex --grant-id {grant}`, resume, "
                   f"and allow it when asked (or pass --grant {grant}=persistent)"
                   if grant else "authorize it and resume")
            parts.append(f"configured check {item['check']} remains unverified "
                         f"({item['cause']}); {how}")
        return "; ".join(parts)

    def record_reconciliation(self, role: str, reported: Reported, outcome) -> None:
        """Record what became of a stage that reported `BLOCKED` after refusals (#364).

        The original status, the reason, the classified denials and the observed
        SHAs go to the journal; a reconciled stage is also named in the report so
        a person sees that its `BLOCKED` was not taken at face value.
        """
        record = reported.runtime.get("reconciliation")
        if not isinstance(record, dict):
            return
        attempt_id = (outcome.result.artifacts.get("dispatch_attempt_id")
                      if outcome.result is not None else None)
        observed = record.get("shas")
        shas: dict[str, Any] = observed if isinstance(observed, dict) else {}
        denials = [item for item in record.get("denials") or [] if isinstance(item, dict)]
        reconciled = record.get("reconciled") is True
        self.journal.record(
            "stage_result", role=role, stage_seq=self.recorder.stage_seq, attempt_id=attempt_id,
            component="reconciliation", status="reconciled" if reconciled else "not_reconciled",
            reason=record.get("reason"),
            detail="; ".join(f"{item.get('class')}: {item.get('command')} "
                             f"(rule {item.get('rule')}, operation {item.get('operation')})"
                             for item in denials),
            correlation={"original_status": record.get("original_status"),
                         "completed_status": record.get("completed_status"),
                         "local_head": shas.get("local"), "remote_head": shas.get("remote"),
                         "pull_request_head": shas.get("pull_request"),
                         "reported_head": shas.get("reported"),
                         "denial_classes": ",".join(sorted({str(item.get("class")) for item in denials}))})
        if reconciled:
            self.report.note(f"  reconciled  the {role} stage reported BLOCKED but "
                             f"{record.get('completed_status')} is verified: {record.get('reason')}")

    def stop(self, because: str, status: str = "HUMAN_INTERVENTION",
             reported: Reported | None = None, *,
             stop_reason: str = "stage_not_completed",
             refusals_are_warnings: bool = False) -> CycleReport:
        """Close the cycle. `refusals_are_warnings` is for a stop whose cause is a
        valid stage result: the commands the stage had refused are then listed as
        warnings, never as part of why the cycle stopped."""
        if status == self.api.APPROVED_END:
            barrier = self.verification_barrier()
            if barrier is not None:
                return barrier
            declined = [item for item in self.state.declined_checks.values()
                        if not any(pending.get("check") == item["check"]
                                   for pending in self.report.pending_verification)]
            if self.report.pending_verification or declined:
                accepted = bool(self.report.pending_verification)
                self.report.pending_verification = [*self.report.pending_verification, *declined]
                status = "VERIFICATION_INCOMPLETE"
                stop_reason = "verification_unverified"
                because = ("Required verification accepted as not_verified; other applicable stages and CI completed."
                           if accepted else
                           "A declined configured check remains not_verified; other applicable stages and CI completed.")
            if status == self.api.APPROVED_END and self.execution_plan.objective != "merge_ready":
                status = self.api.UNRESOLVED_END
                stop_reason = ("approved" if self.execution_plan.objective == "verified_change"
                               else "local_only")
                because = f"objective {self.execution_plan.objective} completed before merge readiness"
        if self.state.declined_checks:
            # Any final result names the pending check and how to resume (#398).
            because += "; " + self.declined_summary()
        self.save_plan("stopped", stop_reason=stop_reason)
        self.control.move("stop")
        if self.report.stages and self.report.stage_details:
            # The exact command a permission stopped, in the stop line, so a
            # person can authorize it once and resume (#142).
            denied = self.report.stage_details[-1].blocked_commands
            if denied and not refusals_are_warnings:
                more = f" (and {len(denied) - 1} more)" if len(denied) > 1 else ""
                because += (f"; blocked command in the {self.report.stages[-1].role} stage: "
                            f"`{denied[0]}`{more}")
        self.report.stopped_because = because
        self.report.status = status
        self.report.iterations = self.recorder.iteration
        self.report.stop_reason = stop_reason
        if reported is not None:
            self.report.reason = reported.reason
        refusals = self.journal.refusals(stage_seq=self.recorder.stage_seq)
        for refusal in refusals:
            citation = (f"refused by {refusal['component']}: {refusal['command'] or '(no command)'}"
                        f" — {refusal['reason']}")
            if refusals_are_warnings:
                # The stage's own line already lists a blocked command as a
                # warning; the hook's refusal joins it there.
                if self.report.stage_details and citation not in self.report.stage_details[-1].warnings:
                    self.report.stage_details[-1].warnings.append(citation)
            elif citation not in because:
                because += f"; {citation}"
        self.report.stopped_because = because
        self.journal.record("stop", stage_seq=self.recorder.stage_seq,
                       role=self.report.stages[-1].role if self.report.stages else None,
                       component="runtime", status=status, reason=because,
                       detail=self.report.reason, correlation={"stop_reason": stop_reason})
        try:
            from cycle_journal import explain as explain_journal
            self.report.journal_explanation = explain_journal(
                self.journal.path, self.repo_id, self.task_id, self.recorder.cycle_id)
        except Exception:
            pass
        for error in self.journal.errors:
            self.report.note(f"  journal  {error}")
        self.recorder.close(status, stop_reason=stop_reason, **self.issue_review_fields)
        self.status_writer.finish(
            status,
            stop_reason=stop_reason,
            reason=because,
            readiness=self.report.readiness,
            review_provenance=self.report.review_provenance,
        )
        return self.report

    def interrupted(self, interruption: CycleInterrupted) -> CycleReport:
        """Close an interrupted cycle so its record says what happened."""
        self.control.move("interrupt")
        outcome = interruption.outcome
        where = "between stages"
        if outcome is not None:
            where = f"during {outcome.role}"
            self.report.stages.append(outcome)
            duration = self.status_writer.stage_finished(outcome.result, None)
            self.report.stage_details.append(self.api.StageReportDetails(
                round=self.recorder.iteration, status=None, duration_seconds=duration))
        return self.stop(
            f"interrupted by {interruption.signal_name} {where}; the checkout "
            "was left as it was",
            stop_reason="interrupted")

    def execute(self) -> Any:
        try:
            if self.orca_action != "launch":
                # Resume/cancellation belong to the existing coordinator.
                # Reconcile durable obligations before any attempt to launch;
                # no documented live capability is assumed by this driver.
                try:
                    record = self.plan_store.latest_plan(
                        self.repo_id, self.task_id, self.state.change_request_id)
                    if record is None or self.orca_context is None:
                        raise ValueError("Orca resume needs an existing context and persisted plan")
                    launches = [row["correlation"] for row in journal_events(
                        self.telemetry.path, self.repo_id, task_id=self.task_id,
                        kinds=("stage_result",)) if row.get("component") == "orca_launch"]
                    launch = launches[-1] if launches else None
                    if (launch is None
                            or launch.get("run_id") != self.orca_context.run_id
                            or launch.get("task_id") != self.orca_context.task_id
                            or launch.get("plan_id") != record["plan_id"]):
                        raise ValueError("Orca resume does not match the persisted launch identities")
                    self.execution_plan = reconcile_plan(record, self.authorized_bindings(),
                        head=self.plan_head)
                    self.state.verification_plan = self.execution_plan.verification
                    self.plan_conclusion = self.execution_plan.metadata.get("verification_conclusion")
                    self.save_plan("recovered")
                    outcome = reconcile_outcome(self.orca_context, self.execution_plan,
                        cancel=self.orca_action == "cancel")
                    return self.stop(outcome["reason"], "BLOCKED",
                                     stop_reason="stage_not_completed")
                except (ValueError, self.api.HistoryError) as error:
                    return self.stop(str(error), "BLOCKED", stop_reason="readiness_unconfirmed")
            try:
                self.api.journal_cleanup(self.telemetry.path, self.repo_id,
                                (lambda request: self.checks_provider.pull(self.repo_id, request).state)
                                if self.policy_host == "github" else None)
            except Exception as error:
                self.report.note(f"  journal  cleanup failed ({type(error).__name__}); detail retained")
            try:
                if self.start_from not in self.api.RESUMES:
                    self.prepare_plan()
            except (self.api.verification_contract.VerificationError,
                    self.api.HistoryError, ValueError) as error:
                return self.stop(str(error), "BLOCKED", stop_reason="readiness_unconfirmed")
            return self.drive()
        except self.api.CycleInterrupted as interruption:
            return self.interrupted(interruption)
        finally:
            self.journal.end_cycle()
            if self.consent is not None:
                self.consent.observer = self.previous_consent_observer
            self.release_lock()
