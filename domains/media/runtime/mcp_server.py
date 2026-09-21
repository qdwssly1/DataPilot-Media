"""Minimal read-only MCP stdio adapter for the shared Media Tool Registry."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from datapilot.tools.contracts import ToolRegistry
from datapilot.tools.wren_tools import WrenToolAdapter
from domains.media.runtime.tools import (
    AlarmSeverity,
    AlarmStatus,
    CDN,
    Codec,
    Device,
    LogLevel,
    Region,
    TranscodeStatus,
    WindowName,
    build_media_tool_registry,
)

READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _arguments(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in values.items()
        if value is not None and key != "registry"
    }


def _call(
    registry: ToolRegistry,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Delegate validation and execution to the in-process implementation."""

    result = registry.execute(tool_name, arguments)
    return result.model_dump(mode="json")


def build_server(registry: ToolRegistry) -> FastMCP:
    """Expose only the four registered Media read-only tools over MCP."""

    required = {
        "query_qoe_metrics",
        "get_alarm_events",
        "query_logs",
        "get_transcode_status",
    }
    if set(registry.names()) != required:
        raise ValueError("Media MCP requires exactly the approved read-only tools")

    server = FastMCP("datapilot-media")

    @server.tool(
        name="query_qoe_metrics",
        description=(
            "Query bounded playback success, startup, buffering, rebuffer, "
            "and failed-session metrics from the synthetic Media domain."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def query_qoe_metrics(
        windows: list[WindowName] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        region: Region | None = None,
        cdn: CDN | None = None,
        device: Device | None = None,
        group_by: list[
            Literal["window_name", "region", "cdn", "device"]
        ]
        | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return _call(registry, "query_qoe_metrics", _arguments(locals()))

    @server.tool(
        name="get_alarm_events",
        description=(
            "Return individual synthetic Media alarms with an exact mixed-status "
            "distribution; no categorical MIN/MAX aggregation is used."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_alarm_events(
        windows: list[WindowName] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        region: Region | None = None,
        cdn: CDN | None = None,
        error_code: str | None = None,
        severity: AlarmSeverity | None = None,
        status: AlarmStatus | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return _call(registry, "get_alarm_events", _arguments(locals()))

    @server.tool(
        name="query_logs",
        description=(
            "Query only deterministic structured Media logs with bounded filters; "
            "this does not connect to a production log platform."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def query_logs(
        windows: list[WindowName] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        region: Region | None = None,
        cdn: CDN | None = None,
        service: Literal["player-api", "cdn-gateway", "origin-proxy"]
        | None = None,
        level: LogLevel | None = None,
        error_code: str | None = None,
        trace_id: str | None = None,
        message_contains: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return _call(registry, "query_logs", _arguments(locals()))

    @server.tool(
        name="get_transcode_status",
        description=(
            "Return deterministic synthetic transcode status records; it never "
            "invokes FFmpeg or a production transcode service."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_transcode_status(
        windows: list[WindowName] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        region: Region | None = None,
        codec: Codec | None = None,
        status: TranscodeStatus | None = None,
        error_code: str | None = None,
        gpu_pool: Literal["gpu-a", "gpu-b", "gpu-c"] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return _call(registry, "get_transcode_status", _arguments(locals()))

    # JSON Schema permits additional properties by default. FastMCP builds the
    # callable schemas but does not emit this keyword, so make the MCP catalog
    # match the registry's strict ``extra=forbid`` contract explicitly.
    for tool in server._tool_manager._tools.values():
        tool.parameters["additionalProperties"] = False

    return server


def build_server_from_env(
    environ: Mapping[str, str] | None = None,
) -> FastMCP:
    """Build the stdio server from the same Wren adapter used by DataPilot."""

    values = os.environ if environ is None else environ
    project_value = values.get("WREN_PROJECT_PATH", "").strip()
    if not project_value:
        raise ValueError("WREN_PROJECT_PATH is required")
    project = Path(project_value).resolve()
    if not (project / "wren_project.yml").is_file():
        raise ValueError("WREN_PROJECT_PATH must identify a Wren project")
    local_wren_home = project / ".wren"
    if not (local_wren_home / "profiles.yml").is_file():
        raise ValueError("Media MCP requires a project-local Wren profile")
    # Resolve Wren's profile registry inside the selected project before the
    # SDK is imported. MCP tool inputs never accept a path and cannot alter it.
    os.environ["WREN_HOME"] = str(local_wren_home)
    safe_values = dict(values)
    safe_values["WREN_PROJECT_PATH"] = str(project)
    previous_directory = Path.cwd()
    try:
        # Wren's profile expansion checks CWD for a dotenv file. Initializing
        # from the domain root prevents the repository-level LLM .env from
        # being read by this data-only MCP process.
        os.chdir(project)
        wren_tools = WrenToolAdapter.from_env(safe_values)
    finally:
        os.chdir(previous_directory)
    return build_server(build_media_tool_registry(wren_tools))


def main() -> None:
    """Run the protocol adapter on stdio; stdout remains MCP-only."""

    build_server_from_env().run(transport="stdio")


if __name__ == "__main__":
    main()
