from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from datapilot.agent.follow_up import (
    FollowUpResolutionError,
    FollowUpResolver,
    merge_session_context,
)
from datapilot.agent.graph import prepare_session_turn
from datapilot.agent.planner import Planner
from datapilot.agent.state import (
    SessionContext,
    TimeRangeContext,
    create_initial_state,
)
from datapilot.memory.session_memory import SessionMemoryStore
from datapilot.tracing.trace import EventType, TraceCollector


class SequenceModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response_schema": response_schema,
            }
        )
        return self.responses.pop(0)


def _task(task_id: str = "query") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "description": "Retrieve the requested data.",
        "task_type": "query",
        "depends_on": [],
        "status": "pending",
    }


def _plan(intent: str) -> str:
    follow_up = intent == "follow_up"
    tasks = [_task()]
    if intent == "multi_step_analysis":
        tasks.append(
            {
                "task_id": "analysis",
                "description": "Analyze the requested data.",
                "task_type": "analysis",
                "depends_on": ["query"],
                "status": "pending",
            }
        )
    return json.dumps(
        {
            "intent": intent,
            "reason_summary": "Structured route.",
            "tasks": tasks,
            "requires_database": True,
            "requires_context": follow_up,
            "is_follow_up": follow_up,
        }
    )


def _previous(session_id: str = "session-a") -> SessionContext:
    return SessionContext(
        session_id=session_id,
        turn_index=1,
        metrics=["GMV"],
        dimensions=["商品类别"],
        time_range=TimeRangeContext(labels=["Q2", "Q3"]),
        filters={},
        analysis_goal="比较并找出下降最大的类别",
        last_user_query="比较 Q2 和 Q3 各商品类别 GMV",
        last_resolved_query="比较 Q2 和 Q3 各商品类别 GMV",
    )


def _resolution(
    *,
    metrics: list[str] | None = None,
    labels: list[str] | None = None,
    filters: dict[str, list[str]] | None = None,
    inherited: list[str] | None = None,
    overridden: list[str] | None = None,
    can_resolve: bool = True,
    missing: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "resolved_query": (
                "比较 Q2 和 Q3 华南地区各商品类别 GMV，并找出下降最大的类别。"
                if can_resolve
                else None
            ),
            "inherited_fields": inherited
            if inherited is not None
            else ["metrics", "dimensions", "time_range", "analysis_goal"],
            "overridden_fields": overridden if overridden is not None else ["filters"],
            "missing_fields": missing or [],
            "can_resolve": can_resolve,
            "reason_summary": (
                "Added the requested region filter."
                if can_resolve
                else "The requested change is ambiguous."
            ),
            "metrics": metrics or ["GMV"],
            "dimensions": ["商品类别"],
            "time_range": {
                "labels": labels or ["Q2", "Q3"],
                "start": None,
                "end": None,
            },
            "filters": filters if filters is not None else {"region": ["华南"]},
            "entities": {},
            "analysis_goal": "比较并找出下降最大的类别",
        },
        ensure_ascii=False,
    )


def _resolve(response: str) -> Any:
    previous = _previous()
    trace = TraceCollector()
    return FollowUpResolver(SequenceModel([response])).resolve(
        "那华南地区呢？",
        previous,
        trace=trace,
    )


def test_follow_up_loads_previous_context() -> None:
    previous = _previous()
    model = SequenceModel([_resolution()])

    FollowUpResolver(model).resolve(
        "那华南地区呢？",
        previous,
        trace=TraceCollector(),
    )

    prompt = model.calls[0]["user_prompt"]
    assert '"metrics":["GMV"]' in prompt
    assert '"labels":["Q2","Q3"]' in prompt


def test_follow_up_inherits_metric() -> None:
    result = _resolve(_resolution())

    assert result.metrics == ["GMV"]
    assert "metrics" in result.inherited_fields


def test_follow_up_inherits_dimension() -> None:
    result = _resolve(_resolution())

    assert result.dimensions == ["商品类别"]
    assert "dimensions" in result.inherited_fields


def test_follow_up_adds_filter() -> None:
    previous = _previous()
    result = _resolve(_resolution())

    merged = merge_session_context(previous, result)

    assert merged.filters == {"region": ["华南"]}
    assert "filters" in result.overridden_fields


def test_follow_up_normalizes_region_type_suffix() -> None:
    result = _resolve(_resolution(filters={"region": ["华南地区"]}))

    assert result.filters == {"region": ["华南"]}


def test_follow_up_region_value_is_huanan_not_huanan_diqu() -> None:
    previous = _previous()
    result = _resolve(_resolution(filters={"region": ["华南地区"]}))

    merged = merge_session_context(previous, result)

    assert merged.filters["region"] == ["华南"]
    assert "华南地区" not in merged.filters["region"]


def test_follow_up_normalization_preserves_original_query() -> None:
    previous = _previous()
    store = SessionMemoryStore()
    store.save(previous)
    state = create_initial_state("那华南地区呢？", previous_session=previous)

    prepare_session_turn(
        state,
        Planner(SequenceModel([_plan("follow_up"), _plan("multi_step_analysis")])),
        FollowUpResolver(
            SequenceModel([_resolution(filters={"region": ["华南地区"]})])
        ),
        store,
        trace=TraceCollector(trace_id=state["trace_id"]),
    )

    assert state["original_query"] == "那华南地区呢？"
    assert state["follow_up_resolution"].filters == {"region": ["华南"]}


def test_follow_up_normalization_does_not_translate_value() -> None:
    result = _resolve(_resolution(filters={"region": ["华南"]}))

    assert result.filters == {"region": ["华南"]}
    assert "South China" not in result.filters["region"]


def test_follow_up_normalization_does_not_modify_unrelated_filters() -> None:
    result = _resolve(
        _resolution(
            filters={
                "region": ["华南地区"],
                "customer_type": ["华南地区"],
            }
        )
    )

    assert result.filters == {
        "region": ["华南"],
        "customer_type": ["华南地区"],
    }


def test_follow_up_can_add_new_semantic_region_filter() -> None:
    previous = _previous()
    assert "region" not in previous.filters

    result = _resolve(_resolution())

    assert result.can_resolve is True
    assert result.filters == {"region": ["华南"]}
    assert result.missing_fields == []


def test_follow_up_does_not_require_physical_region_field() -> None:
    model = SequenceModel([_resolution()])

    FollowUpResolver(model).resolve(
        "那华南地区呢？",
        _previous(),
        trace=TraceCollector(),
    )

    prompt = model.calls[0]["system_prompt"]
    assert "Do not require a physical database column" in prompt
    assert "must not be reported as missing" in prompt


def test_follow_up_leaves_schema_mapping_to_sql_agent() -> None:
    model = SequenceModel([_resolution()])

    FollowUpResolver(model).resolve(
        "那华南地区呢？",
        _previous(),
        trace=TraceCollector(),
    )

    prompt = model.calls[0]["system_prompt"]
    assert "Wren Context and the SQL Agent" in prompt
    assert "semantic filter" in prompt


def test_follow_up_still_rejects_truly_ambiguous_reference() -> None:
    response = _resolution(can_resolve=False, missing=["referenced_metric"])

    result = FollowUpResolver(SequenceModel([response])).resolve(
        "换成那个指标。",
        _previous(),
        trace=TraceCollector(),
    )

    assert result.can_resolve is False
    assert result.missing_fields == ["referenced_metric"]
    assert result.resolved_query is None


def test_follow_up_new_filter_preserves_metric_dimension_time() -> None:
    previous = _previous()
    result = _resolve(_resolution())

    merged = merge_session_context(previous, result)

    assert merged.metrics == previous.metrics
    assert merged.dimensions == previous.dimensions
    assert merged.time_range == previous.time_range
    assert merged.filters == {"region": ["华南"]}


def test_follow_up_overrides_time_range() -> None:
    previous = _previous()
    response = _resolution(
        labels=["Q3"],
        filters={},
        inherited=["metrics", "dimensions", "analysis_goal"],
        overridden=["time_range"],
    )
    result = FollowUpResolver(SequenceModel([response])).resolve(
        "只看 Q3。",
        previous,
        trace=TraceCollector(),
    )

    assert merge_session_context(previous, result).time_range.labels == ["Q3"]


def test_follow_up_overrides_metric() -> None:
    previous = _previous()
    response = _resolution(
        metrics=["订单量"],
        filters={},
        inherited=["dimensions", "time_range", "analysis_goal"],
        overridden=["metrics"],
    )
    result = FollowUpResolver(SequenceModel([response])).resolve(
        "改看订单量。",
        previous,
        trace=TraceCollector(),
    )

    assert merge_session_context(previous, result).metrics == ["订单量"]


def test_consecutive_follow_up_override_preserves_filter() -> None:
    previous = _previous()
    south = _resolve(_resolution())
    after_south = merge_session_context(previous, south)
    q3_response = _resolution(
        labels=["Q3"],
        filters={"region": ["华南"]},
        inherited=["metrics", "dimensions", "filters", "analysis_goal"],
        overridden=["time_range"],
    )
    q3 = FollowUpResolver(SequenceModel([q3_response])).resolve(
        "只看 Q3。",
        after_south,
        trace=TraceCollector(),
    )

    final_context = merge_session_context(after_south, q3)
    assert final_context.metrics == ["GMV"]
    assert final_context.time_range.labels == ["Q3"]
    assert final_context.filters == {"region": ["华南"]}


def test_follow_up_without_context_fails_safely() -> None:
    store = SessionMemoryStore()
    blank = store.create("session-a")
    state = create_initial_state(
        "那华南地区呢？",
        previous_session=blank,
    )
    trace = TraceCollector(trace_id=state["trace_id"])
    resolver_model = SequenceModel([])

    result = prepare_session_turn(
        state,
        Planner(SequenceModel([_plan("follow_up")])),
        FollowUpResolver(resolver_model),
        store,
        trace=trace,
    )

    assert result.can_execute is False
    assert result.resolution.missing_fields == ["session_context"]
    assert resolver_model.calls == []


def test_ambiguous_follow_up_does_not_guess() -> None:
    previous = _previous()
    response = _resolution(can_resolve=False, missing=["requested_scope"])

    result = FollowUpResolver(SequenceModel([response])).resolve(
        "那呢？",
        previous,
        trace=TraceCollector(),
    )

    assert result.can_resolve is False
    assert result.resolved_query is None
    assert result.missing_fields == ["requested_scope"]


def test_new_topic_does_not_inherit_old_context() -> None:
    previous = _previous()
    store = SessionMemoryStore()
    store.save(previous)
    state = create_initial_state(
        "统计所有客户数量。",
        previous_session=previous,
    )
    resolver_model = SequenceModel([])

    result = prepare_session_turn(
        state,
        Planner(SequenceModel([_plan("single_query")])),
        FollowUpResolver(resolver_model),
        store,
        trace=TraceCollector(trace_id=state["trace_id"]),
    )

    assert result.can_execute is True
    assert state["resolved_query"] is None
    assert resolver_model.calls == []


def test_resolved_query_preserves_original_query() -> None:
    previous = _previous()
    store = SessionMemoryStore()
    store.save(previous)
    state = create_initial_state("那华南地区呢？", previous_session=previous)

    result = prepare_session_turn(
        state,
        Planner(
            SequenceModel([_plan("follow_up"), _plan("multi_step_analysis")])
        ),
        FollowUpResolver(SequenceModel([_resolution()])),
        store,
        trace=TraceCollector(trace_id=state["trace_id"]),
    )

    assert result.can_execute is True
    assert state["original_query"] == "那华南地区呢？"
    assert "华南" in state["resolved_query"]


def test_follow_up_replans_resolved_query() -> None:
    previous = _previous()
    store = SessionMemoryStore()
    store.save(previous)
    state = create_initial_state("那华南地区呢？", previous_session=previous)
    planner_model = SequenceModel(
        [_plan("follow_up"), _plan("multi_step_analysis")]
    )

    result = prepare_session_turn(
        state,
        Planner(planner_model),
        FollowUpResolver(SequenceModel([_resolution()])),
        store,
        trace=TraceCollector(trace_id=state["trace_id"]),
    )

    assert result.effective_result.intent.value == "multi_step_analysis"
    assert "华南" in planner_model.calls[1]["user_prompt"]


def test_second_planner_follow_up_stops_safely() -> None:
    previous = _previous()
    store = SessionMemoryStore()
    store.save(previous)
    state = create_initial_state("那华南地区呢？", previous_session=previous)

    result = prepare_session_turn(
        state,
        Planner(SequenceModel([_plan("follow_up"), _plan("follow_up")])),
        FollowUpResolver(SequenceModel([_resolution()])),
        store,
        trace=TraceCollector(trace_id=state["trace_id"]),
    )

    assert result.can_execute is False
    assert "still classified" in result.error


def test_turn_index_increments() -> None:
    previous = _previous()

    state = create_initial_state("下一轮", previous_session=previous)

    assert state["session_context"].turn_index == 2
    assert state["session_context"].turn_number == 2


def test_follow_up_trace_success() -> None:
    previous = _previous()
    trace = TraceCollector()

    FollowUpResolver(SequenceModel([_resolution()])).resolve(
        "那华南地区呢？",
        previous,
        trace=trace,
    )

    assert [event.event_type for event in trace.get_events()] == [
        EventType.FOLLOW_UP_RESOLUTION_STARTED,
        EventType.FOLLOW_UP_RESOLVED,
    ]


def test_follow_up_trace_failure() -> None:
    previous = _previous()
    trace = TraceCollector()
    response = _resolution(can_resolve=False, missing=["requested_scope"])

    FollowUpResolver(SequenceModel([response])).resolve(
        "那呢？",
        previous,
        trace=trace,
    )

    assert trace.get_events()[-1].event_type is EventType.FOLLOW_UP_RESOLUTION_FAILED


def test_follow_up_structured_output_retry_is_bounded() -> None:
    previous = _previous()
    model = SequenceModel(["not-json", _resolution()])

    result = FollowUpResolver(model).resolve(
        "那华南地区呢？",
        previous,
        trace=TraceCollector(),
    )

    assert result.can_resolve is True
    assert len(model.calls) == 2


def test_follow_up_invalid_output_stops_after_one_retry() -> None:
    previous = _previous()
    model = SequenceModel(["not-json", "still-not-json"])

    with pytest.raises(FollowUpResolutionError):
        FollowUpResolver(model).resolve(
            "那华南地区呢？",
            previous,
            trace=TraceCollector(),
        )

    assert len(model.calls) == 2
