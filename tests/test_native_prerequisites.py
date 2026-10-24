"""Prerequisite recovery uses official artifacts, never unconfined project code."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
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
