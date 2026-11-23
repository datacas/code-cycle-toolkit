"""#455 PA-19: direct Bash baselines and shared-hook consent per executor.

Executor names describe grant metadata, not a native CLI dispatch. Manual
native comparisons remain separate obligations in docs/native-parity-runs.md.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import command_permissions as cp
import grants
import stage_permissions as sp

COMMANDS = runpy.run_path(str(ROOT / "docs/native-parity/run.py"))["PA19_COMMANDS"]


@unittest.skipIf(os.name == "nt", "POSIX Bash baselines; Linux CI runs both executor cases")
class CompoundParityTests(unittest.TestCase):
    def parity(self, executor):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, "TMPDIR": directory}
            for argv in (["git", "init", "-q"], ["git", "-c", "user.name=fixture",
                         "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "fixture"]):
                subprocess.run(argv, cwd=root, env=env, check=True, capture_output=True, timeout=30)
            policy = sp.StagePolicy("implement", tmpdir=directory)
            store = grants.ConsentStore(root / "consents.json")
            broker = grants.ConsentBroker("owner/repo", store=store, platform="linux",
                                          cycle_id="cycle", task_id="455")
            for command in COMMANDS:
                with self.subTest(executor=executor, command=command):
                    def run():
                        return subprocess.run(["bash", "-c", command], cwd=root, env=env,
                                              capture_output=True, timeout=30)
                    direct = run()
                    self.assertEqual(0, direct.returncode, direct.stderr)
                    target = root / "pa19.txt"
                    expected_file = target.read_bytes() if target.exists() else None
                    if target.exists():
                        target.unlink()
                    self.assertFalse(sp.check(command, policy, cwd=directory).allowed)
                    request = cp.refused_operation([command], policy, broker, executor, directory)
                    self.assertIsNotNone(request)
                    grant = broker.operations[request["resource"]]
                    asked = grants.question(grant, grant.scopes[0], role="implement", blocker=request)
                    self.assertIn("interpreter", asked["prompt"].lower())
                    broker.apply_answer(grant, grant.scopes[0], "persistent")
                    current = replace(policy, command_permissions=broker.command_entries(executor, "implement"))
                    path = root / "policy.json"
                    path.write_text(current.to_json())
                    hook = subprocess.run([sys.executable, sp.__file__, "hook", str(path)],
                        cwd=root, env=env, capture_output=True, text=True, timeout=30,
                        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": directory}))
                    self.assertEqual((0, ""), (hook.returncode, hook.stdout), hook.stderr)
                    actual = run()
                    self.assertEqual((direct.returncode, direct.stdout, direct.stderr),
                                     (actual.returncode, actual.stdout, actual.stderr))
                    self.assertEqual(expected_file, target.read_bytes() if target.exists() else None)
                    broker.record_executions([command], executor, current, directory)
                    saved = next(item for item in store.list("owner/repo") if item["operation"] == grant.operation)
                    self.assertEqual("allow", saved["decision"])
                    self.assertEqual(command, saved["executed_commands"][0]["command"])
                    self.assertFalse(sp.check(command + "\nprintf extra", current, cwd=directory).allowed)
                    broker.record_executions([command + "\nprintf extra"], executor, current, directory)
                    saved = next(item for item in store.list("owner/repo") if item["operation"] == grant.operation)
                    self.assertEqual(1, len(saved["executed_commands"]))

    def test_claude_pa19_direct_baseline(self):
        self.parity("claude")

    def test_codex_pa19_direct_baseline(self):
        self.parity("codex")
