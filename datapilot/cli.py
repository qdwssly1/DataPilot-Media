"""Interactive multi-turn CLI for the DataPilot workflow."""

from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any

from datapilot.agent.analyst import Analyst, AnalystError
from datapilot.agent.follow_up import (
    FollowUpResolutionError,
    FollowUpResolver,
    SessionContextError,
    SessionContextExtractor,
)
from datapilot.agent.graph import (
    commit_session_context,
    execute_task_plan,
    prepare_session_turn,
)
from datapilot.agent.planner import Planner, PlannerError, PlannerResult
from datapilot.agent.reviewer import Reviewer, ReviewerError
from datapilot.agent.sql_agent import SQLAgent, SQLAgentError, WrenTools
from datapilot.agent.state import (
    AgentState,
    AnalysisResult,
    FinalAnswerResult,
    ReviewerResult,
    SQLResult,
    SessionContext,
    TaskItem,
    create_initial_state,
)
from datapilot.cli_renderer import HUMAN_ANSWER_UNAVAILABLE, format_human_answer
from datapilot.llm.openai_compatible import OpenAICompatiblePlannerModel
from datapilot.memory.session_memory import SessionMemoryStore
from datapilot.retrieval.integration import (
    KnowledgeRetrieverProtocol,
    build_domain_retriever,
    retrieve_into_state,
)
from datapilot.tools.contracts import ToolRegistry
from datapilot.tools.discovery import build_domain_tool_registry
from datapilot.tools.router import ToolRouter
from datapilot.tools.wren_tools import (
    WrenConfigurationError,
    WrenToolAdapter,
    try_fetch_planning_context,
)
from datapilot.tracing.summary import format_trace_summary, summarize_trace
from datapilot.tracing.trace import EventType, TraceCollector

PROMPT = "DataPilot > "
EXIT_COMMANDS = frozenset({"exit", "quit"})
RESET_COMMANDS = frozenset({"reset", "clear"})
PLANNER_CONFIGURATION_REQUIRED = "Planner requires LLM configuration."
PLANNER_CONFIGURATION_HELP = "Set LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL."
WREN_RUNTIME_NOT_CONFIGURED = "Wren runtime/data source is not configured."
FINAL_ANSWER_UNAVAILABLE = "No grounded response was produced."
SESSION_CLEARED = "Session context cleared."
HUMAN_CONFIGURATION_REQUIRED = "模型尚未配置完成，请先完成配置后再提问。"
HUMAN_WREN_NOT_CONFIGURED = "数据源尚未配置完成，暂时无法分析。"
HUMAN_SESSION_CLEARED = "会话已清空。"
HUMAN_GOODBYE = "再见。"
ANALYST_BOUNDARY = FINAL_ANSWER_UNAVAILABLE
REVIEW_BOUNDARY = FINAL_ANSWER_UNAVAILABLE

_HEADER_CONTENT_WIDTH = 40
_HEADER_TITLE = "─ DataPilot-Media "
_HEADER_SUBTITLE = "音视频质量分析与故障排查智能体"
_HEADER_SUBTITLE_CELLS = sum(
    2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    for char in _HEADER_SUBTITLE
)
CLI_HEADER = "\n".join(
    (
        "╭" + _HEADER_TITLE + "─" * (_HEADER_CONTENT_WIDTH - len(_HEADER_TITLE)) + "╮",
        "│ "
        + _HEADER_SUBTITLE
        + " " * (_HEADER_CONTENT_WIDTH - 2 - _HEADER_SUBTITLE_CELLS)
        + " │",
        "╰" + "─" * _HEADER_CONTENT_WIDTH + "╯",
    )
)
CLI_QUESTION_HINT = "输入问题，或输入 exit 退出"
CLI_VERBOSE_HINT = "使用 --verbose 查看完整执行链路"
CLI_ANALYZING = "● 正在分析…"
CLI_ANALYSIS_COMPLETE = "✓ 分析完成"

_ANSI_RESET = "\x1b[0m"
_PALETTE = {
    "brand": "\x1b[38;5;110m",
    "border": "\x1b[38;5;103m",
    "heading": "\x1b[38;5;110m",
    "evidence": "\x1b[38;5;108m",
    "success": "\x1b[38;5;108m",
    "uncertain": "\x1b[38;5;180m",
    "boundary": "\x1b[38;5;173m",
    "body": "\x1b[38;5;250m",
    "muted": "\x1b[38;5;245m",
}


def _terminal_supports_color(
    output_fn: Callable[[str], None],
    environ: Mapping[str, str] | None,
) -> bool:
    """Use ANSI only for the actual interactive stdout, never captured output."""

    if output_fn is not print:
        return False
    if "NO_COLOR" in os.environ or (environ is not None and "NO_COLOR" in environ):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    if environ is not None and environ.get("TERM", "").lower() == "dumb":
        return False
    if os.name == "nt" and not any(
        (
            os.environ.get("WT_SESSION"),
            os.environ.get("ANSICON"),
            os.environ.get("ConEmuANSI") == "ON",
            os.environ.get("TERM_PROGRAM"),
            os.environ.get("TERM"),
            (environ or {}).get("TERM"),
        )
    ):
        return False
    return sys.stdout.isatty()


def _color(text: str, name: str, *, enabled: bool) -> str:
    return f"{_PALETTE[name]}{text}{_ANSI_RESET}" if enabled else text


def _welcome_header(*, color: bool) -> str:
    if not color:
        return CLI_HEADER
    top, middle, bottom = CLI_HEADER.splitlines()
    prefix, brand, suffix = top.partition("DataPilot-Media")
    return "\n".join(
        (
            _color(prefix, "border", enabled=True)
            + _color(brand, "brand", enabled=True)
            + _color(suffix, "border", enabled=True),
            _color(middle[:2], "border", enabled=True)
            + _color(middle[2:-1], "body", enabled=True)
            + _color(middle[-1], "border", enabled=True),
            _color(bottom, "border", enabled=True),
        )
    )


def _present_human_answer(answer: str, *, color: bool) -> str:
    """Style only the formatter's known headings and bullets; keep wording intact."""

    heading_colors = {
        "关键证据": "evidence",
        "可能原因": "uncertain",
        "证据边界": "boundary",
    }
    lines: list[str] = []
    for line in answer.splitlines():
        if line.startswith("## "):
            title = line[3:]
            tone = heading_colors.get(title, "heading")
            lines.append(_color("  " + title, tone, enabled=color))
        elif line.startswith("- "):
            lines.append(
                "  "
                + _color("•", "border", enabled=color)
                + " "
                + _color(line[2:], "body", enabled=color)
            )
        elif line.startswith("  ") and line.strip():
            lines.append("  " + _color(line, "body", enabled=color))
        else:
            lines.append(line)
    return "\n".join(lines)


@dataclass(slots=True)
class InitializationResult:
    """State and trace created for one accepted CLI question."""

    state: AgentState
    trace: TraceCollector


@dataclass(slots=True)
class PlannerExecutionResult:
    """State, trace, and structured result from one Planner execution."""

    state: AgentState
    trace: TraceCollector
    planner_result: PlannerResult


def process_input(
    user_query: str,
    *,
    session_id: str | None = None,
    previous_session: SessionContext | None = None,
) -> InitializationResult:
    """Initialize state and observable trace events without running an agent."""

    query = user_query.strip()
    if not query:
        raise ValueError("user_query must not be empty")

    trace = TraceCollector()
    trace.add_event(
        EventType.USER_QUERY,
        component="cli",
        action="accept_input",
        summary="Accepted a user query from the CLI.",
        metadata={"query": query},
    )
    state = create_initial_state(
        query,
        trace_id=trace.trace_id,
        session_id=session_id,
        previous_session=previous_session,
    )
    trace.add_event(
        EventType.STATE_CREATED,
        component="agent.state",
        action="create_initial_state",
        summary="Created the initial DataPilot agent state.",
        metadata={"retry_count": state["retry_count"]},
    )
    if session_id is not None and (
        previous_session is None or previous_session.turn_index == 0
    ):
        trace.add_event(
            EventType.SESSION_CREATED,
            component="cli",
            action="create_session",
            summary="Created a DataPilot CLI session.",
            metadata={
                "session_id": state["session_context"].session_id[:8],
                "turn_index": state["session_context"].turn_index,
            },
        )
    return InitializationResult(state=state, trace=trace)


def process_planner_input(
    user_query: str,
    planner: Planner,
    *,
    planning_context: Mapping[str, Any] | None = None,
) -> PlannerExecutionResult:
    """Initialize one request and run only the Planner component."""

    initialized = process_input(user_query)
    planner_result = planner.plan(
        initialized.state,
        trace=initialized.trace,
        planning_context=planning_context,
    )
    return PlannerExecutionResult(
        state=initialized.state,
        trace=initialized.trace,
        planner_result=planner_result,
    )


def _json_ready(value: Any) -> Any:
    """Convert state values to standard JSON-compatible structures."""

    if is_dataclass(value) and not isinstance(value, type):
        return {key: _json_ready(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value


def state_snapshot(state: AgentState) -> dict[str, Any]:
    """Return the full initialized state as a JSON-compatible dictionary."""

    return {key: _json_ready(value) for key, value in state.items()}


def format_planner_result(result: PlannerResult) -> str:
    """Render a validated plan without inventing SQL or data results."""

    lines = [
        "[Planner]",
        f"Intent: {result.intent.value}",
        f"Reason: {result.reason_summary}",
        "",
        "Tasks:",
    ]
    for index, task in enumerate(result.tasks, start=1):
        dependencies = ", ".join(task.depends_on) or "none"
        lines.append(
            f"{index}. [{task.task_type}] {task.description} "
            f"(depends_on: {dependencies})"
        )
    return "\n".join(lines)


def format_sql_result(
    task: TaskItem,
    result: SQLResult,
    *,
    is_semantic_retry: bool = False,
) -> str:
    """Render observable SQL Agent stages without generating an answer."""

    if result.execution_source == "tool":
        source_label = f"[Media Tool: {result.tool_name}]"
    else:
        source_label = "[SQL Agent Retry]" if is_semantic_retry else "[SQL Agent]"
    lines = [
        source_label,
        f"Task: {task.task_id} - {task.description}",
        "",
        "[Context]",
        result.context_summary or "Unavailable",
    ]
    if not result.success:
        lines.extend(["", "[Execution]", f"Failed: {result.error}"])
        return "\n".join(lines)
    lines.extend(
        [
            "",
            "[SQL]",
            result.sql,
            "",
            "[Dry Plan]",
            "Success",
            "",
            "[Execution]",
            f"Rows: {result.row_count}",
        ]
    )
    return "\n".join(lines)


def format_reviewer_result(result: ReviewerResult) -> str:
    """Render one structured semantic decision without inventing analysis."""

    lines = [
        "[Reviewer]",
        f"Decision: {result.decision}",
        f"Reason: {result.reason_summary}",
    ]
    for issue in result.issues:
        lines.append(f"Issue: {issue.issue_type} - {issue.description}")
    if result.retry_instruction:
        lines.append(f"Retry instruction: {result.retry_instruction}")
    return "\n".join(lines)


def format_analysis_result(result: AnalysisResult) -> str:
    """Render one grounded analysis result and its deterministic findings."""

    lines = [
        "[Analyst]",
        f"Task: {result.task_id}",
        f"Summary: {result.summary}",
    ]
    for finding in result.findings:
        lines.append(f"Finding: {finding}")
    if not result.success:
        lines.append(f"Failed: {result.error}")
    return "\n".join(lines)


def format_final_answer(result: FinalAnswerResult) -> str:
    """Render the model-authored answer only after grounding validation."""

    if not result.success:
        return f"[Final Answer]\nFailed: {result.error}"
    return f"[Final Answer]\n{result.answer}"


def clear_session(
    session_store: SessionMemoryStore,
    session_id: str,
    *,
    trace: TraceCollector | None = None,
) -> None:
    """Clear one CLI session and optionally emit a safe trace event."""

    session_store.clear(session_id)
    session_store.create(session_id)
    if trace is not None:
        trace.add_event(
            EventType.SESSION_CONTEXT_CLEARED,
            component="cli",
            action="clear_session",
            summary="Cleared DataPilot session context.",
            metadata={"session_id": session_id[:8]},
        )


def _print_run_summary(
    output_fn: Callable[[str], None],
    trace: TraceCollector,
) -> None:
    output_fn(format_trace_summary(summarize_trace(trace.get_events())))


def run_cli(
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    planner: Planner | None = None,
    sql_agent: SQLAgent | None = None,
    reviewer: Reviewer | None = None,
    analyst: Analyst | None = None,
    follow_up_resolver: FollowUpResolver | None = None,
    context_extractor: SessionContextExtractor | None = None,
    session_store: SessionMemoryStore | None = None,
    session_id: str | None = None,
    wren_tools: WrenTools | None = None,
    knowledge_retriever: KnowledgeRetrieverProtocol | None = None,
    tool_registry: ToolRegistry | None = None,
    tool_router: ToolRouter | None = None,
    environ: Mapping[str, str] | None = None,
    verbose: bool = False,
) -> int:
    """Run one session; verbose controls presentation only."""

    active_planner = planner
    active_sql_agent = sql_agent
    active_reviewer = reviewer
    active_analyst = analyst
    active_resolver = follow_up_resolver
    active_extractor = context_extractor
    active_wren_tools = wren_tools
    active_knowledge_retriever = knowledge_retriever
    active_tool_registry = tool_registry
    active_tool_router = tool_router
    if active_tool_router is None and active_tool_registry is not None:
        active_tool_router = ToolRouter(active_tool_registry)
    knowledge_discovery_attempted = knowledge_retriever is not None
    tool_discovery_attempted = active_tool_router is not None
    active_store = (
        session_store if session_store is not None else SessionMemoryStore()
    )
    active_session = active_store.create(session_id)
    active_session_id = active_session.session_id
    use_color = not verbose and _terminal_supports_color(output_fn, environ)
    if not verbose:
        output_fn(_welcome_header(color=use_color))
        output_fn(_color(CLI_QUESTION_HINT, "muted", enabled=use_color))
        output_fn(_color(CLI_VERBOSE_HINT, "muted", enabled=use_color))

    while True:
        try:
            raw_input = input_fn(PROMPT)
        except EOFError:
            output_fn("Goodbye." if verbose else HUMAN_GOODBYE)
            return 0
        except KeyboardInterrupt:
            output_fn("")
            output_fn("Goodbye." if verbose else HUMAN_GOODBYE)
            return 0

        query = raw_input.strip()
        if query.lower() in EXIT_COMMANDS:
            output_fn("Goodbye." if verbose else HUMAN_GOODBYE)
            return 0
        if query.lower() in RESET_COMMANDS:
            reset_trace = TraceCollector()
            clear_session(
                active_store,
                active_session_id,
                trace=reset_trace,
            )
            output_fn(SESSION_CLEARED if verbose else HUMAN_SESSION_CLEARED)
            continue
        if not query:
            continue

        if active_planner is None:
            try:
                model_client = OpenAICompatiblePlannerModel.from_env(environ)
            except PlannerError:
                if verbose:
                    output_fn(PLANNER_CONFIGURATION_REQUIRED)
                    output_fn(PLANNER_CONFIGURATION_HELP)
                else:
                    output_fn(HUMAN_CONFIGURATION_REQUIRED)
                continue
            active_planner = Planner(model_client=model_client)
        if active_resolver is None:
            active_resolver = FollowUpResolver(
                model_client=active_planner.model_client,
            )
        if active_extractor is None:
            active_extractor = SessionContextExtractor(
                model_client=active_planner.model_client,
            )

        if not verbose:
            output_fn(_color(CLI_ANALYZING, "brand", enabled=use_color))

        previous_session = active_store.get(active_session_id)
        initialized = process_input(
            query,
            session_id=active_session_id,
            previous_session=previous_session,
        )
        if not knowledge_discovery_attempted:
            knowledge_discovery_attempted = True
            try:
                active_knowledge_retriever = build_domain_retriever(environ)
            except Exception:
                active_knowledge_retriever = None
        retrieve_into_state(
            initialized.state,
            active_knowledge_retriever,
        )
        planning_tools: Any = active_wren_tools
        if planning_tools is None and active_sql_agent is not None:
            planning_tools = active_sql_agent.wren_tools
        if planning_tools is None:
            try:
                active_wren_tools = WrenToolAdapter.from_env(environ)
            except WrenConfigurationError:
                active_wren_tools = None
            planning_tools = active_wren_tools
        planning_context = try_fetch_planning_context(planning_tools)
        if not tool_discovery_attempted:
            tool_discovery_attempted = True
            active_tool_registry = build_domain_tool_registry(
                planning_tools,
                planning_context,
            )
            if active_tool_registry is not None:
                active_tool_router = ToolRouter(
                    active_tool_registry,
                    capability_context=planning_context,
                )
        try:
            planning = prepare_session_turn(
                initialized.state,
                active_planner,
                active_resolver,
                active_store,
                trace=initialized.trace,
                planning_context=planning_context,
            )
        except (PlannerError, FollowUpResolutionError) as exc:
            if verbose:
                output_fn(f"Planner failed: {exc}")
                _print_run_summary(output_fn, initialized.trace)
            else:
                output_fn(HUMAN_ANSWER_UNAVAILABLE)
            continue
        if verbose:
            output_fn(format_planner_result(planning.initial_result))
        if initialized.state["was_follow_up"]:
            if verbose:
                output_fn("[Session]\nFollow-up detected")
            if not planning.can_execute or planning.resolution is None:
                reason = planning.error or "Follow-up requires clarification."
                if verbose:
                    output_fn(f"[Session]\nCannot resolve follow-up: {reason}")
                    _print_run_summary(output_fn, initialized.trace)
                else:
                    output_fn("请补充本次问题的分析对象或时间范围，再试一次。")
                continue
            if verbose:
                output_fn(f"[Resolved Query]\n{initialized.state['resolved_query']}")
                if planning.effective_result is not None:
                    output_fn(format_planner_result(planning.effective_result))

        if active_sql_agent is None:
            if active_wren_tools is None:
                output_fn(
                    WREN_RUNTIME_NOT_CONFIGURED
                    if verbose
                    else HUMAN_WREN_NOT_CONFIGURED
                )
                continue
            active_sql_agent = SQLAgent(
                model_client=active_planner.model_client,
                wren_tools=active_wren_tools,
            )
        if active_reviewer is None:
            active_reviewer = Reviewer(
                model_client=active_planner.model_client,
            )
        if active_analyst is None:
            active_analyst = Analyst(
                model_client=active_planner.model_client,
            )

        task_by_id = {
            task.task_id: task for task in initialized.state["task_plan"]
        }
        try:
            workflow = execute_task_plan(
                initialized.state,
                active_sql_agent,
                active_reviewer,
                active_analyst,
                trace=initialized.trace,
                tool_router=active_tool_router,
            )
        except (SQLAgentError, ReviewerError, AnalystError) as exc:
            if verbose:
                output_fn(f"DataPilot workflow failed: {exc}")
                _print_run_summary(output_fn, initialized.trace)
            else:
                output_fn(HUMAN_ANSWER_UNAVAILABLE)
            continue
        if verbose:
            for query_run in workflow.reviewed_queries:
                task = task_by_id[query_run.task_id]
                for index, sql_result in enumerate(query_run.sql_results):
                    output_fn(
                        format_sql_result(
                            task,
                            sql_result,
                            is_semantic_retry=index > 0,
                        )
                    )
                    if index < len(query_run.review_results):
                        output_fn(
                            format_reviewer_result(query_run.review_results[index])
                        )
        if any(not query_run.approved for query_run in workflow.reviewed_queries):
            if verbose:
                _print_run_summary(output_fn, initialized.trace)
            else:
                output_fn(HUMAN_ANSWER_UNAVAILABLE)
            continue
        if verbose:
            for analysis_result in workflow.analysis_results:
                output_fn(format_analysis_result(analysis_result))
        if workflow.final_answer_result is None:
            output_fn(
                FINAL_ANSWER_UNAVAILABLE if verbose else HUMAN_ANSWER_UNAVAILABLE
            )
        elif verbose:
            output_fn(format_final_answer(workflow.final_answer_result))
        else:
            human_answer = format_human_answer(workflow.final_answer_result)
            if human_answer == HUMAN_ANSWER_UNAVAILABLE:
                output_fn(human_answer)
            else:
                output_fn(
                    _color(CLI_ANALYSIS_COMPLETE, "success", enabled=use_color)
                )
                output_fn(_present_human_answer(human_answer, color=use_color))
        if planning.effective_result is not None:
            try:
                commit_session_context(
                    initialized.state,
                    planning.effective_result,
                    workflow,
                    active_store,
                    active_extractor,
                    trace=initialized.trace,
                    resolution=planning.resolution,
                )
            except SessionContextError as exc:
                if verbose:
                    output_fn(f"Session memory update failed: {exc}")
                else:
                    output_fn("会话记忆未能更新，后续提问请提供完整条件。")
        if verbose:
            _print_run_summary(output_fn, initialized.trace)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI module entry point."""

    parser = argparse.ArgumentParser(description="DataPilot 交互式分析助手")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="显示任务规划、查询、证据和运行摘要等调试信息",
    )
    args = parser.parse_args(argv)
    return run_cli(verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
