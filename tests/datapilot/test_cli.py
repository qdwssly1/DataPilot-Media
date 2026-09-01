from __future__ import annotations

import json
from typing import Any

from datapilot.agent.sql_agent import SQLAgent
from datapilot.agent.planner import Planner
from datapilot.agent.reviewer import Reviewer
from datapilot.cli import (
    PLANNER_CONFIGURATION_HELP,
    PLANNER_CONFIGURATION_REQUIRED,
    PROMPT,
    REVIEW_BOUNDARY,
    WREN_RUNTIME_NOT_CONFIGURED,
    process_input,
    run_cli,
    state_snapshot,
)
from datapilot.tools.wren_tools import WrenQueryResult
from datapilot.tracing.trace import EventType


class StaticPlannerModel:
    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any],
    ) -> str:
        del system_prompt, user_prompt, response_schema
        return json.dumps(
            {
                "intent": "single_query",
                "reason_summary": "The question needs one database lookup.",
                "tasks": [
                    {
                        "task_id": "task_1",
                        "description": "Retrieve July GMV.",
                        "task_type": "query",
                        "depends_on": [],
                        "status": "pending",
                    }
                ],
                "requires_database": True,
                "requires_context": False,
                "is_follow_up": False,
            }
        )


class StaticSQLModel:
    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any],
    ) -> str:
        del system_prompt, user_prompt, response_schema
        return json.dumps(
            {
                "sql": "SELECT SUM(gmv) AS gmv FROM orders",
                "summary": "Retrieve July GMV.",
            }
        )


class StaticReviewerModel:
    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any],
    ) -> str:
        del system_prompt, user_prompt, response_schema
        return json.dumps(
            {
                "decision": "approve",
                "reason_summary": "The SQL result supports the task.",
                "issues": [],
                "retry_instruction": None,
                "confidence": 0.95,
            }
        )


class SequenceModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any],
    ) -> str:
        del system_prompt, user_prompt, response_schema
        return self.responses.pop(0)


class FakeWrenTools:
    def fetch_context(self, question: str, *, limit: int = 5) -> dict[str, Any]:
        del question, limit
        return {"strategy": "full", "schema": "orders(gmv decimal)"}

    def recall_queries(
        self,
        question: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        del question, limit
        return []

    def dry_plan(self, sql: str) -> str:
        return f"planned: {sql}"

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        del sql, limit
        return WrenQueryResult(
            columns=["gmv"],
            rows=[{"gmv": 42}],
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


def test_cli_state_initialization() -> None:
    result = process_input("分析 7 月 GMV")

    assert result.state["original_query"] == "分析 7 月 GMV"
    assert result.state["trace_id"] == result.trace.trace_id
    assert result.state["retry_count"] == 0
    assert [event.event_type for event in result.trace.get_events()] == [
        EventType.USER_QUERY,
        EventType.STATE_CREATED,
    ]


def test_state_snapshot_is_json_serializable() -> None:
    result = process_input("分析 7 月 GMV")

    snapshot = state_snapshot(result.state)

    assert snapshot["messages"] == [{"role": "user", "content": "分析 7 月 GMV"}]
    assert snapshot["business_context"] == {"rules": [], "definitions": {}}
    assert json.loads(json.dumps(snapshot, ensure_ascii=False))["original_query"] == (
        "分析 7 月 GMV"
    )


def test_cli_requires_llm_configuration_without_fake_success() -> None:
    inputs = iter(["分析 7 月 GMV", "exit"])
    prompts: list[str] = []
    outputs: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(inputs)

    exit_code = run_cli(
        input_fn=fake_input,
        output_fn=outputs.append,
        environ={},
    )

    assert exit_code == 0
    assert prompts == [PROMPT, PROMPT]
    assert outputs == [
        PLANNER_CONFIGURATION_REQUIRED,
        PLANNER_CONFIGURATION_HELP,
        "Goodbye.",
    ]
    assert not any("Intent:" in output for output in outputs)


def test_cli_prints_real_planner_result_from_injected_model() -> None:
    inputs = iter(["分析 7 月 GMV", "quit"])
    outputs: list[str] = []
    planner = Planner(model_client=StaticPlannerModel())

    exit_code = run_cli(
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        planner=planner,
        environ={},
    )

    assert exit_code == 0
    assert outputs[-1] == "Goodbye."
    assert "[Planner]" in outputs[0]
    assert "Intent: single_query" in outputs[0]
    assert "Retrieve July GMV." in outputs[0]
    assert "SELECT" not in outputs[0].upper()
    assert outputs[1] == WREN_RUNTIME_NOT_CONFIGURED


def test_cli_runs_sql_agent_without_fake_final_answer() -> None:
    inputs = iter(["分析 7 月 GMV", "exit"])
    outputs: list[str] = []
    planner = Planner(model_client=StaticPlannerModel())
    sql_agent = SQLAgent(
        model_client=StaticSQLModel(),
        wren_tools=FakeWrenTools(),
    )
    reviewer = Reviewer(model_client=StaticReviewerModel())

    exit_code = run_cli(
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        planner=planner,
        sql_agent=sql_agent,
        reviewer=reviewer,
        environ={},
    )

    assert exit_code == 0
    assert "[Planner]" in outputs[0]
    assert "[SQL Agent]" in outputs[1]
    assert "[Context]" in outputs[1]
    assert "[SQL]" in outputs[1]
    assert "[Dry Plan]\nSuccess" in outputs[1]
    assert "[Execution]\nRows: 1" in outputs[1]
    assert "[Reviewer]" in outputs[2]
    assert "Decision: approve" in outputs[2]
    assert outputs[3] == REVIEW_BOUNDARY
    assert outputs[4] == "Goodbye."
    assert not any("final answer" in output.lower() for output in outputs)


def test_cli_displays_semantic_retry_without_final_answer() -> None:
    inputs = iter(["分析 7 月 GMV", "exit"])
    outputs: list[str] = []
    sql_model = SequenceModel(
        [
            json.dumps({"sql": "SELECT COUNT(*) FROM orders", "summary": "Count."}),
            json.dumps(
                {
                    "sql": "SELECT SUM(gmv) AS gmv FROM orders",
                    "summary": "Calculate GMV.",
                }
            ),
        ]
    )
    reviewer_model = SequenceModel(
        [
            json.dumps(
                {
                    "decision": "retry",
                    "reason_summary": "COUNT(*) is not GMV.",
                    "issues": [
                        {
                            "issue_type": "metric_mismatch",
                            "description": "COUNT(*) is not GMV.",
                        }
                    ],
                    "retry_instruction": "Use SUM(gmv).",
                    "confidence": 0.99,
                }
            ),
            StaticReviewerModel().complete(
                system_prompt="",
                user_prompt="",
                response_schema={},
            ),
        ]
    )

    exit_code = run_cli(
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        planner=Planner(model_client=StaticPlannerModel()),
        sql_agent=SQLAgent(model_client=sql_model, wren_tools=FakeWrenTools()),
        reviewer=Reviewer(model_client=reviewer_model),
        environ={},
    )

    assert exit_code == 0
    assert "[Reviewer]\nDecision: retry" in outputs[2]
    assert "Issue: metric_mismatch" in outputs[2]
    assert "[SQL Agent Retry]" in outputs[3]
    assert "Decision: approve" in outputs[4]
    assert outputs[5] == REVIEW_BOUNDARY
    assert outputs[6] == "Goodbye."
    assert not any("final answer" in output.lower() for output in outputs)


def test_cli_quit_exits_without_creating_state() -> None:
    outputs: list[str] = []

    exit_code = run_cli(input_fn=lambda _: "quit", output_fn=outputs.append)

    assert exit_code == 0
    assert outputs == ["Goodbye."]
