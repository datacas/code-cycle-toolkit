"""Regression coverage for the ordinary reads in #524."""
from __future__ import annotations

import json
import fnmatch
import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import stage_permissions as sp


class ReadParityTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX Bash baselines; native Windows parity is separate")
    def test_hook_matches_direct_command_baselines_for_both_executor_policies(self):
        # This executes the shared hook, not a native Claude/Codex dispatch.
        # The native comparison remains a separately frozen check.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            outside = root / "outside"
            work.mkdir()
            outside.mkdir()
            for target in (work, outside):
                subprocess.run(["git", "init", "-q", "-b", "main"], cwd=target, check=True)
                for name in ("scripts", "docs", "skills", "tests"):
                    (target / name).mkdir()
                (target / "scripts/read.py").write_text("parity = True\n")
                (target / "AGENTS.md").write_text("parity\n")
                (target / "snapshot.json").write_text('{"issue": {"id": 524}}')
                (target / "tests/__init__.py").write_text("")
                (target / "tests/test_fixture.py").write_text("import unittest\nclass Fixture(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n")
                for argv in (["git", "add", "."], ["git", "-c", "user.name=fixture", "-c",
                             "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
                             ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                             ["git", "config", "branch.main.remote", "origin"],
                             ["git", "config", "branch.main.merge", "refs/heads/main"],
                             ["git", "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"],
                             ["git", "config", "remote.origin.url", str(outside)]):
                    subprocess.run(argv, cwd=target, check=True, capture_output=True)
            binpath = root / "bin"
            binpath.mkdir()
            fake = binpath / "gh"
            fake.write_text('#!/bin/sh\nprintf "fixture-provider-output\\n"\n')
            fake.chmod(0o755)
            env = {**os.environ, "PATH": f"{binpath}:{os.environ['PATH']}", "TMPDIR": directory,
                   "GIT_SSH_COMMAND": "fixture-ssh", "GIT_SSH": "fixture-ssh"}
            commands = [
                "git diff HEAD..HEAD --stat", "git diff HEAD -- scripts/",
                f"git -C {outside} diff HEAD --stat",
                'git grep -n -E "parity|missing" -- docs/ skills/ scripts/*.py',
                "grep -n parity scripts/*.py",
                f"git -C {outside} merge-base HEAD origin/main",
                f"git -C {outside} grep -n parity -- scripts/",
                "git rev-parse --abbrev-ref --symbolic-full-name @{u}",
                "git show HEAD:AGENTS.md | grep parity",
                r'git show HEAD:AGENTS.md | grep -n "parity\|consent"',
                r'git show HEAD:AGENTS.md | grep -n "\.\|\bparity\b"',
                "gh pr view 7 --json headRefOid --jq '.headRefOid'",
                "gh api repos/owner/repo/issues/524/comments --paginate --jq '.[].body'",
                "printenv GIT_SSH_COMMAND GIT_SSH",
                "python3 -m unittest tests.test_fixture",
                "python3 -m unittest discover -s tests",
                "python3 -I -c 'import json,sys;d=json.load(open(sys.argv[1]));print(list(d.keys()));i=d.get(\"issue\") or d.get(\"work_item\");print(type(i),list(i.keys()) if isinstance(i,dict) else \"\")' snapshot.json",
            ]
            for executor in ("claude", "codex"):
                policy = sp.StagePolicy(role="review", repo="owner/repo", tmpdir=directory,
                                        verify_prefixes=("python3 -m unittest",))
                path = root / "policy.json"
                path.write_text(policy.to_json())
                for command in commands:
                    with self.subTest(executor=executor, command=command):
                        direct = subprocess.run(["bash", "-c", command], cwd=work, env=env,
                                                capture_output=True, text=True, timeout=30)
                        hook = subprocess.run([sys.executable, sp.__file__, "hook", str(path)],
                            cwd=work, env=env, capture_output=True, text=True, timeout=30,
                            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(work)}))
                        self.assertEqual((0, ""), (hook.returncode, hook.stdout), hook.stderr)
                        actual = subprocess.run(["bash", "-c", command], cwd=work, env=env,
                                                capture_output=True, text=True, timeout=30)
                        self.assertEqual((direct.returncode, direct.stdout), (actual.returncode, actual.stdout))
                        self.assertEqual(0, direct.returncode, direct.stderr)

    def test_executor_refusal_keeps_layer_and_setting(self):
        import command_permissions as cp
        policy = sp.StagePolicy(role="review")
        records = cp.denial_records([{"tool": "Bash", "input": {"command": "git status"},
                                     "layer": "claude_permission"}], policy, "claude", str(ROOT))
        self.assertEqual("claude_permission", records[0]["layer"])
        self.assertIn("--allowedTools", records[0]["reason"])
        self.assertIn("permissions.allow/deny", records[0]["reason"])

    def test_glob_dialects_cannot_hide_option_named_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "-e1eid").write_text("fixture", encoding="utf-8")
            (root / ".-e1eid").write_text("fixture", encoding="utf-8")
            policy_path = root / "policy.json"
            cases = [
                ({}, "[^x]*"), ({}, "[[:punct:]]*"),
                ({}, "[[=x=]]*"), ({}, "[[.x.]]*"),
                ({"BASHOPTS": "dotglob"}, "*"),
                ({"BASHOPTS": "nocaseglob"}, "*"),
                ({"BASHOPTS": "globstar"}, "**/*"),
                ({"GLOBIGNORE": "ignored"}, "*"),
            ]
            for role in ("implement", "resolve", "review", "rereview", "issue_review",
                         "security", "verify", "bootstrap", "coordinate"):
                policy = sp.StagePolicy(role=role, tmpdir=directory, rtk=True)
                policy_path.write_text(policy.to_json(), encoding="utf-8")
                for environment, operand in cases:
                    for wrapper in ("", "rtk ", "rtk proxy "):
                        command = f"{wrapper}sed -n p {operand} | head"
                        with self.subTest(role=role, command=command, environment=environment), \
                                patch.dict(os.environ, {"BASHOPTS": "", "GLOBIGNORE": "", **environment}):
                            decision = sp.check(command, policy, cwd=str(root))
                            self.assertFalse(decision.allowed)
                            self.assertNotEqual("validation_error", decision.rule)
                            output = io.StringIO()
                            event = {"tool_name": "Bash", "cwd": str(root),
                                     "tool_input": {"command": command}}
                            sp.hook_main([str(policy_path)], io.StringIO(json.dumps(event)), output)
                            response = json.loads(output.getvalue())["hookSpecificOutput"]
                            self.assertEqual("deny", response["permissionDecision"])
                            self.assertNotIn("could not", response["permissionDecisionReason"])

    @unittest.skipIf(os.name == "nt", "POSIX Bash glob comparison")
    def test_bash_glob_option_injection_is_refused_at_the_cli_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "-e1eid").write_text("fixture", encoding="utf-8")
            policy = root / "policy.json"
            policy.write_text(sp.StagePolicy(role="review", tmpdir=directory).to_json(), encoding="utf-8")
            for pattern in ("[^x]*", "[[:punct:]]*"):
                # printf shows Bash's expanded argv; never run sed's e command.
                argv = subprocess.run(["bash", "-c", "printf '%s\\n' " + pattern],
                                      cwd=root, capture_output=True, text=True, check=True)
                self.assertIn("-e1eid", argv.stdout.splitlines())
                command = f"sed -n p {pattern} | head"
                result = subprocess.run([sys.executable, sp.__file__, "hook", str(policy)],
                    cwd=root, capture_output=True, text=True, timeout=30,
                    input=json.dumps({"tool_name": "Bash", "cwd": str(root),
                                      "tool_input": {"command": command}}))
                self.assertEqual(0, result.returncode, result.stderr)
                response = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual("deny", response["permissionDecision"])

    def test_inline_python_uses_the_ordinary_read_path_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            temp = root / "dispatch"
            temp.mkdir()
            evidence = root / "evidence.json"
            output = root / "tool-results"
            output.mkdir()
            policy = sp.StagePolicy(role="issue_review", tmpdir=str(temp),
                                    evidence_paths=(str(evidence),), tool_results=str(output))
            paths = [(work / "snapshot.json", True), (temp / "snapshot.json", True),
                     (evidence, True), (output / "result.txt", True),
                     (root / "outside.json", False), (Path("/proc/self/environ"), False),
                     (Path.home() / ".git-credentials", False),
                     (Path.home() / ".netrc", False),
                     (Path.home() / ".config/bitbucket/bitbucket.env", False)]
            for path, allowed in paths:
                value = path.as_posix()
                commands = [
                    "python3 -I -c " + shlex.quote(f"print(open({value!r}).read())"),
                    "python3 -I -c 'import sys;print(open(sys.argv[1]).read())' " + shlex.quote(value),
                    "python3 -I -c 'print(1)' " + shlex.quote(value),
                ]
                for command in commands:
                    with self.subTest(command=command):
                        self.assertEqual(allowed, sp.check(command, policy, cwd=str(work)).allowed)

    def test_glob_order_cannot_select_a_script_or_git_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in ("p", "1e id", "status", "push", "README.md"):
                (root / filename).write_text("fixture", encoding="utf-8")
            for role in ("implement", "resolve", "review", "rereview", "issue_review",
                         "security", "verify", "bootstrap", "coordinate"):
                policy = sp.StagePolicy(role=role, tmpdir=directory, rtk=True)
                for wrapper in ("", "rtk ", "rtk proxy "):
                    for command in ("sed -n * | head", "sed -n -e * README.md | head",
                                    "sed -n * README.md | head", "git * | head",
                                    "git -C * show | head"):
                        with self.subTest(role=role, command=wrapper + command):
                            decision = sp.check(wrapper + command, policy, cwd=str(root))
                            self.assertFalse(decision.allowed)
                            self.assertNotEqual("validation_error", decision.rule)
                    # A literal script keeps the glob in file-operand position.
                    self.assertTrue(sp.check(wrapper + "sed -n p * | head", policy, cwd=str(root)).allowed)

    @unittest.skipIf(os.name == "nt", "POSIX symlink and CLI boundary")
    def test_python_read_boundary_keeps_symlinks_and_hook_results_guarded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            outside = root / "outside.json"
            outside.write_text('{"fixture": true}', encoding="utf-8")
            (work / "link.json").symlink_to(outside)
            (work / "snapshot.json").write_text(outside.read_text(encoding="utf-8"), encoding="utf-8")
            policy_path = root / "policy.json"
            for role in ("implement", "resolve", "review", "rereview", "issue_review",
                         "security", "verify", "bootstrap", "coordinate"):
                policy = sp.StagePolicy(role=role, tmpdir=str(root / "dispatch"), rtk=True)
                policy_path.write_text(policy.to_json(), encoding="utf-8")
                for filename, allowed in (("snapshot.json", True), ("link.json", False)):
                    for wrapper in ("", "rtk ", "rtk proxy "):
                        command = wrapper + "python3 -I -c 'import sys;print(open(sys.argv[1]).read())' " + filename
                        with self.subTest(role=role, command=command):
                            result = subprocess.run([sys.executable, sp.__file__, "hook", str(policy_path)],
                                cwd=work, capture_output=True, text=True, timeout=30,
                                input=json.dumps({"tool_name": "Bash", "cwd": str(work),
                                                  "tool_input": {"command": command}}))
                            self.assertEqual(0, result.returncode, result.stderr)
                            if allowed:
                                self.assertEqual("", result.stdout)
                            else:
                                response = json.loads(result.stdout)["hookSpecificOutput"]
                                self.assertEqual("deny", response["permissionDecision"])
                                self.assertNotIn("could not", response["permissionDecisionReason"])

    def test_completed_snapshot_inspection_is_an_auxiliary_host_refusal(self):
        import command_permissions as cp
        policy = sp.StagePolicy(role="issue_review")
        command = "python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1])))' snapshot.json"
        records = cp.denial_records([{"tool": "Bash", "input": {"command": command},
                                     "layer": "claude_permission"}], policy, "claude", str(ROOT))
        cp.classify_denials(records, {"status": "READY"}, cwd=str(ROOT))
        self.assertEqual(cp.AUXILIARY_DENIAL, records[0]["class"])

    def test_read_documentation_covers_converted_refusals(self):
        page = (ROOT / "docs/host-permissions.md").read_text(encoding="utf-8")
        audit = (ROOT / "docs/capability-parity-audit.md").read_text(encoding="utf-8")
        for command in ("git -C", "python3 -I -c", "printenv GIT_SSH_COMMAND GIT_SSH", "--paginate", "python3 -m unittest"):
            self.assertIn(command, page)
        self.assertIn("#524", audit)

    def test_issue_commands_need_no_consent(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = sp.StagePolicy(role="review", repo="owner/repo", tmpdir=directory,
                                    test_command="python3 -m unittest discover -s tests",
                                    verify_prefixes=("python3 -m unittest",))
            commands = [
                "git diff HEAD..HEAD --stat", "git diff HEAD -- scripts/",
                f"git -C {ROOT} diff HEAD --stat",
                'git grep -n -E "allowedTools|allowed_tools" -- docs/ skills/ scripts/*.py',
                "grep -n allowedTools scripts/*.py",
                f"git -C {ROOT} merge-base HEAD origin/main",
                f"git -C {ROOT} grep -n parity -- docs/",
                "git rev-parse --abbrev-ref --symbolic-full-name @{u}",
                "git show HEAD:AGENTS.md | grep parity",
                r'git show HEAD:AGENTS.md | grep -n "parity\|consent"',
                r'git show HEAD:AGENTS.md | grep -n "\.\|\bparity\b"',
                "gh pr view 7 --json headRefOid --jq '.headRefOid'",
                "gh api repos/owner/repo/issues/524/comments --paginate --jq '.[].body'",
                "printenv GIT_SSH_COMMAND GIT_SSH",
                "python3 -m unittest tests.test_read_parity",
                "python3 -m unittest discover -s tests",
                "python3 -I -c 'import json,sys;d=json.load(open(sys.argv[1]));print(list(d.keys()))' snapshot.json",
            ]
            for role in ("implement", "resolve", "review", "rereview", "issue_review", "verify", "security"):
                from dataclasses import replace
                current = replace(policy, role=role)
                for command in commands:
                    if command.startswith("python3 -m") and role not in sp.WRITING_ROLES | sp.REVIEW_ROLES | {"verify"}:
                        continue
                    with self.subTest(role=role, command=command):
                        decision = sp.check(command, current, cwd=str(ROOT))
                        self.assertTrue(decision.allowed, decision.reason)

    def test_writes_and_credentials_keep_their_guards(self):
        policy = sp.StagePolicy(role="review", repo="owner/repo", tmpdir="/tmp/dispatch")
        for command in (
            "git diff --output=/etc/out", "git -C /tmp diff --output=/etc/out",
            "git show HEAD:AGENTS.md | grep parity > /etc/out",
            "git status; touch /etc/out", "git status $(touch /etc/out)",
            "git -c core.pager=sh status", "git grep -Osh parity",
            "gh api repos/owner/repo/issues/524/comments -X POST",
            "gh api repos/other/repo/issues/524/comments --paginate",
            "python3 -I -c 'open(\"/etc/out\",\"w\").write(\"x\")'",
            "python3 -I -c 'import os;os.system(\"id\")'",
            f"python3 -I -c 'print(open(\"{Path.home()}/.codex/auth.json\").read())'",
            f"cat {Path.home()}/.codex/auth.json | grep token",
        ):
            with self.subTest(command=command):
                self.assertFalse(sp.check(command, policy, cwd=str(ROOT)).allowed)

    def test_claude_patterns_open_selector_inspection_and_temp_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = sp.StagePolicy(role="issue_review", repo="owner/repo", tmpdir=directory)
            patterns = sp.allowed_tool_patterns(policy)
            self.assertIn("Bash(git *)", patterns)
            self.assertIn("Bash(python3 -I -c:*)", patterns)
            self.assertIn("Bash(printenv GIT_SSH_COMMAND:*)", patterns)
            self.assertIn("Bash(gh api repos/owner/repo/issues/*/comments*)", patterns)
            self.assertIn(f"Read({sp._absolute_rule_path(directory)}/**)", patterns)
            self.assertNotIn("Bash(*)", patterns)

    def test_quoted_pipe_arguments_cannot_hide_sed_execution_or_writes(self):
        # Validate the actual CLI hook without running either payload. Quoted
        # and escaped pipes are sed arguments, never a new command's argv.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "policy.json"
            for role in ("implement", "resolve", "review", "rereview", "issue_review",
                         "security", "verify", "bootstrap", "coordinate"):
                policy = sp.StagePolicy(role=role, repo="owner/repo", tmpdir=directory, rtk=True)
                path.write_text(policy.to_json(), encoding="utf-8")
                for wrapper in ("", "rtk ", "rtk proxy "):
                    for pipe in ('"|"', "'|'", r"\|"):
                        for script in ("1e id", "w /etc/parity-out"):
                            command = f'{wrapper}sed -n p AGENTS.md {pipe} cat -e "{script}" | head'
                            with self.subTest(role=role, command=command):
                                decision = sp.check(command, policy, cwd=str(root))
                                self.assertFalse(decision.allowed)
                                self.assertNotEqual("validation_error", decision.rule)
                                result = subprocess.run([sys.executable, sp.__file__, "hook", str(path)],
                                    cwd=root, capture_output=True, text=True, timeout=30,
                                    input=json.dumps({"tool_name": "Bash", "cwd": str(root),
                                                      "tool_input": {"command": command}}))
                                self.assertEqual(0, result.returncode, result.stderr)
                                output = json.loads(result.stdout)["hookSpecificOutput"]
                                self.assertEqual("deny", output["permissionDecision"])
                                reason = output["permissionDecisionReason"]
                                self.assertIn("[layer=toolkit_hook]", reason)
                                self.assertNotIn("could not run", reason)
                                self.assertNotIn("could not be validated", reason)

    def test_brace_expansion_cannot_hide_execution_or_output_options(self):
        # Exercise the real CLI hook, never execute the rejected payloads.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "policy.json"
            commands = (
                "sed -n p {AGENTS.md,-e1eid} | head",
                "sed -n p {AGENTS.md,-e1wparity-out} | head",
                "sed -n p {AGENTS.md,-e1w/etc/parity-out} | head",
                "git log {-1,--output=parity-out} | head",
                "git log {-1,--output=/etc/parity-out} | head",
                "git log -{1..2} | head",
                'sed -n p {AGENTS.md,"-e"1eid} | head',
                "sed -n p {{AGENTS.md,README.md},-e1eid} | head",
            )
            for role in ("implement", "resolve", "review", "rereview", "issue_review",
                         "security", "verify", "bootstrap", "coordinate"):
                policy = sp.StagePolicy(role=role, repo="owner/repo", tmpdir=directory, rtk=True)
                path.write_text(policy.to_json(), encoding="utf-8")
                for wrapper in ("", "rtk ", "rtk proxy "):
                    for command in commands:
                        with self.subTest(role=role, command=wrapper + command):
                            decision = sp.check(wrapper + command, policy, cwd=str(root))
                            self.assertFalse(decision.allowed)
                            self.assertNotEqual("validation_error", decision.rule)
                            result = subprocess.run([sys.executable, sp.__file__, "hook", str(path)],
                                cwd=root, capture_output=True, text=True, timeout=30,
                                input=json.dumps({"tool_name": "Bash", "cwd": str(root),
                                                  "tool_input": {"command": wrapper + command}}))
                            self.assertEqual(0, result.returncode, result.stderr)
                            output = json.loads(result.stdout)["hookSpecificOutput"]
                            self.assertEqual("deny", output["permissionDecision"])
                            self.assertIn("[layer=toolkit_hook]", output["permissionDecisionReason"])
                            self.assertNotIn("could not", output["permissionDecisionReason"])

    def test_literal_braces_and_double_quote_backslashes_remain_readable(self):
        policy = sp.StagePolicy(role="review", rtk=True)
        for wrapper in ("", "rtk ", "rtk proxy "):
            for pattern in (r'"parity\|consent"', r'"\."', r'"\b"', r'"\["',
                            r'"\{a,b\}"', '"{a,b}"', "'{a,b}'", r'\{a,b\}'):
                with self.subTest(wrapper=wrapper, pattern=pattern):
                    decision = sp.check(wrapper + "git show HEAD:AGENTS.md | grep -n " + pattern,
                                        policy, cwd=str(ROOT))
                    self.assertTrue(decision.allowed, decision.reason)

    @unittest.skipIf(os.name == "nt", "POSIX Bash argv comparison")
    def test_double_quote_backslashes_match_bash_argv(self):
        for argument in (r'"parity\|consent"', r'"\."', r'"\b"', r'"\["',
                         r'"\{a,b\}"', r'"a\\b"', r'"a\"b"', r'"a\$b"',
                         r'"a\`b"', '"a\\\nb"', r"'a\|b'", r'a\|b'):
            with self.subTest(argument=argument):
                actual = subprocess.run(["bash", "-c", "printf '%s' " + argument],
                                        capture_output=True, text=True, check=True)
                parsed = sp.parse_command("cat " + argument + " | head", None)
                self.assertEqual(["cat", actual.stdout], parsed.segments[0].words)

    def test_documented_first_barrier_exceptions_reach_hook_denials(self):
        # Only model the two documented prefix/wildcard forms here, not
        # Claude's complete permission grammar or native executor behavior.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "policy.json"
            for role in ("implement", "resolve", "review", "rereview", "issue_review",
                         "security", "verify", "bootstrap", "coordinate"):
                policy = sp.StagePolicy(role=role, repo="owner/repo", tmpdir=directory, rtk=True)
                path.write_text(policy.to_json(), encoding="utf-8")
                patterns = sp.allowed_tool_patterns(policy)
                commands = [
                    ("git *", "git diff --output=/etc/parity-out"),
                    ("python3 -I -c:*", "python3 -I -c 'import os;os.system(\"id\")'"),
                    ("python3 -I -c:*", 'python3 -I -c \'open("/etc/parity-out","w").write("x")\''),
                    # as_posix keeps a Windows home free of `\U` escapes, so the
                    # credential guard, not a SyntaxError, decides this case.
                    ("python3 -I -c:*", "python3 -I -c 'print(open(\""
                     f"{Path.home().as_posix()}/.codex/auth.json\").read())'"),
                ]
                if role not in sp.WRITING_ROLES:
                    commands.append(("git *", "git push origin HEAD"))
                for wrapper in ("", "rtk ", "rtk proxy "):
                    for opening, command in commands:
                        with self.subTest(role=role, command=wrapper + command):
                            pattern = wrapper + opening
                            self.assertIn(f"Bash({pattern})", patterns)
                            self.assertTrue(fnmatch.fnmatchcase(
                                wrapper + command, pattern.replace(":*", " *")))
                            # The broad opening is not an authorization. Invoke
                            # the real CLI hook, without executing the payload.
                            result = subprocess.run([sys.executable, sp.__file__, "hook", str(path)],
                                cwd=root, capture_output=True, text=True, timeout=30,
                                input=json.dumps({"tool_name": "Bash", "cwd": str(root),
                                                  "tool_input": {"command": wrapper + command}}))
                            self.assertEqual(0, result.returncode, result.stderr)
                            output = json.loads(result.stdout)["hookSpecificOutput"]
                            self.assertEqual("deny", output["permissionDecision"])
                            reason = output["permissionDecisionReason"]
                            self.assertIn("[layer=toolkit_hook]", reason)
                            # A crashing or failing validator also denies; this
                            # case must be refused by a policy rule instead.
                            self.assertNotIn("could not run", reason)
                            self.assertNotIn("could not be validated", reason)

    def test_inspection_exceptions_fail_closed_on_validation_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(sp.StagePolicy(role="review", tmpdir=directory).to_json(), encoding="utf-8")
            for command in ("git status", "python3 -I -c 'print(1)'"):
                for target in ("_check_words", "check_bash_tool_input"):
                    with self.subTest(command=command, target=target), \
                            patch.object(sp, target, side_effect=RuntimeError("validation probe")):
                        output = io.StringIO()
                        event = {"tool_name": "Bash", "tool_input": {"command": command}}
                        self.assertEqual(0, sp.hook_main([str(path)], io.StringIO(json.dumps(event)), output))
                        decision = json.loads(output.getvalue())["hookSpecificOutput"]
                        self.assertEqual("deny", decision["permissionDecision"])
                        self.assertIn("RuntimeError", decision["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main()
