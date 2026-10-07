"""Shared helpers for tests that must not inherit the developer's agent host."""

from __future__ import annotations

import os
import tempfile


_HOST_PREFIXES = ("HERDR_", "CODEX_", "CLAUDE_", "ORCA_")
_HOST_SETTINGS = {"CODE_CYCLE_HOME", "XDG_CONFIG_HOME", "APPDATA"}


def isolate_host_environment():
    """Clear host-specific settings for a test module and return a restore hook.

    Tests that exercise a host integration set its variables explicitly in the
    test body, where the dependency is visible and scoped to that test.
    """
    removed = {
        key: value for key, value in os.environ.items()
        if key in _HOST_SETTINGS or key.startswith(_HOST_PREFIXES)
    }
    for key in removed:
        os.environ.pop(key, None)
    isolated_codex_home = tempfile.TemporaryDirectory(prefix="cycle-codex-home-")
    os.environ["CODEX_HOME"] = isolated_codex_home.name

    def restore() -> None:
        for key in _HOST_SETTINGS:
            os.environ.pop(key, None)
        for key in tuple(os.environ):
            if key.startswith(_HOST_PREFIXES):
                os.environ.pop(key, None)
        os.environ.update(removed)
        isolated_codex_home.cleanup()

    return restore
