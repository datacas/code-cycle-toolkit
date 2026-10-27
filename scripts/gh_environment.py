"""Environment defaults that keep GitHub CLI telemetry out of cycle runs."""

from __future__ import annotations

import os


GH_ENVIRONMENT_DEFAULTS = {
    "GH_TELEMETRY": "false",
    "GH_NO_UPDATE_NOTIFIER": "1",
}


def gh_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment with gh telemetry disabled unless explicitly set."""
    result = dict(os.environ if environment is None else environment)
    for name, value in GH_ENVIRONMENT_DEFAULTS.items():
        result.setdefault(name, value)
    return result


def apply_gh_environment_defaults() -> None:
    """Apply the defaults to this runtime process before it invokes ``gh``."""
    for name, value in GH_ENVIRONMENT_DEFAULTS.items():
        os.environ.setdefault(name, value)
