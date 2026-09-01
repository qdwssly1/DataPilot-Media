from __future__ import annotations

from uuid import UUID

import pytest

from datapilot.agent.state import TaskItem, create_initial_state


def test_create_initial_state() -> None:
    state = create_initial_state("比较 Q2 和 Q3 销售额")

    assert state["original_query"] == "比较 Q2 和 Q3 销售额"
    assert [(message.role, message.content) for message in state["messages"]] == [
        ("user", "比较 Q2 和 Q3 销售额")
    ]
    assert state["current_task"] is None
    assert state["task_plan"] == []
    assert state["completed_tasks"] == []
    assert state["pending_tasks"] == []
    assert state["relevant_tables"] == []
    assert state["business_context"].rules == []
    assert state["business_context"].definitions == {}
    assert state["generated_sql"] == []
    assert state["sql_results"] == []
    assert state["retry_count"] == 0
    assert state["review_result"] is None
    assert state["review_results"] == []
    assert state["analysis_results"] == []
    assert state["final_answer"] is None
    assert state["final_answer_result"] is None
    assert state["session_context"].turn_number == 1


def test_state_mutable_fields_are_isolated() -> None:
    first = create_initial_state("first query")
    second = create_initial_state("second query")

    first["pending_tasks"].append(TaskItem("task-1", "Inspect sales"))
    first["generated_sql"].append("SELECT 1")
    first["business_context"].rules.append("Use net revenue")
    first["business_context"].definitions["GMV"] = "Gross merchandise value"

    assert second["pending_tasks"] == []
    assert second["generated_sql"] == []
    assert second["business_context"].rules == []
    assert second["business_context"].definitions == {}


def test_trace_id_created() -> None:
    first = create_initial_state("first query")
    second = create_initial_state("second query")

    assert str(UUID(first["trace_id"])) == first["trace_id"]
    assert first["trace_id"] != second["trace_id"]


def test_create_initial_state_rejects_empty_query() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        create_initial_state("   ")
