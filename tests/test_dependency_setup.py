"""Preparation never executes project code outside the selected sandbox."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import dependency_setup as ds
import executors as ex
import stage_permissions as sp
import user_choices
from router import Target


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # prepare() canonicalizes the checkout; Windows temp paths may be 8.3 short names.
        self.root = Path(self.temp.name).resolve() / "source"
        self.root.mkdir()
        self.evidence = Path(self.temp.name) / "evidence"
        self.git("init", "-q")
        self.package(self.root)
        self.package(self.root / "apps/web")
        (self.root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        (self.root / ".gitignore").write_text("node_modules/\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture")
        self.calls = []
        self.failures = []

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              text=True, check=True).stdout.strip()

    def package(self, directory, scripts=None):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "package.json").write_text(json.dumps({"name": "fixture",
                                                           "scripts": scripts or {}}))

    def runner(self, argv, **options):
        self.calls.append((argv, options))
        self.assertEqual("codex", argv[0])
        self.assertIn("sandbox", argv)
        command = argv[argv.index("--") + 1:]
        if command[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "9.1.0\n", "")
        if self.failures:
            return subprocess.CompletedProcess(argv, 1, "", self.failures.pop(0))
        for directory in (Path(options["cwd"]), Path(options["cwd"]) / "apps/web"):
            (directory / "node_modules").mkdir(exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, "installed\n", "")

    def filesystem(self, argv):
        return next(arg for arg in argv if arg.startswith(
            "permissions.code_cycle_prepare.filesystem="))

    def writable(self, argv):
        return [Path(json.loads(key)) for key in
                re.findall(r'("(?:[^"\\]|\\.)*")="write"', self.filesystem(argv))]

    def prepare(self):
        return ds.prepare(str(self.root), "codex", "codex", runner=self.runner,
                          evidence_root=self.evidence, sleep=lambda _: None)

    def test_detects_managers_and_workspace_roots(self):
        self.assertEqual("pnpm", ds.projects(self.root)[0][1])
        self.assertEqual(1, len(ds.projects(self.root)))
        self.assertEqual(2, len(ds.manifests(self.root)))
        for lock, manager in ds.LOCKS.items():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.package(root)
                (root / lock).write_text("fixture")
                self.assertEqual(manager, ds.projects(root)[0][1])

    def test_retries_transient_failure_records_head_logs_and_package_outputs(self):
        self.failures = ["ECONNRESET"]
        before = self.git("status", "--porcelain")
        result = self.prepare()
        self.assertEqual("prepared", result["status"])
        item = result["items"][0]
        self.assertEqual(self.git("rev-parse", "HEAD"), item["head_sha"])
        self.assertEqual([0, 1, 0], [a["exit_code"] for a in item["attempts"]])
        self.assertEqual("ECONNRESET", Path(item["attempts"][1]["stderr"]).read_text())
        self.assertEqual(before, self.git("status", "--porcelain"))
        self.assertTrue(Path(result["evidence"]).is_file())
        self.assertTrue((self.root / "apps/web/node_modules").is_dir())
        for argv, options in self.calls:
            self.assertTrue(self.writable(argv))
            self.assertFalse([path for path in self.writable(argv) if path.is_relative_to(self.root)])
            self.assertFalse(Path(options["cwd"]).is_relative_to(self.root))
            self.assertIn("registry.npmjs.org", " ".join(argv))
            self.assertEqual("true", options["env"]["npm_config_ignore_scripts"])
            self.assertNotIn("GITHUB_TOKEN", options["env"])

    def test_failed_install_never_becomes_a_prepared_or_passing_check(self):
        self.failures = ["invalid credentials"]
        result = self.prepare()
        self.assertEqual("incomplete", result["status"])
        self.assertEqual("failed", result["items"][0]["status"])
        self.assertNotIn("tests", result)
        self.assertEqual(2, len(self.calls))

    def test_install_runs_in_a_scratch_copy_and_replaces_checkout_outputs(self):
        # Codex mounts a protected .agents inside every writable root, and
        # npm ci failed with EBUSY emptying such a node_modules. The checkout
        # gets no write grant; finished outputs move in from the scratch copy.
        stale = self.root / "apps/web/node_modules/stale.js"
        stale.parent.mkdir()
        stale.write_text("stale")
        seen = []
        def runner(argv, **options):
            command = argv[argv.index("--") + 1:]
            if command[-1] != "--version":
                cwd = Path(options["cwd"])
                seen.append(sorted(str(path.relative_to(cwd)) for path in cwd.rglob("*")))
                (cwd / "node_modules/.agents").mkdir(parents=True)
                (cwd / "node_modules/is-number").mkdir()
                (cwd / "apps/web/node_modules/local").mkdir(parents=True)
            return subprocess.CompletedProcess(argv, 0, "9.1.0\n", "")
        result = ds.prepare(str(self.root), "codex", "codex", runner=runner,
                            evidence_root=self.evidence, sleep=lambda _: None)
        self.assertEqual("prepared", result["status"], result)
        self.assertEqual([[".gitignore", "apps", "apps/web", "apps/web/package.json",
                           "package.json", "pnpm-lock.yaml"]], seen)
        self.assertTrue((self.root / "node_modules/is-number").is_dir())
        self.assertTrue((self.root / "apps/web/node_modules/local").is_dir())
        self.assertFalse(stale.exists())

    def test_failed_install_leaves_checkout_outputs_untouched(self):
        existing = self.root / "node_modules/kept.js"
        existing.parent.mkdir()
        existing.write_text("kept")
        self.failures = ["invalid credentials", "invalid credentials"]
        self.assertEqual("incomplete", self.prepare()["status"])
        self.assertEqual("kept", existing.read_text())
        self.assertFalse((self.root / "apps/web/node_modules").exists())

    def test_yarn_publishes_only_untracked_generated_outputs(self):
        (self.root / "pnpm-lock.yaml").unlink()
        (self.root / "yarn.lock").write_text("# yarn lockfile v1\n")
        release = self.root / ".yarn/releases/yarn.cjs"
        release.parent.mkdir(parents=True)
        release.write_text("tracked release")
        self.git("add", "-A")
        def runner(argv, **options):
            command = argv[argv.index("--") + 1:]
            if command[-1] != "--version":
                cwd = Path(options["cwd"])
                (cwd / ".yarn/releases/yarn.cjs").write_text("rewritten")
                (cwd / ".yarn/install-state.gz").write_text("state")
                (cwd / ".pnp.cjs").write_text("pnp")
            return subprocess.CompletedProcess(argv, 0, "4.1.0\n", "")
        result = ds.prepare(str(self.root), "codex", "codex", runner=runner,
                            evidence_root=self.evidence, sleep=lambda _: None)
        self.assertEqual("prepared", result["status"], result)
        self.assertEqual("tracked release", release.read_text())
        self.assertEqual("state", (self.root / ".yarn/install-state.gz").read_text())
        self.assertEqual("pnp", (self.root / ".pnp.cjs").read_text())

    def test_placeholder_only_output_is_not_a_successful_install(self):
        (self.root / "package.json").write_text(json.dumps(
            {"name": "fixture", "dependencies": {"is-number": "7.0.0"}}))
        self.git("add", ".")
        def runner(argv, **options):
            version = argv[-1] == "--version"
            if not version:
                (Path(options["cwd"]) / "node_modules/.agents").mkdir(parents=True)
            return subprocess.CompletedProcess(argv, 0, "9.1.0\n" if version else "", "")
        result = ds.prepare(str(self.root), "codex", "codex", runner=runner,
                            evidence_root=self.evidence, sleep=lambda _: None)
        self.assertEqual("incomplete", result["status"])
        self.assertIn("without required dependency outputs", result["items"][0]["error"])
        self.assertFalse((self.root / "node_modules").exists())

    def test_large_tracked_source_tree_does_not_inflate_codex_filesystem_argument(self):
        # This source listing alone exceeded Linux's 128 KiB argv-entry limit.
        source = self.root / "src"
        source.mkdir()
        for index in range(3000):
            (source / f"component-{index:05d}-with-a-descriptive-name.js").touch()
        (self.root / ".yarn").mkdir()
        protected = self.root / ".yarn/tracked-plugin.cjs"
        protected.write_text("tracked plugin")
        self.git("add", ".")
        result = self.prepare()
        self.assertEqual("prepared", result["status"])
        for argv, options in self.calls:
            filesystem = self.filesystem(argv)
            self.assertLess(len(filesystem.encode()), 16 * 1024)
            self.assertNotIn("component-", filesystem)
            self.assertNotIn(str(protected), filesystem)
            self.assertIn(json.dumps(str(Path(options["cwd"]) / "pnpm-lock.yaml")) + '="read"',
                          filesystem)
            self.assertIn('permissions.code_cycle_prepare.extends=":read-only"', argv)
        self.assertEqual("tracked plugin", protected.read_text())

    def test_codex_protects_tracked_ancestors_of_writable_outputs(self):
        ancestor = self.root / ".yarn"
        argv = ds.sandbox_command(
            "codex", "codex", ["npm", "--version"],
            writable=[ancestor / "cache"], protected=[ancestor], domains=(),
            settings=Path(self.temp.name) / "settings.json")
        self.assertIn(json.dumps(str(ancestor)) + '="read"', " ".join(argv))

    def test_rereview_reuses_only_same_head_and_present_package_local_outputs(self):
        self.prepare()
        self.calls.clear()
        self.assertTrue(self.prepare()["items"][0]["reused"])
        (self.root / "apps/web/node_modules").rmdir()
        self.calls.clear()
        self.assertNotIn("reused", self.prepare()["items"][0])
        self.assertEqual(2, len(self.calls))
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-qm", "different head")
        self.assertNotIn("reused", self.prepare()["items"][0])

    def test_cached_install_detects_changed_dependency_contents(self):
        self.prepare()
        (self.root / "node_modules/changed.js").write_text("changed")
        self.assertNotIn("reused", self.prepare()["items"][0])

    def test_nonzero_install_crosses_a_real_process_boundary(self):
        if os.name == "nt":
            self.skipTest("executable fixture uses a Unix shebang")
        binary = Path(self.temp.name) / "failing-sandbox"
        binary.write_text(f"#!{sys.executable}\nimport sys\n"
                          "if sys.argv[-1] == '--version': print('9.1.0')\n"
                          "else:\n print('fixture install failed', file=sys.stderr)\n sys.exit(12)\n")
        binary.chmod(0o700)
        result = ds.prepare(str(self.root), "codex", str(binary), evidence_root=self.evidence)
        self.assertEqual("incomplete", result["status"])
        attempt = result["items"][0]["attempts"][-1]
        self.assertEqual(12, attempt["exit_code"])
        self.assertIn("fixture install failed", Path(attempt["stderr"]).read_text())

    def test_scripts_are_reported_as_incomplete_with_no_host_execution(self):
        self.package(self.root, {"postinstall": "touch /tmp/should-not-run"})
        result = self.prepare()
        self.assertEqual("incomplete", result["status"])
        self.assertEqual("scripts_disabled", result["items"][0]["status"])
        self.assertEqual(["package.json"], result["items"][0]["needs_build"])

    def test_symlink_outputs_never_grant_writes_outside_checkout(self):
        if os.name == "nt":
            self.skipTest("Unix symlink fixture")
        (self.root / "node_modules").symlink_to(self.evidence, target_is_directory=True)
        result = self.prepare()
        self.assertEqual("incomplete", result["status"])
        self.assertFalse(self.calls)

    def test_both_sandbox_wrappers_disable_general_network_and_protect_lock(self):
        with patch.object(ds.shutil, "which", return_value="/usr/bin/srt"):
            for executor in ("claude", "codex"):
                settings = Path(self.temp.name) / "settings.json"
                argv = ds.sandbox_command(executor, executor, ["npm", "ci", "--ignore-scripts"],
                                          writable=[self.root / "node_modules"],
                                          protected=[self.root / "pnpm-lock.yaml"],
                                          domains=ds.REGISTRIES, settings=settings)
                if executor == "claude":
                    config = json.loads(settings.read_text())
                    self.assertEqual(list(ds.REGISTRIES), config["network"]["allowedDomains"])
                    self.assertIn(str(self.root / "pnpm-lock.yaml"), config["filesystem"]["denyWrite"])
                    # srt quotes each positional argument; a joined string
                    # would run as one word, and its own --version would win.
                    self.assertEqual(["/usr/bin/srt", "--settings", str(settings), "--",
                                      "npm", "ci", "--ignore-scripts"], argv)
                else:
                    self.assertIn('permissions.code_cycle_prepare.extends=":read-only"', argv)
                    self.assertNotIn("danger-full-access", argv)

    def test_fresh_review_and_rereview_cross_process_boundary_without_source_dependencies(self):
        if os.name == "nt":
            self.skipTest("executable fixture uses a Unix shebang")
        # A fake sandbox binary exercises argv/process/clone contracts without
        # claiming that this test establishes real OS confinement.
        binary = Path(self.temp.name) / "sandbox-fixture"
        binary.write_text(f"#!{sys.executable}\n" +
                          "import sys, shlex\nfrom pathlib import Path\n"
                          "args = sys.argv[1:]\n"
                          "cmd = args[args.index('--')+1:] if '--' in args else shlex.split(args[-1])\n"
                          "if cmd[-1] == '--version': print('9.1.0')\n"
                          "else:\n"
                          " Path('node_modules').mkdir(exist_ok=True)\n"
                          " Path('apps/web/node_modules').mkdir(exist_ok=True)\n"
                          " print('fixture installed')\n")
        binary.chmod(0o700)
        target = Target("codex", "openai", "test", "medium")
        source_head = self.git("rev-parse", "HEAD")
        for role in ("review", "rereview"):
            for executor in ("codex", "claude"):
                with self.subTest(role=role, executor=executor):
                    adapter = ex.CodexAdapter() if executor == "codex" else ex.ClaudeAdapter()
                    adapter.binary = str(binary)
                    target = Target(executor, "openai" if executor == "codex" else "anthropic",
                                    "test", "medium")
                    real_prepare = ds.prepare
                    def review(prompt, clone):
                        self.assertFalse((Path(clone) / "apps/web/node_modules").exists())
                        def prepare(*args, **kwargs):
                            self.assertEqual(clone, args[0])
                            return real_prepare(*args, **kwargs, evidence_root=self.evidence)
                        def runner(argv, **options):
                            self.assertEqual(clone, options["cwd"])
                            self.assertTrue((Path(clone) / "apps/web/node_modules").is_dir())
                            notice = argv[-1] if executor == "codex" else argv[argv.index("-p") + 1]
                            self.assertIn("Runtime dependency preparation evidence", notice)
                            self.assertIn(source_head, notice)
                            return subprocess.CompletedProcess(argv, 0, '{"type":"turn.completed"}', "")
                        with patch.object(ds, "prepare", side_effect=prepare) as called:
                            result = adapter.dispatch(
                                target, prompt, cwd=clone,
                                command_policy=sp.StagePolicy(role=role), runner=runner)
                        called.assert_called_once()
                        report = result.artifacts["dependency_preparation"]
                        self.assertEqual("prepared", report["status"])
                        self.assertEqual(source_head, report["items"][0]["head_sha"])
                        return result
                    with patch.object(ds.shutil, "which", return_value=str(binary)):
                        result = ex._isolated_review_dispatch(
                            adapter, target, role, cwd=str(self.root), dispatch_call=review,
                            command_policy=True)
                    self.assertEqual(ex.DispatchOutcome.SUCCEEDED, result.outcome, result.detail)
                    self.assertFalse((self.root / "node_modules").exists())
                    self.assertEqual(source_head, self.git("rev-parse", "HEAD"))


class ChoiceTests(unittest.TestCase):
    def test_selected_arbitrary_and_previously_denied_tools_survive_serialization(self):
        choices = user_choices.load({"code_cycle": {"user_choices": {
            "tools": ["bespoke-search", "rg"], "commands": ["sha256sum"],
            "integrations": ["mcp__custom__*"], "registry_domains": ["packages.example.org"]}}})
        for role in ("implement", "resolve", "review", "rereview", "issue_review"):
            policy = sp.StagePolicy.from_json(sp.StagePolicy(role=role, **choices).to_json())
            for command in ("bespoke-search hello", "rg -n hello .", "sha256sum file"):
                self.assertTrue(sp.check(command, policy).allowed)
            self.assertIn("Bash(bespoke-search:*)", sp.allowed_tool_patterns(policy))
            self.assertIn("mcp__custom__*", sp.allowed_tool_patterns(policy))
            self.assertFalse(sp.check("git push --force origin main", policy).allowed)
            self.assertFalse(sp.check("gh pr merge 7", policy).allowed)
            self.assertIn("bespoke-search", sp.prompt_note(policy))

    def test_invalid_choices_fail_closed_without_a_tool_name_allowlist(self):
        for choices in ({"tools": ["tool; sh"]}, {"commands": ["foo && bar"]},
                        {"registry_domains": ["*"]}, {"integrations": ["Bash(*)"]}):
            with self.assertRaises(sp.PolicyError):
                user_choices.load({"code_cycle": {"user_choices": choices}})

    def test_nonpublishing_codex_registry_choice_does_not_grant_code_host_route(self):
        target = Target("codex", "openai", "test", "medium")
        argv = ex.CodexAdapter().argv(
            target, "read supplied issue context", network_domains=("packages.example.org",))
        for host in sp.REVIEW_SANDBOX_DOMAINS:
            self.assertNotIn(host, " ".join(argv))
        self.assertIn("packages.example.org", " ".join(argv))
        self.assertIn('permissions.code_cycle_registry_read.extends=":read-only"', argv)
