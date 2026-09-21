from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from datapilot.agent.analyst import Analyst
from datapilot.agent.evidence import (
    EvidenceProjectionError,
    build_evidence_contract,
    build_final_evidence_pack,
    build_final_evidence_projection,
    evidence_accuracy_violations,
    evidence_pack_source_violations,
    evidence_projection_claim_violations,
    evidence_projection_violations,
    final_claim_violations,
    normalize_evidence_language,
    render_final_answer,
)
from datapilot.agent.reviewer import Reviewer
from datapilot.agent.state import (
    AnalysisResult,
    ReviewerResult,
    SQLResult,
    TaskItem,
    create_initial_state,
)
from datapilot.retrieval import KnowledgeRetriever
from datapilot.retrieval.integration import retrieve_into_state
from datapilot.tools.integration import (
    mark_evidence_review_status,
    merge_correction_evidence,
)
from datapilot.tracing.trace import TraceCollector
from domains.media.runtime.tools import build_alarm_evidence

ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = ROOT / "domains" / "media" / "knowledge"


class RecordingModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response_schema": response_schema,
            }
        )
        return self.responses.pop(0)


def _trace(state: Any) -> TraceCollector:
    return TraceCollector(trace_id=state["trace_id"])


def _qoe_result() -> SQLResult:
    return SQLResult(
        task_id="qoe",
        sql="SELECT window_name, cdn, session_count, successful_sessions, "
        "playback_success_rate FROM hourly_alarm_correlation",
        columns=[
            "window_name",
            "cdn",
            "session_count",
            "successful_sessions",
            "playback_success_rate",
        ],
        rows=[
            {
                "window_name": "previous_window",
                "cdn": "CDN-A",
                "session_count": 20,
                "successful_sessions": 19,
                "playback_success_rate": 0.95,
            },
            {
                "window_name": "previous_window",
                "cdn": "CDN-B",
                "session_count": 20,
                "successful_sessions": 19,
                "playback_success_rate": 0.95,
            },
            {
                "window_name": "previous_window",
                "cdn": "CDN-C",
                "session_count": 20,
                "successful_sessions": 18,
                "playback_success_rate": 0.90,
            },
            {
                "window_name": "current_window",
                "cdn": "CDN-A",
                "session_count": 20,
                "successful_sessions": 17,
                "playback_success_rate": 0.85,
            },
            {
                "window_name": "current_window",
                "cdn": "CDN-B",
                "session_count": 20,
                "successful_sessions": 10,
                "playback_success_rate": 0.50,
            },
            {
                "window_name": "current_window",
                "cdn": "CDN-C",
                "session_count": 20,
                "successful_sessions": 16,
                "playback_success_rate": 0.80,
            },
        ],
        row_count=6,
    )


def _split_qoe_results(
    *,
    baseline_region: str = "华南",
    current_region: str = "华南",
) -> tuple[SQLResult, SQLResult]:
    combined = _qoe_result()

    def build(task_id: str, window: str, region: str) -> SQLResult:
        rows = [
            {key: value for key, value in row.items() if key != "window_name"}
            for row in combined.rows
            if row["window_name"] == window
        ]
        return SQLResult(
            task_id=task_id,
            sql="synthetic reviewed tool query",
            columns=[
                "cdn",
                "session_count",
                "successful_sessions",
                "playback_success_rate",
            ],
            rows=rows,
            row_count=len(rows),
            execution_source="tool",
            tool_name="query_qoe_metrics",
            tool_input={
                "windows": [window],
                "region": region,
                "group_by": ["cdn"],
                "limit": 100,
            },
            tool_metadata={
                "dimensions": ["cdn"],
                "metrics": [
                    "session_count",
                    "successful_sessions",
                    "playback_success_rate",
                ],
            },
        )

    return (
        build("baseline_metrics", "previous_window", baseline_region),
        build("current_metrics", "current_window", current_region),
    )


def _multi_code_alarm_tool_result() -> SQLResult:
    rows = [
        {
            "alarm_id": "alarm-prev-001",
            "timestamp": "2026-09-01 10:28:00",
            "window_name": "previous_window",
            "region": "华南",
            "cdn": "CDN-B",
            "error_code": "I101",
            "severity": "low",
            "status": "resolved",
            "message": "Routine health notification recovered.",
        },
        {
            "alarm_id": "alarm-cur-001",
            "timestamp": "2026-09-01 11:06:00",
            "window_name": "current_window",
            "region": "华南",
            "cdn": "CDN-B",
            "error_code": "E302",
            "severity": "high",
            "status": "open",
            "message": "Upstream timeout threshold exceeded.",
        },
        {
            "alarm_id": "alarm-cur-002",
            "timestamp": "2026-09-01 11:18:00",
            "window_name": "current_window",
            "region": "华南",
            "cdn": "CDN-B",
            "error_code": "E302",
            "severity": "high",
            "status": "investigating",
            "message": "Upstream timeouts continue.",
        },
        {
            "alarm_id": "alarm-cur-003",
            "timestamp": "2026-09-01 11:25:00",
            "window_name": "current_window",
            "region": "华南",
            "cdn": "CDN-C",
            "error_code": "W201",
            "severity": "medium",
            "status": "investigating",
            "message": "Synthetic warning.",
        },
        {
            "alarm_id": "alarm-cur-004",
            "timestamp": "2026-09-01 11:36:00",
            "window_name": "current_window",
            "region": "华南",
            "cdn": "CDN-B",
            "error_code": "E302",
            "severity": "high",
            "status": "resolved",
            "message": "Timeout rate recovered.",
        },
        {
            "alarm_id": "alarm-cur-005",
            "timestamp": "2026-09-01 11:44:00",
            "window_name": "current_window",
            "region": "华南",
            "cdn": "CDN-A",
            "error_code": "I101",
            "severity": "low",
            "status": "resolved",
            "message": "Synthetic informational alarm.",
        },
    ]
    return SQLResult(
        task_id="all_alarms",
        sql="synthetic fixed read",
        columns=list(rows[0]),
        rows=rows,
        row_count=len(rows),
        execution_source="tool",
        tool_name="get_alarm_events",
        tool_input={
            "windows": ["previous_window", "current_window"],
            "region": "华南",
            "limit": 100,
        },
        tool_metadata={"alarm_evidence": build_alarm_evidence(rows)},
    )


def _alarm_result() -> SQLResult:
    rows = [
        {
            "window_name": "current_window",
            "cdn": "CDN-B",
            "severity": "high",
            "status": status,
            "e302_alarm_count": 1,
        }
        for status in ("open", "investigating", "resolved")
    ]
    return SQLResult(
        task_id="alarms",
        sql=(
            "SELECT window_name, cdn, severity, status, COUNT(*) AS "
            "e302_alarm_count FROM alarm_events GROUP BY window_name, cdn, "
            "severity, status"
        ),
        columns=[
            "window_name",
            "cdn",
            "severity",
            "status",
            "e302_alarm_count",
        ],
        rows=rows,
        row_count=3,
    )


def _raw_alarm_tool_result() -> SQLResult:
    rows = [
        {
            "alarm_id": f"alarm-{index}",
            "timestamp": f"2026-09-01 11:{index * 10:02d}:00",
            "window_name": "current_window",
            "region": "华南",
            "cdn": "CDN-B",
            "error_code": "E302",
            "severity": "high",
            "status": status,
            "message": f"Origin upstream timeout sample {index}.",
        }
        for index, status in enumerate(
            ("open", "investigating", "resolved"),
            start=1,
        )
    ]
    return SQLResult(
        task_id="alarms",
        sql=(
            "SELECT alarm_id, timestamp, window_name, region, cdn, error_code, "
            "severity, status, message FROM alarm_events ORDER BY timestamp"
        ),
        columns=list(rows[0]),
        rows=rows,
        row_count=len(rows),
        execution_source="tool",
        tool_name="get_alarm_events",
        tool_input={
            "windows": ["current_window"],
            "region": "华南",
            "cdn": "CDN-B",
            "error_code": "E302",
        },
        tool_metadata={"alarm_evidence": build_alarm_evidence(rows)},
    )


def _alarm_correction_result() -> SQLResult:
    rows = [
        {
            "window_name": "current_window",
            "error_code": "E302",
            "alarm_count": 3,
        },
        {
            "window_name": "current_window",
            "error_code": "OTHER",
            "alarm_count": 2,
        },
    ]
    return SQLResult(
        task_id="alarms",
        sql=(
            "SELECT window_name, error_code, COUNT(*) AS alarm_count "
            "FROM alarm_events GROUP BY window_name, error_code"
        ),
        columns=list(rows[0]),
        rows=rows,
        row_count=len(rows),
        semantic_retry_count=1,
    )


def _log_tool_result() -> SQLResult:
    rows = [
        {
            "window_name": "current_window",
            "cdn": "CDN-B",
            "service": service,
            "level": level,
            "error_code": "E302",
            "trace_id": f"trace-{index}",
            "message": "Synthetic upstream timeout.",
        }
        for index, (service, level) in enumerate(
            [
                ("origin-proxy", "ERROR"),
                ("cdn-gateway", "ERROR"),
                ("origin-proxy", "ERROR"),
                ("cdn-gateway", "WARN"),
            ],
            start=1,
        )
    ]
    return SQLResult(
        task_id="logs",
        sql="synthetic fixed read",
        columns=list(rows[0]),
        rows=rows,
        row_count=len(rows),
        execution_source="tool",
        tool_name="query_logs",
        tool_metadata={"columns": list(rows[0]), "row_count": len(rows)},
    )


def _log_correction_result() -> SQLResult:
    rows = [
        {
            "window_name": "current_window",
            "service": "origin-proxy",
            "log_count": 3,
        },
        {
            "window_name": "current_window",
            "service": "cdn-gateway",
            "log_count": 2,
        },
    ]
    return SQLResult(
        task_id="logs",
        sql=(
            "SELECT window_name, service, COUNT(*) AS log_count "
            "FROM log_events GROUP BY window_name, service"
        ),
        columns=list(rows[0]),
        rows=rows,
        row_count=len(rows),
        semantic_retry_count=1,
    )


def _contract() -> dict[str, Any]:
    return build_evidence_contract([_qoe_result(), _alarm_result()])


@pytest.mark.parametrize("field", ["status", "message"])
def test_reviewer_rejects_lossy_categorical_alarm_aggregation(
    field: str,
) -> None:
    state = create_initial_state("Show E302 alarm categorical evidence")
    task = TaskItem(
        "alarms",
        "Return E302 alarm categorical evidence and count.",
        "query",
        status="executed",
    )
    state["task_plan"] = [task]
    state["pending_tasks"] = [task]
    state["current_task"] = task
    result = SQLResult(
        task_id="alarms",
        sql=(
            f"SELECT MAX({field}) AS {field}, COUNT(*) AS alarm_count "
            "FROM alarm_events"
        ),
        columns=[field, "alarm_count"],
        rows=[{field: "resolved", "alarm_count": 3}],
        row_count=1,
    )
    model = RecordingModel([])

    review = Reviewer(model_client=model).review(
        state,
        task,
        result,
        trace=_trace(state),
    )

    assert review.decision == "retry"
    assert review.issues[0].issue_type == "aggregation_mismatch"
    assert field in review.reason_summary
    assert model.calls == []
    assert task.status == "pending"


def test_evidence_contract_preserves_mixed_alarm_status_distribution() -> None:
    distribution = _contract()["status_distributions"][0]

    assert distribution["mixed"] is True
    assert {
        item["value"]: item["count"] for item in distribution["distribution"]
    } == {"open": 1, "investigating": 1, "resolved": 1}


def test_alarm_tool_summary_enters_evidence_pack_with_bounded_samples() -> None:
    pack = build_final_evidence_pack([_raw_alarm_tool_result()], [])
    alarm = next(
        item
        for item in pack["data_evidence"]
        if item["kind"] == "alarm_status_distribution"
    )

    assert alarm["facts"]["total"] == 3
    assert alarm["facts"]["severity_distribution"] == {"high": 3}
    assert alarm["facts"]["error_code_distribution"] == {"E302": 3}
    assert alarm["facts"]["affected_objects"]["cdn"] == ["CDN-B"]
    assert len(alarm["facts"]["sample_events"]) == 3
    assert alarm["facts"]["sample_semantics"] == (
        "bounded_examples_not_distribution"
    )
    assert "examples only" in alarm["statement"]
    assert evidence_pack_source_violations(pack) == []


def test_bounded_alarm_sample_debug_text_cannot_reach_renderer() -> None:
    pack = build_final_evidence_pack([_raw_alarm_tool_result()], [])
    alarm_id = next(
        item["evidence_id"]
        for item in pack["data_evidence"]
        if item["kind"] == "alarm_status_distribution"
    )
    selection = {
        "data_evidence_ids": [alarm_id],
        "knowledge_evidence_ids": [],
        "inferences": [
            {
                "bundle_id": "bundle:entity:ALL:cdn:CDN-B",
                "claim_type": "observation",
                "predicate": "general",
                "polarity": "neutral",
                "subject_evidence_ids": [],
                # Legacy/debug text is intentionally outside the typed model
                # schema. Direct validator/renderer calls must ignore it.
                "statement": (
                    "All alarm messages report an origin upstream timeout."
                ),
                "supporting_evidence_ids": [alarm_id],
            }
        ],
        "limitation_ids": [
            item["limitation_id"] for item in pack["limitations"]
        ],
        "source_task_ids": ["alarms"],
    }

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert "All alarm messages" not in answer
    assert "examples only" in answer


def test_reviewer_accepts_complete_deterministic_alarm_summary_without_sql() -> None:
    state = create_initial_state("Summarize current 华南 E302 alarm evidence.")
    task = TaskItem(
        "alarms",
        "Return current_window 华南 E302 total count, status/severity/error-code "
        "distributions, affected CDN, and bounded raw message samples.",
        "query",
        status="executed",
    )
    state["task_plan"] = [task]
    state["pending_tasks"] = [task]
    state["current_task"] = task
    result = _raw_alarm_tool_result()
    model = RecordingModel(
        [
            json.dumps(
                {
                    "decision": "approve",
                    "reason_summary": (
                        "The deterministic Tool summary satisfies the task."
                    ),
                    "issues": [],
                    "retry_instruction": None,
                    "confidence": 0.99,
                }
            )
        ]
    )

    review = Reviewer(model_client=model).review(
        state,
        task,
        result,
        trace=_trace(state),
    )

    assert review.decision == "approve"
    assert task.status == "completed"
    assert "alarm_evidence" in model.calls[0]["user_prompt"]
    assert "do not require a SQL GROUP BY" in model.calls[0]["system_prompt"]


def test_alarm_summary_does_not_bypass_missing_scope_review() -> None:
    state = create_initial_state("Summarize current_window 华南 E302 alarms.")
    task = TaskItem(
        "alarms",
        "Return current_window 华南 E302 alarm evidence.",
        "query",
        status="executed",
    )
    state["task_plan"] = [task]
    state["pending_tasks"] = [task]
    state["current_task"] = task
    result = _raw_alarm_tool_result()
    result.tool_input = {"error_code": "E302"}
    model = RecordingModel(
        [
            json.dumps(
                {
                    "decision": "retry",
                    "reason_summary": "Required region and time filters are absent.",
                    "issues": [
                        {
                            "issue_type": "filter_mismatch",
                            "description": (
                                "Tool input omits current_window and 华南."
                            ),
                        }
                    ],
                    "retry_instruction": (
                        "Apply current_window and region=华南 without changing scope."
                    ),
                    "confidence": 0.99,
                },
                ensure_ascii=False,
            )
        ]
    )

    review = Reviewer(model_client=model).review(
        state,
        task,
        result,
        trace=_trace(state),
    )

    assert review.decision == "retry"
    assert task.status == "pending"
    assert review.issues[0].issue_type == "filter_mismatch"


def test_final_answer_retries_instead_of_saying_mixed_alarms_all_resolved() -> None:
    state = create_initial_state(
        "华南地区播放成功率下降，并出现 E302 告警，应该怎么排查？"
    )
    retrieve_into_state(state, KnowledgeRetriever.from_directory(KNOWLEDGE), top_k=5)
    qoe = TaskItem("qoe", "Compare QoE by CDN.", "query", status="completed")
    alarms = TaskItem(
        "alarms",
        "Return E302 alarm status distribution.",
        "query",
        status="completed",
    )
    analysis = TaskItem(
        "analysis",
        "Correlate the approved evidence.",
        "analysis",
        ["qoe", "alarms"],
        status="completed",
    )
    response = TaskItem(
        "answer",
        "Answer with evidence boundaries.",
        "response",
        ["analysis"],
    )
    state["task_plan"] = [qoe, alarms, analysis, response]
    state["completed_tasks"] = [qoe, alarms, analysis]
    state["pending_tasks"] = [response]
    state["current_task"] = response
    state["sql_results"] = [_qoe_result(), _alarm_result()]
    state["review_results"] = [
        ReviewerResult("qoe", "approve", "Verified.", confidence=1.0),
        ReviewerResult("alarms", "approve", "Verified.", confidence=1.0),
    ]
    state["analysis_results"] = [
        AnalysisResult(
            task_id="analysis",
            summary="CDN-B has the largest decline; E302 overlaps in time.",
            source_task_ids=["qoe", "alarms"],
        )
    ]
    citation = state["knowledge_evidence"][0]["chunk_id"]
    data_ids = [
        "data:comparison:1:overall",
        "data:comparison:1:group:1",
        "data:comparison:1:group:2",
        "data:comparison:1:group:3",
        "data:status:1",
    ]
    limitation_ids = [
        "limitation:correlation_not_causation",
        "limitation:missing_trace_data",
        "limitation:missing_origin_metrics",
        "limitation:missing_routing_change_records",
        "limitation:limited_time_windows",
    ]
    invalid = {
        "data_evidence_ids": data_ids,
        "knowledge_evidence_ids": [f"knowledge:{citation}"],
        "inferences": [
            {
                "claim_type": "observation",
                "predicate": "general",
                "polarity": "neutral",
                "subject_evidence_ids": [],
                "statement": "All E302 alarms were resolved.",
                "supporting_evidence_ids": ["data:status:1"],
            }
        ],
        "limitation_ids": limitation_ids,
        "source_task_ids": ["analysis"],
    }
    valid = {
        "data_evidence_ids": data_ids,
        "knowledge_evidence_ids": [f"knowledge:{citation}"],
        "inferences": [
            {
                "bundle_id": "bundle:entity:ALL:cdn:CDN-B",
                "claim_type": "correlation",
                "predicate": "general",
                "polarity": "neutral",
                "subject_evidence_ids": [],
                "supporting_evidence_ids": [
                    "data:comparison:1:group:2",
                    "data:status:1",
                    f"knowledge:{citation}",
                ],
            }
        ],
        "limitation_ids": limitation_ids,
        "source_task_ids": ["analysis"],
    }
    model = RecordingModel(
        [
            json.dumps(invalid, ensure_ascii=False),
            json.dumps(valid, ensure_ascii=False),
        ]
    )

    result = Analyst(model_client=model).generate_final_answer(
        state,
        response,
        trace=_trace(state),
    )

    assert result.retry_count == 1
    assert "open=1" in result.answer
    assert "healthy" not in result.answer
    assert "does not prove causation" in result.answer
    assert result.claims[0]["claim_type"] == "correlation"
    assert result.validator_result["valid"] is True
    assert len(model.calls) == 2
    assert "Final Evidence Projection" in model.calls[0]["user_prompt"]


def test_declining_groups_cannot_be_called_healthy_controls() -> None:
    contract = {**_contract(), "status_distributions": []}

    invalid = evidence_accuracy_violations(
        "CDN-B dropped most; use CDN-A and CDN-C as healthy controls.",
        contract=contract,
    )
    valid = evidence_accuracy_violations(
        "CDN-B dropped most; CDN-A and CDN-C also declined, but by less.",
        contract=contract,
    )

    assert any(
        "declining groups cannot be described as healthy" in item
        for item in invalid
    )
    assert valid == []


def test_all_declining_groups_normalize_healthy_control_wording() -> None:
    contract = _contract()

    normalized = normalize_evidence_language(
        "Use CDN-A and CDN-C as healthy controls / 健康对照组.",
        contract=contract,
    )

    assert "healthy" not in normalized.lower()
    assert "健康对照组" not in normalized
    assert "smaller-decline comparisons" in normalized
    assert "降幅较小的比较组" in normalized


def test_correlation_cannot_be_upgraded_to_causation() -> None:
    contract = {**_contract(), "status_distributions": []}

    invalid = evidence_accuracy_violations(
        "E302 caused the playback success decline.",
        contract=contract,
    )
    valid = evidence_accuracy_violations(
        "E302 is highly correlated and is one possible factor; causality is unproven.",
        contract=contract,
    )

    assert "correlation is incorrectly stated as proven causation" in invalid
    assert valid == []


def test_mixed_answer_requires_all_four_evidence_sections() -> None:
    contract = _contract()

    invalid = evidence_accuracy_violations(
        "DATA EVIDENCE\nobserved facts.\nKNOWLEDGE EVIDENCE\nSOP.\n"
        "INFERENCE\nlikely.",
        contract=contract,
        require_sections=True,
    )
    valid = evidence_accuracy_violations(
        "数据证据\nopen: 1, investigating: 1, resolved: 1; CDN-A and "
        "CDN-C also declined.\n知识证据\nSOP.\n推断\npossible.\n"
        "局限\ncausality is unproven.",
        contract=contract,
        require_sections=True,
    )

    assert any("LIMITATION" in item for item in invalid)
    assert valid == []


def test_evidence_contract_compares_separate_current_and_baseline_queries() -> None:
    combined = _qoe_result()
    current_rows = [
        {key: value for key, value in row.items() if key != "window_name"}
        for row in combined.rows
        if row["window_name"] == "current_window"
    ]
    baseline_rows = [
        {key: value for key, value in row.items() if key != "window_name"}
        for row in combined.rows
        if row["window_name"] == "previous_window"
    ]
    columns = [column for column in combined.columns if column != "window_name"]
    current = SQLResult(
        "q1_current",
        "SELECT * FROM stream_sessions WHERE window_name = 'current_window'",
        columns=columns,
        rows=current_rows,
        row_count=3,
    )
    baseline = SQLResult(
        "q2_previous",
        "SELECT * FROM stream_sessions WHERE window_name = 'previous_window'",
        columns=columns,
        rows=baseline_rows,
        row_count=3,
    )

    comparison = build_evidence_contract([current, baseline])["comparisons"][0]
    deltas = {row["group"]: row["delta"] for row in comparison["groups"]}

    assert comparison["largest_decline_group"] == "CDN-B"
    assert deltas == pytest.approx(
        {"CDN-A": -0.10, "CDN-B": -0.45, "CDN-C": -0.10}
    )
    assert comparison["overall"]["baseline_rate"] == 56 / 60
    assert comparison["overall"]["current_rate"] == 43 / 60


def _synthetic_knowledge() -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": "synthetic::e302",
            "title": "E302 upstream timeout",
            "category": "error_code",
            "source": "synthetic.md",
            "text": (
                "E302 is an upstream-timeout symptom. Inspect the CDN and "
                "origin path; temporal overlap alone is not causal proof."
            ),
        }
    ]


def _selection(
    pack: Mapping[str, Any],
    *,
    claim_type: str,
    statement: str,
    support: list[str],
    predicate: str = "general",
    polarity: str = "neutral",
    subject_evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "data_evidence_ids": [
            item["evidence_id"] for item in pack["data_evidence"]
        ],
        "knowledge_evidence_ids": [
            item["evidence_id"] for item in pack["knowledge_evidence"]
        ],
        "inferences": [
            {
                "claim_type": claim_type,
                "predicate": predicate,
                "polarity": polarity,
                "subject_evidence_ids": list(subject_evidence_ids or []),
                "statement": statement,
                "supporting_evidence_ids": support,
            }
        ],
        "limitation_ids": [
            item["limitation_id"] for item in pack["limitations"]
        ],
        "source_task_ids": ["analysis"],
    }


def test_current_only_data_cannot_claim_largest_decline() -> None:
    combined = _qoe_result()
    rows = [
        {key: value for key, value in row.items() if key != "window_name"}
        for row in combined.rows
        if row["window_name"] == "current_window"
    ]
    result = SQLResult(
        "current",
        "SELECT cdn, playback_success_rate FROM stream_sessions",
        columns=["cdn", "playback_success_rate"],
        rows=rows,
        row_count=3,
    )
    pack = build_final_evidence_pack([result], [])
    selection = _selection(
        pack,
        claim_type="observation",
        statement="CDN-B has the largest decline.",
        support=["data:result:1"],
    )

    answer = render_final_answer(pack, selection)

    assert not any(
        item["kind"] == "group_comparison" for item in pack["data_evidence"]
    )
    assert final_claim_violations(selection, pack) == []
    assert "largest decline" not in answer
    assert "下降幅度最大" not in answer


def test_paired_evidence_contains_delta_and_relative_change() -> None:
    pack = build_final_evidence_pack([_qoe_result()], [])
    groups = {
        item["facts"]["group"]: item["facts"]
        for item in pack["data_evidence"]
        if item["kind"] == "group_comparison"
    }

    assert groups["CDN-A"]["delta"] == pytest.approx(-0.10)
    assert groups["CDN-B"]["delta"] == pytest.approx(-0.45)
    assert groups["CDN-B"]["relative_change"] == pytest.approx(-0.45 / 0.95)
    assert groups["CDN-C"]["delta"] == pytest.approx(-0.10)


def test_largest_decline_is_a_structured_evidence_fact() -> None:
    pack = build_final_evidence_pack([_qoe_result()], [])
    largest = [
        item
        for item in pack["data_evidence"]
        if item.get("facts", {}).get("is_largest_decline")
    ]

    assert len(largest) == 1
    assert largest[0]["facts"]["group"] == "CDN-B"
    assert largest[0]["facts"]["delta"] == pytest.approx(-0.45)


@pytest.mark.parametrize("claim_type", ["correlation", "hypothesis"])
def test_noncausal_grounded_claim_types_are_allowed(claim_type: str) -> None:
    pack = build_final_evidence_pack(
        [_qoe_result(), _alarm_result()],
        _synthetic_knowledge(),
    )
    knowledge_id = pack["knowledge_evidence"][0]["evidence_id"]
    selection = _selection(
        pack,
        claim_type=claim_type,
        statement="The upstream-timeout evidence is a priority candidate factor.",
        support=[
            "data:comparison:1:group:2",
            "data:status:1",
            knowledge_id,
        ],
    )

    assert final_claim_violations(selection, pack) == []


def test_unsupported_causal_claim_is_rejected() -> None:
    pack = build_final_evidence_pack(
        [_qoe_result(), _alarm_result()],
        _synthetic_knowledge(),
    )
    knowledge_id = pack["knowledge_evidence"][0]["evidence_id"]
    selection = _selection(
        pack,
        claim_type="causal_claim",
        statement="The upstream timeout is the confirmed root cause.",
        support=["data:comparison:1:group:2", knowledge_id],
    )

    violations = final_claim_violations(selection, pack)

    assert any("lacks approved causal evidence" in item for item in violations)


def test_debug_causal_wording_cannot_change_correlation_renderer() -> None:
    pack = build_final_evidence_pack(
        [_qoe_result(), _alarm_result()],
        _synthetic_knowledge(),
    )
    knowledge_id = pack["knowledge_evidence"][0]["evidence_id"]
    selection = _selection(
        pack,
        claim_type="correlation",
        statement="The upstream timeout caused the QoE decline.",
        support=["data:comparison:1:group:2", knowledge_id],
    )

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert "caused" not in answer
    assert "不足以证明因果关系" in answer


def test_debug_text_cannot_introduce_number_into_rendered_answer() -> None:
    pack = build_final_evidence_pack(
        [_qoe_result(), _alarm_result()],
        _synthetic_knowledge(),
    )
    knowledge_id = pack["knowledge_evidence"][0]["evidence_id"]
    selection = _selection(
        pack,
        claim_type="hypothesis",
        statement="The candidate factor explains 77% of the incident.",
        support=["data:comparison:1:group:2", knowledge_id],
    )

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert "77%" not in answer


@pytest.mark.parametrize(
    "statement",
    [
        "CDN-A is healthy.",
        "CDN-A is unaffected.",
        "CDN-A is a stable control.",
    ],
)
def test_positive_stable_control_claim_is_rejected_for_declining_group(
    statement: str,
) -> None:
    pack = build_final_evidence_pack([_qoe_result()], [])
    selection = _selection(
        pack,
        claim_type="observation",
        predicate="stable_control",
        polarity="positive",
        subject_evidence_ids=["data:comparison:1:group:1"],
        statement=statement,
        support=["data:comparison:1:group:1"],
    )

    violations = final_claim_violations(selection, pack)

    assert any("positive stable_control" in item for item in violations)


@pytest.mark.parametrize(
    "statement",
    [
        "CDN-A is not healthy.",
        "CDN-A is not unaffected.",
        "CDN-A cannot be treated as a healthy control.",
        "CDN-A cannot be treated as an unaffected control.",
        "No declining CDN can be treated as a stable unaffected control.",
    ],
)
def test_negative_stable_control_claim_is_allowed_for_declining_group(
    statement: str,
) -> None:
    pack = build_final_evidence_pack([_qoe_result()], [])
    selection = _selection(
        pack,
        claim_type="observation",
        predicate="stable_control",
        polarity="negative",
        subject_evidence_ids=["data:comparison:1:group:1"],
        statement=statement,
        support=["data:comparison:1:group:1"],
    )

    assert final_claim_violations(selection, pack) == []


def test_stable_control_semantics_require_traceable_group_subjects() -> None:
    pack = build_final_evidence_pack([_qoe_result()], [])
    selection = _selection(
        pack,
        claim_type="observation",
        predicate="stable_control",
        polarity="negative",
        subject_evidence_ids=["data:missing"],
        statement="This group cannot be treated as an unaffected control.",
        support=["data:comparison:1:group:1"],
    )

    violations = final_claim_violations(selection, pack)

    assert any("unselected subject evidence" in item for item in violations)


def test_negative_stable_control_claim_renders_deterministic_chinese() -> None:
    pack = build_final_evidence_pack([_qoe_result()], [])
    selection = _selection(
        pack,
        claim_type="observation",
        predicate="stable_control",
        polarity="negative",
        subject_evidence_ids=[
            "data:comparison:1:group:1",
            "data:comparison:1:group:3",
        ],
        statement="Debug wording must not become final prose.",
        support=[
            "data:comparison:1:group:1",
            "data:comparison:1:group:3",
        ],
    )

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert "CDN-A 和 CDN-C 出现下降" in answer
    assert "不能作为稳定、未受影响的对照组" in answer
    assert "previous_window" in answer
    assert "Debug wording" not in answer


def test_final_evidence_pack_sources_are_traceable() -> None:
    pack = build_final_evidence_pack(
        [_qoe_result(), _alarm_result()],
        _synthetic_knowledge(),
    )

    assert evidence_pack_source_violations(pack) == []


def test_tool_alarm_evidence_survives_sql_correction_in_final_pack() -> None:
    merged = merge_correction_evidence(
        _raw_alarm_tool_result(),
        _alarm_correction_result(),
    )
    mark_evidence_review_status(merged, "approve")

    pack = build_final_evidence_pack([merged], [])
    alarm = next(
        item
        for item in pack["data_evidence"]
        if item["kind"] == "alarm_status_distribution"
    )

    assert alarm["facts"]["total"] == 3
    assert alarm["facts"]["distribution"] == [
        {"value": "investigating", "count": 1},
        {"value": "open", "count": 1},
        {"value": "resolved", "count": 1},
    ]
    assert alarm["facts"]["severity_distribution"] == {"high": 3}
    assert alarm["facts"]["error_code_distribution"] == {"E302": 3}
    assert alarm["facts"]["cdn_distribution"] == {"CDN-B": 3}
    assert len(alarm["facts"]["sample_events"]) == 3
    assert (
        alarm["facts"]["sample_semantics"]
        == "bounded_examples_not_distribution"
    )


def test_sql_correction_supplements_instead_of_replacing_tool_evidence() -> None:
    base = _raw_alarm_tool_result()
    merged = merge_correction_evidence(base, _alarm_correction_result())
    mark_evidence_review_status(merged, "approve")

    pack = build_final_evidence_pack([merged], [])
    kinds = {item["kind"] for item in pack["data_evidence"]}

    assert "alarm_status_distribution" in kinds
    assert "reviewed_result_summary" in kinds
    assert (
        merged.tool_metadata["preserved_tool_evidence"]["alarm_evidence"]
        == base.tool_metadata["alarm_evidence"]
    )


def test_log_tool_rows_and_sql_correction_are_both_reviewed_evidence() -> None:
    merged = merge_correction_evidence(
        _log_tool_result(),
        _log_correction_result(),
    )
    mark_evidence_review_status(merged, "approve")

    pack = build_final_evidence_pack([merged], [])
    evidence_by_id = {
        item["evidence_id"]: item for item in pack["data_evidence"]
    }

    assert evidence_by_id["data:tool-result:1"]["facts"]["row_count"] == 4
    assert (
        evidence_by_id["data:tool-result:1"]["facts"]["evidence_role"]
        == "base_tool_evidence"
    )
    assert evidence_by_id["data:result:1"]["facts"]["row_count"] == 2
    assert (
        evidence_by_id["data:result:1"]["facts"]["evidence_role"]
        == "correction_evidence"
    )
    assert evidence_pack_source_violations(pack) == []


def test_explicit_correction_supersedes_only_named_tool_field() -> None:
    merged = merge_correction_evidence(
        _raw_alarm_tool_result(),
        _alarm_correction_result(),
        superseded_fields=["alarm_evidence.count_by_severity"],
    )
    mark_evidence_review_status(merged, "approve")

    pack = build_final_evidence_pack([merged], [])
    alarm = next(
        item
        for item in pack["data_evidence"]
        if item["kind"] == "alarm_status_distribution"
    )
    lineage = pack["reviewed_evidence"][0]

    assert alarm["facts"]["severity_distribution"] == {}
    assert alarm["facts"]["error_code_distribution"] == {"E302": 3}
    assert alarm["facts"]["distribution"]
    assert lineage["superseded_fields"] == [
        "alarm_evidence.count_by_severity"
    ]


def test_reviewed_evidence_provenance_keeps_tool_and_correction_sources() -> None:
    merged = merge_correction_evidence(
        _raw_alarm_tool_result(),
        _alarm_correction_result(),
    )
    mark_evidence_review_status(merged, "approve")

    pack = build_final_evidence_pack([merged], [])
    lineage = pack["reviewed_evidence"][0]
    roles = {item["role"] for item in lineage["evidence_sources"]}
    source_ids = {
        item["source_id"] for item in lineage["evidence_sources"]
    }

    assert lineage["final_review_status"] == "approve"
    assert roles == {"base_tool_evidence", "correction_evidence"}
    assert source_ids == {
        "source:alarms:tool:get_alarm_events",
        "source:alarms:sql-correction:1",
    }
    assert evidence_pack_source_violations(pack) == []


def test_corrected_media_evidence_pack_validates_and_renders_all_sections() -> None:
    alarms = merge_correction_evidence(
        _raw_alarm_tool_result(),
        _alarm_correction_result(),
    )
    logs = merge_correction_evidence(
        _log_tool_result(),
        _log_correction_result(),
    )
    mark_evidence_review_status(alarms, "approve")
    mark_evidence_review_status(logs, "approve")
    pack = build_final_evidence_pack(
        [_qoe_result(), alarms, logs],
        _synthetic_knowledge(),
    )
    selection = _selection(
        pack,
        claim_type="correlation",
        statement="The reviewed QoE, alarm, and log evidence overlaps.",
        support=[
            "data:comparison:1:group:2",
            "data:status:1",
            "data:tool-result:3",
        ],
    )

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert {item["kind"] for item in pack["data_evidence"]} >= {
        "group_comparison",
        "alarm_status_distribution",
        "reviewed_tool_result_summary",
        "reviewed_result_summary",
    }
    assert "investigating=1, open=1, resolved=1" in answer
    assert all(
        heading in answer
        for heading in (
            "DATA EVIDENCE",
            "KNOWLEDGE EVIDENCE",
            "INFERENCE",
            "LIMITATION",
        )
    )


def test_data_plus_data_correlation_is_valid_without_knowledge() -> None:
    pack = build_final_evidence_pack([_qoe_result(), _alarm_result()], [])
    selection = _selection(
        pack,
        claim_type="correlation",
        statement="E302 and the CDN-B decline occurred in the reviewed window.",
        support=["data:comparison:1:group:2", "data:status:1"],
    )

    assert final_claim_violations(selection, pack) == []


def test_correlation_without_lexical_modality_is_valid() -> None:
    pack = build_final_evidence_pack([_qoe_result(), _alarm_result()], [])
    selection = _selection(
        pack,
        claim_type="correlation",
        statement="E302 and the CDN-B metric changed in the reviewed window.",
        support=["data:comparison:1:group:2", "data:status:1"],
    )

    assert final_claim_violations(selection, pack) == []


def test_knowledge_claim_type_requires_knowledge_evidence() -> None:
    pack = build_final_evidence_pack([_qoe_result(), _alarm_result()], [])
    selection = _selection(
        pack,
        claim_type="knowledge",
        statement="E302 means an upstream timeout.",
        support=["data:comparison:1:group:2", "data:status:1"],
    )

    violations = final_claim_violations(selection, pack)

    assert any("knowledge lacks knowledge evidence" in item for item in violations)

    grounded_pack = build_final_evidence_pack(
        [_qoe_result(), _alarm_result()],
        _synthetic_knowledge(),
    )
    knowledge_id = grounded_pack["knowledge_evidence"][0]["evidence_id"]
    grounded = _selection(
        grounded_pack,
        claim_type="knowledge",
        statement="E302 means an upstream timeout.",
        support=[
            "data:comparison:1:group:2",
            "data:status:1",
            knowledge_id,
        ],
    )
    assert final_claim_violations(grounded, grounded_pack) == []


@pytest.mark.parametrize(
    "statement",
    [
        "Shared origin pressure is the working hypothesis.",
        "A shared origin issue cannot be excluded.",
    ],
)
def test_hypothesis_data_plus_limitation_is_valid_without_required_modal_words(
    statement: str,
) -> None:
    pack = build_final_evidence_pack([_qoe_result(), _alarm_result()], [])
    selection = _selection(
        pack,
        claim_type="hypothesis",
        statement=statement,
        support=[
            "data:comparison:1:group:2",
            "limitation:missing_origin_metrics",
        ],
    )

    assert final_claim_violations(selection, pack) == []


@pytest.mark.parametrize(
    "statement",
    [
        "Shared origin pressure is the proven root cause.",
        "共享 origin 导致了播放成功率下降。",
        "共享 origin 造成了播放成功率下降。",
    ],
)
def test_hypothesis_debug_wording_does_not_become_causal_semantics(
    statement: str,
) -> None:
    pack = build_final_evidence_pack([_qoe_result(), _alarm_result()], [])
    selection = _selection(
        pack,
        claim_type="hypothesis",
        statement=statement,
        support=[
            "data:comparison:1:group:2",
            "limitation:missing_origin_metrics",
        ],
    )

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert statement not in answer
    assert "尚不能确认其为根因" in answer


def test_correlation_renderer_is_deterministically_noncausal() -> None:
    pack = build_final_evidence_pack([_qoe_result(), _alarm_result()], [])
    selection = _selection(
        pack,
        claim_type="correlation",
        statement="Debug wording must not become final prose.",
        support=["data:comparison:1:group:2", "data:status:1"],
    )

    answer = render_final_answer(pack, selection)

    assert "存在相关性" in answer
    assert "不足以证明因果关系" in answer
    assert "Debug wording" not in answer


def test_hypothesis_renderer_is_deterministically_cautious() -> None:
    pack = build_final_evidence_pack([_qoe_result(), _alarm_result()], [])
    selection = _selection(
        pack,
        claim_type="hypothesis",
        statement="Debug wording must not become final prose.",
        support=[
            "data:comparison:1:group:2",
            "limitation:missing_origin_metrics",
        ],
    )

    answer = render_final_answer(pack, selection)

    assert "需要优先验证的候选影响因素" in answer
    assert "尚不能确认其为根因" in answer
    assert "Debug wording" not in answer


def test_deterministic_renderer_outputs_four_evidence_sections() -> None:
    pack = build_final_evidence_pack(
        [_qoe_result(), _alarm_result()],
        _synthetic_knowledge(),
    )
    knowledge_id = pack["knowledge_evidence"][0]["evidence_id"]
    selection = _selection(
        pack,
        claim_type="correlation",
        statement="The upstream-timeout symptom is a priority candidate factor.",
        support=["data:comparison:1:group:2", "data:status:1", knowledge_id],
    )

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert all(
        heading in answer
        for heading in (
            "DATA EVIDENCE",
            "KNOWLEDGE EVIDENCE",
            "INFERENCE",
            "LIMITATION",
        )
    )
    assert "open=1" in answer
    assert "CDN-A" in answer and "classification=declined" in answer


def _group_facts(pack: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["facts"]["group"]): dict(item["facts"])
        for item in pack["data_evidence"]
        if item["kind"] == "group_comparison"
    }


def _status_evidence_for_code(
    pack: Mapping[str, Any],
    error_code: str,
) -> dict[str, Any]:
    return next(
        item
        for item in pack["data_evidence"]
        if item["kind"] == "alarm_status_distribution"
        and item["scope"]["error_code"] == error_code
    )


def test_single_task_comparison_normalizes_to_group_evidence() -> None:
    facts = _group_facts(build_final_evidence_pack([_qoe_result()], []))

    assert facts["CDN-A"]["delta"] == pytest.approx(-0.10)
    assert facts["CDN-B"]["delta"] == pytest.approx(-0.45)
    assert facts["CDN-C"]["delta"] == pytest.approx(-0.10)


def test_split_task_comparison_normalizes_to_group_evidence() -> None:
    baseline, current = _split_qoe_results()
    pack = build_final_evidence_pack([baseline, current], [])
    facts = _group_facts(pack)

    assert facts["CDN-A"]["delta"] == pytest.approx(-0.10)
    assert facts["CDN-B"]["delta"] == pytest.approx(-0.45)
    assert facts["CDN-C"]["delta"] == pytest.approx(-0.10)
    assert facts["CDN-B"]["is_largest_decline"] is True


def test_single_and_split_planner_shapes_have_identical_comparison_values() -> None:
    baseline, current = _split_qoe_results()
    single = _group_facts(build_final_evidence_pack([_qoe_result()], []))
    split = _group_facts(build_final_evidence_pack([baseline, current], []))

    fields = ("baseline_value", "current_value", "delta", "classification")
    assert {
        group: {field: facts[field] for field in fields}
        for group, facts in single.items()
    } == {
        group: {field: facts[field] for field in fields}
        for group, facts in split.items()
    }


def test_cross_task_comparison_rejects_region_mismatch() -> None:
    baseline, current = _split_qoe_results(current_region="华北")

    assert _group_facts(build_final_evidence_pack([baseline, current], [])) == {}


def test_cross_task_comparison_rejects_metric_mismatch() -> None:
    baseline, current = _split_qoe_results()
    current.columns[-1] = "rebuffer_ratio"
    for row in current.rows:
        row["rebuffer_ratio"] = row.pop("playback_success_rate")

    assert _group_facts(build_final_evidence_pack([baseline, current], [])) == {}


def test_cross_task_comparison_rejects_unit_mismatch() -> None:
    baseline, current = _split_qoe_results()
    current.tool_metadata["units"] = {"playback_success_rate": "percent"}

    assert _group_facts(build_final_evidence_pack([baseline, current], [])) == {}


def test_cross_task_comparison_rejects_aggregation_mismatch() -> None:
    baseline, current = _split_qoe_results()
    current.tool_metadata["aggregation_semantics"] = {
        "playback_success_rate": "avg(preaggregated_rate)"
    }

    assert _group_facts(build_final_evidence_pack([baseline, current], [])) == {}


def test_cross_task_comparison_rejects_group_by_mismatch() -> None:
    baseline, current = _split_qoe_results()
    current.tool_input["group_by"] = ["device"]
    current.tool_metadata["dimensions"] = ["device"]
    current.columns[0] = "device"
    for row in current.rows:
        row["device"] = row.pop("cdn")

    assert _group_facts(build_final_evidence_pack([baseline, current], [])) == {}


def test_cross_task_comparison_requires_both_windows() -> None:
    baseline, _ = _split_qoe_results()

    assert _group_facts(build_final_evidence_pack([baseline], [])) == {}


def test_cross_task_comparison_requires_reviewer_approved_results() -> None:
    baseline, current = _split_qoe_results()
    current.tool_metadata["review_status"] = "retry"

    assert _group_facts(build_final_evidence_pack([baseline, current], [])) == {}


def test_cross_task_comparison_fails_closed_on_group_key_mismatch() -> None:
    baseline, current = _split_qoe_results()
    current.rows.pop()
    current.row_count = len(current.rows)

    assert _group_facts(build_final_evidence_pack([baseline, current], [])) == {}


def test_cross_task_comparison_records_both_source_provenances() -> None:
    baseline, current = _split_qoe_results()
    pack = build_final_evidence_pack([baseline, current], [])
    evidence = next(
        item
        for item in pack["data_evidence"]
        if item["kind"] == "group_comparison"
    )
    provenance = evidence["facts"]["provenance"]

    assert provenance["source_task_ids"] == [
        "baseline_metrics",
        "current_metrics",
    ]
    assert provenance["baseline_source_id"] == (
        "source:baseline_metrics:tool:query_qoe_metrics"
    )
    assert provenance["current_source_id"] == (
        "source:current_metrics:tool:query_qoe_metrics"
    )
    assert provenance["scope_filter_fingerprint"]
    assert provenance["derivation"] == "deterministic_cross_task_pair"
    assert evidence_pack_source_violations(pack) == []


def test_alarm_contract_keeps_all_and_e302_scopes_distinct() -> None:
    pack = build_final_evidence_pack([_multi_code_alarm_tool_result()], [])
    all_alarms = _status_evidence_for_code(pack, "ALL")
    e302 = _status_evidence_for_code(pack, "E302")

    assert all_alarms["facts"]["total"] == 6
    assert all_alarms["facts"]["distribution"] == [
        {"value": "investigating", "count": 2},
        {"value": "open", "count": 1},
        {"value": "resolved", "count": 3},
    ]
    assert e302["facts"]["total"] == 3
    assert e302["facts"]["distribution"] == [
        {"value": "investigating", "count": 1},
        {"value": "open", "count": 1},
        {"value": "resolved", "count": 1},
    ]
    assert e302["scope"]["region"] == "华南"
    assert e302["scope"]["cdn"] == "CDN-B"


def test_e302_claim_using_e302_distribution_is_valid() -> None:
    pack = build_final_evidence_pack([_multi_code_alarm_tool_result()], [])
    e302 = _status_evidence_for_code(pack, "E302")
    selection = _selection(
        pack,
        claim_type="observation",
        statement=(
            "3 E302 alarms have open=1, investigating=1, and resolved=1."
        ),
        support=[e302["evidence_id"]],
    )

    assert final_claim_violations(selection, pack) == []


def test_all_alarm_distribution_cannot_be_rewritten_by_debug_text() -> None:
    pack = build_final_evidence_pack([_multi_code_alarm_tool_result()], [])
    all_alarms = _status_evidence_for_code(pack, "ALL")
    selection = _selection(
        pack,
        claim_type="observation",
        statement=(
            "3 E302 alarms have open=1, investigating=2, and resolved=3."
        ),
        support=[all_alarms["evidence_id"]],
    )

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert "3 E302 alarms have" not in answer
    assert "total=6" in answer


@pytest.mark.parametrize("mismatch", ["region", "windows"])
def test_claim_scope_guard_rejects_region_or_window_mismatch(
    mismatch: str,
) -> None:
    left = _raw_alarm_tool_result()
    right = deepcopy(_raw_alarm_tool_result())
    right.task_id = "other_alarms"
    if mismatch == "region":
        right.tool_input["region"] = "华北"
        for row in right.rows:
            row["region"] = "华北"
    else:
        right.tool_input["windows"] = ["previous_window"]
        for row in right.rows:
            row["window_name"] = "previous_window"
    right.tool_metadata["alarm_evidence"] = build_alarm_evidence(right.rows)
    pack = build_final_evidence_pack([left, right], [])
    status_ids = [
        item["evidence_id"]
        for item in pack["data_evidence"]
        if item["kind"] == "alarm_status_distribution"
    ]
    selection = _selection(
        pack,
        claim_type="correlation",
        statement="The two reviewed alarm groups overlap.",
        support=status_ids,
    )

    violations = final_claim_violations(selection, pack)

    assert any(f"incompatible {mismatch}" in item for item in violations)


def test_observation_requires_data_not_only_knowledge() -> None:
    pack = build_final_evidence_pack([], _synthetic_knowledge())
    knowledge_id = pack["knowledge_evidence"][0]["evidence_id"]
    selection = _selection(
        pack,
        claim_type="observation",
        statement="E302 is documented as an upstream-timeout symptom.",
        support=[knowledge_id],
    )

    assert any(
        "observation lacks data evidence" in item
        for item in final_claim_violations(selection, pack)
    )


def test_knowledge_definition_requires_knowledge_but_not_data() -> None:
    pack = build_final_evidence_pack([], _synthetic_knowledge())
    knowledge_id = pack["knowledge_evidence"][0]["evidence_id"]
    selection = _selection(
        pack,
        claim_type="knowledge",
        statement="E302 means an upstream-timeout symptom.",
        support=[knowledge_id],
    )

    assert final_claim_violations(selection, pack) == []


def test_hypothesis_without_data_is_invalid() -> None:
    pack = build_final_evidence_pack([], _synthetic_knowledge())
    knowledge_id = pack["knowledge_evidence"][0]["evidence_id"]
    selection = _selection(
        pack,
        claim_type="hypothesis",
        statement="Origin pressure is a candidate explanation.",
        support=[knowledge_id, "limitation:knowledge_not_observed_data"],
    )

    violations = final_claim_violations(selection, pack)

    assert any("lacks data evidence" in item for item in violations)


def test_recommendation_accepts_knowledge_plus_current_data() -> None:
    pack = build_final_evidence_pack([_qoe_result()], _synthetic_knowledge())
    knowledge_id = pack["knowledge_evidence"][0]["evidence_id"]
    selection = _selection(
        pack,
        claim_type="recommendation",
        statement="Inspect the CDN and origin path described by the SOP.",
        support=["data:comparison:1:group:2", knowledge_id],
    )

    assert final_claim_violations(selection, pack) == []


def test_recommendation_accepts_knowledge_plus_limitation() -> None:
    pack = build_final_evidence_pack(
        [_qoe_result(), _alarm_result()],
        _synthetic_knowledge(),
    )
    knowledge_id = pack["knowledge_evidence"][0]["evidence_id"]
    selection = _selection(
        pack,
        claim_type="recommendation",
        statement="Inspect missing origin telemetry according to the SOP.",
        support=[knowledge_id, "limitation:missing_origin_metrics"],
    )

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert "[recommendation]" in answer
    assert "建议进一步检查或验证" in answer


def test_negated_causal_wording_is_not_a_noncausal_type_violation() -> None:
    pack = build_final_evidence_pack([], _synthetic_knowledge())
    knowledge_id = pack["knowledge_evidence"][0]["evidence_id"]
    selection = _selection(
        pack,
        claim_type="knowledge",
        statement="This knowledge does not prove E302 is the root cause.",
        support=[knowledge_id],
    )

    assert final_claim_violations(selection, pack) == []


def test_split_task_negative_stable_control_is_valid() -> None:
    baseline, current = _split_qoe_results()
    pack = build_final_evidence_pack([baseline, current], [])
    selection = _selection(
        pack,
        claim_type="observation",
        predicate="stable_control",
        polarity="negative",
        subject_evidence_ids=[
            "data:comparison:1:group:1",
            "data:comparison:1:group:3",
        ],
        statement="CDN-A and CDN-C are not stable unaffected controls.",
        support=[
            "data:comparison:1:group:1",
            "data:comparison:1:group:3",
        ],
    )

    assert final_claim_violations(selection, pack) == []


def _phase37_knowledge() -> list[dict[str, Any]]:
    filler = " Synthetic supporting detail." * 45
    return [
        {
            "chunk_id": "phase37::e302",
            "title": "E302 CDN Upstream Timeout",
            "category": "error_code",
            "source": "phase37-error-codes.md",
            "retrieved_reason": "synthetic Phase 3.7 fixture",
            "text": (
                "E302 indicates a CDN upstream-timeout symptom. "
                "It does not by itself prove causation." + filler
            ),
        },
        {
            "chunk_id": "phase37::cdn-sop",
            "title": "CDN Playback Success Degradation SOP",
            "category": "troubleshooting_sop",
            "source": "phase37-sop.md",
            "retrieved_reason": "synthetic Phase 3.7 fixture",
            "text": (
                "For playback success degradation, inspect origin latency, "
                "routing, packet loss, and recent changes. Check E302 overlap "
                "without treating it as causal proof." + filler
            ),
        },
        {
            "chunk_id": "phase37::playback-success",
            "title": "Playback Success Rate",
            "category": "qoe_metric",
            "source": "phase37-qoe.md",
            "retrieved_reason": "synthetic Phase 3.7 fixture",
            "text": (
                "Playback success rate is successful sessions divided by total "
                "sessions for the same scope." + filler
            ),
        },
        {
            "chunk_id": "phase37::startup",
            "title": "Startup Latency SOP",
            "category": "troubleshooting_sop",
            "source": "phase37-startup.md",
            "retrieved_reason": "synthetic Phase 3.7 fixture",
            "text": (
                "Startup latency checks may include playback success rate and "
                "session volume before deeper network checks." + filler
            ),
        },
        {
            "chunk_id": "phase37::transcode",
            "title": "Unrelated Transcode Note",
            "category": "encoding",
            "source": "phase37-transcode.md",
            "retrieved_reason": "synthetic Phase 3.7 fixture",
            "text": "Inspect codec compatibility for failed transcode jobs."
            + filler,
        },
    ]


def _phase37_full_pack() -> dict[str, Any]:
    logs = merge_correction_evidence(
        _log_tool_result(),
        _log_correction_result(),
    )
    mark_evidence_review_status(logs, "approve")
    pack = build_final_evidence_pack(
        [_qoe_result(), _raw_alarm_tool_result(), logs],
        _phase37_knowledge(),
    )
    # Reproduce the real failure shape: canonical lineage remains available to
    # the system, but is irrelevant to claim selection and must not be prompted.
    pack["reviewed_evidence"][0]["diagnostic_lineage"] = {
        "dry_plan": "deterministic-plan:" + ("P" * 9_000),
        "sql_metadata": "reviewed-metadata:" + ("M" * 9_000),
        "raw_rows": [{"debug": "not-model-facing"}],
    }
    return pack


def _projection_by_id(projection: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(item["id"]): item
        for item in projection["data_evidence"]
    }


def test_large_full_pack_projects_within_bounded_context() -> None:
    pack = _phase37_full_pack()
    projection, stats = build_final_evidence_projection(pack, max_chars=10_000)

    assert stats["full_evidence_pack_chars"] > 20_000
    assert stats["projected_evidence_chars"] <= 10_000
    assert stats["compression_ratio"] < 0.5
    assert len(
        json.dumps(
            projection,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    ) <= 10_000


def test_projection_does_not_modify_full_pack() -> None:
    pack = _phase37_full_pack()
    before = deepcopy(pack)

    build_final_evidence_projection(pack, max_chars=10_000)

    assert pack == before


def test_projection_preserves_all_comparison_facts() -> None:
    projection, _ = build_final_evidence_projection(
        _phase37_full_pack(),
        max_chars=10_000,
    )
    by_id = _projection_by_id(projection)

    assert by_id["data:comparison:1:overall"]["facts"] == {
        "metric": "playback_success_rate",
        "baseline_window": "previous_window",
        "baseline_value": pytest.approx(56 / 60),
        "current_window": "current_window",
        "current_value": pytest.approx(43 / 60),
        "delta": pytest.approx((43 / 60) - (56 / 60)),
        "relative_change": pytest.approx(((43 / 60) - (56 / 60)) / (56 / 60)),
    }
    groups = {
        item["facts"]["group"]: item["facts"]
        for item in projection["data_evidence"]
        if item["type"] == "group_comparison"
    }
    assert groups["CDN-A"]["delta"] == pytest.approx(-0.10)
    assert groups["CDN-B"]["delta"] == pytest.approx(-0.45)
    assert groups["CDN-B"]["is_largest_decline"] is True
    assert groups["CDN-C"]["delta"] == pytest.approx(-0.10)


def test_projection_preserves_mixed_alarm_distribution() -> None:
    projection, _ = build_final_evidence_projection(
        _phase37_full_pack(),
        max_chars=10_000,
    )
    alarm = next(
        item
        for item in projection["data_evidence"]
        if item["type"] == "alarm_status_distribution"
    )

    assert alarm["facts"]["total"] == 3
    assert alarm["facts"]["status"] == {
        "investigating": 1,
        "open": 1,
        "resolved": 1,
    }
    assert alarm["facts"]["mixed"] is True
    assert alarm["facts"]["error_code"] == {"E302": 3}


def test_projection_preserves_log_error_warn_distribution() -> None:
    projection, _ = build_final_evidence_projection(
        _phase37_full_pack(),
        max_chars=10_000,
    )
    logs = next(
        item
        for item in projection["data_evidence"]
        if item["id"] == "data:tool-result:3"
    )

    assert logs["facts"]["columns"]["level"] == {"ERROR": 3, "WARN": 1}
    assert logs["facts"]["columns"]["service"] == {
        "cdn-gateway": 2,
        "origin-proxy": 2,
    }
    assert logs["facts"]["columns"]["error_code"] == {"E302": 4}


def test_projection_compacts_knowledge_without_losing_supported_fact() -> None:
    pack = _phase37_full_pack()
    pack["knowledge_evidence"][0]["supported_statement"] += (
        " FULL_KNOWLEDGE_CHUNK_TAIL_MUST_NOT_BE_PROMPTED"
    )

    projection, _ = build_final_evidence_projection(pack, max_chars=10_000)
    e302 = next(
        item
        for item in projection["knowledge_evidence"]
        if item["id"] == "knowledge:phase37::e302"
    )

    assert "upstream-timeout symptom" in e302["supported_fact"]
    assert "FULL_KNOWLEDGE_CHUNK_TAIL" not in e302["supported_fact"]
    assert len(e302["supported_fact"]) < len(
        pack["knowledge_evidence"][0]["supported_statement"]
    )


def test_projection_preserves_required_limitations() -> None:
    pack = _phase37_full_pack()
    projection, _ = build_final_evidence_projection(pack, max_chars=10_000)

    assert {item["id"] for item in projection["limitations"]} == {
        item["limitation_id"] for item in pack["limitations"]
    }


def test_projection_scope_refs_resolve_and_deduplicate() -> None:
    pack = _phase37_full_pack()
    duplicate = deepcopy(pack["data_evidence"][1])
    duplicate["evidence_id"] = "data:duplicate-scope"
    pack["data_evidence"].append(duplicate)
    projection, _ = build_final_evidence_projection(
        pack,
        max_chars=11_000,
    )
    refs = {
        item["id"]: item.get("scope_ref")
        for item in projection["data_evidence"]
    }

    assert all(ref in projection["scopes"] for ref in refs.values() if ref)
    assert refs["data:comparison:1:group:1"] == refs["data:duplicate-scope"]
    assert len(projection["scopes"]) < len([ref for ref in refs.values() if ref])


def test_projection_ids_and_scopes_are_consistent_with_full_pack() -> None:
    pack = _phase37_full_pack()
    projection, _ = build_final_evidence_projection(pack, max_chars=10_000)

    assert evidence_projection_violations(projection, pack) == []


def test_projection_can_omit_optional_full_pack_ids() -> None:
    pack = _phase37_full_pack()
    full_projection, full_stats = build_final_evidence_projection(
        pack,
        max_chars=20_000,
    )
    projection, stats = build_final_evidence_projection(
        pack,
        max_chars=full_stats["projected_evidence_chars"] - 1,
    )

    visible = {
        item["id"] for item in projection["knowledge_evidence"]
    }
    assert "knowledge:phase37::transcode" not in visible
    assert "knowledge:phase37::transcode" in stats["omitted_evidence_ids"]
    assert len(full_projection["knowledge_evidence"]) > len(
        projection["knowledge_evidence"]
    )


def test_claim_cannot_reference_evidence_omitted_from_projection() -> None:
    pack = _phase37_full_pack()
    _, full_stats = build_final_evidence_projection(pack, max_chars=20_000)
    projection, _ = build_final_evidence_projection(
        pack,
        max_chars=full_stats["projected_evidence_chars"] - 1,
    )
    selection = _selection(
        pack,
        claim_type="knowledge",
        statement="A transcode note exists.",
        support=["knowledge:phase37::transcode"],
    )

    violations = evidence_projection_claim_violations(selection, projection)

    assert any("omitted from projection" in item for item in violations)


def test_projection_excludes_full_lineage_raw_rows_sql_and_dry_plan() -> None:
    projection, stats = build_final_evidence_projection(
        _phase37_full_pack(),
        max_chars=10_000,
    )
    serialized = json.dumps(projection, ensure_ascii=False)

    assert "diagnostic_lineage" not in serialized
    assert "not-model-facing" not in serialized
    assert "deterministic-plan" not in serialized
    assert "reviewed-metadata" not in serialized
    assert "source_evidence_ids" not in serialized
    assert all(
        item["provenance"] == "v1"
        for item in projection["evidence_bundles"]
    )
    assert "reviewed_evidence.full_lineage" in stats[
        "projection_omitted_fields"
    ]


def test_projection_bounded_samples_follow_budget() -> None:
    pack = build_final_evidence_pack([_raw_alarm_tool_result()], [])
    generous, stats = build_final_evidence_projection(pack, max_chars=10_000)
    tight, tight_stats = build_final_evidence_projection(
        pack,
        max_chars=(
            stats["projected_evidence_chars"]
            - max(1, stats["sample_chars"] // 2)
        ),
    )

    assert generous["data_evidence"][0]["samples"]
    assert "samples" not in tight["data_evidence"][0]
    assert tight["data_evidence"][0]["facts"]["sample_count"] == 3
    assert tight_stats["sample_chars"] == 0


def test_projection_priority_is_deterministic() -> None:
    pack = _phase37_full_pack()
    # Scope-aware bundles are P0 context, so this exercises deterministic
    # priority at the smallest round-number budget that preserves all P0 data.
    first, first_stats = build_final_evidence_projection(pack, max_chars=10_000)
    second, second_stats = build_final_evidence_projection(pack, max_chars=10_000)

    assert first == second
    assert first_stats == second_stats
    assert {
        item["id"]
        for item in first["data_evidence"]
        if item["type"] in {"metric_comparison", "group_comparison"}
    } == {
        "data:comparison:1:overall",
        "data:comparison:1:group:1",
        "data:comparison:1:group:2",
        "data:comparison:1:group:3",
    }


def test_projection_fails_closed_when_p0_cannot_fit() -> None:
    with pytest.raises(EvidenceProjectionError, match="required P0 evidence"):
        build_final_evidence_projection(_phase37_full_pack(), max_chars=100)


def test_full_pack_validator_remains_authoritative() -> None:
    pack = _phase37_full_pack()
    projection, _ = build_final_evidence_projection(pack, max_chars=10_000)
    selection = _selection(
        pack,
        claim_type="observation",
        predicate="stable_control",
        polarity="negative",
        subject_evidence_ids=["data:status:1"],
        statement="Debug text is not authoritative.",
        support=["data:status:1"],
    )
    projected_knowledge = {
        item["id"] for item in projection["knowledge_evidence"]
    }
    selection["knowledge_evidence_ids"] = [
        item
        for item in selection["knowledge_evidence_ids"]
        if item in projected_knowledge
    ]

    assert evidence_projection_claim_violations(selection, projection) == []
    assert any(
        "not group comparison evidence" in item
        for item in final_claim_violations(selection, pack)
    )


def test_large_pack_final_answer_uses_projection_and_full_validator(
    monkeypatch: Any,
) -> None:
    pack = _phase37_full_pack()
    state = create_initial_state("Explain the synthetic Phase 3.7 evidence.")
    state["knowledge_evidence"] = _phase37_knowledge()
    response = TaskItem("answer", "Return grounded claims.", "response")
    state["task_plan"] = [response]
    state["pending_tasks"] = [response]
    state["current_task"] = response
    selection = {
        "data_evidence_ids": [
            item["evidence_id"] for item in pack["data_evidence"]
        ],
        "knowledge_evidence_ids": ["knowledge:phase37::e302"],
        "inferences": [],
        "limitation_ids": [
            item["limitation_id"] for item in pack["limitations"]
        ],
        "source_task_ids": [],
    }
    model = RecordingModel([json.dumps(selection)])
    monkeypatch.setattr(
        "datapilot.agent.analyst.build_final_evidence_pack",
        lambda *args, **kwargs: deepcopy(pack),
    )

    result = Analyst(model_client=model).generate_final_answer(
        state,
        response,
        trace=_trace(state),
    )

    stats = result.evidence_projection_stats
    assert stats["full_evidence_pack_chars"] > 20_000
    assert stats["projected_evidence_chars"] <= stats[
        "available_evidence_budget"
    ]
    assert stats["final_prompt_chars"] <= stats["budget_limit"]
    assert stats["budget_remaining"] >= stats["safety_margin_chars"]
    assert result.validator_result["valid"] is True
    assert result.validator_result["validated_against"] == "full_evidence_pack"
    assert "DATA EVIDENCE" in result.answer
    assert "Final Evidence Projection" in model.calls[0]["user_prompt"]
    assert "diagnostic_lineage" not in model.calls[0]["user_prompt"]


def test_oversized_pack_completes_both_planner_shapes_and_session_memory(
    monkeypatch: Any,
) -> None:
    import datapilot.agent.analyst as analyst_module  # noqa: PLC0415
    from evals.media.run_tool_offline_cases import (  # noqa: PLC0415
        _run_composite,
        _runtime,
    )

    original_builder = analyst_module.build_final_evidence_pack

    def inflated_builder(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pack = original_builder(*args, **kwargs)
        pack["reviewed_evidence"][0]["diagnostic_lineage"] = {
            "dry_plan": "retained-canonical-plan:" + ("P" * 8_000),
        }
        return pack

    monkeypatch.setattr(
        analyst_module,
        "build_final_evidence_pack",
        inflated_builder,
    )
    wren, router = _runtime()

    for planner_shape in ("single_task", "split_tasks"):
        result = _run_composite(
            wren,
            router,
            planner_shape=planner_shape,
        )
        stats = result["evidence_projection_stats"]

        assert result["success"] is True
        assert result["memory_updated"] is True
        assert result["validator_result"]["valid"] is True
        assert result["validator_result"]["violations"] == []
        assert result["final_claims"]
        assert stats["full_evidence_pack_chars"] > 20_000
        assert stats["projected_evidence_chars"] <= stats[
            "available_evidence_budget"
        ]
        assert stats["final_prompt_chars"] <= stats["budget_limit"]
        assert all(
            heading in result["final_answer"]
            for heading in (
                "DATA EVIDENCE",
                "KNOWLEDGE EVIDENCE",
                "INFERENCE",
                "LIMITATION",
            )
        )
