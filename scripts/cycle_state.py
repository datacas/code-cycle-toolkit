"""Mutable state shared by dispatch, history, and the stage loop."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .cycle_history import CycleHistoryStore
    from .router import Target
    from .verification_plan import VerificationPlan


@dataclass
class SessionState:
    change_request_id: str | None = None
    previous_resolve_target: Target | None = None
    previous_resolve_used_fallback: bool = False
    review_attempts: set[str] = field(default_factory=set)
    pending_recovery: dict[str, Any] | None = None
    verification_plan: VerificationPlan | None = None
    criteria_context: str = ""
    history_store: CycleHistoryStore | None = None
    history_lock: tuple[CycleHistoryStore, str, str] | None = None
