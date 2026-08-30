"""Explicit state model for the future DataPilot agent graph.

The state deliberately contains more than a message list. Later graph nodes
will update task planning, SQL execution, review, and answer fields without
having to hide workflow state inside chat messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict
from uuid import uuid4

MessageRole = Literal["user", "assistant", "system", "tool"]
TaskStatus = Literal["pending", "in_progress", "completed", "failed"]


@dataclass(slots=True)
class Message:
    """A dependency-free message used until LangGraph wiring is introduced."""

    role: MessageRole
    content: str


@dataclass(slots=True)
class TaskItem:
    """One explicit unit of work in a future multi-step task plan."""

    task_id: str
    description: str
    status: TaskStatus = "pending"


@dataclass(slots=True)
class BusinessContext:
    """Business rules and definitions selected for the current question."""

    rules: list[str] = field(default_factory=list)
    definitions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SQLResult:
    """Structured result produced by a future SQL execution step."""

    task_id: str
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class ReviewResult:
    """Structured decision produced by a future reviewer node."""

    approved: bool
    summary: str
    issues: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SessionContext:
    """Session identity only; conversational session memory is not implemented."""

    session_id: str = field(default_factory=lambda: str(uuid4()))
    turn_number: int = 1


class AgentState(TypedDict):
    """Complete, explicit state passed between future DataPilot graph nodes."""

    messages: list[Message]
    original_query: str
    current_task: TaskItem | None
    task_plan: list[TaskItem]
    completed_tasks: list[TaskItem]
    pending_tasks: list[TaskItem]
    relevant_tables: list[str]
    business_context: BusinessContext
    generated_sql: list[str]
    sql_results: list[SQLResult]
    retry_count: int
    review_result: ReviewResult | None
    final_answer: str | None
    session_context: SessionContext
    trace_id: str


def create_initial_state(
    user_query: str,
    *,
    trace_id: str | None = None,
) -> AgentState:
    """Create isolated state for one user query.

    Every mutable field is constructed for this call. No list, dictionary, or
    nested dataclass instance is shared with another state.
    """

    query = user_query.strip()
    if not query:
        raise ValueError("user_query must not be empty")

    return AgentState(
        messages=[Message(role="user", content=query)],
        original_query=query,
        current_task=None,
        task_plan=[],
        completed_tasks=[],
        pending_tasks=[],
        relevant_tables=[],
        business_context=BusinessContext(),
        generated_sql=[],
        sql_results=[],
        retry_count=0,
        review_result=None,
        final_answer=None,
        session_context=SessionContext(),
        trace_id=trace_id or str(uuid4()),
    )
