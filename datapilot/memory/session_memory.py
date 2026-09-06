"""Small in-memory store for authoritative DataPilot session context."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from datapilot.agent.state import SessionContext


class SessionMemoryStore:
    """Isolated, copy-on-read in-memory session storage."""

    def __init__(self) -> None:
        self._contexts: dict[str, SessionContext] = {}

    def create(self, session_id: str | None = None) -> SessionContext:
        """Create an empty session or return an isolated copy if it exists."""

        identity = (session_id or str(uuid4())).strip()
        if not identity:
            raise ValueError("session_id must not be empty")
        existing = self._contexts.get(identity)
        if existing is not None:
            return deepcopy(existing)
        context = SessionContext(session_id=identity, turn_index=0)
        self._contexts[identity] = deepcopy(context)
        return context

    def get(self, session_id: str) -> SessionContext | None:
        """Return an isolated context copy for one session."""

        context = self._contexts.get(session_id)
        return deepcopy(context) if context is not None else None

    def save(self, context: SessionContext) -> None:
        """Replace one session atomically with a defensive copy."""

        if not context.session_id.strip():
            raise ValueError("session_id must not be empty")
        self._contexts[context.session_id] = deepcopy(context)

    def clear(self, session_id: str) -> bool:
        """Remove one session and report whether it existed."""

        return self._contexts.pop(session_id, None) is not None

    def __len__(self) -> int:
        return len(self._contexts)
