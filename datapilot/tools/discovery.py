"""Small domain-tool discovery boundary for configured Wren capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from datapilot.tools.contracts import ToolRegistry


_MEDIA_ENTITIES = {"stream_sessions", "alarm_events", "log_events", "transcode_jobs"}


def build_domain_tool_registry(
    wren_tools: Any,
    planning_context: Mapping[str, Any] | None,
) -> ToolRegistry | None:
    """Build tools only when the active schema advertises the Media fixture."""

    if not isinstance(planning_context, Mapping):
        return None
    raw_entities = planning_context.get("entities", [])
    names = {
        item.get("name")
        for item in raw_entities
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    if not _MEDIA_ENTITIES <= names:
        return None
    try:
        from domains.media.runtime import build_media_tool_registry  # noqa: PLC0415

        return build_media_tool_registry(wren_tools)
    except Exception:
        return None
