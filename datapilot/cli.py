"""Interactive CLI through DataPilot planning, SQL, and semantic review."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any

from datapilot.agent.graph import execute_ready_query_tasks
from datapilot.agent.planner import Planner, PlannerError, PlannerResult
from datapilot.agent.reviewer import Reviewer, ReviewerError
from datapilot.agent.sql_agent import SQLAgent, SQLAgentError, WrenTools
from datapilot.agent.state import (
    AgentState,
    ReviewerResult,
    SQLResult,
    TaskItem,
    create_initial_state,
)
from datapilot.llm.openai_compatible import OpenAICompatiblePlannerModel
from datapilot.tools.wren_tools import WrenConfigurationError, WrenToolAdapter
from datapilot.tracing.trace import EventType, TraceCollector

PROMPT = "DataPilot > "
EXIT_COMMANDS = frozenset({"exit", "quit"})
PLANNER_CONFIGURATION_REQUIRED = "Planner requires LLM configuration."
PLANNER_CONFIGURATION_HELP = "Set LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL."
WREN_RUNTIME_NOT_CONFIGURED = "Wren runtime/data source is not configured."
ANALYST_BOUNDARY = "Analyst is not implemented yet."
REVIEW_BOUNDARY = ANALYST_BOUNDARY


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


def process_input(user_query: str) -> InitializationResult:
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
    state = create_initial_state(query, trace_id=trace.trace_id)
    trace.add_event(
        EventType.STATE_CREATED,
        component="agent.state",
        action="create_initial_state",
        summary="Created the initial DataPilot agent state.",
        metadata={"retry_count": state["retry_count"]},
    )
    return InitializationResult(state=state, trace=trace)


def process_planner_input(
    user_query: str,
    planner: Planner,
) -> PlannerExecutionResult:
    """Initialize one request and run only the Planner component."""

    initialized = process_input(user_query)
    planner_result = planner.plan(initialized.state, trace=initialized.trace)
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

    lines = [
        "[SQL Agent Retry]" if is_semantic_retry else "[SQL Agent]",
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


def run_cli(
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    planner: Planner | None = None,
    sql_agent: SQLAgent | None = None,
    reviewer: Reviewer | None = None,
    wren_tools: WrenTools | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run implemented nodes until the review boundary or user exit."""

    active_planner = planner
    active_sql_agent = sql_agent
    active_reviewer = reviewer
    active_wren_tools = wren_tools

    while True:
        try:
            raw_input = input_fn(PROMPT)
        except EOFError:
            output_fn("Goodbye.")
            return 0
        except KeyboardInterrupt:
            output_fn("")
            output_fn("Goodbye.")
            return 0

        query = raw_input.strip()
        if query.lower() in EXIT_COMMANDS:
            output_fn("Goodbye.")
            return 0
        if not query:
            continue

        if active_planner is None:
            try:
                model_client = OpenAICompatiblePlannerModel.from_env(environ)
            except PlannerError:
                output_fn(PLANNER_CONFIGURATION_REQUIRED)
                output_fn(PLANNER_CONFIGURATION_HELP)
                continue
            active_planner = Planner(model_client=model_client)

        try:
            result = process_planner_input(query, active_planner)
        except PlannerError as exc:
            output_fn(f"Planner failed: {exc}")
            continue
        output_fn(format_planner_result(result.planner_result))

        if active_sql_agent is None:
            if active_wren_tools is None:
                try:
                    active_wren_tools = WrenToolAdapter.from_env(environ)
                except WrenConfigurationError:
                    output_fn(WREN_RUNTIME_NOT_CONFIGURED)
                    continue
            active_sql_agent = SQLAgent(
                model_client=active_planner.model_client,
                wren_tools=active_wren_tools,
            )
        if active_reviewer is None:
            active_reviewer = Reviewer(
                model_client=active_planner.model_client,
            )

        task_by_id = {task.task_id: task for task in result.state["task_plan"]}
        try:
            query_runs = execute_ready_query_tasks(
                result.state,
                active_sql_agent,
                active_reviewer,
                trace=result.trace,
            )
        except (SQLAgentError, ReviewerError) as exc:
            output_fn(f"DataPilot workflow failed: {exc}")
            continue
        for query_run in query_runs:
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
        if any(not query_run.approved for query_run in query_runs):
            continue
        output_fn(ANALYST_BOUNDARY)


def main() -> int:
    """CLI module entry point."""

    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
