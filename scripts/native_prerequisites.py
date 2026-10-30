"""Host-owned native sandbox prerequisites, never a project-code installer.

Only Linux/WSL2 has automatic recovery. Existing working tools are preferred;
managed installs are immutable, integrity checked, and never auto-updated.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

PACKAGE = "@anthropic-ai/sandbox-runtime"
REGISTRY = "https://registry.npmjs.org/@anthropic-ai%2fsandbox-runtime"
RELEASES = "https://api.github.com/repos/BurntSushi/ripgrep/releases/latest"
TOOLS = ("srt", "rg", "bwrap", "socat")


def _download(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("prerequisite source must use HTTPS")
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read(64 * 1024 * 1024 + 1)


def _run(argv, *, cwd=None):
    # Do not inherit project npm config or credentials into tool installation.
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "HOME", "TMPDIR", "SYSTEMROOT", "HTTPS_PROXY", "HTTP_PROXY",
                      "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"}}
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                            text=True, timeout=120)
    if result.returncode:
        raise OSError(f"{Path(argv[0]).name} exited {result.returncode}: {result.stderr.strip()[:600]}")
    return result.stdout.strip()


def _version(text):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        raise ValueError(f"unsupported version: {text}")
    return tuple(map(int, match.groups()))


def compatible(node: str, engine: str) -> bool:
    """Evaluate published comparator ranges; fail closed on unknown syntax.

    srt currently declares a >=major.minor.patch range. Unknown future range
    syntax stops recovery rather than silently selecting an older release.
    """
    current = _version(node)
    outcomes = []
    for branch in engine.split("||"):
        comparisons = re.findall(r"(>=|<=|>|<|=|\^|~)?\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", branch)
        residue = re.sub(r"(>=|<=|>|<|=|\^|~)?\s*\d+(?:\.\d+)?(?:\.\d+)?", "", branch)
        if residue.strip() or not comparisons:
            raise ValueError(f"unsupported Node engine range: {engine}")
        accepted = True
        for op, major, minor, patch in comparisons:
            target = (int(major), int(minor or 0), int(patch or 0))
            precision = 3 if patch else 2 if minor else 1
            if op == "^":
                index = next((i for i, value in enumerate(target[:precision]) if value), precision - 1)
                upper = tuple(value + 1 if i == index else value if i < index else 0
                              for i, value in enumerate(target))
                ok = target <= current < upper
            elif op == "~":
                upper = (target[0], target[1] + 1, 0) if minor else (target[0] + 1, 0, 0)
                ok = target <= current < upper
            else:
                upper_partial = (target[0] + 1, 0, 0) if precision == 1 else (target[0], target[1] + 1, 0)
                ok = {">=": current >= target, ">": current > target,
                      "<=": current <= target, "<": current < target,
                      "=": current[:precision] == target[:precision],
                      "": current[:precision] == target[:precision]}[op]
                if precision < 3 and op in {">", "<="}:
                    ok = current >= upper_partial if op == ">" else current < upper_partial
            accepted &= ok
        outcomes.append(accepted)
    return any(outcomes)


def _srt(directory: Path, *, run, download, which) -> dict[str, Any]:
    node, npm = which("node"), which("npm")
    if not node or not npm:
        raise OSError("srt recovery skipped: Node/npm unavailable")
    node_version = run([node, "--version"])
    metadata = json.loads(download(REGISTRY))
    candidates = sorted((version for version in metadata["versions"]
                         if re.fullmatch(r"\d+\.\d+\.\d+", version)), key=_version, reverse=True)
    chosen = next((metadata["versions"][version] for version in candidates
                   if compatible(node_version, metadata["versions"][version].get(
                       "engines", {}).get("node", ">=0.0.0"))), None)
    if chosen is None:
        raise OSError("no published srt version supports this Node version")
    source = chosen["dist"]["tarball"]
    if not source.startswith("https://registry.npmjs.org/@anthropic-ai/sandbox-runtime/-/"):
        raise ValueError("unexpected srt artifact source")
    integrity = chosen["dist"]["integrity"]
    algorithm, encoded = integrity.split("-", 1)
    if algorithm != "sha512":
        raise ValueError("srt requires npm SHA-512 integrity")
    artifact = download(source)
    if hashlib.sha512(artifact).digest() != base64.b64decode(encoded, validate=True):
        raise ValueError("srt integrity mismatch")
    archive = directory / "srt.tgz"
    archive.write_bytes(artifact)
    user_config, global_config = directory / "user.npmrc", directory / "global.npmrc"
    user_config.write_text("")
    global_config.write_text("")
    run([npm, "install", "--prefix", str(directory), "--ignore-scripts", "--engine-strict",
         "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org",
         f"--userconfig={user_config}", f"--globalconfig={global_config}", str(archive)], cwd=directory)
    archive.unlink()
    return {"version": chosen["version"], "source": source, "integrity": integrity,
            "executable": "node_modules/.bin/srt", "node": node_version}


def _rg(directory: Path, *, download) -> dict[str, Any]:
    architecture = {"x86_64": "x86_64-unknown-linux-musl",
                    "aarch64": "aarch64-unknown-linux-gnu"}.get(platform.machine())
    if architecture is None:
        raise OSError("no validated ripgrep artifact for this architecture")
    release = json.loads(download(RELEASES))
    version = release["tag_name"]
    _version(version)
    name = f"ripgrep-{version}-{architecture}.tar.gz"
    source = f"https://github.com/BurntSushi/ripgrep/releases/download/{version}/{name}"
    checksum_text = download(source + ".sha256").decode().split()
    if not checksum_text:
        raise ValueError("missing official ripgrep checksum")
    checksum = checksum_text[0]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
        raise ValueError("invalid official ripgrep checksum")
    artifact = download(source)
    if hashlib.sha256(artifact).hexdigest() != checksum.lower():
        raise ValueError("ripgrep integrity mismatch")
    with tarfile.open(fileobj=io.BytesIO(artifact), mode="r:gz") as archive:
        entry = archive.getmember(name.removesuffix(".tar.gz") + "/rg")
        if not entry.isfile():
            raise ValueError("ripgrep executable is not a regular file")
        stream = archive.extractfile(entry)
        if stream is None:
            raise ValueError("missing ripgrep executable")
        (directory / "rg").write_bytes(stream.read())
    (directory / "rg").chmod(0o755)
    return {"version": version, "source": source, "integrity": "sha256-" + checksum,
            "executable": "rg"}


def ensure(tool: str, *, root: Path | None = None, run=_run, download=_download,
           which=shutil.which) -> dict[str, Any]:
    """Reuse a working executable or atomically publish a verified tool bundle."""
    if tool not in {"srt", "rg"}:
        raise ValueError("only srt and rg have automatic recovery")
    root = root if root is not None else Path.home() / ".local" / "lib" / "code-cycle-tools"
    target = root / tool
    try:
        existing = which(tool)
        if existing:
            try:
                return {"outcome": "reused", "path": existing,
                        "version": run([existing, "--version"])}
            except (OSError, subprocess.SubprocessError):
                pass
        if target.exists():
            receipt = json.loads((target / "receipt.json").read_text())
            executable = target / receipt["executable"]
            run([str(executable), "--version"])
            return {**receipt, "path": str(executable), "outcome": "reused"}
        if not sys.platform.startswith("linux"):
            return {"outcome": "not_validated", "path": None}
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{tool}-", dir=root) as scratch:
            directory = Path(scratch) / "bundle"
            directory.mkdir()
            receipt = (_srt(directory, run=run, download=download, which=which) if tool == "srt"
                       else _rg(directory, download=download))
            run([str(directory / receipt["executable"]), "--version"])
            (directory / "receipt.json").write_text(json.dumps(receipt))
            outcome = "installed"
            try:
                directory.rename(target)
            except OSError as error:
                if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                # Another cycle won publication. Reuse its committed bundle.
                receipt = json.loads((target / "receipt.json").read_text())
                outcome = "reused"
        executable = target / receipt["executable"]
        run([str(executable), "--version"])
        return {**receipt, "outcome": outcome, "path": str(executable)}
    except (OSError, ValueError, KeyError, TypeError, IndexError,
            subprocess.SubprocessError, tarfile.TarError) as error:
        return {"outcome": "not_verified", "path": None, "error": str(error)}


def nested_codex() -> bool:
    # An explicit stage marker avoids inferring this from a host socket probe.
    return os.environ.get("CODE_CYCLE_STAGE_EXECUTOR") == "codex"


def executable(tool: str, *, path: str | None = None) -> str | None:
    """Find a committed managed bundle even if ~/.local/bin is not on PATH."""
    if path is not None and (selected := shutil.which(tool, path=path)):
        return selected
    bundle = Path.home() / ".local" / "lib" / "code-cycle-tools" / tool
    try:
        receipt = json.loads((bundle / "receipt.json").read_text())
        managed_path = bundle / receipt["executable"]
        if managed_path.is_file() and managed_path.resolve().is_relative_to(bundle.resolve()):
            return str(managed_path)
    except (OSError, ValueError, KeyError):
        pass
    return shutil.which(tool)


def tool_path(path: str, *, probes: list[dict[str, Any]] | None = None) -> str:
    """Prepend only managed bundles, preserving the host toolchain's order."""
    if probes is None:
        binaries = []
        for tool in ("srt", "rg"):
            if shutil.which(tool, path=path):
                continue
            managed_binary = executable(tool)
            bundle = Path.home() / ".local" / "lib" / "code-cycle-tools" / tool
            if managed_binary and Path(managed_binary).is_relative_to(bundle):
                binaries.append(managed_binary)
    else:
        # Preflight has already selected and executed these tools. Rediscovery
        # could shadow a working PATH tool with a stale managed installation.
        selected = [probe for probe in probes if probe.get("name") in {"srt", "rg"}
                    and probe.get("path") and probe.get("executable")
                    and probe.get("outcome") in {"installed", "reused"}]
        # Host PATH tools need no promotion. Only immutable recovery bundles
        # must precede a broken system tool without reordering the host PATH.
        binaries = [probe["path"] for probe in selected]
    directories = [str(Path(binary).parent) for binary in binaries if binary]
    return os.pathsep.join((*dict.fromkeys(directories), path)) if directories else path


def isolation_probe(kind: str, *, runner, path: str | None = None) -> tuple[bool, str]:
    """Try allowed and denied writes/reads and a denied local connection.

    Only the host runs this fixed program; no repository code is evaluated.
    The denied sentinel is checked outside the sandbox as well, so a sandbox
    that falsely returns success cannot establish confinement.
    """
    import socket
    probe_path = tool_path(os.environ.get("PATH", "")) if path is None else path
    with tempfile.TemporaryDirectory(prefix="code-cycle-isolation-") as temporary:
        base = Path(temporary)
        allowed = base / "allowed"
        allowed.mkdir()
        denied = base / "denied"
        secret = base / "secret"
        secret.write_text("probe")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            script = (
                "import pathlib,socket,sys\n"
                f"pathlib.Path({str(allowed / 'ok')!r}).write_text('ok')\n"
                f"try: pathlib.Path({str(denied)!r}).write_text('leak')\n"
                "except OSError: pass\nelse: sys.exit('write escaped')\n"
                f"try: pathlib.Path({str(secret)!r}).read_text()\n"
                "except OSError: pass\nelse: sys.exit('read escaped')\n"
                "s=socket.socket(); s.settimeout(1)\n"
                f"try: s.connect({listener.getsockname()!r})\n"
                "except OSError: pass\nelse: sys.exit('network escaped')\n"
            )
            if kind == "bwrap":
                binary = shutil.which("bwrap")
                if not binary:
                    return False, "bwrap not found"
                argv = [binary, "--die-with-parent", "--unshare-net", "--ro-bind", "/", "/",
                        "--bind", str(allowed), str(allowed), "--ro-bind", os.devnull, str(secret),
                        "--", sys.executable, "-c", script]
                # /dev/null is readable: use a mode-000 file mask instead.
                mask = base / "mask"
                mask.write_text("")
                mask.chmod(0o000)
                argv[argv.index(os.devnull)] = str(mask)
            else:
                binary = executable("srt", path=probe_path)
                if not binary:
                    return False, "srt not found"
                settings = base / "settings.json"
                settings.write_text(json.dumps({
                    "filesystem": {"allowWrite": [str(allowed)],
                                   "denyWrite": [str(denied)], "denyRead": [str(secret)]},
                    "network": {"allowedDomains": [], "deniedDomains": []},
                }))
                argv = [binary, "--settings", str(settings), "--", sys.executable, "-c", script]
            try:
                result = runner(argv, cwd=str(allowed), capture_output=True, text=True,
                                timeout=30, stdin=subprocess.DEVNULL,
                                env={**os.environ, "PATH": probe_path})
                ok = result.returncode == 0 and (allowed / "ok").exists() and not denied.exists()
                detail = (result.stderr or result.stdout or "").strip()[:600]
                return ok, ("allowed writes, denied reads/writes/network verified" if ok
                            else f"{kind} isolation probe exited {result.returncode}: {detail}")
            except (OSError, subprocess.SubprocessError) as error:
                return False, f"{kind} isolation probe unavailable: {error}"


def observe(executor: str, *, recover: bool = True) -> dict[str, Any]:
    probes = []
    for tool in TOOLS:
        if (tool == "rg" or tool == "srt" and executor == "claude") and recover and sys.platform.startswith("linux"):
            record = ensure(tool)
        else:
            path = shutil.which(tool)
            record = {"path": path, "outcome": "available" if path else "missing"}
        probes.append({"name": tool, "observed": record.get("path"), **record})
    required = {"rg"}
    if sys.platform.startswith("linux"):
        required |= {"bwrap", "socat"}
    if executor == "claude":
        required.add("srt")
    missing = [probe["name"] for probe in probes
               if probe["name"] in required and not probe.get("path")]
    result: dict[str, Any] = {"platform": sys.platform, "validated_platform": sys.platform.startswith("linux"),
              "probes": probes, "missing": missing, "functional_check": "not_run",
              "status": "unverified" if missing else "observed"}
    if executor == "codex" and nested_codex():
        result["codex_socket"] = {"outcome": "known_limited",
                                  "reason": "Codex stage masks its app-server socket (#268); no host probe"}
        result["status"] = "unverified"
    host_missing = [tool for tool in missing if tool in {"bwrap", "socat"}]
    if host_missing:
        result["action"] = "install the missing host isolation prerequisites: " + ", ".join(host_missing)
    return result


def known_failure(command: str, error: str) -> bool:
    """Recognise prerequisites narrowly, never general permissions or services."""
    lower = error.lower()
    if any(word in lower for word in ("approval denied", "credential", "authentication")):
        return False
    if re.search(r"(?:^|\s|/)codex(?:\s|$)", command) and "app-server socket directory must be" in lower:
        return True
    if any(marker in lower for marker in ("sandbox required but unavailable",
                                          "refusing to start without a working sandbox")):
        return True
    # Match a missing executable, not a tool-shaped substring in a project path
    # or a permission error on data that a working tool is trying to access.
    tool = r"(?:srt|rg|ripgrep(?:\s+\(rg\))?|bwrap|bubblewrap|socat)"
    missing = (
        rf"(?<![\w.-]){tool}['\"]?(?::\s*|\s+)(?:command\s+)?(?:not found|not installed)",
        rf"needs the installed\s+{tool}\b",
        rf"no such file or directory:\s*['\"](?:[^'\"]*/)?{tool}['\"]",
        rf"\bspawn\s+{tool}\s+enoent\b",
    )
    if any(re.search(pattern, lower) for pattern in missing):
        return True
    return bool(re.search(
        r"\b(?:bwrap|bubblewrap|srt):\s*(?:error:\s*)?"
        r"(?:cannot create namespace|creating new namespace failed|no permissions to create new namespace)",
        lower))


def static_result(spoken: str, commands, evidence: dict[str, Any]) -> str:
    """A static dispatch cannot vouch for executed tests, even if an agent does."""
    end = spoken.rfind("END_ORCHESTRATION_RESULT")
    begin = spoken.rfind("ORCHESTRATION_RESULT", 0, end) if end >= 0 else -1
    if begin < 0:
        return spoken
    try:
        payload = json.loads(spoken[begin + len("ORCHESTRATION_RESULT"):end].strip())
    except ValueError:
        return spoken
    if not isinstance(payload, dict):
        return spoken
    payload["tests"] = {"version": 2, "ran": False, "conclusion": "not_verified",
                        "checks": [{"id": f"native-{index}", "command": command,
                                    "executor": "native", "profile": "selected",
                                    "context": "native", "required": True, "criteria": [],
                                    "level": "test", "provenance": "runtime",
                                    "reason": "configured verification",
                                    "head_sha": payload.get("head_sha"), "result": "unverified",
                                    "evidence": [], "cause": "native isolation unavailable",
                                    "attempts": []}
                                   for index, command in enumerate(commands, 1)]}
    payload["native_sandbox_preflight"] = evidence
    return (spoken[:begin] + "ORCHESTRATION_RESULT\n" + json.dumps(payload)
            + "\n" + spoken[end:])
