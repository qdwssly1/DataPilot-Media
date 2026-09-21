"""Run the three Phase 2 Media Agent scenarios against real configured services.

The script never prints credentials, prompts, model responses, or local source.
It reports only workflow artifacts already exposed by AgentState and trace events.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping

from dotenv import dotenv_values

from datapilot.agent.analyst import Analyst
from datapilot.agent.follow_up import FollowUpResolver, SessionContextExtractor
from datapilot.agent.graph import (
    commit_session_context,
    execute_task_plan,
    prepare_session_turn,
)
from datapilot.agent.planner import Planner
from datapilot.agent.reviewer import Reviewer
from datapilot.agent.sql_agent import SQLAgent
from datapilot.cli import process_input
from datapilot.llm.openai_compatible import OpenAICompatiblePlannerModel
from datapilot.memory.session_memory import SessionMemoryStore
from datapilot.retrieval import KnowledgeRetriever
from datapilot.retrieval.integration import retrieve_into_state
from datapilot.tools.wren_tools import WrenToolAdapter, try_fetch_planning_context
from datapilot.tracing.summary import summarize_trace

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "domains" / "media"

CASES = {
    "a": "E302 是什么？",
    "b": "华南地区播放成功率下降，并出现 E302 告警，应该怎么排查？",
    "c": "首帧耗时升高应该检查什么？",
}


class CountingModel:
    """Measure calls without retaining credentials, prompts, or raw responses."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _component(system_prompt: str) -> str:
        if "semantic reviewer" in system_prompt.lower():
            return "reviewer"
        if "task planner" in system_prompt:
            return "planner"
        if "SQL Agent" in system_prompt:
            return "sql_agent"
        if "final response writer" in system_prompt:
            return "final_answer"
        if "grounded Analyst" in system_prompt:
            return "analyst"
        if "follow-up" in system_prompt:
            return "follow_up"
        if "extract compact business context" in system_prompt:
            return "session_memory"
        return "other"

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        started_at = perf_counter()
        try:
            return self.delegate.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=response_schema,
            )
        finally:
            self.calls.append(
                {
                    "component": self._component(system_prompt),
                    "latency_ms": (perf_counter() - started_at) * 1000,
                }
            )


def _safe_environment() -> dict[str, str]:
    configured = dotenv_values(ROOT / ".env")
    allowed_keys = (
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_TIMEOUT_SECONDS",
    )
    values = {
        key: value
        for key in allowed_keys
        if isinstance((value := configured.get(key)), str)
    }
    values["WREN_PROJECT_PATH"] = str(MEDIA.resolve())
    values["WREN_PROFILE"] = "datapilot_media_duckdb"
    os.environ["MEDIA_DUCKDB_DIR"] = str((MEDIA / "data").resolve())
    os.environ["WREN_HOME"] = str((MEDIA / ".wren").resolve())
    return values


def run_case(
    case_id: str, query: str, *, reviewer_model: str | None = None,
    planner_model: str | None = None,
) -> dict[str, Any]:
    environment = _safe_environment()
    counting_model = CountingModel(
        OpenAICompatiblePlannerModel.from_env(environment)
    )
    review_counting_model = (
        CountingModel(replace(counting_model.delegate, model=reviewer_model))
        if reviewer_model else counting_model
    )
    plan_counting_model = (
        CountingModel(replace(counting_model.delegate, model=planner_model))
        if planner_model else counting_model
    )
    wren = WrenToolAdapter.from_project(
        MEDIA,
        profile=environment["WREN_PROFILE"],
    )
    retriever = KnowledgeRetriever.from_directory(MEDIA / "knowledge")
    planner = Planner(model_client=plan_counting_model)
    sql_agent = SQLAgent(model_client=counting_model, wren_tools=wren)
    reviewer = Reviewer(model_client=review_counting_model)
    analyst = Analyst(model_client=counting_model)
    resolver = FollowUpResolver(model_client=counting_model)
    extractor = SessionContextExtractor(model_client=counting_model)
    store = SessionMemoryStore()
    session_id = f"media-phase2-{case_id}"
    store.create(session_id)
    initialized = process_input(query, session_id=session_id)
    retrieval_results = retrieve_into_state(
        initialized.state,
        retriever,
        top_k=5,
    )
    started_at = perf_counter()
    try:
        planning = prepare_session_turn(
            initialized.state,
            planner,
            resolver,
            store,
            trace=initialized.trace,
            planning_context=try_fetch_planning_context(wren),
        )
        if not planning.can_execute or planning.effective_result is None:
            raise RuntimeError(planning.error or "planning could not execute")
        workflow = execute_task_plan(
            initialized.state,
            sql_agent,
            reviewer,
            analyst,
            trace=initialized.trace,
        )
        memory_updated = commit_session_context(
            initialized.state,
            planning.effective_result,
            workflow,
            store,
            extractor,
            trace=initialized.trace,
            resolution=planning.resolution,
        )
        summary = summarize_trace(initialized.trace.get_events())
        calls = list(counting_model.calls)
        if review_counting_model is not counting_model:
            calls.extend(review_counting_model.calls)
        if plan_counting_model is not counting_model:
            calls.extend(plan_counting_model.calls)
        return {
            "case": case_id,
            "query": query,
            "success": bool(workflow.final_answer_result and workflow.final_answer_result.success),
            "tasks": [asdict(task) for task in initialized.state["task_plan"]],
            "retrieval": {
                "calls": 1,
                "latency_ms": initialized.state["knowledge_retrieval_latency_ms"],
                "chunk_ids": [item["chunk_id"] for item in retrieval_results],
            },
            "sql_results": [asdict(item) for item in initialized.state["sql_results"]],
            "reviewer_results": [asdict(item) for item in initialized.state["review_results"]],
            "analysis_results": [asdict(item) for item in initialized.state["analysis_results"]],
            "final_answer": initialized.state["final_answer"],
            "memory_updated": memory_updated,
            "trace_summary": asdict(summary),
            "structured_output_retries": {
                name: sum(event.event_type.value == name for event in initialized.trace.get_events())
                for name in ("PLANNER_RETRY", "REVIEW_OUTPUT_RETRY", "ANALYST_OUTPUT_RETRY")
            },
            "llm": {
                "model": counting_model.delegate.model,
                "planner_model": plan_counting_model.delegate.model,
                "reviewer_model": review_counting_model.delegate.model,
                "calls": len(calls),
                "by_component": {
                    component: sum(
                        call["component"] == component for call in calls
                    )
                    for component in sorted(
                        {call["component"] for call in calls}
                    )
                },
                "total_latency_ms": sum(
                    call["latency_ms"] for call in calls
                ),
            },
            "wall_latency_ms": (perf_counter() - started_at) * 1000,
        }
    except Exception as exc:
        calls = list(counting_model.calls)
        if review_counting_model is not counting_model:
            calls.extend(review_counting_model.calls)
        if plan_counting_model is not counting_model:
            calls.extend(plan_counting_model.calls)
        return {
            "case": case_id,
            "query": query,
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "tasks": [asdict(task) for task in initialized.state["task_plan"]],
            "retrieval": {
                "calls": 1,
                "latency_ms": initialized.state["knowledge_retrieval_latency_ms"],
                "chunk_ids": [item["chunk_id"] for item in retrieval_results],
            },
            "sql_results": [asdict(item) for item in initialized.state["sql_results"]],
            "reviewer_results": [asdict(item) for item in initialized.state["review_results"]],
            "trace_summary": asdict(summarize_trace(initialized.trace.get_events())),
            "structured_output_retries": {
                name: sum(event.event_type.value == name for event in initialized.trace.get_events())
                for name in ("PLANNER_RETRY", "REVIEW_OUTPUT_RETRY", "ANALYST_OUTPUT_RETRY")
            },
            "llm": {
                "model": counting_model.delegate.model,
                "planner_model": plan_counting_model.delegate.model,
                "reviewer_model": review_counting_model.delegate.model,
                "calls": len(calls),
                "by_component": {
                    component: sum(
                        call["component"] == component for call in calls
                    )
                    for component in sorted(
                        {call["component"] for call in calls}
                    )
                },
            },
            "wall_latency_ms": (perf_counter() - started_at) * 1000,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["a", "b", "c", "all"], default="all")
    parser.add_argument("--reviewer-model", default=None)
    parser.add_argument("--planner-model", default=None)
    arguments = parser.parse_args()
    selected = CASES.items() if arguments.case == "all" else [(arguments.case, CASES[arguments.case])]
    report = {
        "model": _safe_environment().get("LLM_MODEL", "unknown"),
        "cases": [
            run_case(
                case_id, query, reviewer_model=arguments.reviewer_model,
                planner_model=arguments.planner_model,
            )
            for case_id, query in selected
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if all(case["success"] for case in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
