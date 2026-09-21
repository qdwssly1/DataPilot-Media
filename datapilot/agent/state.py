"""Explicit state model for the future DataPilot agent graph.

The state deliberately contains more than a message list. Later graph nodes
will update task planning, SQL execution, review, and answer fields without
having to hide workflow state inside chat messages.
"""

from __future__ import annotations

from copy import deepcopy
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
    metric_binding: dict[str, Any] = field(default_factory=dict)
    requested_dimensions: list[str] = field(default_factory=list)
    window_role_binding: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BusinessContext:
    """Business rules and definitions selected for the current question."""

    rules: list[str] = field(default_factory=list)
    definitions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SQLResult:
    """Bounded query evidence produced by SQL or an approved read-only tool."""

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
    execution_source: Literal["sql", "tool"] = "sql"
    tool_name: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_metadata: dict[str, Any] = field(default_factory=dict)


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


@dataclass(slots=True)
class AnalysisResult:
    """Grounded result produced for one analysis task."""

    task_id: str
    summary: str
    findings: list[str] = field(default_factory=list)
    derived_values: dict[str, Any] = field(default_factory=dict)
    source_task_ids: list[str] = field(default_factory=list)
    success: bool = True
    error: str | None = None
    retry_count: int = 0


@dataclass(slots=True)
class FinalAnswerResult:
    """Structured final response grounded in completed task outputs."""

    task_id: str
    answer: str
    key_findings: list[str] = field(default_factory=list)
    source_task_ids: list[str] = field(default_factory=list)
    success: bool = True
    error: str | None = None
    retry_count: int = 0
    evidence_pack: dict[str, Any] = field(default_factory=dict)
    evidence_projection: dict[str, Any] = field(default_factory=dict)
    evidence_projection_stats: dict[str, Any] = field(default_factory=dict)
    claims: list[dict[str, Any]] = field(default_factory=list)
    validator_result: dict[str, Any] = field(default_factory=dict)


# Phase 4 exposed this name; keep it as a compatibility alias.
ReviewResult = ReviewerResult


@dataclass(slots=True)
class TimeRangeContext:
    """Small structured representation of the active business time range."""

    labels: list[str] = field(default_factory=list)
    start: str | None = None
    end: str | None = None


@dataclass(slots=True)
class SessionContext:
    """Compact authoritative business context for one successful session."""

    session_id: str = field(default_factory=lambda: str(uuid4()))
    turn_index: int = 0
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    time_range: TimeRangeContext = field(default_factory=TimeRangeContext)
    filters: dict[str, list[str]] = field(default_factory=dict)
    entities: dict[str, list[str]] = field(default_factory=dict)
    analysis_goal: str | None = None
    last_user_query: str | None = None
    last_resolved_query: str | None = None
    last_intent: str | None = None
    last_answer_summary: str | None = None
    last_source_task_ids: list[str] = field(default_factory=list)
    updated_at: str | None = None

    @property
    def turn_number(self) -> int:
        """Compatibility name retained for the Phase 2 public state shape."""

        return self.turn_index

    @property
    def has_business_context(self) -> bool:
        """Return whether the session has verified semantic slots to inherit."""

        return bool(
            self.metrics
            or self.dimensions
            or self.time_range.labels
            or self.time_range.start
            or self.time_range.end
            or self.filters
            or self.entities
            or self.analysis_goal
        )


class AgentState(TypedDict):
    """Complete, explicit state passed between future DataPilot graph nodes."""

    messages: list[Message]
    original_query: str
    resolved_query: str | None
    was_follow_up: bool
    follow_up_resolution: Any | None
    current_task: TaskItem | None
    task_plan: list[TaskItem]
    completed_tasks: list[TaskItem]
    pending_tasks: list[TaskItem]
    relevant_tables: list[str]
    business_context: BusinessContext
    knowledge_evidence: list[dict[str, Any]]
    knowledge_retrieval_error: str | None
    knowledge_retrieval_latency_ms: float
    tool_routes: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    tool_fallback_count: int
    generated_sql: list[str]
    sql_results: list[SQLResult]
    retry_count: int
    review_result: ReviewerResult | None
    review_results: list[ReviewerResult]
    analysis_results: list[AnalysisResult]
    final_answer: str | None
    final_answer_result: FinalAnswerResult | None
    final_evidence_pack: dict[str, Any]
    final_evidence_projection: dict[str, Any]
    final_evidence_projection_stats: dict[str, Any]
    final_claims: list[dict[str, Any]]
    final_validator_result: dict[str, Any]
    session_context: SessionContext
    trace_id: str


def create_initial_state(
    user_query: str,
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
    previous_session: SessionContext | None = None,
) -> AgentState:
    """Create isolated state for one user query.

    Every mutable field is constructed for this call. No list, dictionary, or
    nested dataclass instance is shared with another state.
    """

    query = user_query.strip()
    if not query:
        raise ValueError("user_query must not be empty")
    if previous_session is not None:
        if session_id is not None and session_id != previous_session.session_id:
            raise ValueError("session_id must match previous_session")
        session_context = deepcopy(previous_session)
        session_context.turn_index += 1
    else:
        session_context = SessionContext(
            session_id=session_id or str(uuid4()),
            turn_index=1,
        )

    return AgentState(
        messages=[Message(role="user", content=query)],
        original_query=query,
        resolved_query=None,
        was_follow_up=False,
        follow_up_resolution=None,
        current_task=None,
        task_plan=[],
        completed_tasks=[],
        pending_tasks=[],
        relevant_tables=[],
        business_context=BusinessContext(),
        knowledge_evidence=[],
        knowledge_retrieval_error=None,
        knowledge_retrieval_latency_ms=0.0,
        tool_routes=[],
        tool_results=[],
        tool_fallback_count=0,
        generated_sql=[],
        sql_results=[],
        retry_count=0,
        review_result=None,
        review_results=[],
        analysis_results=[],
        final_answer=None,
        final_answer_result=None,
        final_evidence_pack={},
        final_evidence_projection={},
        final_evidence_projection_stats={},
        final_claims=[],
        final_validator_result={},
        session_context=session_context,
        trace_id=trace_id or str(uuid4()),
    )


def get_effective_query(state: AgentState) -> str:
    """Return the standalone resolved query, or the user's original input."""

    return state["resolved_query"] or state["original_query"]
