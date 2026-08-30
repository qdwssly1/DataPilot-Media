from __future__ import annotations

import json
from typing import Any

from datapilot.agent.planner import Planner
from datapilot.cli import (
    PLANNER_CONFIGURATION_HELP,
    PLANNER_CONFIGURATION_REQUIRED,
    PROMPT,
    process_input,
    run_cli,
    state_snapshot,
)
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


def test_cli_quit_exits_without_creating_state() -> None:
    outputs: list[str] = []

    exit_code = run_cli(input_fn=lambda _: "quit", output_fn=outputs.append)

    assert exit_code == 0
    assert outputs == ["Goodbye."]
