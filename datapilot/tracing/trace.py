"""Minimal in-memory tracing without hidden chain-of-thought.

Events should contain observable inputs, outputs, actions, and summaries only.
They must never contain private model reasoning or hidden chain-of-thought.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class EventType(StrEnum):
    """Observable event types supported by the DataPilot trace model."""

    USER_QUERY = "USER_QUERY"
    STATE_CREATED = "STATE_CREATED"
    PLANNER = "PLANNER"
    PLANNER_STARTED = "PLANNER_STARTED"
    PLANNER_RESULT = "PLANNER_RESULT"
    PLANNER_RETRY = "PLANNER_RETRY"
    PLANNER_FAILED = "PLANNER_FAILED"
    SQL_AGENT_STARTED = "SQL_AGENT_STARTED"
    CONTEXT_FETCHED = "CONTEXT_FETCHED"
    SQL_MEMORY_RECALLED = "SQL_MEMORY_RECALLED"
    SQL_GENERATED = "SQL_GENERATED"
    SQL_SAFETY_REJECTED = "SQL_SAFETY_REJECTED"
    DRY_PLAN_STARTED = "DRY_PLAN_STARTED"
    DRY_PLAN_SUCCEEDED = "DRY_PLAN_SUCCEEDED"
    DRY_PLAN_FAILED = "DRY_PLAN_FAILED"
    SQL_EXECUTION_STARTED = "SQL_EXECUTION_STARTED"
    SQL_EXECUTION_SUCCEEDED = "SQL_EXECUTION_SUCCEEDED"
    SQL_EXECUTION_FAILED = "SQL_EXECUTION_FAILED"
    SQL_RETRY = "SQL_RETRY"
    SQL_AGENT_COMPLETED = "SQL_AGENT_COMPLETED"
    SQL_AGENT_FAILED = "SQL_AGENT_FAILED"
    REVIEW_STARTED = "REVIEW_STARTED"
    REVIEW_RESULT = "REVIEW_RESULT"
    REVIEW_OUTPUT_RETRY = "REVIEW_OUTPUT_RETRY"
    REVIEW_APPROVED = "REVIEW_APPROVED"
    REVIEW_RETRY_REQUESTED = "REVIEW_RETRY_REQUESTED"
    REVIEW_FAILED = "REVIEW_FAILED"
    SEMANTIC_RETRY_STARTED = "SEMANTIC_RETRY_STARTED"
    SEMANTIC_RETRY_COMPLETED = "SEMANTIC_RETRY_COMPLETED"
    SQL_MEMORY_STORE_STARTED = "SQL_MEMORY_STORE_STARTED"
    SQL_MEMORY_STORE_SUCCEEDED = "SQL_MEMORY_STORE_SUCCEEDED"
    SQL_MEMORY_STORE_FAILED = "SQL_MEMORY_STORE_FAILED"
    ANALYST_STARTED = "ANALYST_STARTED"
    ANALYST_INPUT_PREPARED = "ANALYST_INPUT_PREPARED"
    ANALYST_CALCULATION_COMPLETED = "ANALYST_CALCULATION_COMPLETED"
    ANALYST_RESULT = "ANALYST_RESULT"
    ANALYST_OUTPUT_RETRY = "ANALYST_OUTPUT_RETRY"
    ANALYST_COMPLETED = "ANALYST_COMPLETED"
    ANALYST_FAILED = "ANALYST_FAILED"
    FINAL_ANSWER_STARTED = "FINAL_ANSWER_STARTED"
    FINAL_ANSWER_COMPLETED = "FINAL_ANSWER_COMPLETED"
    FINAL_ANSWER_FAILED = "FINAL_ANSWER_FAILED"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    SQL = "SQL"
    REVIEW = "REVIEW"
    RETRY = "RETRY"
    FINAL_ANSWER = "FINAL_ANSWER"


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One observable event emitted by a DataPilot component."""

    trace_id: str
    timestamp: datetime
    event_type: EventType
    component: str
    action: str
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)


class TraceCollector:
    """Append-only, in-memory collector for one trace identifier."""

    def __init__(self, trace_id: str | None = None) -> None:
        self.trace_id = trace_id or str(uuid4())
        if not self.trace_id.strip():
            raise ValueError("trace_id must not be empty")
        self._events: list[TraceEvent] = []

    def add_event(
        self,
        event_type: EventType | str,
        *,
        component: str,
        action: str,
        summary: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> TraceEvent:
        """Append and return an observable event."""

        event = TraceEvent(
            trace_id=self.trace_id,
            timestamp=datetime.now(UTC),
            event_type=EventType(event_type),
            component=component,
            action=action,
            summary=summary,
            metadata=dict(metadata or {}),
        )
        self._events.append(event)
        return event

    def get_events(self) -> list[TraceEvent]:
        """Return events in insertion order without exposing the internal list."""

        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)
