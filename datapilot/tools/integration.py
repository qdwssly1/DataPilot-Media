"""Adapters from generic tools into DataPilot's existing query evidence."""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping
from copy import deepcopy
from typing import Any

from datapilot.agent.state import SQLResult, TaskItem
from datapilot.tools.contracts import ToolResult


_SUPERSEDE_DIRECTIVE = "supersedes_tool_evidence_fields"
_MAX_PRESERVED_TOOL_ROWS = 20
_CONTRACT_METADATA_FIELDS = (
    "metric_binding",
    "requested_dimensions",
    "window_role_binding",
)


def task_evidence_metadata(
    task: TaskItem,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach the Planner's query semantics without re-inferring them downstream."""

    output = deepcopy(metadata or {})
    if task.metric_binding:
        existing = output.get("metric_binding")
        binding = dict(existing) if isinstance(existing, dict) else {}
        binding.update(deepcopy(task.metric_binding))
        output["metric_binding"] = binding
    if task.requested_dimensions:
        output["requested_dimensions"] = list(task.requested_dimensions)
    if task.window_role_binding:
        output["window_role_binding"] = deepcopy(task.window_role_binding)
    return output


def _remove_nested_field(payload: MutableMapping[str, Any], path: str) -> bool:
    """Remove one explicitly named dotted field from copied evidence metadata."""

    parts = [part for part in path.split(".") if part]
    if not parts:
        return False
    current: MutableMapping[str, Any] = payload
    for part in parts[:-1]:
        value = current.get(part)
        if not isinstance(value, MutableMapping):
            return False
        current = value
    return current.pop(parts[-1], None) is not None


def merge_correction_evidence(
    base_result: SQLResult,
    correction_result: SQLResult,
    *,
    superseded_fields: Iterable[str] = (),
) -> SQLResult:
    """Preserve valid tool evidence when Reviewer requests a SQL supplement.

    A correction is additive by default.  Only explicitly named dotted metadata
    fields are removed from the preserved tool payload; this prevents a SQL
    retry from silently replacing deterministic summaries such as mixed alarm
    status and bounded samples.
    """

    if base_result.task_id != correction_result.task_id:
        raise ValueError("base and correction results must belong to one task")
    correction_metadata = deepcopy(correction_result.tool_metadata)
    for field_name in _CONTRACT_METADATA_FIELDS:
        original = base_result.tool_metadata.get(field_name)
        if original not in (None, {}, []):
            correction_metadata[field_name] = deepcopy(original)
    correction_result.tool_metadata = correction_metadata
    if base_result.execution_source != "tool" or not base_result.success:
        return correction_result

    directive = correction_metadata.pop(_SUPERSEDE_DIRECTIVE, ())
    requested = [str(item) for item in superseded_fields]
    if isinstance(directive, list):
        requested.extend(str(item) for item in directive)
    explicit_fields = sorted({item.strip() for item in requested if item.strip()})

    preserved = deepcopy(base_result.tool_metadata)
    applied_fields = [
        path for path in explicit_fields if _remove_nested_field(preserved, path)
    ]
    tool_source_id = (
        f"source:{base_result.task_id}:tool:"
        f"{base_result.tool_name or 'unknown'}"
    )
    correction_source_id = (
        f"source:{correction_result.task_id}:sql-correction:"
        f"{correction_result.semantic_retry_count}"
    )
    preserve_rows = (
        base_result.row_count == len(base_result.rows)
        and base_result.row_count <= _MAX_PRESERVED_TOOL_ROWS
    )
    correction_metadata.update(
        {
            "preserved_tool_evidence": preserved,
            "preserved_tool_result": {
                "columns": list(base_result.columns),
                "rows": deepcopy(base_result.rows) if preserve_rows else [],
                "row_count": base_result.row_count,
                "complete": preserve_rows,
                "tool_name": base_result.tool_name,
                "tool_input": deepcopy(base_result.tool_input),
                "context_summary": base_result.context_summary,
            },
            "evidence_continuity": {
                "sources": [
                    {
                        "source_id": tool_source_id,
                        "role": "base_tool_evidence",
                        "execution_source": "tool",
                        "tool_name": base_result.tool_name,
                    },
                    {
                        "source_id": correction_source_id,
                        "role": "correction_evidence",
                        "execution_source": "sql",
                        "semantic_retry_count": (
                            correction_result.semantic_retry_count
                        ),
                    },
                ],
                "preserved_fields": sorted(preserved),
                "superseded_fields": applied_fields,
                "final_review_status": "pending",
            },
        }
    )
    correction_result.tool_metadata = correction_metadata
    return correction_result


def mark_evidence_review_status(result: SQLResult, status: str) -> None:
    """Record the terminal Reviewer decision on a merged evidence lineage."""

    continuity = result.tool_metadata.get("evidence_continuity")
    if isinstance(continuity, MutableMapping):
        continuity["final_review_status"] = status


def tool_result_to_sql_result(
    task: TaskItem,
    result: ToolResult,
) -> SQLResult:
    """Adapt one tool result without creating a parallel Reviewer contract."""

    metadata = task_evidence_metadata(task, dict(result.metadata))
    statement = metadata.pop("executed_query", "")
    raw_columns = metadata.get("columns", [])
    columns = (
        [str(item) for item in raw_columns]
        if isinstance(raw_columns, list)
        else []
    )
    if not columns and result.data:
        columns = list(result.data[0])
    arguments = metadata.pop("arguments", {})
    return SQLResult(
        task_id=task.task_id,
        sql=str(statement),
        success=result.success,
        columns=columns,
        rows=list(result.data),
        row_count=len(result.data),
        error=result.error,
        execution_time=result.execution_time_ms / 1000,
        context_summary=result.summary,
        execution_source="tool",
        tool_name=result.tool_name,
        tool_input=dict(arguments) if isinstance(arguments, dict) else {},
        tool_metadata=metadata,
    )


def serialized_tool_result(result: ToolResult) -> dict[str, Any]:
    """Return the JSON-safe state/trace representation of one tool result."""

    return result.model_dump(mode="json")
