"""Credential policy uses metadata and synthetic stores, never host secrets."""
from __future__ import annotations

import builtins
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dependency_setup as ds
import executors as ex
import stage_permissions as sp
import user_choices
from router import Target


def rules(argv):
    table = next(arg.split("=", 1)[1] for arg in argv
                 if re.match(r"permissions\.[a-z_]+\.filesystem=", arg))
    return {json.loads(path): json.loads(access) for path, access in
            re.findall(r'("(?:\\.|[^"\\])*")=("\w+")', table)}


class CredentialReadsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        for name in (*sp.CREDENTIAL_STORES, sp.GH_CREDENTIAL_STORE):
            target = self.home / name[2:]
            target.parent.mkdir(parents=True, exist_ok=True)
            if name in ("~/.ssh", "~/.aws", sp.GH_CREDENTIAL_STORE):
                target.mkdir()
            else:
                target.write_text("fixture")
        self.patch = patch.object(Path, "home", return_value=self.home)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_every_codex_profile_denies_stores_and_preserves_clone_and_scratch(self):
        target = Target("codex", "openai", "fixture", "medium")
        expected = {str(self.home / name[2:]) for name in sp.CREDENTIAL_STORES}
        clone = self.root / "clone"
        clone.mkdir()
        for writes, publishes, registry, docker in itertools.product((False, True), repeat=4):
            with self.subTest(writes=writes, publishes=publishes, registry=registry, docker=docker):
                argv = ex.CodexAdapter().argv(
                    target, "task", writes=writes, publishes=publishes,
                    review_clone=str(clone) if not writes else None,
                    network_domains=("registry.npmjs.org",) if registry else (),
                    sandbox_grants=("docker_host",) if docker else ())
                with ex._native_review_scratch(ex.CodexAdapter(), argv, {},
                                              writes=writes, publishes=publishes,
                                              cwd=str(clone)) as (argv, env):
                    actual = rules(argv)
                    self.assertTrue(all(actual[path] == "none" for path in expected))
                    self.assertEqual("write", actual[env["TMPDIR"]])
                    if not writes:
                        self.assertEqual("write", actual[str(clone)])
                    self.assertEqual(publishes, str(self.home / ".config/gh") not in actual)

    def test_claude_policy_files_apply_denials_for_every_role(self):
        for role in ("implement", "resolve", "review", "rereview", "issue_review", "triage"):
            with self.subTest(role=role):
                adapter = ex.ClaudeAdapter()
                argv = ["claude", "--verbose", "-p", "task"]
                policy = sp.StagePolicy(role=role, credential_publishing=role in
                                        sp.WRITING_ROLES | sp.REVIEW_ROLES)
                with adapter.policy_files(argv, policy, scratch=str(self.root),
                                          cwd=str(self.home)) as result:
                    config = json.loads(result[result.index("--settings") + 1])
                    denied = config["sandbox"]["filesystem"]["denyRead"]
                    self.assertEqual(set(sp.credential_read_denials(
                        publishes=policy.credential_publishing)), set(denied))
                    self.assertTrue(config["sandbox"]["failIfUnavailable"])

    @unittest.skipIf(os.name == "nt", "symlink fixture")
    def test_resolves_symlinks_skips_absent_and_never_reads_contents(self):
        original = self.home / ".aws"
        original.rmdir()
        real = self.root / "real-aws"
        real.mkdir()
        original.symlink_to(real, target_is_directory=True)
        (self.home / ".claude.json").unlink()
        with patch.object(Path, "read_bytes", side_effect=AssertionError("credential read")):
            denied = sp.credential_read_denials()
        self.assertIn(str(real), denied)
        self.assertNotIn(str(original), denied)
        self.assertNotIn(str(self.home / ".claude.json"), denied)

    def test_opt_out_loaded_from_real_config_validated_and_round_tripped(self):
        import run_cycle
        config = self.root / ".code-cycle.yml"
        config.write_text('code_cycle:\n  user_choices:\n    credential_reads: ["~/.ssh"]\n')
        choices = user_choices.load(run_cycle.load_config(config))
        policy = sp.StagePolicy.from_json(sp.StagePolicy(role="implement", **choices).to_json())
        self.assertEqual(("~/.ssh",), policy.credential_reads)
        denied = sp.credential_read_denials(credential_reads=policy.credential_reads)
        self.assertNotIn(str(self.home / ".ssh"), denied)
        self.assertIn(str(self.home / ".aws"), denied)
        self.assertIn('"credential_reads": ["~/.ssh"]', sp.prompt_note(policy))
        config.write_text('code_cycle:\n  user_choices:\n    credential_reads: ["*"]\n')
        with self.assertRaises(run_cycle.CycleDriverError):
            run_cycle.load_config(config)

    def test_linux_preparation_uses_identical_srt_policy_for_both_executors(self):
        settings = self.root / "settings.json"
        with patch.object(ds.sys, "platform", "linux"), patch.object(
                ds.native_prerequisites, "executable", return_value="/tools/srt"):
            configs = []
            for executor in ("codex", "claude"):
                argv = ds.sandbox_command(executor, executor, ["npm", "ci"],
                                          writable=[self.root], protected=[settings],
                                          domains=ds.REGISTRIES, settings=settings)
                self.assertEqual("/tools/srt", argv[0])
                configs.append(json.loads(settings.read_text()))
            self.assertEqual(configs[0], configs[1])
            self.assertEqual(set(sp.credential_read_denials()),
                             set(configs[0]["filesystem"]["denyRead"]))
        with patch.object(ds.sys, "platform", "linux"), patch.object(
                ds.native_prerequisites, "executable", return_value=None):
            with self.assertRaisesRegex(OSError, "installed srt"):
                ds.sandbox_command("codex", "codex", ["npm", "ci"], writable=[],
                                   protected=[], domains=(), settings=settings)

    def test_unvalidated_platform_codex_fallback_has_same_denials(self):
        for platform in ("darwin", "win32"):
            with patch.object(ds.sys, "platform", platform), patch.object(
                    ds.native_prerequisites, "nested_codex", return_value=False):
                argv = ds.sandbox_command("codex", "codex", ["npm", "ci"],
                                          writable=[], protected=[], domains=(),
                                          settings=self.root / "settings.json")
                self.assertEqual({path: "none" for path in sp.credential_read_denials()}, rules(argv))

    def test_confinement_probe_rejects_each_missing_credential_denial(self):
        # Emulate the declared filesystem inside the generated probe. This is a
        # policy/probe regression test, not evidence of real OS confinement.
        for missing in (None, *range(6)):
            def runner(argv, **options):
                table = rules(argv)
                denied = [path for path, access in table.items() if access == "none"]
                if missing is not None:
                    del table[denied[missing]]
                def sandbox_open(path, mode="r", *args, **kwargs):
                    resolved = str(Path(path).resolve())
                    matching = [(root, access) for root, access in table.items()
                                if Path(resolved).is_relative_to(root)]
                    access = max(matching, key=lambda item: len(item[0]))[1] if matching else "read"
                    if access == "none" or ("w" in mode and access != "write"):
                        raise PermissionError("synthetic policy denied")
                    return builtins.open(path, mode, *args, **kwargs)
                try:
                    exec(argv[-1], {"__builtins__": {**vars(builtins), "open": sandbox_open}})
                except SystemExit as error:
                    return subprocess.CompletedProcess(argv, 0 if error.code == 0 else 1,
                                                       "", str(error.code))
                self.fail("probe did not exit")
            ok, detail = ex.CodexAdapter().probe_review_confinement(runner=runner)
            self.assertEqual(missing is None, ok, detail)

    def test_inherited_allow_read_cannot_cancel_stage_denials(self):
        with patch.object(sp, "_read_settings", return_value={
                "sandbox": {"filesystem": {"allowRead": [str(self.home)]}}}):
            self.assertIn("allowRead", sp.sandbox_override())

    def test_opt_out_is_reported_in_dispatch_evidence(self):
        target = Target("codex", "openai", "fixture", "medium")
        with patch.object(ex.native, "observe", return_value={
                "probes": [], "missing": [], "status": "observed"}):
            result = ex.CodexAdapter().dispatch(
                target, "inspect", cwd=str(self.home),
                command_policy=sp.StagePolicy(role="triage", credential_reads=("~/.aws",)),
                runner=lambda argv, **options: subprocess.CompletedProcess(
                    argv, 0, '{"type":"turn.completed"}', ""))
        self.assertEqual(["~/.aws"], result.artifacts["credential_read_policy"]["exceptions"])
        self.assertNotIn(str(self.home / ".aws"),
                         result.artifacts["credential_read_policy"]["denied_paths"])

    @unittest.skipUnless(os.environ.get("CODE_CYCLE_LIVE_CREDENTIAL_PROBE") == "1"
                         and not ds.native_prerequisites.nested_codex(),
                         "host-only E7 probe; nested native sandboxes are not supported")
    def test_live_e7_stage_profiles_on_both_executors(self):
        """Opt-in host boundary check; prints states only, with synthetic stores."""
        import shutil
        target = Target("codex", "openai", "fixture", "medium")
        files = []
        for store in (*sp.CREDENTIAL_STORES, sp.GH_CREDENTIAL_STORE):
            path = self.home / store[2:]
            if path.is_dir():
                path = path / "probe"
                path.write_text("synthetic credential probe")
            files.append((store, path))
        for executor, writes, publishes, registry, docker in itertools.product(
                ("codex", "claude"), (False, True), (False, True), (False, True), (False, True)):
            with self.subTest(executor=executor, writes=writes, publishes=publishes,
                              registry=registry, docker=docker):
                script = "import sys\nfailed=[]\n" + "\n".join(
                    f"try:\n open({str(path)!r}, 'rb').close(); readable=True\n"
                    "except OSError:\n readable=False\n"
                    f"print({store!r}, 'READABLE' if readable else 'denied')\n"
                    f"if readable != {publishes and store == sp.GH_CREDENTIAL_STORE}: failed.append({store!r})"
                    for store, path in files) + "\nsys.exit('; '.join(failed) if failed else 0)"
                domains = ("registry.npmjs.org",) if registry else ()
                if executor == "codex":
                    binary = shutil.which("codex")
                    self.assertIsNotNone(binary, "host E7 requires codex")
                    argv = ex.CodexAdapter().argv(target, "probe", writes=writes,
                        publishes=publishes, network_domains=domains,
                        sandbox_grants=("docker_host",) if docker else (),
                        review_clone=str(self.root) if not writes else None)
                    profile = next(arg.split("=", 1)[1].strip('"') for arg in argv
                                   if arg.startswith("default_permissions="))
                    command = [binary, "sandbox", "--permission-profile", profile]
                    for index, arg in enumerate(argv):
                        if arg == "-c" and argv[index + 1].startswith(("permissions.", "features.")):
                            command.extend((arg, argv[index + 1]))
                    command.extend(("--", sys.executable, "-c", script))
                else:
                    binary = shutil.which("srt")
                    self.assertIsNotNone(binary, "host E7 requires srt")
                    sandbox = sp.review_sandbox_settings((str(self.root),), domains,
                                                        publishes=publishes, docker_host=docker)["sandbox"]
                    settings = self.root / "live-srt.json"
                    settings.write_text(json.dumps({key: sandbox[key] for key in ("filesystem", "network")}))
                    command = [binary, "--settings", str(settings), "--", sys.executable, "-c", script]
                completed = subprocess.run(command, cwd=self.root, capture_output=True,
                                           text=True, timeout=30)
                print(executor, writes, publishes, registry, docker, completed.stdout)
                self.assertEqual(0, completed.returncode, completed.stderr)
