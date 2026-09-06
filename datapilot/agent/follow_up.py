"""Structured context extraction and follow-up resolution for DataPilot."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from time import perf_counter
from typing import Any, Protocol

from datapilot.agent.state import SessionContext, TaskItem, TimeRangeContext
from datapilot.tracing.trace import EventType, TraceCollector

SLOT_FIELDS = frozenset(
    {"metrics", "dimensions", "time_range", "filters", "entities", "analysis_goal"}
)


class ContextModel(Protocol):
    """Minimal model boundary shared by extractor and resolver."""

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str: ...


class SessionContextError(RuntimeError):
    """Safe structured-context error."""

    def __init__(self, *, stage: str, summary: str) -> None:
        self.stage = stage
        self.summary = summary
        super().__init__(f"{stage} failed: {summary}")


class FollowUpResolutionError(SessionContextError):
    """Follow-up resolver output or model-call failure."""


@dataclass(slots=True)
class SessionContextUpdate:
    """Business slots extracted from one successful standalone turn."""

    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    time_range: TimeRangeContext = field(default_factory=TimeRangeContext)
    filters: dict[str, list[str]] = field(default_factory=dict)
    entities: dict[str, list[str]] = field(default_factory=dict)
    analysis_goal: str | None = None


@dataclass(slots=True)
class FollowUpResolution(SessionContextUpdate):
    """Strict standalone-query resolution plus explicit merge accounting."""

    resolved_query: str | None = None
    inherited_fields: list[str] = field(default_factory=list)
    overridden_fields: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    can_resolve: bool = False
    reason_summary: str = ""


_SLOT_SCHEMA: dict[str, Any] = {
    "metrics": {
        "type": "array",
        "maxItems": 10,
        "items": {"type": "string", "minLength": 1, "maxLength": 120},
    },
    "dimensions": {
        "type": "array",
        "maxItems": 10,
        "items": {"type": "string", "minLength": 1, "maxLength": 120},
    },
    "time_range": {
        "type": "object",
        "additionalProperties": False,
        "required": ["labels", "start", "end"],
        "properties": {
            "labels": {
                "type": "array",
                "maxItems": 10,
                "items": {"type": "string", "minLength": 1, "maxLength": 120},
            },
            "start": {"type": ["string", "null"], "maxLength": 120},
            "end": {"type": ["string", "null"], "maxLength": 120},
        },
    },
    "filters": {
        "type": "object",
        "maxProperties": 10,
        "additionalProperties": {
            "type": "array",
            "maxItems": 10,
            "items": {"type": "string", "minLength": 1, "maxLength": 120},
        },
    },
    "entities": {
        "type": "object",
        "maxProperties": 10,
        "additionalProperties": {
            "type": "array",
            "maxItems": 10,
            "items": {"type": "string", "minLength": 1, "maxLength": 120},
        },
    },
    "analysis_goal": {"type": ["string", "null"], "maxLength": 400},
}

CONTEXT_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(_SLOT_SCHEMA),
    "properties": _SLOT_SCHEMA,
}

FOLLOW_UP_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "resolved_query",
        "inherited_fields",
        "overridden_fields",
        "missing_fields",
        "can_resolve",
        "reason_summary",
        *_SLOT_SCHEMA,
    ],
    "properties": {
        **_SLOT_SCHEMA,
        "resolved_query": {"type": ["string", "null"], "maxLength": 1000},
        "inherited_fields": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "enum": sorted(SLOT_FIELDS)},
        },
        "overridden_fields": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "enum": sorted(SLOT_FIELDS)},
        },
        "missing_fields": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 120},
        },
        "can_resolve": {"type": "boolean"},
        "reason_summary": {"type": "string", "minLength": 1, "maxLength": 240},
    },
}

EXTRACTOR_SYSTEM_PROMPT = """You extract compact business context from one
successfully completed DataPilot turn. Return only the supplied JSON schema.
Use the standalone query and structured task descriptions. Do not invent business
facts, SQL, data rows, credentials, or hidden reasoning. Metrics and dimensions are
short canonical labels. Time labels preserve expressions such as Q2 and Q3. Filters
contain explicit restrictions only. Return empty collections for absent slots.
"""

FOLLOW_UP_SYSTEM_PROMPT = """You resolve one DataPilot follow-up into a standalone
data question using only the supplied authoritative session context. Return only the
supplied JSON schema. Do not concatenate text mechanically, invent context, SQL,
database facts, or hidden reasoning. Preserve inherited slots, replace overridden
slots, and list each in inherited_fields or overridden_fields. A changed slot must be
listed as overridden. If the follow-up is ambiguous, set can_resolve=false, provide
missing_fields, and leave resolved_query null. Keep reason_summary brief.
Session slots are business semantics, not physical schema. An explicit value in the
raw follow-up may add a new semantic filter such as region, city, or customer_type,
even when that filter key was absent from the previous context.
When extracting a structured semantic filter, do not repeat its field type in the
value. For example, "华南地区" means filters.region=["华南"] because "地区" is
already represented by the region field. Keep the original natural-language wording
in the resolved query, but normalize only the structured slot. Do not translate
"华南" to "South China" or otherwise rewrite categorical values unnecessarily.
Adding or normalizing a filter does not by itself change analysis_goal: copy the
existing goal unchanged and mark it inherited unless the user explicitly changes it.
Do not require a physical database column for an explicit semantic filter;
physical field mapping is
the responsibility of Wren Context and the SQL Agent. A missing physical field name
must not be reported as missing. Use missing_fields only when session semantics are
truly insufficient or the user's reference is ambiguous. Never invent filter values
that the user did not explicitly provide.
"""


def _safe_error_summary(error: BaseException) -> str:
    text = " ".join(str(error).split()) or type(error).__name__
    text = re.sub(
        r"(?i)(api[_ -]?key|password|token|authorization)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    return re.sub(r"[a-z][a-z0-9+.-]*://\S+", "[REDACTED_URL]", text)[:300]


def _string_list(value: Any, *, name: str, max_items: int = 10) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise SessionContextError(stage="structured_output", summary=f"{name} invalid")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise SessionContextError(stage="structured_output", summary=f"{name} invalid")
    cleaned = [item.strip() for item in value]
    if len(cleaned) != len(set(cleaned)):
        raise SessionContextError(
            stage="structured_output",
            summary=f"{name} contains duplicates",
        )
    return cleaned


def _mapping_lists(value: Any, *, name: str) -> dict[str, list[str]]:
    if not isinstance(value, dict) or len(value) > 10:
        raise SessionContextError(stage="structured_output", summary=f"{name} invalid")
    result: dict[str, list[str]] = {}
    for raw_key, raw_values in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise SessionContextError(
                stage="structured_output",
                summary=f"{name} key invalid",
            )
        result[raw_key.strip()] = _string_list(
            raw_values,
            name=f"{name}.{raw_key}",
        )
    return result


def _parse_slots(raw: Mapping[str, Any]) -> SessionContextUpdate:
    time_range = raw.get("time_range")
    if not isinstance(time_range, dict) or set(time_range) != {
        "labels",
        "start",
        "end",
    }:
        raise SessionContextError(
            stage="structured_output",
            summary="time_range invalid",
        )
    start = time_range["start"]
    end = time_range["end"]
    if start is not None and (not isinstance(start, str) or not start.strip()):
        raise SessionContextError(stage="structured_output", summary="start invalid")
    if end is not None and (not isinstance(end, str) or not end.strip()):
        raise SessionContextError(stage="structured_output", summary="end invalid")
    goal = raw.get("analysis_goal")
    if goal is not None and (not isinstance(goal, str) or not goal.strip()):
        raise SessionContextError(
            stage="structured_output",
            summary="analysis_goal invalid",
        )
    return SessionContextUpdate(
        metrics=_string_list(raw.get("metrics"), name="metrics"),
        dimensions=_string_list(raw.get("dimensions"), name="dimensions"),
        time_range=TimeRangeContext(
            labels=_string_list(time_range["labels"], name="time_range.labels"),
            start=start.strip() if isinstance(start, str) else None,
            end=end.strip() if isinstance(end, str) else None,
        ),
        filters=_mapping_lists(raw.get("filters"), name="filters"),
        entities=_mapping_lists(raw.get("entities"), name="entities"),
        analysis_goal=goal.strip() if isinstance(goal, str) else None,
    )


def _normalize_follow_up_filters(
    filters: Mapping[str, list[str]],
) -> dict[str, list[str]]:
    """Remove only a duplicated Chinese region type suffix from region values."""

    normalized = {name: list(values) for name, values in filters.items()}
    for field_name, values in normalized.items():
        if field_name.casefold() != "region":
            continue
        normalized[field_name] = [
            value[: -len("地区")]
            if value.endswith("地区") and len(value) > len("地区")
            else value
            for value in values
        ]
    return normalized


def _slot_values(value: SessionContext | SessionContextUpdate) -> dict[str, Any]:
    return {
        "metrics": value.metrics,
        "dimensions": value.dimensions,
        "time_range": asdict(value.time_range),
        "filters": value.filters,
        "entities": value.entities,
        "analysis_goal": value.analysis_goal,
    }


def merge_session_context(
    previous: SessionContext,
    resolution: FollowUpResolution,
) -> SessionContext:
    """Apply a validated follow-up merge without mutating stored context."""

    if not resolution.can_resolve or not resolution.resolved_query:
        raise FollowUpResolutionError(
            stage="merge",
            summary="follow-up is not resolvable",
        )
    inherited = set(resolution.inherited_fields)
    overridden = set(resolution.overridden_fields)
    if inherited & overridden:
        raise FollowUpResolutionError(
            stage="merge",
            summary="inherited and overridden fields overlap",
        )
    previous_values = _slot_values(previous)
    resolved_values = _slot_values(resolution)
    changed = {
        field_name
        for field_name in SLOT_FIELDS
        if previous_values[field_name] != resolved_values[field_name]
    }
    if not changed <= overridden:
        raise FollowUpResolutionError(
            stage="merge",
            summary=(
                "changed fields not declared overridden: "
                f"{sorted(changed - overridden)}"
            ),
        )
    invalid_inherited = {
        field_name
        for field_name in inherited
        if previous_values[field_name] != resolved_values[field_name]
    }
    if invalid_inherited:
        raise FollowUpResolutionError(
            stage="merge",
            summary=f"inherited fields changed: {sorted(invalid_inherited)}",
        )
    merged = deepcopy(previous)
    merged.metrics = list(resolution.metrics)
    merged.dimensions = list(resolution.dimensions)
    merged.time_range = deepcopy(resolution.time_range)
    merged.filters = deepcopy(resolution.filters)
    merged.entities = deepcopy(resolution.entities)
    merged.analysis_goal = resolution.analysis_goal
    return merged


class SessionContextExtractor:
    """Extract structured slots after a standalone turn succeeds."""

    def __init__(self, model_client: ContextModel, *, max_retries: int = 1) -> None:
        if max_retries not in {0, 1}:
            raise ValueError("max_retries must be 0 or 1")
        self.model_client = model_client
        self.max_retries = max_retries

    def extract(
        self,
        query: str,
        tasks: Sequence[TaskItem],
    ) -> SessionContextUpdate:
        prompt = (
            f"Successful standalone query:\n{query}\n\n"
            "Structured tasks:\n"
            + json.dumps(
                [
                    {"description": task.description, "task_type": task.task_type}
                    for task in tasks
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        last_error: SessionContextError | None = None
        for attempt in range(self.max_retries + 1):
            suffix = ""
            if last_error is not None:
                suffix = f"\n\nCorrection: {last_error.summary}. Return JSON only."
            try:
                response = self.model_client.complete(
                    system_prompt=EXTRACTOR_SYSTEM_PROMPT,
                    user_prompt=f"{prompt}{suffix}",
                    response_schema=CONTEXT_EXTRACTION_SCHEMA,
                )
                raw = json.loads(response)
                if not isinstance(raw, dict) or set(raw) != SLOT_FIELDS:
                    raise SessionContextError(
                        stage="structured_output",
                        summary="context fields invalid",
                    )
                return _parse_slots(raw)
            except SessionContextError as exc:
                last_error = exc
            except (TypeError, json.JSONDecodeError) as exc:
                last_error = SessionContextError(
                    stage="structured_output",
                    summary="context response is not valid JSON",
                )
            except Exception as exc:
                raise SessionContextError(
                    stage="model_call",
                    summary=_safe_error_summary(exc),
                ) from exc
            if attempt >= self.max_retries:
                raise last_error
        raise AssertionError("context extraction retry loop exited unexpectedly")


class FollowUpResolver:
    """Resolve a Planner-confirmed follow-up from authoritative session slots."""

    def __init__(
        self,
        model_client: ContextModel,
        *,
        trace: TraceCollector | None = None,
        max_retries: int = 1,
    ) -> None:
        if max_retries not in {0, 1}:
            raise ValueError("max_retries must be 0 or 1")
        self.model_client = model_client
        self.trace = trace
        self.max_retries = max_retries

    def resolve(
        self,
        user_query: str,
        session_context: SessionContext,
        *,
        trace: TraceCollector | None = None,
    ) -> FollowUpResolution:
        active_trace = trace if trace is not None else self.trace
        if active_trace is None:
            raise ValueError("a TraceCollector is required")
        started_at = perf_counter()
        active_trace.add_event(
            EventType.FOLLOW_UP_RESOLUTION_STARTED,
            component="follow_up_resolver",
            action="resolve",
            summary="Follow-up resolution started.",
            metadata={
                "session_id": session_context.session_id[:8],
                "turn_index": session_context.turn_index + 1,
            },
        )
        compact_context = {
            **_slot_values(session_context),
            "last_user_query": session_context.last_user_query,
            "last_resolved_query": session_context.last_resolved_query,
            "last_answer_summary": session_context.last_answer_summary,
        }
        prompt = (
            f"Raw follow-up:\n{user_query}\n\n"
            "Authoritative session context:\n"
            + json.dumps(compact_context, ensure_ascii=False, separators=(",", ":"))
        )
        last_error: SessionContextError | None = None
        for attempt in range(self.max_retries + 1):
            suffix = ""
            if last_error is not None:
                suffix = f"\n\nCorrection: {last_error.summary}. Return JSON only."
            try:
                response = self.model_client.complete(
                    system_prompt=FOLLOW_UP_SYSTEM_PROMPT,
                    user_prompt=f"{prompt}{suffix}",
                    response_schema=FOLLOW_UP_RESPONSE_SCHEMA,
                )
                result = self._parse_resolution(response, session_context)
            except SessionContextError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    continue
                active_trace.add_event(
                    EventType.FOLLOW_UP_RESOLUTION_FAILED,
                    component="follow_up_resolver",
                    action="validate_output",
                    summary="Follow-up resolution failed.",
                    metadata={
                        "session_id": session_context.session_id[:8],
                        "turn_index": session_context.turn_index + 1,
                        "duration": perf_counter() - started_at,
                    },
                )
                raise FollowUpResolutionError(
                    stage=exc.stage,
                    summary=exc.summary,
                ) from exc
            except Exception as exc:
                error = FollowUpResolutionError(
                    stage="model_call",
                    summary=_safe_error_summary(exc),
                )
                active_trace.add_event(
                    EventType.FOLLOW_UP_RESOLUTION_FAILED,
                    component="follow_up_resolver",
                    action="model_call",
                    summary="Follow-up resolution failed.",
                    metadata={
                        "session_id": session_context.session_id[:8],
                        "turn_index": session_context.turn_index + 1,
                        "duration": perf_counter() - started_at,
                    },
                )
                raise error from exc

            event_type = (
                EventType.FOLLOW_UP_RESOLVED
                if result.can_resolve
                else EventType.FOLLOW_UP_RESOLUTION_FAILED
            )
            active_trace.add_event(
                event_type,
                component="follow_up_resolver",
                action="resolve",
                summary=(
                    "Follow-up resolved."
                    if result.can_resolve
                    else "Follow-up requires clarification."
                ),
                metadata={
                    "session_id": session_context.session_id[:8],
                    "turn_index": session_context.turn_index + 1,
                    "inherited_field_count": len(result.inherited_fields),
                    "overridden_field_count": len(result.overridden_fields),
                    "duration": perf_counter() - started_at,
                },
            )
            return result
        raise AssertionError("follow-up retry loop exited unexpectedly")

    @staticmethod
    def _parse_resolution(
        response: str,
        previous: SessionContext,
    ) -> FollowUpResolution:
        try:
            raw = json.loads(response)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SessionContextError(
                stage="structured_output",
                summary="follow-up response is not valid JSON",
            ) from exc
        expected = set(FOLLOW_UP_RESPONSE_SCHEMA["required"])
        if not isinstance(raw, dict) or set(raw) != expected:
            raise SessionContextError(
                stage="structured_output",
                summary="follow-up fields invalid",
            )
        slots = _parse_slots(raw)
        inherited = _string_list(
            raw["inherited_fields"],
            name="inherited_fields",
        )
        overridden = _string_list(
            raw["overridden_fields"],
            name="overridden_fields",
        )
        missing = _string_list(raw["missing_fields"], name="missing_fields")
        if not (set(inherited) | set(overridden)) <= SLOT_FIELDS:
            raise SessionContextError(
                stage="structured_output",
                summary="merge field name invalid",
            )
        can_resolve = raw["can_resolve"]
        if type(can_resolve) is not bool:
            raise SessionContextError(
                stage="structured_output",
                summary="can_resolve must be boolean",
            )
        resolved_query = raw["resolved_query"]
        if resolved_query is not None and (
            not isinstance(resolved_query, str) or not resolved_query.strip()
        ):
            raise SessionContextError(
                stage="structured_output",
                summary="resolved_query invalid",
            )
        reason = raw["reason_summary"]
        if not isinstance(reason, str) or not reason.strip():
            raise SessionContextError(
                stage="structured_output",
                summary="reason_summary invalid",
            )
        if can_resolve and (resolved_query is None or missing):
            raise SessionContextError(
                stage="structured_output",
                summary="resolved follow-up requires a query and no missing fields",
            )
        if not can_resolve and (resolved_query is not None or not missing):
            raise SessionContextError(
                stage="structured_output",
                summary="unresolved follow-up requires missing fields and null query",
            )
        result = FollowUpResolution(
            metrics=list(slots.metrics),
            dimensions=list(slots.dimensions),
            time_range=deepcopy(slots.time_range),
            filters=_normalize_follow_up_filters(slots.filters),
            entities=deepcopy(slots.entities),
            analysis_goal=slots.analysis_goal,
            resolved_query=(resolved_query.strip() if resolved_query else None),
            inherited_fields=inherited,
            overridden_fields=overridden,
            missing_fields=missing,
            can_resolve=can_resolve,
            reason_summary=reason.strip(),
        )
        if can_resolve:
            merge_session_context(previous, result)
        return result
