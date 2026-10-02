"""Build a stable, content-free snapshot of the code-cycle execution harness."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

try:
    from .telemetry import (
        CREDENTIAL_PREFIXES,
        IDENTIFIER_PATTERN,
        MAX_IDENTIFIER_LENGTH,
    )
except ImportError:
    from telemetry import (
        CREDENTIAL_PREFIXES,
        IDENTIFIER_PATTERN,
        MAX_IDENTIFIER_LENGTH,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hash(path: Path) -> str | None:
    try:
        return _sha256(path.read_bytes())
    except OSError:
        return None


def _version(root: Path) -> str | None:
    for relative in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
        try:
            value = json.loads((root / relative).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        version = value.get("version") if isinstance(value, dict) else None
        if isinstance(version, str) and version and len(version) <= 64:
            return version
    return None


def _commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, check=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if len(value) == 40 and all(c in "0123456789abcdef" for c in value) else None


def _skill_hash(root: Path, skill: str | None) -> str | None:
    if not skill or "/" in skill or ".." in skill:
        return None
    candidates = (
        root / ".agents" / "skills" / skill / "SKILL.md",
        root / ".codex" / "skills" / skill / "SKILL.md",
        Path.home() / ".agents" / "skills" / skill / "SKILL.md",
        Path.home() / ".codex" / "skills" / skill / "SKILL.md",
    )
    for candidate in candidates:
        digest = _file_hash(candidate)
        if digest:
            return digest
    return None


def _safe_profile_hash(profiles, profile: str) -> str | None:
    if not isinstance(profiles, dict):
        return None
    configured = profiles.get(profile)
    if isinstance(configured, dict):
        primary = configured.get("primary")
        fallback = configured.get("fallback")
    else:
        primary = getattr(configured, "primary", None)
        fallback = getattr(configured, "fallback", None)
    if primary is None:
        return None

    def safe_target(target) -> dict | None:
        if isinstance(target, str):
            try:
                where, effort = target.rsplit(" ", 1)
                executor, rest = where.split(":", 1)
                provider, model = rest.split("/", 1)
            except ValueError:
                return None
        elif isinstance(target, dict):
            executor = target.get("executor")
            provider = target.get("provider")
            model = target.get("model")
            effort = target.get("effort")
        else:
            executor = getattr(target, "executor", None)
            provider = getattr(target, "provider", None)
            model = getattr(target, "model", None)
            effort = getattr(target, "effort", None)

        if not all(isinstance(value, str) for value in (executor, provider, model, effort)):
            return None
        executor, provider, model, effort = (
            value.strip() for value in (executor, provider, model, effort)
        )
        if executor not in {"codex", "claude", "orca"}:
            return None
        if provider not in {"openai", "anthropic"}:
            return None
        if effort not in {"low", "medium", "high", "max"}:
            return None
        if (len(model) > MAX_IDENTIFIER_LENGTH
                or not IDENTIFIER_PATTERN.fullmatch(model)
                or model.startswith(CREDENTIAL_PREFIXES)):
            return None
        return {
            "executor": executor,
            "provider": provider,
            "model_sha256": _sha256(model.encode("utf-8")),
            "effort": effort,
        }

    primary_fields = safe_target(primary)
    if primary_fields is None:
        return None
    fallback_fields = safe_target(fallback) if fallback is not None else None
    if fallback is not None and fallback_fields is None:
        return None

    effective_profile = {"primary": primary_fields, "fallback": fallback_fields}
    encoded = json.dumps(
        effective_profile, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def build_harness_snapshot(
    *,
    role: str,
    skill: str | None,
    profile: str,
    routing_strategy: str,
    readiness_policy: str,
    executor: str,
    probe,
    profiles,
    root: Path | None = None,
) -> dict:
    """Return only safe labels and hashes; never return a path or source text."""
    root = root or Path(__file__).resolve().parents[1]
    manifest_hash = _file_hash(root / "scripts" / "runtime.manifest")
    routing_hash = _file_hash(root / "scripts" / "router.py")
    prompt_hashes = [
        _file_hash(root / "scripts" / name)
        for name in ("run_cycle.py", "cycle.py", "review_contract.py")
    ]
    prompt_hashes = [value for value in prompt_hashes if value]
    prompt_hash = _sha256("".join(prompt_hashes).encode("ascii")) if prompt_hashes else None
    raw_version = getattr(probe, "version", None)
    if isinstance(raw_version, (tuple, list)) and raw_version:
        executor_version = ".".join(str(part) for part in raw_version)
    elif isinstance(raw_version, str):
        match = re.search(r"\d+(?:\.\d+){0,5}", raw_version)
        executor_version = match.group(0) if match else None
    else:
        executor_version = None
    components = {
        "toolkit_release": _version(root),
        "toolkit_commit": _commit(root),
        "runtime_manifest_sha256": manifest_hash,
        "routing_policy_sha256": routing_hash,
        "prompt_template_sha256": prompt_hash,
        "executor": executor,
        "executor_version": executor_version,
        "profile": profile,
        "profile_config_sha256": _safe_profile_hash(profiles, profile),
        "routing_strategy": routing_strategy,
        "readiness_policy": readiness_policy,
        "role": role,
        "skill": skill,
        "skill_sha256": _skill_hash(root, skill),
    }
    encoded = json.dumps(components, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fingerprint = _sha256(encoded)
    return {
        "harness_snapshot_id": f"harness-{fingerprint}",
        "fingerprint": fingerprint,
        "components": components,
    }
