"""Project decisions across issues, dispatches and the user-facing control CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import executors as ex
import grants
import router
import run_cycle as rc
from test_run_cycle import RunCycleTestCase, NeedsDocker, Talker


class AcrossIssuesTests(RunCycleTestCase):
    def test_two_issues_receive_grants_on_their_first_dispatch(self):
        store = grants.ConsentStore(self.home / "consents.json")
        profiles = router.load_profiles({"code_cycle": {"profiles": {
            "reviewer": {"primary": "claude:anthropic/claude-sonnet-5 high"}}}})
        for issue in ("API-289", "API-290"):
            with self.subTest(issue=issue):
                writer, reviewer = NeedsDocker(), Talker("claude")
                broker = grants.ConsentBroker("owner/api", platform="linux", store=store,
                    answers={"docker_host": "persistent"} if issue == "API-289" else {},
                    prompt=lambda q: self.fail("asked for an existing scope"))
                report = rc.run_cycle("owner/api", issue, router.TaskSignals(), self.store,
                    registry=ex.Registry([writer, reviewer]),
                    availability={"codex": ex.Availability.READY, "claude": ex.Availability.READY},
                    profiles=profiles, issue_review="off", consent=broker)
                self.assertEqual(rc.APPROVED_END, report.status)
                self.assertEqual(1, len(writer.calls))
                self.assertEqual(("docker_host",), writer.calls[0][1]["sandbox_grants"])
                self.assertIn("The user granted", reviewer.dispatched[0])


class PermissionCLITests(unittest.TestCase):
    def test_real_cli_lists_denials_revokes_and_exposes_configuration_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = grants.ConsentStore(root / "consents.json")
            scope = grants.DOCKER_HOST.scope_for("codex", "linux")
            config = root / ".code-cycle.yml"
            config.write_text("code_cycle:\n  repository:\n    selector: owner/api\n"
                              "  docker:\n    host_socket_access: true\n")
            store.save("owner/api", grants.DOCKER_HOST, scope, "deny")
            command = [sys.executable, str(Path(grants.__file__).with_name("consent_control.py")), "list", "--repo", "owner/api",
                       "--executor", "codex", "--platform", "linux", "--config", str(config)]
            env = {**os.environ, "CODE_CYCLE_CONSENT_FILE": str(store.path)}
            listed = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(0, listed.returncode, listed.stderr)
            entries = json.loads(listed.stdout)
            self.assertEqual("DENIED", entries[0]["status"])
            self.assertEqual("project configuration", entries[-1]["source"])
            self.assertEqual("GRANTED", entries[-1]["status"])
            command[2] = "revoke"
            revoked = subprocess.run(command + ["--grant-id", "docker_host"], cwd=root,
                                     env=env, capture_output=True, text=True)
            self.assertEqual(0, revoked.returncode, revoked.stderr)
            self.assertIsNone(store.load("owner/api", grants.DOCKER_HOST, scope))
            self.assertEqual("project configuration", json.loads(revoked.stdout)[0]["source"])
            self.assertEqual(1, len(json.loads(store.path.read_text())["revocations"]))
            # Without the independent configuration grant, revocation is UNKNOWN.
            config.write_text("code_cycle:\n  repository:\n    selector: owner/api\n")
            unknown = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(2, unknown.returncode)  # missing --grant-id is refused
            command[2] = "list"
            unknown = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual("UNKNOWN", json.loads(unknown.stdout)[0]["status"])


if __name__ == "__main__":
    unittest.main()
