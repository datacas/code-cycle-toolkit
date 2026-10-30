"""Ownership ledger for setup, committed at the first dispatch or handoff."""
from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CreatedResource:
    kind: str
    identity: str
    provenance: str
    compensate: Callable[[], None]


_active: ContextVar[WorkspaceSetup | None] = ContextVar("workspace_setup", default=None)


class WorkspaceSetup:
    """Compensate owned resources in reverse order until dispatch commits.

    The ledger is run-local, never inferred from an existing checkout. Commit
    is permanent: failures in a stage cannot reactivate setup rollback.
    """

    def __init__(self):
        self.resources: list[CreatedResource] = []
        self.committed = False
        self.errors: list[str] = []
        self._token: Token[WorkspaceSetup | None] | None = None

    def record(self, kind: str, identity: str | Path, provenance: str,
               compensate: Callable[[], None]) -> None:
        if not self.committed:
            self.resources.append(CreatedResource(kind, str(identity), provenance, compensate))

    def commit(self) -> None:
        self.committed = True
        self.resources.clear()

    def rollback(self) -> list[str]:
        if not self.committed:
            while self.resources:
                resource = self.resources.pop()
                try:
                    resource.compensate()
                except Exception as error:
                    self.errors.append(f"{resource.kind} {resource.identity}: {error}")
        return self.errors

    def __enter__(self):
        self._token = _active.set(self)
        return self

    def __exit__(self, *exc):
        try:
            self.rollback()
        finally:
            assert self._token is not None
            _active.reset(self._token)


def current_setup() -> WorkspaceSetup | None:
    return _active.get()


def commit_setup() -> None:
    setup = current_setup()
    if setup is not None:
        setup.commit()


def own_file(path: Path, provenance: str):
    """Reserve an absent setup file before its writer can fail halfway."""
    setup = current_setup()
    if setup is not None and not path.exists():
        setup.record("file", path, provenance, lambda: path.unlink(missing_ok=True))
