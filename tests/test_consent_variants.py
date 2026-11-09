"""#439: reusable consent at the storage, parser and hook boundaries."""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import grants
import stage_permissions as sp
import command_permissions as cp


class ConsentVariantsTests(unittest.TestCase):
    def approved(self, policy, command, regex=None, executor="claude", store=None, cwd=None):
        op = sp.operation("Bash", {"command": command}, policy, cwd)
        grant = (grants.make_pattern_grant(regex, policy.role, executor, "linux",
                 proposer="test agent", justification="read API contracts",
                 positive=[sp.normalized_command(op)], negative=["rm -rf /"])
                 if regex else grants.make_grant(op, executor, "linux"))
        if store:
            store.save("owner/repo", grant, grant.scopes[0], "allow")
        return replace(policy, command_permissions=({"operation": grant.operation, "decision": "allow"},)), grant

    def test_ac1_ac3_ac4_configured_variants_survive_dispatch(self):
        for role in ("resolve", "rereview"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as root:
                old, new = Path(root) / "old", Path(root) / "new"
                old.mkdir()
                new.mkdir()
                policy = sp.StagePolicy(role, tmpdir=str(old), rtk=True,
                                        test_command="pnpm run test:unit")
                op = sp.operation("Bash", {"command": "pnpm run test:unit"}, policy, root)
                grant = grants.make_grant(op, "claude", "linux")
                store = grants.ConsentStore(Path(root) / "consents.json")
                store.save("owner/repo", grant, grant.scopes[0], "allow")
                broker = grants.ConsentBroker("owner/repo", store=store, platform="linux")
                current = replace(policy, tmpdir=str(new), command_permissions=broker.command_entries("claude", role))
                for command in ("pnpm run test:unit", "rtk pnpm run test:unit",
                                "CACHE=$TMPDIR/cache pnpm run test:unit",
                                "rtk proxy CACHE=$TMPDIR/cache pnpm run test:unit > $TMPDIR/check.log",
                                "CACHE=$TMPDIR/cache rtk pnpm run test:unit 2> $TMPDIR/check.log"):
                    with self.subTest(command=command):
                        self.assertEqual("allow", sp.matching_answer("Bash", {"command": command}, current, root))
                        self.assertTrue(sp.check(command, current, cwd=root).allowed)

    def test_ac2_distinct_commands_still_ask(self):
        policy = sp.StagePolicy("resolve", tmpdir="/tmp/current", test_command="pnpm run test:unit")
        op = sp.operation("Bash", {"command": "pnpm run test:unit"}, policy)
        policy = replace(policy, command_permissions=({"operation": op, "decision": "allow"},))
        for command in ("pnpm run test:unit:host", "node --test", "DOCKER_CONFIG=$TMPDIR/config pnpm run test:unit",
                        "PATH=/tmp/bin pnpm run test:unit"):
            self.assertIsNone(sp.matching_answer("Bash", {"command": command}, policy, None))
            self.assertFalse(sp.check(command, policy).allowed)

    def test_ac2_question_names_exact_configured_command_and_answer_stops_it(self):
        policy = sp.StagePolicy("resolve", test_command="pnpm run test:unit")
        broker = grants.ConsentBroker("owner/repo", platform="linux")
        command = "pnpm run test:unit:host"
        request = cp.refused_operation([command], policy, broker, "claude")
        self.assertIsNotNone(request)
        grant = broker.operations[request["resource"]]
        scope = grant.scopes[0]
        asked = grants.question(grant, scope, role="resolve", blocker=request)
        self.assertIn("Configured command: pnpm run test:unit.", asked["prompt"])
        broker.apply_answer(grant, scope, "cycle")
        current = replace(policy, command_permissions=broker.command_entries("claude", "resolve"))
        self.assertTrue(sp.check(command, current).allowed)

    def test_ac3_all_operands_relative_paths_and_symlink_membership(self):
        with tempfile.TemporaryDirectory() as root:
            old, new, other = [Path(root) / name for name in ("old", "new", "other")]
            for path in (old, new, other):
                path.mkdir()
            (new / "escape").symlink_to(other, target_is_directory=True)
            first = sp.StagePolicy("resolve", tmpdir=str(old))
            second = replace(first, tmpdir=str(new))
            def operation(policy, path):
                return sp.operation("Bash", {"command": "custom-check " + str(path)}, policy, root)
            self.assertEqual(operation(first, old / "result"), operation(second, new / "result"))
            self.assertEqual(operation(first, old), operation(second, new))
            self.assertEqual(operation(second, "./new/result"), operation(second, new / "result"))
            self.assertNotEqual(operation(second, old / "result"), operation(second, new / "result"))
            self.assertNotEqual(operation(second, new / "escape/result"), operation(second, new / "result"))
            first = replace(first, attempt_id="attempt-old")
            second = replace(second, attempt_id="attempt-new")
            self.assertEqual(operation(first, old / "attempt-old.log"), operation(second, new / "attempt-new.log"))
            def shell(policy, path):
                return sp.operation("Bash", {"command": "cat " + str(path) + " | head"}, policy, root)
            self.assertEqual(shell(first, old / "result"), shell(second, new / "result"))

    def test_ac5_ac9_pattern_survives_scope_and_dispatch_and_revocation(self):
        for role in ("resolve", "rereview"):
            for scope in ("cycle", "persistent", "forever"):
                if role == "rereview" and scope == "forever":
                    continue
                with self.subTest(role=role, scope=scope), tempfile.TemporaryDirectory() as root:
                    store = grants.ConsentStore(Path(root) / "consents.json")
                    policy = sp.StagePolicy(role, tmpdir=root)
                    grant = grants.make_pattern_grant(r"(?:ls|cat|grep) [-a-zA-Z0-9 ]*/outside/api[/a-zA-Z0-9_.-]*",
                        role, "claude", "linux", proposer="agent", justification="inspect contracts",
                        positive=["ls /outside/api"], negative=["cat /outside/secrets"])
                    broker = grants.ConsentBroker("owner/repo", store=store, platform="linux", cycle_id="one", task_id="439")
                    broker.operations[grant.id] = grant
                    broker.apply_answer(grant, grant.scopes[0], scope)
                    resumed = grants.ConsentBroker("owner/repo", store=store, platform="linux", cycle_id="one", task_id="439")
                    policy = replace(policy, command_permissions=resumed.command_entries("claude", role))
                    for command in ("ls /outside/api/", "ls -la /outside/api", "cat /outside/api/contracts.json", "grep -n schema /outside/api/v1"):
                        self.assertTrue(sp.check(command, policy).allowed, command)
                    self.assertTrue(store.list("owner/repo"))
                    storage_scope = broker.cycle_scope if scope == "cycle" else "*" if scope == "forever" else "owner/repo"
                    store.record_execution(storage_scope, grant, grant.scopes[0], "ls /outside/api/")
                    entry = store.list("owner/repo")[0]
                    self.assertEqual("agent", entry["operation"]["proposer"])
                    self.assertEqual("allow", entry["user_approval"]["decision"])
                    self.assertEqual("ls /outside/api/", entry["executed_commands"][0]["command"])
                    self.assertTrue(store.revoke(storage_scope, grant, grant.scopes[0]))
                    self.assertFalse(resumed.command_entries("claude", role))

    def test_ac6_invalid_patterns_are_rejected_with_reason(self):
        for regex, reason in (("", "empty"), ("[", "invalid"), (".*", "empty command"),
                              ("(a+)+b", "nested"), ("(a|aa)+b", "alternatives")):
            with self.subTest(regex=regex), self.assertRaisesRegex(grants.GrantError, reason):
                grants.validate_pattern(regex, ["ab"], ["other"])
        for positive, negative, reason in ((["cat /no"], ["rm /x"], "positive"),
                                            (["ls /x"], ["ls /x"], "negative")):
            with self.assertRaisesRegex(grants.GrantError, reason):
                grants.validate_pattern(r"ls /x", positive, negative)
        self.assertFalse(grants.pattern_matches(".+", "a" * 4097))

    def test_ac7_patterns_cover_interpreters_and_warn_about_guards(self):
        policy, grant = self.approved(sp.StagePolicy("resolve"), "python3 -c 'print(1)'", r"python3 -c .+")
        self.assertTrue(sp.check("python3 -c 'print(2)'", policy).allowed)
        self.assertIn("interpreter", grant.scopes[0].risks.lower())
        self.assertIn("protected command", grant.scopes[0].risks)
        record = grants.PermissionRequest(grant, grant.scopes[0], "resolve", {}).record()
        self.assertEqual(["python3 -c print(1)"], record["pattern"]["positive"])
        self.assertIn("Bash(*)", sp.allowed_tool_patterns(policy))

    def test_ac8_compound_consent_guards_every_segment_and_does_not_extend(self):
        with tempfile.TemporaryDirectory() as root:
            policy = sp.StagePolicy("resolve", tmpdir=root)
            command = "printf hello | cat"
            parsed = sp.parse_command(command, root)
            self.assertEqual([["printf", "hello"], ["cat"]], [item.words for item in parsed.segments])
            self.assertFalse(sp.check(command, policy).allowed)
            current, _ = self.approved(policy, command)
            self.assertTrue(sp.check(command, current).allowed)
            self.assertFalse(sp.check("printf hello | cat; rm file", current).allowed)
            single, _ = self.approved(policy, "printf hello")
            self.assertFalse(sp.check(command, single).allowed)
            for command in ("printf x | git push --force origin main", "echo $(git push --force origin main)",
                            "if true; then git push --force origin main; fi",
                            "for x in a; do git push --force origin main; done",
                            "{ git push --force origin main; }",
                            "echo ${x:-$(git push --force origin main)}",
                            "cat <<EOF > /outside/file\ndata\nEOF",
                            "cat <<EOF\n$(git push --force origin main)\nEOF",
                            "echo `git push --force origin main`", "cat <(git push --force origin main)",
                            "printf x | cat ~/.codex/auth.json", "cat $HOME/.codex/auth.json",
                            "cd ~/.codex; cat auth.json",
                            "printf x | cat > /outside/file"):
                with self.subTest(command=command):
                    current, _ = self.approved(policy, command)
                    self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, root))
                    self.assertFalse(sp.check(command, current, cwd=root).allowed)

    def test_ac8_claude_shell_baselines_and_guarded_execution(self):
        self.shell_baselines_and_guarded_execution("claude")

    def test_ac8_codex_shell_baselines_and_guarded_execution(self):
        self.shell_baselines_and_guarded_execution("codex")

    def shell_baselines_and_guarded_execution(self, executor):
        # These process-boundary cases exercise both executor policies. Native
        # CLI baselines are separate frozen checks, never inferred from these.
        for _ in (executor,):
            with self.subTest(executor=executor), tempfile.TemporaryDirectory() as root:
                policy = sp.StagePolicy("resolve", tmpdir=root)
                commands = ("printf hello | cat", "printf hello | cat 2>&1", "true && printf hello", "true; printf hello",
                            "cat <<EOF\nhello\nEOF", "printf '%s' $(printf hello)",
                            "printf '%s' ${value:-hello}", "printf '%s' *.txt",
                            "printf hello > $TMPDIR/out.log", "cat < /dev/null")
                Path(root, "hello.txt").touch()
                for command in commands:
                    with self.subTest(command=command):
                        baseline = subprocess.run(["bash", "-c", command], cwd=root,
                            env={**os.environ, "TMPDIR": root}, capture_output=True)
                        current, _ = self.approved(policy, command, executor=executor, cwd=root)
                        self.assertTrue(sp.check(command, current, cwd=root).allowed)
                        actual = subprocess.run(["bash", "-c", command], cwd=root,
                            env={**os.environ, "TMPDIR": root}, capture_output=True)
                        self.assertEqual((baseline.returncode, baseline.stdout), (actual.returncode, actual.stdout))

    def test_ac10_patterns_never_lift_protected_guards(self):
        policy = sp.StagePolicy("rereview", tmpdir="/tmp/current")
        current, grant = self.approved(policy, "ls /outside/api", r"(?:git|cat|echo|ls) .+")
        for command in ("git push --force origin main", "cat ~/.codex/auth.json", "echo data > /outside/file"):
            self.assertFalse(sp.check(command, current).allowed)
            self.assertIsNone(sp.matching_answer("Bash", {"command": command}, current, None))

    def test_ac11_hook_label_and_host_note(self):
        with tempfile.TemporaryDirectory() as root:
            policy = sp.StagePolicy("resolve", tmpdir=root)
            path = Path(root) / "policy.json"
            path.write_text(policy.to_json())
            output = io.StringIO()
            sp.hook_main([str(path)], stdin=io.StringIO(json.dumps({
                "tool_name": "Bash", "tool_input": {"command": "unknown-command"}})), stdout=output)
            self.assertIn("[layer=toolkit_hook]", output.getvalue())
            self.assertIn("unlabelled refusal", sp.prompt_note(policy))

    def test_ac7_ac9_pattern_proposal_edited_approval_and_real_hook(self):
        with tempfile.TemporaryDirectory() as root:
            store = grants.ConsentStore(Path(root) / "consents.json")
            policy = sp.StagePolicy("resolve", tmpdir=root)
            command = "cat /outside/api/schema.json"
            proposal = {"pattern": r"cat /outside/api/.+", "proposer": "agent",
                        "justification": "inspect API contract", "positive": [command],
                        "negative": ["cat /outside/secrets"]}
            broker = grants.ConsentBroker("owner/repo", store=store, platform="linux",
                prompt=lambda question: {"answer": "persistent", "pattern": r"(?:ls|cat) /outside/api[/a-z.]*",
                    "positive": [command], "negative": ["cat /outside/secrets"]})
            request = cp.refused_operation([{"tool": "Bash", "input": {"command": command},
                "pattern_proposal": proposal}], policy, broker, "claude")
            grant = broker.operations[request["resource"]]
            self.assertEqual(2, grant.operation["version"])
            self.assertEqual("persistent", broker.request(grant, "claude", role="resolve", blocker=request))
            current = replace(policy, command_permissions=broker.command_entries("claude", "resolve"))
            policy_path = Path(root) / "policy.json"
            policy_path.write_text(current.to_json())
            for command, allowed in (("ls /outside/api/", True), ("cat /outside/secrets", False)):
                completed = subprocess.run([sys.executable, str(Path(sp.__file__)), "hook", str(policy_path)],
                    input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": root}),
                    capture_output=True, text=True)
                self.assertEqual(0, completed.returncode)
                self.assertEqual(allowed, completed.stdout == "", completed.stdout)
            broker.record_executions(["ls /outside/api/"], "claude", current, root)
            entries = store.list("owner/repo")
            self.assertEqual(1, len(entries))
            self.assertEqual("ls /outside/api/", entries[0]["executed_commands"][0]["command"])

    def test_ac11_layer_records_include_both_executors(self):
        for executor in ("claude", "codex"):
            records = cp.denial_records([{"tool": "Bash", "input": {"command": "ls /outside/api"},
                "layer": executor + "_permission"}], sp.StagePolicy("resolve"), executor)
            self.assertEqual(executor + "_permission", records[0]["layer"])

    def test_ac9_execution_audit_keeps_verified_temporary_identity_after_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            scratch = Path(root) / "dispatch"
            scratch.mkdir()
            policy = sp.StagePolicy("resolve", tmpdir=str(scratch))
            command = "cat $TMPDIR/output"
            op = sp.operation("Bash", {"command": command}, policy, root)
            grant = grants.make_pattern_grant(r"cat <TMPDIR>/.+", "resolve", "claude", "linux",
                proposer="agent", justification="read check output", positive=["cat <TMPDIR>/output"],
                negative=["cat /outside/file"])
            store = grants.ConsentStore(Path(root) / "consents.json")
            store.save("owner/repo", grant, grant.scopes[0], "allow")
            scratch.rmdir()
            broker = grants.ConsentBroker("owner/repo", store=store, platform="linux")
            broker.record_executions([command], "claude", replace(policy, tmpdir=None), root,
                                     observed=[{"command": command, "operation": op}])
            self.assertEqual(command, store.list("owner/repo")[0]["executed_commands"][0]["command"])

    def test_ac12_documentation_contract(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "docs/host-permissions.md").read_text()
        for token in ("command_pattern_proposals", "positive", "negative", "toolkit_hook", "#439"):
            self.assertIn(token, text)
        audit = (root / "docs/capability-parity-audit.md").read_text()
        self.assertIn("PA-19", audit)
        self.assertIn("#439", audit)
        self.assertIn("#455", (root / "docs/ROADMAP_V2.md").read_text())


if __name__ == "__main__":
    unittest.main()
