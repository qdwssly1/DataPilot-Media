from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from datapilot.tools.router import ToolRouter
from datapilot.tools.wren_tools import WrenQueryResult
from domains.media.runtime import build_media_tool_registry


class EmptyWren:
    def dry_plan(self, sql: str) -> str:
        return sql

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        del sql, limit
        return WrenQueryResult(columns=[], rows=[], row_count=0)


class SequenceRoutingModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        del system_prompt, response_schema
        self.calls.append(user_prompt)
        return self.responses.pop(0)


@pytest.mark.parametrize(
    ("description", "tool_name", "expected"),
    [
        (
            "查询华南当前窗口播放成功率和卡顿情况。",
            "query_qoe_metrics",
            {
                "windows": ["current_window"],
                "region": "华南",
                "primary_metric": "playback_success_rate",
            },
        ),
        (
            "华南 CDN-B 当前有哪些 E302 高等级告警？",
            "get_alarm_events",
            {
                "windows": ["current_window"],
                "region": "华南",
                "cdn": "CDN-B",
                "error_code": "E302",
                "severity": "high",
            },
        ),
        (
            "检查 CDN-B 同期是否存在 upstream timeout 相关日志。",
            "query_logs",
            {
                "windows": ["current_window"],
                "cdn": "CDN-B",
                "message_contains": "upstream timeout",
            },
        ),
        (
            "最近是否存在失败的 H.265 转码任务？",
            "get_transcode_status",
            {
                "windows": ["current_window"],
                "codec": "H.265",
                "status": "failed",
            },
        ),
    ],
)
def test_deterministic_media_routes(
    description: str,
    tool_name: str,
    expected: dict[str, Any],
) -> None:
    router = ToolRouter(build_media_tool_registry(EmptyWren()))

    decision = router.route(description)

    assert decision.route == "tool"
    assert decision.tool_name == tool_name
    assert decision.arguments == expected
    assert decision.deterministic is True


def test_unmatched_task_falls_back_to_sql_without_model() -> None:
    router = ToolRouter(build_media_tool_registry(EmptyWren()))

    decision = router.route("按自定义业务标签汇总收入")

    assert decision.route == "sql"
    assert decision.tool_name is None
    assert decision.arguments == {}


def test_real_decline_task_routes_to_paired_qoe_comparison() -> None:
    router = ToolRouter(build_media_tool_registry(EmptyWren()))
    description = (
        "From stream_sessions, filter region='华南' and "
        "window_name='current_window', group by cdn, and return "
        "playback_success_rate to identify which CDN contributes the largest "
        "weighted share of the success-rate decline."
    )

    decision = router.route(description)

    assert decision.route == "tool"
    assert decision.tool_name == "query_qoe_metrics"
    assert decision.arguments == {
        "windows": ["previous_window", "current_window"],
        "region": "华南",
        "group_by": ["window_name", "cdn"],
        "primary_metric": "playback_success_rate",
    }


def test_planner_grouping_contract_passes_every_required_qoe_dimension() -> None:
    router = ToolRouter(build_media_tool_registry(EmptyWren()))
    description = (
        "Query stream_sessions for region = '华南' grouped by window_name "
        "(previous_window as baseline and current_window), cdn, and device; "
        "return session_count and playback_success_rate per group."
    )

    decision = router.route(description)

    assert decision.route == "tool"
    assert decision.tool_name == "query_qoe_metrics"
    assert decision.arguments == {
        "windows": ["previous_window", "current_window"],
        "region": "华南",
        "group_by": ["window_name", "cdn", "device"],
        "primary_metric": "playback_success_rate",
    }


@pytest.mark.parametrize(
    "description",
    [
        "在一个查询里同时返回播放成功率、告警和日志明细。",
        "删除当前窗口的 E302 日志。",
    ],
)
def test_ambiguous_or_mutating_task_fails_safe_to_sql(
    description: str,
) -> None:
    router = ToolRouter(build_media_tool_registry(EmptyWren()))

    decision = router.route(description)

    assert decision.route == "sql"
    assert decision.tool_name is None
    assert decision.arguments == {}


def test_malformed_or_unknown_model_routes_retry_once_then_fail_safe() -> None:
    model = SequenceRoutingModel(
        [
            "not-json",
            json.dumps(
                {
                    "route": "tool",
                    "tool_name": "not_registered",
                    "arguments": {},
                    "reason": "invented",
                }
            ),
        ]
    )
    router = ToolRouter(
        build_media_tool_registry(EmptyWren()),
        model_client=model,
        max_output_retries=1,
    )

    decision = router.route("custom unsupported analytical slice")

    assert decision.route == "sql"
    assert decision.model_attempts == 2
    assert len(model.calls) == 2


def test_model_selected_tool_arguments_still_require_registry_validation() -> None:
    model = SequenceRoutingModel(
        [
            json.dumps(
                {
                    "route": "tool",
                    "tool_name": "query_qoe_metrics",
                    "arguments": {"region": "不存在区域"},
                    "reason": "QoE task",
                }
            )
        ]
    )
    registry = build_media_tool_registry(EmptyWren())
    router = ToolRouter(registry, model_client=model, max_output_retries=0)

    decision = router.route("custom metric request without known keywords")
    result = registry.execute(decision.tool_name or "", decision.arguments)

    assert decision.route == "tool"
    assert result.success is False
    assert result.metadata["error_type"] == "invalid_arguments"
