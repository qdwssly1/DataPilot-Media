"""Exercise the Media MCP server over a real stdio client session."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from time import perf_counter
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "domains" / "media"


def _safe_subprocess_environment() -> dict[str, str]:
    """Pass runtime paths only; never forward LLM or credential variables."""

    allowed = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
    )
    environment = {
        key: os.environ[key] for key in allowed if key in os.environ
    }
    environment.update(
        {
            "WREN_PROJECT_PATH": str(MEDIA.resolve()),
            "WREN_PROFILE": "datapilot_media_duckdb",
            "MEDIA_DUCKDB_DIR": str((MEDIA / "data").resolve()),
            "WREN_HOME": str((MEDIA / ".wren").resolve()),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return environment


def _failure_reason(result: Any) -> str | None:
    if not result.isError:
        return None
    for content in result.content:
        text = getattr(content, "text", None)
        if isinstance(text, str) and text.strip():
            return " ".join(text.split())[:240]
    return "MCP tool call failed"


async def run_smoke() -> dict[str, Any]:
    """Run list_tools plus success, invalid, unknown, and empty calls."""

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "domains.media.runtime.mcp_server"],
        cwd=ROOT,
        env=_safe_subprocess_environment(),
    )
    calls = [
        (
            "query_qoe_metrics",
            {"windows": ["current_window"], "region": "华南"},
            "success",
        ),
        (
            "get_alarm_events",
            {
                "windows": ["current_window"],
                "region": "华南",
                "cdn": "CDN-B",
                "error_code": "E302",
                "severity": "high",
            },
            "success",
        ),
        (
            "query_logs",
            {
                "windows": ["current_window"],
                "cdn": "CDN-B",
                "message_contains": "upstream timeout",
            },
            "success",
        ),
        (
            "get_transcode_status",
            {
                "windows": ["current_window"],
                "codec": "H.265",
                "status": "failed",
            },
            "success",
        ),
        ("get_alarm_events", {"region": "不存在"}, "invalid_args"),
        ("tool_does_not_exist", {}, "unknown_tool"),
        ("query_logs", {"trace_id": "trace-does-not-exist"}, "empty"),
    ]
    details: list[dict[str, Any]] = []
    session_started = perf_counter()
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            list_started = perf_counter()
            listed = await session.list_tools()
            list_latency = (perf_counter() - list_started) * 1000
            for name, arguments, expectation in calls:
                started = perf_counter()
                result = await session.call_tool(name, arguments)
                latency = (perf_counter() - started) * 1000
                payload = result.structuredContent or {}
                data = payload.get("data") if isinstance(payload, dict) else None
                application_success = (
                    payload.get("success") if isinstance(payload, dict) else None
                )
                if expectation == "success":
                    passed = not result.isError and payload.get("success") is True
                elif expectation in {"invalid_args", "unknown_tool"}:
                    passed = result.isError or payload.get("success") is False
                else:
                    passed = (
                        not result.isError
                        and payload.get("success") is True
                        and data == []
                    )
                details.append(
                    {
                        "tool": name,
                        "expectation": expectation,
                        "passed": passed,
                        "mcp_error": result.isError,
                        "application_success": application_success,
                        "row_count": len(data) if isinstance(data, list) else None,
                        "latency_ms": latency,
                        "failure_reason": _failure_reason(result)
                        or (
                            str(payload.get("error"))[:240]
                            if isinstance(payload, dict) and payload.get("error")
                            else None
                        ),
                    }
                )

    expected_tools = {
        "query_qoe_metrics",
        "get_alarm_events",
        "query_logs",
        "get_transcode_status",
    }
    listed_names = {tool.name for tool in listed.tools}
    return {
        "success": listed_names == expected_tools
        and all(item["passed"] for item in details),
        "list_tools": {
            "passed": listed_names == expected_tools,
            "tools": sorted(listed_names),
            "latency_ms": list_latency,
        },
        "calls": details,
        "total_latency_ms": (perf_counter() - session_started) * 1000,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_smoke()), ensure_ascii=False, indent=2))
