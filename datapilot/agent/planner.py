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

from datapilot.agent.state import AgentState, TaskItem, get_effective_query
from datapilot.retrieval.integration import compact_knowledge_evidence
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
When a current domain planning context is supplied, treat its listed capabilities
as the authoritative planning boundary. Query task descriptions may use only the
listed entities, fields, dimensions, time dimensions, metrics, views, and
relationships. Do not invent unavailable schema concepts or treat an alarm field
as a session field. A view may use only its explicitly listed available_fields;
never project fields from an entity onto a view. Only entities and views are SQL
query targets. Semantic objects with queryable_with_sql=false define metrics and
dimensions but are not tables; use each metric's source_entity in query tasks.
Retrieved domain knowledge is advisory terminology and procedure context, not
database schema or observed data. It may justify a knowledge-only response task,
but must never be turned into a query field, table, alarm, or measured fact.
Use exact schema identifiers in query task descriptions so the SQL Agent receives
an implementable contract. If a requested breakdown is unavailable, omit it and
plan the supported analysis or a grounded response that states the limitation.
When lifecycle status or other categorical evidence is available and relevant,
plan either individual records or counts grouped by that categorical field. Do
not substitute an active count, MIN/MAX value, or representative field for the
full observed distribution.
For alarm tasks, request total count, status/severity/error-code distributions,
affected objects, and bounded raw message samples only when troubleshooting needs
text examples. Samples are examples, not a distribution or a statement about all
events. Never request representative_message, MIN/MAX(message), MIN/MAX(status),
or any arbitrary representative textual aggregation. A deterministic summary
built from the filtered raw alarm rows satisfies these distribution requirements.
Return one JSON object that exactly matches the provided schema.
Keep reason_summary short and state only the observable decision reason.
All tasks must start with status 'pending' and may depend only on earlier tasks.
For a database question, end the plan with a response task grounded in completed
query or analysis tasks. For a comparison, use separate query tasks when needed,
then an analysis task for calculations, then a response task. Never put SQL in a
task description. Reserve exactly two query dependencies on an analysis task for
a same-metric grouped comparison. When correlating different metric families,
create at least three query tasks: separate target-metric comparison inputs plus
an independent evidence query, then make the analysis depend on all of them.
For any decline, growth, change, contribution, or comparison by a group, require
matched baseline and current values for the same metric and grouping dimension.
A current-only grouped result cannot establish change or rank the largest decline.
Describe the query so it returns both windows directly or creates paired baseline
and current query inputs; never ask Analyst to infer a delta from current values.
For every metric-bearing query task, add metric_binding with exactly one
primary_metric from the domain context, its unit and aggregation_semantics, and
supporting_fields that may validate or recompute it. Supporting numeric fields are
not alternative primary metrics. Add requested_dimensions using exact schema names.
For a time comparison, also add one shared window_role_binding to every participating
query task: comparison_target is the current window, baseline_windows lists every
requested baseline, and primary_baseline is a baseline only when the user or task
explicitly selects one. Never silently choose a primary from multiple baselines.
For non-metric or non-comparison tasks, return null metric/window bindings and an
empty requested_dimensions list. The parser still accepts legacy saved plans that
predate these fields, but new structured Planner output must include them.
"""

_METRIC_BINDING_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": [
        "primary_metric",
        "unit",
        "aggregation_semantics",
        "supporting_fields",
    ],
    "properties": {
        "primary_metric": {"type": "string", "minLength": 1},
        "unit": {"type": "string", "minLength": 1},
        "aggregation_semantics": {"type": "string", "minLength": 1},
        "supporting_fields": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
}

_WINDOW_ROLE_BINDING_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": [
        "comparison_target",
        "baseline_windows",
        "primary_baseline",
    ],
    "properties": {
        "comparison_target": {"type": "string", "minLength": 1},
        "baseline_windows": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "primary_baseline": {"type": ["string", "null"]},
    },
}

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
                    "metric_binding",
                    "requested_dimensions",
                    "window_role_binding",
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
                    "metric_binding": _METRIC_BINDING_SCHEMA,
                    "requested_dimensions": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "window_role_binding": _WINDOW_ROLE_BINDING_SCHEMA,
                },
            },
        },
        "requires_database": {"type": "boolean"},
        "requires_context": {"type": "boolean"},
        "is_follow_up": {"type": "boolean"},
    },
}

_TOP_LEVEL_FIELDS = frozenset(PLANNER_RESPONSE_SCHEMA["required"])
_TASK_REQUIRED_FIELDS = frozenset(
    {"task_id", "description", "task_type", "depends_on", "status"}
)
_TASK_ALLOWED_FIELDS = frozenset(
    PLANNER_RESPONSE_SCHEMA["properties"]["tasks"]["items"]["properties"]
)


def _build_user_prompt(
    state: AgentState,
    planning_context: Mapping[str, Any] | None,
) -> str:
    query = get_effective_query(state)
    sections = [f"User query:\n{query}"]
    if planning_context is not None:
        context_json = json.dumps(
            dict(planning_context),
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        sections.append(
            "Current domain planning context (read-only and authoritative for "
            "database capabilities):\n"
            f"{context_json}\n\n"
            "Use this context only to choose feasible tasks and dependencies. "
            "Do not generate SQL, query data, or copy schema text into the answer."
        )
    knowledge = compact_knowledge_evidence(
        state,
        max_items=4,
        max_text_chars=500,
    )
    if knowledge:
        sections.append(
            "Retrieved domain knowledge (advisory; not schema and not observed "
            "data):\n"
            f"{json.dumps(knowledge, ensure_ascii=False, separators=(',', ':'))}\n\n"
            "Use it only to understand domain concepts or plan a response. A "
            "knowledge-only request may be a simple_question with one response "
            "task and no database query."
        )
    return "\n\n".join(sections)


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


def _require_allowed_fields(
    value: Mapping[str, Any],
    *,
    required: frozenset[str],
    allowed: frozenset[str],
    location: str,
) -> None:
    actual = frozenset(value)
    missing = sorted(required - actual)
    extra = sorted(actual - allowed)
    if not missing and not extra:
        return
    raise PlannerOutputError(
        f"{location} fields are invalid; missing={missing}, extra={extra}"
    )


def _parse_metric_binding(raw: Any, *, location: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PlannerOutputError(f"{location} must be an object or null")
    expected = frozenset(_METRIC_BINDING_SCHEMA["required"])
    _require_exact_fields(raw, expected, location=location)
    for field_name in ("primary_metric", "unit", "aggregation_semantics"):
        value = raw[field_name]
        if not isinstance(value, str) or not value.strip():
            raise PlannerOutputError(f"{location}.{field_name} must not be empty")
    supporting = raw["supporting_fields"]
    if not isinstance(supporting, list) or not all(
        isinstance(item, str) and item.strip() for item in supporting
    ):
        raise PlannerOutputError(
            f"{location}.supporting_fields must be a string list"
        )
    normalized_supporting = [item.strip() for item in supporting]
    if len(normalized_supporting) != len(set(normalized_supporting)):
        raise PlannerOutputError(
            f"{location}.supporting_fields contains duplicates"
        )
    primary = raw["primary_metric"].strip()
    if primary in normalized_supporting:
        raise PlannerOutputError(
            f"{location}.supporting_fields must not include primary_metric"
        )
    return {
        "primary_metric": primary,
        "unit": raw["unit"].strip(),
        "aggregation_semantics": raw["aggregation_semantics"].strip(),
        "supporting_fields": normalized_supporting,
    }


def _parse_window_role_binding(raw: Any, *, location: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PlannerOutputError(f"{location} must be an object or null")
    expected = frozenset(_WINDOW_ROLE_BINDING_SCHEMA["required"])
    _require_exact_fields(raw, expected, location=location)
    target = raw["comparison_target"]
    baselines = raw["baseline_windows"]
    primary = raw["primary_baseline"]
    if not isinstance(target, str) or not target.strip():
        raise PlannerOutputError(f"{location}.comparison_target must not be empty")
    if not isinstance(baselines, list) or not baselines or not all(
        isinstance(item, str) and item.strip() for item in baselines
    ):
        raise PlannerOutputError(
            f"{location}.baseline_windows must be a non-empty string list"
        )
    normalized_baselines = [item.strip() for item in baselines]
    if len(normalized_baselines) != len(set(normalized_baselines)):
        raise PlannerOutputError(f"{location}.baseline_windows contains duplicates")
    target = target.strip()
    if target in normalized_baselines:
        raise PlannerOutputError(
            f"{location}.comparison_target cannot also be a baseline"
        )
    if primary is not None:
        if not isinstance(primary, str) or not primary.strip():
            raise PlannerOutputError(
                f"{location}.primary_baseline must be a string or null"
            )
        primary = primary.strip()
        if primary not in normalized_baselines:
            raise PlannerOutputError(
                f"{location}.primary_baseline must be listed in baseline_windows"
            )
    return {
        "comparison_target": target,
        "baseline_windows": normalized_baselines,
        "primary_baseline": primary,
    }


def _parse_dimensions(raw: Any, *, location: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        raise PlannerOutputError(f"{location} must be a string list")
    dimensions = [item.strip() for item in raw]
    if len(dimensions) != len(set(dimensions)):
        raise PlannerOutputError(f"{location} contains duplicates")
    return dimensions


def _require_bool(value: Any, *, field_name: str) -> bool:
    if type(value) is not bool:
        raise PlannerOutputError(f"{field_name} must be a boolean")
    return value


def _parse_task(raw_task: Any, *, known_ids: set[str], index: int) -> TaskItem:
    if not isinstance(raw_task, dict):
        raise PlannerOutputError(f"tasks[{index}] must be an object")
    _require_allowed_fields(
        raw_task,
        required=_TASK_REQUIRED_FIELDS,
        allowed=_TASK_ALLOWED_FIELDS,
        location=f"tasks[{index}]",
    )

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
        metric_binding=_parse_metric_binding(
            raw_task.get("metric_binding"),
            location=f"tasks[{index}].metric_binding",
        ),
        requested_dimensions=_parse_dimensions(
            raw_task.get("requested_dimensions"),
            location=f"tasks[{index}].requested_dimensions",
        ),
        window_role_binding=_parse_window_role_binding(
            raw_task.get("window_role_binding"),
            location=f"tasks[{index}].window_role_binding",
        ),
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
        planning_context: Mapping[str, Any] | None = None,
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
                    user_prompt=_build_user_prompt(state, planning_context),
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
