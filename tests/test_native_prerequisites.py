"""Prerequisite recovery uses official artifacts, never unconfined project code."""
from __future__ import annotations

import base64
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import native_prerequisites as native
import stage_permissions as sp
import dependency_setup


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        linux = patch.object(native.sys, "platform", "linux")
        linux.start()
        self.addCleanup(linux.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "tools"
        self.run = Mock(return_value="1.0.0")
        self.which = lambda name: f"/tools/{name}" if name in {"node", "npm"} else None

    def srt_source(self, *, corrupt=False, versions=None):
        artifact = b"verified npm artifact"
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(artifact).digest()).decode()
        metadata = {"versions": versions or {
            "1.0.0": {"version": "1.0.0", "engines": {"node": ">=18.0.0"},
                      "dist": {"tarball": native.REGISTRY.replace("%2f", "/") + "/-/sandbox-runtime-1.0.0.tgz",
                               "integrity": integrity}}}}
        def download(url):
            if url == native.REGISTRY:
                return json.dumps(metadata).encode()
            return b"corrupt" if corrupt else artifact
        return download, metadata

    def npm_runner(self, argv, **kw):
        if argv[0] == "/tools/node":
            return "v20.0.0"
        if "install" in argv:
            self.assertIn("--ignore-scripts", argv)
            self.assertIn("--engine-strict", argv)
            self.assertFalse((self.root / "srt").exists())
            bundle = Path(argv[argv.index("--prefix") + 1])
            executable = bundle / "node_modules/.bin/srt"
            executable.parent.mkdir(parents=True)
            executable.write_text("fake")
        return "1.0.0"

    def test_srt_integrity_atomic_install_reuse_and_no_update(self):
        download, _ = self.srt_source()
        with patch.object(native.sys, "platform", "linux"):
            first = native.ensure("srt", root=self.root, which=self.which,
                                  download=download, run=self.npm_runner)
            self.assertEqual("installed", first["outcome"], first)
            self.assertTrue(Path(first["path"]).is_file())
            never_download = Mock(side_effect=AssertionError("auto-update"))
            second = native.ensure("srt", root=self.root, which=self.which,
                                   download=never_download, run=self.npm_runner)
        self.assertEqual("reused", second["outcome"])
        self.assertEqual(first["integrity"], second["integrity"])
        never_download.assert_not_called()

    def test_latest_compatible_node_version_is_selected(self):
        download, metadata = self.srt_source()
        versions = metadata["versions"]
        versions["2.0.0"] = {**versions["1.0.0"], "version": "2.0.0",
                             "engines": {"node": ">=22.12.0"}}
        download, _ = self.srt_source(versions=versions)
        result = native.ensure("srt", root=self.root, which=self.which,
                               download=download, run=self.npm_runner)
        self.assertEqual("1.0.0", result["version"])

    def test_missing_node_or_npm_does_not_download_or_run(self):
        for missing in ("node", "npm"):
            with self.subTest(missing=missing):
                download = Mock(side_effect=AssertionError("downloaded"))
                result = native.ensure("srt", root=self.root, run=self.run, download=download,
                                       which=lambda name: None if name in {missing, "srt"} else "/tools/" + name)
                self.assertEqual("not_verified", result["outcome"])
                self.assertIn("Node/npm", result["error"])
                self.run.assert_not_called()
                download.assert_not_called()

    def test_npm_integrity_failure_never_runs_install_or_publishes(self):
        download, _ = self.srt_source(corrupt=True)
        run = Mock(return_value="v20.0.0")
        result = native.ensure("srt", root=self.root, which=self.which, download=download, run=run)
        self.assertEqual("not_verified", result["outcome"])
        self.assertIn("integrity mismatch", result["error"])
        self.assertEqual(1, run.call_count)
        self.assertFalse((self.root / "srt").exists())

    def test_existing_compatible_working_tool_skips_network(self):
        for tool in ("rg", "srt"):
            download = Mock(side_effect=AssertionError("network on reuse"))
            result = native.ensure(tool, root=self.root, which=lambda name: "/tools/" + name,
                                   download=download, run=self.run)
            self.assertEqual("reused", result["outcome"])
            download.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_concurrent_publication_reuses_verified_winner(self):
        for code in (errno.EEXIST, errno.ENOTEMPTY):
            with self.subTest(errno=code), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "tools"
                def lost_publication(directory, target):
                    shutil.copytree(directory, target)
                    raise OSError(code, "concurrent publication")
                with patch.object(Path, "rename", lost_publication), patch.object(
                        native.platform, "machine", return_value="x86_64"):
                    result = native.ensure("rg", root=root, which=lambda _: None,
                                           download=self.rg_source(), run=self.run)
                self.assertEqual("reused", result["outcome"], result)
                self.assertTrue(Path(result["path"]).is_file())
                self.assertEqual([result["path"], "--version"], self.run.call_args.args[0])

    def test_publication_permission_error_does_not_become_reuse(self):
        with patch.object(Path, "rename", side_effect=PermissionError(errno.EACCES, "denied")), patch.object(
                native.platform, "machine", return_value="x86_64"):
            result = native.ensure("rg", root=self.root, which=lambda _: None,
                                   download=self.rg_source(), run=self.run)
        self.assertEqual("not_verified", result["outcome"])
        self.assertFalse((self.root / "rg").exists())

    def rg_source(self, *, corrupt=False, link=False):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            entry = tarfile.TarInfo("ripgrep-1.0.0-x86_64-unknown-linux-musl/rg")
            if link:
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/etc/passwd"
                archive.addfile(entry)
            else:
                data = b"#!/bin/sh\necho ripgrep 1.0.0\n"
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
        artifact = stream.getvalue()
        def download(url):
            if url == native.RELEASES:
                return b'{"tag_name":"1.0.0"}'
            if url.endswith(".sha256"):
                return (hashlib.sha256(artifact).hexdigest() + "  rg.tar.gz").encode()
            return b"corrupt" if corrupt else artifact
        return download

    @unittest.skipIf(os.name == "nt", "the fake release executable is POSIX")
    def test_ripgrep_real_process_install_and_reuse(self):
        # Boundary: the verified fake official artifact is launched as a real
        # process both before atomic publication and on subsequent reuse.
        with patch.object(native.platform, "machine", return_value="x86_64"):
            first = native.ensure("rg", root=self.root, which=lambda _: None,
                                  download=self.rg_source())
            self.assertEqual("installed", first["outcome"], first)
            second = native.ensure("rg", root=self.root, which=lambda _: None,
                                   download=Mock(side_effect=AssertionError("auto-update")))
        self.assertEqual("reused", second["outcome"])
        self.assertIn("ripgrep 1.0.0", native._run([second["path"], "--version"]))

    def test_ripgrep_checksum_and_archive_link_fail_closed(self):
        for corrupt, link in ((True, False), (False, True)):
            with self.subTest(corrupt=corrupt, link=link), patch.object(
                    native.platform, "machine", return_value="x86_64"):
                result = native.ensure("rg", root=self.root, which=lambda _: None,
                                       download=self.rg_source(corrupt=corrupt, link=link), run=self.run)
                self.assertEqual("not_verified", result["outcome"])
                self.assertFalse((self.root / "rg").exists())
                self.run.assert_not_called()

    def test_failed_tool_execution_is_not_published(self):
        with patch.object(native.platform, "machine", return_value="x86_64"):
            result = native.ensure("rg", root=self.root, which=lambda _: None,
                                   download=self.rg_source(), run=Mock(side_effect=OSError("cannot execute")))
        self.assertEqual("not_verified", result["outcome"])
        self.assertFalse((self.root / "rg").exists())

    def test_unknown_engine_syntax_never_selects_a_guess(self):
        self.assertTrue(native.compatible("v22.12.0", ">=22.12.0"))
        self.assertFalse(native.compatible("v20.0.0", ">=22.12.0"))
        with self.assertRaises(ValueError):
            native.compatible("v20.0.0", "banana")
        for node, engine, expected in (("v18.5.0", "18", True), ("v19.0.0", "<=18", False),
                                       ("v0.5.0", "^0", True), ("v0.2.0", "^0.1", False)):
            self.assertEqual(expected, native.compatible(node, engine))

    def test_incomplete_artifact_metadata_is_unverified(self):
        for data in (b"[]", b'{"versions":null}'):
            result = native.ensure("srt", root=self.root, which=self.which,
                                   download=Mock(return_value=data), run=Mock(return_value="v20.0.0"))
            self.assertEqual("not_verified", result["outcome"])
            self.assertFalse((self.root / "srt").exists())

    def test_nonlinux_does_not_install_or_infer_failure_from_platform(self):
        for system in ("darwin", "win32"):
            with patch.object(native.sys, "platform", system):
                result = native.ensure("rg", root=self.root, which=lambda _: None,
                                       download=Mock(side_effect=AssertionError("download")))
            self.assertEqual("not_validated", result["outcome"])


class PreflightTests(unittest.TestCase):
    def test_host_tools_preserve_path_order(self):
        original = os.pathsep.join(("/venv/bin", "/local/bin", "/system/bin"))
        for outcome in ("reused", "available"):
            with self.subTest(outcome=outcome):
                self.assertEqual(original, native.tool_path(original, probes=[
                    {"name": tool, "path": f"/system/bin/{tool}", "outcome": outcome}
                    for tool in ("srt", "rg")]))
        with patch.object(native.shutil, "which", side_effect=lambda tool, **kw: f"/system/bin/{tool}"):
            self.assertEqual(original, native.tool_path(original))

    @unittest.skipIf(os.name == "nt", "the fake executables are POSIX")
    def test_host_rg_does_not_switch_verification_python_at_process_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv, system = root / "venv/bin", root / "system/bin"
            for directory, marker in ((venv, "venv"), (system, "system")):
                directory.mkdir(parents=True)
                binary = directory / "python3"
                binary.write_text(f"#!{sys.executable}\nprint({marker!r})\n")
                binary.chmod(0o755)
            original = os.pathsep.join((str(venv), str(system)))
            selected = native.tool_path(original, probes=[
                {"name": "rg", "path": str(system / "rg"), "outcome": "reused"}])
            result = subprocess.run(["python3"], env={**os.environ, "PATH": selected},
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("venv", result.stdout.strip())

    def test_codex_does_not_attempt_srt_recovery(self):
        for executor in ("codex", "claude"):
            with self.subTest(executor=executor), patch.object(native.sys, "platform", "linux"), patch.object(
                    native, "ensure", return_value={"path": None, "outcome": "not_verified"}) as ensure:
                native.observe(executor)
                self.assertEqual(["rg"] if executor == "codex" else ["srt", "rg"],
                                 [call.args[0] for call in ensure.call_args_list])

    def test_known_failure_rejects_unrelated_restrictions(self):
        for command, error in (
            ("cargo test", "open /work/target/debug/.cargo-lock: Operation not permitted"),
            ("gradle test", "/cache/.gradle/caches/org.gradle: No such file or directory"),
            ("rg needle /private", "rg: /private: Operation not permitted"),
            ("srt -- npm ci", "srt: project file /work/package.json: No such file or directory"),
            ("python3 check.py", "large argv merge: operation not permitted"),
        ):
            with self.subTest(error=error):
                self.assertFalse(native.known_failure(command, error))

    def test_known_failure_accepts_only_named_prerequisite_diagnostics(self):
        for command, error in (
            ("npm ci", "Sandbox dependencies not available: ripgrep (rg) not found"),
            ("srt -- true", "[Errno 2] No such file or directory: '/tools/srt'"),
            ("npm ci", "spawn rg ENOENT"),
            ("npm ci", "Claude dependency preparation needs the installed srt sandbox runtime"),
            ("claude", "Sandbox required but unavailable"),
            ("bwrap -- true", "bwrap: Creating new namespace failed: Operation not permitted"),
        ):
            with self.subTest(error=error):
                self.assertTrue(native.known_failure(command, error))

    def test_recovered_rg_precedes_system_srt_directory_with_broken_rg(self):
        for outcome in ("installed", "reused"):
            with self.subTest(outcome=outcome):
                path = native.tool_path("/system/bin", probes=[
                    {"name": "srt", "path": "/system/bin/srt", "outcome": "reused"},
                    {"name": "rg", "path": "/managed/rg/rg", "outcome": outcome, "executable": "rg"}])
                self.assertEqual(str(Path("/managed/rg/rg").parent), path.split(os.pathsep)[0])

    @unittest.skipIf(os.name == "nt", "the fake sandbox executable is POSIX")
    def test_srt_probe_gets_managed_rg_at_the_process_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            tools = home / ".local/lib/code-cycle-tools"
            for tool in ("rg", "srt"):
                bundle = tools / tool
                bundle.mkdir(parents=True)
                (bundle / "receipt.json").write_text(json.dumps({"executable": tool}))
            rg = tools / "rg/rg"
            rg.write_text("#!/bin/sh\necho ripgrep\n")
            rg.chmod(0o755)
            srt = tools / "srt/srt"
            # This fixture checks PATH and process wiring; it is not an OS
            # confinement implementation and must not be counted as one.
            srt.write_text(f"#!{sys.executable}\nimport pathlib,shutil,sys\n"
                           "if not shutil.which('rg'): sys.exit('rg: not found')\n"
                           "pathlib.Path('ok').write_text('fixture')\n")
            srt.chmod(0o755)
            with patch.object(Path, "home", return_value=home), patch.dict(os.environ, {"PATH": ""}):
                ok, evidence = native.isolation_probe("srt", runner=subprocess.run)
                self.assertTrue(ok, evidence)
                self.assertEqual("", os.environ["PATH"])

    @unittest.skipIf(os.name == "nt", "the fake executables are POSIX")
    def test_selected_system_rg_is_not_shadowed_by_stale_managed_rg(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            tools = home / ".local/lib/code-cycle-tools"
            system = home / "bin"
            system.mkdir()
            for tool in ("srt", "rg"):
                bundle = tools / tool
                bundle.mkdir(parents=True)
                (bundle / "receipt.json").write_text(json.dumps({"executable": tool}))
            system_rg = system / "rg"
            system_rg.write_text(f"#!{sys.executable}\nprint('ripgrep 1.0.0')\n")
            system_rg.chmod(0o755)
            stale = tools / "rg/rg"
            stale.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n")
            stale.chmod(0o755)
            srt = tools / "srt/srt"
            srt.write_text(f"#!{sys.executable}\nimport pathlib,shutil,sys\n"
                           "if '--version' in sys.argv: print('1.0.0'); sys.exit(0)\n"
                           f"if shutil.which('rg') != {str(system_rg)!r}: sys.exit('stale rg selected')\n"
                           "pathlib.Path('ok').write_text('fixture')\n")
            srt.chmod(0o755)
            with patch.object(Path, "home", return_value=home), patch.dict(os.environ, {"PATH": str(system)}):
                probes = [{"name": tool, **native.ensure(tool, root=tools,
                           which=lambda name: str(system_rg) if name == "rg" else None)}
                          for tool in ("srt", "rg")]
                path = native.tool_path(str(system), probes=probes)
                self.assertEqual(str(system_rg), shutil.which("rg", path=path))
                self.assertTrue(native.isolation_probe("srt", runner=subprocess.run, path=path)[0])

    def test_nested_preparation_is_classified_without_a_socket_or_process_probe(self):
        with patch.dict(os.environ, {"CODE_CYCLE_STAGE_EXECUTOR": "codex"}), patch.object(
                native.subprocess, "run", side_effect=AssertionError("launched nested process")):
            with self.assertRaisesRegex(OSError, "not_verified without probing"):
                dependency_setup.sandbox_command(
                    "codex", "codex", ["npm", "ci"], writable=[], protected=[],
                    domains=(), settings=Path("unused"))

    def test_missing_matrix_and_no_host_socket_probe(self):
        for absent in ({"srt", "rg"}, {"srt"}, {"rg"}, set()):
            with self.subTest(absent=absent), patch.object(native.shutil, "which", side_effect=(
                    lambda tool: None if tool in absent else "/tools/" + tool)), patch.dict(
                        os.environ, {"CODE_CYCLE_STAGE_EXECUTOR": "codex"}):
                evidence = native.observe("codex", recover=False)
                self.assertIn("codex_socket", evidence)
                self.assertEqual("known_limited", evidence["codex_socket"]["outcome"])
                self.assertEqual("unverified", evidence["status"])
                self.assertEqual("not_run", evidence["functional_check"])
        with patch.dict(os.environ, {"CODE_CYCLE_STAGE_EXECUTOR": ""}):
            self.assertNotIn("codex_socket", native.observe("codex", recover=False))

    def test_static_boundary_drops_code_execution_even_explicit_tools(self):
        policy = sp.StagePolicy(role="review", static_only=True, test_command="pytest",
                                tools=("python3",), commands=("npm run",), docker=True)
        for command in ("pytest", "python3 evil.py", "npm run test", "docker compose build"):
            self.assertFalse(sp.check(command, policy).allowed, command)
        self.assertTrue(sp.check("git diff", policy).allowed)
        self.assertTrue(sp.check("grep -n bug source.py", policy).allowed)

    def test_probe_failure_and_timeout_are_not_verified(self):
        with patch.object(native.shutil, "which", return_value="/tools/bwrap"):
            for runner in (Mock(return_value=subprocess.CompletedProcess([], 1, "", "namespace denied")),
                           Mock(side_effect=subprocess.TimeoutExpired("probe", 30))):
                ok, evidence = native.isolation_probe("bwrap", runner=runner)
                self.assertFalse(ok)
                self.assertIn("probe", evidence)
                argv = runner.call_args.args[0]
                self.assertIn("--unshare-net", argv)

    def test_lying_probe_success_is_not_verified(self):
        with patch.object(native.shutil, "which", return_value="/tools/bwrap"):
            ok, _ = native.isolation_probe("bwrap", runner=Mock(
                return_value=subprocess.CompletedProcess([], 0, "", "")))
        self.assertFalse(ok)

    @unittest.skipIf(os.name == "nt", "the fake sandbox executable is POSIX")
    def test_nonzero_real_process_boundary(self):
        with tempfile.TemporaryDirectory() as scratch:
            binary = Path(scratch) / "bwrap"
            binary.write_text("#!/bin/sh\necho 'bwrap: Creating new namespace failed' >&2\nexit 1\n")
            binary.chmod(0o755)
            with patch.object(native.shutil, "which", return_value=str(binary)):
                ok, evidence = native.isolation_probe("bwrap", runner=subprocess.run)
        self.assertFalse(ok)
        self.assertIn("Creating new namespace failed", evidence)


if __name__ == "__main__":
    unittest.main()
