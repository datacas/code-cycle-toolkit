"""Deterministic Node preparation inside a native executor's OS sandbox.

Never execute a project script on the host. Cache downloads, not another HEAD's
node_modules. A receipt reuses an install only in the same checkout at the same
HEAD, with the same lockfile, manifests, manager version and platform.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

REGISTRIES = ("registry.npmjs.org", "registry.yarnpkg.com")
LOCKS = {"pnpm-lock.yaml": "pnpm", "package-lock.json": "npm",
         "npm-shrinkwrap.json": "npm", "yarn.lock": "yarn"}
TRANSIENT = ("econnreset", "etimedout", "eai_again", "502", "503", "504",
             "socket hang up", "fetch failed")
LIFECYCLE = ("preinstall", "install", "postinstall", "prepare")
# Never publish Git metadata, a native sandbox's protected placeholders (Codex's
# and srt's), or configuration that host tools load outside the sandbox.
UNPUBLISHED = {".git", ".agents", ".codex", "node_modules", ".yarn",
               ".pnp.cjs", ".pnp.loader.mjs",
               ".vscode", ".idea", ".claude", ".mcp.json", ".gitconfig", ".gitmodules",
               ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile", ".ripgreprc",
               ".envrc", ".cursor", ".husky"}


def projects(root: Path) -> list[tuple[Path, str, Path]]:
    """Find independent lock roots; a workspace root covers all its packages."""
    found = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in
                   {"node_modules", ".git", ".yarn", ".worktree"}
                   and not (Path(directory) / name).is_symlink()]
        locks = [name for name in LOCKS if name in files]
        if not locks or "package.json" not in files:
            continue
        managers = {LOCKS[name] for name in locks}
        if len(managers) != 1:
            raise ValueError(f"conflicting package-manager lockfiles in {directory}")
        # npm-shrinkwrap takes precedence over package-lock, as npm does.
        lock = "npm-shrinkwrap.json" if "npm-shrinkwrap.json" in locks else locks[0]
        found.append((Path(directory), LOCKS[lock], Path(directory) / lock))
        dirs[:] = []
    return found


def manifests(root: Path) -> list[Path]:
    result = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in
                   {"node_modules", ".git", ".yarn", ".worktree"}
                   and not (Path(directory) / name).is_symlink()]
        if "package.json" in files:
            result.append(Path(directory) / "package.json")
    return sorted(result)


def install_command(manager: str, version: str, cache: Path) -> list[str]:
    if manager == "npm":
        return ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund",
                "--cache", str(cache)]
    if manager == "pnpm":
        return ["pnpm", "install", "--frozen-lockfile", "--ignore-scripts",
                "--store-dir", str(cache), "--package-import-method=copy"]
    major = int(version.split(".")[0])
    if major == 1:
        return ["yarn", "install", "--frozen-lockfile", "--ignore-scripts",
                "--cache-folder", str(cache)]
    return ["yarn", "install", "--immutable"]


def build_command(manager: str, version: str, cache: Path) -> list[str]:
    """The same frozen install with the package manager's scripts enabled.

    The manager's own build policy still applies, such as pnpm's
    onlyBuiltDependencies or Yarn's dependenciesMeta.
    """
    return [arg for arg in install_command(manager, version, cache) if arg != "--ignore-scripts"]


def _manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("scripts", {}), dict):
        raise ValueError(f"invalid package manifest: {path}")
    return data


def sandbox_command(executor: str, binary: str, command: list[str], *,
                    writable: list[Path], protected: list[Path], domains: tuple[str, ...],
                    settings: Path) -> list[str]:
    if executor == "codex":
        profile = "code_cycle_prepare"
        rules = {str(path): "write" for path in writable}
        # :read-only already protects the rest of the checkout. Serialising
        # every tracked source file here can exceed the OS per-argument limit.
        # Keep exceptions inside writable roots, and tracked ancestors that
        # must prevent a writable output from replacing a source file.
        rules.update({str(path): "read" for path in protected
                      if any(path.is_relative_to(output) or output.is_relative_to(path)
                             for output in writable)})
        filesystem = "{" + ",".join(json.dumps(key) + "=" + json.dumps(value)
                                    for key, value in rules.items()) + "}"
        network = "{" + ",".join(json.dumps(host) + '=\"allow\"' for host in domains) + "}"
        return [binary, "sandbox", "--permission-profile", profile,
                "-c", f'permissions.{profile}.extends=":read-only"',
                "-c", "features.network_proxy=true",
                "-c", f"permissions.{profile}.network.enabled=true",
                "-c", f"permissions.{profile}.network.domains={network}",
                "-c", f"permissions.{profile}.filesystem={filesystem}", "--", *command]
    # This is the same sandbox runtime Claude Code uses for Bash. No npx
    # bootstrap or unsandboxed fallback when the host has not installed it.
    srt = shutil.which("srt")
    if not srt:
        raise OSError("Claude dependency preparation needs the installed srt sandbox runtime")
    settings.write_text(json.dumps({
        "network": {"allowedDomains": list(domains), "deniedDomains": [],
                    "allowLocalBinding": False},
        "filesystem": {"allowWrite": [str(path) for path in writable],
                       "denyWrite": [str(path) for path in protected],
                       "denyRead": ["~/.ssh", "~/.aws", "~/.config/gh"]},
    }), encoding="utf-8")
    # srt shell-quotes each positional argument itself, so a pre-joined string
    # would run as one word. "--" keeps srt from parsing npm's own flags, such
    # as --version.
    return [srt, "--settings", str(settings), "--", *command]


def _digest(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(paths[0].parent)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _text(value) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else value or ""


def _populated(path: Path) -> bool:
    # Sandbox placeholders such as Codex's .agents are dot-entries; installed
    # packages are not.
    return path.is_dir() and any(not entry.name.startswith(".") for entry in path.iterdir())


def _stage(root: Path, project: Path, names: list[str], inputs: list[Path], stage: Path) -> Path:
    """Copy the project's sources so the install never writes the checkout.

    Codex mounts protected placeholders (.agents, .codex, .git) inside every
    writable root, and npm ci cannot empty a node_modules holding a mount
    point. Only the scratch copy is writable; the checkout stays read-only.
    """
    for source in [*(root / name for name in names), *inputs]:
        if not source.is_relative_to(project) or not (source.is_file() or source.is_symlink()):
            continue
        target = stage / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            continue
        if source.is_symlink():
            os.symlink(os.readlink(source), target)
        else:
            shutil.copy2(source, target)
    return stage / project.relative_to(root)


def _replace(source: Path, target: Path) -> None:
    """Move a finished output from the scratch copy; never follow links."""
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    if source.is_symlink() or source.exists():
        shutil.move(str(source), str(target))


def _publish(staged: Path, project: Path, outputs: list[Path], tracked: set[Path]) -> None:
    """Replace each package's node_modules, as a frozen install does.

    Yarn writes into .yarn beside tracked releases or plugins; only its
    untracked entries and untracked PnP files are generated outputs.
    """
    for output in outputs:
        _replace(staged / output.relative_to(project), output)
        # A package without dependencies may get none; a rereview's reuse
        # check still expects every package-local output to exist.
        output.mkdir(exist_ok=True)
    for name in (".pnp.cjs", ".pnp.loader.mjs"):
        if (staged / name).is_file() and project / name not in tracked:
            _replace(staged / name, project / name)
    if (staged / ".yarn").is_dir():
        (project / ".yarn").mkdir(exist_ok=True)
        for entry in (staged / ".yarn").iterdir():
            target = project / ".yarn" / entry.name
            if not any(path == target or path.is_relative_to(target) for path in tracked):
                _replace(entry, target)


def _needs_build(tree: Path, packages: list[Path]) -> list[str]:
    """Manifests whose lifecycle scripts or native builds a disabled install skipped."""
    installed = []
    for pattern in ("node_modules/*/package.json", "node_modules/@*/*/package.json",
                    "node_modules/.pnpm/*/node_modules/*/package.json",
                    "node_modules/.pnpm/*/node_modules/@*/*/package.json"):
        installed += sorted(tree.glob(pattern))
    found = []
    for path in [*packages, *installed]:
        scripts = _manifest(path).get("scripts") or {}
        if any(name in scripts for name in LIFECYCLE) or (path.parent / "binding.gyp").is_file():
            found.append(path.relative_to(tree).as_posix())
    return found


def _generated(built: Path, root: Path, project: Path, packages: list[Path],
               tracked: set[Path]) -> list[Path]:
    """Checkout paths of untracked, Git-ignored entries a build created beside a manifest.

    Ignore rules come from the read-only checkout, never from the build tree,
    which project scripts could rewrite. Links are not published.
    """
    candidates = {}
    for manifest in packages:
        directory = manifest.parent
        for entry in sorted((built / directory.relative_to(project)).iterdir()):
            target = directory / entry.name
            if (entry.name in UNPUBLISHED or entry.is_symlink()
                    or any(path == target or path.is_relative_to(target) for path in tracked)):
                continue
            # A trailing slash lets directory-only patterns such as ".nuxt/"
            # match a directory that does not exist in the checkout yet.
            candidates[target.relative_to(root).as_posix() + ("/" if entry.is_dir() else "")] = target
    if not candidates:
        return []
    result = subprocess.run(["git", "check-ignore", "-z", "--stdin"], cwd=root,
                            input="\0".join(candidates), capture_output=True, text=True, timeout=30)
    if result.returncode not in (0, 1):
        raise ValueError(f"git check-ignore failed with exit {result.returncode}: {result.stderr}")
    return [candidates[name] for name in result.stdout.split("\0") if name in candidates]


def _entry_digest(path: Path) -> str:
    if path.is_file() and not path.is_symlink():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return _installed_digest([path])


def _owned(ledger: Path) -> dict[str, str]:
    """Generated entries this checkout's preparation published, by content digest.

    An unreadable ledger owns nothing, so every existing entry is preserved.
    """
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items()
            if isinstance(key, str) and isinstance(value, str)}


def _installed_digest(outputs: list[Path]) -> str:
    """Verify the cached tree itself, including pnpm's real .pnpm contents."""
    digest = hashlib.sha256()
    for output in outputs:
        for directory, dirs, files in os.walk(output, followlinks=False):
            dirs.sort()
            for name in sorted([*dirs, *files]):
                path = Path(directory) / name
                digest.update(str(path).encode())
                if path.is_symlink():
                    digest.update(os.readlink(path).encode())
                elif path.is_file():
                    digest.update(path.read_bytes())
    return digest.hexdigest()


def _run(argv, *, timeout, **options):
    """A timed-out install must exit with its children before a retry starts."""
    options.pop("capture_output", None)
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=os.name != "nt", **options)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def prepare(cwd: str, executor: str, binary: str, *,
            domains: tuple[str, ...] = (), runner=_run,
            evidence_root: Path | None = None, sleep=time.sleep) -> dict:
    """Return honest setup evidence even on failure; no verdict or CI claim.

    Logs outlive the disposable clone. Failure is given to the stage so it can
    continue source inspection and consult exact-HEAD CI rather than publish a
    terminal blocker before automatic recovery has run.
    """
    root = Path(cwd).resolve()
    plans = projects(root)
    if not plans:
        return {"status": "not_required", "items": []}
    base = evidence_root or Path.home() / ".local/state/code-cycle-toolkit/preparation"
    base.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="setup-", dir=base))
    report = {"status": "prepared", "items": [], "evidence": str(evidence / "report.json")}
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                          capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                             capture_output=True, text=True, timeout=30, check=True).stdout
    names = [name for name in tracked.split("\0") if name]
    tracked_paths = {root / name for name in names}
    for index, (project, manager, lock) in enumerate(plans):
        item = {"cwd": str(project), "head_sha": head, "manager": manager,
                "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
                "attempts": [], "status": "failed"}
        report["items"].append(item)
        packages = manifests(project)
        inputs = [lock, *packages]
        fingerprint = _digest(inputs)
        outputs = [path.parent / "node_modules" for path in packages]
        generated = [project / ".yarn", project / ".pnp.cjs", project / ".pnp.loader.mjs"]
        # Output paths must never escape via links left by a previous run.
        if any(path.is_symlink() or not path.resolve().is_relative_to(root)
               for path in [*outputs, *generated]):
            item["error"] = "dependency output path escapes the checkout"
            report["status"] = "incomplete"
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="code-cycle-prepare-") as temporary:
                scratch = Path(temporary)
                env = {key: value for key, value in os.environ.items()
                       if key in {"PATH", "SYSTEMROOT", "LANG", "LC_ALL",
                                  "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                                  "http_proxy", "https_proxy", "no_proxy"}}
                env.update(HOME=str(scratch), TMPDIR=str(scratch), CI="true",
                           COREPACK_ENABLE_DOWNLOAD_PROMPT="0", COREPACK_HOME=str(scratch / "corepack"),
                           YARN_ENABLE_SCRIPTS="false", npm_config_ignore_scripts="true")
                staged = _stage(root, project, names, inputs, scratch / "tree")
                staged_inputs = [staged / path.relative_to(project) for path in inputs]
                # Nothing in the checkout is writable. The lockfile and
                # manifests stay read-only in the copy as well.
                writable = [scratch]
                def run(command, label, timeout, *, cwd=staged, environment=env,
                        allowed=writable, protected=staged_inputs):
                    record = {"command": command}
                    item["attempts"].append(record)
                    try:
                        argv = sandbox_command(executor, binary, command, writable=allowed,
                                               protected=protected, domains=tuple(dict.fromkeys(
                                                   (*REGISTRIES, *domains))), settings=scratch / "sandbox.json")
                        record["sandbox_command"] = argv
                        result = runner(argv, cwd=cwd, env=environment, capture_output=True,
                                        text=True, timeout=timeout, stdin=subprocess.DEVNULL)
                        code, stdout, stderr = result.returncode, _text(result.stdout), _text(result.stderr)
                    except subprocess.TimeoutExpired as error:
                        code, stdout, stderr = 124, _text(error.stdout), _text(error.stderr) + "\nsetup timed out"
                    except OSError as error:
                        code, stdout, stderr = None, "", str(error)
                    record.update(exit_code=code)
                    for stream, content in (("stdout", stdout), ("stderr", stderr)):
                        path = evidence / f"{index}-{label}.{stream}.log"
                        path.write_text(content, encoding="utf-8")
                        record[stream] = str(path)
                    return code, stdout, stderr
                def install(command, label, **options):
                    for attempt in range(2):
                        code, stdout, stderr = run(command, f"{label}-{attempt + 1}", 600, **options)
                        if code == 0 or (code != 124 and not any(
                                token in (stdout + stderr).lower() for token in TRANSIENT)):
                            break
                        if attempt == 0:
                            sleep(1)
                    return code
                def complete(tree):
                    required = [tree / path.parent.relative_to(project) / "node_modules"
                                for path in packages
                                if any(_manifest(path).get(key) for key in
                                       ("dependencies", "devDependencies", "optionalDependencies"))]
                    # Neither an empty directory nor a sandbox placeholder is an install.
                    return not (required and not (tree / ".pnp.cjs").is_file()
                                and (any(not _populated(output) for output in required)
                                     if manager == "pnpm" else not _populated(tree / "node_modules")))
                # --version may bootstrap a declared pnpm/Corepack binary from
                # the registry. It needs the same bounded recovery as install.
                code, version, error = run([manager, "--version"], "version", 60)
                if code == 124 or (code != 0 and any(
                        token in (version + error).lower() for token in TRANSIENT)):
                    sleep(1)
                    code, version, error = run([manager, "--version"], "version-retry", 60)
                if code != 0:
                    raise ValueError(f"package manager version failed with exit {code}: {error}")
                if not version.strip():
                    raise ValueError("package manager returned no version")
                version = version.strip().splitlines()[-1]
                item["version"] = version
                declaration = _manifest(project / "package.json").get("packageManager")
                if declaration:
                    if not isinstance(declaration, str):
                        raise ValueError("packageManager must be a string")
                    declared_manager, _, declared_version = declaration.partition("@")
                    declared_version = declared_version.split("+", 1)[0]
                    if declared_manager != manager or declared_version != version:
                        raise ValueError(f"packageManager {declaration} does not match {manager}@{version}")
                cache_key = hashlib.sha256((fingerprint + version + platform.platform()).encode()).hexdigest()
                item["cache_key"] = cache_key
                cache = base / "cache" / cache_key
                cache.mkdir(parents=True, exist_ok=True)
                writable.append(cache)
                receipt = base / (hashlib.sha256((str(project) + head + cache_key).encode()).hexdigest()
                                  + ".json")
                # A marker alone never establishes that package-local outputs
                # still exist (notably apps/web/node_modules on a rereview).
                if receipt.is_file() and all(path.is_dir() for path in outputs):
                    previous = json.loads(receipt.read_text(encoding="utf-8"))
                    if (previous.get("inputs") == fingerprint and previous.get("head_sha") == head
                            and previous.get("installed") == _installed_digest(outputs)
                            and all((project / name).exists() for name in previous.get("generated", []))):
                        item.update(status="prepared", reused=True)
                        continue
                env["YARN_CACHE_FOLDER"] = str(cache)
                code = install(install_command(manager, version, cache), "install")
                if _digest(inputs) != fingerprint or _digest(staged_inputs) != fingerprint:
                    raise ValueError("preparation changed the lockfile or package manifests")
                if code != 0:
                    raise ValueError(f"frozen install failed with exit {code}")
                if not complete(staged):
                    raise ValueError("frozen install returned success without required dependency outputs")
                source, built_outputs = staged, []
                # Disabled lifecycle scripts can leave native modules or project
                # outputs incomplete. Rebuild in a separate scratch tree with
                # scripts enabled, inside the same sandbox and registry-only
                # network. Only that tree, its own HOME and a private copy of
                # the download cache are writable: project scripts never write
                # the checkout, the shared cache or the scripts-disabled tree,
                # which stays as the fallback when the build fails.
                needs_build = _needs_build(staged, [staged / path.relative_to(project)
                                                    for path in packages])
                if needs_build:
                    item["needs_build"] = needs_build
                    built = _stage(root, project, names, inputs, scratch / "build")
                    built_inputs = [built / path.relative_to(project) for path in inputs]
                    home, build_cache = scratch / "build-home", scratch / "build-cache"
                    home.mkdir()
                    shutil.copytree(cache, build_cache, symlinks=True)
                    if (scratch / "corepack").is_dir():
                        shutil.copytree(scratch / "corepack", home / "corepack", symlinks=True)
                    build_env = {**env, "HOME": str(home), "TMPDIR": str(home),
                                 "COREPACK_HOME": str(home / "corepack"),
                                 "YARN_CACHE_FOLDER": str(build_cache),
                                 "YARN_ENABLE_SCRIPTS": "true", "npm_config_ignore_scripts": "false"}
                    command = build_command(manager, version, build_cache)
                    build = {"command": command}
                    item["lifecycle_build"] = build
                    code = install(command, "build", cwd=built, environment=build_env,
                                   allowed=[scratch / "build", home, build_cache], protected=built_inputs)
                    build["exit_code"] = code
                    if _digest(built_inputs) != fingerprint:
                        build.update(status="failed", error="the build changed the lockfile or package manifests")
                    elif code != 0:
                        build.update(status="failed", error=f"lifecycle build failed with exit {code}")
                    elif not complete(built):
                        build.update(status="failed",
                                     error="lifecycle build returned success without required dependency outputs")
                    else:
                        source = built
                        built_outputs = _generated(built, root, project, packages, tracked_paths)
                        build["status"] = "built"
                # Output paths were checked above; check again before host writes.
                if any(path.is_symlink() or not path.resolve().is_relative_to(root)
                       for path in [*outputs, *generated]):
                    raise ValueError("dependency output path escapes the checkout")
                _publish(source, project, outputs, tracked_paths)
                # The build copy holds no untracked checkout content, so an
                # existing ignored entry such as a developer's .env or dist/ is
                # not the build's. Replace one only when an earlier preparation
                # of this checkout published it and it is unchanged since.
                ledger = base / (hashlib.sha256(str(project).encode()).hexdigest() + "-generated.json")
                owned, published, preserved = _owned(ledger), [], []
                for target in built_outputs:
                    name = target.relative_to(project).as_posix()
                    if target.is_symlink() or (target.exists() and owned.get(name) != _entry_digest(target)):
                        preserved.append(name)
                        owned.pop(name, None)
                        continue
                    _replace(source / name, target)
                    owned[name] = _entry_digest(target)
                    published.append(name)
                if built_outputs:
                    ledger.write_text(json.dumps(owned, sort_keys=True), encoding="utf-8")
                if source is not staged:
                    build["generated"] = published
                    if preserved:
                        build["preserved"] = preserved
                if source is not staged or not needs_build:
                    item["status"] = "prepared"
                    receipt.write_text(json.dumps({"head_sha": head, "inputs": fingerprint,
                                                   "cache_key": cache_key,
                                                   "installed": _installed_digest(outputs),
                                                   "generated": published}),
                                       encoding="utf-8")
                else:
                    item["status"] = "scripts_disabled"
                    item["isolated_recovery"] = (
                        "The required lifecycle build ran in this same restricted preparation "
                        "sandbox and did not succeed; its logs are in this evidence. The "
                        "scripts-disabled install was published instead. Never run the build on "
                        "the host. Local verification remains unverified until that build and "
                        "the original checks actually succeed.")
                    report["status"] = "incomplete"
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            item["error"] = str(error)
            report["status"] = "incomplete"
    (evidence / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
