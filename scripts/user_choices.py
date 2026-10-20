"""Project-scoped user authorization, carried as immutable dispatch policy.

This configuration is trusted on the source checkout, never loaded from the
reviewed clone. It does not grant general networking or publication rights.
"""
from __future__ import annotations

import re

from stage_permissions import PolicyError, parse_command

KEYS = frozenset({"tools", "commands", "integrations", "registry_domains"})


def load(config: dict) -> dict[str, tuple[str, ...]]:
    value = (config.get("code_cycle") or {}).get("user_choices", {})
    if not isinstance(value, dict) or set(value) - KEYS:
        raise PolicyError("code_cycle.user_choices must be a mapping with only "
                          "tools, commands, integrations and registry_domains")
    result = {}
    for key in KEYS:
        entries = value.get(key, [])
        if (not isinstance(entries, list)
                or any(not isinstance(entry, str) or not entry.strip() for entry in entries)):
            raise PolicyError(f"user_choices.{key} must be a list of non-empty strings")
        normalized = []
        for entry in entries:
            entry = entry.strip()
            if key == "tools" and not re.fullmatch(r"[a-zA-Z0-9_./+-]+", entry):
                raise PolicyError("a selected tool must name one executable")
            if key == "commands":
                parsed = parse_command(entry, None)
                if parsed.redirects:
                    raise PolicyError("selected commands cannot contain redirections")
            if key == "integrations" and not re.fullmatch(r"mcp__[a-zA-Z0-9_*-]+", entry):
                raise PolicyError("an integration must name an MCP tool or server pattern")
            if key == "registry_domains" and not re.fullmatch(
                    r"(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", entry):
                raise PolicyError("registry_domains must name exact package-registry hosts")
            normalized.append(entry)
        result[key] = tuple(dict.fromkeys(normalized))
    return result
