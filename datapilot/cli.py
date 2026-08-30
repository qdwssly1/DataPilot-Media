"""Interactive CLI skeleton for DataPilot Phase 2."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any

from datapilot.agent.state import AgentState, create_initial_state
from datapilot.tracing.trace import EventType, TraceCollector

PROMPT = "DataPilot > "
EXIT_COMMANDS = frozenset({"exit", "quit"})
WORKFLOW_NOT_IMPLEMENTED = "Agent workflow is not implemented yet."


@dataclass(slots=True)
class InitializationResult:
    """State and trace created for one accepted CLI question."""

    state: AgentState
    trace: TraceCollector


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


def run_cli(
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Run the Phase 2 interactive loop until the user exits."""

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

        result = process_input(query)
        rendered_state = json.dumps(
            state_snapshot(result.state), ensure_ascii=False, indent=2
        )
        output_fn(rendered_state)
        output_fn(WORKFLOW_NOT_IMPLEMENTED)


def main() -> int:
    """CLI module entry point."""

    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
