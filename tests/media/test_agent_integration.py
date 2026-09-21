from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from datapilot.agent.analyst import Analyst, AnalystOutputError
from datapilot.agent.graph import execute_task_plan
from datapilot.agent.planner import Planner
from datapilot.agent.sql_agent import SQLAgent
from datapilot.agent.state import (
    ReviewerResult,
    SQLResult,
    TaskItem,
    create_initial_state,
)
from datapilot.retrieval import KnowledgeRetriever
from datapilot.retrieval.integration import retrieve_into_state
from datapilot.tools.wren_tools import WrenQueryResult
from datapilot.tracing.trace import TraceCollector

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


class FakeWren:
    def fetch_context(self, question: str, *, limit: int = 5) -> dict[str, Any]:
        del question, limit
        return {"schema": "stream_sessions(play_success)"}

    def recall_queries(self, question: str, *, limit: int = 3) -> list[dict[str, Any]]:
        del question, limit
        return []

    def dry_plan(self, sql: str) -> str:
        return f"plan:{sql}"

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        del sql, limit
        return WrenQueryResult(
            columns=["playback_success_rate"],
            rows=[{"playback_success_rate": 0.91}],
            row_count=1,
        )

    def store_query(
        self,
        nl: str,
        sql: str,
        *,
        tags: list[str] | None = None,
    ) -> None:
        del nl, sql, tags


def _trace(state: Any) -> TraceCollector:
    return TraceCollector(trace_id=state["trace_id"])


def _retrieved_state(query: str) -> Any:
    state = create_initial_state(query)
    retrieve_into_state(state, KnowledgeRetriever.from_directory(KNOWLEDGE))
    return state


def test_planner_receives_bounded_knowledge_without_schema_change() -> None:
    state = _retrieved_state("E302 是什么？")
    payload = {
        "intent": "simple_question",
        "reason_summary": "This is a domain knowledge question.",
        "tasks": [
            {
                "task_id": "answer",
                "description": "Explain E302 from retrieved knowledge.",
                "task_type": "response",
                "depends_on": [],
                "status": "pending",
            }
        ],
        "requires_database": False,
        "requires_context": False,
        "is_follow_up": False,
    }
    model = RecordingModel([json.dumps(payload)])

    result = Planner(model).plan(state, trace=_trace(state))

    assert result.tasks[0].task_type == "response"
    assert "Retrieved domain knowledge" in model.calls[0]["user_prompt"]
    assert "media-error-codes::01-e302" in model.calls[0]["user_prompt"]
    assert set(model.calls[0]["response_schema"]["properties"]) == {
        "intent",
        "reason_summary",
        "tasks",
        "requires_database",
        "requires_context",
        "is_follow_up",
    }


def test_sql_agent_sees_knowledge_as_advisory_not_data() -> None:
    state = _retrieved_state("查询播放成功率并说明口径")
    task = TaskItem("query", "Retrieve playback_success_rate.", "query")
    state["task_plan"] = [task]
    state["pending_tasks"] = [task]
    state["current_task"] = task
    model = RecordingModel(
        [
            json.dumps(
                {
                    "sql": (
                        "SELECT AVG(play_success) AS playback_success_rate "
                        "FROM stream_sessions"
                    ),
                    "summary": "Query rate.",
                }
            )
        ]
    )

    SQLAgent(model_client=model, wren_tools=FakeWren()).execute_task(
        state,
        task,
        trace=_trace(state),
    )

    prompt = model.calls[0]["user_prompt"]
    assert "Retrieved knowledge evidence" in prompt
    assert "advisory semantics/SOP only" in prompt
    assert "media-qoe-metrics::01-playback-success-rate" in prompt


def test_knowledge_only_response_uses_existing_response_task_contract() -> None:
    state = _retrieved_state("E302 是什么？")
    response = TaskItem("answer", "Explain E302.", "response")
    state["task_plan"] = [response]
    state["pending_tasks"] = [response]
    state["current_task"] = response
    evidence_id = (
        "knowledge:media-error-codes::01-e302-cdn-upstream-timeout"
    )
    model = RecordingModel(
        [
            json.dumps(
                {
                    "data_evidence_ids": [],
                    "knowledge_evidence_ids": [evidence_id],
                    "inferences": [],
                    "limitation_ids": [
                        "limitation:knowledge_not_observed_data"
                    ],
                    "source_task_ids": [],
                }
            )
        ]
    )

    workflow = execute_task_plan(
        state,
        object(),  # response-only plan never dispatches SQL
        object(),  # response-only plan never invokes Reviewer
        Analyst(model_client=model),
        trace=_trace(state),
    )

    assert workflow.reviewed_queries == ()
    assert workflow.final_answer_result is not None
    assert workflow.final_answer_result.source_task_ids == []
    assert "E302" in state["final_answer"]
    assert "No database evidence was queried" in state["final_answer"]
    source_schema = model.calls[0]["response_schema"]["properties"][
        "source_task_ids"
    ]
    assert source_schema["minItems"] == 0


def test_final_answer_rejects_unknown_knowledge_citation() -> None:
    state = _retrieved_state("E302 是什么？")
    response = TaskItem("answer", "Explain E302.", "response")
    state["task_plan"] = [response]
    state["pending_tasks"] = [response]
    invented = json.dumps(
        {
            "data_evidence_ids": [],
            "knowledge_evidence_ids": ["knowledge:invented::chunk"],
            "inferences": [],
            "limitation_ids": ["limitation:knowledge_not_observed_data"],
            "source_task_ids": [],
        }
    )

    with pytest.raises(AnalystOutputError, match="omitted from projection"):
        Analyst(
            model_client=RecordingModel([invented]),
            max_output_retries=0,
        ).generate_final_answer(
            state,
            response,
            trace=_trace(state),
        )


def test_mixed_answer_prompt_keeps_evidence_types_separate() -> None:
    state = _retrieved_state("华南播放成功率下降并出现 E302，怎么排查？")
    query = TaskItem(
        "query",
        "Retrieve approved evidence.",
        "query",
        status="completed",
    )
    response = TaskItem(
        "answer",
        "Answer with cautious diagnosis.",
        "response",
        ["query"],
    )
    state["task_plan"] = [query, response]
    state["completed_tasks"] = [query]
    state["pending_tasks"] = [response]
    state["sql_results"] = [
        SQLResult(
            task_id="query",
            sql="SELECT 0.91 AS playback_success_rate",
            columns=["playback_success_rate"],
            rows=[{"playback_success_rate": 0.91}],
            row_count=1,
        )
    ]
    state["review_results"] = [
        ReviewerResult("query", "approve", "Verified.", confidence=1.0)
    ]
    cited = state["knowledge_evidence"][0]["chunk_id"]
    model = RecordingModel(
        [
            json.dumps(
                {
                    "data_evidence_ids": ["data:result:1"],
                    "knowledge_evidence_ids": [f"knowledge:{cited}"],
                    "inferences": [
                        {
                            "bundle_id": "bundle:scope:1",
                            "claim_type": "correlation",
                            "predicate": "general",
                            "polarity": "neutral",
                            "subject_evidence_ids": [],
                            "supporting_evidence_ids": [
                                "data:result:1",
                                f"knowledge:{cited}",
                            ],
                        }
                    ],
                    "limitation_ids": [
                        "limitation:correlation_not_causation"
                    ],
                    "source_task_ids": ["query"],
                }
            )
        ]
    )

    Analyst(model_client=model).generate_final_answer(
        state,
        response,
        trace=_trace(state),
    )

    prompt = model.calls[0]["user_prompt"]
    variants = model.calls[0]["response_schema"]["properties"]["inferences"][
        "items"
    ]["oneOf"]
    claim_types = variants[0]["properties"]["claim_type"]["enum"]
    assert "Final Evidence Projection" in prompt
    assert "claim_type" in prompt
    assert set(claim_types) == {
        "observation",
        "knowledge",
        "correlation",
        "hypothesis",
        "recommendation",
        "causal_claim",
    }
    assert "recommendation needs" in model.calls[0]["system_prompt"]
    assert "Knowledge plus Data or Limitation" in model.calls[0]["system_prompt"]


def test_rag_unavailable_falls_back_without_mutating_data_evidence() -> None:
    class FailingRetriever:
        def retrieve(self, query: str, *, top_k: int = 5) -> list[Any]:
            del query, top_k
            raise RuntimeError("offline")

    state = create_initial_state("Show playback success rate")
    state["sql_results"] = [SQLResult(task_id="old", sql="SELECT 1")]

    results = retrieve_into_state(state, FailingRetriever())

    assert results == []
    assert state["knowledge_evidence"] == []
    assert state["knowledge_retrieval_error"] == (
        "knowledge retrieval failed: RuntimeError"
    )
    assert state["sql_results"][0].sql == "SELECT 1"
