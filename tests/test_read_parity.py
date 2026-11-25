"""Regression coverage for the ordinary reads in #524."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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

    def test_completed_snapshot_inspection_is_an_auxiliary_host_refusal(self):
        import command_permissions as cp
        policy = sp.StagePolicy(role="issue_review")
        command = "python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1])))' snapshot.json"
        records = cp.denial_records([{"tool": "Bash", "input": {"command": command},
                                     "layer": "claude_permission"}], policy, "claude", str(ROOT))
        cp.classify_denials(records, {"status": "READY"}, cwd=str(ROOT))
        self.assertEqual(cp.AUXILIARY_DENIAL, records[0]["class"])

    def test_read_documentation_covers_converted_refusals(self):
        page = (ROOT / "docs/host-permissions.md").read_text()
        audit = (ROOT / "docs/capability-parity-audit.md").read_text()
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


if __name__ == "__main__":
    unittest.main()
