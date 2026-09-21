"""Run the deterministic Media Tool routing and execution golden set."""

from __future__ import annotations

import json
import os
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any

from datapilot.tools.router import ToolRouter
from datapilot.tools.wren_tools import WrenToolAdapter, try_fetch_planning_context
from domains.media.runtime import build_media_tool_registry

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "domains" / "media"


def run_evaluation() -> dict[str, Any]:
    """Evaluate the immutable golden expectations against real Wren/DuckDB."""

    cases = json.loads(
        (ROOT / "evals" / "media" / "tool_cases.json").read_text(
            encoding="utf-8"
        )
    )
    os.environ["MEDIA_DUCKDB_DIR"] = str((MEDIA / "data").resolve())
    wren = WrenToolAdapter.from_project(
        MEDIA,
        profile="datapilot_media_duckdb",
    )
    registry = build_media_tool_registry(wren)
    router = ToolRouter(
        registry,
        capability_context=try_fetch_planning_context(wren),
    )

    selection_correct = 0
    argument_correct = 0
    argument_cases = 0
    execution_success = 0
    execution_cases = 0
    sql_fallbacks = 0
    invalid_selections = 0
    router_latencies: list[float] = []
    execution_latencies: list[float] = []
    details: list[dict[str, Any]] = []

    for case in cases:
        decision = router.route(case["query"])
        router_latencies.append(decision.latency_ms)
        selected = decision.tool_name
        route_correct = (
            decision.route == case["expected_route"]
            and selected == case["expected_tool"]
        )
        selection_correct += int(route_correct)
        if decision.route == "sql":
            sql_fallbacks += 1
        if selected is not None and registry.get(selected) is None:
            invalid_selections += 1

        args_correct: bool | None = None
        tool_success: bool | None = None
        row_count: int | None = None
        error: str | None = None
        if case["expected_route"] == "tool":
            argument_cases += 1
            args_correct = decision.arguments == case["expected_arguments"]
            argument_correct += int(args_correct)
        if decision.route == "tool" and selected is not None:
            execution_cases += 1
            result = registry.execute(selected, decision.arguments)
            execution_success += int(result.success)
            execution_latencies.append(result.execution_time_ms)
            tool_success = result.success
            row_count = len(result.data)
            error = result.error

        details.append(
            {
                "id": case["id"],
                "expected_route": case["expected_route"],
                "actual_route": decision.route,
                "expected_tool": case["expected_tool"],
                "actual_tool": selected,
                "selection_correct": route_correct,
                "argument_correct": args_correct,
                "actual_arguments": decision.arguments,
                "tool_success": tool_success,
                "row_count": row_count,
                "error": error,
            }
        )

    total = len(cases)
    return {
        "cases": total,
        "tool_selection_accuracy": selection_correct / total,
        "tool_argument_accuracy": argument_correct / argument_cases,
        "tool_execution_success_rate": execution_success / execution_cases,
        "sql_fallback_rate": sql_fallbacks / total,
        "invalid_tool_selection_rate": invalid_selections / total,
        "counts": {
            "tool_routes": execution_cases,
            "sql_fallbacks": sql_fallbacks,
            "invalid_tool_selections": invalid_selections,
            "router_model_calls": 0,
        },
        "latency_ms": {
            "router_mean": mean(router_latencies),
            "router_max": max(router_latencies),
            "tool_execution_mean": mean(execution_latencies),
            "tool_execution_max": max(execution_latencies),
        },
        "details": details,
    }


if __name__ == "__main__":
    print(json.dumps(run_evaluation(), ensure_ascii=False, indent=2))
