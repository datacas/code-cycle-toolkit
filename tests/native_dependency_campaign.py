"""Opt-in actual native campaign; intentionally outside test_*.py discovery.

Run: python3 -m unittest tests.native_dependency_campaign
This requires native sandbox tools and registry access. It never substitutes
the ordinary subprocess fixture when a real sandbox fails.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dependency_setup as ds


class NativeCampaign(unittest.TestCase):
    def campaign(self, executor, lifecycle=False):
        with tempfile.TemporaryDirectory(prefix="native-campaign-") as directory:
            root = Path(directory)
            (root / "apps/web").mkdir(parents=True)
            manifest = {"name": "native-campaign", "version": "1.0.0", "private": True,
                        "workspaces": ["apps/*"], "dependencies": {"is-number": "7.0.0"}}
            escape = root / "escaped-from-build.txt"
            if lifecycle:
                # The project's own postinstall runs only in the contained
                # build: it generates an ignored output and tries to write
                # the checkout directly, which the sandbox must deny.
                manifest["scripts"] = {"postinstall": f"node postinstall.js {json.dumps(str(escape))}"}
                (root / "postinstall.js").write_text(
                    "const fs = require('fs');\n"
                    "fs.mkdirSync('generated', {recursive: true});\n"
                    "fs.writeFileSync('generated/ok.txt', require('is-number')(42) ? 'built' : 'no');\n"
                    "try { fs.writeFileSync(process.argv[2], 'escaped'); } catch (error) {\n"
                    "  console.log('checkout write denied: ' + error.code); }\n")
            (root / "package.json").write_text(json.dumps(manifest))
            (root / "apps/web/package.json").write_text(json.dumps({
                "name": "campaign-web", "version": "1.0.0"}))
            (root / "package-lock.json").write_text(json.dumps({
                "name": "native-campaign", "version": "1.0.0", "lockfileVersion": 3,
                "requires": True, "packages": {
                    "": {"name": "native-campaign", "version": "1.0.0", "workspaces": ["apps/*"],
                         "dependencies": {"is-number": "7.0.0"}},
                    "apps/web": {"name": "campaign-web", "version": "1.0.0"},
                    "node_modules/campaign-web": {"resolved": "apps/web", "link": True},
                    "node_modules/is-number": {"version": "7.0.0", "resolved":
                        "https://registry.npmjs.org/is-number/-/is-number-7.0.0.tgz",
                        "engines": {"node": ">=0.12.0"}},
                }}))
            (root / ".gitignore").write_text("node_modules/\ngenerated/\n")
            def git(*args):
                return subprocess.run(["git", *args], cwd=root, capture_output=True,
                                      text=True, check=True).stdout.strip()
            git("init", "-q")
            git("add", ".")
            git("-c", "user.name=Campaign", "-c", "user.email=campaign@example.invalid",
                "commit", "-qm", "cold Node workspace")
            head = git("rev-parse", "HEAD")
            lock = (root / "package-lock.json").read_bytes()
            # A new cache for every campaign proves cold registry access.
            label = f"native-{executor}" + ("-lifecycle" if lifecycle else "")
            evidence = Path(os.environ.get("TMPDIR", tempfile.gettempdir())) / label
            result = ds.prepare(str(root), executor, executor, evidence_root=evidence)
            print(json.dumps({"executor": executor, "head_sha": head, "preparation": result}))
            for item in result["items"]:
                for attempt in item["attempts"]:
                    for stream in ("stdout", "stderr"):
                        print(f"{executor} {stream}: {Path(attempt[stream]).read_text()}")
            self.assertEqual("prepared", result["status"], json.dumps(result))
            if lifecycle:
                build = result["items"][0]["lifecycle_build"]
                self.assertEqual("built", build["status"], json.dumps(result))
                self.assertEqual(["generated"], build["generated"])
                self.assertEqual("built", (root / "generated/ok.txt").read_text())
                self.assertFalse(escape.exists())
                self.assertEqual("", git("status", "--porcelain"))
            # Run actual dependency lookup inside the same native sandbox.
            settings = root / "verification-settings.json"
            command = ["node", "-e", "if (!require('is-number')(42)) process.exit(1)"]
            argv = ds.sandbox_command(executor, executor, command, writable=[evidence],
                                      protected=[root / "package-lock.json"],
                                      domains=(), settings=settings)
            checked = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=60)
            print(json.dumps({"verification_command": argv, "exit_code": checked.returncode,
                              "stdout": checked.stdout, "stderr": checked.stderr, "head_sha": head}))
            self.assertEqual(0, checked.returncode, checked.stderr)
            self.assertEqual(lock, (root / "package-lock.json").read_bytes())
            self.assertEqual(head, git("rev-parse", "HEAD"))

    def test_claude(self):
        self.campaign("claude")

    def test_codex(self):
        self.campaign("codex")

    def test_claude_lifecycle_build(self):
        self.campaign("claude", lifecycle=True)

    def test_codex_lifecycle_build(self):
        self.campaign("codex", lifecycle=True)
