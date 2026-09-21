from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import pytest

from datapilot.agent.analyst import Analyst, AnalystError
from datapilot.agent.evidence import (
    build_evidence_contract,
    build_final_evidence_pack,
    build_final_evidence_projection,
    evidence_projection_violations,
)
from datapilot.agent.planner import (
    PLANNER_RESPONSE_SCHEMA,
    parse_planner_response,
)
from datapilot.agent.state import (
    ReviewerResult,
    SQLResult,
    TaskItem,
    create_initial_state,
)
from datapilot.tools.integration import (
    mark_evidence_review_status,
    merge_correction_evidence,
    tool_result_to_sql_result,
)
from datapilot.tools.router import ToolRouter
from datapilot.tools.wren_tools import WrenQueryResult
from datapilot.tracing.trace import TraceCollector
from domains.media.runtime import build_media_tool_registry


PLAYBACK_BINDING = {
    "primary_metric": "playback_success_rate",
    "unit": "ratio",
    "aggregation_semantics": "sum(successful_sessions)/sum(session_count)",
    "supporting_fields": [
        "session_count",
        "successful_sessions",
        "failed_sessions",
    ],
}
FAILED_BINDING = {
    "primary_metric": "failed_sessions",
    "unit": "count",
    "aggregation_semantics": "sum(play_success=false)",
    "supporting_fields": ["session_count", "successful_sessions"],
}


def _roles(
    baselines: list[str],
    *,
    primary: str | None,
) -> dict[str, Any]:
    return {
        "comparison_target": "current_window",
        "baseline_windows": baselines,
        "primary_baseline": primary,
    }


def _rows(window: str) -> list[dict[str, Any]]:
    values = {
        "previous_day": ((18, 2, 0.90), (18, 2, 0.90), (18, 2, 0.90)),
        "previous_window": ((19, 1, 0.95), (19, 1, 0.95), (18, 2, 0.90)),
        "current_window": ((17, 3, 0.85), (10, 10, 0.50), (16, 4, 0.80)),
    }
    return [
        {
            "window_name": window,
            "region": "华南",
            "cdn": cdn,
            "session_count": 20,
            "successful_sessions": successful,
            "failed_sessions": failed,
            "playback_success_rate": rate,
        }
        for cdn, (successful, failed, rate) in zip(
            ("CDN-A", "CDN-B", "CDN-C"),
            values[window],
            strict=True,
        )
    ]


def _bound_result(
    task_id: str,
    windows: list[str],
    *,
    binding: Mapping[str, Any] = PLAYBACK_BINDING,
    roles: Mapping[str, Any] | None = None,
    region: str = "华南",
    review_status: str = "approve",
    execution_source: str = "sql",
) -> SQLResult:
    rows = [row for window in windows for row in _rows(window)]
    result = SQLResult(
        task_id=task_id,
        sql="synthetic reviewed query",
        columns=list(rows[0]),
        rows=rows,
        row_count=len(rows),
        semantic_retry_count=1 if execution_source == "sql" else 0,
        execution_source=execution_source,  # type: ignore[arg-type]
        tool_name="query_qoe_metrics" if execution_source == "tool" else None,
        tool_input={
            "windows": list(windows),
            "region": region,
            "group_by": ["window_name", "region", "cdn"],
            "primary_metric": binding["primary_metric"],
        },
        tool_metadata={
            "metric_binding": deepcopy(dict(binding)),
            "requested_dimensions": ["window_name", "region", "cdn"],
            "window_role_binding": deepcopy(dict(roles or {})),
            "dimensions": ["window_name", "region", "cdn"],
            "review_status": review_status,
        },
    )
    return result


def _comparisons(results: list[SQLResult]) -> list[dict[str, Any]]:
    return build_evidence_contract(results)["comparisons"]


def _primary(comparisons: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [item for item in comparisons if item["is_primary_comparison"]]
    return matches[0] if len(matches) == 1 else None


def _analysis_state(
    left: SQLResult,
    right: SQLResult,
) -> tuple[dict[str, Any], TaskItem]:
    state = create_initial_state("Compare the bound metric by CDN")
    left_task = TaskItem(left.task_id, "baseline", "query", status="completed")
    right_task = TaskItem(right.task_id, "current", "query", status="completed")
    analysis = TaskItem(
        "analysis",
        "Compare the bound metric.",
        "analysis",
        [left.task_id, right.task_id],
    )
    state["task_plan"] = [left_task, right_task, analysis]
    state["completed_tasks"] = [left_task, right_task]
    state["pending_tasks"] = [analysis]
    state["current_task"] = analysis
    state["sql_results"] = [left, right]
    state["review_results"] = [
        ReviewerResult(left.task_id, "approve", "verified", confidence=1.0),
        ReviewerResult(right.task_id, "approve", "verified", confidence=1.0),
    ]
    return state, analysis


class _NoModelCalls:
    def complete(self, **kwargs: Any) -> str:
        del kwargs
        raise AssertionError("the bound deterministic comparison must not call a model")


class _QoEWren:
    def dry_plan(self, sql: str) -> str:
        return f"planned:{sql}"

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        del sql, limit
        rows = _rows("current_window")
        return WrenQueryResult(columns=list(rows[0]), rows=rows, row_count=len(rows))


def test_planner_parses_explicit_metric_and_window_roles() -> None:
    payload = {
        "intent": "single_query",
        "reason_summary": "Use an explicitly bound playback comparison.",
        "tasks": [
            {
                "task_id": "q1",
                "description": "Query playback success rate by CDN.",
                "task_type": "query",
                "depends_on": [],
                "status": "pending",
                "metric_binding": PLAYBACK_BINDING,
                "requested_dimensions": ["window_name", "region", "cdn"],
                "window_role_binding": _roles(
                    ["previous_day", "previous_window"],
                    primary="previous_window",
                ),
            }
        ],
        "requires_database": True,
        "requires_context": True,
        "is_follow_up": False,
    }

    task = parse_planner_response(json.dumps(payload, ensure_ascii=False)).tasks[0]
    properties = PLANNER_RESPONSE_SCHEMA["properties"]["tasks"]["items"][
        "properties"
    ]

    assert task.metric_binding == PLAYBACK_BINDING
    assert task.requested_dimensions == ["window_name", "region", "cdn"]
    assert task.window_role_binding["primary_baseline"] == "previous_window"
    assert {"metric_binding", "requested_dimensions", "window_role_binding"} <= set(
        properties
    )
    assert {"metric_binding", "requested_dimensions", "window_role_binding"} <= set(
        PLANNER_RESPONSE_SCHEMA["properties"]["tasks"]["items"]["required"]
    )


@pytest.mark.parametrize(
    ("binding", "expected_metric"),
    [
        (PLAYBACK_BINDING, "playback_success_rate"),
        (FAILED_BINDING, "failed_sessions"),
    ],
)
def test_analyst_uses_primary_binding_with_multiple_numeric_columns(
    binding: Mapping[str, Any],
    expected_metric: str,
) -> None:
    roles = _roles(["previous_window"], primary="previous_window")
    baseline = _bound_result(
        "baseline",
        ["previous_window"],
        binding=binding,
        roles=roles,
    )
    current = _bound_result(
        "current",
        ["current_window"],
        binding=binding,
        roles=roles,
    )
    state, analysis = _analysis_state(baseline, current)

    result = Analyst(model_client=_NoModelCalls()).execute_task(
        state,
        analysis,
        trace=TraceCollector(trace_id=state["trace_id"]),
    )

    comparison = result.derived_values["normalized_comparisons"][0]
    assert comparison["metric"] == expected_metric
    assert result.success is True


def test_bound_metric_missing_from_rows_fails_closed() -> None:
    roles = _roles(["previous_window"], primary="previous_window")
    baseline = _bound_result("baseline", ["previous_window"], roles=roles)
    current = _bound_result("current", ["current_window"], roles=roles)
    current.columns.remove("playback_success_rate")
    for row in current.rows:
        row.pop("playback_success_rate")
    state, analysis = _analysis_state(baseline, current)

    with pytest.raises(AnalystError, match="bound primary metric"):
        Analyst(model_client=_NoModelCalls()).execute_task(
            state,
            analysis,
            trace=TraceCollector(trace_id=state["trace_id"]),
        )


def test_legacy_unbound_single_metric_remains_valid() -> None:
    left = SQLResult(
        "baseline",
        "SELECT cdn, value FROM baseline",
        columns=["cdn", "value"],
        rows=[{"cdn": "CDN-A", "value": 10}],
        row_count=1,
    )
    right = SQLResult(
        "current",
        "SELECT cdn, value FROM current",
        columns=["cdn", "value"],
        rows=[{"cdn": "CDN-A", "value": 8}],
        row_count=1,
    )
    state, analysis = _analysis_state(left, right)

    result = Analyst(model_client=_NoModelCalls()).execute_task(
        state,
        analysis,
        trace=TraceCollector(trace_id=state["trace_id"]),
    )

    assert result.derived_values["comparison"][0]["difference"] == -2


def test_tool_and_adapter_preserve_authoritative_metric_binding() -> None:
    router = ToolRouter(build_media_tool_registry(_QoEWren()))
    decision = router.route(
        "Query failed sessions in the current window by CDN.",
        metric_binding=FAILED_BINDING,
    )
    tool_result = router.registry.execute(
        decision.tool_name or "",
        decision.arguments,
    )
    task = TaskItem(
        "qoe",
        "Query failed sessions.",
        "query",
        metric_binding=deepcopy(FAILED_BINDING),
        requested_dimensions=["cdn"],
        window_role_binding=_roles(["previous_window"], primary="previous_window"),
    )
    adapted = tool_result_to_sql_result(task, tool_result)

    assert decision.arguments["primary_metric"] == "failed_sessions"
    assert tool_result.metadata["metric_binding"] == FAILED_BINDING
    assert adapted.tool_metadata["metric_binding"] == FAILED_BINDING
    assert adapted.tool_metadata["requested_dimensions"] == ["cdn"]
    assert adapted.tool_metadata["window_role_binding"] == task.window_role_binding


def test_sql_correction_and_reviewed_lineage_preserve_query_semantics() -> None:
    roles = _roles(["previous_window"], primary="previous_window")
    base = _bound_result(
        "qoe",
        ["previous_window"],
        roles=roles,
        execution_source="tool",
    )
    correction = SQLResult(
        "qoe",
        "SELECT corrected",
        columns=list(base.columns),
        rows=deepcopy(base.rows),
        row_count=base.row_count,
        semantic_retry_count=1,
    )

    merged = merge_correction_evidence(base, correction)
    mark_evidence_review_status(merged, "approve")
    lineage = build_final_evidence_pack([merged], [])["reviewed_evidence"][0]

    assert merged.tool_metadata["metric_binding"] == PLAYBACK_BINDING
    assert merged.tool_metadata["requested_dimensions"] == [
        "window_name",
        "region",
        "cdn",
    ]
    assert merged.tool_metadata["window_role_binding"] == roles
    assert lineage["metric_binding"] == PLAYBACK_BINDING
    assert lineage["window_role_binding"] == roles
    assert lineage["final_review_status"] == "approve"


def test_shape_a_single_task_uses_explicit_primary_baseline() -> None:
    roles = _roles(
        ["previous_day", "previous_window"],
        primary="previous_window",
    )
    result = _bound_result(
        "combined",
        ["previous_day", "previous_window", "current_window"],
        roles=roles,
    )

    comparisons = _comparisons([result])
    primary = _primary(comparisons)

    assert len(comparisons) == 2
    assert primary is not None
    assert primary["baseline_window"] == "previous_window"
    assert primary["largest_decline_group"] == "CDN-B"


def test_shape_b_split_tasks_matches_single_task_primary_values() -> None:
    roles = _roles(["previous_window"], primary="previous_window")
    baseline = _bound_result("baseline", ["previous_window"], roles=roles)
    current = _bound_result("current", ["current_window"], roles=roles)
    split = _primary(_comparisons([baseline, current]))
    combined = _primary(
        _comparisons(
            [
                _bound_result(
                    "combined",
                    ["previous_window", "current_window"],
                    roles=roles,
                )
            ]
        )
    )

    assert split is not None and combined is not None
    fields = ("group", "baseline", "current", "delta")
    assert [
        {field: row[field] for field in fields} for row in split["groups"]
    ] == [{field: row[field] for field in fields} for row in combined["groups"]]


def test_shape_b2_multi_baseline_selects_explicit_primary() -> None:
    roles = _roles(
        ["previous_day", "previous_window"],
        primary="previous_window",
    )
    baseline = _bound_result(
        "q1_baseline",
        ["previous_day", "previous_window"],
        roles=roles,
    )
    current = _bound_result("q2_current", ["current_window"], roles=roles)

    comparisons = _comparisons([baseline, current])
    primary = _primary(comparisons)
    assert [item["baseline_window"] for item in comparisons] == [
        "previous_window",
        "previous_day",
    ]
    assert primary is not None
    deltas = {row["group"]: row["delta"] for row in primary["groups"]}
    assert deltas == pytest.approx(
        {"CDN-A": -0.10, "CDN-B": -0.45, "CDN-C": -0.10}
    )
    assert primary["largest_decline_group"] == "CDN-B"


def test_shape_b3_multi_baseline_never_silently_selects_primary() -> None:
    roles = _roles(["previous_day", "previous_window"], primary=None)
    baseline = _bound_result(
        "q1_baseline",
        ["previous_day", "previous_window"],
        roles=roles,
    )
    current = _bound_result("q2_current", ["current_window"], roles=roles)
    state, analysis = _analysis_state(baseline, current)

    comparisons = _comparisons([baseline, current])
    analyzed = Analyst(model_client=_NoModelCalls()).execute_task(
        state,
        analysis,
        trace=TraceCollector(trace_id=state["trace_id"]),
    )

    assert {item["baseline_window"] for item in comparisons} == {
        "previous_day",
        "previous_window",
    }
    assert _primary(comparisons) is None
    assert all(not item["allows_unique_baseline_claims"] for item in comparisons)
    assert analyzed.derived_values["primary_comparison_id"] is None
    assert "No unique primary baseline" in analyzed.summary


def test_multi_baseline_comparison_provenance_tracks_both_source_tasks() -> None:
    roles = _roles(
        ["previous_day", "previous_window"],
        primary="previous_window",
    )
    baseline = _bound_result("q1_baseline", list(roles["baseline_windows"]), roles=roles)
    current = _bound_result("q2_current", ["current_window"], roles=roles)

    comparison = _primary(_comparisons([baseline, current]))

    assert comparison is not None
    assert comparison["source_task_ids"] == ["q1_baseline", "q2_current"]
    assert comparison["baseline_source_id"].startswith(
        "source:q1_baseline:sql-correction"
    )
    assert comparison["current_source_id"].startswith(
        "source:q2_current:sql-correction"
    )


@pytest.mark.parametrize(
    "mutation",
    ["metric", "unit", "scope", "review"],
)
def test_cross_task_binding_guards_reject_incompatible_sources(mutation: str) -> None:
    roles = _roles(["previous_window"], primary="previous_window")
    baseline = _bound_result("baseline", ["previous_window"], roles=roles)
    current = _bound_result("current", ["current_window"], roles=roles)
    if mutation == "metric":
        current.tool_metadata["metric_binding"] = deepcopy(FAILED_BINDING)
    elif mutation == "unit":
        current.tool_metadata["metric_binding"]["unit"] = "percent"
    elif mutation == "scope":
        current.tool_input["region"] = "华北"
    else:
        current.tool_metadata["review_status"] = "retry"

    assert _comparisons([baseline, current]) == []


def test_phase37_projection_stays_bounded_with_two_comparison_sets() -> None:
    roles = _roles(["previous_day", "previous_window"], primary=None)
    baseline = _bound_result(
        "q1_baseline",
        ["previous_day", "previous_window"],
        roles=roles,
    )
    current = _bound_result("q2_current", ["current_window"], roles=roles)
    pack = build_final_evidence_pack([baseline, current], [])

    projection, stats = build_final_evidence_projection(
        pack,
        max_chars=12_000,
    )

    assert evidence_projection_violations(projection, pack) == []
    assert stats["projected_evidence_chars"] <= stats["max_projection_chars"]
    assert len(
        {
            item["facts"]["baseline_window"]
            for item in pack["data_evidence"]
            if item["kind"] == "group_comparison"
        }
    ) == 2


def test_phase38_all_four_shapes_complete_offline_with_real_correction_replay() -> None:
    from evals.media.run_tool_offline_cases import (  # noqa: PLC0415
        _run_composite,
        _runtime,
    )

    wren, router = _runtime()
    results = {
        shape: _run_composite(wren, router, planner_shape=shape)
        for shape in (
            "single_task",
            "split_tasks",
            "multi_baseline_primary",
            "multi_baseline_no_primary",
        )
    }

    for result in results.values():
        stats = result["evidence_projection_stats"]
        assert result["success"] is True
        assert result["memory_updated"] is True
        assert result["validator_result"]["valid"] is True
        assert result["validator_result"]["violations"] == []
        assert stats["final_prompt_chars"] <= stats["budget_limit"] == 20_000
        assert stats["projected_evidence_chars"] <= stats[
            "available_evidence_budget"
        ]
        assert all(
            heading in result["final_answer"]
            for heading in (
                "DATA EVIDENCE",
                "KNOWLEDGE EVIDENCE",
                "INFERENCE",
                "LIMITATION",
            )
        )

    primary = results["multi_baseline_primary"]
    comparisons = [
        item
        for item in primary["evidence_pack"]["data_evidence"]
        if item["kind"] == "group_comparison"
    ]
    primary_groups = [
        item for item in comparisons if item["facts"]["is_primary_comparison"]
    ]
    lineage = {
        item["task_id"]: item for item in primary["evidence_pack"]["reviewed_evidence"]
    }

    assert len(primary_groups) == 3
    assert {item["facts"]["baseline_window"] for item in primary_groups} == {
        "previous_window"
    }
    assert {
        item["facts"]["group"]: item["facts"]["delta"]
        for item in primary_groups
    } == pytest.approx({"CDN-A": -0.10, "CDN-B": -0.45, "CDN-C": -0.10})
    assert primary["reviewer_decisions"].count("retry") == 3
    assert primary["model_calls"].count("sql_agent") == 3
    assert lineage["q1_baseline_playback"]["metric_binding"] == PLAYBACK_BINDING
    assert {
        source["role"]
        for source in lineage["q1_baseline_playback"]["evidence_sources"]
    } == {"base_tool_evidence", "correction_evidence"}

    no_primary = results["multi_baseline_no_primary"]
    no_primary_groups = [
        item
        for item in no_primary["evidence_pack"]["data_evidence"]
        if item["kind"] == "group_comparison"
    ]
    assert {item["facts"]["baseline_window"] for item in no_primary_groups} == {
        "previous_day",
        "previous_window",
    }
    assert all(
        item["facts"]["is_primary_comparison"] is False
        and item["facts"]["allows_unique_baseline_claims"] is False
        for item in no_primary_groups
    )
