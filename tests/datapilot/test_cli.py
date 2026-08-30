from __future__ import annotations

import json

from datapilot.cli import (
    PROMPT,
    WORKFLOW_NOT_IMPLEMENTED,
    process_input,
    run_cli,
    state_snapshot,
)
from datapilot.tracing.trace import EventType


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


def test_cli_prints_initialized_state_without_fake_answer() -> None:
    inputs = iter(["分析 7 月 GMV", "exit"])
    prompts: list[str] = []
    outputs: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(inputs)

    exit_code = run_cli(input_fn=fake_input, output_fn=outputs.append)

    assert exit_code == 0
    assert prompts == [PROMPT, PROMPT]
    assert WORKFLOW_NOT_IMPLEMENTED in outputs
    assert "Goodbye." == outputs[-1]
    rendered_state = json.loads(outputs[0])
    assert rendered_state["original_query"] == "分析 7 月 GMV"
    assert rendered_state["generated_sql"] == []
    assert rendered_state["final_answer"] is None


def test_cli_quit_exits_without_creating_state() -> None:
    outputs: list[str] = []

    exit_code = run_cli(input_fn=lambda _: "quit", output_fn=outputs.append)

    assert exit_code == 0
    assert outputs == ["Goodbye."]
