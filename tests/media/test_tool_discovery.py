from __future__ import annotations

from datapilot.tools.discovery import build_domain_tool_registry


class FakeWren:
    pass


def test_media_registry_is_discovered_only_for_complete_media_capabilities() -> None:
    context = {
        "entities": [
            {"name": "stream_sessions"},
            {"name": "alarm_events"},
            {"name": "log_events"},
            {"name": "transcode_jobs"},
        ]
    }

    registry = build_domain_tool_registry(FakeWren(), context)

    assert registry is not None
    assert set(registry.names()) == {
        "query_qoe_metrics",
        "get_alarm_events",
        "query_logs",
        "get_transcode_status",
    }


def test_non_media_or_missing_context_preserves_sql_only_behavior() -> None:
    assert build_domain_tool_registry(FakeWren(), None) is None
    assert build_domain_tool_registry(
        FakeWren(), {"entities": [{"name": "orders"}]}
    ) is None
