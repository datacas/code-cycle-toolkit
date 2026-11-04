"""Operation consent: Claude boundary, exact scope, denial evidence and revocation."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import command_permissions as cp
import executors as ex
import grants
import router
import run_cycle as rc
import stage_permissions as sp
from test_run_cycle import RunCycleTestCase, Talker, block


class OperationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="code-cycle-tmp-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = grants.ConsentStore(self.root / "consents.json")
        self.policy = sp.StagePolicy("implement", repo="owner/repo", tmpdir=str(self.root))
        self.broker = grants.ConsentBroker("owner/repo", platform="linux", store=self.store)

    def grant(self, command="stat /opt/runtime", policy=None):
        op = cp.operation("Bash", {"command": command}, policy or self.policy)
        grant = cp.make_grant(op, "claude", "linux")
        self.broker.operations[grant.id] = grant
        return grant, grant.scope_for("claude", "linux")

    def test_ac1_pending_has_exact_options_and_full_operation(self):
        grant, scope = self.grant()
        blocker = cp.request_record(grant, "stat /opt/runtime", "outside table")
        self.assertEqual("pending", self.broker.request(grant, "claude", role="implement", blocker=blocker))
        request = self.broker.pending[0]
        self.assertEqual(cp.COMMAND_OPTIONS, request.question["options"])
        self.assertEqual(grant.operation, request.record()["pattern"])
        self.assertEqual("stat /opt/runtime", request.record()["operation"])
        self.assertLessEqual(len(request.question["prompt"]), 600)

    def test_ac2_repository_global_and_cycle_survive_reconstruction(self):
        for answer in ("cycle", "persistent", "forever"):
            with self.subTest(answer=answer):
                self.store = grants.ConsentStore(self.root / (answer + ".json"))
                self.broker.store = self.store
                self.broker.cycle_id = "cycle-one"
                grant, scope = self.grant()
                self.broker.apply_answer(grant, scope, answer)
                for issue, cycle_id in (("406", "cycle-one"), ("407", "cycle-two")):
                    resumed = grants.ConsentBroker("owner/repo", platform="linux", store=self.store,
                                                   cycle_id=cycle_id)
                    entries = resumed.command_entries("claude", "implement")
                    self.assertEqual(answer != "cycle" or issue == "406", bool(entries))
                    if entries:
                        self.assertTrue(sp.check("stat /opt/runtime", replace(
                            self.policy, command_permissions=entries)).allowed)
                other = grants.ConsentBroker("owner/other", platform="linux", store=self.store)
                self.assertEqual(answer == "forever", bool(other.command_entries("claude", "implement")))
                self.assertFalse(other.command_entries("claude", "review"))

    def test_global_grant_never_covers_review_roles(self):
        for role in ("review", "rereview"):
            with self.subTest(role=role):
                grant, scope = self.grant(policy=replace(self.policy, role=role))
                with self.assertRaises(grants.GrantError):
                    self.broker.apply_answer(grant, scope, "forever")
                question = grants.question(grant, scope, role=role)
                self.assertNotIn("forever", question["options"])
                self.assertIn("this repository", question["options"])
                # A hand-written global entry for a review role is ignored too.
                self.store.save("*", grant, scope, "allow")
                other = grants.ConsentBroker("owner/other", platform="linux", store=self.store)
                other.operations[grant.id] = grant
                self.assertFalse(other.command_entries("claude", role))
        grant, scope = self.grant()
        self.assertIn("forever", grants.question(grant, scope, role="implement")["options"])

    def test_ac3_risky_operation_is_a_question_with_tradeoffs(self):
        blocker = cp.refused_operation(["rm -rf scratch"], self.policy, self.broker, "claude")
        self.assertIsNotNone(blocker)
        grant = self.broker.grant_for(blocker)
        self.assertEqual("pending", self.broker.request(grant, "claude", role="implement", blocker=blocker))
        question = self.broker.pending[-1].question
        for term in ("rm -rf scratch", "Risk:", "Pro:", "Con:", "Alternative:"):
            self.assertIn(term, question["prompt"])

    def test_ac4_interpreter_exact_code_not_arbitrary_prefix(self):
        command = "python3 -c 'print(1)'"
        grant, scope = self.grant(command)
        self.broker.apply_answer(grant, scope, "persistent")
        policy = replace(self.policy, command_permissions=self.broker.command_entries("claude", "implement"))
        self.assertTrue(sp.check(command, policy).allowed)
        self.assertFalse(sp.check("python3 -c 'print(2)'", policy).allowed)
        self.assertIn("external isolation", scope.risks)
        self.assertIn("Bash(python3:*)", sp.allowed_tool_patterns(policy))

    def test_ac4_forbidden_edit_asks_and_exact_target_can_be_granted(self):
        review = replace(self.policy, role="review")
        data = {"file_path": str(self.root.parent / "review-target")}
        blocker = cp.refused_operation([{"tool": "Edit", "input": data}], review, self.broker, "claude")
        self.assertIsNotNone(blocker)
        grant = self.broker.grant_for(blocker)
        scope = grant.scope_for("claude", "linux")
        self.broker.apply_answer(grant, scope, "persistent")
        allowed = replace(review, command_permissions=self.broker.command_entries("claude", "review"))
        self.assertTrue(sp.check_file_edit("Edit", data, allowed).allowed)
        self.assertFalse(sp.check_file_edit("Edit", {"file_path": "/other"}, allowed).allowed)

    def test_ac4_read_outside_workspace_asks_and_grant_keeps_target_exact(self):
        data = {"command": "cat /opt/runtime/decision.json"}
        self.assertFalse(sp.check(data["command"], self.policy, cwd=str(self.root)).allowed)
        blocker = cp.refused_operation([data["command"]], self.policy, self.broker, "claude", str(self.root))
        self.assertIsNotNone(blocker)
        grant = self.broker.grant_for(blocker)
        self.broker.apply_answer(grant, grant.scope_for("claude", "linux"), "persistent")
        policy = replace(self.policy, command_permissions=self.broker.command_entries("claude", "implement"))
        self.assertTrue(sp.check(data["command"], policy, cwd=str(self.root)).allowed)
        self.assertFalse(sp.check("cat /opt/runtime/other.json", policy, cwd=str(self.root)).allowed)

    def test_ac8_documented_options_scopes_and_limits(self):
        for path in (ROOT / "docs" / "configuration.md", ROOT / "docs" / "host-permissions.md"):
            content = path.read_text()
            for term in ("this cycle / this repository / forever / deny", "permission_required",
                         "not_verified", "HUMAN_INTERVENTION", "external isolation", "command_<sha256>"):
                self.assertIn(term, content)

    def test_ac4_direct_publication_guards_survive_even_a_forged_grant(self):
        for command in ("git push --force origin topic", "git push origin HEAD:main",
                        "git push origin :topic", "gh pr merge 4 --repo owner/repo"):
            with self.subTest(command=command):
                grant, _ = self.grant(command)
                policy = replace(self.policy, command_permissions=({"operation": grant.operation,
                                                                    "decision": "allow"},))
                self.assertFalse(sp.check(command, policy).allowed)

    def test_edit_grant_does_not_defeat_git_alias_guards(self):
        for name in (".git", ".GIT", ".git. ", ".git::$INDEX_ALLOCATION"):
            data = {"file_path": str(self.root / name / "config")}
            op = cp.operation("Edit", data, self.policy)
            policy = replace(self.policy, command_permissions=({"operation": op, "decision": "allow"},))
            self.assertFalse(sp.check_file_edit("Edit", data, policy).allowed)

    def test_workspace_read_guard_resolves_relative_symlinks(self):
        (self.root / "outside").symlink_to(self.root.parent, target_is_directory=True)
        self.assertFalse(sp.check("cat outside/decision.json", self.policy, cwd=str(self.root)).allowed)

    def test_never_grantable_ref_writes_are_not_offered_in_any_role(self):
        for role in ("implement", "review", "rereview", "resolve", "issue_review"):
            for command in ("git push --force origin topic", "git push origin HEAD:main",
                            "git branch -df topic", "git tag --delete=tag"):
                self.assertIsNone(cp.refused_operation([command], replace(self.policy, role=role),
                                                       self.broker, "claude"))

    def test_ac4_protected_credentials_are_never_grantable(self):
        command = f"cat {Path.home() / '.codex' / 'auth.json'}"
        grant, _ = self.grant(command)
        policy = replace(self.policy, command_permissions=({"operation": grant.operation, "decision": "allow"},))
        self.assertFalse(sp.check(command, policy).allowed)
        self.assertIsNone(cp.refused_operation([command], policy, self.broker, "claude"))

    def test_ac4_tmpdir_only_normalizes_resolved_members_not_code(self):
        command = f"stat {self.root / 'snapshot'}"
        grant, _ = self.grant(command)
        self.assertIn({"tmpdir": "snapshot"}, grant.operation["pattern"]["words"])
        (self.root / "escape").symlink_to(self.root.parent, target_is_directory=True)
        escaped, _ = self.grant(f"stat {self.root / 'escape' / 'snapshot'}")
        self.assertNotIn({"tmpdir": "snapshot"}, escaped.operation["pattern"]["words"])
        code, _ = self.grant(f"python3 -c 'print(\"{self.root}/snapshot\")'")
        self.assertIn(str(self.root), code.operation["pattern"]["words"][-1])

    def test_ac5_denied_check_is_not_verified_and_essential_decision_is_named(self):
        grant, _ = self.grant()
        blocker = cp.request_record(grant, "stat /opt/runtime", "table")
        spoken = block("BLOCKED", tests={"conclusion": "verified", "passed": True,
            "checks": [{"id": "runtime", "command": "stat /opt/runtime", "result": "pass"}]},
            permission_effects=[{"command": blocker["command"], "essential": True, "check_ids": ["runtime"]}])
        changed, essential = cp.reconcile_result(spoken, blocker, "deny", "implement")
        payload = json.loads(changed.split("ORCHESTRATION_RESULT\n", 1)[1].split("\nEND_ORCHESTRATION_RESULT", 1)[0])
        self.assertTrue(essential)
        self.assertEqual("HUMAN_INTERVENTION", payload["stop_reason"])
        self.assertEqual(grant.id, payload["essential_permission"])
        self.assertEqual("not_verified", payload["tests"]["conclusion"])
        self.assertEqual("unverified", payload["tests"]["checks"][0]["result"])

    def test_ac6_completed_accessory_refusal_does_not_stop(self):
        grant, _ = self.grant()
        blocker = cp.request_record(grant, "stat /opt/runtime", "table")
        spoken = block("BLOCKED", head_sha="a" * 40, change_request_id="4", completed_status="IMPLEMENTED",
                       permission_effects=[{"command": blocker["command"], "essential": False, "check_ids": []}])
        changed, essential = cp.reconcile_result(spoken, blocker, "deny", "implement")
        self.assertFalse(essential)
        self.assertIn('"status": "IMPLEMENTED"', changed)

    def test_ac7_list_and_revoke_apply_before_next_stage(self):
        for answer, repo in (("persistent", "owner/repo"), ("forever", "*"),
                             ("cycle", "cycle:cycle-one:owner/repo")):
            with self.subTest(answer=answer):
                self.broker.cycle_id = "cycle-one"
                grant, scope = self.grant()
                self.broker.apply_answer(grant, scope, answer)
                self.assertTrue(self.broker.command_entries("claude", "implement"))
                self.assertTrue(any(row["grant"] == grant.id for row in self.store.list(repo)))
                self.assertTrue(self.store.revoke(repo, grant, scope))
                self.assertFalse(self.broker.command_entries("claude", "implement"))

    def test_ac7_real_control_cli_lists_and_revokes_command(self):
        grant, scope = self.grant()
        self.broker.apply_answer(grant, scope, "forever")
        env = {**os.environ, "CODE_CYCLE_CONSENT_FILE": str(self.store.path)}
        command = [sys.executable, str(ROOT / "scripts" / "consent_control.py"), "list",
                   "--repo", "owner/repo", "--executor", "claude", "--platform", "linux"]
        listed = subprocess.run(command, cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertTrue(any(item["grant"] == grant.id for item in json.loads(listed.stdout)))
        command[2] = "revoke"
        revoked = subprocess.run(command + ["--grant-id", grant.id, "--global"],
                                 cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(0, revoked.returncode, revoked.stderr)
        self.assertFalse(self.broker.command_entries("claude", "implement"))

    def test_hook_process_allowed_and_denied_boundaries(self):
        grant, scope = self.grant()
        self.broker.apply_answer(grant, scope, "persistent")
        policy = replace(self.policy, command_permissions=self.broker.command_entries("claude", "implement"))
        path = self.root / "policy.json"
        path.write_text(policy.to_json())
        for command, allowed in (("stat /opt/runtime", True), ("stat /private/other", False)):
            event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.root)}
            result = subprocess.run([sys.executable, str(ROOT / "scripts" / "stage_permissions.py"),
                                     "hook", str(path)], input=json.dumps(event), capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(allowed, result.stdout == "")
            if not allowed:
                self.assertEqual("deny", json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"])

    def test_claude_stream_preserves_verified_tmpdir_pattern(self):
        command = 'stat "$TMPDIR/snapshot"'
        scratch = tempfile.TemporaryDirectory(prefix="code-cycle-tmp-", dir=self.root)
        self.addCleanup(scratch.cleanup)
        path = self.root / "policy.json"
        path.write_text(replace(self.policy, tmpdir=scratch.name).to_json())
        output = io.StringIO()
        sp.hook_main([str(path)], io.StringIO(json.dumps({"tool_name": "Bash",
            "tool_input": {"command": command}, "cwd": str(self.root)})), output)
        reason = json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecisionReason"]
        stream = "\n".join(json.dumps(event) for event in [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "x",
                "name": "Bash", "input": {"command": command}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x",
                "is_error": True, "content": reason}]}}])
        artifacts = ex.ClaudeAdapter().extra_artifacts(stream)
        scratch.cleanup()
        self.assertFalse(Path(scratch.name).exists())
        request = cp.refused_operation(artifacts["command_refusals"], replace(self.policy, tmpdir=None),
                                       self.broker, "claude", str(self.root))
        self.assertIsNotNone(request)
        grant = self.broker.grant_for(request)
        self.assertIn({"tmpdir": "snapshot"}, grant.operation["pattern"]["words"])

    def test_interpreter_script_targets_and_marker_literals_never_generalize(self):
        script, _ = self.grant(f"python3 {self.root / 'helper.py'}")
        self.assertEqual(str(self.root / 'helper.py'), script.operation["pattern"]["words"][1])
        actual, _ = self.grant(f"stat {self.root / 'snapshot'}")
        literal, _ = self.grant("stat '<TMPDIR>/snapshot'")
        self.assertNotEqual(actual.id, literal.id)

    def test_malformed_result_fields_cannot_crash_permission_reconciliation(self):
        grant, _ = self.grant()
        blocker = cp.request_record(grant, "stat /opt/runtime", "table")
        changed, essential = cp.reconcile_result(block("BLOCKED", tests="bad",
            permission_outcomes="bad", permission_effects="bad"), blocker, None, "implement")
        self.assertTrue(essential)
        self.assertIn('"answer": "pending"', changed)

    def test_rtk_denial_cannot_leave_the_unwrapped_check_passed(self):
        grant, _ = self.grant()
        blocker = cp.request_record(grant, "rtk proxy stat /opt/runtime", "table")
        changed, _ = cp.reconcile_result(block("IMPLEMENTED", tests={"checks": [
            {"id": "stat", "command": "stat /opt/runtime", "result": "pass"}]}), blocker, "deny", "implement")
        self.assertIn('"result": "unverified"', changed)


class RefusedClaude(Talker):
    def __init__(self, accessory=False):
        super().__init__("claude")
        self.accessory = accessory

    def dispatch(self, target, task, **kwargs):
        self.dispatched.append(task)
        policy = kwargs.get("command_policy")
        if policy is not None and policy.role != "implement":
            return ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, "claude", target,
                start_state="started", process_exited=True, agent_output=block("APPROVED"))
        allowed = sp.check("stat /opt/runtime", policy).allowed if policy else False
        return ex.DispatchResult(ex.DispatchOutcome.SUCCEEDED, "claude", target,
            start_state="started", process_exited=True,
            agent_output=block("IMPLEMENTED" if allowed or self.accessory else "BLOCKED"),
            artifacts={} if allowed else {"permission_denials": ["stat /opt/runtime"]})


class RuntimeTests(RunCycleTestCase):
    def run_refusal(self, accessory=False, issue="406", **broker_kw):
        claude = RefusedClaude(accessory)
        broker = grants.ConsentBroker("owner/api", platform="linux",
                                      store=grants.ConsentStore(self.home / "consents.json"), **broker_kw)
        profiles = router.load_profiles({"code_cycle": {"profiles": {
            "deep_coder": {"primary": "claude:anthropic/claude-opus-5-5 high"},
            "reviewer": {"primary": "claude:anthropic/claude-sonnet-5 high"}}}})
        report = rc.run_cycle("owner/api", issue, router.TaskSignals(), self.store,
            registry=ex.Registry([claude]), availability={"claude": ex.Availability.READY},
            profiles=profiles, issue_review="off", consent=broker)
        return report, broker, claude

    def test_ac1_claude_refusal_becomes_permission_required(self):
        report, broker, claude = self.run_refusal()
        self.assertEqual("permission_required", report.stop_reason)
        self.assertEqual(cp.COMMAND_OPTIONS, report.permission_requests[0].question["options"])
        self.assertNotEqual("BLOCKED", report.status)
        self.assertEqual(1, len(claude.dispatched))

    def test_ac6_completed_claude_stage_survives_accessory_refusal(self):
        report, broker, claude = self.run_refusal(accessory=True)
        self.assertNotEqual("permission_required", report.stop_reason)
        self.assertFalse(broker.pending)
        self.assertEqual("IMPLEMENTED", report.stage_details[0].status)

    def test_ac2_two_issues_hydrate_repository_and_global_answers_before_dispatch(self):
        for answer in ("persistent", "forever"):
            with self.subTest(answer=answer):
                store = grants.ConsentStore(self.home / "consents.json")
                broker = grants.ConsentBroker("owner/api", store=store, platform="linux")
                grant = cp.make_grant(cp.operation("Bash", {"command": "stat /opt/runtime"},
                    sp.StagePolicy("implement")), "claude", "linux")
                broker.apply_answer(grant, grant.scope_for("claude", "linux"), answer)
                for issue in ("406", "407"):
                    report, fresh, claude = self.run_refusal(issue=issue,
                        prompt=lambda question: self.fail("asked twice for the same operation"))
                    self.assertEqual(rc.APPROVED_END, report.status, report.stopped_because)
                    self.assertFalse(fresh.pending)
                    self.assertEqual(2, len(claude.dispatched))  # implement and review, no retry

    def test_ac2_cycle_lineage_survives_resume_without_reaching_second_issue(self):
        store = grants.ConsentStore(self.home / "consents.json")
        broker = grants.ConsentBroker("owner/api", platform="linux", store=store,
                                      cycle_id="original", task_id="406")
        grant = cp.make_grant(cp.operation("Bash", {"command": "stat /opt/runtime"},
            sp.StagePolicy("implement")), "claude", "linux")
        broker.apply_answer(grant, grant.scope_for("claude", "linux"), "cycle")
        self.assertEqual("original", store.cycle_for("owner/api", "406"))
        self.assertIsNone(store.cycle_for("owner/api", "407"))
        resumed = grants.ConsentBroker("owner/api", platform="linux", store=store,
            cycle_id=store.cycle_for("owner/api", "406"), task_id="406")
        self.assertTrue(resumed.command_entries("claude", "implement"))
        self.assertFalse(grants.ConsentBroker("owner/api", platform="linux", store=store,
            cycle_id="second", task_id="407").command_entries("claude", "implement"))


if __name__ == "__main__":
    unittest.main()
