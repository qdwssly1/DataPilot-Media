from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from datapilot.agent.graph import execute_ready_query_tasks
from datapilot.agent.sql_agent import (
    SQLAgent,
    SQLAgentError,
    validate_read_only_sql,
)
from datapilot.agent.state import SQLResult, TaskItem, create_initial_state
from datapilot.tools.wren_tools import WrenQueryResult
from datapilot.tracing.trace import EventType, TraceCollector


class FakeSQLModel:
    def __init__(self, responses: list[str | Exception]) -> None:
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
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeWrenTools:
    def __init__(
        self,
        *,
        context: dict[str, Any] | None = None,
        recalled: list[dict[str, Any]] | None = None,
        dry_plan_outcomes: list[str | Exception] | None = None,
        query_outcomes: list[WrenQueryResult | Exception] | None = None,
    ) -> None:
        self.context = context or {
            "strategy": "search",
            "results": [
                {
                    "item_type": "model",
                    "name": "orders",
                    "summary": "Order facts with GMV.",
                }
            ],
        }
        self.recalled = recalled or []
        self.dry_plan_outcomes = list(dry_plan_outcomes or [])
        self.query_outcomes = list(query_outcomes or [])
        self.calls: list[tuple[str, Any]] = []
        self.store_calls: list[tuple[str, str]] = []

    def fetch_context(self, question: str, *, limit: int = 5) -> dict[str, Any]:
        self.calls.append(("fetch_context", (question, limit)))
        return self.context

    def recall_queries(
        self,
        question: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        self.calls.append(("recall_queries", (question, limit)))
        return self.recalled

    def dry_plan(self, sql: str) -> str:
        self.calls.append(("dry_plan", sql))
        outcome: str | Exception = (
            self.dry_plan_outcomes.pop(0)
            if self.dry_plan_outcomes
            else f"planned: {sql}"
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        self.calls.append(("query", (sql, limit)))
        outcome: WrenQueryResult | Exception = (
            self.query_outcomes.pop(0)
            if self.query_outcomes
            else WrenQueryResult(
                columns=["gmv"],
                rows=[{"gmv": 125}],
                row_count=1,
            )
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def store_query(self, nl: str, sql: str) -> None:
        self.store_calls.append((nl, sql))


def _response(sql: str, summary: str = "Retrieve the requested metric.") -> str:
    return json.dumps({"sql": sql, "summary": summary})


def _state_for(*tasks: TaskItem) -> tuple[Any, TraceCollector]:
    state = create_initial_state("Show July GMV")
    state["task_plan"] = list(tasks)
    state["pending_tasks"] = list(tasks)
    state["current_task"] = tasks[0] if tasks else None
    trace = TraceCollector(trace_id=state["trace_id"])
    return state, trace


def _query_task(
    task_id: str = "task_1",
    *,
    depends_on: list[str] | None = None,
) -> TaskItem:
    return TaskItem(
        task_id=task_id,
        description=f"Retrieve metric for {task_id}.",
        task_type="query",
        depends_on=depends_on or [],
    )


def test_sql_agent_fetches_context() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    tools = FakeWrenTools()

    result = SQLAgent(
        model_client=FakeSQLModel([_response("SELECT SUM(gmv) FROM orders")]),
        wren_tools=tools,
    ).execute_task(state, task, trace=trace)

    assert result.success is True
    assert tools.calls[0][0] == "fetch_context"
    assert "orders" in state["business_context"].definitions["wren_context:task_1"]


def test_sql_agent_recalls_sql_memory() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    recalled = [
        {
            "nl_query": "monthly GMV",
            "sql_query": "SELECT SUM(gmv) FROM orders",
        }
    ]
    tools = FakeWrenTools(recalled=recalled)
    model = FakeSQLModel([_response("SELECT SUM(gmv) FROM orders")])

    SQLAgent(model_client=model, wren_tools=tools).execute_task(
        state,
        task,
        trace=trace,
    )

    assert [name for name, _ in tools.calls[:2]] == [
        "fetch_context",
        "recall_queries",
    ]
    assert "monthly GMV" in model.calls[0]["user_prompt"]


def test_sql_agent_generates_sql() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    model = FakeSQLModel([_response("SELECT SUM(gmv) AS gmv FROM orders")])

    result = SQLAgent(model_client=model, wren_tools=FakeWrenTools()).execute_task(
        state,
        task,
        trace=trace,
    )

    assert result.sql == "SELECT SUM(gmv) AS gmv FROM orders"
    assert model.calls[0]["response_schema"]["additionalProperties"] is False
    assert "Do not answer the user" in model.calls[0]["system_prompt"]


def test_sql_agent_rejects_write_sql() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    tools = FakeWrenTools()

    result = SQLAgent(
        model_client=FakeSQLModel([_response("UPDATE orders SET gmv = 0")]),
        wren_tools=tools,
        max_attempts=1,
    ).execute_task(state, task, trace=trace)

    assert result.success is False
    assert "UPDATE" in (result.error or "")
    assert not any(name in {"dry_plan", "query"} for name, _ in tools.calls)
    assert EventType.SQL_SAFETY_REJECTED in {
        event.event_type for event in trace.get_events()
    }


def test_sql_agent_allows_cte_select() -> None:
    sql = "WITH totals AS (SELECT SUM(gmv) AS gmv FROM orders) SELECT * FROM totals"
    task = _query_task()
    state, trace = _state_for(task)

    result = SQLAgent(
        model_client=FakeSQLModel([_response(sql)]),
        wren_tools=FakeWrenTools(),
    ).execute_task(state, task, trace=trace)

    assert result.success is True
    validate_read_only_sql("-- comment\n" + sql + ";")


def test_sql_agent_calls_dry_plan_before_query() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    tools = FakeWrenTools()

    SQLAgent(
        model_client=FakeSQLModel([_response("SELECT * FROM orders")]),
        wren_tools=tools,
    ).execute_task(state, task, trace=trace)

    names = [name for name, _ in tools.calls]
    assert names.index("dry_plan") < names.index("query")


def test_sql_agent_does_not_query_when_dry_plan_fails() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    tools = FakeWrenTools(dry_plan_outcomes=[ValueError("unknown model")])

    result = SQLAgent(
        model_client=FakeSQLModel([_response("SELECT * FROM missing")]),
        wren_tools=tools,
        max_attempts=1,
    ).execute_task(state, task, trace=trace)

    assert result.success is False
    assert not any(name == "query" for name, _ in tools.calls)


def test_sql_agent_retries_after_dry_plan_error() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    tools = FakeWrenTools(
        dry_plan_outcomes=[ValueError("unknown column"), "planned SQL"]
    )
    model = FakeSQLModel(
        [
            _response("SELECT bad_column FROM orders"),
            _response("SELECT gmv FROM orders"),
        ]
    )

    result = SQLAgent(model_client=model, wren_tools=tools).execute_task(
        state,
        task,
        trace=trace,
    )

    assert result.success is True
    assert result.retry_count == 1
    assert len(model.calls) == 2
    assert "unknown column" in model.calls[1]["user_prompt"]


def test_sql_agent_retries_after_query_error() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    tools = FakeWrenTools(
        query_outcomes=[
            RuntimeError("database timeout"),
            WrenQueryResult(columns=["gmv"], rows=[{"gmv": 5}], row_count=1),
        ]
    )
    model = FakeSQLModel(
        [_response("SELECT gmv FROM orders"), _response("SELECT gmv FROM orders")]
    )

    result = SQLAgent(model_client=model, wren_tools=tools).execute_task(
        state,
        task,
        trace=trace,
    )

    assert result.success is True
    assert result.retry_count == 1
    assert [name for name, _ in tools.calls].count("query") == 2


def test_sql_agent_stops_after_retry_limit() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    tools = FakeWrenTools(
        dry_plan_outcomes=[ValueError("bad one"), ValueError("bad two")]
    )
    model = FakeSQLModel(
        [_response("SELECT x FROM orders"), _response("SELECT y FROM orders")]
    )

    result = SQLAgent(model_client=model, wren_tools=tools).execute_task(
        state,
        task,
        trace=trace,
    )

    assert result.success is False
    assert result.retry_count == 1
    assert len(model.calls) == 2
    assert task.status == "pending"


def test_sql_agent_retries_invalid_structured_output() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    model = FakeSQLModel(
        ["not-json", _response("SELECT gmv FROM orders")]
    )

    result = SQLAgent(
        model_client=model,
        wren_tools=FakeWrenTools(),
    ).execute_task(state, task, trace=trace)

    assert result.success is True
    assert result.retry_count == 1
    assert len(model.calls) == 2
    assert EventType.SQL_RETRY in {
        event.event_type for event in trace.get_events()
    }


def test_sql_agent_invalid_structured_output_fails_after_retry() -> None:
    task = _query_task()
    state, trace = _state_for(task)

    result = SQLAgent(
        model_client=FakeSQLModel(["not-json", '{"sql": "SELECT 1"}']),
        wren_tools=FakeWrenTools(),
    ).execute_task(state, task, trace=trace)

    assert result.success is False
    assert result.retry_count == 1
    assert state["generated_sql"] == []
    assert state["completed_tasks"] == []
    assert trace.get_events()[-1].metadata["error_type"] == "SQLGenerationError"


def test_sql_agent_updates_state_on_success() -> None:
    task = _query_task()
    state, trace = _state_for(task)

    result = SQLAgent(
        model_client=FakeSQLModel([_response("SELECT gmv FROM orders")]),
        wren_tools=FakeWrenTools(),
    ).execute_task(state, task, trace=trace)

    assert state["generated_sql"] == [result.sql]
    assert state["sql_results"] == [result]
    assert state["completed_tasks"] == [task]
    assert state["pending_tasks"] == []
    assert state["current_task"] is None
    assert task.status == "completed"


def test_sql_agent_preserves_state_on_failure() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    completed = TaskItem("done", "Completed", status="completed")
    existing = SQLResult("done", "SELECT 1")
    state["completed_tasks"] = [completed]
    state["generated_sql"] = ["SELECT 1"]
    state["sql_results"] = [existing]

    result = SQLAgent(
        model_client=FakeSQLModel([_response("DELETE FROM orders")]),
        wren_tools=FakeWrenTools(),
        max_attempts=1,
    ).execute_task(state, task, trace=trace)

    assert result.success is False
    assert state["generated_sql"] == ["SELECT 1"]
    assert state["completed_tasks"] == [completed]
    assert state["sql_results"] == [existing, result]
    assert state["pending_tasks"] == [task]
    assert state["current_task"] is task
    assert task.status == "pending"


def test_sql_agent_respects_task_dependencies() -> None:
    task = _query_task("task_2", depends_on=["task_1"])
    state, trace = _state_for(task)
    tools = FakeWrenTools()

    with pytest.raises(SQLAgentError, match="not a ready query task"):
        SQLAgent(
            model_client=FakeSQLModel([_response("SELECT 1")]),
            wren_tools=tools,
        ).execute_task(state, task, trace=trace)

    assert tools.calls == []
    assert state["sql_results"] == []


def test_sql_agent_handles_multiple_query_tasks() -> None:
    first = _query_task("task_1")
    second = _query_task("task_2")
    analysis = TaskItem(
        "task_3",
        "Compare results.",
        task_type="analysis",
        depends_on=["task_1", "task_2"],
    )
    state, trace = _state_for(first, second, analysis)
    model = FakeSQLModel(
        [_response("SELECT 1 AS value"), _response("SELECT 2 AS value")]
    )
    agent = SQLAgent(model_client=model, wren_tools=FakeWrenTools())

    results = execute_ready_query_tasks(state, agent, trace=trace)

    assert len(results) == 2
    assert [task.task_id for task in state["completed_tasks"]] == [
        "task_1",
        "task_2",
    ]
    assert state["pending_tasks"] == [analysis]
    assert state["current_task"] is analysis
    assert analysis.status == "pending"


def test_sql_agent_does_not_execute_analysis_task() -> None:
    analysis = TaskItem("task_1", "Compare results.", task_type="analysis")
    state, trace = _state_for(analysis)
    model = FakeSQLModel([])
    tools = FakeWrenTools()

    results = execute_ready_query_tasks(
        state,
        SQLAgent(model_client=model, wren_tools=tools),
        trace=trace,
    )

    assert results == ()
    assert model.calls == []
    assert tools.calls == []


def test_sql_agent_trace_success() -> None:
    task = _query_task()
    state, trace = _state_for(task)

    SQLAgent(
        model_client=FakeSQLModel([_response("SELECT gmv FROM orders")]),
        wren_tools=FakeWrenTools(),
    ).execute_task(state, task, trace=trace)

    event_types = [event.event_type for event in trace.get_events()]
    assert event_types == [
        EventType.SQL_AGENT_STARTED,
        EventType.CONTEXT_FETCHED,
        EventType.SQL_MEMORY_RECALLED,
        EventType.SQL_GENERATED,
        EventType.DRY_PLAN_STARTED,
        EventType.DRY_PLAN_SUCCEEDED,
        EventType.SQL_EXECUTION_STARTED,
        EventType.SQL_EXECUTION_SUCCEEDED,
        EventType.SQL_AGENT_COMPLETED,
    ]
    allowed = {"task_id", "attempt", "row_count", "duration", "error_type", "tool"}
    assert all(set(event.metadata) <= allowed for event in trace.get_events())
    assert not any("SELECT" in str(event.metadata) for event in trace.get_events())


def test_sql_agent_trace_retry() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    tools = FakeWrenTools(
        dry_plan_outcomes=[ValueError("bad column"), "planned SQL"]
    )

    SQLAgent(
        model_client=FakeSQLModel(
            [_response("SELECT x FROM orders"), _response("SELECT gmv FROM orders")]
        ),
        wren_tools=tools,
    ).execute_task(state, task, trace=trace)

    assert [event.event_type for event in trace.get_events()].count(
        EventType.SQL_RETRY
    ) == 1


def test_sql_agent_trace_failure() -> None:
    task = _query_task()
    state, trace = _state_for(task)

    SQLAgent(
        model_client=FakeSQLModel(
            [_response("DROP TABLE orders"), _response("DELETE FROM orders")]
        ),
        wren_tools=FakeWrenTools(),
    ).execute_task(state, task, trace=trace)

    event_types = [event.event_type for event in trace.get_events()]
    assert event_types[-1] is EventType.SQL_AGENT_FAILED
    assert event_types.count(EventType.SQL_SAFETY_REJECTED) == 2
    assert event_types.count(EventType.SQL_RETRY) == 1


def test_sql_agent_never_stores_unverified_sql() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    tools = FakeWrenTools()

    SQLAgent(
        model_client=FakeSQLModel([_response("SELECT gmv FROM orders")]),
        wren_tools=tools,
    ).execute_task(state, task, trace=trace)

    assert tools.store_calls == []


def test_sql_agent_rejects_multiple_statements() -> None:
    with pytest.raises(SQLAgentError, match="multiple SQL statements"):
        validate_read_only_sql("SELECT 1; DROP TABLE orders")


def test_sql_agent_ignores_write_words_inside_literals() -> None:
    validate_read_only_sql("SELECT 'DROP TABLE orders' AS warning")
