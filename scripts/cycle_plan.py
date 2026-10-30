"""Plan revisions and verification barriers for the existing native stage loop."""
from __future__ import annotations

import json
import re
import hashlib
from dataclasses import replace
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .cycle_journal import events as journal_events
    from .cycle_history import HistoryError, restore_plan
    from .execution_plan import ContractError, ExecutionPlan, ExecutionState, Obligation, Resolution, StagePolicy
    from .execution_profiles import ExecutionSettings, PlanSignals, build_plan, invocation_settings, resolve_execution
    from .security_gate import GateRule, evaluate
    from .stage_permissions import StagePolicy as CommandPolicy, _is_verification_command
    from .verification_plan import CheckResult, VerificationError, VerificationPlan, configured_check
else:
    from cycle_journal import events as journal_events
    from cycle_history import HistoryError, restore_plan
    from execution_plan import ContractError, ExecutionPlan, ExecutionState, Obligation, Resolution, StagePolicy
    from execution_profiles import ExecutionSettings, PlanSignals, build_plan, invocation_settings, resolve_execution
    from security_gate import GateRule, evaluate
    from stage_permissions import StagePolicy as CommandPolicy, _is_verification_command
    from verification_plan import CheckResult, VerificationError, VerificationPlan, configured_check


HOTFIX_IDS = ("hotfix-reproduction", "hotfix-regression")


def hotfix_problem(verification: VerificationPlan | None, head: str) -> str | None:
    """A generic suite cannot stand in for test-first proof of a defect."""
    if verification is None or not set(HOTFIX_IDS) <= {
            check.id for check in verification.checks if check.required}:
        return "missing_checks"
    commands = {check.id: check.command for check in verification.checks}
    if (commands[HOTFIX_IDS[0]] == commands[HOTFIX_IDS[1]]
            or any(commands[item] == commands.get("configured-test") for item in HOTFIX_IDS)):
        return "missing_checks"
    previous = [result for result in verification.results
                if result.id == HOTFIX_IDS[0] and result.head_sha != head
                and result.result == "fail" and result.cause == "defect"]
    current = [result for result in verification.results
               if result.id == HOTFIX_IDS[0] and result.head_sha == head
               and result.result == "pass"]
    if not previous or not current:
        return "missing_proof"
    before, after = previous[-1], current[-1]
    # An environment/import failure and an edited reproduction are not proof.
    for key in ("test_sha256", "context_sha256"):
        old = {entry.get(key) for entry in before.evidence}
        new = {entry.get(key) for entry in after.evidence}
        if (len(old) != 1 or None in old or old != new
                or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                       for value in old)):
            return "incompatible_proof"
    if not all(isinstance(entry.get("output_sha256"), str)
               and re.fullmatch(r"[a-f0-9]{64}", entry["output_sha256"]) for entry in before.evidence):
        return "missing_output"
    return None


class PlanRuntime:
    security_rule: GateRule = GateRule()
    plan_managed: bool = False
    execution_plan: Any = None
    plan_receipt: dict[str, Any] | None = None
    verification_resume: bool = False
    plan_partial: bool = False
    plan_head: str | None = None
    plan_store: Any = None
    recorded_commands: Any = None
    plan_conclusion: str | None = None

    def initialize_plan(self: Any, supplied, security_rule) -> None:
        self.security_rule = security_rule or GateRule()
        self.plan_managed = supplied is not None
        resolved = resolve_execution(ExecutionSettings(), invocation_settings(
            local_only=self.local_only,
            issue_review="off" if self.issue_review.value == "off" else "auto"))
        self.execution_plan = supplied or build_plan(
            resolved, repository=self.repo_id, work_item=self.task_id,
            signals=PlanSignals(self.signals.difficulty, self.signals.security_sensitive),
            start_from=self.start_from, continue_work=self.continue_work,
            change_request=self.state.change_request_id,
            plan_id=f"plan-{self.recorder.cycle_id}", provenance="run")
        # A dry-run identity is deterministic; every real run has its own identity.
        self.execution_plan = replace(self.execution_plan,
            plan_id=f"plan-{self.recorder.cycle_id}", revision=1,
            head_sha=self.api._git_line(self.cwd, ["rev-parse", "HEAD"], 30) if self.cwd else None)
        self.execution_plan.validate()
        self.plan_receipt = None
        self.verification_resume = False
        self.plan_partial = False
        self.plan_head = self.execution_plan.head_sha
        self.plan_store = self.api.CycleHistoryStore(self.telemetry.path)
        self.recorded_commands = set()
        self.plan_conclusion = None
        if self.start_from == "implement" and not self.continue_work:
            if self.execution_plan.stage("issue_review").selected:
                self.issue_review_fields["issue_review"] = "dispatched"

    def save_plan(self: Any, provenance: str = "observed", stop_reason=None) -> None:
        if self.state.verification_plan is not None:
            conclusion = self.state.verification_plan.cap(self.plan_conclusion or "verified", self.plan_head or "")
            self.execution_plan = replace(self.execution_plan, stages=tuple(
                replace(item,
                    state=ExecutionState.COMPLETED if conclusion in {"verified", "verified_with_reservations"}
                          else ExecutionState.PLANNED,
                    conclusion=conclusion if conclusion in {"verified", "verified_with_reservations"} else None)
                if item.stage_id == "verify" and item.selected else item
                for item in self.execution_plan.stages))
        self.execution_plan = replace(self.execution_plan,
            revision=self.execution_plan.revision + 1,
            verification=self.state.verification_plan,
            head_sha=self.plan_head,
            metadata={"verification_conclusion": self.plan_conclusion})
        self.execution_plan.validate()
        self.plan_receipt = self.plan_store.save_plan(
            self.execution_plan, cycle_id=self.recorder.cycle_id,
            change_request_id=self.state.change_request_id,
            provenance=provenance, stop_reason=stop_reason)
        self.report.execution_plan = self.plan_receipt
        if self.state.verification_plan is not None:
            for check in self.state.verification_plan.checks:
                if check.command not in self.recorded_commands and not check.command.startswith("sha256:"):
                    self.journal.record("stage_result", component="verification_command", command=check.command,
                                        status="proposed")
                    self.recorded_commands.add(check.command)
        self.journal.record("stage_result", component="execution_plan", status=provenance,
            correlation={"plan_id": self.execution_plan.plan_id,
                         "revision": self.execution_plan.revision,
                         "digest": self.plan_receipt["digest"], "head_sha": self.plan_head})

    def prepare_plan(self: Any):
        """Reconcile authorized inputs and local history before any dispatch."""
        record = None
        if self.start_from in self.api.RESUMES or self.continue_work:
            try:
                record = self.plan_store.latest_plan(
                    self.repo_id, self.task_id, self.state.change_request_id)
            except (HistoryError, ValueError, TypeError):
                self.plan_partial = True
        if record is not None:
            try:
                commands = self.authorized_bindings()
                recovered = restore_plan(record, commands)
                # New policy can add obligations. It cannot erase the old ones.
                same_policy = (recovered.objective == self.execution_plan.objective
                    and recovered.execution_profile == self.execution_plan.execution_profile
                    and recovered.control("ci").timing == self.execution_plan.control("ci").timing
                    and all(old.policy == self.execution_plan.stage(old.stage_id).policy
                            for old in recovered.stages))
                if not same_policy:
                    self.plan_partial = True
                for old in recovered.stages:
                    new = self.execution_plan.stage(old.stage_id)
                    if (old.policy is StagePolicy.REQUIRED
                            and new.policy is StagePolicy.DISABLED):
                        raise ContractError("resume would withdraw a required stage")
                old_security = recovered.stage("security")
                if old_security.selected and old_security.obligation.value == "required":
                    security = self.execution_plan.stage("security")
                    if security.resolution is Resolution.PENDING:
                        self.execution_plan = self.execution_plan.resolve_decision(
                            "security", True, "retained security obligation", "runtime")
                self.execution_plan = replace(self.execution_plan,
                    plan_id=recovered.plan_id, revision=recovered.revision,
                    verification=recovered.verification)
                self.state.verification_plan = recovered.verification
                self.plan_conclusion = recovered.metadata.get("verification_conclusion")
                self.verification_resume = (
                    self.start_from == "rereview" and record["stop_reason"] == "verification_unverified"
                    and same_policy
                    and record["head_sha"] == self.plan_head and self.plan_receipt_matches(record)
                    and (not recovered.stage("security").selected
                         or recovered.stage("security").state is ExecutionState.COMPLETED))
                if self.verification_resume:
                    self.execution_plan = recovered
            except HistoryError:
                self.plan_partial = True
                self.plan_conclusion = "not_verified"
                if record is not None:
                    # Keep unavailable command identities as obligations. A
                    # fingerprint is not executable and grants no permission.
                    # Re-authorizing its exact command permits a later resume.
                    try:
                        retained = ExecutionPlan.from_dict(record["payload"]["plan"])
                    except (KeyError, TypeError, ContractError):
                        retained = None
                    self.state.verification_plan = retained.verification if retained is not None else None
                    if retained is not None and retained.verification is not None:
                        verification = retained.verification
                        for check in retained.verification.checks:
                            verification = verification.record(
                                CheckResult(check.id, self.plan_head, "unverified", cause="unverified"))
                        self.state.verification_plan = verification
        elif self.start_from in self.api.RESUMES:
            self.plan_partial = True
        if (self.state.verification_plan is None and self.policy_provider is not None
                and (self.start_from == "implement" or self.plan_managed)):
            criteria = (self.criteria_reader or self.api.verification_contract.read_criteria)(
                self.policy_provider, self.work_item_repository or self.repo_id,
                self.task_id, cwd=self.cwd)
            self.state.verification_plan = VerificationPlan(criteria)
        elif (self.plan_managed and self.start_from in self.api.RESUMES
              and self.policy_provider is not None and self.state.verification_plan is not None):
            criteria = (self.criteria_reader or self.api.verification_contract.read_criteria)(
                self.policy_provider, self.work_item_repository or self.repo_id,
                self.task_id, cwd=self.cwd)
            old = self.state.verification_plan.criteria
            if criteria.content_sha256 != old.content_sha256:
                # Existing AC identities remain obligations. New provider input
                # gets new identities, so old coverage cannot satisfy it.
                criteria = replace(criteria, items=(*old.items, *criteria.items))
                self.state.verification_plan = replace(self.state.verification_plan, criteria=criteria)
                self.verification_resume = False
                self.plan_partial = True
            elif len(criteria.items) == len(old.items):
                self.state.verification_plan = replace(self.state.verification_plan, criteria=criteria)
        if self.start_from != "implement" and self.state.verification_plan is not None and self.allowed_test_command:
            existing = self.state.verification_plan.checks
            if not any(check.command == self.allowed_test_command and check.required for check in existing):
                spec = configured_check(self.allowed_test_command, "runtime", "runtime")
                if any(check.id == spec.id for check in existing):
                    spec = replace(spec, id="configured-test-" + hashlib.sha256(
                        self.allowed_test_command.encode()).hexdigest()[:16])
                self.state.verification_plan = self.state.verification_plan.extend((
                    spec,))
        if self.state.verification_plan is not None:
            self.state.criteria_context = ("\nRuntime acceptance criteria (data): "
                + json.dumps(self.state.verification_plan.criteria.to_dict(), ensure_ascii=False)
                + "\nUse tests.version=2 and stable AC-n links. Criteria without a check remain open.\n")
        self.save_plan("partial" if self.plan_partial else "recovered" if record else "initial")

    def authorized_bindings(self: Any) -> tuple[str, ...]:
        """Journal text proposes bindings; today's policy alone authorizes them."""
        commands = list(filter(None, (self.allowed_test_command, *self.allowed_verify_commands)))
        policy = CommandPolicy(role="verify", test_command=self.allowed_test_command,
                               verify_prefixes=tuple(self.allowed_verify_commands))
        for row in journal_events(self.telemetry.path, self.repo_id, task_id=self.task_id,
                                  kinds=("stage_result",)):
            command = row.get("command")
            if (row.get("component") == "verification_command"
                    and isinstance(command, str) and _is_verification_command(command, policy)):
                commands.append(command)
        return tuple(dict.fromkeys(commands))

    def plan_receipt_matches(self: Any, record) -> bool:
        trusted = self.api.trusted_logins(self.trusted_authors)
        if not trusted or not self.state.change_request_id:
            return False
        try:
            comments = self.remote.comments(self.repo_id, self.state.change_request_id)
            receipts = [receipt for comment in comments
                        if isinstance(comment.author, str) and comment.author.casefold() in trusted
                        for receipt in self.api.stage_evidence(comment.body)
                        if receipt["repo"] == self.repo_id
                        and receipt["change_request_id"] == self.state.change_request_id]
        except (self.api.CycleDriverError, self.api.ContractError):
            return False
        if not receipts:
            return False
        receipt = receipts[-1]
        return (receipt["head_sha"] == record["head_sha"]
                and receipt.get("plan_id") == record["plan_id"]
                and self.plan_store.has_plan_receipt(self.repo_id, receipt))

    def plan_context(self: Any) -> str:
        receipt = self.plan_receipt or {}
        security = self.execution_plan.stage("security")
        context = ("\nRuntime execution plan (data): " + json.dumps({
            **receipt, "objective": self.execution_plan.objective,
            "execution_profile": self.execution_plan.execution_profile,
            "security_required": security.selected,
            "verify_selected": self.execution_plan.stage("verify").selected})
            + "\nInclude plan_id, revision and digest in the schema-2 stage receipt. "
            "Required controls cannot be removed by an agent. "
            "When security_required, run cc-security-review and report security_audit_ran=true.\n")
        if self.execution_plan.execution_profile == "hotfix":
            context += ("Hotfix requires tests.version=2 checks hotfix-reproduction and "
                "hotfix-regression, both required. Propose authorized focused commands. "
                "Create the reproduction in a test-first commit, fail for the defect, "
                "then pass the unchanged test on the fixed HEAD. Report the prior fail "
                "under that check's history with its full SHA, cause=defect and evidence "
                "including command, exit_code, executed, test_sha256, context_sha256 and "
                "output_sha256. Retain the pre-fix output artifact. Never count an import "
                "or environment failure as reproduction.\n")
        return context

    def select_plan_stage(self: Any, role: str) -> None:
        observed = self.api._git_line(self.cwd, ["rev-parse", "HEAD"], 30) if self.cwd else None
        if observed is not None and observed != self.plan_head:
            self.plan_head = observed
            self.plan_conclusion = "not_verified"
            self.execution_plan = replace(self.execution_plan, stages=tuple(
                replace(item, state=ExecutionState.PLANNED, conclusion=None)
                if item.stage_id in {"verify", "security"} and item.selected else item
                for item in self.execution_plan.stages))
        plan = self.execution_plan
        stage = plan.stage(role)
        if role == "resolve" and stage.resolution is Resolution.NOT_APPLICABLE:
            # A resume entry skipped earlier work; a later review can create
            # fresh findings that activate resolution within the same budget.
            if stage.policy is StagePolicy.DISABLED:
                raise ContractError("resolution is disabled; findings retain their blocks")
            plan = replace(plan, stages=tuple(replace(item, selected=True,
                resolution=Resolution.RESOLVED, state=ExecutionState.PLANNED)
                if item.stage_id == role else item for item in plan.stages))
        if stage.resolution is Resolution.PENDING:
            if role in {"resolve", "rereview"}:
                plan = plan.resolve_decision(role, True, "runtime condition observed", "runtime")
        if role in {"review", "rereview"}:
            paths = None
            if self.cwd and self.change_bases:
                for base in self.change_bases:
                    raw = self.api._git_line(self.cwd, ["diff", "--name-only", f"{base}...HEAD"], 30)
                    if raw is not None:
                        paths = raw.splitlines()
                        break
            # Unknown diff is conservative. Agent requests only add controls.
            required = paths is None or evaluate(paths, rule=self.security_rule).required
            security = plan.stage("security")
            required = required or self.signals.security_sensitive or security.policy in {
                StagePolicy.REQUIRED, StagePolicy.ENABLED} or security.selected
            if required:
                plan = self.require_security(plan, "runtime security gate")
            elif security.resolution is Resolution.PENDING:
                plan = plan.resolve_decision("security", False, "runtime security gate", "runtime")
        self.execution_plan = plan
        plan.check_executable(role)
        self.save_plan()

    def require_security(self: Any, plan, reason: str):
        security = plan.stage("security")
        if security.resolution is Resolution.PENDING:
            return plan.resolve_decision("security", True, reason, "runtime")
        if security.policy is StagePolicy.DISABLED:
            # Existing obligation validation explains the policy conflict.
            return plan.settle_obligation("security_policy", True, reason)
        plan = replace(plan, stages=tuple(
            replace(item, selected=True, obligation=Obligation.REQUIRED,
                    state=ExecutionState.PLANNED if not item.selected else item.state,
                    resolution=Resolution.RESOLVED)
            if item.stage_id == "security" else item for item in plan.stages))
        return plan.settle_obligation("security_policy", True, reason)

    def record_plan_stage(self: Any, role: str, reported, outcome, test_payload=None) -> None:
        payload = reported.payload or {}
        head = payload.get("head_sha")
        if isinstance(head, str) and self.api.FULL_SHA.fullmatch(head):
            if self.plan_managed and self.cwd:
                actual = self.api._git_line(self.cwd, ["rev-parse", "HEAD"], 30)
                if actual is not None and actual != head:
                    raise ContractError("reported evidence does not match the workspace HEAD")
            if self.plan_head and head != self.plan_head:
                self.plan_conclusion = "not_verified"
                self.execution_plan = replace(self.execution_plan, stages=tuple(
                    replace(item, state=ExecutionState.PLANNED, conclusion=None)
                    if item.stage_id in {"verify", "security"} and item.selected else item
                    for item in self.execution_plan.stages))
            self.plan_head = head
        conclusion = self.api._legacy_tests(test_payload).get("verification")
        if conclusion is not None:
            self.plan_conclusion = conclusion
        stage = self.execution_plan.stage(role)
        # Functional outcomes remain separate from transport completion.
        stages = tuple(replace(item, state=ExecutionState.COMPLETED,
                               conclusion=reported.status)
                       if item.stage_id == role else item for item in self.execution_plan.stages)
        if stage.selected and reported.status:
            self.execution_plan = replace(self.execution_plan, stages=stages)
        if role in {"review", "rereview"} and payload.get("security_audit_ran") is True:
            self.execution_plan = replace(self.execution_plan, stages=tuple(
                replace(item, state=ExecutionState.COMPLETED, conclusion="completed")
                if item.stage_id == "security" and item.selected else item
                for item in self.execution_plan.stages))
        if role in {"implement", "resolve"} and payload.get("security_required") is True:
            self.execution_plan = self.require_security(self.execution_plan, "agent added a control")
        self.save_plan()

    def verification_barrier(self: Any, head: str | None = None):
        plan = self.state.verification_plan
        security = self.execution_plan.stage("security")
        if (self.plan_managed and security.selected
                and security.state is not ExecutionState.COMPLETED):
            return self.stop("required security verification remains unverified",
                             stop_reason="verification_unverified")
        current_head: str = head or self.plan_head or ""
        if self.execution_plan.execution_profile == "hotfix":
            problem = hotfix_problem(plan, current_head)
            if problem is None and self.cwd:
                before = next(result for result in reversed(plan.results)
                    if result.id == HOTFIX_IDS[0] and result.head_sha != current_head
                    and result.result == "fail" and result.cause == "defect")
                if self.api._git_line(self.cwd, ["merge-base", before.head_sha, current_head], 30) != before.head_sha:
                    problem = "test_first_commit_unverified"
            if problem:
                return self.stop(f"Hotfix proof is incomplete: {problem}",
                    stop_reason="needs_refinement" if problem == "missing_checks" else "verification_unverified")
        if plan is None:
            if self.plan_managed and self.execution_plan.objective != "local_change":
                return self.stop("required verification has no recoverable check plan",
                                 stop_reason="verification_unverified")
            return None
        required = [check for check in plan.checks if check.required]
        if self.plan_conclusion == "failed" or any(plan.current(check.id, current_head) == "fail" for check in required):
            return self.stop("a required verification check failed", stop_reason="stage_not_completed")
        if plan.cap(self.plan_conclusion or "verified", current_head) == "not_verified":
            return self.stop("required verification remains unverified on the current HEAD; "
                "resume with --from rereview --pr to run only pending checks",
                stop_reason="verification_unverified")
        return None

    def verify_pending(self: Any):
        """A single isolated cc-verify dispatch; no publication or review loop."""
        from executors import DisposableWorkspace, _git_output
        plan = self.state.verification_plan
        pending = [check for check in plan.checks if check.required
                   and plan.current(check.id, self.plan_head) == "unverified"] if plan else []
        if not pending:
            return self.verification_barrier()
        source = self.cwd
        if not source or self.api._git_line(source, ["rev-parse", "HEAD"], 30) != self.plan_head:
            return self.stop("verification resume HEAD changed", stop_reason="head_moved")
        directory = self.api.tempfile.mkdtemp(prefix="code-cycle-verify-")
        try:
            source_status = _git_output(source, "status", "--porcelain", "--untracked-files=all")
            source_branch = _git_output(source, "rev-parse", "--symbolic-full-name", "HEAD")
            self.api.Path(directory).rmdir()
            _git_output(source, "clone", "--no-hardlinks", "--no-checkout", source, directory, timeout=300)
            _git_output(directory, "remote", "remove", "origin")
            _git_output(directory, "checkout", "--detach", self.plan_head)
            instruction = (f"Run cc-verify on HEAD {self.plan_head}. Run only these pending required checks "
                + json.dumps([check.to_dict() for check in pending])
                + ". Do not review, change source, commit or publish. Return VERIFICATION_RESULT "
                "with version=2, head_sha and checks, including unverified causes. Run every command "
                "in the foreground and allow at least 600 seconds for each configured check. "
                "No general CI result satisfies a check.")
            kwargs = {**self.dispatch_kwargs, "cwd": directory,
                "workspace": DisposableWorkspace(directory, source), "writable_dirs": (),
                "command_policy": replace(self.stage_policy("verify", self.state.change_request_id),
                    test_command=None, verify_prefixes=tuple(check.command for check in pending))}
            self.control.move("verify")
            outcome = self.recorder.stage("verify", instruction, **kwargs)
            self.report.stages.append(outcome)
            self.report.stage_details.append(self.api.StageReportDetails(
                self.recorder.iteration, None))
            if not outcome.succeeded or outcome.result is None or outcome.result.asynchronous:
                return self.stop("pending verification could not execute", stop_reason="verification_unverified")
            if (_git_output(directory, "rev-parse", "HEAD") != self.plan_head
                    or _git_output(directory, "diff", "--name-only", "HEAD")):
                raise VerificationError("verifier changed the tracked source under verification")
            spoken = outcome.result.agent_output
            end = spoken.rfind("END_VERIFICATION_RESULT")
            begin = spoken.rfind("VERIFICATION_RESULT", 0, end)
            if begin < 0 or end < 0:
                raise VerificationError("verifier returned no structured result")
            result = json.loads(spoken[begin + len("VERIFICATION_RESULT"):end].strip())
            if (not isinstance(result, dict) or not isinstance(result.get("checks"), list)
                    or any(not isinstance(item, dict) for item in result["checks"])):
                raise VerificationError("verifier returned malformed check results")
            if result.get("head_sha") != self.plan_head:
                raise VerificationError("verifier returned another HEAD")
            if any(item.get("id") not in {check.id for check in pending}
                   or item.get("head_sha") != self.plan_head or item.get("history")
                   for item in result.get("checks", [])):
                raise VerificationError("verifier reported checks outside the pending set")
            if self.api._git_line(source, ["rev-parse", "HEAD"], 30) != self.plan_head:
                raise VerificationError("source HEAD changed during verification")
            if (_git_output(source, "status", "--porcelain", "--untracked-files=all") != source_status
                    or _git_output(source, "rev-parse", "--symbolic-full-name", "HEAD") != source_branch):
                raise VerificationError("source workspace changed during verification")
            tests, self.state.verification_plan = self.api.verification_contract.normalize_tests(
                result, self.plan_head, plan=plan)
            self.plan_conclusion = tests["conclusion"]
            self.recorder.record_verdict("verify", "READY" if tests["passed"] else "BLOCKED",
                **self.api._legacy_tests({"tests": tests}))
            self.report.stage_details[-1].status = "READY" if tests["passed"] else "BLOCKED"
            self.status_writer.stage_finished(outcome.result, self.report.stage_details[-1].status)
            self.journal.record("stage_result", role="verify", component="execution_plan",
                status=self.report.stage_details[-1].status, correlation={"head_sha": self.plan_head})
            self.save_plan()
            return self.verification_barrier()
        except (OSError, ValueError, RuntimeError, self.api.subprocess.SubprocessError) as error:
            return self.stop(f"pending verification is unverified: {error}",
                             stop_reason="verification_unverified")
        finally:
            self.api._remove_workspace(directory)
