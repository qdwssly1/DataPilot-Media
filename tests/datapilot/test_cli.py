from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

import pytest

from datapilot import cli
from datapilot.agent.analyst import Analyst
from datapilot.agent.planner import Planner
from datapilot.agent.reviewer import Reviewer
from datapilot.agent.sql_agent import SQLAgent
from datapilot.cli import (
    CLI_ANALYSIS_COMPLETE,
    CLI_ANALYZING,
    CLI_HEADER,
    CLI_QUESTION_HINT,
    CLI_VERBOSE_HINT,
    HUMAN_CONFIGURATION_REQUIRED,
    HUMAN_GOODBYE,
    PLANNER_CONFIGURATION_HELP,
    PLANNER_CONFIGURATION_REQUIRED,
    PROMPT,
    REVIEW_BOUNDARY,
    WREN_RUNTIME_NOT_CONFIGURED,
    process_input,
    run_cli,
    state_snapshot,
)
from datapilot.cli_renderer import HUMAN_ANSWER_UNAVAILABLE
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


class RecordingPlannerModel(StaticPlannerModel):
    def __init__(self) -> None:
        self.user_prompts: list[str] = []

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any],
    ) -> str:
        self.user_prompts.append(user_prompt)
        return super().complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=response_schema,
        )


class MultiStepPlannerModel:
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
                "intent": "multi_step_analysis",
                "reason_summary": "Two periods must be compared.",
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
                        "description": "Compare category GMV.",
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


class PlanningContextWrenTools(FakeWrenTools):
    def fetch_planning_context(self) -> dict[str, Any]:
        return {
            "version": 1,
            "entities": [
                {
                    "name": "orders",
                    "important_fields": [{"name": "gmv", "type": "DECIMAL"}],
                }
            ],
            "metrics": [{"name": "gmv", "source": "orders"}],
            "truncated": False,
        }


class FailingPlanningContextWrenTools(FakeWrenTools):
    def fetch_planning_context(self) -> dict[str, Any]:
        raise RuntimeError("compiled manifest is temporarily unavailable")


class ComparisonWrenTools(FakeWrenTools):
    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        del limit
        rows = (
            [{"category": "A", "gmv": 100}, {"category": "B", "gmv": 200}]
            if "Q2" in sql
            else [{"category": "A", "gmv": 80}, {"category": "B", "gmv": 240}]
        )
        return WrenQueryResult(
            columns=["category", "gmv"],
            rows=rows,
            row_count=2,
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
        verbose=True,
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
        verbose=True,
    )

    assert exit_code == 0
    assert outputs[-1] == "Goodbye."
    assert "[Planner]" in outputs[0]
    assert "Intent: single_query" in outputs[0]
    assert "Retrieve July GMV." in outputs[0]
    assert "SELECT" not in outputs[0].upper()
    assert outputs[1] == WREN_RUNTIME_NOT_CONFIGURED


def test_cli_fetches_planning_context_before_planner() -> None:
    inputs = iter(["分析 7 月 GMV", "exit"])
    outputs: list[str] = []
    planner_model = RecordingPlannerModel()
    tools = PlanningContextWrenTools()

    exit_code = run_cli(
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        planner=Planner(model_client=planner_model),
        sql_agent=SQLAgent(
            model_client=StaticSQLModel(),
            wren_tools=tools,
        ),
        reviewer=Reviewer(model_client=StaticReviewerModel()),
        wren_tools=tools,
        environ={},
        verbose=True,
    )

    assert exit_code == 0
    assert "Current domain planning context" in planner_model.user_prompts[0]
    assert '"name":"orders"' in planner_model.user_prompts[0]
    assert "[Planner]" in outputs[0]


def test_cli_planner_continues_when_planning_context_fetch_fails() -> None:
    inputs = iter(["分析 7 月 GMV", "exit"])
    outputs: list[str] = []
    planner_model = RecordingPlannerModel()
    tools = FailingPlanningContextWrenTools()

    exit_code = run_cli(
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        planner=Planner(model_client=planner_model),
        sql_agent=SQLAgent(
            model_client=StaticSQLModel(),
            wren_tools=tools,
        ),
        reviewer=Reviewer(model_client=StaticReviewerModel()),
        wren_tools=tools,
        environ={},
        verbose=True,
    )

    assert exit_code == 0
    assert planner_model.user_prompts == ["User query:\n分析 7 月 GMV"]
    assert "[Planner]" in outputs[0]
    assert not any("Planner failed" in output for output in outputs)


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
        verbose=True,
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
    assert "[Run Summary]" in outputs[4]
    assert "Tasks: 1/1 completed" in outputs[4]
    assert "SQL Queries: 1" in outputs[4]
    assert outputs[5] == "Goodbye."
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
        verbose=True,
    )

    assert exit_code == 0
    assert "[Reviewer]\nDecision: retry" in outputs[2]
    assert "Issue: metric_mismatch" in outputs[2]
    assert "[SQL Agent Retry]" in outputs[3]
    assert "Decision: approve" in outputs[4]
    assert outputs[5] == REVIEW_BOUNDARY
    assert "[Run Summary]" in outputs[6]
    assert "Semantic Retries: 1" in outputs[6]
    assert outputs[7] == "Goodbye."
    assert not any("final answer" in output.lower() for output in outputs)


@pytest.mark.parametrize("verbose", [False, True])
def test_cli_displays_analyst_and_grounded_final_answer(verbose: bool) -> None:
    inputs = iter(["比较 Q2 和 Q3 各商品类别 GMV", "exit"])
    outputs: list[str] = []
    sql_model = SequenceModel(
        [
            json.dumps(
                {
                    "sql": (
                        "SELECT category, gmv FROM orders "
                        "WHERE quarter = 'Q2'"
                    ),
                    "summary": "Retrieve Q2.",
                }
            ),
            json.dumps(
                {
                    "sql": (
                        "SELECT category, gmv FROM orders "
                        "WHERE quarter = 'Q3'"
                    ),
                    "summary": "Retrieve Q3.",
                }
            ),
        ]
    )
    answer_model = SequenceModel(
        [
            json.dumps(
                {
                    "data_evidence_ids": [
                        "data:analysis-comparison:1:group:1",
                        "data:analysis-comparison:1:group:2",
                    ],
                    "knowledge_evidence_ids": [],
                    "inferences": [
                        {
                            "bundle_id": "bundle:multi_group:ALL:category",
                            "claim_type": "observation",
                            "predicate": "general",
                            "polarity": "neutral",
                            "subject_evidence_ids": [],
                            "supporting_evidence_ids": [
                                "data:analysis-comparison:1:group:1",
                                "data:analysis-comparison:1:group:2",
                            ],
                        }
                    ],
                    "limitation_ids": [
                        "limitation:bounded_evidence",
                        "limitation:limited_time_windows",
                    ],
                    "source_task_ids": ["compare"],
                }
            )
        ]
    )

    exit_code = run_cli(
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        planner=Planner(model_client=MultiStepPlannerModel()),
        sql_agent=SQLAgent(
            model_client=sql_model,
            wren_tools=ComparisonWrenTools(),
        ),
        reviewer=Reviewer(model_client=StaticReviewerModel()),
        analyst=Analyst(model_client=answer_model),
        environ={},
        verbose=verbose,
    )

    assert exit_code == 0
    if verbose:
        assert sum("[SQL Agent]" in output for output in outputs) == 2
        assert sum("[Reviewer]" in output for output in outputs) == 2
        assert any("[Analyst]" in output for output in outputs)
        assert any("[Final Answer]" in output for output in outputs)
        assert any("category=A" in output for output in outputs)
        assert any("[Run Summary]" in output for output in outputs)
    else:
        rendered = "\n".join(outputs)
        assert outputs[:3] == [CLI_HEADER, CLI_QUESTION_HINT, CLI_VERBOSE_HINT]
        assert CLI_ANALYZING in outputs
        assert CLI_ANALYSIS_COMPLETE in outputs
        assert "分析结论" in rendered
        assert "关键证据" in rendered
        for hidden in (
            "[Planner]", "[SQL]", "[Reviewer]", "[Analyst]", "[Run Summary]",
            "[Context]", "[Dry Plan]", "supports:", "data:q", "knowledge:",
            "data:analysis-comparison:", "SELECT",
        ):
            assert hidden not in rendered
    assert not any("A 类是主要变化对象" in output for output in outputs)


def test_cli_quit_exits_without_creating_state() -> None:
    outputs: list[str] = []

    exit_code = run_cli(input_fn=lambda _: "quit", output_fn=outputs.append)

    assert exit_code == 0
    assert outputs == [
        CLI_HEADER, CLI_QUESTION_HINT, CLI_VERBOSE_HINT, HUMAN_GOODBYE,
    ]


def test_cli_default_configuration_failure_is_concise() -> None:
    inputs = iter(["分析 7 月 GMV", "exit"])
    outputs: list[str] = []

    assert run_cli(
        input_fn=lambda _: next(inputs), output_fn=outputs.append, environ={}
    ) == 0

    assert outputs == [
        CLI_HEADER, CLI_QUESTION_HINT, CLI_VERBOSE_HINT,
        HUMAN_CONFIGURATION_REQUIRED, HUMAN_GOODBYE,
    ]


def test_cli_default_unapproved_result_has_no_debug_output() -> None:
    inputs = iter(["分析 7 月 GMV", "exit"])
    outputs: list[str] = []
    reviewer_response = json.dumps({
        "decision": "fail",
        "reason_summary": "Internal diagnostic: SQL has the wrong scope.",
        "issues": [{"issue_type": "filter_mismatch", "description": "Wrong scope."}],
        "retry_instruction": None,
        "confidence": 0.95,
    })

    exit_code = run_cli(
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        planner=Planner(model_client=StaticPlannerModel()),
        sql_agent=SQLAgent(model_client=StaticSQLModel(), wren_tools=FakeWrenTools()),
        reviewer=Reviewer(model_client=SequenceModel([reviewer_response])),
        environ={},
    )

    assert exit_code == 0
    assert outputs == [
        CLI_HEADER, CLI_QUESTION_HINT, CLI_VERBOSE_HINT, CLI_ANALYZING,
        HUMAN_ANSWER_UNAVAILABLE, HUMAN_GOODBYE,
    ]


def test_default_header_has_three_aligned_lines_and_no_ansi() -> None:
    lines = CLI_HEADER.splitlines()
    display_widths = [
        sum(2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
            for char in line)
        for line in lines
    ]

    assert len(lines) == 3
    assert display_widths[0] == display_widths[1] == display_widths[2]
    assert "DataPilot-Media" in lines[0]
    assert "\x1b[" not in CLI_HEADER


def test_human_answer_terminal_presentation_preserves_content() -> None:
    answer = (
        "## 分析结论\n\n- 播放成功率 95.00% → 50.00%，下降 45.00 个百分点。"
        "\n\n## 证据边界\n\n- 相关性不能证明因果关系。"
    )
    plain = cli._present_human_answer(answer, color=False)
    styled = cli._present_human_answer(answer, color=True)

    assert "## " not in plain
    assert "分析结论" in plain
    assert "证据边界" in plain
    assert "95.00% → 50.00%" in plain
    assert "下降 45.00 个百分点" in plain
    assert "\x1b[" not in plain
    assert "\x1b[" in styled
    assert re.sub(r"\x1b\[[0-9;]*m", "", styled) == plain


def test_terminal_color_requires_tty_and_respects_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TTY:
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    with monkeypatch.context() as context:
        context.setattr(cli.sys, "stdout", TTY())
        assert cli._terminal_supports_color(print, {}) is True
        assert cli._terminal_supports_color(print, {"NO_COLOR": "1"}) is False
        assert cli._terminal_supports_color(print, {"TERM": "dumb"}) is False
        assert cli._terminal_supports_color([].append, {}) is False


@pytest.mark.parametrize("argv,verbose", [([], False), (["--verbose"], True)])
def test_main_passes_presentation_flag_only(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], verbose: bool
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run_cli(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_cli", fake_run_cli)

    assert cli.main(argv) == 0
    assert calls == [{"verbose": verbose}]
