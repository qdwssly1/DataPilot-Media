"""Structured Planner for the DataPilot agent.

The Planner classifies a user question and decomposes it into typed tasks. It
does not generate SQL, execute tools, inspect a database, or answer the user.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from time import perf_counter
from typing import Any, Protocol

from datapilot.agent.state import AgentState, TaskItem
from datapilot.tracing.trace import EventType, TraceCollector


class PlannerIntent(StrEnum):
    """Supported high-level routes for one user question."""

    SIMPLE_QUESTION = "simple_question"
    SINGLE_QUERY = "single_query"
    MULTI_STEP_ANALYSIS = "multi_step_analysis"
    FOLLOW_UP = "follow_up"


@dataclass(frozen=True, slots=True)
class PlannerResult:
    """Validated, structured output returned by the Planner."""

    intent: PlannerIntent
    reason_summary: str
    tasks: tuple[TaskItem, ...]
    requires_database: bool
    requires_context: bool
    is_follow_up: bool


class PlannerError(RuntimeError):
    """Base error raised when planning cannot complete safely."""


class PlannerOutputError(PlannerError):
    """Raised when a model response violates the planner schema."""


class PlannerConfigurationError(PlannerError):
    """Raised when an LLM client cannot be configured."""


class PlannerModel(Protocol):
    """Small dependency-injection boundary used by real and fake clients."""

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        """Return one complete JSON response as text."""


SYSTEM_PROMPT = """You are DataPilot's task planner.
Only classify the request, decompose data work into tasks, and define dependencies.
Do not generate SQL, execute SQL, call tools, guess data, or answer the question.
Return one JSON object that exactly matches the provided schema.
Keep reason_summary short and state only the observable decision reason.
All tasks must start with status 'pending' and may depend only on earlier tasks.
For a database question, end the plan with a response task grounded in completed
query or analysis tasks. For a comparison, use separate query tasks when needed,
then an analysis task for calculations, then a response task. Never put SQL in a
task description.
"""

PLANNER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "intent",
        "reason_summary",
        "tasks",
        "requires_database",
        "requires_context",
        "is_follow_up",
    ],
    "properties": {
        "intent": {
            "type": "string",
            "enum": [intent.value for intent in PlannerIntent],
        },
        "reason_summary": {"type": "string", "minLength": 1, "maxLength": 240},
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "task_id",
                    "description",
                    "task_type",
                    "depends_on",
                    "status",
                ],
                "properties": {
                    "task_id": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                    "task_type": {
                        "type": "string",
                        "enum": ["query", "analysis", "response"],
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "status": {"type": "string", "enum": ["pending"]},
                },
            },
        },
        "requires_database": {"type": "boolean"},
        "requires_context": {"type": "boolean"},
        "is_follow_up": {"type": "boolean"},
    },
}

_TOP_LEVEL_FIELDS = frozenset(PLANNER_RESPONSE_SCHEMA["required"])
_TASK_FIELDS = frozenset(
    PLANNER_RESPONSE_SCHEMA["properties"]["tasks"]["items"]["required"]
)


def _require_exact_fields(
    value: Mapping[str, Any],
    expected: frozenset[str],
    *,
    location: str,
) -> None:
    actual = frozenset(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    raise PlannerOutputError(
        f"{location} fields are invalid; missing={missing}, extra={extra}"
    )


def _require_bool(value: Any, *, field_name: str) -> bool:
    if type(value) is not bool:
        raise PlannerOutputError(f"{field_name} must be a boolean")
    return value


def _parse_task(raw_task: Any, *, known_ids: set[str], index: int) -> TaskItem:
    if not isinstance(raw_task, dict):
        raise PlannerOutputError(f"tasks[{index}] must be an object")
    _require_exact_fields(raw_task, _TASK_FIELDS, location=f"tasks[{index}]")

    task_id = raw_task["task_id"]
    description = raw_task["description"]
    task_type = raw_task["task_type"]
    depends_on = raw_task["depends_on"]

    if not isinstance(task_id, str) or not task_id.strip():
        raise PlannerOutputError(f"tasks[{index}].task_id must not be empty")
    if task_id in known_ids:
        raise PlannerOutputError(f"duplicate task_id: {task_id}")
    if not isinstance(description, str) or not description.strip():
        raise PlannerOutputError(f"tasks[{index}].description must not be empty")
    if not isinstance(task_type, str) or task_type not in {
        "query",
        "analysis",
        "response",
    }:
        raise PlannerOutputError(f"tasks[{index}].task_type is invalid")
    if raw_task["status"] != "pending":
        raise PlannerOutputError(f"tasks[{index}].status must be pending")
    if not isinstance(depends_on, list) or not all(
        isinstance(dependency, str) for dependency in depends_on
    ):
        raise PlannerOutputError(f"tasks[{index}].depends_on must be a string list")
    if len(depends_on) != len(set(depends_on)):
        raise PlannerOutputError(f"tasks[{index}].depends_on contains duplicates")
    unknown_dependencies = sorted(set(depends_on) - known_ids)
    if unknown_dependencies:
        raise PlannerOutputError(
            f"tasks[{index}] has unknown or forward dependencies: "
            f"{unknown_dependencies}"
        )

    return TaskItem(
        task_id=task_id,
        description=description.strip(),
        task_type=task_type,
        depends_on=list(depends_on),
        status="pending",
    )


def parse_planner_response(response_text: str) -> PlannerResult:
    """Parse one complete JSON document and validate the planning contract."""

    try:
        raw_result = json.loads(response_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PlannerOutputError("planner response is not valid JSON") from exc

    if not isinstance(raw_result, dict):
        raise PlannerOutputError("planner response must be a JSON object")
    _require_exact_fields(raw_result, _TOP_LEVEL_FIELDS, location="planner result")

    try:
        intent = PlannerIntent(raw_result["intent"])
    except (TypeError, ValueError) as exc:
        raise PlannerOutputError("intent is invalid") from exc

    reason_summary = raw_result["reason_summary"]
    if not isinstance(reason_summary, str) or not reason_summary.strip():
        raise PlannerOutputError("reason_summary must not be empty")
    reason_summary = reason_summary.strip()
    if len(reason_summary) > 240:
        raise PlannerOutputError("reason_summary must be at most 240 characters")

    raw_tasks = raw_result["tasks"]
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlannerOutputError("tasks must be a non-empty list")

    tasks: list[TaskItem] = []
    known_ids: set[str] = set()
    for index, raw_task in enumerate(raw_tasks):
        task = _parse_task(raw_task, known_ids=known_ids, index=index)
        tasks.append(task)
        known_ids.add(task.task_id)

    requires_database = _require_bool(
        raw_result["requires_database"], field_name="requires_database"
    )
    requires_context = _require_bool(
        raw_result["requires_context"], field_name="requires_context"
    )
    is_follow_up = _require_bool(raw_result["is_follow_up"], field_name="is_follow_up")

    if intent is PlannerIntent.FOLLOW_UP:
        if not is_follow_up or not requires_context:
            raise PlannerOutputError(
                "follow_up intent requires is_follow_up and requires_context"
            )
    elif is_follow_up:
        raise PlannerOutputError("is_follow_up requires follow_up intent")

    if intent is PlannerIntent.SIMPLE_QUESTION and requires_database:
        raise PlannerOutputError("simple_question must not require a database")
    if intent in {
        PlannerIntent.SINGLE_QUERY,
        PlannerIntent.MULTI_STEP_ANALYSIS,
    } and not requires_database:
        raise PlannerOutputError(f"{intent.value} must require a database")
    if requires_database and not any(task.task_type == "query" for task in tasks):
        raise PlannerOutputError("database plans require at least one query task")
    if intent is PlannerIntent.MULTI_STEP_ANALYSIS and len(tasks) < 2:
        raise PlannerOutputError("multi_step_analysis requires multiple tasks")

    return PlannerResult(
        intent=intent,
        reason_summary=reason_summary,
        tasks=tuple(tasks),
        requires_database=requires_database,
        requires_context=requires_context,
        is_follow_up=is_follow_up,
    )


class Planner:
    """Call a model, validate its plan, and update only planning state fields."""

    def __init__(self, model_client: PlannerModel, *, max_retries: int = 1) -> None:
        if max_retries not in {0, 1}:
            raise ValueError("max_retries must be 0 or 1")
        self.model_client = model_client
        self.max_retries = max_retries

    def plan(
        self,
        state: AgentState,
        *,
        trace: TraceCollector,
    ) -> PlannerResult:
        """Create a validated plan and write it into the supplied AgentState."""

        if trace.trace_id != state["trace_id"]:
            raise ValueError("trace and state must use the same trace_id")

        started_at = perf_counter()
        retry_count = 0
        trace.add_event(
            EventType.PLANNER_STARTED,
            component="planner",
            action="plan",
            summary="Planner started classifying and decomposing the request.",
            metadata={"retry_count": retry_count, "duration": 0.0},
        )

        for attempt in range(self.max_retries + 1):
            try:
                response_text = self.model_client.complete(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=f"User query:\n{state['original_query']}",
                    response_schema=PLANNER_RESPONSE_SCHEMA,
                )
            except Exception as exc:
                trace.add_event(
                    EventType.PLANNER_FAILED,
                    component="planner",
                    action="model_call",
                    summary="Planner model call failed.",
                    metadata={
                        "retry_count": retry_count,
                        "duration": perf_counter() - started_at,
                    },
                )
                if isinstance(exc, PlannerError):
                    raise
                raise PlannerError("planner model call failed") from exc

            try:
                result = parse_planner_response(response_text)
            except PlannerOutputError as exc:
                if attempt < self.max_retries:
                    retry_count += 1
                    trace.add_event(
                        EventType.PLANNER_RETRY,
                        component="planner",
                        action="validate_output",
                        summary="Planner output was invalid; retrying once.",
                        metadata={
                            "retry_count": retry_count,
                            "duration": perf_counter() - started_at,
                        },
                    )
                    continue

                trace.add_event(
                    EventType.PLANNER_FAILED,
                    component="planner",
                    action="validate_output",
                    summary="Planner failed to return valid structured output.",
                    metadata={
                        "retry_count": retry_count,
                        "duration": perf_counter() - started_at,
                    },
                )
                attempts = self.max_retries + 1
                raise PlannerError(
                    f"planner returned invalid structured output after {attempts} "
                    f"attempts: {exc}"
                ) from exc

            planned_tasks = list(result.tasks)
            state["task_plan"] = planned_tasks
            state["pending_tasks"] = list(planned_tasks)
            state["current_task"] = planned_tasks[0]

            trace.add_event(
                EventType.PLANNER_RESULT,
                component="planner",
                action="update_state",
                summary="Planner produced a validated structured plan.",
                metadata={
                    "intent": result.intent.value,
                    "task_count": len(result.tasks),
                    "retry_count": retry_count,
                    "duration": perf_counter() - started_at,
                },
            )
            return result

        raise AssertionError("planner retry loop exited unexpectedly")
