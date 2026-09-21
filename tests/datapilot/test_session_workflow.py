from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from datapilot.agent.analyst import Analyst
from datapilot.agent.follow_up import FollowUpResolver, SessionContextExtractor
from datapilot.agent.graph import WorkflowRun, commit_session_context, run_session_turn
from datapilot.agent.planner import Planner, PlannerIntent, PlannerResult
from datapilot.agent.reviewer import Reviewer
from datapilot.agent.sql_agent import SQLAgent
from datapilot.agent.state import (
    AnalysisResult,
    FinalAnswerResult,
    TaskItem,
    create_initial_state,
)
from datapilot.cli import SESSION_CLEARED, clear_session, process_input, run_cli
from datapilot.memory.session_memory import SessionMemoryStore
from datapilot.tools.wren_tools import WrenQueryResult
from datapilot.tracing.trace import EventType, TraceCollector


class SequenceModel:
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


class ApproveModel:
    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        del system_prompt, user_prompt, response_schema
        return json.dumps(
            {
                "decision": "approve",
                "reason_summary": "The synthetic result supports the task.",
                "issues": [],
                "retry_instruction": None,
                "confidence": 1.0,
            }
        )


class SyntheticWren:
    def fetch_context(self, question: str, *, limit: int = 5) -> dict[str, Any]:
        del question, limit
        return {
            "strategy": "full",
            "schema": "orders(quarter, region, category, gmv)",
        }

    def recall_queries(
        self,
        question: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        del question, limit
        return []

    def dry_plan(self, sql: str) -> str:
        return sql

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        del limit
        if "customer_count" in sql:
            return WrenQueryResult(
                columns=["customer_count"],
                rows=[{"customer_count": 12}],
                row_count=1,
            )
        south = "华南" in sql
        q2 = "Q2" in sql
        if south and q2:
            rows = [{"category": "A", "gmv": 150}, {"category": "B", "gmv": 100}]
        elif south:
            rows = [{"category": "A", "gmv": 120}, {"category": "B", "gmv": 130}]
        elif q2:
            rows = [{"category": "A", "gmv": 250}, {"category": "B", "gmv": 300}]
        else:
            rows = [{"category": "A", "gmv": 200}, {"category": "B", "gmv": 370}]
        return WrenQueryResult(
            columns=["category", "gmv"],
            rows=rows,
            row_count=2,
        )

    def store_query(
        self,
        nl: str,
        sql: str,
        *,
        tags: list[str] | None = None,
    ) -> None:
        del nl, sql, tags


def _multi_plan() -> str:
    return json.dumps(
        {
            "intent": "multi_step_analysis",
            "reason_summary": "Compare two periods.",
            "tasks": [
                {
                    "task_id": "q2",
                    "description": "Retrieve Q2 category GMV.",
                    "task_type": "query",
                    "depends_on": [],
                    "status": "pending",
                },
                {
                    "task_id": "q3",
                    "description": "Retrieve Q3 category GMV.",
                    "task_type": "query",
                    "depends_on": [],
                    "status": "pending",
                },
                {
                    "task_id": "compare",
                    "description": "Compare periods and find the largest decline.",
                    "task_type": "analysis",
                    "depends_on": ["q2", "q3"],
                    "status": "pending",
                },
                {
                    "task_id": "response",
                    "description": "Answer the user.",
                    "task_type": "response",
                    "depends_on": ["compare"],
                    "status": "pending",
                },
            ],
            "requires_database": True,
            "requires_context": False,
            "is_follow_up": False,
        }
    )


def _follow_plan() -> str:
    return json.dumps(
        {
            "intent": "follow_up",
            "reason_summary": "The request depends on prior context.",
            "tasks": [
                {
                    "task_id": "follow",
                    "description": "Resolve the follow-up.",
                    "task_type": "query",
                    "depends_on": [],
                    "status": "pending",
                }
            ],
            "requires_database": True,
            "requires_context": True,
            "is_follow_up": True,
        }
    )


def _single_customer_plan() -> str:
    return json.dumps(
        {
            "intent": "single_query",
            "reason_summary": "Count customers.",
            "tasks": [
                {
                    "task_id": "customers",
                    "description": "Count all customers.",
                    "task_type": "query",
                    "depends_on": [],
                    "status": "pending",
                },
                {
                    "task_id": "response",
                    "description": "Answer the user.",
                    "task_type": "response",
                    "depends_on": ["customers"],
                    "status": "pending",
                },
            ],
            "requires_database": True,
            "requires_context": False,
            "is_follow_up": False,
        }
    )


def _context_payload(metric: str = "GMV") -> str:
    customer = metric == "customer_count"
    return json.dumps(
        {
            "metrics": [metric],
            "dimensions": [] if customer else ["商品类别"],
            "time_range": {
                "labels": [] if customer else ["Q2", "Q3"],
                "start": None,
                "end": None,
            },
            "filters": {},
            "entities": {},
            "analysis_goal": "统计客户数量" if customer else "比较并找出下降最大的类别",
        },
        ensure_ascii=False,
    )


def _resolution_payload() -> str:
    return json.dumps(
        {
            "resolved_query": "比较 Q2 和 Q3 华南地区各商品类别 GMV，并找出下降最大的类别。",
            "inherited_fields": [
                "metrics",
                "dimensions",
                "time_range",
                "analysis_goal",
            ],
            "overridden_fields": ["filters"],
            "missing_fields": [],
            "can_resolve": True,
            "reason_summary": "Added the South China region filter.",
            "metrics": ["GMV"],
            "dimensions": ["商品类别"],
            "time_range": {"labels": ["Q2", "Q3"], "start": None, "end": None},
            "filters": {"region": ["华南"]},
            "entities": {},
            "analysis_goal": "比较并找出下降最大的类别",
        },
        ensure_ascii=False,
    )


def _answer(text: str = "A 类下降最大。", source: str = "compare") -> str:
    evidence_ids = (
        ["data:result:1"]
        if source in {"query", "customers"}
        else [
            "data:analysis-comparison:1:group:1",
            "data:analysis-comparison:1:group:2",
        ]
    )
    limitation_ids = ["limitation:bounded_evidence"]
    if source not in {"query", "customers"}:
        limitation_ids.append("limitation:limited_time_windows")
    return json.dumps(
        {
            "data_evidence_ids": evidence_ids,
            "knowledge_evidence_ids": [],
            "inferences": [
                {
                    "bundle_id": (
                        "bundle:scope:1"
                        if source in {"query", "customers"}
                        else "bundle:multi_group:ALL:category"
                    ),
                    "claim_type": "observation",
                    "predicate": "general",
                    "polarity": "neutral",
                    "subject_evidence_ids": [],
                    "supporting_evidence_ids": evidence_ids,
                }
            ],
            "limitation_ids": limitation_ids,
            "source_task_ids": [source],
        },
        ensure_ascii=False,
    )


def _sql_responses(*, south: bool = False) -> list[str]:
    region = " AND region = '华南'" if south else ""
    return [
        json.dumps(
            {
                "sql": (
                    "SELECT category, SUM(gmv) AS gmv FROM orders "
                    f"WHERE quarter = 'Q2'{region} GROUP BY category"
                ),
                "summary": "Retrieve Q2 category GMV.",
            }
        ),
        json.dumps(
            {
                "sql": (
                    "SELECT category, SUM(gmv) AS gmv FROM orders "
                    f"WHERE quarter = 'Q3'{region} GROUP BY category"
                ),
                "summary": "Retrieve Q3 category GMV.",
            }
        ),
    ]


def _run_two_turns() -> tuple[Any, Any, SessionMemoryStore, list[TraceCollector]]:
    store = SessionMemoryStore()
    blank = store.create("session-a")
    planner = Planner(
        SequenceModel([_multi_plan(), _follow_plan(), _multi_plan()])
    )
    sql_agent = SQLAgent(
        model_client=SequenceModel(
            [*_sql_responses(), *_sql_responses(south=True)]
        ),
        wren_tools=SyntheticWren(),
        max_attempts=1,
    )
    reviewer = Reviewer(model_client=ApproveModel())
    analyst = Analyst(
        model_client=SequenceModel([_answer(), _answer("华南 A 类下降最大。")])
    )
    resolver = FollowUpResolver(SequenceModel([_resolution_payload()]))
    extractor = SessionContextExtractor(SequenceModel([_context_payload()]))

    first = create_initial_state(
        "比较 Q2 和 Q3 各商品类别 GMV，并找出下降最大的类别。",
        previous_session=blank,
    )
    first_trace = TraceCollector(trace_id=first["trace_id"])
    first_run = run_session_turn(
        first,
        planner,
        sql_agent,
        reviewer,
        analyst,
        resolver,
        extractor,
        store,
        trace=first_trace,
    )
    second = create_initial_state(
        "那华南地区呢？",
        previous_session=store.get("session-a"),
    )
    second_trace = TraceCollector(trace_id=second["trace_id"])
    second_run = run_session_turn(
        second,
        planner,
        sql_agent,
        reviewer,
        analyst,
        resolver,
        extractor,
        store,
        trace=second_trace,
    )
    return first_run, second_run, store, [first_trace, second_trace]


def test_successful_turn_updates_session_context() -> None:
    first_run, _, store, _ = _run_two_turns()
    context = store.get("session-a")

    assert first_run.success is True
    assert context.metrics == ["GMV"]
    assert context.dimensions == ["商品类别"]
    assert context.time_range.labels == ["Q2", "Q3"]


def test_failed_turn_does_not_poison_session_context() -> None:
    _, _, store, _ = _run_two_turns()
    before = store.get("session-a")
    state = create_initial_state(
        "改看不存在的字段。",
        previous_session=before,
    )
    trace = TraceCollector(trace_id=state["trace_id"])
    run = run_session_turn(
        state,
        Planner(SequenceModel([_multi_plan()])),
        SQLAgent(
            model_client=SequenceModel(
                [json.dumps({"sql": "DELETE FROM orders", "summary": "Invalid."})]
            ),
            wren_tools=SyntheticWren(),
            max_attempts=1,
        ),
        Reviewer(model_client=ApproveModel()),
        Analyst(model_client=SequenceModel([])),
        FollowUpResolver(SequenceModel([])),
        SessionContextExtractor(SequenceModel([])),
        store,
        trace=trace,
    )

    assert run.memory_updated is False
    assert store.get("session-a") == before


def test_failed_analysis_does_not_commit_session_context() -> None:
    store = SessionMemoryStore()
    previous = store.create("session-a")
    previous.metrics = ["verified_metric"]
    store.save(previous)
    state = create_initial_state("Compare metrics", previous_session=previous)
    query = TaskItem("query", "Retrieve data", "query", status="completed")
    analysis = TaskItem(
        "analysis",
        "Compare data",
        "analysis",
        ["query"],
        status="completed",
    )
    response = TaskItem(
        "response",
        "Answer",
        "response",
        ["analysis"],
        status="completed",
    )
    failed_analysis = AnalysisResult(
        task_id="analysis",
        summary="Analysis failed.",
        success=False,
        error="ambiguous metric columns / source result contract mismatch",
        source_task_ids=["query"],
    )
    final = FinalAnswerResult(
        task_id="response",
        answer="This answer must not be authoritative.",
        source_task_ids=["analysis"],
    )
    state["task_plan"] = [query, analysis, response]
    state["pending_tasks"] = []
    state["completed_tasks"] = [query, analysis, response]
    state["analysis_results"] = [failed_analysis]
    state["final_answer"] = final.answer
    state["final_answer_result"] = final
    workflow = WorkflowRun((), (failed_analysis,), final)
    planner_result = PlannerResult(
        intent=PlannerIntent.MULTI_STEP_ANALYSIS,
        reason_summary="Compare metrics.",
        tasks=(query, analysis, response),
        requires_database=True,
        requires_context=False,
        is_follow_up=False,
    )
    extractor_model = SequenceModel([])

    updated = commit_session_context(
        state,
        planner_result,
        workflow,
        store,
        SessionContextExtractor(extractor_model),
        trace=TraceCollector(trace_id=state["trace_id"]),
    )

    assert updated is False
    assert store.get("session-a") == previous
    assert extractor_model.calls == []


def test_complete_multi_turn_follow_up_workflow() -> None:
    first_run, second_run, store, _ = _run_two_turns()
    context = store.get("session-a")

    assert first_run.success is True
    assert second_run.success is True
    assert second_run.planning.resolution.can_resolve is True
    assert "华南" in second_run.planning.resolution.resolved_query
    assert context.filters == {"region": ["华南"]}
    assert context.turn_index == 2
    assert context.last_user_query == "那华南地区呢？"


def test_new_topic_replaces_old_session_context() -> None:
    _, _, store, _ = _run_two_turns()
    previous = store.get("session-a")
    state = create_initial_state("统计所有客户数量。", previous_session=previous)
    trace = TraceCollector(trace_id=state["trace_id"])
    run = run_session_turn(
        state,
        Planner(SequenceModel([_single_customer_plan()])),
        SQLAgent(
            model_client=SequenceModel(
                [
                    json.dumps(
                        {
                            "sql": "SELECT COUNT(*) AS customer_count FROM customers",
                            "summary": "Count customers.",
                        }
                    )
                ]
            ),
            wren_tools=SyntheticWren(),
            max_attempts=1,
        ),
        Reviewer(model_client=ApproveModel()),
        Analyst(
            model_client=SequenceModel(
                [_answer("共有 12 位客户。", source="customers")]
            )
        ),
        FollowUpResolver(SequenceModel([])),
        SessionContextExtractor(
            SequenceModel([_context_payload("customer_count")])
        ),
        store,
        trace=trace,
    )

    context = store.get("session-a")
    assert run.success is True
    assert context.metrics == ["customer_count"]
    assert context.time_range.labels == []
    assert context.filters == {}


def test_session_trace_success() -> None:
    _, _, _, traces = _run_two_turns()

    first_events = [event.event_type for event in traces[0].get_events()]
    second_events = [event.event_type for event in traces[1].get_events()]
    assert EventType.SESSION_CONTEXT_UPDATED in first_events
    assert EventType.SESSION_TURN_COMPLETED in first_events
    assert EventType.FOLLOW_UP_DETECTED in second_events
    assert EventType.SESSION_CONTEXT_LOADED in second_events
    assert EventType.FOLLOW_UP_RESOLVED in second_events


def test_cli_reuses_session() -> None:
    store = SessionMemoryStore()
    inputs = iter(
        [
            "比较 Q2 和 Q3 各商品类别 GMV，并找出下降最大的类别。",
            "那华南地区呢？",
            "exit",
        ]
    )
    outputs: list[str] = []

    exit_code = run_cli(
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        planner=Planner(
            SequenceModel([_multi_plan(), _follow_plan(), _multi_plan()])
        ),
        sql_agent=SQLAgent(
            model_client=SequenceModel(
                [*_sql_responses(), *_sql_responses(south=True)]
            ),
            wren_tools=SyntheticWren(),
            max_attempts=1,
        ),
        reviewer=Reviewer(model_client=ApproveModel()),
        analyst=Analyst(
            model_client=SequenceModel([_answer(), _answer("华南 A 类下降最大。")])
        ),
        follow_up_resolver=FollowUpResolver(
            SequenceModel([_resolution_payload()])
        ),
        context_extractor=SessionContextExtractor(
            SequenceModel([_context_payload()])
        ),
        session_store=store,
        session_id="session-cli",
        environ={},
    )

    assert exit_code == 0
    assert any("Follow-up detected" in output for output in outputs)
    assert any("[Resolved Query]" in output for output in outputs)
    assert store.get("session-cli").filters == {"region": ["华南"]}


def test_cli_reset_clears_session() -> None:
    store = SessionMemoryStore()
    context = store.create("session-cli")
    context.metrics = ["GMV"]
    store.save(context)
    inputs = iter(["reset", "exit"])
    outputs: list[str] = []

    exit_code = run_cli(
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        session_store=store,
        session_id="session-cli",
    )

    assert exit_code == 0
    assert outputs == [SESSION_CLEARED, "Goodbye."]
    assert store.get("session-cli").has_business_context is False


def test_session_created_and_cleared_trace() -> None:
    store = SessionMemoryStore()
    blank = store.create("session-trace")
    initialized = process_input(
        "first turn",
        session_id="session-trace",
        previous_session=blank,
    )

    clear_session(store, "session-trace", trace=initialized.trace)

    event_types = [event.event_type for event in initialized.trace.get_events()]
    assert EventType.SESSION_CREATED in event_types
    assert EventType.SESSION_CONTEXT_CLEARED in event_types
