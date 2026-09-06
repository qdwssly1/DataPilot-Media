"""Semantic review for executed DataPilot query tasks.

The Reviewer checks whether an executed SQL result can satisfy its Planner
task. SQL parsing, planning, and execution remain responsibilities of the SQL
Agent and Wren.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict
from time import perf_counter
from typing import Any, Protocol

from datapilot.agent.state import (
    AgentState,
    ReviewerIssue,
    ReviewerResult,
    SQLResult,
    TaskItem,
    get_effective_query,
)
from datapilot.tracing.trace import EventType, TraceCollector


class ReviewerModel(Protocol):
    """Small dependency-injection boundary for real and fake review models."""

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        """Return one complete structured review as text."""


class ReviewerError(RuntimeError):
    """Reviewer failure with a safe stage, task identifier, and summary."""

    def __init__(self, *, stage: str, task_id: str, summary: str) -> None:
        self.stage = stage
        self.task_id = task_id
        self.summary = summary
        super().__init__(f"{stage} failed for task {task_id}: {summary}")


class ReviewerOutputError(ReviewerError):
    """Raised when structured model output violates the review contract."""


class ReviewerRetryLimitError(ReviewerError):
    """Raised by orchestration when semantic correction is already exhausted."""


REVIEWER_SYSTEM_PROMPT = """You are DataPilot's Semantic Reviewer.
Decide whether the SQL and its execution result are sufficient for the current task.
Check metric, dimensions, time range, filters, aggregation, joins, result relevance,
and task completeness. You are not the SQL Agent or final analyst.
Review ONLY the current task contract. Planner task decomposition is authoritative.
Do not ask the SQL Agent to duplicate or absorb sibling tasks. If another Planner
task owns another period, dimension, or analysis step, do not broaden the current
SQL query to include it. Every retry_instruction must preserve current task scope.
Do not execute SQL, answer the user, invent data, or reveal hidden reasoning.
Return exactly one JSON object matching the supplied schema.
Use approve only when the result supports the task. Use retry only when one corrected
SQL query could fix the listed issues. Use fail when correction cannot safely help.
Keep summaries and retry instructions short and directly actionable.
"""

ISSUE_TYPES = (
    "metric_mismatch",
    "dimension_mismatch",
    "time_range_mismatch",
    "filter_mismatch",
    "aggregation_mismatch",
    "join_mismatch",
    "missing_data",
    "result_mismatch",
    "other",
)

REVIEWER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "decision",
        "reason_summary",
        "issues",
        "retry_instruction",
        "confidence",
    ],
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "retry", "fail"]},
        "reason_summary": {"type": "string", "minLength": 1, "maxLength": 240},
        "issues": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["issue_type", "description"],
                "properties": {
                    "issue_type": {"type": "string", "enum": list(ISSUE_TYPES)},
                    "description": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 240,
                    },
                },
            },
        },
        "retry_instruction": {"type": ["string", "null"], "maxLength": 400},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

_TOP_LEVEL_FIELDS = frozenset(REVIEWER_RESPONSE_SCHEMA["required"])
_ISSUE_FIELDS = frozenset({"issue_type", "description"})


def _safe_error_summary(error: BaseException) -> str:
    """Bound an error and redact common secret-bearing forms and URLs."""

    text = " ".join(str(error).split()) or type(error).__name__
    text = re.sub(
        r"(?i)(api[_ -]?key|password|token|authorization)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"[a-z][a-z0-9+.-]*://\S+", "[REDACTED_URL]", text)
    return text[:300]


def _compact_json(value: Any, *, max_chars: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."


def _output_error(task_id: str, summary: str) -> ReviewerOutputError:
    return ReviewerOutputError(
        stage="structured_output",
        task_id=task_id,
        summary=summary,
    )


def _parse_result(
    response_text: str,
    *,
    task_id: str,
    review_retry_count: int,
) -> ReviewerResult:
    """Parse and strictly validate one complete Reviewer JSON document."""

    try:
        raw = json.loads(response_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _output_error(task_id, "review response is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise _output_error(task_id, "review response must be a JSON object")
    if frozenset(raw) != _TOP_LEVEL_FIELDS:
        missing = sorted(_TOP_LEVEL_FIELDS - frozenset(raw))
        extra = sorted(frozenset(raw) - _TOP_LEVEL_FIELDS)
        raise _output_error(
            task_id,
            f"review fields invalid; missing={missing}, extra={extra}",
        )

    decision = raw["decision"]
    if decision not in {"approve", "retry", "fail"}:
        raise _output_error(task_id, "decision is invalid")
    reason_summary = raw["reason_summary"]
    if not isinstance(reason_summary, str) or not reason_summary.strip():
        raise _output_error(task_id, "reason_summary must not be empty")
    reason_summary = reason_summary.strip()
    if len(reason_summary) > 240:
        raise _output_error(task_id, "reason_summary exceeds 240 characters")

    raw_issues = raw["issues"]
    if not isinstance(raw_issues, list) or len(raw_issues) > 10:
        raise _output_error(task_id, "issues must be a list with at most 10 items")
    issues: list[ReviewerIssue] = []
    for index, raw_issue in enumerate(raw_issues):
        if not isinstance(raw_issue, dict):
            raise _output_error(task_id, f"issues[{index}] must be an object")
        if frozenset(raw_issue) != _ISSUE_FIELDS:
            raise _output_error(task_id, f"issues[{index}] fields are invalid")
        issue_type = raw_issue["issue_type"]
        description = raw_issue["description"]
        if issue_type not in ISSUE_TYPES:
            raise _output_error(task_id, f"issues[{index}].issue_type is invalid")
        if not isinstance(description, str) or not description.strip():
            raise _output_error(task_id, f"issues[{index}].description is empty")
        description = description.strip()
        if len(description) > 240:
            raise _output_error(
                task_id,
                f"issues[{index}].description exceeds 240 characters",
            )
        issues.append(
            ReviewerIssue(issue_type=issue_type, description=description)
        )

    retry_instruction = raw["retry_instruction"]
    if retry_instruction is not None:
        if not isinstance(retry_instruction, str) or not retry_instruction.strip():
            raise _output_error(task_id, "retry_instruction must be null or non-empty")
        retry_instruction = retry_instruction.strip()
        if len(retry_instruction) > 400:
            raise _output_error(task_id, "retry_instruction exceeds 400 characters")

    confidence = raw["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0 <= float(confidence) <= 1
    ):
        raise _output_error(task_id, "confidence must be between 0 and 1")

    if decision == "approve" and (issues or retry_instruction is not None):
        raise _output_error(
            task_id,
            "approve requires empty issues and null retry_instruction",
        )
    if decision == "retry" and (not issues or retry_instruction is None):
        raise _output_error(
            task_id,
            "retry requires issues and a retry_instruction",
        )
    if decision == "fail" and (not issues or retry_instruction is not None):
        raise _output_error(
            task_id,
            "fail requires issues and null retry_instruction",
        )

    return ReviewerResult(
        task_id=task_id,
        decision=decision,
        reason_summary=reason_summary,
        issues=issues,
        retry_instruction=retry_instruction,
        confidence=float(confidence),
        review_retry_count=review_retry_count,
    )


def _next_ready_task(state: AgentState) -> TaskItem | None:
    completed_ids = {
        item.task_id for item in state["completed_tasks"] if item.status == "completed"
    }
    pending_ids = {item.task_id for item in state["pending_tasks"]}
    return next(
        (
            task
            for task in state["task_plan"]
            if task.task_id in pending_ids
            and task.status == "pending"
            and set(task.depends_on) <= completed_ids
        ),
        None,
    )


class Reviewer:
    """Produce and apply one bounded semantic decision for an executed query."""

    def __init__(
        self,
        *,
        model_client: ReviewerModel,
        trace: TraceCollector | None = None,
        max_output_retries: int = 1,
        sample_row_limit: int = 5,
    ) -> None:
        if max_output_retries not in {0, 1}:
            raise ValueError("max_output_retries must be 0 or 1")
        if not 1 <= sample_row_limit <= 10:
            raise ValueError("sample_row_limit must be between 1 and 10")
        self.model_client = model_client
        self.trace = trace
        self.max_output_retries = max_output_retries
        self.sample_row_limit = sample_row_limit

    def review(
        self,
        state: AgentState,
        task: TaskItem,
        result: SQLResult,
        *,
        trace: TraceCollector | None = None,
        review_retry_count: int = 0,
        previous_feedback: ReviewerResult | None = None,
    ) -> ReviewerResult:
        """Review one SQL result, update review state, and apply its decision."""

        active_trace = trace if trace is not None else self.trace
        if active_trace is None:
            raise ValueError("a TraceCollector is required")
        if active_trace.trace_id != state["trace_id"]:
            raise ValueError("trace and state must use the same trace_id")
        if result.task_id != task.task_id:
            raise ReviewerError(
                stage="input",
                task_id=task.task_id,
                summary="SQL result belongs to a different task",
            )
        if result.success and task.status != "executed":
            raise ReviewerError(
                stage="input",
                task_id=task.task_id,
                summary="successful SQL must be in executed status before review",
            )

        started_at = perf_counter()
        task.status = "reviewing"
        self._add_event(
            active_trace,
            EventType.REVIEW_STARTED,
            task_id=task.task_id,
            duration=0.0,
            review_retry_count=review_retry_count,
        )

        if not result.success:
            review = ReviewerResult(
                task_id=task.task_id,
                decision="fail",
                reason_summary="SQL execution did not produce a reviewable result.",
                issues=[
                    ReviewerIssue(
                        issue_type="result_mismatch",
                        description=(
                            result.error or "SQL execution failed."
                        )[:240],
                    )
                ],
                confidence=1.0,
                review_retry_count=review_retry_count,
            )
            self._apply_review(state, task, review)
            self._record_result(active_trace, review, started_at=started_at)
            return review

        prompt = self._build_prompt(
            state,
            task,
            result,
            previous_feedback=previous_feedback,
        )
        last_output_error: ReviewerOutputError | None = None
        for output_attempt in range(self.max_output_retries + 1):
            retry_suffix = ""
            if last_output_error is not None:
                retry_suffix = (
                    "\n\nStructured output correction:\n"
                    f"Error: {last_output_error.summary}\n"
                    "Return a corrected JSON object only."
                )
            try:
                response = self.model_client.complete(
                    system_prompt=REVIEWER_SYSTEM_PROMPT,
                    user_prompt=f"{prompt}{retry_suffix}",
                    response_schema=REVIEWER_RESPONSE_SCHEMA,
                )
            except Exception as exc:
                error = ReviewerError(
                    stage="model_call",
                    task_id=task.task_id,
                    summary=_safe_error_summary(exc),
                )
                self._mark_failed(state, task)
                self._add_event(
                    active_trace,
                    EventType.REVIEW_FAILED,
                    task_id=task.task_id,
                    duration=perf_counter() - started_at,
                    review_retry_count=review_retry_count,
                    error_type=type(error).__name__,
                )
                raise error from exc

            try:
                review = _parse_result(
                    response,
                    task_id=task.task_id,
                    review_retry_count=review_retry_count,
                )
            except ReviewerOutputError as exc:
                last_output_error = exc
                if output_attempt < self.max_output_retries:
                    self._add_event(
                        active_trace,
                        EventType.REVIEW_OUTPUT_RETRY,
                        task_id=task.task_id,
                        duration=perf_counter() - started_at,
                        review_retry_count=review_retry_count,
                        error_type=type(exc).__name__,
                    )
                    continue
                self._mark_failed(state, task)
                self._add_event(
                    active_trace,
                    EventType.REVIEW_FAILED,
                    task_id=task.task_id,
                    duration=perf_counter() - started_at,
                    review_retry_count=review_retry_count,
                    error_type=type(exc).__name__,
                )
                raise exc

            self._apply_review(state, task, review)
            self._record_result(active_trace, review, started_at=started_at)
            return review

        raise AssertionError("Reviewer output retry loop exited unexpectedly")

    def _build_prompt(
        self,
        state: AgentState,
        task: TaskItem,
        result: SQLResult,
        *,
        previous_feedback: ReviewerResult | None,
    ) -> str:
        context = state["business_context"].definitions.get(
            f"wren_context:{task.task_id}",
            result.context_summary,
        )
        previous = asdict(previous_feedback) if previous_feedback else None
        current_contract = {
            "task_id": task.task_id,
            "description": task.description,
            "task_type": task.task_type,
            "depends_on": task.depends_on,
        }
        task_boundaries = [
            {
                "task_id": planned.task_id,
                "description": planned.description,
                "task_type": planned.task_type,
            }
            for planned in state["task_plan"]
        ]
        return (
            f"Effective user query:\n{get_effective_query(state)}\n\n"
            "Current task contract (authoritative):\n"
            f"{_compact_json(current_contract, max_chars=2000)}\n\n"
            "Task plan boundaries:\n"
            f"{_compact_json(task_boundaries, max_chars=4000)}\n"
            "Sibling tasks define scope boundaries only; never merge them into "
            "the current task.\n\n"
            f"SQL:\n{result.sql}\n\n"
            "Execution metadata:\n"
            f"columns={_compact_json(result.columns, max_chars=1000)}\n"
            f"row_count={result.row_count}\n"
            "sample_rows="
            f"{_compact_json(result.rows[: self.sample_row_limit], max_chars=4000)}"
            "\n\nWren context:\n"
            f"{str(context)[:4000]}\n\n"
            "Previous reviewer feedback:\n"
            f"{_compact_json(previous, max_chars=2000)}"
        )

    @staticmethod
    def _apply_review(
        state: AgentState,
        task: TaskItem,
        review: ReviewerResult,
    ) -> None:
        state["review_result"] = review
        state["review_results"].append(review)
        if review.decision == "approve":
            task.status = "completed"
            if all(
                item.task_id != task.task_id for item in state["completed_tasks"]
            ):
                state["completed_tasks"].append(task)
            state["pending_tasks"] = [
                item
                for item in state["pending_tasks"]
                if item.task_id != task.task_id
            ]
            state["current_task"] = _next_ready_task(state)
            return
        if review.decision == "retry":
            task.status = "pending"
            state["current_task"] = task
            return
        Reviewer._mark_failed(state, task)

    @staticmethod
    def _mark_failed(state: AgentState, task: TaskItem) -> None:
        task.status = "failed"
        state["pending_tasks"] = [
            item for item in state["pending_tasks"] if item.task_id != task.task_id
        ]
        state["current_task"] = task

    def _record_result(
        self,
        trace: TraceCollector,
        review: ReviewerResult,
        *,
        started_at: float,
    ) -> None:
        duration = perf_counter() - started_at
        self._add_event(
            trace,
            EventType.REVIEW_RESULT,
            task_id=review.task_id,
            duration=duration,
            review_retry_count=review.review_retry_count,
            decision=review.decision,
            issue_types=[item.issue_type for item in review.issues],
            confidence=review.confidence,
        )
        event_type = {
            "approve": EventType.REVIEW_APPROVED,
            "retry": EventType.REVIEW_RETRY_REQUESTED,
            "fail": EventType.REVIEW_FAILED,
        }[review.decision]
        self._add_event(
            trace,
            event_type,
            task_id=review.task_id,
            duration=duration,
            review_retry_count=review.review_retry_count,
            decision=review.decision,
            issue_types=[item.issue_type for item in review.issues],
            confidence=review.confidence,
        )

    @staticmethod
    def _add_event(
        trace: TraceCollector,
        event_type: EventType,
        *,
        task_id: str,
        duration: float,
        review_retry_count: int,
        decision: str | None = None,
        issue_types: list[str] | None = None,
        confidence: float | None = None,
        error_type: str | None = None,
    ) -> None:
        metadata: dict[str, Any] = {
            "task_id": task_id,
            "review_retry_count": review_retry_count,
            "duration": duration,
        }
        if decision is not None:
            metadata["decision"] = decision
        if issue_types is not None:
            metadata["issue_types"] = issue_types
        if confidence is not None:
            metadata["confidence"] = confidence
        if error_type is not None:
            metadata["error_type"] = error_type
        trace.add_event(
            event_type,
            component="reviewer",
            action=event_type.value.lower(),
            summary=f"Reviewer event: {event_type.value}.",
            metadata=metadata,
        )
