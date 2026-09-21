"""Interactive multi-turn CLI for the DataPilot workflow."""

from __future__ import annotations

from collections.abc import Callable, Mapping
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
ANALYST_BOUNDARY = FINAL_ANSWER_UNAVAILABLE
REVIEW_BOUNDARY = FINAL_ANSWER_UNAVAILABLE


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
) -> int:
    """Run a reusable in-memory session until user exit."""

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
        if query.lower() in RESET_COMMANDS:
            reset_trace = TraceCollector()
            clear_session(
                active_store,
                active_session_id,
                trace=reset_trace,
            )
            output_fn(SESSION_CLEARED)
            continue
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
        if active_resolver is None:
            active_resolver = FollowUpResolver(
                model_client=active_planner.model_client,
            )
        if active_extractor is None:
            active_extractor = SessionContextExtractor(
                model_client=active_planner.model_client,
            )

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
            output_fn(f"Planner failed: {exc}")
            _print_run_summary(output_fn, initialized.trace)
            continue
        output_fn(format_planner_result(planning.initial_result))
        if initialized.state["was_follow_up"]:
            output_fn("[Session]\nFollow-up detected")
            if not planning.can_execute or planning.resolution is None:
                reason = planning.error or "Follow-up requires clarification."
                output_fn(f"[Session]\nCannot resolve follow-up: {reason}")
                _print_run_summary(output_fn, initialized.trace)
                continue
            output_fn(f"[Resolved Query]\n{initialized.state['resolved_query']}")
            if planning.effective_result is not None:
                output_fn(format_planner_result(planning.effective_result))

        if active_sql_agent is None:
            if active_wren_tools is None:
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
            output_fn(f"DataPilot workflow failed: {exc}")
            _print_run_summary(output_fn, initialized.trace)
            continue
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
            _print_run_summary(output_fn, initialized.trace)
            continue
        for analysis_result in workflow.analysis_results:
            output_fn(format_analysis_result(analysis_result))
        if workflow.final_answer_result is None:
            output_fn(FINAL_ANSWER_UNAVAILABLE)
        else:
            output_fn(format_final_answer(workflow.final_answer_result))
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
                output_fn(f"Session memory update failed: {exc}")
        _print_run_summary(output_fn, initialized.trace)


def main() -> int:
    """CLI module entry point."""

    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
