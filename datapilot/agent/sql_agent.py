"""DataPilot SQL Agent orchestration over existing Wren capabilities."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol

from datapilot.agent.state import (
    AgentState,
    ReviewerResult,
    SQLResult,
    TaskItem,
    get_effective_query,
)
from datapilot.tools.wren_tools import WrenQueryResult
from datapilot.tracing.trace import EventType, TraceCollector


class SQLModel(Protocol):
    """Dependency-injection boundary shared by real and fake LLM clients."""

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        """Return one complete structured response as text."""


class WrenTools(Protocol):
    """Only the existing Wren operations needed by the SQL Agent."""

    def fetch_context(
        self,
        question: str,
        *,
        limit: int = 5,
    ) -> dict[str, Any]: ...

    def recall_queries(
        self,
        question: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]: ...

    def dry_plan(self, sql: str) -> str: ...

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult: ...

    def store_query(
        self,
        nl: str,
        sql: str,
        *,
        tags: list[str] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class GeneratedSQL:
    """Validated structured output from the text-to-SQL model call."""

    sql: str
    summary: str


class SQLAgentError(RuntimeError):
    """Base error carrying a safe stage, task, attempt, and summary."""

    def __init__(
        self,
        *,
        stage: str,
        task_id: str,
        attempt: int,
        summary: str,
    ) -> None:
        self.stage = stage
        self.task_id = task_id
        self.attempt = attempt
        self.summary = summary
        super().__init__(
            f"{stage} failed for task {task_id} on attempt {attempt}: {summary}"
        )


class SQLSafetyError(SQLAgentError):
    """Raised when generated SQL violates the read-only entrance policy."""


class SQLGenerationError(SQLAgentError):
    """Raised when the LLM call or structured SQL output is invalid."""


class SQLFilterConsistencyError(SQLAgentError):
    """Raised when SQL changes an authoritative semantic filter literal."""


class SQLPlanningError(SQLAgentError):
    """Raised when Wren cannot dry-plan generated SQL."""


class SQLExecutionError(SQLAgentError):
    """Raised when Wren cannot execute dry-planned SQL."""


SQL_SYSTEM_PROMPT = """You are DataPilot's SQL Agent.
Generate exactly one read-only SQL query for the current task.
Use only the supplied Wren context, relevant schema, and verified query examples.
The original current TaskItem is authoritative. Reviewer feedback may correct SQL
but must never broaden, replace, or merge its task contract with sibling tasks.
Preserve every authoritative semantic filter value exactly as provided. Do not
translate categorical literals. A different value is allowed only when the supplied
Wren context contains an explicit canonical mapping for that filter and value.
Do not answer the user, invent data, modify data, or emit Markdown code fences.
Return one JSON object that exactly matches the supplied schema.
The sql field must contain SQL only; summary must be a short observable description.
"""

SQL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sql", "summary"],
    "properties": {
        "sql": {"type": "string", "minLength": 1},
        "summary": {"type": "string", "minLength": 1, "maxLength": 240},
    },
}

_DENIED_SQL_TOKENS = {
    "ALTER",
    "ATTACH",
    "CALL",
    "COPY",
    "CREATE",
    "DELETE",
    "DETACH",
    "DROP",
    "EXECUTE",
    "GRANT",
    "INSERT",
    "INTO",
    "MERGE",
    "PRAGMA",
    "REPLACE",
    "REVOKE",
    "TRUNCATE",
    "UPDATE",
    "VACUUM",
}

_CANONICAL_MAPPING_KEYS = {
    "canonical_mappings",
    "canonical_filter_mappings",
    "filter_value_mappings",
    "value_mappings",
}


def _safe_error_summary(error: BaseException) -> str:
    """Return a bounded message with common secret-bearing forms redacted."""

    text = " ".join(str(error).split()) or type(error).__name__
    text = re.sub(
        r"(?i)(api[_ -]?key|password|token|authorization)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"[a-z][a-z0-9+.-]*://\S+", "[REDACTED_URL]", text)
    return text[:300]


def _compact_json(value: Any, *, max_chars: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."


def _parse_generated_sql(response_text: str) -> GeneratedSQL:
    try:
        raw = json.loads(response_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("SQL response is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("SQL response must be a JSON object")
    expected = {"sql", "summary"}
    if set(raw) != expected:
        missing = sorted(expected - set(raw))
        extra = sorted(set(raw) - expected)
        raise ValueError(
            f"SQL response fields invalid; missing={missing}, extra={extra}"
        )
    sql = raw["sql"]
    summary = raw["summary"]
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("sql must not be empty")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary must not be empty")
    summary = summary.strip()
    if len(summary) > 240:
        raise ValueError("summary must be at most 240 characters")
    return GeneratedSQL(sql=sql.strip(), summary=summary)


def _authoritative_semantic_filters(
    state: AgentState,
) -> dict[str, list[str]]:
    """Return resolved follow-up filters that govern the current turn."""

    if not state["was_follow_up"]:
        return {}
    resolution = state["follow_up_resolution"]
    if resolution is None or not getattr(resolution, "can_resolve", False):
        return {}
    raw_filters = getattr(resolution, "filters", None)
    if not isinstance(raw_filters, Mapping):
        return {}
    return {
        str(name): [str(value) for value in values]
        for name, values in raw_filters.items()
        if isinstance(values, list)
    }


def _iter_canonical_mapping_blocks(value: Any) -> list[Mapping[str, Any]]:
    """Find only explicitly labelled canonical mapping objects in Wren context."""

    blocks: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _CANONICAL_MAPPING_KEYS:
                if isinstance(child, Mapping):
                    blocks.append(child)
                continue
            blocks.extend(_iter_canonical_mapping_blocks(child))
    elif isinstance(value, list):
        for child in value:
            blocks.extend(_iter_canonical_mapping_blocks(child))
    return blocks


def _allowed_filter_literals(
    context: Mapping[str, Any],
    filter_name: str,
    semantic_value: str,
) -> set[str]:
    """Return the source literal plus explicit Wren canonical equivalents."""

    allowed = {semantic_value}
    for block in _iter_canonical_mapping_blocks(context):
        field_mapping = block.get(filter_name)
        if not isinstance(field_mapping, Mapping):
            continue
        canonical = field_mapping.get(semantic_value)
        if isinstance(canonical, str) and canonical:
            allowed.add(canonical)
        elif isinstance(canonical, list):
            allowed.update(
                value for value in canonical if isinstance(value, str) and value
            )
    return allowed


def _sql_string_literals(sql: str) -> set[str]:
    """Extract single-quoted SQL literals after read-only syntax validation."""

    literals: set[str] = set()
    index = 0
    while index < len(sql):
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if sql[index] == "-" and following == "-":
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql[index] == "/" and following == "*":
            end = sql.find("*/", index + 2)
            index = len(sql) if end < 0 else end + 2
            continue
        if sql[index] != "'":
            index += 1
            continue
        index += 1
        value: list[str] = []
        while index < len(sql):
            if sql[index] != "'":
                value.append(sql[index])
                index += 1
                continue
            if index + 1 < len(sql) and sql[index + 1] == "'":
                value.append("'")
                index += 2
                continue
            index += 1
            literals.add("".join(value))
            break
    return literals


def validate_semantic_filter_literals(
    sql: str,
    *,
    filters: Mapping[str, list[str]],
    wren_context: Mapping[str, Any],
    task_id: str,
    attempt: int,
) -> None:
    """Fail before dry-plan when SQL drops or translates a semantic literal."""

    sql_literals = _sql_string_literals(sql)
    for filter_name, semantic_values in filters.items():
        for semantic_value in semantic_values:
            allowed = _allowed_filter_literals(
                wren_context,
                filter_name,
                semantic_value,
            )
            if sql_literals.isdisjoint(allowed):
                raise SQLFilterConsistencyError(
                    stage="filter_consistency",
                    task_id=task_id,
                    attempt=attempt,
                    summary=(
                        "authoritative semantic filter literal missing: "
                        f"{filter_name}={semantic_value}; preserve it exactly unless "
                        "Wren context provides an explicit canonical mapping"
                    ),
                )


def _sql_tokens(sql: str) -> list[str]:
    """Tokenize enough SQL to enforce a small read-only entrance policy."""

    tokens: list[str] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if char.isspace():
            index += 1
            continue
        if char == "-" and following == "-":
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if char == "/" and following == "*":
            end = sql.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated block comment")
            index = end + 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            while index < len(sql):
                if sql[index] == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise ValueError("unterminated quoted value")
            continue
        if char == "[":
            end = sql.find("]", index + 1)
            if end < 0:
                raise ValueError("unterminated quoted identifier")
            index = end + 1
            continue
        if char == ";":
            tokens.append(";")
            index += 1
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] in {"_", "$"}):
                end += 1
            tokens.append(sql[index:end].upper())
            index = end
            continue
        index += 1
    return tokens


def validate_read_only_sql(
    sql: str,
    *,
    task_id: str = "unknown",
    attempt: int = 1,
) -> None:
    """Reject writes and multi-statements while allowing SELECT and CTE queries."""

    try:
        tokens = _sql_tokens(sql)
    except ValueError as exc:
        raise SQLSafetyError(
            stage="safety",
            task_id=task_id,
            attempt=attempt,
            summary=str(exc),
        ) from exc
    if not tokens:
        raise SQLSafetyError(
            stage="safety",
            task_id=task_id,
            attempt=attempt,
            summary="SQL is empty after comments are removed",
        )
    semicolons = [index for index, token in enumerate(tokens) if token == ";"]
    if len(semicolons) > 1 or (semicolons and semicolons[0] != len(tokens) - 1):
        raise SQLSafetyError(
            stage="safety",
            task_id=task_id,
            attempt=attempt,
            summary="multiple SQL statements are not allowed",
        )
    statement_tokens = [token for token in tokens if token != ";"]
    denied = sorted(set(statement_tokens) & _DENIED_SQL_TOKENS)
    if denied:
        raise SQLSafetyError(
            stage="safety",
            task_id=task_id,
            attempt=attempt,
            summary=f"read-only policy rejected token: {denied[0]}",
        )
    if not statement_tokens or statement_tokens[0] not in {"SELECT", "WITH"}:
        raise SQLSafetyError(
            stage="safety",
            task_id=task_id,
            attempt=attempt,
            summary="only SELECT or WITH queries are allowed",
        )
    if "SELECT" not in statement_tokens:
        raise SQLSafetyError(
            stage="safety",
            task_id=task_id,
            attempt=attempt,
            summary="query must contain SELECT",
        )


def is_task_ready(state: AgentState, task: TaskItem) -> bool:
    """Return whether a pending query task has all dependencies completed."""

    completed_ids = {
        item.task_id for item in state["completed_tasks"] if item.status == "completed"
    }
    return (
        task.task_type == "query"
        and task.status == "pending"
        and set(task.depends_on) <= completed_ids
    )


class SQLAgent:
    """Generate and execute one ready query task through Wren."""

    def __init__(
        self,
        *,
        model_client: SQLModel,
        wren_tools: WrenTools,
        trace: TraceCollector | None = None,
        max_attempts: int = 2,
        query_limit: int = 100,
    ) -> None:
        if max_attempts not in {1, 2}:
            raise ValueError("max_attempts must be 1 or 2")
        if not 1 <= query_limit <= 1000:
            raise ValueError("query_limit must be between 1 and 1000")
        self.model_client = model_client
        self.wren_tools = wren_tools
        self.trace = trace
        self.max_attempts = max_attempts
        self.query_limit = query_limit

    def execute_task(
        self,
        state: AgentState,
        task: TaskItem,
        *,
        trace: TraceCollector | None = None,
        semantic_feedback: ReviewerResult | None = None,
        previous_result: SQLResult | None = None,
        semantic_retry_count: int = 0,
    ) -> SQLResult:
        """Run Context, Memory, generation, dry-plan, and query in order."""

        active_trace = trace if trace is not None else self.trace
        if active_trace is None:
            raise ValueError("a TraceCollector is required")
        if active_trace.trace_id != state["trace_id"]:
            raise ValueError("trace and state must use the same trace_id")
        if semantic_retry_count < 0:
            raise ValueError("semantic_retry_count must not be negative")
        if (semantic_feedback is None) != (previous_result is None):
            raise ValueError(
                "semantic_feedback and previous_result must be supplied together"
            )
        if semantic_feedback is not None:
            if semantic_feedback.decision != "retry":
                raise ValueError("semantic correction requires a retry decision")
            if previous_result is None or previous_result.task_id != task.task_id:
                raise ValueError("semantic correction result must match the task")

        started_at = perf_counter()
        self._add_event(
            active_trace,
            EventType.SQL_AGENT_STARTED,
            task_id=task.task_id,
            attempt=1,
            duration=0.0,
        )
        if not is_task_ready(state, task):
            error = SQLAgentError(
                stage="task_validation",
                task_id=task.task_id,
                attempt=1,
                summary="task is not a ready query task",
            )
            self._add_event(
                active_trace,
                EventType.SQL_AGENT_FAILED,
                task_id=task.task_id,
                attempt=1,
                duration=perf_counter() - started_at,
                error_type=type(error).__name__,
            )
            raise error
        task.status = "executing"

        try:
            context_started = perf_counter()
            context = self.wren_tools.fetch_context(task.description, limit=5)
        except Exception as exc:
            error = SQLAgentError(
                stage="context",
                task_id=task.task_id,
                attempt=1,
                summary=_safe_error_summary(exc),
            )
            return self._record_failure(
                state,
                task,
                error=error,
                sql="",
                retry_count=0,
                semantic_retry_count=semantic_retry_count,
                context_summary="",
                started_at=started_at,
                trace=active_trace,
            )
        self._add_event(
            active_trace,
            EventType.CONTEXT_FETCHED,
            task_id=task.task_id,
            attempt=1,
            duration=perf_counter() - context_started,
            tool="wren_fetch_context",
        )
        context_summary = _compact_json(context, max_chars=500)
        state["business_context"].definitions[
            f"wren_context:{task.task_id}"
        ] = context_summary

        memory_started = perf_counter()
        memory_error_type: str | None = None
        try:
            recalled_queries = self.wren_tools.recall_queries(
                task.description,
                limit=3,
            )
        except Exception as exc:
            recalled_queries = []
            memory_error_type = type(exc).__name__
        memory_metadata: dict[str, Any] = {
            "task_id": task.task_id,
            "attempt": 1,
            "row_count": len(recalled_queries),
            "duration": perf_counter() - memory_started,
            "tool": "wren_recall_queries",
        }
        if memory_error_type:
            memory_metadata["error_type"] = memory_error_type
        active_trace.add_event(
            EventType.SQL_MEMORY_RECALLED,
            component="sql_agent",
            action="recall_memory",
            summary="Wren SQL memory lookup completed.",
            metadata=memory_metadata,
        )

        authoritative_filters = _authoritative_semantic_filters(state)
        previous_sql = ""
        previous_error = ""
        last_error: SQLAgentError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                generated = self._generate_sql(
                    state,
                    task,
                    context=context,
                    recalled_queries=recalled_queries,
                    previous_sql=previous_sql,
                    previous_error=previous_error,
                    attempt=attempt,
                    semantic_feedback=semantic_feedback,
                    previous_result=previous_result,
                    authoritative_filters=authoritative_filters,
                )
                previous_sql = generated.sql
                self._add_event(
                    active_trace,
                    EventType.SQL_GENERATED,
                    task_id=task.task_id,
                    attempt=attempt,
                    duration=perf_counter() - started_at,
                )
                try:
                    validate_read_only_sql(
                        generated.sql,
                        task_id=task.task_id,
                        attempt=attempt,
                    )
                except SQLSafetyError as exc:
                    self._add_event(
                        active_trace,
                        EventType.SQL_SAFETY_REJECTED,
                        task_id=task.task_id,
                        attempt=attempt,
                        duration=perf_counter() - started_at,
                        error_type=type(exc).__name__,
                        summary=f"SQL safety rejected the query: {exc.summary}",
                    )
                    raise

                validate_semantic_filter_literals(
                    generated.sql,
                    filters=authoritative_filters,
                    wren_context=context,
                    task_id=task.task_id,
                    attempt=attempt,
                )

                dry_started = perf_counter()
                self._add_event(
                    active_trace,
                    EventType.DRY_PLAN_STARTED,
                    task_id=task.task_id,
                    attempt=attempt,
                    duration=0.0,
                    tool="wren_dry_plan",
                )
                try:
                    planned_sql = self.wren_tools.dry_plan(generated.sql)
                    if not isinstance(planned_sql, str) or not planned_sql.strip():
                        raise ValueError("Wren dry plan returned empty SQL")
                except Exception as exc:
                    error = SQLPlanningError(
                        stage="dry_plan",
                        task_id=task.task_id,
                        attempt=attempt,
                        summary=_safe_error_summary(exc),
                    )
                    self._add_event(
                        active_trace,
                        EventType.DRY_PLAN_FAILED,
                        task_id=task.task_id,
                        attempt=attempt,
                        duration=perf_counter() - dry_started,
                        error_type=type(exc).__name__,
                        tool="wren_dry_plan",
                        summary=f"Wren dry plan failed: {error.summary}",
                    )
                    raise error from exc
                self._add_event(
                    active_trace,
                    EventType.DRY_PLAN_SUCCEEDED,
                    task_id=task.task_id,
                    attempt=attempt,
                    duration=perf_counter() - dry_started,
                    tool="wren_dry_plan",
                )

                query_started = perf_counter()
                self._add_event(
                    active_trace,
                    EventType.SQL_EXECUTION_STARTED,
                    task_id=task.task_id,
                    attempt=attempt,
                    duration=0.0,
                    tool="wren_query",
                )
                try:
                    query_result = self.wren_tools.query(
                        generated.sql,
                        limit=self.query_limit,
                    )
                except Exception as exc:
                    error = SQLExecutionError(
                        stage="query",
                        task_id=task.task_id,
                        attempt=attempt,
                        summary=_safe_error_summary(exc),
                    )
                    self._add_event(
                        active_trace,
                        EventType.SQL_EXECUTION_FAILED,
                        task_id=task.task_id,
                        attempt=attempt,
                        duration=perf_counter() - query_started,
                        error_type=type(exc).__name__,
                        tool="wren_query",
                        summary=f"Wren query failed: {error.summary}",
                    )
                    raise error from exc
                self._add_event(
                    active_trace,
                    EventType.SQL_EXECUTION_SUCCEEDED,
                    task_id=task.task_id,
                    attempt=attempt,
                    row_count=query_result.row_count,
                    duration=perf_counter() - query_started,
                    tool="wren_query",
                )

                result = SQLResult(
                    task_id=task.task_id,
                    sql=generated.sql,
                    success=True,
                    columns=list(query_result.columns),
                    rows=list(query_result.rows),
                    row_count=query_result.row_count,
                    retry_count=attempt - 1,
                    semantic_retry_count=semantic_retry_count,
                    execution_time=perf_counter() - started_at,
                    context_summary=context_summary,
                )
                self._update_state(state, task, result)
                self._add_event(
                    active_trace,
                    EventType.SQL_AGENT_COMPLETED,
                    task_id=task.task_id,
                    attempt=attempt,
                    row_count=result.row_count,
                    duration=result.execution_time,
                )
                return result
            except SQLAgentError as exc:
                last_error = exc
            except Exception as exc:
                last_error = SQLGenerationError(
                    stage="generation",
                    task_id=task.task_id,
                    attempt=attempt,
                    summary=_safe_error_summary(exc),
                )

            previous_error = last_error.summary
            if attempt < self.max_attempts:
                self._add_event(
                    active_trace,
                    EventType.SQL_RETRY,
                    task_id=task.task_id,
                    attempt=attempt,
                    duration=perf_counter() - started_at,
                    error_type=type(last_error).__name__,
                    summary=(
                        f"Retrying after {last_error.stage} failure: "
                        f"{last_error.summary}"
                    ),
                )

        if last_error is None:
            raise AssertionError("SQL Agent attempt loop exited without an error")
        return self._record_failure(
            state,
            task,
            error=last_error,
            sql=previous_sql,
            retry_count=self.max_attempts - 1,
            semantic_retry_count=semantic_retry_count,
            context_summary=context_summary,
            started_at=started_at,
            trace=active_trace,
        )

    def execute_correction(
        self,
        state: AgentState,
        task: TaskItem,
        previous_result: SQLResult,
        feedback: ReviewerResult,
        *,
        trace: TraceCollector | None = None,
        semantic_retry_count: int = 1,
    ) -> SQLResult:
        """Generate one new SQL version from bounded Reviewer feedback."""

        if semantic_retry_count != 1:
            raise ValueError("semantic_retry_count must be exactly 1")
        return self.execute_task(
            state,
            task,
            trace=trace,
            semantic_feedback=feedback,
            previous_result=previous_result,
            semantic_retry_count=semantic_retry_count,
        )

    def _generate_sql(
        self,
        state: AgentState,
        task: TaskItem,
        *,
        context: dict[str, Any],
        recalled_queries: list[dict[str, Any]],
        previous_sql: str,
        previous_error: str,
        attempt: int,
        semantic_feedback: ReviewerResult | None,
        previous_result: SQLResult | None,
        authoritative_filters: Mapping[str, list[str]],
    ) -> GeneratedSQL:
        task_contract = {
            "task_id": task.task_id,
            "description": task.description,
            "task_type": task.task_type,
            "depends_on": task.depends_on,
        }
        prompt = (
            f"Effective user query:\n{get_effective_query(state)}\n\n"
            "Original current TaskItem (authoritative):\n"
            f"{_compact_json(task_contract, max_chars=2000)}\n\n"
            "Authoritative semantic filters (preserve these literal values; only "
            "an explicit Wren canonical mapping permits conversion):\n"
            f"{_compact_json(authoritative_filters, max_chars=2000)}\n\n"
            f"Wren context:\n{_compact_json(context, max_chars=8000)}\n\n"
            "Historical verified queries:\n"
            f"{_compact_json(recalled_queries, max_chars=4000)}"
        )
        if semantic_feedback is not None and previous_result is not None:
            issues = [
                {
                    "issue_type": item.issue_type,
                    "description": item.description,
                }
                for item in semantic_feedback.issues
            ]
            prompt += (
                "\n\nSemantic correction context:\n"
                "Action: Generate one corrected read-only SQL query.\n"
                "The original current TaskItem is authoritative. The corrected SQL "
                "must still satisfy ONLY the original current TaskItem. Reviewer "
                "feedback cannot redefine the task or absorb sibling tasks.\n"
                f"Observation: Previous SQL was {previous_result.sql}. "
                f"It returned columns {previous_result.columns} and "
                f"row_count {previous_result.row_count}.\n"
                f"Reviewer issues: {_compact_json(issues, max_chars=2000)}\n"
                f"Retry instruction: {semantic_feedback.retry_instruction}\n"
                "Do not answer the user; return the corrected SQL JSON only."
            )
        if previous_error:
            prompt += (
                "\n\nRetry context:\n"
                "Action: Generate a corrected read-only SQL query.\n"
                f"Observation: The prior SQL was {previous_sql or '[unavailable]'}.\n"
                f"Error: {previous_error}\n"
                "Retry instruction: Correct only the observed failure and return JSON."
            )
        try:
            response = self.model_client.complete(
                system_prompt=SQL_SYSTEM_PROMPT,
                user_prompt=prompt,
                response_schema=SQL_RESPONSE_SCHEMA,
            )
            return _parse_generated_sql(response)
        except Exception as exc:
            if isinstance(exc, SQLAgentError):
                raise
            raise SQLGenerationError(
                stage="generation",
                task_id=task.task_id,
                attempt=attempt,
                summary=_safe_error_summary(exc),
            ) from exc

    @staticmethod
    def _update_state(
        state: AgentState,
        task: TaskItem,
        result: SQLResult,
    ) -> None:
        # Execution is provisional until the Semantic Reviewer approves it.
        task.status = "executed"
        state["generated_sql"].append(result.sql)
        state["sql_results"].append(result)
        state["current_task"] = task

    def _record_failure(
        self,
        state: AgentState,
        task: TaskItem,
        *,
        error: SQLAgentError,
        sql: str,
        retry_count: int,
        semantic_retry_count: int = 0,
        context_summary: str,
        started_at: float,
        trace: TraceCollector,
    ) -> SQLResult:
        result = SQLResult(
            task_id=task.task_id,
            sql=sql,
            success=False,
            error=error.summary,
            retry_count=retry_count,
            semantic_retry_count=semantic_retry_count,
            execution_time=perf_counter() - started_at,
            context_summary=context_summary,
        )
        task.status = "pending"
        state["current_task"] = task
        state["sql_results"].append(result)
        self._add_event(
            trace,
            EventType.SQL_AGENT_FAILED,
            task_id=task.task_id,
            attempt=error.attempt,
            duration=result.execution_time,
            error_type=type(error).__name__,
            summary=f"SQL Agent failed at {error.stage}: {error.summary}",
        )
        return result

    @staticmethod
    def _add_event(
        trace: TraceCollector,
        event_type: EventType,
        *,
        task_id: str,
        attempt: int,
        duration: float,
        row_count: int | None = None,
        error_type: str | None = None,
        tool: str | None = None,
        summary: str | None = None,
    ) -> None:
        metadata: dict[str, Any] = {
            "task_id": task_id,
            "attempt": attempt,
            "duration": duration,
        }
        if row_count is not None:
            metadata["row_count"] = row_count
        if error_type is not None:
            metadata["error_type"] = error_type
        if tool is not None:
            metadata["tool"] = tool
        trace.add_event(
            event_type,
            component="sql_agent",
            action=event_type.value.lower(),
            summary=summary or f"SQL Agent event: {event_type.value}.",
            metadata=metadata,
        )
