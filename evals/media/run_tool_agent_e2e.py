"""Run the authorized Phase 3 DeepSeek + Wren + RAG + Tool E2E case.

The script requires an explicit command-line acknowledgement. It never prints
credentials, prompts, raw model responses, environment contents, or local code.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, replace
from pathlib import Path
from time import perf_counter
from typing import Any

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
from datapilot.tools.router import ToolRouter
from datapilot.tools.wren_tools import WrenToolAdapter, try_fetch_planning_context
from datapilot.tracing.summary import summarize_trace
from domains.media.runtime import build_media_tool_registry
from evals.media.run_agent_e2e import CountingModel, MEDIA, _safe_environment

QUERY = (
    "华南播放成功率下降并出现 E302，结合告警、日志和知识库分析原因，"
    "并给出排查建议。"
)
REPORT_PATH = Path(__file__).with_name("phase3_real_e2e_result.json")


def _safe_failure_summary(error: BaseException) -> str:
    """Return bounded failure metadata without persisting exception internals."""

    summary = getattr(error, "summary", None)
    if not isinstance(summary, str) or not summary.strip():
        return "Execution failed; details intentionally omitted."
    cleaned = " ".join(summary.split())
    cleaned = re.sub(
        r"(?i)(api[_ -]?key|password|token|authorization)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        cleaned,
    )
    cleaned = re.sub(r"[a-z][a-z0-9+.-]*://\S+", "[REDACTED_URL]", cleaned)
    cleaned = re.sub(r"(?i)\b[a-z]:\\\S+", "[REDACTED_PATH]", cleaned)
    return cleaned[:300]


def _summarize_tool_calls(
    tool_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep bounded tool metadata, never raw result rows or generated queries."""

    summaries: list[dict[str, Any]] = []
    allowed_metadata = (
        "arguments",
        "columns",
        "row_count",
        "dry_plan",
        "dimensions",
        "metrics",
        "rebuffer_contract",
        "status_distribution",
        "alarm_evidence",
    )
    evidence_fields = (
        "window_name",
        "region",
        "cdn",
        "session_count",
        "successful_sessions",
        "failed_sessions",
        "playback_success_rate",
        "average_startup_time_ms",
        "rebuffer_ratio",
        "error_code",
        "severity",
        "status",
        "service",
        "level",
        "codec",
        "gpu_pool",
    )
    for item in tool_results:
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        data = item.get("data")
        if not isinstance(data, list):
            data = []
        evidence_summary = [
            {key: row[key] for key in evidence_fields if key in row}
            for row in data[:20]
            if isinstance(row, dict)
        ]
        evidence_summary = [row for row in evidence_summary if row]
        summaries.append(
            {
                "tool_name": item.get("tool_name"),
                "success": item.get("success"),
                "summary": item.get("summary"),
                "metadata": {
                    key: metadata[key]
                    for key in allowed_metadata
                    if key in metadata
                },
                "evidence_summary": evidence_summary,
                "error": item.get("error"),
                "execution_time_ms": item.get("execution_time_ms"),
            }
        )
    return summaries


def _collect_model_calls(*models: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    seen: set[int] = set()
    for model in models:
        if model is None or id(model) in seen:
            continue
        seen.add(id(model))
        calls.extend(model.calls)
    return calls


def _task_component(state: dict[str, Any]) -> str | None:
    task = state.get("current_task")
    task_type = getattr(task, "task_type", None)
    return {
        "query": "query_pipeline",
        "analysis": "analyst",
        "response": "final_answer",
    }.get(task_type)


def _resolved_failure_stage(
    error: BaseException | None,
    current_stage: str,
    state: dict[str, Any],
) -> str | None:
    if error is None:
        return None
    detail = getattr(error, "stage", None)
    component = _task_component(state)
    if isinstance(detail, str) and detail:
        return f"{component or current_stage}.{detail}"
    return current_stage


def _task_status(
    tasks: list[dict[str, Any]],
    task_type: str,
) -> str:
    matching = [item for item in tasks if item.get("task_type") == task_type]
    if not matching:
        return "not_planned"
    statuses = {item.get("status") for item in matching}
    if "failed" in statuses:
        return "failed"
    if statuses == {"completed"}:
        return "completed"
    if "in_progress" in statuses:
        return "in_progress"
    return "not_started"


def _evidence_pack_report(state: dict[str, Any]) -> dict[str, Any]:
    """Return bounded provenance metadata without raw rows or chunk text."""

    pack = state.get("final_evidence_pack")
    if not isinstance(pack, dict):
        pack = {}
    data_summary: list[dict[str, Any]] = []
    grouped_comparison: list[dict[str, Any]] = []
    allowed_facts = (
        "dimension",
        "group",
        "metric",
        "baseline_window",
        "baseline_value",
        "current_window",
        "current_value",
        "delta",
        "relative_change",
        "classification",
        "is_largest_decline",
        "filters",
        "distribution",
        "mixed",
        "total",
        "row_count",
        "execution_source",
        "tool_name",
        "evidence_role",
        "severity_distribution",
        "error_code_distribution",
        "cdn_distribution",
        "service_distribution",
        "affected_objects",
        "sample_events",
        "sample_semantics",
    )
    for item in pack.get("data_evidence", []):
        facts = item.get("facts")
        if not isinstance(facts, dict):
            facts = {}
        safe_facts = {key: facts[key] for key in allowed_facts if key in facts}
        entry = {
            "evidence_id": item.get("evidence_id"),
            "kind": item.get("kind"),
            "source_task_ids": list(item.get("source_task_ids", [])),
            "source_evidence_ids": list(
                item.get("source_evidence_ids", [])
            ),
            "facts": safe_facts,
        }
        data_summary.append(entry)
        if item.get("kind") == "group_comparison":
            grouped_comparison.append(safe_facts)
    knowledge_summary = [
        {
            "evidence_id": item.get("evidence_id"),
            "title": item.get("title"),
            "source_category": item.get("source_category"),
        }
        for item in pack.get("knowledge_evidence", [])
    ]
    claims = state.get("final_claims")
    if not isinstance(claims, list):
        claims = []
    claim_summary = [
        {
            "bundle_id": item.get("bundle_id"),
            "claim_type": item.get("claim_type"),
            "predicate": item.get("predicate"),
            "polarity": item.get("polarity"),
            "subject_evidence_ids": list(
                item.get("subject_evidence_ids", [])
            ),
            "supporting_evidence_ids": list(
                item.get("supporting_evidence_ids", [])
            ),
        }
        for item in claims
        if isinstance(item, dict)
    ]
    projection = state.get("final_evidence_projection")
    if not isinstance(projection, dict):
        projection = {}
    projection_ids = {
        key: [
            item.get("id")
            for item in projection.get(key, [])
            if isinstance(item, dict)
        ]
        for key in ("data_evidence", "knowledge_evidence", "limitations")
    }
    return {
        "evidence_pack_summary": {
            "version": pack.get("version"),
            "reviewed_task_ids": list(pack.get("reviewed_task_ids", [])),
            "reviewed_evidence": list(pack.get("reviewed_evidence", [])),
            "data_evidence": data_summary,
            "knowledge_evidence": knowledge_summary,
            "limitation_ids": [
                item.get("limitation_id")
                for item in pack.get("limitations", [])
            ],
        },
        "grouped_comparison": grouped_comparison,
        "final_claims": claim_summary,
        "claim_evidence_mapping": [
            {
                "bundle_id": item["bundle_id"],
                "claim_type": item["claim_type"],
                "predicate": item["predicate"],
                "polarity": item["polarity"],
                "subject_evidence_ids": item["subject_evidence_ids"],
                "supporting_evidence_ids": item["supporting_evidence_ids"],
            }
            for item in claim_summary
        ],
        "final_validator_result": dict(
            state.get("final_validator_result") or {}
        ),
        "evidence_projection_summary": {
            "version": projection.get("version"),
            "scope_count": len(projection.get("scopes", {})),
            "visible_ids": projection_ids,
            "evidence_bundles": [
                {
                    "bundle_id": item.get("bundle_id"),
                    "bundle_type": item.get("bundle_type"),
                    "scope": item.get("scope"),
                    "allowed_evidence_ids": list(
                        item.get("allowed_evidence_ids", [])
                    ),
                    "allowed_knowledge_ids": list(
                        item.get("allowed_knowledge_ids", [])
                    ),
                    "allowed_limitation_ids": list(
                        item.get("allowed_limitation_ids", [])
                    ),
                    "supported_claim_types": list(
                        item.get("supported_claim_types", [])
                    ),
                    "target_entities": item.get("target_entities", {}),
                    "provenance": item.get("provenance", {}),
                }
                for item in projection.get("evidence_bundles", [])
                if isinstance(item, dict)
            ],
        },
        "evidence_projection_stats": dict(
            state.get("final_evidence_projection_stats") or {}
        ),
    }


def _build_report(
    *,
    initialized: Any | None,
    retrieval_results: list[dict[str, Any]],
    retrieval_calls: int,
    memory_updated: bool,
    success: bool,
    failure: BaseException | None,
    current_stage: str,
    started_at: float,
    models: tuple[Any, ...],
) -> dict[str, Any]:
    state: dict[str, Any] = initialized.state if initialized is not None else {}
    events = initialized.trace.get_events() if initialized is not None else []
    trace_summary = summarize_trace(events)
    tasks = [asdict(task) for task in state.get("task_plan", [])]
    calls = _collect_model_calls(*models)
    components = sorted({str(call["component"]) for call in calls})
    component_call_counts = {
        component: sum(call["component"] == component for call in calls)
        for component in components
    }
    component_llm_latency = {
        component: sum(
            float(call["latency_ms"])
            for call in calls
            if call["component"] == component
        )
        for component in components
    }
    structured_retries = {
        name: sum(event.event_type.value == name for event in events)
        for name in (
            "PLANNER_RETRY",
            "REVIEW_OUTPUT_RETRY",
            "ANALYST_OUTPUT_RETRY",
        )
    }
    reviewer_decisions = [
        {
            "task_id": item.task_id,
            "decision": item.decision,
            "reason_summary": item.reason_summary,
            "confidence": item.confidence,
            "review_retry_count": item.review_retry_count,
        }
        for item in state.get("review_results", [])
    ]
    analysis_results = [
        {
            "task_id": item.task_id,
            "success": item.success,
            "summary": item.summary,
            "source_task_ids": item.source_task_ids,
            "retry_count": item.retry_count,
            "error": item.error,
        }
        for item in state.get("analysis_results", [])
    ]
    final_result = state.get("final_answer_result")
    if failure is not None:
        run_status = "exception"
    else:
        run_status = "passed" if success else "failed"
    failure_stage = _resolved_failure_stage(failure, current_stage, state)
    failure_type = type(failure).__name__ if failure is not None else None
    failure_summary = (
        _safe_failure_summary(failure) if failure is not None else None
    )
    if not success and failure is None:
        failure_stage = current_stage
        failure_type = "IncompleteWorkflow"
        failure_summary = "The workflow did not satisfy every acceptance gate."
    evidence_report = _evidence_pack_report(state)
    return {
        "query": QUERY,
        "run_status": run_status,
        "success": success,
        "failure_stage": failure_stage,
        "failure_type": failure_type,
        "failure_summary": failure_summary,
        "planner_tasks": tasks,
        "tool_routes": list(state.get("tool_routes", [])),
        "tool_calls": _summarize_tool_calls(
            list(state.get("tool_results", []))
        ),
        "sql_fallback_count": int(state.get("tool_fallback_count", 0)),
        "sql_evidence": [
            {
                "task_id": item.task_id,
                "success": item.success,
                "columns": item.columns,
                "row_count": item.row_count,
                "retry_count": item.retry_count,
                "semantic_retry_count": item.semantic_retry_count,
                "execution_time_ms": item.execution_time * 1000,
                "execution_source": item.execution_source,
                "tool_name": item.tool_name,
            }
            for item in state.get("sql_results", [])
        ],
        "retrieval_calls": retrieval_calls,
        "retrieved_chunk_ids": [
            str(item["chunk_id"])
            for item in retrieval_results
            if "chunk_id" in item
        ],
        "retrieval_top_k": len(retrieval_results),
        "retrieval_latency_ms": float(
            state.get("knowledge_retrieval_latency_ms", 0.0)
        ),
        "reviewer_decisions": reviewer_decisions,
        "analyst_status": {
            "status": _task_status(tasks, "analysis"),
            "results": analysis_results,
        },
        "final_answer_status": {
            "status": _task_status(tasks, "response"),
            "success": bool(final_result and final_result.success),
            "retry_count": getattr(final_result, "retry_count", 0),
        },
        "final_answer": state.get("final_answer"),
        **evidence_report,
        "session_commit_status": (
            "committed" if memory_updated else "not_committed"
        ),
        "llm_call_count": len(calls),
        "component_call_counts": component_call_counts,
        "technical_retry": trace_summary.technical_retry_count,
        "semantic_retry": trace_summary.semantic_retry_count,
        "structured_output_retry": {
            "total": sum(structured_retries.values()),
            "by_event": structured_retries,
        },
        "wall_latency_ms": (perf_counter() - started_at) * 1000,
        "component_latency_ms": {
            "llm": component_llm_latency,
            "planner": trace_summary.planner_duration_ms,
            "sql_agent": trace_summary.sql_agent_duration_ms,
            "tool_router": trace_summary.tool_router_duration_ms,
            "tool_execution": trace_summary.tool_execution_duration_ms,
            "reviewer": trace_summary.reviewer_duration_ms,
            "analyst": trace_summary.analyst_duration_ms,
            "final_answer": trace_summary.final_answer_duration_ms,
            "retrieval": float(
                state.get("knowledge_retrieval_latency_ms", 0.0)
            ),
        },
        "trace_summary": asdict(trace_summary),
    }


def _write_report(report: dict[str, Any]) -> None:
    """Atomically persist the bounded report inside the eval directory."""

    serialized = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    temporary = REPORT_PATH.with_suffix(".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(REPORT_PATH)


def run_case(
    *,
    reviewer_model: str | None = None,
    planner_model: str | None = None,
) -> dict[str, Any]:
    """Execute one case and always return a bounded structured report."""

    started_at = perf_counter()
    initialized: Any | None = None
    retrieval_results: list[dict[str, Any]] = []
    retrieval_calls = 0
    memory_updated = False
    success = False
    failure: BaseException | None = None
    current_stage = "initialization"
    counting_model: CountingModel | None = None
    review_model: CountingModel | None = None
    plan_model: CountingModel | None = None
    try:
        session_id = "media-phase3-real-e2e"
        initialized = process_input(QUERY, session_id=session_id)
        current_stage = "configuration"
        environment = _safe_environment()
        counting_model = CountingModel(
            OpenAICompatiblePlannerModel.from_env(environment)
        )
        review_model = (
            CountingModel(replace(counting_model.delegate, model=reviewer_model))
            if reviewer_model
            else counting_model
        )
        plan_model = (
            CountingModel(replace(counting_model.delegate, model=planner_model))
            if planner_model
            else counting_model
        )
        current_stage = "wren_and_capability_setup"
        wren = WrenToolAdapter.from_project(
            MEDIA,
            profile=environment["WREN_PROFILE"],
        )
        planning_context = try_fetch_planning_context(wren)
        registry = build_media_tool_registry(wren)
        router = ToolRouter(registry, capability_context=planning_context)
        retriever = KnowledgeRetriever.from_directory(MEDIA / "knowledge")
        planner = Planner(model_client=plan_model)
        sql_agent = SQLAgent(model_client=counting_model, wren_tools=wren)
        reviewer = Reviewer(model_client=review_model)
        analyst = Analyst(model_client=counting_model)
        resolver = FollowUpResolver(model_client=counting_model)
        extractor = SessionContextExtractor(model_client=counting_model)
        store = SessionMemoryStore()
        store.create(session_id)
        current_stage = "knowledge_retrieval"
        retrieval_results = retrieve_into_state(
            initialized.state,
            retriever,
            top_k=5,
        )
        retrieval_calls = 1
        current_stage = "planning"
        planning = prepare_session_turn(
            initialized.state,
            planner,
            resolver,
            store,
            trace=initialized.trace,
            planning_context=planning_context,
        )
        if not planning.can_execute or planning.effective_result is None:
            raise RuntimeError("planning could not execute")
        current_stage = "workflow"
        workflow = execute_task_plan(
            initialized.state,
            sql_agent,
            reviewer,
            analyst,
            trace=initialized.trace,
            tool_router=router,
        )
        current_stage = "session_memory"
        memory_updated = commit_session_context(
            initialized.state,
            planning.effective_result,
            workflow,
            store,
            extractor,
            trace=initialized.trace,
            resolution=planning.resolution,
        )
        success = bool(
            workflow.final_answer_result
            and workflow.final_answer_result.success
            and memory_updated
        )
        current_stage = "complete" if success else "acceptance"
    except Exception as exc:  # report partial state without raw model material
        failure = exc
    return _build_report(
        initialized=initialized,
        retrieval_results=retrieval_results,
        retrieval_calls=retrieval_calls,
        memory_updated=memory_updated,
        success=success,
        failure=failure,
        current_stage=current_stage,
        started_at=started_at,
        models=(counting_model, review_model, plan_model),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviewer-model", default=None)
    parser.add_argument("--planner-model", default=None)
    parser.add_argument(
        "--confirm-external-synthetic-data",
        action="store_true",
        help="Acknowledge the separately granted Phase 3 synthetic-data scope.",
    )
    arguments = parser.parse_args()
    if not arguments.confirm_external_synthetic_data:
        parser.error(
            "Phase 3 external synthetic-data authorization is required before "
            "calling the configured model endpoint"
        )
    report = run_case(
        reviewer_model=arguments.reviewer_model,
        planner_model=arguments.planner_model,
    )
    _write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
