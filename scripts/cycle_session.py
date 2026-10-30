"""Cycle lifetime, shared state and reporting."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from .run_cycle import CycleHistoryStore, CycleInterrupted, CycleReport, Reported, RunStatuses
    from . import verification_plan as verification_contract


if TYPE_CHECKING:
    from .cycle_state import SessionState
    from .cycle_dispatch import StageDispatch
    from .cycle_publication import PublicationHistory
    from .cycle_review_gate import ReviewGate
    from .cycle_loop import CycleControl, StageLoop
    from .workspace_setup import commit_setup, own_file
else:
    from cycle_state import SessionState
    from cycle_dispatch import StageDispatch
    from cycle_publication import PublicationHistory
    from cycle_review_gate import ReviewGate
    from cycle_loop import CycleControl, StageLoop
    from workspace_setup import commit_setup, own_file

class CycleSession(StageDispatch, PublicationHistory, ReviewGate, StageLoop):
    def __init__(self, api: Any, options: dict[str, Any]):
        # The facade supplies dependencies so existing integrations keep
        # the same public names and injection points.
        self.api = api
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
        self.criteria_reader = options["criteria_reader"]
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
        questions = payload.get("questions")
        if isinstance(questions, list):
            for question in questions:
                if isinstance(question, dict):
                    self.journal.record("decision_asked", role=role, stage_seq=self.recorder.stage_seq,
                                   attempt_id=attempt_id, component="stage_result",
                                   decision_key=f"{attempt_id}:{question.get('id')}",
                                   status="unanswered", reason=question.get("prompt"),
                                   detail=self.api.json.dumps(question.get("options")))

    def stop(self, because: str, status: str = "HUMAN_INTERVENTION",
             reported: Reported | None = None, *,
             stop_reason: str = "stage_not_completed") -> CycleReport:
        self.control.move("stop")
        if self.report.stages and self.report.stage_details:
            # The exact command a permission stopped, in the stop line, so a
            # person can authorize it once and resume (#142).
            denied = self.report.stage_details[-1].blocked_commands
            if denied:
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
            if citation not in because:
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

    def execute(self):
        try:
            try:
                self.api.journal_cleanup(self.telemetry.path, self.repo_id,
                                (lambda request: self.checks_provider.pull(self.repo_id, request).state)
                                if self.policy_host == "github" else None)
            except Exception as error:
                self.report.note(f"  journal  cleanup failed ({type(error).__name__}); detail retained")
            return self.drive()
        except self.api.CycleInterrupted as interruption:
            return self.interrupted(interruption)
        finally:
            self.journal.end_cycle()
            if self.consent is not None:
                self.consent.observer = self.previous_consent_observer
            self.release_lock()
