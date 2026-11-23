"""Direct-agent baselines and recorded consent at the real process boundary (#457)."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import command_permissions as cp
import grants
import publication_audit as pa
import stage_permissions as sp
import executors
from router import Target


@unittest.skipIf(os.name == "nt", "POSIX command baselines; Linux CI covers these fixtures")
class SensitiveConsentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.remote = self.root / "remote.git"
        self.run_git("init", "--bare", str(self.remote))
        self.run_git("init", "-b", "topic")
        self.run_git("config", "user.name", "Fixture")
        self.run_git("config", "user.email", "fixture@example.invalid")
        self.run_git("commit", "--allow-empty", "-m", "baseline")
        self.run_git("remote", "add", "origin", str(self.remote))
        self.run_git("push", "origin", "HEAD:topic", "HEAD:main")
        self.policy = sp.StagePolicy(role="implement", repo="owner/repo", base_branch="main",
                                     tmpdir=str(self.root), cycle_id="fixture")

    def run_git(self, *args):
        done = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, timeout=30)
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout.strip()

    def approve(self, command, executor, policy=None):
        policy = policy or self.policy
        broker = grants.ConsentBroker("owner/repo", platform="linux", cycle_id="fixture")
        request = cp.refused_operation([command], policy, broker, executor, str(self.repo))
        if request and broker.operations[request["resource"]].operation["sensitive"]["kind"] == "git_inspection":
            inspection = broker.operations[request["resource"]]
            broker.apply_answer(inspection, inspection.scopes[0], "cycle")
            policy = replace(policy, command_permissions=broker.command_entries(executor, policy.role))
        request = cp.refused_operation([command], policy, broker, executor, str(self.repo))
        self.assertIsNotNone(request, command)
        grant = broker.operations[request["resource"]]
        question = grants.question(grant, grant.scopes[0], role=policy.role, blocker=request)
        self.assertIn("WARNING", question["prompt"])
        broker.apply_answer(grant, grant.scopes[0], "cycle")
        return replace(policy, command_permissions=broker.command_entries(executor, policy.role)), grant, broker

    def test_host_git_inspection_needs_separate_consent_for_selected_configuration(self):
        selected = self.root / "selected"
        self.run_git("init", str(selected))
        marker = self.root / "host-executed"
        transport = self.root / "ssh-fixture"
        transport.write_text("#!/bin/sh\n: > " + shlex.quote(str(marker)) +
                             "\nexec git-upload-pack " + shlex.quote(str(self.remote)) + "\n")
        transport.chmod(0o755)
        self.run_git("-C", str(selected), "config", "core.sshCommand", str(transport))
        self.run_git("-C", str(selected), "config", "ssh.variant", "ssh")
        self.run_git("-C", str(selected), "remote", "add", "origin", "ssh://fixture.invalid/repo")
        for prefix in (f"git -C {selected}", f"git --git-dir={selected}/.git",
                       f"GIT_DIR={selected}/.git git", f"env -C {selected} git"):
            with self.subTest(prefix=prefix):
                marker.unlink(missing_ok=True)
                command = prefix + " push --force origin :topic"
                data = {"command": command}
                operation = sp.operation("Bash", data, self.policy, str(self.repo))
                self.assertFalse(marker.exists(), "binding inspection ran before consent")
                self.assertEqual("git_inspection", operation["sensitive"]["kind"])
                self.assertIn("host-side", operation["sensitive"]["warning"])
                grant = grants.make_grant(operation, "claude", "linux")
                broker = grants.ConsentBroker("owner/repo", platform="linux", cycle_id="fixture")
                broker.operations[grant.id] = grant
                broker.apply_answer(grant, grant.scopes[0], "cycle")
                allowed = replace(self.policy, command_permissions=broker.command_entries("claude", "implement"))
                resolved = sp.operation("Bash", data, allowed, str(self.repo))
                self.assertTrue(marker.exists(), "the explicitly authorized inspector did not run")
                self.assertEqual("git_refs", resolved["sensitive"]["kind"])
                self.assertFalse(sp.check(command, allowed, cwd=str(self.repo)).allowed,
                                 "inspection consent must never authorize the push")
                other_role = replace(allowed, role="resolve")
                self.assertEqual("git_inspection", sp.operation("Bash", data, other_role,
                                                               str(self.repo))["sensitive"]["kind"])

    def test_push_exceptions_need_warned_exact_consent_not_patterns(self):
        for executor in ("claude", "codex"):
            for command in ("git push https://example.invalid/x.git HEAD:topic",
                            "git push origin HEAD:refs/tags/v9",
                            "git push origin HEAD:refs/notes/fixture"):
                with self.subTest(command=command, executor=executor):
                    broker = grants.ConsentBroker("owner/repo", platform="linux")
                    request = cp.refused_operation([command], self.policy, broker, executor, str(self.repo))
                    self.assertIsNotNone(request)
                    grant = broker.operations[request["resource"]]
                    self.assertEqual("git_inspection", grant.operation["sensitive"]["kind"])
                    self.assertEqual(["this cycle", "deny"], grants.question(
                        grant, grant.scopes[0], role="implement")["options"])
                    pattern = grants.make_pattern_grant("git push .*", "implement", executor, "linux",
                        proposer="fixture", justification="regression", positive=[command], negative=["git status"])
                    allowed = replace(self.policy, command_permissions=({"operation": pattern.operation,
                                                                         "decision": "allow"},))
                    self.assertFalse(sp.check(command, allowed, cwd=str(self.repo)).allowed)

    def test_push_exception_consent_discloses_audit_limits_and_runs(self):
        other = self.root / "other.git"
        self.run_git("init", "--bare", str(other))
        self.run_git("remote", "add", "other", str(other))
        for executor in ("claude", "codex"):
            for command in ("git push other HEAD:topic", f"git push {other} HEAD:topic",
                            "git push origin HEAD:refs/tags/v9", "git push origin HEAD:refs/notes/fixture"):
                with self.subTest(command=command, executor=executor):
                    allowed, grant, _ = self.approve(command, executor)
                    self.assertIn("outside publication-audit coverage", grant.operation["sensitive"]["warning"])
                    self.assertFalse(grants.pattern_covers_operation({"pattern": {"regex": "git push .*"}},
                                                                    grant.operation))
                    self.assertTrue(self.hook(command, allowed))
                    done = subprocess.run(shlex.split(command), cwd=self.repo, capture_output=True, timeout=30)
                    self.assertEqual(0, done.returncode, done.stderr)

    def test_git_configuration_warning_discloses_whole_file_and_host_scope(self):
        for executor in ("claude", "codex"):
            with self.subTest(executor=executor):
                allowed, grant, _ = self.approve("git config fixture.setting value", executor)
                warning = grants.question(grant, grant.scopes[0], role="implement")["prompt"]
                self.assertIn("host-side inspection outside the stage sandbox", warning)
                self.assertIn("whole approved file", warning)
                self.assertIn("common Git directory", warning)
                self.assertEqual((str(self.repo / ".git/config"),), sp.consented_git_edits(allowed))

    def test_selected_head_destination_requires_inspection_and_ref_consent(self):
        selected = self.root / "selected"
        self.run_git("clone", str(self.remote), str(selected))
        self.run_git("-C", str(selected), "checkout", "main")
        for destination in ("HEAD", "@"):
            command = f"git -C {selected} push origin topic:{destination}"
            # The source need not exist for classification; nothing runs yet.
            with patch.object(sp.subprocess, "run", side_effect=AssertionError("unconsented inspection")):
                self.assertIsNotNone(sp.sensitive_operation("Bash", {"command": command}, self.policy,
                                                          str(self.repo), resolve=False))
                self.assertEqual("git_inspection", sp.operation("Bash", {"command": command},
                                                               self.policy, str(self.repo))["sensitive"]["kind"])
            for executor in ("claude", "codex"):
                grant = grants.make_pattern_grant("git .*push.*", "implement", executor, "linux",
                    proposer="fixture", justification="regression", positive=[command], negative=["git status"])
                allowed = replace(self.policy, command_permissions=({"operation": grant.operation, "decision": "allow"},))
                self.assertFalse(sp.check(command, allowed, cwd=str(self.repo)).allowed)

    def test_tag_publication_forms_bind_exact_tags_and_warn(self):
        self.run_git("tag", "lightweight")
        self.run_git("tag", "-a", "annotated", "-m", "fixture")
        for executor in ("claude", "codex"):
            for arguments, tags in (("HEAD:topic --tags", {"lightweight", "annotated"}),
                                    ("--tags", {"lightweight", "annotated"}),
                                    ("HEAD:topic --follow-tags", {"annotated"}),
                                    ("lightweight", {"lightweight"}),
                                    ("refs/tags/annotated:newtag", {"newtag"}),
                                    ("tag lightweight", {"lightweight"})):
                with self.subTest(executor=executor, arguments=arguments):
                    command = "git push origin " + arguments
                    allowed, grant, _ = self.approve(command, executor)
                    moves = grant.operation["sensitive"]["movements"]
                    self.assertEqual({"refs/tags/" + tag for tag in tags},
                                     {move["ref"] for move in moves if move["ref"].startswith("refs/tags/")})
                    self.assertTrue(self.hook(command, allowed))
                    direct = subprocess.run(shlex.split(command), cwd=self.repo, capture_output=True, timeout=30)
                    self.assertEqual(0, direct.returncode, direct.stderr)
                    for tag in tags:
                        self.run_git("push", "origin", "--delete", "refs/tags/" + tag)

    def test_inspection_consent_normalizes_token_spelling_and_dispatch_paths(self):
        policies = [replace(self.policy, tmpdir=str(self.root / name)) for name in ("first", "second")]
        operations = []
        for policy in policies:
            path = Path(policy.tmpdir)
            path.mkdir()
            command = "git -C " + shlex.quote(str(path)) + " push --force origin HEAD:topic"
            operations.append(sp.operation("Bash", {"command": command}, policy, str(self.repo)))
        self.assertTrue(sp.same_operation(operations[0], operations[1]))
        command = "git   -C '" + str(self.root / "second") + "' push --force origin HEAD:topic"
        self.assertTrue(sp.same_operation(operations[1], sp.operation("Bash", {"command": command},
                                                                     policies[1], str(self.repo))))

    def hook(self, command, policy):
        path = self.root / "policy.json"
        path.write_text(policy.to_json())
        done = subprocess.run([sys.executable, str(Path(sp.__file__)), "hook", str(path)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.repo)}),
            text=True, capture_output=True, timeout=30)
        self.assertEqual(0, done.returncode, done.stderr)
        return not done.stdout.strip()

    def parity(self, row, executor):
        if row in (25, 26, 27):
            command = {25: "git push --force origin HEAD:topic", 26: "git push origin HEAD:main",
                       27: "git push origin --delete topic"}[row]
            old = self.run_git("rev-parse", "HEAD")
            if row == 25:
                tree = self.run_git("rev-parse", "HEAD^{tree}")
                replacement = self.run_git("commit-tree", tree, "-m", "replacement root")
                self.run_git("update-ref", "refs/heads/topic", replacement)
                refused = subprocess.run(["git", "push", "origin", "HEAD:topic"], cwd=self.repo,
                                         capture_output=True, timeout=30)
                self.assertNotEqual(0, refused.returncode)
            else:
                self.run_git("commit", "--allow-empty", "-m", "new")
            before = self.run_git("ls-remote", "origin", "refs/heads/topic", "refs/heads/main")
            # Same binary, cwd, credentials and remote for direct and mediated runs.
            direct = subprocess.run(shlex.split(command), cwd=self.repo, capture_output=True, timeout=30)
            self.assertEqual(0, direct.returncode, direct.stderr)
            after = self.run_git("ls-remote", "origin", "refs/heads/topic", "refs/heads/main")
            self.run_git("push", "--force", "origin", f"{old}:refs/heads/topic", f"{old}:refs/heads/main")
            self.assertEqual(before, self.run_git("ls-remote", "origin", "refs/heads/topic", "refs/heads/main"))
            self.assertFalse(self.hook(command, self.policy))
            allowed, grant, broker = self.approve(command, executor)
            self.assertTrue(self.hook(command, allowed))
            self.assertTrue(grant.operation["sensitive"]["cycle_only"])
            self.assertEqual(["this cycle", "deny"], grants.question(grant, grant.scopes[0], role="implement")["options"])
            for scope in ("persistent", "forever"):
                with self.assertRaises(grants.GrantError):
                    broker.apply_answer(grant, grant.scopes[0], scope)
            mediated = subprocess.run(shlex.split(command), cwd=self.repo, capture_output=True, timeout=30)
            self.assertEqual(direct.returncode, mediated.returncode)
            self.assertEqual(after, self.run_git("ls-remote", "origin", "refs/heads/topic", "refs/heads/main"))
            self.assertFalse(self.hook(command, allowed), "changed old SHA must ask again")
        elif row == 28:
            home = self.root / "home"
            (home / ".ssh").mkdir(parents=True)
            fixture = home / ".ssh/config"
            fixture.write_text("fixture-only\n")
            with patch.dict(os.environ, HOME=str(home)):
                command = "cat " + shlex.quote(str(fixture))
                direct = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=30)
                self.assertFalse(self.hook(command, self.policy))
                allowed, _, _ = self.approve(command, executor)
                self.assertTrue(self.hook(command, allowed))
                self.assertEqual(("~/.ssh",), sp.consented_credential_reads(allowed))
                self.assertTrue(self.hook(command, allowed))
                mediated = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=30)
                self.assertEqual((direct.returncode, direct.stdout), (mediated.returncode, mediated.stdout))
                self.assertFalse(self.hook("gh auth token", allowed))
        elif row == 29:
            target = self.repo / ".git/info/exclude"
            original = target.read_text()
            target.write_text("fixture\n")  # The agent's native file edit baseline.
            direct = target.read_text()
            target.write_text(original)
            data = {"file_path": str(target)}
            op = sp.operation("Write", data, self.policy, str(self.repo))
            self.assertIn("common Git directory", op["sensitive"]["warning"])
            self.assertFalse(sp.check_file_edit("Write", data, self.policy, cwd=str(self.repo)).allowed)
            grant = grants.make_grant(op, executor, "linux")
            broker = grants.ConsentBroker("owner/repo", platform="linux")
            broker.operations[grant.id] = grant
            broker.apply_answer(grant, grant.scopes[0], "cycle")
            allowed = replace(self.policy, command_permissions=broker.command_entries(executor, "implement"))
            self.assertTrue(sp.check_file_edit("Write", data, allowed, cwd=str(self.repo)).allowed)
            target.write_text("fixture\n")
            self.assertEqual(direct, target.read_text())
        elif row == 31:
            policy = replace(self.policy, static_only=True)
            command = "git push origin HEAD:topic"
            direct = subprocess.run(shlex.split(command), cwd=self.repo, capture_output=True, timeout=30)
            self.assertEqual(0, direct.returncode)
            self.assertFalse(self.hook(command, policy))
            allowed, _, _ = self.approve(command, executor, policy)
            self.assertTrue(self.hook(command, allowed))
            mediated = subprocess.run(shlex.split(command), cwd=self.repo, capture_output=True, timeout=30)
            self.assertEqual(direct.returncode, mediated.returncode)
            self.assertFalse(self.hook("python3 -m unittest", allowed))

    def test_credential_scope_is_exact_and_not_inherited_by_any_other_role(self):
        command = "cat " + shlex.quote(str(Path.home() / ".ssh/config"))
        allowed, _, _ = self.approve(command, "claude")
        for role in ("review", "rereview", "verify", "issue_review", "security", "coordinate"):
            policy = replace(allowed, role=role)
            self.assertFalse(sp.check(command, policy, cwd=str(self.repo)).allowed)
            fresh, grant, broker = self.approve(command, "claude", replace(self.policy, role=role))
            self.assertTrue(sp.check(command, fresh, cwd=str(self.repo)).allowed)
            self.assertEqual(["this cycle", "deny"], grants.question(grant, grant.scopes[0], role=role)["options"])
            for other in ("implement", "review", "rereview", "verify", "issue_review", "security", "coordinate"):
                if role != other:
                    self.assertEqual((), broker.command_entries("claude", other))

    def test_provider_limit_is_preserved_after_consent(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\necho provider-protection >&2\nexit 1\n")
        hook.chmod(0o755)
        self.run_git("commit", "--allow-empty", "-m", "rejected")
        command = "git push --force origin HEAD:main"
        allowed, _, _ = self.approve(command, "claude")
        self.assertTrue(self.hook(command, allowed))
        done = subprocess.run(shlex.split(command), cwd=self.repo, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(0, done.returncode)
        self.assertIn("provider-protection", done.stderr)

    def test_shell_credential_scope_and_native_sandbox_emission(self):
        home = self.root / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".codex").mkdir()
        (home / ".ssh/config").write_text("fixture")
        (home / ".codex/auth.json").write_text("fixture")
        with patch.dict(os.environ, HOME=str(home)):
            command = "cd ~/.ssh && cat config"
            for executor in ("claude", "codex"):
                allowed, _, _ = self.approve(command, executor)
                self.assertTrue(self.hook(command, allowed))
                stores = sp.consented_credential_reads(allowed)
                self.assertEqual(("~/.ssh",), stores)
                if executor == "claude":
                    argv = ["claude", "--verbose", "-p", "fixture"]
                    with executors.ClaudeAdapter().policy_files(argv, allowed, scratch=str(self.root),
                                                                cwd=str(self.repo)) as emitted:
                        settings = json.loads(emitted[emitted.index("--settings") + 1])
                        denied = settings["sandbox"]["filesystem"]["denyRead"]
                        self.assertNotIn(str(home / ".ssh"), denied)
                        self.assertIn(str(home / ".codex/auth.json"), denied)
                else:
                    argv = executors.CodexAdapter().argv(Target("codex", "openai", "fixture", "medium"),
                        "fixture", cwd=str(self.repo), writes=True, credential_reads=stores)
                    filesystem = next(word for word in argv if word.startswith("permissions.") and ".filesystem=" in word)
                    self.assertNotIn(json.dumps(str(home / ".ssh")), filesystem)
                    self.assertIn(json.dumps(str(home / ".codex/auth.json")), filesystem)

    def test_ref_selectors_and_implicit_push_configuration_are_bound(self):
        self.run_git("config", "remote.origin.push", "HEAD:refs/heads/main")
        command = "git push --force origin"
        allowed, grant, _ = self.approve(command, "claude")
        self.assertEqual("refs/heads/main", grant.operation["sensitive"]["movements"][0]["ref"])
        self.assertTrue(self.hook(command, allowed))
        self.run_git("config", "remote.origin.push", "HEAD:refs/heads/topic")
        self.assertFalse(self.hook(command, allowed))

    def test_reported_metadata_cannot_widen_credential_consent(self):
        policy = replace(self.policy, role="verify")
        command = "cat " + shlex.quote(str(Path.home() / ".ssh/config"))
        operation = sp.operation("Bash", {"command": command}, policy, str(self.repo))
        operation["sensitive"].update(role_bound=False, cycle_only=False, stores=["~/.aws"])
        broker = grants.ConsentBroker("owner/repo", platform="linux")
        request = cp.refused_operation([{"tool": "Bash", "input": {"command": command},
                                         "operation": operation}], policy, broker, "claude", str(self.repo))
        grant = broker.operations[request["resource"]]
        self.assertEqual(["~/.ssh"], grant.operation["sensitive"]["stores"])
        self.assertTrue(grant.operation["sensitive"]["cycle_only"])
        self.assertTrue(grant.operation["sensitive"]["role_bound"])

    def test_multiple_push_endpoints_each_get_exact_bindings(self):
        other = self.root / "other.git"
        self.run_git("init", "--bare", str(other))
        self.run_git("config", "--add", "remote.origin.pushurl", str(self.remote))
        self.run_git("config", "--add", "remote.origin.pushurl", str(other))
        command = "git push --force origin HEAD:main"
        allowed, grant, _ = self.approve(command, "claude")
        moves = grant.operation["sensitive"]["movements"]
        self.assertEqual(2, len(moves))
        self.assertNotEqual(moves[0]["remote_identity"], moves[1]["remote_identity"])
        self.assertTrue(self.hook(command, allowed))
        self.run_git("push", "--force", "origin", "HEAD:main")

    def test_abbreviated_options_and_matching_refspec_retain_ref_bindings(self):
        command = "git push --del origin topic"
        old = self.run_git("rev-parse", "HEAD")
        direct = subprocess.run(shlex.split(command), cwd=self.repo, capture_output=True, timeout=30)
        self.assertEqual(0, direct.returncode)
        self.run_git("push", "origin", f"{old}:refs/heads/topic")
        allowed, grant, _ = self.approve(command, "claude")
        self.assertEqual("refs/heads/topic", grant.operation["sensitive"]["movements"][0]["ref"])
        self.assertTrue(self.hook(command, allowed))
        self.run_git("push", "--del", "origin", "topic")
        self.run_git("push", "origin", f"{old}:refs/heads/topic")
        _, grant, _ = self.approve("git push --force origin +:", "claude")
        self.assertEqual(["refs/heads/topic"], [move["ref"] for move in grant.operation["sensitive"]["movements"]])
        self.assertEqual(old, grant.operation["sensitive"]["movements"][0]["after"])

    def test_audit_matches_a_relative_remote_spelling_to_the_observed_endpoint(self):
        class Host:
            def protected_branches(self, repo): return ()
            def change_requests(self, *args): return {}
        auditor = pa.PublicationAuditor(str(self.repo), "owner/repo", Host(), default_branch="main")
        before = auditor.capture(checkout_branch="topic")
        self.run_git("commit", "--allow-empty", "-m", "new")
        command = "git push --force ../remote.git HEAD:main"
        allowed, _, _ = self.approve(command, "claude")
        self.run_git("push", "--force", "../remote.git", "HEAD:main")
        result = auditor.verify(before, consents=allowed.command_permissions)
        self.assertEqual(pa.PASSED, result.status)
        self.assertEqual("../remote.git", result.authorized_changes[0]["consented_remote"])

    def test_consent_inspection_contacts_selected_endpoints_only_after_consent(self):
        stranger = self.root / "stranger.git"
        self.run_git("init", "--bare", str(stranger))
        contacted = []
        real = sp.subprocess.run

        def spy(command, *args, **kwargs):
            if "ls-remote" in command:
                contacted.append(command[-1])
            return real(command, *args, **kwargs)

        for command in (f"git push --force {stranger} HEAD:main",
                        f"git push --force --repo={stranger} HEAD:main",
                        f"git -c remote.origin.pushurl={stranger} push --force origin HEAD:main"):
            with self.subTest(command=command), patch.object(sp.subprocess, "run", spy):
                contacted.clear()
                first = sp.operation("Bash", {"command": command}, self.policy, str(self.repo))
                self.assertEqual("git_inspection", first["sensitive"]["kind"])
                broker = grants.ConsentBroker("owner/repo", platform="linux", cycle_id="fixture")
                self.assertIsNotNone(cp.refused_operation([command], self.policy, broker, "claude", str(self.repo)))
                self.assertEqual([], contacted)
                allowed, grant, _ = self.approve(command, "claude")
                self.assertIn(str(stranger), contacted)
                self.assertTrue(grant.operation["sensitive"]["movements"])
                self.assertTrue(self.hook(command, allowed))

    def test_unconfined_publication_does_not_authorize_compound_verification(self):
        policy = replace(self.policy, static_only=True, test_command="python3 -m unittest")
        command = "git push --force origin HEAD:topic"
        allowed, _, _ = self.approve(command, "claude", policy)
        self.assertTrue(self.hook(command, allowed))
        self.assertFalse(self.hook(command + " && python3 -m unittest", allowed))

    def test_audit_authorizes_each_observed_ref_and_deletion(self):
        class Host:
            def protected_branches(self, repo): return ("protected",)
            def change_requests(self, *args): return {}
        self.run_git("push", "origin", "HEAD:protected")
        auditor = pa.PublicationAuditor(str(self.repo), "owner/repo", Host(), default_branch="main")
        for name in ("topic", "main", "protected"):
            before = auditor.capture(checkout_branch="topic")
            command = f"git push origin --delete {name}"
            allowed, _, _ = self.approve(command, "claude")
            self.run_git("push", "origin", "--delete", name)
            result = auditor.verify(before, consents=allowed.command_permissions)
            self.assertEqual(pa.PASSED, result.status)
            self.assertEqual(1, len(result.authorized_changes))
            self.assertEqual(pa.VIOLATED, auditor.verify(before).status)
            self.run_git("push", "origin", f"HEAD:refs/heads/{name}")

    def test_publication_audit_records_only_matching_remote_ref_and_shas(self):
        class Host:
            def protected_branches(self, repo): return ("protected",)
            def change_requests(self, *args): return {}
        self.run_git("push", "origin", "HEAD:protected")
        auditor = pa.PublicationAuditor(str(self.repo), "owner/repo", Host(), default_branch="main")
        before = auditor.capture(checkout_branch="topic")
        self.run_git("commit", "--allow-empty", "-m", "move")
        allowed, _, _ = self.approve("git push --force origin HEAD:main", "claude")
        self.run_git("push", "--force", "origin", "HEAD:main")
        result = auditor.verify(before, consents=allowed.command_permissions)
        self.assertEqual(pa.PASSED, result.status)
        self.assertEqual(1, len(result.authorized_changes))
        self.assertEqual(pa.VIOLATED, auditor.verify(before).status)
        for field, value in (("ref", "refs/heads/protected"),
                             ("before", "0" * 40), ("after", "0" * 40), ("remote_identity", "other")):
            entries = json.loads(json.dumps(allowed.command_permissions))
            entry = next(item for item in entries if item["operation"]["sensitive"]["kind"] == "git_refs")
            entry["operation"]["sensitive"]["movements"][0][field] = value
            self.assertEqual(pa.VIOLATED, auditor.verify(before, consents=tuple(entries)).status)

    def test_documentation_and_reduced_roadmap_scope(self):
        root = Path(__file__).resolve().parents[1]
        audit = (root / "docs/capability-parity-audit.md").read_text()
        for row in (25, 26, 27, 28, 29, 31):
            line = next(line for line in audit.splitlines() if line.startswith(f"| PA-{row} |"))
            self.assertIn("#457", line)
            self.assertIn("consent", line)
        pa30 = next(line for line in audit.splitlines() if line.startswith("| PA-30 |"))
        self.assertIn("role contract", pa30)
        child = next(line for line in audit.splitlines() if line.startswith("| #457 |"))
        self.assertNotIn("PA-29, PA-30", child)
        for page in ("host-permissions", "configuration"):
            contents = (root / f"docs/{page}.md").read_text()
            self.assertIn("#457", contents)
            self.assertIn("old/new SHAs", contents)
            self.assertIn("common Git directory", contents)
        for skill in ("cc-orchestrator", "cc-orca-orchestrator", "cc-issue-review"):
            contents = (root / "skills" / skill / "SKILL.md").read_text()
            self.assertNotIn("Never offer cycle, repository, or forever consent for credential reads", contents)
            self.assertIn("fresh cycle-scoped consent", contents)
        roadmap = (root / "docs/ROADMAP_V2.md").read_text()
        self.assertIn("PA-30 stays a role contract", roadmap)


for _row in (25, 26, 27, 28, 29, 31):
    for _executor in ("claude", "codex"):
        def _test(self, row=_row, executor=_executor):
            self.parity(row, executor)
        setattr(SensitiveConsentTests, f"test_pa{_row}_{_executor}_direct_agent_parity", _test)


if __name__ == "__main__":
    unittest.main()
