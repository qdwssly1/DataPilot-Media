"""Run the Phase 3 Media scenarios without any external model call."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any

from datapilot.agent.analyst import Analyst
from datapilot.agent.follow_up import SessionContextExtractor
from datapilot.agent.graph import commit_session_context, execute_task_plan
from datapilot.agent.planner import Planner
from datapilot.agent.reviewer import Reviewer
from datapilot.agent.sql_agent import SQLAgent
from datapilot.cli import process_input
from datapilot.memory.session_memory import SessionMemoryStore
from datapilot.retrieval import KnowledgeRetriever
from datapilot.retrieval.integration import retrieve_into_state
from datapilot.tools.router import ToolRouter
from datapilot.tools.wren_tools import WrenToolAdapter, try_fetch_planning_context
from datapilot.tracing.summary import summarize_trace
from domains.media.runtime import build_media_tool_registry

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "domains" / "media"

DIRECT_CASES = (
    "查询华南当前窗口播放成功率和卡顿情况。",
    "华南 CDN-B 当前有哪些 E302 高等级告警？",
    "检查 CDN-B 同期是否存在 upstream timeout 相关日志。",
    "最近是否存在失败的 H.265 转码任务？",
)
COMPOSITE_QUERY = (
    "华南播放成功率下降并出现 E302，结合告警、日志和知识库分析原因"
    "并给出排查建议。"
)


class OfflineWorkflowModel:
    """Deterministic structured outputs used only for offline orchestration."""

    def __init__(
        self,
        knowledge_chunk_ids: list[str],
        *,
        planner_shape: str = "single_task",
    ) -> None:
        self.knowledge_chunk_ids = knowledge_chunk_ids
        self.planner_shape = planner_shape
        self.calls: list[str] = []
        self.review_attempts: dict[str, int] = {}
        self.analyst_corrected_log_evidence_visible = False
        self.analyst_log_evidence_markers: dict[str, bool] = {}

    def _chunk(self, prefix: str) -> str | None:
        return next(
            (item for item in self.knowledge_chunk_ids if prefix in item),
            None,
        )

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        del response_schema
        lowered = system_prompt.lower()
        if "task planner" in lowered:
            component = "planner"
            metric_binding = {
                "primary_metric": "playback_success_rate",
                "unit": "ratio",
                "aggregation_semantics": (
                    "sum(successful_sessions)/sum(session_count)"
                ),
                "supporting_fields": [
                    "session_count",
                    "successful_sessions",
                    "failed_sessions",
                ],
            }
            requested_dimensions = ["window_name", "region", "cdn"]
            if self.planner_shape == "overlapping_real_shape":
                window_roles = {
                    "comparison_target": "current_window",
                    "baseline_windows": ["previous_window", "previous_day"],
                    "primary_baseline": None,
                }
                qoe_tasks = [
                    {
                        "task_id": "q_current_qoe",
                        "description": (
                            "Query previous_window and current_window playback "
                            "success by window_name, region, and CDN for 华南."
                        ),
                        "task_type": "query",
                        "depends_on": [],
                        "status": "pending",
                        "metric_binding": metric_binding,
                        "requested_dimensions": requested_dimensions,
                        "window_role_binding": window_roles,
                    },
                    {
                        "task_id": "q_baseline_qoe",
                        "description": (
                            "Query previous_day, previous_window, and "
                            "current_window playback success by window_name, "
                            "region, and CDN for 华南."
                        ),
                        "task_type": "query",
                        "depends_on": [],
                        "status": "pending",
                        "metric_binding": metric_binding,
                        "requested_dimensions": requested_dimensions,
                        "window_role_binding": window_roles,
                    },
                ]
                analysis_tasks = [
                    {
                        "task_id": "analysis",
                        "description": (
                            "Correlate canonical QoE comparisons with approved "
                            "alarm and corrected log evidence without causality."
                        ),
                        "task_type": "analysis",
                        "depends_on": [
                            "q_current_qoe",
                            "q_baseline_qoe",
                            "alarms",
                            "logs",
                        ],
                        "status": "pending",
                    }
                ]
                response_dependency = "analysis"
            elif self.planner_shape == "single_task_multi_baseline":
                window_roles = {
                    "comparison_target": "current_window",
                    "baseline_windows": ["previous_window", "previous_day"],
                    "primary_baseline": None,
                }
                qoe_tasks = [
                    {
                        "task_id": "qoe",
                        "description": (
                            "Query previous_window, previous_day, and "
                            "current_window playback success rate by window_name, "
                            "region, and CDN for 华南."
                        ),
                        "task_type": "query",
                        "depends_on": [],
                        "status": "pending",
                        "metric_binding": metric_binding,
                        "requested_dimensions": requested_dimensions,
                        "window_role_binding": window_roles,
                    }
                ]
                analysis_tasks = [
                    {
                        "task_id": "analysis",
                        "description": (
                            "Correlate both normalized baseline comparisons with "
                            "approved alarm and log evidence without causality."
                        ),
                        "task_type": "analysis",
                        "depends_on": ["qoe", "alarms", "logs"],
                        "status": "pending",
                    }
                ]
                response_dependency = "analysis"
            elif self.planner_shape in {
                "multi_baseline_primary",
                "multi_baseline_no_primary",
            }:
                primary = (
                    "previous_window"
                    if self.planner_shape == "multi_baseline_primary"
                    else None
                )
                window_roles = {
                    "comparison_target": "current_window",
                    "baseline_windows": ["previous_day", "previous_window"],
                    "primary_baseline": primary,
                }
                qoe_tasks = [
                    {
                        "task_id": "q1_baseline_playback",
                        "description": (
                            "Query previous_day and previous_window playback "
                            "success rate by window_name, region, and CDN for 华南."
                        ),
                        "task_type": "query",
                        "depends_on": [],
                        "status": "pending",
                        "metric_binding": metric_binding,
                        "requested_dimensions": requested_dimensions,
                        "window_role_binding": window_roles,
                    },
                    {
                        "task_id": "q2_current_playback",
                        "description": (
                            "Query current_window playback success rate by "
                            "window_name, region, and CDN for 华南."
                        ),
                        "task_type": "query",
                        "depends_on": [],
                        "status": "pending",
                        "metric_binding": metric_binding,
                        "requested_dimensions": requested_dimensions,
                        "window_role_binding": window_roles,
                    },
                ]
                analysis_tasks = [
                    {
                        "task_id": "a1_playback_comparison",
                        "description": (
                            "Normalize every compatible baseline-to-current "
                            "playback success comparison by CDN."
                        ),
                        "task_type": "analysis",
                        "depends_on": [
                            "q1_baseline_playback",
                            "q2_current_playback",
                        ],
                        "status": "pending",
                    },
                    {
                        "task_id": "a2_correlation",
                        "description": (
                            "Correlate the normalized QoE comparison with approved "
                            "alarm and log evidence without claiming causality."
                        ),
                        "task_type": "analysis",
                        "depends_on": [
                            "a1_playback_comparison",
                            "alarms",
                            "logs",
                        ],
                        "status": "pending",
                    },
                ]
                response_dependency = "a2_correlation"
            elif self.planner_shape == "split_tasks":
                window_roles = {
                    "comparison_target": "current_window",
                    "baseline_windows": ["previous_window"],
                    "primary_baseline": "previous_window",
                }
                qoe_tasks = [
                    {
                        "task_id": "qoe_baseline",
                        "description": (
                            "Query previous_window playback success rate by CDN "
                            "for 华南."
                        ),
                        "task_type": "query",
                        "depends_on": [],
                        "status": "pending",
                        "metric_binding": metric_binding,
                        "requested_dimensions": ["cdn"],
                        "window_role_binding": window_roles,
                    },
                    {
                        "task_id": "qoe_current",
                        "description": (
                            "Query current_window playback success rate by CDN "
                            "for 华南."
                        ),
                        "task_type": "query",
                        "depends_on": [],
                        "status": "pending",
                        "metric_binding": metric_binding,
                        "requested_dimensions": ["cdn"],
                        "window_role_binding": window_roles,
                    },
                ]
                analysis_dependencies = [
                    "qoe_baseline",
                    "qoe_current",
                    "alarms",
                    "logs",
                ]
                analysis_tasks = [
                    {
                        "task_id": "analysis",
                        "description": (
                            "Correlate approved QoE, alarm, and log evidence without "
                            "claiming causality."
                        ),
                        "task_type": "analysis",
                        "depends_on": analysis_dependencies,
                        "status": "pending",
                    }
                ]
                response_dependency = "analysis"
            else:
                window_roles = {
                    "comparison_target": "current_window",
                    "baseline_windows": ["previous_window"],
                    "primary_baseline": "previous_window",
                }
                qoe_tasks = [
                    {
                        "task_id": "qoe",
                        "description": (
                            "Compare previous_window and current_window playback "
                            "success rate, startup time, rebuffer ratio, and failed "
                            "sessions by CDN for 华南."
                        ),
                        "task_type": "query",
                        "depends_on": [],
                        "status": "pending",
                        "metric_binding": metric_binding,
                        "requested_dimensions": ["window_name", "cdn"],
                        "window_role_binding": window_roles,
                    }
                ]
                analysis_dependencies = ["qoe", "alarms", "logs"]
                analysis_tasks = [
                    {
                        "task_id": "analysis",
                        "description": (
                            "Correlate approved QoE, alarm, and log evidence without "
                            "claiming causality."
                        ),
                        "task_type": "analysis",
                        "depends_on": analysis_dependencies,
                        "status": "pending",
                    }
                ]
                response_dependency = "analysis"
            payload = {
                "intent": "multi_step_analysis",
                "reason_summary": (
                    "QoE, alarm, log, and knowledge evidence are required."
                ),
                "tasks": [
                    *qoe_tasks,
                    {
                        "task_id": "alarms",
                        "description": (
                            "Retrieve individual current_window 华南 CDN-B high "
                            "E302 alarm events with status."
                        ),
                        "task_type": "query",
                        "depends_on": [],
                        "status": "pending",
                    },
                    {
                        "task_id": "logs",
                        "description": (
                            "Retrieve current_window 华南 CDN-B E302 upstream "
                            "timeout structured logs."
                        ),
                        "task_type": "query",
                        "depends_on": [],
                        "status": "pending",
                    },
                    *analysis_tasks,
                    {
                        "task_id": "response",
                        "description": (
                            "Answer with DATA EVIDENCE, KNOWLEDGE EVIDENCE, "
                            "INFERENCE, and LIMITATION."
                        ),
                        "task_type": "response",
                        "depends_on": [response_dependency],
                        "status": "pending",
                    },
                ],
                "requires_database": True,
                "requires_context": True,
                "is_follow_up": False,
            }
        elif "semantic reviewer" in lowered:
            component = "reviewer"
            task_match = re.search(r'"task_id":"([^"]+)"', user_prompt)
            task_id = task_match.group(1) if task_match else "unknown"
            attempt = self.review_attempts.get(task_id, 0)
            self.review_attempts[task_id] = attempt + 1
            correction_tasks = {
                "q1_baseline_playback",
                "q2_current_playback",
                "logs",
            }
            if (
                (
                    self.planner_shape == "multi_baseline_primary"
                    or (
                        self.planner_shape == "multi_baseline_no_primary"
                        and task_id == "logs"
                    )
                    or (
                        self.planner_shape == "single_task_multi_baseline"
                        and task_id == "logs"
                    )
                    or (
                        self.planner_shape == "overlapping_real_shape"
                        and task_id == "logs"
                    )
                )
                and task_id in correction_tasks
                and attempt == 0
            ):
                payload = {
                    "decision": "retry",
                    "reason_summary": (
                        "Exercise the bounded SQL correction while preserving "
                        "the originating evidence contract."
                    ),
                    "issues": [
                        {
                            "issue_type": "missing_data",
                            "description": "Add the requested grouped context.",
                        }
                    ],
                    "retry_instruction": (
                        "Return the requested grouped rows without changing the "
                        "metric or window-role contract."
                    ),
                    "confidence": 0.99,
                }
            else:
                payload = {
                    "decision": "approve",
                    "reason_summary": "The structured evidence satisfies this task.",
                    "issues": [],
                    "retry_instruction": None,
                    "confidence": 0.99,
                }
        elif "sql agent" in lowered:
            component = "sql_agent"
            if '"task_id":"q1_baseline_playback"' in user_prompt:
                windows = "'previous_day', 'previous_window'"
                where = f"window_name IN ({windows})"
                source = "stream_sessions"
                select = (
                    "window_name, region, cdn, COUNT(*) AS session_count, "
                    "SUM(CASE WHEN play_success THEN 1 ELSE 0 END) AS "
                    "successful_sessions, SUM(CASE WHEN play_success THEN 0 "
                    "ELSE 1 END) AS failed_sessions, AVG(CASE WHEN "
                    "play_success THEN 1.0 ELSE 0.0 END) AS "
                    "playback_success_rate"
                )
                group = " GROUP BY window_name, region, cdn"
            elif '"task_id":"q2_current_playback"' in user_prompt:
                where = "window_name = 'current_window'"
                source = "stream_sessions"
                select = (
                    "window_name, region, cdn, COUNT(*) AS session_count, "
                    "SUM(CASE WHEN play_success THEN 1 ELSE 0 END) AS "
                    "successful_sessions, SUM(CASE WHEN play_success THEN 0 "
                    "ELSE 1 END) AS failed_sessions, AVG(CASE WHEN "
                    "play_success THEN 1.0 ELSE 0.0 END) AS "
                    "playback_success_rate"
                )
                group = " GROUP BY window_name, region, cdn"
            else:
                where = (
                    "window_name IN ('previous_day', 'previous_window', "
                    "'current_window') AND region = '华南' AND error_code = 'E302'"
                )
                source = "log_events"
                select = (
                    "timestamp, window_name, region, cdn, service, level, "
                    "error_code, trace_id, message"
                )
                group = ""
            payload = {
                "sql": f"SELECT {select} FROM {source} WHERE {where}{group}",
                "summary": "Bounded synthetic correction query.",
            }
        elif "grounded analyst" in lowered:
            component = "analyst"
            self.analyst_log_evidence_markers = {
                "logs_task": '"task_id":"logs"' in user_prompt,
                "sql_execution": '"execution_source":"sql"' in user_prompt,
                "semantic_retry": '"semantic_retry_count":1' in user_prompt,
                "four_rows": '"row_count":4' in user_prompt,
                "e302_scope": "E302" in user_prompt,
            }
            self.analyst_corrected_log_evidence_visible = all(
                self.analyst_log_evidence_markers.values()
            )
            if self.planner_shape in {
                "multi_baseline_primary",
                "multi_baseline_no_primary",
            }:
                analysis_sources = ["a1_playback_comparison", "alarms", "logs"]
            elif self.planner_shape == "overlapping_real_shape":
                analysis_sources = [
                    "q_current_qoe",
                    "q_baseline_qoe",
                    "alarms",
                    "logs",
                ]
            elif self.planner_shape == "split_tasks":
                analysis_sources = [
                    "qoe_baseline",
                    "qoe_current",
                    "alarms",
                    "logs",
                ]
            else:
                analysis_sources = ["qoe", "alarms", "logs"]
            payload = {
                "summary": (
                    "CDN-B has the largest QoE decline alongside E302 alarms and "
                    "upstream-timeout logs; causality remains unproven."
                ),
                "findings": [
                    "CDN-B playback success changed from 0.95 to 0.50.",
                    (
                        "Three high E302 alarms retain status distribution "
                        "open=1, investigating=1, resolved=1."
                    ),
                    "Four matching structured logs are present in the current window.",
                ],
                "source_task_ids": analysis_sources,
            }
        elif "final response writer" in lowered:
            component = "final_answer"
            split_like = self.planner_shape in {
                "split_tasks",
                "multi_baseline_primary",
                "multi_baseline_no_primary",
                "overlapping_real_shape",
            }
            log_evidence_id = "data:result:4" if split_like else "data:result:3"
            comparison_count = (
                2
                if self.planner_shape
                in {
                    "multi_baseline_primary",
                    "multi_baseline_no_primary",
                    "single_task_multi_baseline",
                    "overlapping_real_shape",
                }
                else 1
            )
            comparison_evidence_ids = [
                evidence_id
                for comparison_index in range(1, comparison_count + 1)
                for evidence_id in [
                    f"data:comparison:{comparison_index}:overall",
                    f"data:comparison:{comparison_index}:group:1",
                    f"data:comparison:{comparison_index}:group:2",
                    f"data:comparison:{comparison_index}:group:3",
                ]
            ]
            observation_support_ids = [
                f"data:comparison:{comparison_index}:group:{group_index}"
                for comparison_index in range(1, comparison_count + 1)
                for group_index in range(1, 4)
            ]
            stable_subject_ids = [
                f"data:comparison:{comparison_index}:group:{group_index}"
                for comparison_index in range(1, comparison_count + 1)
                for group_index in (1, 3)
            ]
            correction_evidence_ids = (
                ["data:tool-result:4"]
                if self.planner_shape
                in {
                    "multi_baseline_primary",
                    "multi_baseline_no_primary",
                    "overlapping_real_shape",
                }
                else (
                    ["data:tool-result:3"]
                    if self.planner_shape == "single_task_multi_baseline"
                    else []
                )
            )
            e302_chunk = self._chunk("media-error-codes::01-e302")
            sop_chunk = self._chunk("media-troubleshooting-sop::01-cdn-playback")
            metric_chunk = self._chunk(
                "media-qoe-metrics::01-playback-success-rate"
            )
            payload = {
                "data_evidence_ids": [
                    *comparison_evidence_ids,
                    "data:status:1",
                    *correction_evidence_ids,
                    log_evidence_id,
                ],
                "knowledge_evidence_ids": [
                    f"knowledge:{chunk_id}"
                    for chunk_id in self.knowledge_chunk_ids
                ],
                "inferences": [
                    {
                        "claim_type": "observation",
                        "bundle_id": "bundle:regional:华南:cdn",
                        "predicate": "general",
                        "polarity": "neutral",
                        "subject_evidence_ids": [],
                        "supporting_evidence_ids": [
                            f"data:comparison:{comparison_index}:overall"
                            for comparison_index in range(1, comparison_count + 1)
                        ],
                    },
                    {
                        "claim_type": "observation",
                        "bundle_id": "bundle:multi_group:华南:cdn",
                        "predicate": "general",
                        "polarity": "neutral",
                        "subject_evidence_ids": [],
                        "supporting_evidence_ids": [
                            f"data:comparison:{comparison_index}:group:2"
                            for comparison_index in range(1, comparison_count + 1)
                        ],
                    },
                    {
                        "claim_type": "correlation",
                        "bundle_id": "bundle:entity:华南:cdn:CDN-B",
                        "predicate": "general",
                        "polarity": "neutral",
                        "subject_evidence_ids": [],
                        "supporting_evidence_ids": [
                            "data:comparison:1:group:2",
                            "data:status:1",
                            log_evidence_id,
                            *(
                                [f"knowledge:{self.knowledge_chunk_ids[0]}"]
                                if self.knowledge_chunk_ids
                                else []
                            ),
                        ],
                    },
                    {
                        "claim_type": "knowledge",
                        "bundle_id": "bundle:global:knowledge",
                        "predicate": "general",
                        "polarity": "neutral",
                        "subject_evidence_ids": [],
                        "supporting_evidence_ids": (
                            [f"knowledge:{e302_chunk}"] if e302_chunk else []
                        ),
                    },
                    {
                        "claim_type": "knowledge",
                        "bundle_id": "bundle:global:knowledge",
                        "predicate": "general",
                        "polarity": "neutral",
                        "subject_evidence_ids": [],
                        "supporting_evidence_ids": (
                            [f"knowledge:{metric_chunk}"] if metric_chunk else []
                        ),
                    },
                    {
                        "claim_type": "observation",
                        "bundle_id": "bundle:multi_group:华南:cdn",
                        "predicate": "stable_control",
                        "polarity": "negative",
                        "subject_evidence_ids": stable_subject_ids,
                        "supporting_evidence_ids": stable_subject_ids,
                    },
                    {
                        "claim_type": "hypothesis",
                        "bundle_id": "bundle:entity:华南:cdn:CDN-B",
                        "predicate": "general",
                        "polarity": "neutral",
                        "subject_evidence_ids": [],
                        "supporting_evidence_ids": [
                            "data:comparison:1:group:2",
                            "data:status:1",
                            log_evidence_id,
                            *([f"knowledge:{e302_chunk}"] if e302_chunk else []),
                            "limitation:missing_trace_data",
                            "limitation:missing_origin_metrics",
                            "limitation:missing_routing_change_records",
                        ],
                    },
                    {
                        "claim_type": "recommendation",
                        "bundle_id": "bundle:entity:华南:cdn:CDN-B",
                        "predicate": "general",
                        "polarity": "neutral",
                        "subject_evidence_ids": [],
                        "supporting_evidence_ids": [
                            *([f"knowledge:{sop_chunk}"] if sop_chunk else []),
                            *([f"knowledge:{e302_chunk}"] if e302_chunk else []),
                            "data:comparison:1:group:2",
                            "data:status:1",
                            log_evidence_id,
                            "limitation:missing_trace_data",
                            "limitation:missing_origin_metrics",
                            "limitation:missing_routing_change_records",
                        ],
                    },
                    {
                        "claim_type": "recommendation",
                        "bundle_id": "bundle:regional:华南:cdn",
                        "predicate": "general",
                        "polarity": "neutral",
                        "subject_evidence_ids": [],
                        "supporting_evidence_ids": [
                            *([f"knowledge:{sop_chunk}"] if sop_chunk else []),
                            *observation_support_ids,
                            "limitation:missing_origin_metrics",
                            "limitation:missing_routing_change_records",
                            "limitation:limited_time_windows",
                        ],
                    },
                ],
                "limitation_ids": [
                    "limitation:correlation_not_causation",
                    "limitation:missing_trace_data",
                    "limitation:missing_origin_metrics",
                    "limitation:missing_routing_change_records",
                    "limitation:limited_time_windows",
                ],
                "source_task_ids": [
                    "a2_correlation"
                    if self.planner_shape
                    in {"multi_baseline_primary", "multi_baseline_no_primary"}
                    else "analysis"
                ],
            }
        elif "extract compact business context" in lowered:
            component = "session_memory"
            payload = {
                "metrics": ["playback_success_rate", "rebuffer_ratio"],
                "dimensions": ["cdn"],
                "time_range": {
                    "labels": ["previous_window", "current_window"],
                    "start": None,
                    "end": None,
                },
                "filters": {"region": ["华南"], "error_code": ["E302"]},
                "entities": {"cdn": ["CDN-B"]},
                "analysis_goal": "关联 QoE、告警、日志和 SOP 并保持因果边界",
            }
        else:  # SQL/follow-up must not be used by this offline fixture
            raise AssertionError("unexpected offline model component")
        self.calls.append(component)
        return json.dumps(payload, ensure_ascii=False)


def _runtime() -> tuple[WrenToolAdapter, ToolRouter]:
    os.environ["MEDIA_DUCKDB_DIR"] = str((MEDIA / "data").resolve())
    wren = WrenToolAdapter.from_project(
        MEDIA,
        profile="datapilot_media_duckdb",
    )
    router = ToolRouter(
        build_media_tool_registry(wren),
        capability_context=try_fetch_planning_context(wren),
    )
    return wren, router


def _run_composite(
    wren: WrenToolAdapter,
    router: ToolRouter,
    *,
    planner_shape: str,
) -> dict[str, Any]:
    session_id = f"media-phase3-offline-{planner_shape}"
    initialized = process_input(COMPOSITE_QUERY, session_id=session_id)
    retriever = KnowledgeRetriever.from_directory(MEDIA / "knowledge")
    retrieved = retrieve_into_state(initialized.state, retriever, top_k=5)
    relevant_prefixes = (
        "media-error-codes::01-e302",
        "media-troubleshooting-sop::01-cdn-playback",
        "media-qoe-metrics::01-playback-success-rate",
    )
    selected_chunks = [
        str(item["chunk_id"])
        for prefix in relevant_prefixes
        for item in retrieved
        if str(item["chunk_id"]).startswith(prefix)
    ]
    model = OfflineWorkflowModel(
        selected_chunks,
        planner_shape=planner_shape,
    )
    planner = Planner(model_client=model)
    planning_context = try_fetch_planning_context(wren)
    planning = planner.plan(
        initialized.state,
        trace=initialized.trace,
        planning_context=planning_context,
    )
    sql_agent = SQLAgent(model_client=model, wren_tools=wren)
    reviewer = Reviewer(model_client=model)
    analyst = Analyst(model_client=model)
    started_at = perf_counter()
    workflow = execute_task_plan(
        initialized.state,
        sql_agent,
        reviewer,
        analyst,
        trace=initialized.trace,
        tool_router=router,
    )
    store = SessionMemoryStore()
    store.create(session_id)
    memory_updated = commit_session_context(
        initialized.state,
        planning,
        workflow,
        store,
        SessionContextExtractor(model_client=model),
        trace=initialized.trace,
    )
    summary = summarize_trace(initialized.trace.get_events())
    return {
        "planner_shape": planner_shape,
        "success": bool(
            workflow.final_answer_result
            and workflow.final_answer_result.success
            and memory_updated
        ),
        "tasks": [asdict(task) for task in initialized.state["task_plan"]],
        "tool_routes": initialized.state["tool_routes"],
        "tool_results": initialized.state["tool_results"],
        "sql_fallback_count": initialized.state["tool_fallback_count"],
        "reviewer_decisions": [
            result.decision for result in initialized.state["review_results"]
        ],
        "retrieval_chunks": [item["chunk_id"] for item in retrieved],
        "retrieval_latency_ms": initialized.state[
            "knowledge_retrieval_latency_ms"
        ],
        "final_answer": initialized.state["final_answer"],
        "evidence_pack": initialized.state["final_evidence_pack"],
        "evidence_projection": initialized.state["final_evidence_projection"],
        "evidence_projection_stats": initialized.state[
            "final_evidence_projection_stats"
        ],
        "final_claims": initialized.state["final_claims"],
        "validator_result": initialized.state["final_validator_result"],
        "memory_updated": memory_updated,
        "model_calls": list(model.calls),
        "analyst_corrected_log_evidence_visible": (
            model.analyst_corrected_log_evidence_visible
        ),
        "analyst_log_evidence_markers": dict(
            model.analyst_log_evidence_markers
        ),
        "trace_summary": asdict(summary),
        "workflow_latency_ms": (perf_counter() - started_at) * 1000,
    }


def run_offline_cases() -> dict[str, Any]:
    """Run direct tools and all four supported Planner comparison shapes."""

    wren, router = _runtime()
    direct: list[dict[str, Any]] = []
    for query in DIRECT_CASES:
        decision = router.route(query)
        result = router.registry.execute(
            decision.tool_name or "",
            decision.arguments,
        )
        direct.append(
            {
                "query": query,
                "route": decision.as_dict(),
                "success": result.success,
                "row_count": len(result.data),
                "tool_execution_ms": result.execution_time_ms,
            }
        )

    single_task = _run_composite(
        wren,
        router,
        planner_shape="single_task",
    )
    single_task_multi_baseline = _run_composite(
        wren,
        router,
        planner_shape="single_task_multi_baseline",
    )
    split_tasks = _run_composite(
        wren,
        router,
        planner_shape="split_tasks",
    )
    multi_baseline_primary = _run_composite(
        wren,
        router,
        planner_shape="multi_baseline_primary",
    )
    multi_baseline_no_primary = _run_composite(
        wren,
        router,
        planner_shape="multi_baseline_no_primary",
    )
    overlapping_real_shape = _run_composite(
        wren,
        router,
        planner_shape="overlapping_real_shape",
    )
    return {
        "direct_cases": direct,
        "composite": single_task,
        "composite_shapes": {
            "single_task": single_task,
            "single_task_multi_baseline": single_task_multi_baseline,
            "split_tasks": split_tasks,
            "multi_baseline_primary": multi_baseline_primary,
            "multi_baseline_no_primary": multi_baseline_no_primary,
            "overlapping_real_shape": overlapping_real_shape,
        },
    }


if __name__ == "__main__":
    print(json.dumps(run_offline_cases(), ensure_ascii=False, indent=2, default=str))
