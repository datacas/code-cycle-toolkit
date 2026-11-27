"""Scoped consent for user-controlled settings that reduce toolkit guarantees."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from grants import GrantScope, SensitiveGrant, identifier


UNGUARDED_OPERATIONS = (
    "force-push", "pushes to the base branch", "Git ref deletion", "credential access",
)


def binding(*, setting: str, value: Any, source: str, layer: str,
            executor: str, guarantees: list[str], provenance: str) -> dict[str, Any]:
    """Return the exact, stable identity a reduced-guarantee answer covers."""
    material = {
        "category": "reduced_guarantee_consent",
        "setting": setting,
        "value": value,
        "source": source,
        "layer": layer,
        "executor": executor,
        "disabled_guarantees": sorted(set(guarantees)),
        "provenance": provenance,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    material["fingerprint"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return material


def grant_for(record: dict[str, Any], platform: str, role: str) -> SensitiveGrant:
    """Build a consent request bound to one effective configuration record."""
    effective = json.dumps(record, sort_keys=True, separators=(",", ":"))
    risks = (
        f"Reduced guarantee: {record['setting']} from {record['source']} "
        f"({record['layer']}) disables {', '.join(record['disabled_guarantees'])}. "
        "After consent, the stage may run without those toolkit protections. "
        f"Unguarded operations include {', '.join(UNGUARDED_OPERATIONS)}. "
        "Underlying executor, managed-policy, operating-system, provider and infrastructure "
        "limits remain in force."
    )
    identity = {
        "version": 1,
        "kind": "reduced_guarantee_consent",
        "tool": "ReducedGuarantee",
        **record,
    }
    operation = {
        **identity,
        "role": role,
        "scope_policy": ("cycle" if record["provenance"] == "untrusted_change"
                         else "cycle_repository" if role in {"review", "rereview"}
                         else "cycle_repository_global"),
    }
    return SensitiveGrant(
        id=identifier(identity),
        resource=record["setting"],
        reason="the effective configuration reduces a toolkit guarantee",
        scopes=(GrantScope(record["executor"], platform, effective, risks, True),),
        operation=operation,
    )


def is_granted(answer: str) -> bool:
    from grants import ALLOW_CYCLE, ALLOW_GLOBAL, ALLOW_PERSISTENT
    return answer in {ALLOW_CYCLE, ALLOW_PERSISTENT, ALLOW_GLOBAL}
