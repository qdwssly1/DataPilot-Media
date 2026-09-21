from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("mcp")

from datapilot.tools.wren_tools import WrenQueryResult
from domains.media.runtime import build_media_tool_registry
from domains.media.runtime.mcp_server import build_server


class MCPFixtureWren:
    def dry_plan(self, sql: str) -> str:
        return sql

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        del limit
        if "alarm_events" in sql:
            rows: list[dict[str, Any]] = [
                {"alarm_id": "a1", "status": "open"},
                {"alarm_id": "a2", "status": "resolved"},
            ]
        elif "stream_sessions" in sql:
            rows = [{"session_count": 60, "playback_success_rate": 0.7167}]
        elif "log_events" in sql and "does-not-exist" not in sql:
            rows = [{"trace_id": "trace-e302-001", "error_code": "E302"}]
        elif "transcode_jobs" in sql:
            rows = [{"job_id": "job-cur-001", "status": "failed"}]
        else:
            rows = []
        return WrenQueryResult(
            columns=list(rows[0]) if rows else [],
            rows=rows,
            row_count=len(rows),
        )


def _server() -> Any:
    return build_server(build_media_tool_registry(MCPFixtureWren()))


def _handler(server: Any, name: str) -> Any:
    return server._tool_manager._tools[name].fn


def test_mcp_lists_only_four_read_only_media_tools() -> None:
    server = _server()

    assert set(server._tool_manager._tools) == {
        "query_qoe_metrics",
        "get_alarm_events",
        "query_logs",
        "get_transcode_status",
    }
    for tool in server._tool_manager._tools.values():
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.parameters["additionalProperties"] is False


def test_mcp_handlers_share_registry_implementations() -> None:
    server = _server()

    qoe = _handler(server, "query_qoe_metrics")(
        windows=["current_window"], region="华南"
    )
    alarms = _handler(server, "get_alarm_events")(
        windows=["current_window"], error_code="E302"
    )
    logs = _handler(server, "query_logs")(
        windows=["current_window"], error_code="E302"
    )
    jobs = _handler(server, "get_transcode_status")(
        windows=["current_window"], codec="H.265", status="failed"
    )

    assert qoe["success"] is True
    assert alarms["metadata"]["status_distribution"] == {
        "open": 1,
        "resolved": 1,
    }
    assert logs["data"][0]["trace_id"] == "trace-e302-001"
    assert jobs["data"][0]["status"] == "failed"


def test_mcp_invalid_arguments_and_empty_results_are_structured() -> None:
    server = _server()

    invalid = _handler(server, "get_alarm_events")(region="不存在")
    empty = _handler(server, "query_logs")(trace_id="trace-does-not-exist")

    assert invalid["success"] is False
    assert invalid["metadata"]["error_type"] == "invalid_arguments"
    assert empty["success"] is True
    assert empty["data"] == []
