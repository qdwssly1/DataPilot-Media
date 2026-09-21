from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

import evals.media.run_tool_agent_e2e as runner
from datapilot.agent.state import AnalysisResult, ReviewerResult, TaskItem
from datapilot.cli import process_input


class _FakeCountingModel:
    def __init__(self) -> None:
        self.calls = [
            {"component": "planner", "latency_ms": 12.5},
            {"component": "analyst", "latency_ms": 20.0},
        ]


class _StructuredFailure(RuntimeError):
    stage = "structured_output"
    summary = "response fields do not match the schema"


def _partial_report() -> dict[str, Any]:
    initialized = process_input(runner.QUERY, session_id="runner-test")
    state = initialized.state
    query = TaskItem("q1", "Retrieve synthetic QoE.", "query", status="completed")
    analysis = TaskItem("a1", "Analyze evidence.", "analysis", ["q1"])
    analysis.status = "failed"
    state["task_plan"] = [query, analysis]
    state["current_task"] = analysis
    state["tool_routes"] = [
        {
            "route": "tool",
            "tool_name": "query_qoe_metrics",
            "reason": "Deterministic capability match.",
            "arguments": {"region": "华南"},
        }
    ]
    state["tool_results"] = [
        {
            "tool_name": "query_qoe_metrics",
            "success": True,
            "data": [{"region": "华南", "must_not_persist": "raw row"}],
            "summary": "Returned one synthetic row.",
            "metadata": {
                "arguments": {"region": "华南"},
                "row_count": 1,
                "columns": ["region", "playback_success_rate"],
                "executed_query": "must not persist",
            },
            "error": None,
            "execution_time_ms": 4.5,
        }
    ]
    state["review_results"] = [
        ReviewerResult(
            task_id="q1",
            decision="approve",
            reason_summary="Synthetic evidence matches the task.",
            confidence=1.0,
        )
    ]
    state["analysis_results"] = [
        AnalysisResult(
            task_id="a1",
            summary="Analysis failed safely.",
            success=False,
            error="response fields do not match the schema",
        )
    ]
    state["final_evidence_pack"] = {
        "version": "1.0",
        "reviewed_task_ids": ["q1"],
        "data_evidence": [
            {
                "evidence_id": "data:comparison:1:group:1",
                "kind": "group_comparison",
                "source_task_ids": ["q1"],
                "facts": {
                    "group": "CDN-B",
                    "baseline_value": 0.95,
                    "current_value": 0.50,
                    "delta": -0.45,
                    "must_not_persist": "raw fact",
                },
            }
        ],
        "knowledge_evidence": [
            {
                "evidence_id": "knowledge:synthetic::chunk",
                "title": "Synthetic chunk",
                "source_category": "sop",
                "supported_statement": "must not persist chunk text",
            }
        ],
        "limitations": [
            {"limitation_id": "limitation:correlation_not_causation"}
        ],
    }
    state["final_claims"] = [
        {
            "claim_type": "correlation",
            "predicate": "general",
            "polarity": "neutral",
            "subject_evidence_ids": [],
            "supporting_evidence_ids": [
                "data:comparison:1:group:1",
                "knowledge:synthetic::chunk",
            ],
        }
    ]
    state["final_validator_result"] = {
        "valid": False,
        "violations": ["synthetic violation"],
        "retry_count": 1,
    }
    return runner._build_report(
        initialized=initialized,
        retrieval_results=[{"chunk_id": "synthetic::chunk"}],
        retrieval_calls=1,
        memory_updated=False,
        success=False,
        failure=_StructuredFailure(),
        current_stage="workflow",
        started_at=perf_counter(),
        models=(_FakeCountingModel(),),
    )


def test_partial_failure_report_preserves_observability_without_raw_rows() -> None:
    report = _partial_report()

    assert report["run_status"] == "exception"
    assert report["failure_stage"] == "analyst.structured_output"
    assert report["failure_type"] == "_StructuredFailure"
    assert report["planner_tasks"][0]["task_id"] == "q1"
    assert report["tool_routes"][0]["tool_name"] == "query_qoe_metrics"
    assert report["tool_calls"][0]["metadata"]["row_count"] == 1
    assert report["tool_calls"][0]["evidence_summary"] == [{"region": "华南"}]
    assert report["reviewer_decisions"][0]["decision"] == "approve"
    assert report["analyst_status"]["status"] == "failed"
    assert report["final_answer_status"]["status"] == "not_planned"
    assert report["session_commit_status"] == "not_committed"
    assert report["retrieval_calls"] == 1
    assert report["retrieved_chunk_ids"] == ["synthetic::chunk"]
    assert report["llm_call_count"] == 2
    assert report["component_call_counts"] == {"analyst": 1, "planner": 1}
    assert report["grouped_comparison"][0]["group"] == "CDN-B"
    assert report["final_claims"][0]["claim_type"] == "correlation"
    assert report["claim_evidence_mapping"][0][
        "supporting_evidence_ids"
    ] == [
        "data:comparison:1:group:1",
        "knowledge:synthetic::chunk",
    ]
    assert report["final_validator_result"]["valid"] is False

    serialized = json.dumps(report, ensure_ascii=False)
    assert "must_not_persist" not in serialized
    assert "executed_query" not in serialized
    assert "must not persist chunk text" not in serialized
    assert "raw fact" not in serialized


def test_runner_persists_failure_report_atomically(
    monkeypatch: Any,
) -> None:
    with TemporaryDirectory(
        prefix=".phase3-runner-test-",
        dir=runner.REPORT_PATH.parent,
    ) as temporary:
        report_path = Path(temporary) / "phase3-result.json"
        monkeypatch.setattr(runner, "REPORT_PATH", report_path)

        report = _partial_report()
        runner._write_report(report)

        assert json.loads(report_path.read_text(encoding="utf-8")) == report
        assert not report_path.with_suffix(".tmp").exists()


def test_run_case_captures_setup_exception(monkeypatch: Any) -> None:
    class _SetupFailure(RuntimeError):
        summary = "synthetic setup failed"

    def fail_setup() -> dict[str, str]:
        raise _SetupFailure()

    monkeypatch.setattr(runner, "_safe_environment", fail_setup)

    report = runner.run_case()

    assert report["run_status"] == "exception"
    assert report["failure_stage"] == "configuration"
    assert report["failure_type"] == "_SetupFailure"
    assert report["failure_summary"] == "synthetic setup failed"
    assert report["llm_call_count"] == 0
    assert report["session_commit_status"] == "not_committed"
