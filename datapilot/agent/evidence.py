"""Deterministic evidence contracts for grounded analysis and responses."""

from __future__ import annotations

import math
import re
import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from datapilot.agent.state import AnalysisResult, SQLResult

_CATEGORICAL_FACT_COLUMNS = (
    "status",
    "severity",
    "error_code",
    "message",
)
_COUNT_HINTS = ("alarm_count", "event_count", "record_count", "count")
_WINDOW_COLUMN_HINTS = ("window_name", "time_window", "window", "period")
_RATE_HINTS = ("rate", "ratio", "percentage", "percent", "pct")
_STATUS_SCOPE_HINTS = (
    "window",
    "period",
    "region",
    "cdn",
    "severity",
    "error_code",
    "type",
    "category",
)
_SCOPE_FIELDS = ("region", "error_code", "severity", "level", "cdn")
_ALL_SCOPE = "ALL"
_BUNDLE_CLAIM_TYPES = (
    "observation",
    "knowledge",
    "correlation",
    "hypothesis",
    "recommendation",
    "causal_claim",
)


class EvidenceProjectionError(ValueError):
    """Raised when canonical evidence cannot be projected within its budget."""


class EvidenceConflictError(ValueError):
    """Raised when one canonical comparison scope has conflicting facts."""


_PROJECTION_KNOWLEDGE_CHARS = 300
_PROJECTION_SAMPLE_CHARS = 120
_COMPARISON_NUMBER_SIGNIFICANT_DIGITS = 15


def lossy_categorical_aggregations(sql: str) -> list[str]:
    """Return categorical fields collapsed with MIN/MAX in *sql*.

    MIN/MAX over a categorical value can be a valid lexical operation, but it
    cannot represent the distribution or shared state of multiple records. The
    Reviewer uses this signal only to protect evidence-bearing query results.
    """

    unquoted = re.sub(r'[`"\[\]]', "", sql)
    columns = "|".join(re.escape(item) for item in _CATEGORICAL_FACT_COLUMNS)
    pattern = re.compile(
        rf"\b(?:min|max)\s*\(\s*(?:[a-z_]\w*\s*\.\s*)?"
        rf"(?P<column>{columns})\s*\)",
        flags=re.IGNORECASE,
    )
    return sorted(
        {match.group("column").lower() for match in pattern.finditer(unquoted)}
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        try:
            number = float(Decimal(value.strip()))
        except (InvalidOperation, ValueError):
            return None
        return number if math.isfinite(number) else None
    return None


def _display_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _canonical_value(value: Any) -> Any:
    """Return a stable JSON-compatible value for signatures and scopes."""

    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        )
    return value


def _combined_tool_input(result: SQLResult) -> dict[str, Any]:
    """Return original Tool arguments plus any correction arguments."""

    combined: dict[str, Any] = {}
    preserved = result.tool_metadata.get("preserved_tool_result")
    if isinstance(preserved, Mapping) and isinstance(
        preserved.get("tool_input"), Mapping
    ):
        combined.update(dict(preserved["tool_input"]))
    combined.update(result.tool_input)
    return combined


def _combined_tool_metadata(result: SQLResult) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    preserved = result.tool_metadata.get("preserved_tool_evidence")
    if isinstance(preserved, Mapping):
        metadata.update(dict(preserved))
    metadata.update(result.tool_metadata)
    return metadata


def _distinct_row_values(result: SQLResult, field: str) -> list[str]:
    column = next(
        (item for item in result.columns if item.casefold() == field.casefold()),
        None,
    )
    if column is None:
        return []
    return sorted(
        {
            str(row[column])
            for row in result.rows
            if row.get(column) is not None
        }
    )


def _group_dimensions(result: SQLResult) -> list[str]:
    arguments = _combined_tool_input(result)
    raw = arguments.get("group_by")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    metadata = _combined_tool_metadata(result)
    raw = metadata.get("dimensions")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def _metric_binding(result: SQLResult) -> dict[str, Any] | None:
    raw = _combined_tool_metadata(result).get("metric_binding")
    if not isinstance(raw, Mapping):
        return None
    primary = raw.get("primary_metric")
    if not isinstance(primary, str) or not primary.strip():
        return None
    supporting = raw.get("supporting_fields", [])
    return {
        "primary_metric": primary.strip(),
        "unit": str(raw.get("unit") or "").strip(),
        "aggregation_semantics": str(
            raw.get("aggregation_semantics") or ""
        ).strip(),
        "supporting_fields": (
            [str(item) for item in supporting]
            if isinstance(supporting, list)
            else []
        ),
    }


def _window_role_binding(result: SQLResult) -> dict[str, Any] | None:
    raw = _combined_tool_metadata(result).get("window_role_binding")
    if not isinstance(raw, Mapping):
        return None
    target = raw.get("comparison_target")
    baselines = raw.get("baseline_windows")
    primary = raw.get("primary_baseline")
    if (
        not isinstance(target, str)
        or not target.strip()
        or not isinstance(baselines, list)
        or not baselines
        or not all(isinstance(item, str) and item.strip() for item in baselines)
    ):
        return None
    normalized_baselines = [str(item).strip() for item in baselines]
    if primary is not None:
        if not isinstance(primary, str) or primary.strip() not in normalized_baselines:
            return None
        primary = primary.strip()
    return {
        "comparison_target": target.strip(),
        "baseline_windows": normalized_baselines,
        "primary_baseline": primary,
    }


def _result_scope(
    result: SQLResult,
    *,
    windows: Sequence[str] | None = None,
    group_dimensions: Sequence[str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build explicit scope without treating a bounded sample as a filter."""

    arguments = _combined_tool_input(result)
    scope: dict[str, Any] = {}
    raw_windows = windows if windows is not None else arguments.get("windows")
    if isinstance(raw_windows, Sequence) and not isinstance(
        raw_windows, (str, bytes)
    ):
        window_values = [str(item) for item in raw_windows]
    elif raw_windows is not None:
        window_values = [str(raw_windows)]
    else:
        window_values = _distinct_row_values(result, "window_name")
    scope["windows"] = window_values or [_ALL_SCOPE]

    for field in _SCOPE_FIELDS:
        value = arguments.get(field)
        if value is not None:
            scope[field] = _canonical_value(value)
            continue
        values = _distinct_row_values(result, field)
        if not values and field == "error_code":
            inferred_codes = sorted(
                {
                    item.upper()
                    for item in re.findall(
                        r"E\d{3,}",
                        " ".join([result.sql, *result.columns]),
                        flags=re.IGNORECASE,
                    )
                }
            )
            values = inferred_codes
        scope[field] = values[0] if len(values) == 1 else _ALL_SCOPE

    dimensions = (
        list(map(str, group_dimensions))
        if group_dimensions is not None
        else _group_dimensions(result)
    )
    scope["group_dimensions"] = dimensions
    if overrides:
        scope.update(
            {str(key): _canonical_value(value) for key, value in overrides.items()}
        )
    return scope


def _scope_fingerprint(
    *,
    filters: Mapping[str, Any],
    group_dimensions: Sequence[str],
    metric: str,
    unit: str,
    aggregation_semantics: str,
) -> str:
    payload = {
        "filters": _canonical_value(filters),
        "group_dimensions": list(map(str, group_dimensions)),
        "metric": metric,
        "unit": unit,
        "aggregation_semantics": aggregation_semantics,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _relative_change(baseline: float, current: float) -> float | None:
    if baseline == 0:
        return None
    return (current - baseline) / abs(baseline)


def _count_column(result: SQLResult) -> str | None:
    lowered = {column: column.lower() for column in result.columns}
    candidates = [
        column
        for column in result.columns
        if lowered[column] == "count" or lowered[column].endswith("_count")
    ]
    for hint in _COUNT_HINTS:
        match = next(
            (column for column in candidates if hint in lowered[column]),
            None,
        )
        if match is not None:
            return match
    return candidates[0] if candidates else None


def _status_scope_columns(result: SQLResult, count_column: str | None) -> list[str]:
    output: list[str] = []
    for column in result.columns:
        lowered = column.lower()
        if lowered == "status" or column == count_column:
            continue
        if lowered.endswith("_id") or lowered in {"timestamp", "message"}:
            continue
        if any(hint in lowered for hint in _STATUS_SCOPE_HINTS):
            output.append(column)
    return output


def _result_source_records(result: SQLResult) -> list[dict[str, Any]]:
    """Return bounded provenance records for one reviewed result."""

    continuity = result.tool_metadata.get("evidence_continuity")
    if isinstance(continuity, Mapping):
        sources = continuity.get("sources")
        if isinstance(sources, list):
            normalized = [
                dict(item) for item in sources if isinstance(item, Mapping)
            ]
            if normalized:
                return normalized
    if result.execution_source == "tool":
        return [
            {
                "source_id": (
                    f"source:{result.task_id}:tool:"
                    f"{result.tool_name or 'unknown'}"
                ),
                "role": "tool_evidence",
                "execution_source": "tool",
                "tool_name": result.tool_name,
            }
        ]
    role = "correction_evidence" if result.semantic_retry_count else "sql_evidence"
    source_kind = "sql-correction" if result.semantic_retry_count else "sql"
    return [
        {
            "source_id": (
                f"source:{result.task_id}:{source_kind}:"
                f"{result.semantic_retry_count}"
            ),
            "role": role,
            "execution_source": "sql",
            "semantic_retry_count": result.semantic_retry_count,
        }
    ]


def _source_ids_for_role(result: SQLResult, role: str | None = None) -> list[str]:
    records = _result_source_records(result)
    if role is not None:
        records = [item for item in records if item.get("role") == role]
    return [
        str(item["source_id"])
        for item in records
        if str(item.get("source_id") or "").strip()
    ]


def _normalized_alarm_summary(result: SQLResult) -> dict[str, Any] | None:
    raw = result.tool_metadata.get("alarm_evidence")
    source_ids = _source_ids_for_role(result, "tool_evidence")
    if not isinstance(raw, Mapping):
        preserved = result.tool_metadata.get("preserved_tool_evidence")
        if isinstance(preserved, Mapping):
            raw = preserved.get("alarm_evidence")
            source_ids = _source_ids_for_role(result, "base_tool_evidence")
    if not isinstance(raw, Mapping):
        return None

    def counts(key: str) -> dict[str, int | float]:
        value = raw.get(key)
        if not isinstance(value, Mapping):
            return {}
        output: dict[str, int | float] = {}
        for item, count in value.items():
            number = _number(count)
            if number is not None and number >= 0:
                output[str(item)] = _display_number(number)
        return dict(sorted(output.items()))

    affected: dict[str, list[str]] = {}
    raw_affected = raw.get("affected_objects")
    if isinstance(raw_affected, Mapping):
        for key, value in raw_affected.items():
            if isinstance(value, list):
                affected[str(key)] = sorted(
                    {str(item) for item in value if item is not None}
                )

    raw_samples = raw.get("sample_events")
    samples = (
        [dict(item) for item in raw_samples[:5] if isinstance(item, Mapping)]
        if isinstance(raw_samples, list)
        else []
    )
    total = _number(raw.get("total_count"))
    return {
        "total_count": (
            _display_number(total) if total is not None else result.row_count
        ),
        "count_by_status": counts("count_by_status"),
        "count_by_severity": counts("count_by_severity"),
        "count_by_error_code": counts("count_by_error_code"),
        "count_by_cdn": counts("count_by_cdn"),
        "count_by_service": counts("count_by_service"),
        "count_by_window": counts("count_by_window"),
        "affected_objects": affected,
        "sample_events": samples,
        "sample_semantics": "bounded_examples_not_distribution",
        "source_evidence_ids": source_ids,
    }


def _singleton_scope(summary: Mapping[str, Any]) -> dict[str, str]:
    scope: dict[str, str] = {}
    for field, key in (
        ("window_name", "count_by_window"),
        ("cdn", "count_by_cdn"),
        ("severity", "count_by_severity"),
        ("error_code", "count_by_error_code"),
        ("service", "count_by_service"),
    ):
        values = summary.get(key)
        if isinstance(values, Mapping) and len(values) == 1:
            scope[field] = str(next(iter(values)))
    regions = summary.get("affected_objects", {}).get("region", [])
    if isinstance(regions, list) and len(regions) == 1:
        scope["region"] = str(regions[0])
    return scope


def _raw_alarm_rows(result: SQLResult) -> list[dict[str, Any]]:
    """Return complete bounded alarm rows, including preserved Tool rows."""

    required = {"status", "error_code"}
    if required <= {column.casefold() for column in result.columns}:
        return [dict(row) for row in result.rows]
    preserved = result.tool_metadata.get("preserved_tool_result")
    if not isinstance(preserved, Mapping) or preserved.get("complete") is not True:
        return []
    columns = {
        str(column).casefold() for column in preserved.get("columns", [])
    }
    rows = preserved.get("rows")
    if not required <= columns or not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _alarm_row_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_evidence_ids: Sequence[str],
) -> dict[str, Any]:
    def distribution(field: str) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            value = row.get(field)
            if value is not None:
                counts[str(value)] += 1
        return dict(sorted(counts.items()))

    affected: dict[str, list[str]] = {}
    for field in ("region", "cdn", "service"):
        values = sorted(
            {str(row[field]) for row in rows if row.get(field) is not None}
        )
        affected[field] = values
    samples = [
        {
            key: row[key]
            for key in (
                "alarm_id",
                "timestamp",
                "window_name",
                "region",
                "cdn",
                "status",
                "severity",
                "error_code",
                "message",
            )
            if row.get(key) is not None
        }
        for row in rows[:3]
    ]
    return {
        "total_count": len(rows),
        "count_by_status": distribution("status"),
        "count_by_severity": distribution("severity"),
        "count_by_error_code": distribution("error_code"),
        "count_by_cdn": distribution("cdn"),
        "count_by_service": distribution("service"),
        "count_by_window": distribution("window_name"),
        "affected_objects": affected,
        "sample_events": samples,
        "sample_semantics": "bounded_examples_not_distribution",
        "source_evidence_ids": list(map(str, source_evidence_ids)),
    }


def _status_distribution_record(
    result: SQLResult,
    summary: Mapping[str, Any],
    *,
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    status_counts = dict(summary.get("count_by_status") or {})
    return {
        "source_task_id": result.task_id,
        "scope": dict(scope),
        "count_column": None,
        "distribution": [
            {"value": value, "count": count}
            for value, count in sorted(status_counts.items())
        ],
        "mixed": len(status_counts) > 1,
        **dict(summary),
    }


def _status_distributions(results: Sequence[SQLResult]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for result in results:
        alarm_summary = _normalized_alarm_summary(result)
        if alarm_summary is not None and alarm_summary["count_by_status"]:
            overall_scope = _result_scope(result)
            output.append(
                _status_distribution_record(
                    result,
                    alarm_summary,
                    scope=overall_scope,
                )
            )

            raw_rows = _raw_alarm_rows(result)
            error_codes = sorted(
                {
                    str(row["error_code"])
                    for row in raw_rows
                    if row.get("error_code") is not None
                }
            )
            if overall_scope.get("error_code") == _ALL_SCOPE:
                for error_code in error_codes:
                    subset = [
                        row
                        for row in raw_rows
                        if str(row.get("error_code")) == error_code
                    ]
                    summary = _alarm_row_summary(
                        subset,
                        source_evidence_ids=alarm_summary.get(
                            "source_evidence_ids", []
                        ),
                    )
                    if summary["count_by_status"]:
                        subset_scope = {
                            **overall_scope,
                            "error_code": error_code,
                        }
                        for field in ("region", "cdn", "severity", "level"):
                            values = sorted(
                                {
                                    str(row[field])
                                    for row in subset
                                    if row.get(field) is not None
                                }
                            )
                            if len(values) == 1:
                                subset_scope[field] = values[0]
                        output.append(
                            _status_distribution_record(
                                result,
                                summary,
                                scope=subset_scope,
                            )
                        )
            continue
        status_column = next(
            (column for column in result.columns if column.lower() == "status"),
            None,
        )
        if status_column is None or not result.rows:
            continue
        count_column = _count_column(result)
        scope_columns = _status_scope_columns(result, count_column)
        grouped: dict[tuple[str, ...], dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        for row in result.rows:
            status = row.get(status_column)
            if status is None:
                continue
            scope = tuple(str(row.get(column)) for column in scope_columns)
            count = _number(row.get(count_column)) if count_column else 1.0
            grouped[scope][str(status)] += 1.0 if count is None else count
        for scope, distribution in grouped.items():
            if not distribution:
                continue
            output.append(
                {
                    "source_task_id": result.task_id,
                    "scope": {
                        **_result_scope(result),
                        **dict(zip(scope_columns, scope, strict=True)),
                    },
                    "count_column": count_column,
                    "distribution": [
                        {"value": value, "count": _display_number(count)}
                        for value, count in sorted(distribution.items())
                    ],
                    "mixed": len(distribution) > 1,
                    "source_evidence_ids": _source_ids_for_role(result),
                }
            )
    return output


def _preferred_window(values: Sequence[str], *, current: bool) -> str | None:
    lowered = {value: value.casefold() for value in values}
    if current:
        exact = ("current_window", "current_period", "current")
        contains = ("current",)
    else:
        exact = (
            "previous_window",
            "baseline_window",
            "baseline",
            "previous_period",
            "previous",
        )
        contains = ("previous_window", "baseline", "previous", "prior")
    for candidate in exact:
        match = next(
            (value for value in values if lowered[value] == candidate),
            None,
        )
        if match is not None:
            return match
    return next(
        (
            value
            for token in contains
            for value in values
            if token in lowered[value]
        ),
        None,
    )


def _window_column(result: SQLResult) -> str | None:
    lowered = {column: column.lower() for column in result.columns}
    for hint in _WINDOW_COLUMN_HINTS:
        match = next(
            (column for column in result.columns if lowered[column] == hint),
            None,
        )
        if match is not None:
            return match
    return next(
        (column for column in result.columns if "window" in lowered[column]),
        None,
    )


def _comparison_dimension(
    result: SQLResult,
    *,
    window_column: str,
    current_window: str,
    baseline_window: str,
) -> str | None:
    relevant = [
        row
        for row in result.rows
        if str(row.get(window_column)) in {current_window, baseline_window}
    ]
    candidates: list[str] = []
    for column in result.columns:
        if column == window_column:
            continue
        values = {
            str(row.get(column))
            for row in relevant
            if row.get(column) is not None
        }
        if len(values) > 1 and any(
            _number(row.get(column)) is None for row in relevant
        ):
            candidates.append(column)
    hints = ("cdn", "region", "device", "category", "type", "name")
    for hint in hints:
        match = next(
            (column for column in candidates if hint in column.lower()),
            None,
        )
        if match is not None:
            return match
    return candidates[0] if candidates else None


def _rate_column(result: SQLResult) -> str | None:
    binding = _metric_binding(result)
    if binding is not None:
        primary = str(binding["primary_metric"])
        if primary not in result.columns:
            return None
        if not all(
            row.get(primary) is None or _number(row.get(primary)) is not None
            for row in result.rows
        ):
            return None
        return primary
    candidates = [
        column
        for column in result.columns
        if any(hint in column.lower() for hint in _RATE_HINTS)
        and all(
            row.get(column) is None or _number(row.get(column)) is not None
            for row in result.rows
        )
    ]
    return candidates[0] if len(candidates) == 1 else None


def _overall_window_rates(
    rows: Sequence[Mapping[str, Any]],
    *,
    window_column: str,
    current_window: str,
    baseline_window: str,
) -> dict[str, Any] | None:
    if not rows:
        return None
    columns = set().union(*(row.keys() for row in rows))
    total_column = next(
        (
            column
            for column in columns
            if column.lower() in {"session_count", "total_sessions", "total_count"}
        ),
        None,
    )
    success_column = next(
        (
            column
            for column in columns
            if "successful" in column.lower() and "session" in column.lower()
        ),
        None,
    )
    if total_column is None or success_column is None:
        return None
    totals: dict[str, tuple[float, float]] = {}
    for window in (baseline_window, current_window):
        selected = [row for row in rows if str(row.get(window_column)) == window]
        total = sum(_number(row.get(total_column)) or 0.0 for row in selected)
        successful = sum(_number(row.get(success_column)) or 0.0 for row in selected)
        if total <= 0:
            return None
        totals[window] = (successful, total)
    baseline_success, baseline_total = totals[baseline_window]
    current_success, current_total = totals[current_window]
    baseline_rate = baseline_success / baseline_total
    current_rate = current_success / current_total
    return {
        "baseline_successful": _display_number(baseline_success),
        "baseline_total": _display_number(baseline_total),
        "baseline_rate": baseline_rate,
        "current_successful": _display_number(current_success),
        "current_total": _display_number(current_total),
        "current_rate": current_rate,
        "delta": current_rate - baseline_rate,
        "relative_change": _relative_change(baseline_rate, current_rate),
    }


def _single_result_totals(result: SQLResult) -> tuple[float, float] | None:
    lowered = {column.lower(): column for column in result.columns}
    total_column = next(
        (
            lowered[name]
            for name in ("session_count", "total_sessions", "total_count")
            if name in lowered
        ),
        None,
    )
    success_column = next(
        (
            column
            for column in result.columns
            if "successful" in column.lower() and "session" in column.lower()
        ),
        None,
    )
    if total_column is None or success_column is None:
        return None
    total = sum(_number(row.get(total_column)) or 0.0 for row in result.rows)
    successful = sum(_number(row.get(success_column)) or 0.0 for row in result.rows)
    return (successful, total) if total > 0 else None


def _available_windows(result: SQLResult) -> list[str]:
    arguments = _combined_tool_input(result)
    raw_windows = arguments.get("windows")
    values: list[str] = []
    if isinstance(raw_windows, list):
        values.extend(str(item) for item in raw_windows)
    window_column = _window_column(result)
    if window_column is not None:
        values.extend(
            str(row[window_column])
            for row in result.rows
            if row.get(window_column) is not None
        )
    return list(dict.fromkeys(values))


def _result_window_roles(result: SQLResult) -> list[tuple[str, str]]:
    available = _available_windows(result)
    binding = _window_role_binding(result)
    if binding is not None:
        roles = [
            ("baseline", window)
            for window in binding["baseline_windows"]
            if window in available
        ]
        target = str(binding["comparison_target"])
        if target in available:
            roles.append(("current", target))
        return roles

    if len(available) == 1:
        window = available[0]
        if _preferred_window([window], current=True) is not None:
            return [("current", window)]
        if _preferred_window([window], current=False) is not None:
            return [("baseline", window)]
    match = re.search(
        r"\bwindow_name\s*=\s*'(?P<window>[^']+)'",
        result.sql,
        flags=re.IGNORECASE,
    )
    window = match.group("window") if match else ""
    searchable = f"{result.task_id} {window}".casefold()
    if "current" in searchable:
        return [("current", window or "current")]
    if any(token in searchable for token in ("previous", "baseline", "prior")):
        return [("baseline", window or "baseline")]
    return []


def _result_window(result: SQLResult) -> tuple[str, str] | None:
    """Compatibility view for legacy single-endpoint comparison sources."""

    roles = _result_window_roles(result)
    return roles[0] if len(roles) == 1 else None


def _result_filters(result: SQLResult) -> dict[str, Any]:
    excluded = {
        "windows",
        "start_time",
        "end_time",
        "group_by",
        "limit",
        "primary_metric",
    }
    return {
        str(key): _canonical_value(value)
        for key, value in _combined_tool_input(result).items()
        if key not in excluded and value is not None
    }


def _comparison_group_dimensions(result: SQLResult) -> list[str]:
    metadata = _combined_tool_metadata(result)
    requested = metadata.get("requested_dimensions")
    dimensions = (
        [str(item) for item in requested]
        if isinstance(requested, list) and requested
        else _group_dimensions(result)
    )
    filters = _result_filters(result)
    return [
        dimension
        for dimension in dimensions
        if dimension.casefold() not in _WINDOW_COLUMN_HINTS
        and "window" not in dimension.casefold()
        and dimension not in filters
    ]


def _rows_for_window(result: SQLResult, window: str) -> list[dict[str, Any]]:
    rows = [dict(row) for row in result.rows]
    window_column = _window_column(result)
    if window_column is not None:
        rows = [row for row in rows if str(row.get(window_column)) == window]
    filters = _result_filters(result)
    for field, expected in filters.items():
        if not any(field in row for row in rows):
            continue
        allowed = expected if isinstance(expected, list) else [expected]
        allowed_values = {str(item) for item in allowed}
        rows = [row for row in rows if str(row.get(field)) in allowed_values]
    return rows


def _paired_dimension(
    left: SQLResult,
    right: SQLResult,
    *,
    left_window: str,
    right_window: str,
) -> str | None:
    left_group_by = _comparison_group_dimensions(left)
    right_group_by = _comparison_group_dimensions(right)
    if left_group_by or right_group_by:
        if left_group_by != right_group_by or len(left_group_by) != 1:
            return None
        dimension = left_group_by[0]
        if dimension not in left.columns or dimension not in right.columns:
            return None
        return dimension
    left_rows = _rows_for_window(left, left_window)
    right_rows = _rows_for_window(right, right_window)
    common = [column for column in left.columns if column in right.columns]
    for hint in ("cdn", "region", "device", "category", "type", "name"):
        match = next(
            (
                column
                for column in common
                if hint in column.lower()
                and len(
                    {
                        str(row.get(column))
                        for row in [*left_rows, *right_rows]
                        if row.get(column) is not None
                    }
                )
                > 1
            ),
            None,
        )
        if match is not None:
            return match
    return None


def _metric_unit(result: SQLResult, metric: str) -> str:
    binding = _metric_binding(result)
    if binding is not None and binding["primary_metric"] == metric:
        declared = str(binding.get("unit") or "")
        if declared:
            return declared
    metadata = _combined_tool_metadata(result)
    units = metadata.get("units")
    if isinstance(units, Mapping) and units.get(metric) is not None:
        return str(units[metric])
    if any(hint in metric.casefold() for hint in _RATE_HINTS):
        return "ratio"
    return "number"


def _aggregation_semantics(result: SQLResult, metric: str) -> str:
    binding = _metric_binding(result)
    if binding is not None and binding["primary_metric"] == metric:
        declared = str(binding.get("aggregation_semantics") or "")
        if declared:
            return declared
    metadata = _combined_tool_metadata(result)
    declared = metadata.get("aggregation_semantics")
    if isinstance(declared, Mapping) and declared.get(metric) is not None:
        return str(declared[metric])
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    lowered = {column.casefold() for column in result.columns}
    if (
        "session_count" in lowered
        and "successful_sessions" in lowered
        and any(hint in metric.casefold() for hint in _RATE_HINTS)
    ):
        return "sum(successful_sessions)/sum(session_count)"
    return f"reported:{metric}"


def _review_approved_for_derivation(result: SQLResult) -> bool:
    if not result.success:
        return False
    explicit = result.tool_metadata.get("review_status")
    if explicit is not None:
        return str(explicit) == "approve"
    continuity = result.tool_metadata.get("evidence_continuity")
    if isinstance(continuity, Mapping):
        return str(continuity.get("final_review_status") or "") == "approve"
    return True


def _pair_spec(
    baseline: SQLResult,
    current: SQLResult,
    *,
    baseline_window: str,
    current_window: str,
) -> dict[str, Any] | None:
    if not (
        _review_approved_for_derivation(baseline)
        and _review_approved_for_derivation(current)
    ):
        return None
    baseline_binding = _metric_binding(baseline)
    current_binding = _metric_binding(current)
    if (baseline_binding is None) != (current_binding is None):
        return None
    if baseline_binding is not None and current_binding is not None:
        binding_fields = (
            "primary_metric",
            "unit",
            "aggregation_semantics",
        )
        if any(
            baseline_binding[field] != current_binding[field]
            for field in binding_fields
        ):
            return None
    baseline_roles = _window_role_binding(baseline)
    current_roles = _window_role_binding(current)
    if (baseline_roles is None) != (current_roles is None):
        return None
    if (
        baseline_roles is not None
        and current_roles is not None
        and baseline_roles != current_roles
    ):
        return None
    dimension = _paired_dimension(
        baseline,
        current,
        left_window=baseline_window,
        right_window=current_window,
    )
    baseline_metric = _rate_column(baseline)
    current_metric = _rate_column(current)
    if (
        dimension is None
        or baseline_metric is None
        or current_metric is None
        or baseline_metric != current_metric
    ):
        return None
    baseline_filters = _result_filters(baseline)
    current_filters = _result_filters(current)
    if baseline_filters != current_filters:
        return None
    baseline_unit = _metric_unit(baseline, baseline_metric)
    current_unit = _metric_unit(current, current_metric)
    if baseline_unit != current_unit:
        return None
    baseline_aggregation = _aggregation_semantics(baseline, baseline_metric)
    current_aggregation = _aggregation_semantics(current, current_metric)
    if baseline_aggregation != current_aggregation:
        return None
    group_dimensions = _comparison_group_dimensions(baseline) or [dimension]
    fingerprint = _scope_fingerprint(
        filters=baseline_filters,
        group_dimensions=group_dimensions,
        metric=baseline_metric,
        unit=baseline_unit,
        aggregation_semantics=baseline_aggregation,
    )
    return {
        "dimension": dimension,
        "group_dimensions": group_dimensions,
        "metric": baseline_metric,
        "unit": baseline_unit,
        "aggregation_semantics": baseline_aggregation,
        "filters": baseline_filters,
        "scope_filter_fingerprint": fingerprint,
    }


def _legacy_paired_comparisons(results: Sequence[SQLResult]) -> list[dict[str, Any]]:
    classified = [
        (result, window)
        for result in results
        if (window := _result_window(result)) is not None
    ]
    currents = [item for item in classified if item[1][0] == "current"]
    baselines = [item for item in classified if item[1][0] == "baseline"]
    output: list[dict[str, Any]] = []
    candidate_pairs: list[
        tuple[SQLResult, str, SQLResult, str, dict[str, Any]]
    ] = []
    for current, (_, current_window) in currents:
        matches: list[tuple[SQLResult, str, dict[str, Any]]] = []
        for baseline, (_, baseline_window) in baselines:
            spec = _pair_spec(baseline, current)
            if spec is not None:
                matches.append((baseline, baseline_window, spec))
        if len(matches) != 1:
            continue
        baseline, baseline_window, spec = matches[0]
        reverse_matches = [
            candidate
            for candidate, _ in currents
            if _pair_spec(baseline, candidate) is not None
        ]
        if len(reverse_matches) != 1:
            continue
        candidate_pairs.append(
            (baseline, baseline_window, current, current_window, spec)
        )

    for baseline, baseline_window, current, current_window, spec in candidate_pairs:
        dimension = str(spec["dimension"])
        current_metric = str(spec["metric"])
        current_values = {
            str(row[dimension]): _number(row.get(current_metric))
            for row in current.rows
            if row.get(dimension) is not None
        }
        baseline_values = {
            str(row[dimension]): _number(row.get(current_metric))
            for row in baseline.rows
            if row.get(dimension) is not None
        }
        if (
            not current_values
            or set(current_values) != set(baseline_values)
            or len(current_values) != len(current.rows)
            or len(baseline_values) != len(baseline.rows)
        ):
            continue
        groups: list[dict[str, Any]] = []
        for group in sorted(current_values):
            current_value = current_values[group]
            baseline_value = baseline_values[group]
            if current_value is None or baseline_value is None:
                continue
            delta = current_value - baseline_value
            classification = (
                "declined"
                if delta < 0
                else "improved"
                if delta > 0
                else "unchanged"
            )
            groups.append(
                {
                    "group": group,
                    "baseline": baseline_value,
                    "current": current_value,
                    "delta": delta,
                    "relative_change": _relative_change(
                        baseline_value,
                        current_value,
                    ),
                    "classification": classification,
                }
            )
        if not groups:
            continue
        declining = [row for row in groups if row["delta"] < 0]
        largest = min(declining, key=lambda row: row["delta"], default=None)
        current_totals = _single_result_totals(current)
        baseline_totals = _single_result_totals(baseline)
        overall = None
        if current_totals is not None and baseline_totals is not None:
            current_success, current_total = current_totals
            baseline_success, baseline_total = baseline_totals
            current_rate = current_success / current_total
            baseline_rate = baseline_success / baseline_total
            overall = {
                "baseline_successful": _display_number(baseline_success),
                "baseline_total": _display_number(baseline_total),
                "baseline_rate": baseline_rate,
                "current_successful": _display_number(current_success),
                "current_total": _display_number(current_total),
                "current_rate": current_rate,
                "delta": current_rate - baseline_rate,
                "relative_change": _relative_change(
                    baseline_rate,
                    current_rate,
                ),
            }
        baseline_sources = _source_ids_for_role(baseline)
        current_sources = _source_ids_for_role(current)
        output.append(
            {
                "source_task_ids": [baseline.task_id, current.task_id],
                "baseline_source_id": (
                    baseline_sources[0] if baseline_sources else None
                ),
                "current_source_id": (
                    current_sources[0] if current_sources else None
                ),
                "source_evidence_ids": list(
                    dict.fromkeys([*baseline_sources, *current_sources])
                ),
                "baseline_window": baseline_window,
                "current_window": current_window,
                "dimension": dimension,
                "metric": current_metric,
                "unit": spec["unit"],
                "aggregation_semantics": spec["aggregation_semantics"],
                "filters": dict(spec["filters"]),
                "scope_filter_fingerprint": spec[
                    "scope_filter_fingerprint"
                ],
                "derivation": "deterministic_cross_task_pair",
                "scope": _result_scope(
                    baseline,
                    windows=[baseline_window, current_window],
                    group_dimensions=spec["group_dimensions"],
                ),
                "groups": groups,
                "declining_groups": [row["group"] for row in declining],
                "largest_decline_group": largest["group"] if largest else None,
                "overall": overall,
            }
        )
    return output


def _legacy_comparisons(results: Sequence[SQLResult]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for result in results:
        if not _review_approved_for_derivation(result):
            continue
        window_column = _window_column(result)
        metric_column = _rate_column(result)
        if window_column is None or metric_column is None:
            continue
        windows = list(
            dict.fromkeys(
                str(row[window_column])
                for row in result.rows
                if row.get(window_column) is not None
            )
        )
        current_window = _preferred_window(windows, current=True)
        baseline_window = _preferred_window(windows, current=False)
        if current_window is None or baseline_window is None:
            continue
        dimension = _comparison_dimension(
            result,
            window_column=window_column,
            current_window=current_window,
            baseline_window=baseline_window,
        )
        if dimension is None:
            continue
        declared_dimensions = [
            item
            for item in _group_dimensions(result)
            if item != window_column
            and item.casefold() not in _WINDOW_COLUMN_HINTS
        ]
        if declared_dimensions and declared_dimensions != [dimension]:
            continue
        by_key: dict[tuple[str, str], float] = {}
        duplicate = False
        for row in result.rows:
            window = str(row.get(window_column))
            if window not in {current_window, baseline_window}:
                continue
            group = row.get(dimension)
            value = _number(row.get(metric_column))
            if group is None or value is None:
                continue
            key = (window, str(group))
            if key in by_key:
                duplicate = True
                break
            by_key[key] = value
        if duplicate:
            continue
        baseline_groups = {
            group for window, group in by_key if window == baseline_window
        }
        current_groups = {
            group for window, group in by_key if window == current_window
        }
        if not baseline_groups or baseline_groups != current_groups:
            continue
        groups = sorted(baseline_groups)
        rows: list[dict[str, Any]] = []
        for group in groups:
            baseline = by_key[(baseline_window, group)]
            current = by_key[(current_window, group)]
            delta = current - baseline
            classification = (
                "declined" if delta < 0 else "improved" if delta > 0 else "unchanged"
            )
            rows.append(
                {
                    "group": group,
                    "baseline": baseline,
                    "current": current,
                    "delta": delta,
                    "relative_change": _relative_change(baseline, current),
                    "classification": classification,
                }
            )
        if not rows:
            continue
        declining = [row for row in rows if row["delta"] < 0]
        largest = min(declining, key=lambda row: row["delta"], default=None)
        filters = _result_filters(result)
        group_dimensions = declared_dimensions or [dimension]
        unit = _metric_unit(result, metric_column)
        aggregation_semantics = _aggregation_semantics(result, metric_column)
        source_ids = _source_ids_for_role(result)
        output.append(
            {
                "source_task_ids": [result.task_id],
                "baseline_source_id": source_ids[0] if source_ids else None,
                "current_source_id": source_ids[0] if source_ids else None,
                "source_evidence_ids": source_ids,
                "window_column": window_column,
                "baseline_window": baseline_window,
                "current_window": current_window,
                "dimension": dimension,
                "metric": metric_column,
                "unit": unit,
                "aggregation_semantics": aggregation_semantics,
                "filters": filters,
                "scope_filter_fingerprint": _scope_fingerprint(
                    filters=filters,
                    group_dimensions=group_dimensions,
                    metric=metric_column,
                    unit=unit,
                    aggregation_semantics=aggregation_semantics,
                ),
                "derivation": "deterministic_single_task_pair",
                "scope": _result_scope(
                    result,
                    windows=[baseline_window, current_window],
                    group_dimensions=group_dimensions,
                ),
                "groups": rows,
                "declining_groups": [row["group"] for row in declining],
                "largest_decline_group": largest["group"] if largest else None,
                "overall": _overall_window_rates(
                    result.rows,
                    window_column=window_column,
                    current_window=current_window,
                    baseline_window=baseline_window,
                ),
            }
        )
    output.extend(_legacy_paired_comparisons(results))
    return output


def _comparison_totals(
    result: SQLResult,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[float, float] | None:
    lowered = {column.lower(): column for column in result.columns}
    total_column = next(
        (
            lowered[name]
            for name in ("session_count", "total_sessions", "total_count")
            if name in lowered
        ),
        None,
    )
    success_column = next(
        (
            column
            for column in result.columns
            if "successful" in column.lower() and "session" in column.lower()
        ),
        None,
    )
    if total_column is None or success_column is None:
        return None
    total = sum(_number(row.get(total_column)) or 0.0 for row in rows)
    successful = sum(_number(row.get(success_column)) or 0.0 for row in rows)
    return (successful, total) if total > 0 else None


def _comparison_primary_baseline(
    baseline: SQLResult,
    current: SQLResult,
) -> str | None:
    left = _window_role_binding(baseline)
    right = _window_role_binding(current)
    bindings = [item for item in (left, right) if item is not None]
    if not bindings:
        return None
    primary_values = {
        str(item["primary_baseline"])
        for item in bindings
        if item.get("primary_baseline") is not None
    }
    return next(iter(primary_values)) if len(primary_values) == 1 else None


def _build_comparison_record(
    baseline: SQLResult,
    current: SQLResult,
    *,
    baseline_window: str,
    current_window: str,
    derivation: str,
    is_primary: bool,
    primary_baseline_explicit: bool,
) -> dict[str, Any] | None:
    spec = _pair_spec(
        baseline,
        current,
        baseline_window=baseline_window,
        current_window=current_window,
    )
    if spec is None:
        return None
    dimension = str(spec["dimension"])
    metric = str(spec["metric"])
    baseline_rows = _rows_for_window(baseline, baseline_window)
    current_rows = _rows_for_window(current, current_window)

    def values(rows: Sequence[Mapping[str, Any]]) -> dict[str, float] | None:
        output: dict[str, float] = {}
        for row in rows:
            group = row.get(dimension)
            value = _number(row.get(metric))
            if group is None or value is None:
                continue
            key = str(group)
            if key in output:
                return None
            output[key] = value
        return output or None

    baseline_values = values(baseline_rows)
    current_values = values(current_rows)
    if (
        baseline_values is None
        or current_values is None
        or set(baseline_values) != set(current_values)
    ):
        return None

    groups: list[dict[str, Any]] = []
    for group in sorted(current_values):
        baseline_value = baseline_values[group]
        current_value = current_values[group]
        delta = current_value - baseline_value
        groups.append(
            {
                "group": group,
                "baseline": baseline_value,
                "current": current_value,
                "delta": delta,
                "relative_change": _relative_change(
                    baseline_value,
                    current_value,
                ),
                "classification": (
                    "declined"
                    if delta < 0
                    else "improved"
                    if delta > 0
                    else "unchanged"
                ),
            }
        )
    declining = [row for row in groups if row["delta"] < 0]
    largest = min(declining, key=lambda row: row["delta"], default=None)

    overall = None
    if any(hint in metric.casefold() for hint in _RATE_HINTS):
        baseline_totals = _comparison_totals(baseline, baseline_rows)
        current_totals = _comparison_totals(current, current_rows)
        if baseline_totals is not None and current_totals is not None:
            baseline_success, baseline_total = baseline_totals
            current_success, current_total = current_totals
            baseline_rate = baseline_success / baseline_total
            current_rate = current_success / current_total
            overall = {
                "baseline_successful": _display_number(baseline_success),
                "baseline_total": _display_number(baseline_total),
                "baseline_rate": baseline_rate,
                "current_successful": _display_number(current_success),
                "current_total": _display_number(current_total),
                "current_rate": current_rate,
                "delta": current_rate - baseline_rate,
                "relative_change": _relative_change(
                    baseline_rate,
                    current_rate,
                ),
            }

    baseline_sources = _source_ids_for_role(baseline)
    current_sources = _source_ids_for_role(current)
    source_task_ids = list(
        dict.fromkeys([baseline.task_id, current.task_id])
    )
    comparison_id = (
        f"{baseline_window}->{current_window}:{metric}:"
        f"{spec['scope_filter_fingerprint'][:12]}"
    )
    return {
        "comparison_id": comparison_id,
        "source_task_ids": source_task_ids,
        "baseline_source_id": baseline_sources[0] if baseline_sources else None,
        "current_source_id": current_sources[0] if current_sources else None,
        "source_evidence_ids": list(
            dict.fromkeys([*baseline_sources, *current_sources])
        ),
        "baseline_window": baseline_window,
        "current_window": current_window,
        "dimension": dimension,
        "metric": metric,
        "unit": spec["unit"],
        "aggregation_semantics": spec["aggregation_semantics"],
        "filters": dict(spec["filters"]),
        "scope_filter_fingerprint": spec["scope_filter_fingerprint"],
        "derivation": derivation,
        "scope": _result_scope(
            baseline,
            windows=[baseline_window, current_window],
            group_dimensions=spec["group_dimensions"],
        ),
        "groups": groups,
        "declining_groups": [row["group"] for row in declining],
        "largest_decline_group": largest["group"] if largest else None,
        "overall": overall,
        "is_primary_comparison": is_primary,
        "primary_baseline_explicit": primary_baseline_explicit,
        "allows_unique_baseline_claims": is_primary,
    }


def _single_result_pairs(result: SQLResult) -> list[tuple[str, str, bool, bool]]:
    roles = _result_window_roles(result)
    currents = [window for role, window in roles if role == "current"]
    baselines = [window for role, window in roles if role == "baseline"]
    if not currents:
        windows = _available_windows(result)
        current = _preferred_window(windows, current=True)
        baseline = _preferred_window(windows, current=False)
        if current is not None and baseline is not None:
            return [(baseline, current, True, False)]
        return []
    if len(currents) != 1 or not baselines:
        return []
    primary = _comparison_primary_baseline(result, result)
    if primary is not None:
        baselines = [primary, *[item for item in baselines if item != primary]]
    unique = len(baselines) == 1
    return [
        (
            baseline,
            currents[0],
            baseline == primary if primary is not None else unique,
            primary is not None,
        )
        for baseline in baselines
    ]


def _paired_comparisons(results: Sequence[SQLResult]) -> list[dict[str, Any]]:
    endpoints = [
        (result, role, window)
        for result in results
        for role, window in _result_window_roles(result)
    ]
    currents = [item for item in endpoints if item[1] == "current"]
    baselines = [item for item in endpoints if item[1] == "baseline"]
    raw_candidates: list[
        tuple[SQLResult, str, SQLResult, str, dict[str, Any]]
    ] = []
    for current, _, current_window in currents:
        for baseline, _, baseline_window in baselines:
            if baseline is current:
                continue
            spec = _pair_spec(
                baseline,
                current,
                baseline_window=baseline_window,
                current_window=current_window,
            )
            if spec is not None:
                raw_candidates.append(
                    (baseline, baseline_window, current, current_window, spec)
                )

    grouped: dict[tuple[str, ...], list[Any]] = defaultdict(list)
    for candidate in raw_candidates:
        baseline, baseline_window, current, current_window, spec = candidate
        key = (
            current.task_id,
            baseline_window,
            current_window,
            str(spec["metric"]),
            str(spec["unit"]),
            str(spec["aggregation_semantics"]),
            str(spec["scope_filter_fingerprint"]),
            str(spec["dimension"]),
        )
        grouped[key].append(candidate)
    candidates = [items[0] for items in grouped.values() if len(items) == 1]

    def ordering(candidate: tuple[Any, ...]) -> tuple[int, int]:
        baseline, baseline_window, current, _, _ = candidate
        primary = _comparison_primary_baseline(baseline, current)
        return (0 if primary == baseline_window else 1, raw_candidates.index(candidate))

    candidates.sort(key=ordering)
    output: list[dict[str, Any]] = []
    for baseline, baseline_window, current, current_window, _ in candidates:
        compatible_baselines = {
            item[1]
            for item in candidates
            if item[2] is current and item[3] == current_window
        }
        primary = _comparison_primary_baseline(baseline, current)
        is_primary = (
            baseline_window == primary
            if primary is not None
            else len(compatible_baselines) == 1
        )
        record = _build_comparison_record(
            baseline,
            current,
            baseline_window=baseline_window,
            current_window=current_window,
            derivation="deterministic_cross_task_pair",
            is_primary=is_primary,
            primary_baseline_explicit=primary is not None,
        )
        if record is not None:
            output.append(record)
    return output


def _comparisons(results: Sequence[SQLResult]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for result in results:
        if not _review_approved_for_derivation(result):
            continue
        for baseline_window, current_window, is_primary, explicit in (
            _single_result_pairs(result)
        ):
            record = _build_comparison_record(
                result,
                result,
                baseline_window=baseline_window,
                current_window=current_window,
                derivation="deterministic_single_task_pair",
                is_primary=is_primary,
                primary_baseline_explicit=explicit,
            )
            if record is not None:
                output.append(record)
    output.extend(_paired_comparisons(results))
    return output


def build_evidence_contract(results: Sequence[SQLResult]) -> dict[str, Any]:
    """Build compact facts and language constraints from approved SQL rows."""

    successful = [result for result in results if result.success]
    return {
        "version": "1.0",
        "source_task_ids": [result.task_id for result in successful],
        "status_distributions": _status_distributions(successful),
        "comparisons": _comparisons(successful),
        "rules": [
            "A categorical distribution must not be restated as one shared state.",
            "A group with a negative delta must not be called healthy or unaffected.",
            "Temporal or dimensional correlation must not be stated as causation.",
            "Do not introduce a data fact that is absent from approved SQL evidence.",
            "Only evidence with compatible region, window, CDN, severity, level, "
            "and error-code scopes may support one claim.",
        ],
    }


def _comparison_sources(comparison: Mapping[str, Any]) -> list[str]:
    sources = comparison.get("source_task_ids")
    if isinstance(sources, list):
        return [str(item) for item in sources]
    source = comparison.get("source_task_id")
    return [str(source)] if source else []


def _format_metric_value(metric: str, value: Any) -> str:
    number = _number(value)
    if number is None:
        return str(value)
    if any(hint in metric.lower() for hint in _RATE_HINTS):
        return f"{number * 100:.2f}%"
    return f"{number:.6g}"


def _format_delta(metric: str, value: Any) -> str:
    number = _number(value)
    if number is None:
        return str(value)
    if any(hint in metric.lower() for hint in _RATE_HINTS):
        return f"{number * 100:.2f} percentage points"
    return f"{number:.6g}"


def _format_relative_change(value: Any) -> str:
    number = _number(value)
    return "undefined" if number is None else f"{number * 100:.2f}%"


def _comparison_number_token(value: Any) -> str | None:
    """Return a deterministic token for a comparison number.

    Python arithmetic can represent the same ratio as both ``0.45`` and
    ``0.44999999999999996``. Fifteen significant digits preserve normal
    double-precision business values while removing that representation noise;
    this is a fixed serialization precision, not a fuzzy comparison tolerance.
    """

    number = _number(value)
    if number is None:
        return None
    if number == 0:
        return "0"
    return format(number, f".{_COMPARISON_NUMBER_SIGNIFICANT_DIGITS}g")


def _canonical_comparison_value(value: Any) -> Any:
    """Return stable comparison-key material without changing stored facts."""

    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, Decimal)):
        return {"number": _comparison_number_token(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_comparison_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        normalized = [_canonical_comparison_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )
    return value


def _comparison_classification(facts: Mapping[str, Any]) -> str:
    declared = str(facts.get("classification") or "").strip()
    if declared:
        return declared
    delta = _number(facts.get("delta"))
    if delta is None:
        return "observed"
    return "declined" if delta < 0 else "improved" if delta > 0 else "unchanged"


def _comparison_identity_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    """Build the value-free identity used to detect conflicting facts."""

    facts_value = item.get("facts")
    facts = facts_value if isinstance(facts_value, Mapping) else {}
    return {
        "evidence_type": str(item.get("kind") or "comparison"),
        "primary_metric": str(facts.get("metric") or ""),
        "unit": str(item.get("unit") or facts.get("unit") or ""),
        "aggregation_semantics": str(
            item.get("aggregation_semantics")
            or facts.get("aggregation_semantics")
            or ""
        ),
        "business_scope": _canonical_comparison_value(
            item.get("scope") or facts.get("scope") or {}
        ),
        "filters": _canonical_comparison_value(facts.get("filters") or {}),
        "group_dimension": facts.get("dimension")
        or item.get("comparison_dimension"),
        "group_value": facts.get("group"),
        "baseline_window": str(facts.get("baseline_window") or ""),
        "current_window": str(facts.get("current_window") or ""),
    }


def _comparison_fact_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    """Build the complete canonical fact key, including normalized values."""

    facts_value = item.get("facts")
    facts = facts_value if isinstance(facts_value, Mapping) else {}
    return {
        **_comparison_identity_payload(item),
        "baseline_value": _comparison_number_token(facts.get("baseline_value")),
        "current_value": _comparison_number_token(facts.get("current_value")),
        "delta": _comparison_number_token(facts.get("delta")),
        "relative_change": _comparison_number_token(
            facts.get("relative_change")
        ),
        "classification": _comparison_classification(facts),
        "is_largest_decline": bool(facts.get("is_largest_decline", False)),
    }


def _comparison_payload_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _comparison_provenance_source(
    comparison: Mapping[str, Any],
    *,
    source_task_ids: Sequence[str],
    source_evidence_ids: Sequence[str],
) -> dict[str, Any]:
    return {
        "derivation_type": str(comparison.get("derivation") or "unknown"),
        "source_task_ids": list(map(str, source_task_ids)),
        "source_evidence_ids": list(map(str, source_evidence_ids)),
        "baseline_source_id": comparison.get("baseline_source_id"),
        "current_source_id": comparison.get("current_source_id"),
        "comparison_id": comparison.get("comparison_id"),
    }


def _ordered_unique_strings(values: Sequence[Any]) -> list[str]:
    return sorted({str(value) for value in values if value is not None})


def _refresh_comparison_provenance(item: dict[str, Any]) -> None:
    raw_sources = item.get("provenance_sources")
    sources = [
        dict(source)
        for source in raw_sources or []
        if isinstance(source, Mapping)
    ]
    unique_sources = {
        _comparison_payload_json(source): source for source in sources
    }
    sources = [unique_sources[key] for key in sorted(unique_sources)]
    source_task_ids = _ordered_unique_strings(
        [
            task_id
            for source in sources
            for task_id in source.get("source_task_ids", [])
        ]
    )
    source_evidence_ids = _ordered_unique_strings(
        [
            evidence_id
            for source in sources
            for evidence_id in source.get("source_evidence_ids", [])
        ]
    )
    baseline_source_ids = _ordered_unique_strings(
        [source.get("baseline_source_id") for source in sources]
    )
    current_source_ids = _ordered_unique_strings(
        [source.get("current_source_id") for source in sources]
    )
    derivation_types = _ordered_unique_strings(
        [source.get("derivation_type") for source in sources]
    )
    comparison_ids = _ordered_unique_strings(
        [source.get("comparison_id") for source in sources]
    )
    derivation = (
        derivation_types[0]
        if len(derivation_types) == 1
        else "canonical_merged"
    )

    facts = dict(item.get("facts") or {})
    previous = facts.get("provenance")
    previous = previous if isinstance(previous, Mapping) else {}
    facts["provenance"] = {
        "source_task_ids": source_task_ids,
        "source_evidence_ids": source_evidence_ids,
        "baseline_source_id": (
            baseline_source_ids[0] if len(baseline_source_ids) == 1 else None
        ),
        "current_source_id": (
            current_source_ids[0] if len(current_source_ids) == 1 else None
        ),
        "baseline_source_ids": baseline_source_ids,
        "current_source_ids": current_source_ids,
        "metric": previous.get("metric") or facts.get("metric"),
        "group_dimension": previous.get("group_dimension")
        or facts.get("dimension"),
        "baseline_window": facts.get("baseline_window"),
        "current_window": facts.get("current_window"),
        "scope_filter_fingerprint": previous.get(
            "scope_filter_fingerprint"
        ),
        "derivation": derivation,
        "derivation_types": derivation_types,
        "comparison_id": comparison_ids[0] if len(comparison_ids) == 1 else None,
        "comparison_ids": comparison_ids,
        "is_primary_comparison": bool(
            facts.get("is_primary_comparison", False)
        ),
        "primary_baseline_explicit": bool(
            previous.get("primary_baseline_explicit", False)
        ),
        "sources": sources,
    }
    item["facts"] = facts
    item["source_task_ids"] = source_task_ids
    item["source_evidence_ids"] = source_evidence_ids
    item["provenance_sources"] = sources
    item["derivation_types"] = derivation_types
    item["derivation"] = derivation


def _comparison_order_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return a provenance-independent ordering/grouping key."""

    facts_value = item.get("facts")
    facts = facts_value if isinstance(facts_value, Mapping) else {}
    dimension = str(
        facts.get("dimension") or item.get("comparison_dimension") or ""
    )
    scope = dict(item.get("scope") or facts.get("scope") or {})
    if dimension:
        scope.pop(dimension, None)
    return {
        "primary_metric": facts.get("metric"),
        "unit": item.get("unit") or facts.get("unit"),
        "aggregation_semantics": item.get("aggregation_semantics")
        or facts.get("aggregation_semantics"),
        "business_scope": _canonical_comparison_value(scope),
        "filters": _canonical_comparison_value(facts.get("filters") or {}),
        "group_dimension": dimension,
        "baseline_window": facts.get("baseline_window"),
        "current_window": facts.get("current_window"),
    }


def _canonicalize_comparison_evidence(
    raw_evidence: Sequence[dict[str, Any]],
    *,
    comparison_record_count: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Merge equal derived facts while preserving every derivation path."""

    by_identity: dict[str, tuple[str, dict[str, Any]]] = {}
    before_by_kind: dict[str, int] = defaultdict(int)
    for candidate in raw_evidence:
        kind = str(candidate.get("kind") or "comparison")
        before_by_kind[kind] += 1
        identity_json = _comparison_payload_json(
            _comparison_identity_payload(candidate)
        )
        fact_json = _comparison_payload_json(_comparison_fact_payload(candidate))
        existing = by_identity.get(identity_json)
        if existing is None:
            item = dict(candidate)
            item["facts"] = dict(candidate.get("facts") or {})
            item["provenance_sources"] = [
                dict(source)
                for source in candidate.get("provenance_sources", [])
                if isinstance(source, Mapping)
            ]
            item["canonical_key"] = hashlib.sha256(
                fact_json.encode("utf-8")
            ).hexdigest()
            by_identity[identity_json] = (fact_json, item)
            continue
        existing_fact_json, item = existing
        if existing_fact_json != fact_json:
            identity_hash = hashlib.sha256(
                identity_json.encode("utf-8")
            ).hexdigest()[:16]
            raise EvidenceConflictError(
                "conflicting canonical comparison facts for identity "
                f"{identity_hash}"
            )
        item["provenance_sources"].extend(
            dict(source)
            for source in candidate.get("provenance_sources", [])
            if isinstance(source, Mapping)
        )
        item_facts = item["facts"]
        candidate_facts = candidate.get("facts") or {}
        item_facts["is_primary_comparison"] = bool(
            item_facts.get("is_primary_comparison", False)
            and candidate_facts.get("is_primary_comparison", False)
        )
        item_facts["allows_unique_baseline_claims"] = bool(
            item_facts.get("allows_unique_baseline_claims", False)
            and candidate_facts.get("allows_unique_baseline_claims", False)
        )

    canonical = [item for _, item in by_identity.values()]
    for item in canonical:
        _refresh_comparison_provenance(item)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in canonical:
        key = _comparison_payload_json(_comparison_order_payload(item))
        grouped[key].append(item)

    ordered: list[dict[str, Any]] = []
    comparison_groups = sorted(
        grouped.items(),
        key=lambda pair: (
            not any(
                bool((item.get("facts") or {}).get("is_primary_comparison"))
                for item in pair[1]
            ),
            pair[0],
        ),
    )
    for comparison_index, (_, entries) in enumerate(comparison_groups, start=1):
        entries.sort(
            key=lambda item: (
                0 if item.get("kind") == "metric_comparison" else 1,
                str((item.get("facts") or {}).get("group") or ""),
            )
        )
        group_index = 0
        for item in entries:
            if item.get("kind") == "metric_comparison":
                evidence_id = f"data:comparison:{comparison_index}:overall"
            else:
                group_index += 1
                evidence_id = (
                    f"data:comparison:{comparison_index}:group:{group_index}"
                )
            item["evidence_id"] = evidence_id
            ordered.append(item)

    after_by_kind: dict[str, int] = defaultdict(int)
    for item in ordered:
        after_by_kind[str(item.get("kind") or "comparison")] += 1
    provenance_count = sum(
        len(item.get("provenance_sources") or []) for item in ordered
    )
    return ordered, {
        "comparison_records_before_dedup": comparison_record_count,
        "comparison_count_before_dedup": len(raw_evidence),
        "canonical_comparison_count": len(ordered),
        "duplicate_comparison_count_removed": len(raw_evidence) - len(ordered),
        "overall_before_dedup": before_by_kind["metric_comparison"],
        "overall_after_dedup": after_by_kind["metric_comparison"],
        "group_before_dedup": before_by_kind["group_comparison"],
        "group_after_dedup": after_by_kind["group_comparison"],
        "provenance_sources_retained": provenance_count,
    }


def _comparison_evidence(
    comparisons: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], set[str], dict[str, int]]:
    raw_evidence: list[dict[str, Any]] = []
    covered: set[str] = set()
    for comparison_index, comparison in enumerate(comparisons, start=1):
        sources = _comparison_sources(comparison)
        covered.update(sources)
        metric = str(comparison.get("metric") or "metric")
        baseline_window = str(comparison.get("baseline_window") or "baseline")
        current_window = str(comparison.get("current_window") or "current")
        filters = dict(comparison.get("filters") or {})
        comparison_scope = dict(comparison.get("scope") or {})
        provenance = {
            "source_task_ids": sources,
            "baseline_source_id": comparison.get("baseline_source_id"),
            "current_source_id": comparison.get("current_source_id"),
            "metric": metric,
            "group_dimension": comparison.get("dimension"),
            "baseline_window": baseline_window,
            "current_window": current_window,
            "scope_filter_fingerprint": comparison.get(
                "scope_filter_fingerprint"
            ),
            "derivation": comparison.get("derivation"),
            "comparison_id": comparison.get("comparison_id"),
            "is_primary_comparison": comparison.get(
                "is_primary_comparison", False
            ),
            "primary_baseline_explicit": comparison.get(
                "primary_baseline_explicit", False
            ),
        }
        source_evidence_ids = list(
            map(str, comparison.get("source_evidence_ids", []))
        )
        provenance_source = _comparison_provenance_source(
            comparison,
            source_task_ids=sources,
            source_evidence_ids=source_evidence_ids,
        )
        unit = str(comparison.get("unit") or "")
        aggregation_semantics = str(
            comparison.get("aggregation_semantics") or ""
        )
        dimension = str(comparison.get("dimension") or "group")
        scope = ", ".join(f"{key}={value}" for key, value in filters.items())
        scope_prefix = f"{scope}; " if scope else ""
        overall = comparison.get("overall")
        if isinstance(overall, Mapping):
            baseline = overall.get("baseline_rate")
            current = overall.get("current_rate")
            delta = overall.get("delta")
            relative = overall.get("relative_change")
            statement = (
                f"{scope_prefix}Overall {metric}: {baseline_window}="
                f"{_format_metric_value(metric, baseline)}, "
                f"{current_window}={_format_metric_value(metric, current)}, "
                f"delta={_format_delta(metric, delta)}, relative_change="
                f"{_format_relative_change(relative)}."
            )
            raw_evidence.append(
                {
                    "evidence_id": f"pending:{comparison_index}:overall",
                    "kind": "metric_comparison",
                    "statement": statement,
                    "facts": {
                        "metric": metric,
                        "filters": filters,
                        "scope": comparison_scope,
                        "baseline_window": baseline_window,
                        "baseline_value": baseline,
                        "current_window": current_window,
                        "current_value": current,
                        "delta": delta,
                        "relative_change": relative,
                        "comparison_id": comparison.get("comparison_id"),
                        "is_primary_comparison": comparison.get(
                            "is_primary_comparison", False
                        ),
                        "allows_unique_baseline_claims": comparison.get(
                            "allows_unique_baseline_claims", False
                        ),
                        "provenance": provenance,
                    },
                    "source_task_ids": sources,
                    "source_evidence_ids": source_evidence_ids,
                    "scope": comparison_scope,
                    "comparison_dimension": dimension,
                    "unit": unit,
                    "aggregation_semantics": aggregation_semantics,
                    "derivation": comparison.get("derivation"),
                    "provenance_sources": [provenance_source],
                    "required": True,
                }
            )
        largest = comparison.get("largest_decline_group")
        for group_index, group in enumerate(
            comparison.get("groups", []),
            start=1,
        ):
            if not isinstance(group, Mapping):
                continue
            name = str(group.get("group"))
            baseline = group.get("baseline")
            current = group.get("current")
            delta = group.get("delta")
            relative = group.get("relative_change")
            classification = str(group.get("classification") or "observed")
            group_scope = {**comparison_scope, dimension: name}
            largest_note = (
                " This is the largest observed decline among paired groups."
                if name == largest
                else ""
            )
            statement = (
                f"{scope_prefix}{dimension}={name}: {metric} {baseline_window}="
                f"{_format_metric_value(metric, baseline)}, "
                f"{current_window}={_format_metric_value(metric, current)}, "
                f"delta={_format_delta(metric, delta)}, relative_change="
                f"{_format_relative_change(relative)}; "
                f"classification={classification}.{largest_note}"
            )
            raw_evidence.append(
                {
                    "evidence_id": (
                        f"pending:{comparison_index}:group:{group_index}"
                    ),
                    "kind": "group_comparison",
                    "statement": statement,
                    "facts": {
                        "dimension": dimension,
                        "group": name,
                        "metric": metric,
                        "filters": filters,
                        "scope": group_scope,
                        "baseline_window": baseline_window,
                        "baseline_value": baseline,
                        "current_window": current_window,
                        "current_value": current,
                        "delta": delta,
                        "relative_change": relative,
                        "classification": classification,
                        "is_largest_decline": name == largest,
                        "comparison_id": comparison.get("comparison_id"),
                        "is_primary_comparison": comparison.get(
                            "is_primary_comparison", False
                        ),
                        "allows_unique_baseline_claims": comparison.get(
                            "allows_unique_baseline_claims", False
                        ),
                        "provenance": provenance,
                    },
                    "source_task_ids": sources,
                    "source_evidence_ids": source_evidence_ids,
                    "scope": group_scope,
                    "comparison_dimension": dimension,
                    "unit": unit,
                    "aggregation_semantics": aggregation_semantics,
                    "derivation": comparison.get("derivation"),
                    "provenance_sources": [provenance_source],
                    "required": True,
                }
            )
    evidence, stats = _canonicalize_comparison_evidence(
        raw_evidence,
        comparison_record_count=len(comparisons),
    )
    return evidence, covered, stats


def _analysis_comparison_evidence(
    analysis_results: Sequence[AnalysisResult],
) -> tuple[list[dict[str, Any]], set[str]]:
    evidence: list[dict[str, Any]] = []
    covered: set[str] = set()
    comparison_index = 0
    for result in analysis_results:
        derived = result.derived_values
        rows = derived.get("comparison")
        labels = derived.get("source_labels")
        if (
            not isinstance(rows, list)
            or not isinstance(labels, list)
            or len(labels) != 2
        ):
            continue
        baseline_label, current_label = map(str, labels)
        metric = str(derived.get("metric_column") or "metric")
        dimension = str(derived.get("dimension_column") or "group")
        largest = derived.get("largest_decline")
        largest_group = (
            str(largest.get("key")) if isinstance(largest, Mapping) else None
        )
        sources = list(map(str, result.source_task_ids))
        covered.update(sources)
        comparison_index += 1
        for group_index, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                continue
            group = str(row.get("key"))
            baseline = row.get(baseline_label)
            current = row.get(current_label)
            delta = row.get("difference")
            relative = row.get("growth_rate")
            delta_number = _number(delta)
            classification = (
                "declined"
                if delta_number is not None and delta_number < 0
                else "improved"
                if delta_number is not None and delta_number > 0
                else "unchanged"
            )
            scope = {
                "windows": [baseline_label, current_label],
                **{field: _ALL_SCOPE for field in _SCOPE_FIELDS},
                "group_dimensions": [dimension],
                dimension: group,
            }
            largest_note = (
                " This is the largest observed decline among paired groups."
                if group == largest_group
                else ""
            )
            statement = (
                f"{dimension}={group}: {metric} {baseline_label}="
                f"{_format_metric_value(metric, baseline)}, "
                f"{current_label}={_format_metric_value(metric, current)}, "
                f"delta={_format_delta(metric, delta)}, relative_change="
                f"{_format_relative_change(relative)}; "
                f"classification={classification}.{largest_note}"
            )
            evidence.append(
                {
                    "evidence_id": (
                        "data:analysis-comparison:"
                        f"{comparison_index}:group:{group_index}"
                    ),
                    "kind": "group_comparison",
                    "statement": statement,
                    "facts": {
                        "dimension": dimension,
                        "group": group,
                        "metric": metric,
                        "baseline_window": baseline_label,
                        "baseline_value": baseline,
                        "current_window": current_label,
                        "current_value": current,
                        "delta": delta,
                        "relative_change": relative,
                        "classification": classification,
                        "is_largest_decline": group == largest_group,
                        "source_analysis_task_id": result.task_id,
                        "scope": scope,
                    },
                    "source_task_ids": sources,
                    "scope": scope,
                    "derivation": "deterministic_analysis_pair",
                    "required": True,
                }
            )
    return evidence, covered


def _status_evidence(
    distributions: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    evidence: list[dict[str, Any]] = []
    covered: set[str] = set()
    for index, distribution in enumerate(distributions, start=1):
        source = str(distribution.get("source_task_id") or "")
        if source:
            covered.add(source)
        values = distribution.get("distribution", [])
        rendered = ", ".join(
            f"{item.get('value')}={item.get('count')}"
            for item in values
            if isinstance(item, Mapping)
        )
        scope = distribution.get("scope")
        scope_text = ", ".join(
            f"{key}={value}" for key, value in dict(scope or {}).items()
        )
        distribution_total = sum(
            _number(item.get("count")) or 0.0
            for item in values
            if isinstance(item, Mapping)
        )
        declared_total = _number(distribution.get("total_count"))
        count = distribution_total if declared_total is None else declared_total
        severity_distribution = dict(
            distribution.get("count_by_severity") or {}
        )
        error_code_distribution = dict(
            distribution.get("count_by_error_code") or {}
        )
        cdn_distribution = dict(distribution.get("count_by_cdn") or {})
        service_distribution = dict(
            distribution.get("count_by_service") or {}
        )
        affected_objects = dict(distribution.get("affected_objects") or {})
        sample_events = [
            dict(item)
            for item in distribution.get("sample_events", [])
            if isinstance(item, Mapping)
        ][:5]

        def render_counts(label: str, counts: Mapping[str, Any]) -> str:
            rendered_counts = ", ".join(
                f"{value}={item_count}"
                for value, item_count in sorted(counts.items())
            )
            return f" {label}: {rendered_counts}." if rendered_counts else ""

        messages = [
            " ".join(str(item.get("message") or "").split())[:240]
            for item in sample_events
            if str(item.get("message") or "").strip()
        ]
        sample_statement = ""
        if messages:
            quoted = " | ".join(f'"{message}"' for message in messages)
            sample_statement = (
                " Bounded message samples (examples only, not distribution or "
                f"all-event facts): {quoted}."
            )
        statement = (
            f"Status distribution ({scope_text or 'reviewed scope'}): "
            f"{rendered}; total={_display_number(count)}."
            f"{render_counts('Severity distribution', severity_distribution)}"
            f"{render_counts('Error-code distribution', error_code_distribution)}"
            f"{render_counts('CDN distribution', cdn_distribution)}"
            f"{render_counts('Service distribution', service_distribution)}"
            f"{sample_statement}"
        )
        evidence.append(
            {
                "evidence_id": f"data:status:{index}",
                "kind": "alarm_status_distribution",
                "statement": statement,
                "facts": {
                    "scope": dict(scope or {}),
                    "distribution": list(values),
                    "mixed": bool(distribution.get("mixed")),
                    "total": _display_number(count),
                    "severity_distribution": severity_distribution,
                    "error_code_distribution": error_code_distribution,
                    "cdn_distribution": cdn_distribution,
                    "service_distribution": service_distribution,
                    "affected_objects": affected_objects,
                    "sample_events": sample_events,
                    "sample_semantics": distribution.get(
                        "sample_semantics",
                        "bounded_examples_not_distribution",
                    ),
                },
                "source_task_ids": [source] if source else [],
                "source_evidence_ids": list(
                    map(str, distribution.get("source_evidence_ids", []))
                ),
                "scope": dict(scope or {}),
                "required": True,
            }
        )
    return evidence, covered


def _column_summary(result: SQLResult) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    excluded = {"message", "timestamp", "trace_id"}
    for column in result.columns:
        lowered = column.lower()
        if lowered in excluded or lowered.endswith("_id"):
            continue
        values = [row.get(column) for row in result.rows if row.get(column) is not None]
        if not values:
            continue
        numbers = [_number(value) for value in values]
        if all(number is not None for number in numbers):
            numeric = [float(number) for number in numbers if number is not None]
            if len(numeric) == 1:
                summaries.append(
                    {
                        "column": column,
                        "kind": "scalar",
                        "value": _display_number(numeric[0]),
                    }
                )
            else:
                summaries.append(
                    {
                        "column": column,
                        "kind": "numeric_range",
                        "minimum": _display_number(min(numeric)),
                        "maximum": _display_number(max(numeric)),
                    }
                )
            continue
        counts: dict[str, int] = defaultdict(int)
        for value in values:
            counts[str(value)] += 1
        if len(counts) <= 8:
            summaries.append(
                {
                    "column": column,
                    "kind": "distribution",
                    "values": [
                        {"value": value, "count": count}
                        for value, count in sorted(counts.items())
                    ],
                }
            )
        if len(summaries) >= 8:
            break
    return summaries


def _summary_statement(
    result: SQLResult,
    summaries: Sequence[Mapping[str, Any]],
) -> str:
    parts: list[str] = []
    for summary in summaries:
        column = str(summary["column"])
        kind = summary["kind"]
        if kind == "scalar":
            parts.append(f"{column}={summary['value']}")
        elif kind == "numeric_range":
            parts.append(
                f"{column} range={summary['minimum']}..{summary['maximum']}"
            )
        else:
            values = ",".join(
                f"{item['value']}:{item['count']}"
                for item in summary.get("values", [])
            )
            parts.append(f"{column} distribution=[{values}]")
    details = "; ".join(parts) or "no compact value summary"
    return (
        f"Reviewed task {result.task_id} returned {result.row_count} rows; "
        f"{details}."
    )


def _preserved_base_tool_result(result: SQLResult) -> SQLResult | None:
    payload = result.tool_metadata.get("preserved_tool_result")
    if not isinstance(payload, Mapping) or payload.get("complete") is not True:
        return None
    rows = payload.get("rows")
    columns = payload.get("columns")
    row_count = payload.get("row_count")
    if not isinstance(rows, list) or not isinstance(columns, list):
        return None
    if not isinstance(row_count, int) or row_count != len(rows):
        return None
    if not all(isinstance(item, Mapping) for item in rows):
        return None
    return SQLResult(
        task_id=result.task_id,
        sql="",
        success=True,
        columns=list(map(str, columns)),
        rows=[dict(item) for item in rows],
        row_count=row_count,
        context_summary=str(payload.get("context_summary") or ""),
        execution_source="tool",
        tool_name=(
            str(payload["tool_name"]) if payload.get("tool_name") else None
        ),
        tool_input=(
            dict(payload["tool_input"])
            if isinstance(payload.get("tool_input"), Mapping)
            else {}
        ),
    )


def _result_evidence(
    results: Sequence[SQLResult],
    *,
    covered: set[str],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for index, result in enumerate(results, start=1):
        continuity = result.tool_metadata.get("evidence_continuity")
        has_correction_lineage = isinstance(continuity, Mapping)
        base_tool_result = _preserved_base_tool_result(result)
        if base_tool_result is not None and result.task_id not in covered:
            base_summaries = _column_summary(base_tool_result)
            base_scope = _result_scope(base_tool_result)
            evidence.append(
                {
                    "evidence_id": f"data:tool-result:{index}",
                    "kind": "reviewed_tool_result_summary",
                    "statement": _summary_statement(
                        base_tool_result,
                        base_summaries,
                    ),
                    "facts": {
                        "row_count": base_tool_result.row_count,
                        "columns": base_summaries,
                        "execution_source": "tool",
                        "tool_name": base_tool_result.tool_name,
                        "evidence_role": "base_tool_evidence",
                        "scope": base_scope,
                    },
                    "source_task_ids": [result.task_id],
                    "source_evidence_ids": _source_ids_for_role(
                        result,
                        "base_tool_evidence",
                    ),
                    "scope": base_scope,
                    "required": True,
                }
            )
        if result.task_id in covered and not has_correction_lineage:
            continue
        summaries = _column_summary(result)
        source_records = _result_source_records(result)
        if has_correction_lineage:
            correction_sources = [
                item
                for item in source_records
                if item.get("role") == "correction_evidence"
            ]
        else:
            correction_sources = source_records
        result_scope = _result_scope(result)
        evidence.append(
            {
                "evidence_id": f"data:result:{index}",
                "kind": "reviewed_result_summary",
                "statement": _summary_statement(result, summaries),
                "facts": {
                    "row_count": result.row_count,
                    "columns": summaries,
                    "execution_source": result.execution_source,
                    "tool_name": result.tool_name,
                    "evidence_role": (
                        "correction_evidence"
                        if has_correction_lineage
                        else source_records[0].get("role")
                    ),
                    "scope": result_scope,
                },
                "source_task_ids": [result.task_id],
                "source_evidence_ids": [
                    str(item["source_id"])
                    for item in correction_sources
                    if item.get("source_id")
                ],
                "scope": result_scope,
                # A normalized comparison is the required claim-facing fact.
                # Keep its corrected source result in the canonical Full Pack
                # for additive lineage, but treat the redundant summary as P1
                # projection evidence so it cannot crowd P0 comparisons out of
                # the bounded Final Claims context.
                "required": result.task_id not in covered,
            }
        )
    return evidence


def _reviewed_evidence_lineage(
    results: Sequence[SQLResult],
) -> list[dict[str, Any]]:
    """Describe how each approved task's evidence survived review/correction."""

    output: list[dict[str, Any]] = []
    for result in results:
        continuity = result.tool_metadata.get("evidence_continuity")
        preserved_fields: list[str] = []
        superseded_fields: list[str] = []
        final_status = "approve"
        if isinstance(continuity, Mapping):
            preserved_fields = list(map(str, continuity.get("preserved_fields", [])))
            superseded_fields = list(
                map(str, continuity.get("superseded_fields", []))
            )
            final_status = str(
                continuity.get("final_review_status") or "approve"
            )
        output.append(
            {
                "task_id": result.task_id,
                "evidence_sources": _result_source_records(result),
                "preserved_fields": preserved_fields,
                "superseded_fields": superseded_fields,
                "final_review_status": final_status,
                "metric_binding": deepcopy_binding
                if (
                    deepcopy_binding := _metric_binding(result)
                ) is not None
                else {},
                "requested_dimensions": _comparison_group_dimensions(result),
                "window_role_binding": (
                    dict(window_binding)
                    if (window_binding := _window_role_binding(result)) is not None
                    else {}
                ),
            }
        )
    return output


def _knowledge_evidence(
    knowledge_items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for item in knowledge_items[:5]:
        chunk_id = str(item.get("chunk_id") or "").strip()
        if not chunk_id:
            continue
        title = " ".join(str(item.get("title") or "Untitled").split())
        text = " ".join(str(item.get("text") or "").split())
        if len(text) > 700:
            excerpt = text[:700]
            boundary = max(
                excerpt.rfind(". "),
                excerpt.rfind("。"),
                excerpt.rfind("; "),
            )
            text = (
                f"{excerpt[: boundary + 1].rstrip()}…"
                if boundary >= 350
                else f"{excerpt.rstrip()}…"
            )
        evidence.append(
            {
                "evidence_id": f"knowledge:{chunk_id}",
                "kind": "retrieved_knowledge",
                "title": title,
                "supported_statement": f"{title}: {text}".strip(),
                "source_category": str(item.get("category") or "unknown"),
                "source_identifiers": [
                    f"knowledge:{chunk_id}",
                    f"source:{item.get('source') or 'unknown'}",
                ],
                "required": False,
            }
        )
    return evidence


def _limitations(
    *,
    data_evidence: Sequence[Mapping[str, Any]],
    knowledge_evidence: Sequence[Mapping[str, Any]],
    results: Sequence[SQLResult],
    has_comparison: bool,
) -> list[dict[str, Any]]:
    source_ids = [result.task_id for result in results]
    evidence_ids = [str(item["evidence_id"]) for item in data_evidence]
    output: list[dict[str, Any]] = []

    def add(identifier: str, statement: str, sources: list[str]) -> None:
        output.append(
            {
                "limitation_id": f"limitation:{identifier}",
                "statement": statement,
                "source_identifiers": sources,
                "required": True,
            }
        )

    if data_evidence and knowledge_evidence:
        add(
            "correlation_not_causation",
            "Temporal and dimensional co-occurrence supports correlation or a "
            "candidate hypothesis, but does not prove causation.",
            [*evidence_ids, "policy:correlation-not-causation"],
        )
    elif knowledge_evidence:
        add(
            "knowledge_not_observed_data",
            "Retrieved knowledge is documentation, not an observed database fact.",
            [
                str(item["evidence_id"])
                for item in knowledge_evidence
            ],
        )
    else:
        add(
            "bounded_evidence",
            "The answer is limited to the reviewed query results in this run.",
            source_ids or ["policy:bounded-evidence"],
        )

    columns = {column.lower() for result in results for column in result.columns}
    media_fields = {"cdn", "error_code", "service"}
    if columns & media_fields and len(source_ids) > 1:
        add(
            "missing_trace_data",
            "No request-level trace join proves that the QoE, alarm, and log "
            "records describe the same requests.",
            source_ids,
        )
        add(
            "missing_origin_metrics",
            "Origin latency, saturation, and timeout telemetry were not included.",
            source_ids,
        )
        add(
            "missing_routing_change_records",
            "Routing, configuration, and deployment change records were not included.",
            source_ids,
        )
    if has_comparison:
        add(
            "limited_time_windows",
            "The paired comparison covers only the reviewed windows; broader "
            "windows or a controlled recovery are still needed.",
            source_ids,
        )
    return output


def build_final_evidence_pack(
    results: Sequence[SQLResult],
    knowledge_items: Sequence[Mapping[str, Any]],
    analysis_results: Sequence[AnalysisResult] = (),
) -> dict[str, Any]:
    """Build the only facts and sources selectable by Final Answer."""

    successful = [result for result in results if result.success]
    contract = build_evidence_contract(successful)
    comparisons = contract["comparisons"]
    (
        comparison_evidence,
        comparison_sources,
        comparison_canonicalization,
    ) = _comparison_evidence(comparisons)
    analysis_evidence, analysis_sources = _analysis_comparison_evidence(
        analysis_results
    )
    status_evidence, status_sources = _status_evidence(
        contract["status_distributions"]
    )
    covered = comparison_sources | analysis_sources | status_sources
    data_evidence = [
        *comparison_evidence,
        *analysis_evidence,
        *status_evidence,
        *_result_evidence(successful, covered=covered),
    ]
    knowledge_evidence = _knowledge_evidence(knowledge_items)
    limitations = _limitations(
        data_evidence=data_evidence,
        knowledge_evidence=knowledge_evidence,
        results=successful,
        has_comparison=bool(comparisons or analysis_evidence),
    )
    return {
        "version": "1.0",
        "reviewed_task_ids": [result.task_id for result in successful],
        "reviewed_evidence": _reviewed_evidence_lineage(successful),
        "data_evidence": data_evidence,
        "comparison_canonicalization": comparison_canonicalization,
        "knowledge_evidence": knowledge_evidence,
        "inference_policy": {
            "allowed_claim_types": [
                "observation",
                "knowledge",
                "correlation",
                "hypothesis",
                "recommendation",
                "causal_claim",
            ],
            "allowed_predicates": [
                "general",
                "stable_control",
            ],
            "allowed_polarities": ["neutral", "positive", "negative"],
            "causal_evidence_ids": [],
            "rule": (
                "causal_claim requires explicit causal evidence; correlation "
                "requires two evidence sources but not necessarily knowledge; "
                "hypothesis requires data plus knowledge or a limitation; "
                "knowledge claims require retrieved knowledge; recommendations "
                "require knowledge plus current data or an explicit limitation; "
                "ordinary claims use general/neutral semantics with no subjects; "
                "stable-control assertions must "
                "identify group-comparison subjects and explicit polarity"
            ),
        },
        "limitations": limitations,
    }


def _projection_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    )


def _projection_scope(item: Mapping[str, Any]) -> dict[str, Any]:
    scope = item.get("scope")
    if not isinstance(scope, Mapping):
        facts = item.get("facts")
        if isinstance(facts, Mapping):
            scope = facts.get("scope")
    if not isinstance(scope, Mapping):
        return {}
    return dict(_canonical_value(scope))


def _project_column_summaries(value: Any) -> dict[str, Any]:
    if not isinstance(value, list):
        return {}
    output: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        column = str(item.get("column") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if not column or not kind:
            continue
        if kind == "distribution":
            output[column] = {
                str(entry.get("value")): entry.get("count")
                for entry in item.get("values", [])
                if isinstance(entry, Mapping)
            }
        elif kind == "numeric_range":
            output[column] = {
                "minimum": item.get("minimum"),
                "maximum": item.get("maximum"),
            }
        elif kind == "scalar":
            output[column] = item.get("value")
    return output


def _project_data_evidence(
    item: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    evidence_id = str(item.get("evidence_id") or "")
    kind = str(item.get("kind") or "unknown")
    facts_value = item.get("facts")
    facts = dict(facts_value) if isinstance(facts_value, Mapping) else {}
    projected_facts: dict[str, Any]
    samples: list[dict[str, Any]] = []

    if kind in {"metric_comparison", "group_comparison"}:
        keys = (
            "dimension",
            "group",
            "metric",
            "baseline_window",
            "baseline_value",
            "current_window",
            "current_value",
            "delta",
            "relative_change",
            "classification",
            "is_largest_decline",
        )
        projected_facts = {key: facts[key] for key in keys if key in facts}
    elif kind == "alarm_status_distribution":
        distribution = {
            str(entry.get("value")): entry.get("count")
            for entry in facts.get("distribution", [])
            if isinstance(entry, Mapping)
        }
        projected_facts = {
            "total": facts.get("total"),
            "status": distribution,
            "mixed": bool(facts.get("mixed")),
            "severity": dict(facts.get("severity_distribution") or {}),
            "error_code": dict(facts.get("error_code_distribution") or {}),
            "cdn": dict(facts.get("cdn_distribution") or {}),
            "service": dict(facts.get("service_distribution") or {}),
            "affected_objects": dict(facts.get("affected_objects") or {}),
            "sample_count": len(facts.get("sample_events") or []),
            "sample_semantics": facts.get(
                "sample_semantics",
                "bounded_examples_not_distribution",
            ),
        }
        for index, sample in enumerate(facts.get("sample_events") or [], start=1):
            if not isinstance(sample, Mapping):
                continue
            sample_id = next(
                (
                    str(sample[key])
                    for key in ("alarm_id", "event_id", "trace_id", "log_id")
                    if sample.get(key)
                ),
                f"sample-{index}",
            )
            message = " ".join(str(sample.get("message") or "").split())
            compact = {
                "sample_id": sample_id,
                **{
                    key: sample[key]
                    for key in ("error_code", "status", "service", "level")
                    if sample.get(key) is not None
                },
            }
            if message:
                compact["snippet"] = message[:_PROJECTION_SAMPLE_CHARS]
            samples.append(compact)
    elif kind in {"reviewed_tool_result_summary", "reviewed_result_summary"}:
        projected_facts = {
            "row_count": facts.get("row_count"),
            "execution_source": facts.get("execution_source"),
            "tool_name": facts.get("tool_name"),
            "evidence_role": facts.get("evidence_role"),
            "columns": _project_column_summaries(facts.get("columns")),
        }
    else:
        statement = " ".join(str(item.get("statement") or "").split())
        projected_facts = {"summary": statement[:320]}

    projected = {
        "id": evidence_id,
        "type": kind,
        "facts": projected_facts,
        "required": bool(item.get("required")),
        "origin": (
            "derived"
            if item.get("derivation") or kind.endswith("comparison")
            else "direct"
        ),
        "source_tasks": list(map(str, item.get("source_task_ids", []))),
    }
    return projected, _projection_scope(item), samples


def _knowledge_signals(pack: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    codes: set[str] = set()
    phrases: set[str] = set()
    for item in pack.get("data_evidence", []):
        if not isinstance(item, Mapping):
            continue
        facts = item.get("facts")
        if not isinstance(facts, Mapping):
            continue
        metric = str(facts.get("metric") or "").strip().lower()
        if metric:
            phrases.add(metric.replace("_", " "))
        for key in ("error_code_distribution", "error_code"):
            value = facts.get(key)
            candidates = value if isinstance(value, Mapping) else [value]
            for candidate in candidates:
                text = str(candidate or "").upper()
                if re.fullmatch(r"E\d{3,}", text):
                    codes.add(text)
    return codes, phrases


def _knowledge_sentences(text: str) -> list[str]:
    normalized = " ".join(text.split())
    return [
        item.strip()
        for item in re.split(r"(?<=[.!?。！？；;])\s+", normalized)
        if item.strip()
    ]


def _project_knowledge_evidence(
    item: Mapping[str, Any],
    *,
    codes: set[str],
    phrases: set[str],
) -> tuple[dict[str, Any], bool]:
    evidence_id = str(item.get("evidence_id") or "")
    title = " ".join(str(item.get("title") or "Untitled").split())
    statement = " ".join(
        str(item.get("supported_statement") or "").split()
    )
    prefix = f"{title}:"
    if statement.casefold().startswith(prefix.casefold()):
        statement = statement[len(prefix) :].strip()
    sentences = _knowledge_sentences(statement) or [statement]

    def score(sentence: str) -> int:
        lowered = sentence.lower()
        return 5 * sum(code.lower() in lowered for code in codes) + sum(
            phrase in lowered for phrase in phrases
        )

    ranked = sorted(
        enumerate(sentences),
        key=lambda pair: (-score(pair[1]), pair[0]),
    )
    relevant = any(score(sentence) > 0 for sentence in sentences)
    chosen_indexes = sorted(index for index, _ in ranked[:2])
    chosen = " ".join(sentences[index] for index in chosen_indexes).strip()
    if len(chosen) > _PROJECTION_KNOWLEDGE_CHARS:
        chosen = f"{chosen[: _PROJECTION_KNOWLEDGE_CHARS - 1].rstrip()}…"
    supported_fact = f"{title}: {chosen}".strip()
    return (
        {
            "id": evidence_id,
            "title": title,
            "supported_fact": supported_fact,
            "category": str(item.get("source_category") or "unknown"),
            "required": bool(item.get("required")),
        },
        relevant,
    )


def _projection_omitted_fields(pack: Mapping[str, Any]) -> list[str]:
    omitted: set[str] = set()
    if pack.get("reviewed_evidence"):
        omitted.add("reviewed_evidence.full_lineage")
    policy = pack.get("inference_policy")
    if isinstance(policy, Mapping) and policy.get("rule"):
        omitted.add("inference_policy.rule")
    for item in pack.get("data_evidence", []):
        if not isinstance(item, Mapping):
            continue
        if item.get("statement"):
            omitted.add("data_evidence.statement")
        if item.get("source_evidence_ids"):
            omitted.add("data_evidence.source_evidence_ids")
        facts = item.get("facts")
        if isinstance(facts, Mapping) and facts.get("provenance"):
            omitted.add("data_evidence.facts.provenance")
        if isinstance(facts, Mapping) and facts.get("sample_events"):
            omitted.add("data_evidence.facts.full_sample_events")
    if pack.get("knowledge_evidence"):
        omitted.update(
            {
                "knowledge_evidence.full_supported_statement",
                "knowledge_evidence.source_identifiers",
            }
        )
    serialized = _projection_json(pack)
    for field in ("raw_rows", "rows", "sql", "dry_plan"):
        if f'"{field}":' in serialized:
            omitted.add(field)
    return sorted(omitted)


def _assemble_evidence_projection(
    pack: Mapping[str, Any],
    data_entries: Sequence[
        tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]
    ],
    knowledge_entries: Sequence[dict[str, Any]],
    limitation_entries: Sequence[dict[str, Any]],
    sample_ids: set[str],
) -> dict[str, Any]:
    scopes: dict[str, dict[str, Any]] = {}
    scope_ids: dict[str, str] = {}
    projected_data: list[dict[str, Any]] = []
    for raw_entry, scope, samples in data_entries:
        entry = dict(raw_entry)
        if scope:
            signature = _projection_json(scope)
            scope_ref = scope_ids.get(signature)
            if scope_ref is None:
                scope_ref = f"S{len(scopes) + 1}"
                scope_ids[signature] = scope_ref
                scopes[scope_ref] = scope
            entry["scope_ref"] = scope_ref
        if entry["id"] in sample_ids and samples:
            entry["samples"] = samples[:3]
        projected_data.append(entry)
    policy = pack.get("inference_policy")
    policy = dict(policy) if isinstance(policy, Mapping) else {}
    projection = {
        "version": "1.0",
        "scopes": scopes,
        "data_evidence": projected_data,
        "knowledge_evidence": list(knowledge_entries),
        "limitations": list(limitation_entries),
        "claim_contract": {
            "allowed_claim_types": list(policy.get("allowed_claim_types", [])),
            "allowed_predicates": list(policy.get("allowed_predicates", [])),
            "allowed_polarities": list(policy.get("allowed_polarities", [])),
            "causal_evidence_ids": list(policy.get("causal_evidence_ids", [])),
        },
    }
    projection["evidence_bundles"] = build_scope_aware_evidence_bundles(
        projection
    )
    return projection


def _projected_entry_scope(
    projection: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    scope_ref = entry.get("scope_ref")
    scopes = projection.get("scopes")
    if not isinstance(scope_ref, str) or not isinstance(scopes, Mapping):
        return {}
    scope = scopes.get(scope_ref)
    return dict(scope) if isinstance(scope, Mapping) else {}


def _bundle_token(value: Any) -> str:
    text = re.sub(r"[^\w.-]+", "_", str(value).strip(), flags=re.UNICODE)
    return text.strip("_") or _ALL_SCOPE


def _scope_scalar(scope: Mapping[str, Any], field: str) -> str:
    value = scope.get(field)
    if value is None:
        return _ALL_SCOPE
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = _ordered_unique([str(item) for item in value])
        return values[0] if len(values) == 1 else _ALL_SCOPE
    return str(value)


def _bundle_windows(
    projection: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
) -> list[str]:
    windows: list[str] = []
    for entry in entries:
        value = _projected_entry_scope(projection, entry).get("windows")
        candidates = (
            value
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes))
            else [value]
        )
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text and text.upper() != _ALL_SCOPE and text not in windows:
                windows.append(text)
    return windows or [_ALL_SCOPE]


def _bundle_knowledge_ids(
    knowledge: Sequence[Mapping[str, Any]],
    *,
    bundle_type: str,
) -> list[str]:
    if bundle_type == "multi_group":
        allowed_categories: set[str] | None = set()
    elif bundle_type == "regional":
        allowed_categories = {"troubleshooting_sop"}
    elif bundle_type == "entity":
        allowed_categories = {"error_code", "troubleshooting_sop"}
    else:
        allowed_categories = None
    return [
        str(item.get("id"))
        for item in knowledge
        if item.get("id")
        and (
            allowed_categories is None
            or str(item.get("category") or "") in allowed_categories
        )
    ]


def _make_evidence_bundle(
    *,
    bundle_id: str,
    bundle_type: str,
    scope: Mapping[str, Any],
    data_entries: Sequence[Mapping[str, Any]],
    knowledge_ids: Sequence[str],
    limitation_ids: Sequence[str],
    supported_claim_types: Sequence[str],
    target_entities: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    return {
        "bundle_id": bundle_id,
        "bundle_type": bundle_type,
        "scope": dict(_canonical_value(scope)),
        "allowed_evidence_ids": _ordered_unique(
            [str(item.get("id") or "") for item in data_entries]
        ),
        "allowed_knowledge_ids": _ordered_unique(list(knowledge_ids)),
        "allowed_limitation_ids": _ordered_unique(list(limitation_ids)),
        "supported_claim_types": _ordered_unique(
            list(map(str, supported_claim_types))
        ),
        "target_entities": {
            str(key): _ordered_unique(list(map(str, values)))
            for key, values in target_entities.items()
            if values
        },
        # The generator/version marker is intentionally compact because bundle
        # metadata is part of the bounded Final Claims context.
        "provenance": "v1",
    }


def _bundle_limitation_ids(
    limitation_ids: Sequence[str],
    *,
    bundle_type: str,
) -> list[str]:
    markers = {
        "regional": (
            "missing_origin_metrics",
            "missing_routing_change_records",
            "limited_time_windows",
        ),
        "entity": (
            "correlation_not_causation",
            "missing_trace_data",
            "missing_origin_metrics",
            "missing_routing_change_records",
        ),
    }.get(bundle_type)
    if markers is None:
        return list(map(str, limitation_ids))
    return [
        str(evidence_id)
        for evidence_id in limitation_ids
        if any(marker in str(evidence_id) for marker in markers)
    ]


def build_scope_aware_evidence_bundles(
    projection: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Partition projected evidence into deterministic, claim-safe bundles.

    Bundles are generated only from projected facts and explicit scopes. Entity
    bundles are created for the largest-decline or directly observed entities;
    no domain entity value such as a particular CDN is hard-coded.
    """

    data = [
        item
        for item in projection.get("data_evidence", [])
        if isinstance(item, Mapping) and item.get("id")
    ]
    knowledge = [
        item
        for item in projection.get("knowledge_evidence", [])
        if isinstance(item, Mapping) and item.get("id")
    ]
    limitation_ids = [
        str(item.get("id"))
        for item in projection.get("limitations", [])
        if isinstance(item, Mapping) and item.get("id")
    ]
    all_knowledge_ids = [str(item.get("id")) for item in knowledge]
    bundles: list[dict[str, Any]] = []
    covered_data_ids: set[str] = set()

    group_comparisons = [
        item for item in data if item.get("type") == "group_comparison"
    ]
    overall_comparisons = [
        item for item in data if item.get("type") == "metric_comparison"
    ]
    comparison_groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in group_comparisons:
        facts = item.get("facts")
        facts = facts if isinstance(facts, Mapping) else {}
        dimension = str(facts.get("dimension") or "").strip()
        if not dimension:
            continue
        scope = _projected_entry_scope(projection, item)
        region = _scope_scalar(scope, "region")
        comparison_groups.setdefault((region, dimension), []).append(item)

    for (region, dimension), comparison_entries in comparison_groups.items():
        entity_values = _ordered_unique(
            [
                str(item.get("facts", {}).get("group") or "")
                for item in comparison_entries
                if item.get("facts", {}).get("group")
            ]
        )
        matching_overall = [
            item
            for item in overall_comparisons
            if _scope_scalar(
                _projected_entry_scope(projection, item), "region"
            )
            == region
        ]
        # Cross-task comparison inputs can remain as required reviewed-result
        # summaries after their normalized comparisons are derived. Keep those
        # region-wide, grouped inputs in the regional bundle instead of
        # creating redundant fallback bundles for baseline/current tasks.
        regional_direct = []
        for item in data:
            if item.get("type") not in {
                "reviewed_result_summary",
                "reviewed_tool_result_summary",
            }:
                continue
            item_scope = _projected_entry_scope(projection, item)
            if _scope_scalar(item_scope, "region") not in {region, _ALL_SCOPE}:
                continue
            if _scope_scalar(item_scope, dimension) != _ALL_SCOPE:
                continue
            group_dimensions = item_scope.get("group_dimensions")
            if not isinstance(group_dimensions, Sequence) or isinstance(
                group_dimensions,
                (str, bytes),
            ):
                continue
            if dimension in map(str, group_dimensions):
                regional_direct.append(item)
        regional_entries = [
            *matching_overall,
            *comparison_entries,
            *regional_direct,
        ]
        regional_knowledge = _bundle_knowledge_ids(
            knowledge,
            bundle_type="regional",
        )
        bundles.append(
            _make_evidence_bundle(
                bundle_id=(
                    f"bundle:regional:{_bundle_token(region)}:"
                    f"{_bundle_token(dimension)}"
                ),
                bundle_type="regional",
                scope={
                    "region": region,
                    dimension: _ALL_SCOPE,
                    "windows": _bundle_windows(projection, regional_entries),
                },
                data_entries=regional_entries,
                knowledge_ids=regional_knowledge,
                limitation_ids=_bundle_limitation_ids(
                    limitation_ids,
                    bundle_type="regional",
                ),
                supported_claim_types=(
                    "observation",
                    "hypothesis",
                    "recommendation",
                ),
                target_entities={dimension: entity_values},
            )
        )
        covered_data_ids.update(
            str(item.get("id")) for item in regional_entries
        )

        if len(entity_values) > 1:
            bundles.append(
                _make_evidence_bundle(
                    bundle_id=(
                        f"bundle:multi_group:{_bundle_token(region)}:"
                        f"{_bundle_token(dimension)}"
                    ),
                    bundle_type="multi_group",
                    scope={
                        "region": region,
                        dimension: _ALL_SCOPE,
                        "windows": _bundle_windows(
                            projection,
                            comparison_entries,
                        ),
                    },
                    data_entries=comparison_entries,
                    knowledge_ids=_bundle_knowledge_ids(
                        knowledge,
                        bundle_type="multi_group",
                    ),
                    limitation_ids=(),
                    supported_claim_types=("observation",),
                    target_entities={dimension: entity_values},
                )
            )

        candidate_entities = {
            str(item.get("facts", {}).get("group"))
            for item in comparison_entries
            if item.get("facts", {}).get("is_largest_decline") is True
        }
        for item in data:
            if item.get("type") in {"group_comparison", "metric_comparison"}:
                continue
            scope = _projected_entry_scope(projection, item)
            if _scope_scalar(scope, "region") not in {region, _ALL_SCOPE}:
                continue
            entity = _scope_scalar(scope, dimension)
            if entity != _ALL_SCOPE:
                candidate_entities.add(entity)

        for entity in sorted(candidate_entities):
            entity_comparisons = [
                item
                for item in comparison_entries
                if str(item.get("facts", {}).get("group") or "") == entity
            ]
            entity_direct = [
                item
                for item in data
                if item.get("type")
                not in {"group_comparison", "metric_comparison"}
                and _scope_scalar(
                    _projected_entry_scope(projection, item), "region"
                )
                in {region, _ALL_SCOPE}
                and _scope_scalar(
                    _projected_entry_scope(projection, item), dimension
                )
                == entity
            ]
            entity_entries = [*entity_comparisons, *entity_direct]
            if not entity_entries:
                continue
            bundles.append(
                _make_evidence_bundle(
                    bundle_id=(
                        f"bundle:entity:{_bundle_token(region)}:"
                        f"{_bundle_token(dimension)}:{_bundle_token(entity)}"
                    ),
                    bundle_type="entity",
                    scope={
                        "region": region,
                        dimension: entity,
                        "windows": _bundle_windows(projection, entity_entries),
                    },
                    data_entries=entity_entries,
                    knowledge_ids=_bundle_knowledge_ids(
                        knowledge,
                        bundle_type="entity",
                    ),
                    limitation_ids=_bundle_limitation_ids(
                        limitation_ids,
                        bundle_type="entity",
                    ),
                    supported_claim_types=(
                        "observation",
                        "correlation",
                        "hypothesis",
                        "recommendation",
                        "causal_claim",
                    ),
                    target_entities={dimension: [entity]},
                )
            )
            covered_data_ids.update(
                str(item.get("id")) for item in entity_entries
            )

    uncovered = [
        item for item in data if str(item.get("id")) not in covered_data_ids
    ]
    scope_groups: dict[str, list[Mapping[str, Any]]] = {}
    for item in uncovered:
        scope = _projected_entry_scope(projection, item)
        scope_groups.setdefault(_projection_json(scope), []).append(item)
    for index, entries in enumerate(scope_groups.values(), start=1):
        scope = _projected_entry_scope(projection, entries[0])
        targets = {
            field: [value]
            for field in ("region", "cdn")
            if (value := _scope_scalar(scope, field)) != _ALL_SCOPE
        }
        bundles.append(
            _make_evidence_bundle(
                bundle_id=f"bundle:scope:{index}",
                bundle_type="scope",
                scope=scope,
                data_entries=entries,
                knowledge_ids=all_knowledge_ids,
                limitation_ids=limitation_ids,
                supported_claim_types=_BUNDLE_CLAIM_TYPES,
                target_entities=targets,
            )
        )

    if knowledge:
        bundles.append(
            _make_evidence_bundle(
                bundle_id="bundle:global:knowledge",
                bundle_type="global_knowledge",
                # An empty scope is the canonical representation of global
                # knowledge; spelling out ALL dimensions is redundant.
                scope={},
                data_entries=(),
                knowledge_ids=all_knowledge_ids,
                limitation_ids=(),
                supported_claim_types=("knowledge",),
                target_entities={},
            )
        )
    return bundles


def _projection_stats(
    pack: Mapping[str, Any],
    projection: Mapping[str, Any],
    *,
    max_chars: int,
) -> dict[str, Any]:
    full_chars = len(_projection_json(pack))
    projected_chars = len(_projection_json(projection))
    samples = [
        item.get("samples", [])
        for item in projection.get("data_evidence", [])
        if isinstance(item, Mapping) and item.get("samples")
    ]
    exposed = {
        str(item.get("id"))
        for key in ("data_evidence", "knowledge_evidence")
        for item in projection.get(key, [])
        if isinstance(item, Mapping)
    }
    exposed.update(
        str(item.get("id"))
        for item in projection.get("limitations", [])
        if isinstance(item, Mapping)
    )
    full_ids = {
        str(item.get("evidence_id"))
        for key in ("data_evidence", "knowledge_evidence")
        for item in pack.get(key, [])
        if isinstance(item, Mapping)
    }
    full_ids.update(
        str(item.get("limitation_id"))
        for item in pack.get("limitations", [])
        if isinstance(item, Mapping)
    )
    canonicalization = pack.get("comparison_canonicalization")
    canonicalization = (
        dict(canonicalization)
        if isinstance(canonicalization, Mapping)
        else {}
    )
    return {
        "full_evidence_pack_chars": full_chars,
        "projected_evidence_chars": projected_chars,
        "max_projection_chars": max_chars,
        "data_chars": len(_projection_json(projection.get("data_evidence", []))),
        "knowledge_chars": len(
            _projection_json(projection.get("knowledge_evidence", []))
        ),
        "limitation_chars": len(
            _projection_json(projection.get("limitations", []))
        ),
        "scope_chars": len(_projection_json(projection.get("scopes", {}))),
        "bundle_chars": len(
            _projection_json(projection.get("evidence_bundles", []))
        ),
        "bundle_metadata_chars": len(
            _projection_json(projection.get("evidence_bundles", []))
        ),
        "sample_chars": len(_projection_json(samples)) if samples else 0,
        "compression_ratio": (
            projected_chars / full_chars if full_chars else 1.0
        ),
        **canonicalization,
        "projection_omitted_fields": _projection_omitted_fields(pack),
        "omitted_evidence_ids": sorted(full_ids - exposed),
    }


def build_final_evidence_projection(
    pack: Mapping[str, Any],
    *,
    max_chars: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build deterministic, bounded model context from the canonical pack.

    Required facts, their scopes, directly relevant knowledge, and required
    limitations are P0. Optional data, bounded samples, secondary knowledge,
    and optional limitations are added in stable order only when they fit.
    """

    if max_chars <= 0:
        raise EvidenceProjectionError("final evidence projection has no budget")
    policy = pack.get("inference_policy")
    policy = dict(policy) if isinstance(policy, Mapping) else {}
    causal_ids = set(map(str, policy.get("causal_evidence_ids", [])))
    all_data = [
        _project_data_evidence(item)
        for item in pack.get("data_evidence", [])
        if isinstance(item, Mapping)
    ]
    required_data = [
        entry
        for entry in all_data
        if entry[0].get("required") or entry[0].get("id") in causal_ids
    ]
    optional_data = [entry for entry in all_data if entry not in required_data]

    codes, phrases = _knowledge_signals(pack)
    all_knowledge: list[tuple[dict[str, Any], bool]] = [
        _project_knowledge_evidence(item, codes=codes, phrases=phrases)
        for item in pack.get("knowledge_evidence", [])
        if isinstance(item, Mapping)
    ]
    required_knowledge = [entry for entry, relevant in all_knowledge if relevant]
    if all_knowledge and not required_knowledge:
        required_knowledge = [all_knowledge[0][0]]
    required_knowledge_ids = {
        str(item["id"]) for item in required_knowledge
    }
    optional_knowledge = [
        entry
        for entry, _ in all_knowledge
        if str(entry["id"]) not in required_knowledge_ids
    ]

    all_limitations = [
        {
            "id": str(item.get("limitation_id") or ""),
            "summary": " ".join(str(item.get("statement") or "").split()),
            "required": bool(item.get("required")),
        }
        for item in pack.get("limitations", [])
        if isinstance(item, Mapping)
    ]
    required_limitations = [
        item for item in all_limitations if item.get("required")
    ]
    optional_limitations = [
        item for item in all_limitations if not item.get("required")
    ]

    selected_data = list(required_data)
    selected_knowledge = list(required_knowledge)
    selected_limitations = list(required_limitations)
    selected_samples: set[str] = set()

    def assemble() -> dict[str, Any]:
        return _assemble_evidence_projection(
            pack,
            selected_data,
            selected_knowledge,
            selected_limitations,
            selected_samples,
        )

    projection = assemble()
    if len(_projection_json(projection)) > max_chars:
        raise EvidenceProjectionError(
            "required P0 evidence exceeds the final evidence projection budget"
        )

    candidates: list[tuple[str, Any]] = [
        *(('data', item) for item in optional_data),
        *(
            ('samples', entry[0]["id"])
            for entry in selected_data
            if entry[2]
        ),
        *(('knowledge', item) for item in optional_knowledge),
        *(('limitation', item) for item in optional_limitations),
    ]
    for kind, candidate in candidates:
        if kind == "data":
            selected_data.append(candidate)
        elif kind == "samples":
            selected_samples.add(str(candidate))
        elif kind == "knowledge":
            selected_knowledge.append(candidate)
        else:
            selected_limitations.append(candidate)
        proposed = assemble()
        if len(_projection_json(proposed)) <= max_chars:
            projection = proposed
            continue
        if kind == "data":
            selected_data.pop()
        elif kind == "samples":
            selected_samples.remove(str(candidate))
        elif kind == "knowledge":
            selected_knowledge.pop()
        else:
            selected_limitations.pop()

    violations = evidence_projection_violations(projection, pack)
    if violations:
        raise EvidenceProjectionError(violations[0])
    return projection, _projection_stats(
        pack,
        projection,
        max_chars=max_chars,
    )


def _projection_visible_ids(projection: Mapping[str, Any]) -> set[str]:
    visible = {
        str(item.get("id"))
        for key in ("data_evidence", "knowledge_evidence", "limitations")
        for item in projection.get(key, [])
        if isinstance(item, Mapping) and item.get("id")
    }
    return visible


def evidence_projection_violations(
    projection: Mapping[str, Any],
    pack: Mapping[str, Any],
) -> list[str]:
    """Return dangling-ID or scope-reference errors in a model projection."""

    violations: list[str] = []
    scopes_value = projection.get("scopes")
    scopes = dict(scopes_value) if isinstance(scopes_value, Mapping) else {}
    known_data = {
        str(item.get("evidence_id")): item
        for item in pack.get("data_evidence", [])
        if isinstance(item, Mapping)
    }
    known_knowledge = {
        str(item.get("evidence_id"))
        for item in pack.get("knowledge_evidence", [])
        if isinstance(item, Mapping)
    }
    known_limitations = {
        str(item.get("limitation_id"))
        for item in pack.get("limitations", [])
        if isinstance(item, Mapping)
    }
    seen: set[str] = set()
    for item in projection.get("data_evidence", []):
        if not isinstance(item, Mapping):
            violations.append("invalid projected data evidence")
            continue
        evidence_id = str(item.get("id") or "")
        if evidence_id in seen:
            violations.append(f"duplicate projected evidence id: {evidence_id}")
        seen.add(evidence_id)
        if evidence_id not in known_data:
            violations.append(f"dangling projected data evidence id: {evidence_id}")
            continue
        scope_ref = item.get("scope_ref")
        if scope_ref is not None:
            if scope_ref not in scopes:
                violations.append(
                    f"dangling projected scope reference: {scope_ref}"
                )
            elif scopes[scope_ref] != _projection_scope(known_data[evidence_id]):
                violations.append(
                    f"projected scope differs from full evidence: {evidence_id}"
                )
    for key, known in (
        ("knowledge_evidence", known_knowledge),
        ("limitations", known_limitations),
    ):
        for item in projection.get(key, []):
            if not isinstance(item, Mapping):
                violations.append(f"invalid projected {key}")
                continue
            evidence_id = str(item.get("id") or "")
            if evidence_id in seen:
                violations.append(
                    f"duplicate projected evidence id: {evidence_id}"
                )
            seen.add(evidence_id)
            if evidence_id not in known:
                violations.append(f"dangling projected evidence id: {evidence_id}")
    causal_ids = set(
        map(
            str,
            projection.get("claim_contract", {}).get(
                "causal_evidence_ids",
                [],
            ),
        )
    )
    if not causal_ids <= seen:
        violations.append("projected causal evidence id is not model-visible")
    bundles = projection.get("evidence_bundles")
    if not isinstance(bundles, list):
        violations.append("projected evidence bundles are missing")
        return list(dict.fromkeys(violations))
    bundle_ids: set[str] = set()
    required_bundle_fields = {
        "bundle_id",
        "bundle_type",
        "scope",
        "allowed_evidence_ids",
        "allowed_knowledge_ids",
        "allowed_limitation_ids",
        "supported_claim_types",
        "target_entities",
        "provenance",
    }
    for bundle in bundles:
        if not isinstance(bundle, Mapping) or not required_bundle_fields <= set(bundle):
            violations.append("invalid projected evidence bundle")
            continue
        bundle_id = str(bundle.get("bundle_id") or "")
        if not bundle_id or bundle_id in bundle_ids:
            violations.append(f"duplicate or empty evidence bundle id: {bundle_id}")
        bundle_ids.add(bundle_id)
        allowed_data = set(map(str, bundle.get("allowed_evidence_ids", [])))
        allowed_knowledge = set(
            map(str, bundle.get("allowed_knowledge_ids", []))
        )
        allowed_limitations = set(
            map(str, bundle.get("allowed_limitation_ids", []))
        )
        if not allowed_data <= set(known_data):
            violations.append(f"bundle {bundle_id} has unknown data evidence")
        if not allowed_knowledge <= known_knowledge:
            violations.append(f"bundle {bundle_id} has unknown knowledge evidence")
        if not allowed_limitations <= known_limitations:
            violations.append(f"bundle {bundle_id} has unknown limitations")
        if not (
            allowed_data | allowed_knowledge | allowed_limitations
        ) <= seen:
            violations.append(f"bundle {bundle_id} exposes non-visible evidence")
        supported_types = set(
            map(str, bundle.get("supported_claim_types", []))
        )
        if not supported_types or not supported_types <= set(_BUNDLE_CLAIM_TYPES):
            violations.append(f"bundle {bundle_id} has invalid claim types")
    return list(dict.fromkeys(violations))


def evidence_projection_claim_violations(
    selection: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> list[str]:
    """Reject claim references that the claims model was not shown."""

    visible = _projection_visible_ids(projection)
    referenced = set(
        map(str, selection.get("data_evidence_ids", []))
    ) | set(map(str, selection.get("knowledge_evidence_ids", [])))
    referenced.update(map(str, selection.get("limitation_ids", [])))
    for claim in selection.get("inferences", []):
        if not isinstance(claim, Mapping):
            continue
        referenced.update(map(str, claim.get("supporting_evidence_ids", [])))
        referenced.update(map(str, claim.get("subject_evidence_ids", [])))
    omitted = sorted(referenced - visible)
    if not omitted:
        return []
    return [f"claim references evidence omitted from projection: {omitted}"]


def evidence_bundle_claim_violations(
    selection: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> list[str]:
    """Reject claims that escape their deterministic evidence bundle."""

    bundles = {
        str(item.get("bundle_id")): item
        for item in projection.get("evidence_bundles", [])
        if isinstance(item, Mapping) and item.get("bundle_id")
    }
    violations: list[str] = []
    for index, claim in enumerate(selection.get("inferences", []), start=1):
        if not isinstance(claim, Mapping):
            continue
        bundle_id = str(claim.get("bundle_id") or "")
        bundle = bundles.get(bundle_id)
        if bundle is None:
            violations.append(f"claim {index} references unknown evidence bundle")
            continue
        claim_type = str(claim.get("claim_type") or "")
        supported = set(map(str, bundle.get("supported_claim_types", [])))
        if claim_type not in supported:
            violations.append(
                f"claim {index} type {claim_type} is not supported by bundle "
                f"{bundle_id}"
            )
        allowed_data = set(map(str, bundle.get("allowed_evidence_ids", [])))
        allowed = (
            allowed_data
            | set(map(str, bundle.get("allowed_knowledge_ids", [])))
            | set(map(str, bundle.get("allowed_limitation_ids", [])))
        )
        support = set(map(str, claim.get("supporting_evidence_ids", [])))
        subjects = set(map(str, claim.get("subject_evidence_ids", [])))
        escaped = sorted(support - allowed)
        if escaped:
            violations.append(
                f"claim {index} cites evidence outside bundle {bundle_id}: {escaped}"
            )
        escaped_subjects = sorted(subjects - allowed_data)
        if escaped_subjects:
            violations.append(
                f"claim {index} cites subjects outside bundle {bundle_id}: "
                f"{escaped_subjects}"
            )
    return list(dict.fromkeys(violations))


def _pack_evidence_by_id(pack: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    output: dict[str, Mapping[str, Any]] = {}
    for key in ("data_evidence", "knowledge_evidence"):
        for item in pack.get(key, []):
            output[str(item["evidence_id"])] = item
    return output


def evidence_pack_source_violations(pack: Mapping[str, Any]) -> list[str]:
    """Return provenance errors in a Final Evidence Pack."""

    violations: list[str] = []
    reviewed = set(map(str, pack.get("reviewed_task_ids", [])))
    known_ids = set(_pack_evidence_by_id(pack))
    lineage_source_ids: set[str] = set()
    for item in pack.get("reviewed_evidence", []):
        task_id = str(item.get("task_id") or "")
        sources = item.get("evidence_sources", [])
        if task_id not in reviewed or not isinstance(sources, list) or not sources:
            violations.append(f"untraceable reviewed evidence: {task_id or 'unknown'}")
            continue
        for source in sources:
            if not isinstance(source, Mapping):
                violations.append(f"invalid reviewed evidence source: {task_id}")
                continue
            source_id = str(source.get("source_id") or "")
            if not source_id.startswith("source:"):
                violations.append(f"invalid reviewed evidence source: {task_id}")
                continue
            lineage_source_ids.add(source_id)
    for item in pack.get("data_evidence", []):
        sources = set(map(str, item.get("source_task_ids", [])))
        if not sources or not sources <= reviewed:
            violations.append(
                f"untraceable data evidence: {item.get('evidence_id')}"
            )
        evidence_sources = set(
            map(str, item.get("source_evidence_ids", []))
        )
        if evidence_sources and not evidence_sources <= lineage_source_ids:
            violations.append(
                f"untraceable data evidence source: {item.get('evidence_id')}"
            )
    for item in pack.get("knowledge_evidence", []):
        sources = list(map(str, item.get("source_identifiers", [])))
        if str(item.get("evidence_id")) not in sources:
            violations.append(
                f"untraceable knowledge evidence: {item.get('evidence_id')}"
            )
    allowed_limit_sources = known_ids | reviewed
    for item in pack.get("limitations", []):
        sources = list(map(str, item.get("source_identifiers", [])))
        if not sources or any(
            source not in allowed_limit_sources
            and not source.startswith(("policy:", "source:"))
            for source in sources
        ):
            violations.append(
                f"untraceable limitation: {item.get('limitation_id')}"
            )
    return violations


def _selected_ids(selection: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    data_ids = [str(item) for item in selection.get("data_evidence_ids", [])]
    knowledge_ids = [
        str(item) for item in selection.get("knowledge_evidence_ids", [])
    ]
    return data_ids, knowledge_ids


def _generalizes_bounded_alarm_samples(
    text: str,
    sample_events: Sequence[Mapping[str, Any]],
) -> bool:
    if not sample_events or _has_explicit_negation(text):
        return False
    lowered = text.casefold()
    universal = bool(
        re.search(r"\b(?:all|every)\b|所有|全部|每(?:条|个)?|均|都", lowered)
    )
    if not universal:
        return False
    alarm_subject = bool(
        re.search(r"\b(?:alarm|event)\w*\b|告警|事件", lowered)
    )
    sample_predicate = bool(
        re.search(
            r"\b(?:message|report|show|indicat|contain|share|display)\w*\b|"
            r"消息|报告|显示|表明|包含|相同",
            lowered,
        )
    )
    return alarm_subject and sample_predicate


def _claim_semantic_violations(
    claim: Mapping[str, Any],
    *,
    index: int,
    evidence_by_id: Mapping[str, Mapping[str, Any]],
    selected: set[str],
    support: set[str],
    allowed_predicates: set[str],
    allowed_polarities: set[str],
) -> list[str]:
    """Validate claim polarity against structured, traceable subject evidence."""

    violations: list[str] = []
    predicate = str(claim.get("predicate") or "")
    polarity = str(claim.get("polarity") or "")
    subjects = set(map(str, claim.get("subject_evidence_ids", [])))
    if predicate not in allowed_predicates:
        return [f"claim {index} has invalid predicate"]
    if polarity not in allowed_polarities:
        return [f"claim {index} has invalid polarity"]
    if subjects - selected:
        violations.append(f"claim {index} has unselected subject evidence")
    if subjects - support:
        violations.append(f"claim {index} has subject evidence not cited as support")

    if predicate == "general":
        if polarity != "neutral":
            violations.append(
                f"claim {index} {predicate} predicate requires neutral polarity"
            )
        if subjects:
            violations.append(
                f"claim {index} {predicate} predicate cannot identify subjects"
            )
        return violations

    if predicate != "stable_control":
        return violations
    if str(claim.get("claim_type") or "") != "observation":
        violations.append(
            f"claim {index} stable_control must be an observation"
        )
    if polarity not in {"positive", "negative"}:
        violations.append(
            f"claim {index} stable_control requires positive or negative polarity"
        )
    if not subjects:
        violations.append(
            f"claim {index} stable_control requires subject evidence"
        )
        return violations

    for evidence_id in sorted(subjects):
        item = evidence_by_id.get(evidence_id)
        if item is None:
            continue
        if item.get("kind") != "group_comparison":
            violations.append(
                f"claim {index} stable_control subject is not group comparison evidence"
            )
            continue
        facts = item.get("facts", {})
        classification = str(facts.get("classification") or "")
        group = str(facts.get("group") or evidence_id)
        if polarity == "positive" and classification == "declined":
            violations.append(
                f"claim {index} positive stable_control contradicts declining group "
                f"{group}"
            )
        if polarity == "negative" and classification != "declined":
            violations.append(
                f"claim {index} negative stable_control lacks declining evidence for "
                f"{group}"
            )
    return violations


def _scope_values(scope: Mapping[str, Any], field: str) -> set[str] | None:
    value = scope.get(field)
    if value is None or str(value).upper() == _ALL_SCOPE:
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = {str(item) for item in value if str(item).upper() != _ALL_SCOPE}
        return values or None
    return {str(value)}


def _evidence_scope(item: Mapping[str, Any]) -> dict[str, Any]:
    scope = item.get("scope")
    if isinstance(scope, Mapping):
        return dict(scope)
    facts = item.get("facts")
    if isinstance(facts, Mapping) and isinstance(facts.get("scope"), Mapping):
        return dict(facts["scope"])
    return {}


def _claim_scope_violations(
    claim: Mapping[str, Any],
    *,
    index: int,
    supporting_data: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Reject incompatible evidence scopes without parsing model-authored text."""

    violations: list[str] = []
    scoped = [item for item in supporting_data if _evidence_scope(item)]
    for left_index, left in enumerate(scoped):
        left_scope = _evidence_scope(left)
        for right in scoped[left_index + 1 :]:
            right_scope = _evidence_scope(right)
            for field in (
                "region",
                "windows",
                "error_code",
                "cdn",
                "severity",
                "level",
            ):
                if (
                    field == "cdn"
                    and left.get("kind") == "group_comparison"
                    and right.get("kind") == "group_comparison"
                ):
                    continue
                left_values = _scope_values(left_scope, field)
                right_values = _scope_values(right_scope, field)
                if (
                    left_values is not None
                    and right_values is not None
                    and left_values.isdisjoint(right_values)
                ):
                    violations.append(
                        f"claim {index} combines incompatible {field} scopes"
                    )
    return list(dict.fromkeys(violations))


def _comparison_conclusion(item: Mapping[str, Any]) -> tuple[str, bool | None]:
    """Return deterministic direction/largest semantics for one comparison."""

    facts = item.get("facts")
    if not isinstance(facts, Mapping):
        return "unknown", None
    classification = str(facts.get("classification") or "")
    if not classification:
        delta = _number(facts.get("delta"))
        if delta is None:
            classification = "unknown"
        elif delta < 0:
            classification = "declined"
        elif delta > 0:
            classification = "improved"
        else:
            classification = "unchanged"
    largest = facts.get("is_largest_decline")
    return classification, largest if isinstance(largest, bool) else None


def _multi_baseline_claim_violations(
    supporting_data: Sequence[Mapping[str, Any]],
    *,
    index: int,
) -> list[str]:
    """Allow cross-baseline summaries only for identical structured conclusions."""

    comparisons = [
        item
        for item in supporting_data
        if item.get("kind") in {"metric_comparison", "group_comparison"}
    ]
    by_subject: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for item in comparisons:
        facts = item.get("facts")
        if not isinstance(facts, Mapping):
            continue
        subject = (
            str(item.get("kind") or ""),
            str(facts.get("metric") or ""),
            str(facts.get("group") or "__overall__"),
        )
        by_subject.setdefault(subject, []).append(item)

    violations: list[str] = []
    for (_, _, subject), items in by_subject.items():
        baselines = {
            str(item.get("facts", {}).get("baseline_window") or "")
            for item in items
        }
        if len(baselines) <= 1:
            continue
        conclusions = {_comparison_conclusion(item) for item in items}
        if len(conclusions) > 1:
            violations.append(
                f"claim {index} has inconsistent multi-baseline conclusions "
                f"for {subject}"
            )
    return violations


def final_claim_violations(
    selection: Mapping[str, Any],
    pack: Mapping[str, Any],
) -> list[str]:
    """Validate selected claims against the deterministic Evidence Pack."""

    violations = evidence_pack_source_violations(pack)
    evidence_by_id = _pack_evidence_by_id(pack)
    data_ids, knowledge_ids = _selected_ids(selection)
    selected = set(data_ids + knowledge_ids)
    known_data = {
        str(item["evidence_id"]) for item in pack.get("data_evidence", [])
    }
    known_knowledge = {
        str(item["evidence_id"])
        for item in pack.get("knowledge_evidence", [])
    }
    unknown_data = set(data_ids) - known_data
    unknown_knowledge = set(knowledge_ids) - known_knowledge
    if unknown_data:
        violations.append(f"unknown data evidence ids: {sorted(unknown_data)}")
    if unknown_knowledge:
        violations.append(
            f"unknown knowledge evidence ids: {sorted(unknown_knowledge)}"
        )
    required_data = {
        str(item["evidence_id"])
        for item in pack.get("data_evidence", [])
        if item.get("required")
    }
    missing_data = required_data - set(data_ids)
    if missing_data:
        violations.append(f"required data evidence omitted: {sorted(missing_data)}")
    if known_knowledge and not knowledge_ids:
        violations.append("retrieved knowledge evidence was not selected")

    limitation_ids = [str(item) for item in selection.get("limitation_ids", [])]
    known_limitations = {
        str(item["limitation_id"]) for item in pack.get("limitations", [])
    }
    unknown_limitations = set(limitation_ids) - known_limitations
    if unknown_limitations:
        violations.append(
            f"unknown limitation ids: {sorted(unknown_limitations)}"
        )
    required_limitations = {
        str(item["limitation_id"])
        for item in pack.get("limitations", [])
        if item.get("required")
    }
    missing_limitations = required_limitations - set(limitation_ids)
    if missing_limitations:
        violations.append(
            f"required limitations omitted: {sorted(missing_limitations)}"
        )
    selected_limitations = set(limitation_ids)
    selected_sources = selected | selected_limitations

    allowed_types = set(
        map(str, pack.get("inference_policy", {}).get("allowed_claim_types", []))
    )
    allowed_predicates = set(
        map(str, pack.get("inference_policy", {}).get("allowed_predicates", []))
    )
    allowed_polarities = set(
        map(str, pack.get("inference_policy", {}).get("allowed_polarities", []))
    )
    causal_ids = set(
        map(str, pack.get("inference_policy", {}).get("causal_evidence_ids", []))
    )
    for index, claim in enumerate(selection.get("inferences", []), start=1):
        claim_type = str(claim.get("claim_type") or "")
        support = set(map(str, claim.get("supporting_evidence_ids", [])))
        if claim_type not in allowed_types:
            violations.append(f"claim {index} has invalid claim_type")
            continue
        if not support or not support <= selected_sources:
            violations.append(f"claim {index} cites unselected evidence or limitation")
        evidence_support = support & selected
        data_support = support & known_data
        knowledge_support = support & known_knowledge
        limitation_support = support & selected_limitations
        violations.extend(
            _claim_semantic_violations(
                claim,
                index=index,
                evidence_by_id=evidence_by_id,
                selected=selected,
                support=support,
                allowed_predicates=allowed_predicates,
                allowed_polarities=allowed_polarities,
            )
        )
        if claim_type == "observation" and not data_support:
            violations.append(f"claim {index} observation lacks data evidence")
        if claim_type == "knowledge" and not knowledge_support:
            violations.append(f"claim {index} knowledge lacks knowledge evidence")
        if claim_type == "correlation":
            if len(evidence_support) < 2:
                violations.append(
                    f"claim {index} correlation requires two evidence sources"
                )
            if not data_support:
                violations.append(f"claim {index} lacks data evidence")
        if claim_type == "hypothesis":
            if not data_support:
                violations.append(f"claim {index} lacks data evidence")
            if not (knowledge_support or limitation_support):
                violations.append(
                    f"claim {index} hypothesis requires knowledge or limitation support"
                )
        if claim_type == "recommendation":
            if not knowledge_support:
                violations.append(
                    f"claim {index} recommendation lacks knowledge evidence"
                )
            if not (data_support or limitation_support):
                violations.append(
                    f"claim {index} recommendation lacks current-event grounding"
                )
        if claim_type == "causal_claim" and (
            not causal_ids or not support <= causal_ids
        ):
            violations.append(f"claim {index} lacks approved causal evidence")
        supporting_data = [
            evidence_by_id[evidence_id]
            for evidence_id in data_support
            if evidence_id in evidence_by_id
        ]
        violations.extend(
            _claim_scope_violations(
                claim,
                index=index,
                supporting_data=supporting_data,
            )
        )
        violations.extend(
            _multi_baseline_claim_violations(
                supporting_data,
                index=index,
            )
        )
    return violations


def render_final_answer(
    pack: Mapping[str, Any],
    selection: Mapping[str, Any],
    projection: Mapping[str, Any] | None = None,
) -> str:
    """Render a validated selection without allowing second-generation facts."""

    evidence_by_id = _pack_evidence_by_id(pack)
    codes, phrases = _knowledge_signals(pack)
    knowledge_fact_by_id = {
        str(item["evidence_id"]): str(
            _project_knowledge_evidence(
                item,
                codes=codes,
                phrases=phrases,
            )[0]["supported_fact"]
        )
        for item in pack.get("knowledge_evidence", [])
    }
    limitation_by_id = {
        str(item["limitation_id"]): item for item in pack.get("limitations", [])
    }
    bundle_by_id = {
        str(item.get("bundle_id")): item
        for item in (projection or {}).get("evidence_bundles", [])
        if isinstance(item, Mapping) and item.get("bundle_id")
    }
    data_ids, knowledge_ids = _selected_ids(selection)
    lines = ["DATA EVIDENCE"]
    if data_ids:
        for evidence_id in data_ids:
            item = evidence_by_id[evidence_id]
            sources = ",".join(map(str, item.get("source_task_ids", [])))
            lines.append(f"- {item['statement']} [data:{sources}]")
    else:
        lines.append("- No database evidence was queried for this answer.")

    lines.append("KNOWLEDGE EVIDENCE")
    if knowledge_ids:
        for evidence_id in knowledge_ids:
            lines.append(f"- {knowledge_fact_by_id[evidence_id]} [{evidence_id}]")
    else:
        lines.append("- No retrieved knowledge evidence was selected.")

    lines.append("INFERENCE")
    inferences = selection.get("inferences", [])
    if inferences:
        for claim in inferences:
            support = ", ".join(claim["supporting_evidence_ids"])
            statement = _render_claim_statement(
                claim,
                evidence_by_id,
                limitation_by_id,
                knowledge_fact_by_id,
                bundle=bundle_by_id.get(str(claim.get("bundle_id") or "")),
            )
            lines.append(
                f"- [{claim['claim_type']}] {statement} "
                f"[supports: {support}]"
            )
    else:
        lines.append("- No inference beyond the selected evidence.")

    lines.append("LIMITATION")
    for limitation_id in selection.get("limitation_ids", []):
        item = limitation_by_id[limitation_id]
        sources = ", ".join(map(str, item.get("source_identifiers", [])))
        lines.append(f"- {item['statement']} [sources: {sources}]")
    return "\n".join(lines)


def _ordered_unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _comparison_direction(facts: Mapping[str, Any]) -> str:
    classification = str(facts.get("classification") or "")
    if classification:
        return classification
    delta = _number(facts.get("delta"))
    if delta is None:
        return "unknown"
    if delta < 0:
        return "declined"
    if delta > 0:
        return "improved"
    return "unchanged"


def _is_ratio_metric(metric: str, *values: Any) -> bool:
    normalized = metric.casefold()
    numbers = [_number(value) for value in values]
    return (
        any(token in normalized for token in ("rate", "ratio", "success"))
        and all(number is not None and abs(number) <= 1 for number in numbers)
    )


def _format_percent(value: Any) -> str:
    number = _number(value)
    if number is None:
        return str(value)
    percentage = number * 100
    rendered = f"{percentage:.2f}".rstrip("0").rstrip(".")
    return f"{rendered}%"


def _format_percentage_points(value: Any) -> str:
    number = _number(value)
    if number is None:
        return str(value)
    return f"{number * 100:.2f}".rstrip("0").rstrip(".")


def _render_comparison_item(item: Mapping[str, Any]) -> str:
    facts = item.get("facts")
    if not isinstance(facts, Mapping):
        return "所选比较证据缺少结构化事实。"
    baseline = str(facts.get("baseline_window") or "baseline")
    current = str(facts.get("current_window") or "current")
    metric = str(facts.get("metric") or "metric")
    group = str(facts.get("group") or "整体")
    baseline_value = facts.get("baseline_value")
    current_value = facts.get("current_value")
    delta = facts.get("delta")
    direction = _comparison_direction(facts)
    if _is_ratio_metric(metric, baseline_value, current_value, delta):
        change = _format_percentage_points(abs(float(_number(delta) or 0)))
        verb = {
            "declined": f"下降 {change} 个百分点",
            "improved": f"上升 {change} 个百分点",
            "unchanged": "没有变化",
        }.get(direction, f"变化 {change} 个百分点")
        return (
            f"以 {baseline} 为基线，{group} 的 {metric} 从 "
            f"{_format_percent(baseline_value)} 变为 "
            f"{_format_percent(current_value)}，{verb}（当前窗口：{current}）。"
        )
    return (
        f"以 {baseline} 为基线，{group} 的 {metric} 从 {baseline_value} "
        f"变为 {current_value}，变化量为 {delta}（当前窗口：{current}）。"
    )


def _render_comparison_selection(
    items: Sequence[Mapping[str, Any]],
) -> list[str]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in items:
        facts = item.get("facts")
        if not isinstance(facts, Mapping):
            continue
        key = (
            str(facts.get("metric") or "metric"),
            str(facts.get("group") or "__overall__"),
        )
        grouped.setdefault(key, []).append(item)

    rendered: list[str] = []
    for (metric, group_key), evidence in grouped.items():
        baselines = _ordered_unique(
            [
                str(item.get("facts", {}).get("baseline_window") or "")
                for item in evidence
            ]
        )
        conclusions = {_comparison_conclusion(item) for item in evidence}
        if len(baselines) > 1 and len(conclusions) == 1:
            classification, largest = next(iter(conclusions))
            subject = "整体" if group_key == "__overall__" else group_key
            baseline_text = " 和 ".join(baselines)
            if largest is True and classification == "declined":
                rendered.append(
                    f"在 {baseline_text} 两个基线比较下，{subject} 均为 "
                    f"{metric} 下降幅度最大的分组。"
                )
            else:
                direction = {
                    "declined": "下降",
                    "improved": "上升",
                    "unchanged": "不变",
                }.get(classification, "一致变化")
                rendered.append(
                    f"在 {baseline_text} 两个基线比较下，{subject} 的 "
                    f"{metric} 均表现为{direction}。"
                )
            continue
        rendered.extend(_render_comparison_item(item) for item in evidence)
    return rendered


def _evidence_subject_label(item: Mapping[str, Any]) -> str:
    facts = item.get("facts")
    facts = facts if isinstance(facts, Mapping) else {}
    kind = str(item.get("kind") or "")
    if kind in {"metric_comparison", "group_comparison"}:
        group = str(facts.get("group") or "整体")
        return (
            f"{group} 的 {facts.get('baseline_window')}→"
            f"{facts.get('current_window')} {facts.get('metric')} 变化"
        )
    if kind == "alarm_status_distribution":
        scope = _evidence_scope(item)
        code = scope.get("error_code") or "所选错误码"
        return f"{code} 告警分布"
    if kind in {"reviewed_tool_result_summary", "reviewed_result_summary"}:
        return "结构化日志证据"
    return kind or "所选数据证据"


def _render_claim_statement(
    claim: Mapping[str, Any],
    evidence_by_id: Mapping[str, Mapping[str, Any]],
    limitation_by_id: Mapping[str, Mapping[str, Any]],
    knowledge_fact_by_id: Mapping[str, str],
    *,
    bundle: Mapping[str, Any] | None = None,
) -> str:
    """Render structured predicates without reusing model-authored fact wording."""

    if claim.get("predicate") == "stable_control":
        subject_items = [
            evidence_by_id.get(str(evidence_id), {})
            for evidence_id in claim.get("subject_evidence_ids", [])
        ]
        groups: list[str] = []
        for item in subject_items:
            group = str(item.get("facts", {}).get("group") or "").strip()
            if group and group not in groups:
                groups.append(group)
        subject = "、".join(groups)
        if len(groups) == 2:
            subject = " 和 ".join(groups)
        if not subject:
            subject = "所选分组"
        baselines = _ordered_unique(
            [
                str(item.get("facts", {}).get("baseline_window") or "")
                for item in subject_items
            ]
        )
        if len(baselines) > 1:
            scope_prefix = f"在 {' 和 '.join(baselines)} 两个基线比较下，"
            decline_word = "均出现下降"
        elif baselines:
            scope_prefix = f"以 {baselines[0]} 为基线，"
            decline_word = "出现下降"
        else:
            scope_prefix = "在所选比较范围内，"
            decline_word = "出现下降"
        if claim.get("polarity") == "negative":
            return (
                f"{scope_prefix}{subject} {decline_word}，因此不能作为稳定、"
                "未受影响的对照组。"
            )
        return f"{scope_prefix}{subject} 可作为稳定、未受影响的对照组。"

    claim_type = str(claim.get("claim_type") or "")
    bundle_prefix = _bundle_render_prefix(bundle)
    support_ids = list(map(str, claim.get("supporting_evidence_ids", [])))
    if claim_type == "observation":
        comparison_items = [
            evidence_by_id[evidence_id]
            for evidence_id in support_ids
            if evidence_id in evidence_by_id
            and evidence_by_id[evidence_id].get("kind")
            in {"metric_comparison", "group_comparison"}
        ]
        statements = _render_comparison_selection(comparison_items)
        statements.extend(
            str(evidence_by_id[evidence_id].get("statement") or "")
            for evidence_id in support_ids
            if evidence_id in evidence_by_id
            and evidence_by_id[evidence_id].get("kind") not in {
                "retrieved_knowledge",
                "metric_comparison",
                "group_comparison",
            }
        )
        statements = [item for item in statements if item]
        rendered = "；".join(statements)
        if rendered and bundle_prefix:
            return f"{bundle_prefix}{rendered}"
        return rendered or "所选数据证据中没有可渲染的观测事实。"
    if claim_type == "knowledge":
        statements = [
            knowledge_fact_by_id[evidence_id]
            for evidence_id in support_ids
            if evidence_id in knowledge_fact_by_id
        ]
        statements = [item for item in statements if item]
        return "；".join(statements) or "所选知识证据中没有可渲染的领域定义。"
    if claim_type == "correlation":
        subjects = _ordered_unique(
            [
                _evidence_subject_label(evidence_by_id[evidence_id])
                for evidence_id in support_ids
                if evidence_id in evidence_by_id
                and evidence_by_id[evidence_id].get("kind")
                != "retrieved_knowledge"
            ]
        )
        subject_text = " 与 ".join(subjects[:3]) or "所引用的现象"
        return (
            f"{bundle_prefix}{subject_text} 在所选证据范围内同时出现或存在相关性，"
            "但当前证据不足以"
            "证明因果关系。"
        )
    if claim_type == "hypothesis":
        knowledge_titles = [
            str(evidence_by_id[evidence_id].get("title") or "").strip()
            for evidence_id in support_ids
            if evidence_id in knowledge_fact_by_id
        ]
        candidate = "、".join(item for item in knowledge_titles if item)
        subject = f"{candidate} 所描述的因素" if candidate else "相关因素"
        has_limitation_support = any(
            evidence_id in limitation_by_id for evidence_id in support_ids
        )
        if has_limitation_support:
            return (
                f"{bundle_prefix}基于所选数据及其明确证据缺口，{subject}是当前需要优先验证的"
                "候选影响因素，尚不能确认其为根因。"
            )
        return (
            f"{bundle_prefix}基于所选证据，{subject}是需要进一步验证的候选解释，当前尚未证明"
            "因果关系。"
        )
    if claim_type == "recommendation":
        knowledge_titles = [
            str(evidence_by_id[evidence_id].get("title") or "").strip()
            for evidence_id in support_ids
            if evidence_id in evidence_by_id
            and evidence_by_id[evidence_id].get("kind") == "retrieved_knowledge"
        ]
        limitation_statements = [
            str(limitation_by_id[evidence_id].get("statement") or "").strip()
            for evidence_id in support_ids
            if evidence_id in limitation_by_id
        ]
        basis = "、".join(item for item in knowledge_titles if item)
        gaps = "；".join(item for item in limitation_statements if item)
        prefix = (
            f"{bundle_prefix}根据 {basis}，"
            if basis
            else f"{bundle_prefix}根据所选知识证据，"
        )
        if gaps:
            return (
                f"{prefix}建议进一步检查或验证与这些证据缺口对应的数据："
                f"{gaps} 该建议不代表已证明根因。"
            )
        return f"{prefix}建议进一步检查或验证相关链路和指标。"
    if claim_type == "causal_claim":
        return "所选明确因果证据支持该因果结论。"
    return "该结构化主张没有可用的确定性渲染规则。"


def _bundle_render_prefix(bundle: Mapping[str, Any] | None) -> str:
    """Return a deterministic scope qualifier for one validated bundle."""

    if not isinstance(bundle, Mapping):
        return ""
    bundle_type = str(bundle.get("bundle_type") or "")
    scope = bundle.get("scope")
    scope = scope if isinstance(scope, Mapping) else {}
    targets = bundle.get("target_entities")
    targets = targets if isinstance(targets, Mapping) else {}
    if bundle_type == "entity":
        parts = [
            f"{dimension}={value}"
            for dimension, values in targets.items()
            for value in (
                values
                if isinstance(values, Sequence)
                and not isinstance(values, (str, bytes))
                else [values]
            )
        ]
        if parts:
            return f"针对 {', '.join(parts)}，"
    region = str(scope.get("region") or "").strip()
    if bundle_type == "regional":
        return f"针对{region}区域，" if region and region != _ALL_SCOPE else "针对区域范围，"
    if bundle_type == "multi_group":
        entity_names = _ordered_unique(
            [
                str(value)
                for values in targets.values()
                for value in (
                    values
                    if isinstance(values, Sequence)
                    and not isinstance(values, (str, bytes))
                    else [values]
                )
            ]
        )
        if entity_names:
            return f"在 {'、'.join(entity_names)} 的跨组比较中，"
    return ""


def _clauses(text: str, *, split_commas: bool = False) -> list[str]:
    punctuation = r"[\n。！？!?;；]"
    if split_commas:
        punctuation = r"[\n。！？!?;；,，]"
    return [item.strip() for item in re.split(punctuation, text) if item.strip()]


def _has_explicit_negation(clause: str) -> bool:
    lowered = clause.casefold()
    return any(
        token in lowered
        for token in (
            "not ",
            "doesn't",
            "didn't",
            "cannot",
            "can't",
            "must not",
            "不得",
            "不能",
            "不可",
            "不应",
            "并非",
            "不是",
            "不足以",
            "尚未",
            "勿",
            "禁止",
        )
    )


def normalize_evidence_language(
    text: str,
    *,
    contract: Mapping[str, Any],
) -> str:
    """Replace disproven healthy-control labels with comparison wording.

    The rewrite is enabled only when every observed group in at least one
    comparison has a negative delta, so there is no data-supported healthy
    group that the wording could legitimately describe.
    """

    all_groups_declined = any(
        comparison.get("groups")
        and len(comparison.get("declining_groups", []))
        == len(comparison.get("groups", []))
        for comparison in contract.get("comparisons", [])
    )
    if not all_groups_declined:
        return text
    replacements = (
        (r"\bhealthy\s+control\s+groups?\b", "smaller-decline comparison groups"),
        (r"\bhealthy\s+controls?\b", "smaller-decline comparisons"),
        (r"\bnormal\s+controls?\b", "smaller-decline comparisons"),
        (r"\bunaffected\s+cdns?\b", "other also-declining CDNs"),
        (r"\bhealthy\s+cdns?\b", "other also-declining CDNs"),
        (r"健康对照组", "降幅较小的比较组"),
        (r"健康的?\s*CDN", "也有下降的 CDN"),
        (r"未受影响的?\s*CDN", "也有下降的 CDN"),
        (r"正常对照组", "降幅较小的比较组"),
        (r"\bhealthy\b", "also declining"),
        (r"\bunaffected\b", "also declining"),
        (r"未受影响", "也有下降"),
        (r"健康", "也有下降"),
    )
    output = text
    for pattern, replacement in replacements:
        output = re.sub(pattern, replacement, output, flags=re.IGNORECASE)
    return output


def _count_is_stated(text: str, *, value: str, count: Any) -> bool:
    escaped_value = re.escape(value.casefold())
    escaped_count = re.escape(str(count))
    separator = r"[^\n。；;]{0,24}"
    return bool(
        re.search(rf"{escaped_value}{separator}{escaped_count}(?!\d)", text)
        or re.search(rf"(?<!\d){escaped_count}{separator}{escaped_value}", text)
    )


def evidence_accuracy_violations(
    answer: str,
    *,
    supporting_items: Sequence[str] = (),
    contract: Mapping[str, Any],
    require_sections: bool = False,
) -> list[str]:
    """Return deterministic evidence-expression violations in model output."""

    violations: list[str] = []
    answer_lower = answer.casefold()
    combined = "\n".join((answer, *supporting_items))
    combined_lower = combined.casefold()

    if require_sections:
        section_patterns = {
            "DATA EVIDENCE": r"(?:data evidence|数据证据)",
            "KNOWLEDGE EVIDENCE": r"(?:knowledge evidence|知识证据)",
            "INFERENCE": r"(?:inference|推断)",
            "LIMITATION": r"(?:limitation|局限|限制)",
        }
        heading_prefix = r"(?:^|\n)\s*(?:[#>*-]+\s*)?(?:\*\*|【|\[)?"
        missing = [
            section
            for section, pattern in section_patterns.items()
            if not re.search(heading_prefix + pattern, answer_lower, re.IGNORECASE)
        ]
        if missing:
            violations.append(f"missing evidence sections: {missing}")

    for item in contract.get("status_distributions", []):
        distribution = item.get("distribution", [])
        sample_events = [
            sample
            for sample in item.get("sample_events", [])
            if isinstance(sample, Mapping)
        ]
        if any(
            _generalizes_bounded_alarm_samples(clause, sample_events)
            for clause in _clauses(combined, split_commas=True)
        ):
            violations.append(
                "bounded alarm samples are generalized to every event"
            )
        if not item.get("mixed") or len(distribution) < 2:
            continue
        values = [str(entry["value"]) for entry in distribution]
        counts = [entry["count"] for entry in distribution]
        missing_values = [
            value for value in values if value.casefold() not in combined_lower
        ]
        if missing_values:
            violations.append(
                f"mixed status distribution omits values: {missing_values}"
            )
        shared_count = len(set(map(str, counts))) == 1
        each_count = shared_count and bool(
            re.search(
                rf"(?:each|各)\s*(?:为|有|[:=])?\s*"
                rf"{re.escape(str(counts[0]))}(?!\d)",
                combined_lower,
            )
        )
        if not each_count:
            missing_counts = [
                f"{value}:{count}"
                for value, count in zip(values, counts, strict=True)
                if not _count_is_stated(
                    combined_lower,
                    value=value,
                    count=count,
                )
            ]
            if missing_counts:
                violations.append(
                    "mixed status distribution omits explicit counts: "
                    f"{missing_counts}"
                )
        for clause in _clauses(combined, split_commas=True):
            lowered = clause.casefold()
            if (
                re.search(r"(?:all|every|全部|均|都|三条|3\s*条)", lowered)
                and re.search(r"(?:resolved|已解决|已恢复)", lowered)
                and not _has_explicit_negation(clause)
            ):
                violations.append(
                    "mixed alarm statuses are incorrectly described as all resolved"
                )
                break

    health_terms = (
        "healthy",
        "unaffected",
        "normal control",
        "健康",
        "未受影响",
        "正常对照",
    )
    for comparison in contract.get("comparisons", []):
        declining = [str(group) for group in comparison.get("declining_groups", [])]
        for clause in _clauses(combined):
            lowered = clause.casefold()
            mentions_health = any(term in lowered for term in health_terms)
            if not mentions_health or _has_explicit_negation(clause):
                continue
            named_decline = any(group.casefold() in lowered for group in declining)
            generic_decline = bool(
                declining
                and re.search(
                    r"(?:other|comparison|remaining)\s+cdns?|"
                    r"(?:其他|其余|对照)(?:\s*cdn|组)",
                    lowered,
                )
            )
            if named_decline or generic_decline:
                violations.append(
                    "declining groups cannot be described as healthy controls; "
                    f"state that these groups also declined: {declining}"
                )
                break

    causal_patterns = (
        r"e302.{0,40}(?:caused|causes|导致|造成|引起|根因)",
        r"(?:caused by|root cause.{0,10}(?:is|was)|根因是).{0,40}e302",
    )
    uncertainty_terms = (
        "possible",
        "possibly",
        "likely",
        "candidate",
        "correlat",
        "unproven",
        "可能",
        "疑似",
        "候选",
        "相关",
        "尚未证明",
        "无法证明",
        "不能证明",
        "之一",
    )
    for clause in _clauses(combined):
        lowered = clause.casefold()
        if not any(re.search(pattern, lowered) for pattern in causal_patterns):
            continue
        if _has_explicit_negation(clause) or any(
            token in lowered for token in uncertainty_terms
        ):
            continue
        violations.append("correlation is incorrectly stated as proven causation")
        break

    return violations
