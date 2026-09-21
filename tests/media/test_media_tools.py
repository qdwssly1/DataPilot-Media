from __future__ import annotations

from typing import Any

from datapilot.tools.wren_tools import WrenQueryResult
from domains.media.runtime import build_media_tool_registry
from domains.media.runtime.tools import build_alarm_evidence


class FixtureWren:
    def __init__(self) -> None:
        self.queries: list[tuple[str, int]] = []
        self.dry_plans: list[str] = []

    def dry_plan(self, sql: str) -> str:
        self.dry_plans.append(sql)
        return f"planned: {sql}"

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        self.queries.append((sql, limit))
        if "FROM stream_sessions" in sql:
            rows: list[dict[str, Any]] = [
                {
                    "session_count": 60,
                    "successful_sessions": 43,
                    "failed_sessions": 17,
                    "playback_success_rate": 43 / 60,
                    "average_startup_time_ms": 801.0,
                    "total_buffer_duration_seconds": 108.0,
                    "rebuffer_ratio": 0.0031,
                }
            ]
        elif "FROM alarm_events" in sql:
            rows = [
                {
                    "alarm_id": "a1",
                    "timestamp": "2026-09-01 11:05:00",
                    "window_name": "current_window",
                    "region": "华南",
                    "cdn": "CDN-B",
                    "error_code": "E302",
                    "severity": "high",
                    "status": "open",
                    "message": "Origin upstream timeout at edge 1.",
                },
                {
                    "alarm_id": "a2",
                    "timestamp": "2026-09-01 11:15:00",
                    "window_name": "current_window",
                    "region": "华南",
                    "cdn": "CDN-B",
                    "error_code": "E302",
                    "severity": "high",
                    "status": "investigating",
                    "message": "Origin upstream timeout at edge 2.",
                },
                {
                    "alarm_id": "a3",
                    "timestamp": "2026-09-01 11:25:00",
                    "window_name": "current_window",
                    "region": "华南",
                    "cdn": "CDN-B",
                    "error_code": "E302",
                    "severity": "high",
                    "status": "resolved",
                    "message": "Origin upstream timeout at edge 3.",
                },
            ]
        elif "FROM log_events" in sql:
            rows = [
                {
                    "trace_id": "trace-e302-001",
                    "error_code": "E302",
                    "message": "Origin upstream timeout.",
                }
            ]
        elif "FROM transcode_jobs" in sql:
            rows = [
                {
                    "job_id": "job-cur-001",
                    "codec": "H.265",
                    "status": "failed",
                }
            ]
        else:
            rows = []
        columns = list(rows[0]) if rows else []
        return WrenQueryResult(columns=columns, rows=rows, row_count=len(rows))


def test_qoe_tool_computes_all_required_metrics_with_bounded_filters() -> None:
    wren = FixtureWren()
    registry = build_media_tool_registry(wren)

    result = registry.execute(
        "query_qoe_metrics",
        {
            "windows": ["previous_window", "current_window"],
            "region": "华南",
            "group_by": ["window_name", "cdn"],
        },
    )

    assert result.success is True
    sql = result.metadata["executed_query"]
    assert "playback_success_rate" in sql
    assert "average_startup_time_ms" in sql
    assert "failed_sessions" in sql
    assert "SUM(buffer_duration) / NULLIF(SUM(playback_duration), 0)" in sql
    assert "region = '华南'" in sql
    assert result.metadata["rebuffer_contract"].startswith("SUM(buffer_duration)")


def test_alarm_tool_preserves_mixed_status_distribution() -> None:
    registry = build_media_tool_registry(FixtureWren())

    result = registry.execute(
        "get_alarm_events",
        {
            "windows": ["current_window"],
            "region": "华南",
            "cdn": "CDN-B",
            "error_code": "E302",
            "severity": "high",
        },
    )

    assert result.success is True
    assert result.metadata["status_distribution"] == {
        "open": 1,
        "investigating": 1,
        "resolved": 1,
    }
    assert "MAX(status)" not in result.metadata["executed_query"]
    assert "MIN(status)" not in result.metadata["executed_query"]
    assert "MAX(message)" not in result.metadata["executed_query"]
    assert "MIN(message)" not in result.metadata["executed_query"]
    assert [row["status"] for row in result.data] == [
        "open",
        "investigating",
        "resolved",
    ]

    evidence = result.metadata["alarm_evidence"]
    assert evidence["total_count"] == 3
    assert evidence["count_by_status"] == {
        "investigating": 1,
        "open": 1,
        "resolved": 1,
    }
    assert evidence["count_by_severity"] == {"high": 3}
    assert evidence["count_by_error_code"] == {"E302": 3}
    assert evidence["count_by_cdn"] == {"CDN-B": 3}
    assert evidence["affected_objects"]["cdn"] == ["CDN-B"]
    assert len(evidence["sample_events"]) == 3
    assert evidence["sample_semantics"] == "bounded_examples_not_distribution"


def test_raw_alarm_rows_build_a_bounded_deterministic_summary() -> None:
    rows = FixtureWren().query("SELECT * FROM alarm_events").rows

    evidence = build_alarm_evidence(rows, sample_limit=2)

    assert evidence["total_count"] == 3
    assert evidence["count_by_status"] == {
        "investigating": 1,
        "open": 1,
        "resolved": 1,
    }
    assert evidence["count_by_window"] == {"current_window": 3}
    assert len(evidence["sample_events"]) == 2
    assert [item["alarm_id"] for item in evidence["sample_events"]] == [
        "a1",
        "a2",
    ]
    assert all("message" in item for item in evidence["sample_events"])


def test_logs_and_transcode_tools_are_structured_and_read_only() -> None:
    registry = build_media_tool_registry(FixtureWren())

    logs = registry.execute(
        "query_logs",
        {
            "windows": ["current_window"],
            "cdn": "CDN-B",
            "error_code": "E302",
        },
    )
    jobs = registry.execute(
        "get_transcode_status",
        {
            "windows": ["current_window"],
            "codec": "H.265",
            "status": "failed",
        },
    )

    assert logs.success is True
    assert logs.data[0]["trace_id"] == "trace-e302-001"
    assert jobs.success is True
    assert jobs.metadata["status_distribution"] == {"failed": 1}
    assert all(item["read_only"] for item in registry.catalog())


def test_invalid_filter_never_reaches_wren() -> None:
    wren = FixtureWren()
    registry = build_media_tool_registry(wren)

    result = registry.execute(
        "query_logs",
        {"region": "华南'; DROP TABLE log_events; --"},
    )

    assert result.success is False
    assert result.error == "invalid tool arguments"
    assert wren.dry_plans == []
    assert wren.queries == []


class EmptyWren(FixtureWren):
    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        self.queries.append((sql, limit))
        return WrenQueryResult(columns=[], rows=[], row_count=0)


def test_empty_result_is_successful_and_explicit() -> None:
    result = build_media_tool_registry(EmptyWren()).execute(
        "query_logs",
        {"trace_id": "trace-does-not-exist"},
    )

    assert result.success is True
    assert result.data == []
    assert result.metadata["row_count"] == 0
    assert result.summary.startswith("No structured log rows")
