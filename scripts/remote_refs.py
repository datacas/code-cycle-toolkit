"""Configuration shared by remote-ref inspection callers."""

from __future__ import annotations

from pathlib import Path


DEFAULT_TIMEOUT = 10
MIN_TIMEOUT = 1
MAX_TIMEOUT = 300


def timeout_of(config: dict) -> int:
    """Return the configured timeout for one remote-ref inspection attempt."""
    code_cycle = config.get("code_cycle") or {}
    review = code_cycle.get("review") or {}
    value = review.get("remote_refs_timeout", DEFAULT_TIMEOUT)
    if (isinstance(value, bool) or not isinstance(value, int)
            or not MIN_TIMEOUT <= value <= MAX_TIMEOUT):
        raise ValueError(
            "code_cycle.review.remote_refs_timeout must be a whole number of "
            f"seconds between {MIN_TIMEOUT} and {MAX_TIMEOUT}")
    return value


def timeout_from_root(root: str) -> int:
    """Read the repository setting, using the default when no config exists."""
    path = Path(root) / ".code-cycle.yml"
    if not path.exists():
        return DEFAULT_TIMEOUT
    try:
        import yaml
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ValueError(f"{path} could not be read") from exc
    if not isinstance(config, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return timeout_of(config)
