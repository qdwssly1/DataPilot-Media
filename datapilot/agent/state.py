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
TaskStatus = Literal[
    "pending",
    "in_progress",
    "executing",
    "executed",
    "reviewing",
    "completed",
    "failed",
]
TaskType = Literal["query", "analysis", "response"]
ReviewDecision = Literal["approve", "retry", "fail"]
ReviewIssueType = Literal[
    "metric_mismatch",
    "dimension_mismatch",
    "time_range_mismatch",
    "filter_mismatch",
    "aggregation_mismatch",
    "join_mismatch",
    "missing_data",
    "result_mismatch",
    "other",
]


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
    task_type: TaskType = "analysis"
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = "pending"


@dataclass(slots=True)
class BusinessContext:
    """Business rules and definitions selected for the current question."""

    rules: list[str] = field(default_factory=list)
    definitions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SQLResult:
    """Bounded, structured result produced by the SQL Agent."""

    task_id: str
    sql: str
    success: bool = True
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    error: str | None = None
    retry_count: int = 0
    semantic_retry_count: int = 0
    execution_time: float = 0.0
    context_summary: str = ""


@dataclass(slots=True)
class ReviewerIssue:
    """One bounded semantic problem identified by the Reviewer."""

    issue_type: ReviewIssueType
    description: str


@dataclass(slots=True)
class ReviewerResult:
    """Strict semantic decision for one task and one SQL result."""

    task_id: str
    decision: ReviewDecision
    reason_summary: str
    issues: list[ReviewerIssue] = field(default_factory=list)
    retry_instruction: str | None = None
    confidence: float = 0.0
    review_retry_count: int = 0


# Phase 4 exposed this name; keep it as a compatibility alias.
ReviewResult = ReviewerResult


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
    review_result: ReviewerResult | None
    review_results: list[ReviewerResult]
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
        review_results=[],
        final_answer=None,
        session_context=SessionContext(),
        trace_id=trace_id or str(uuid4()),
    )
