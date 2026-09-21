"""Strict read-only Media tools backed by the configured Wren project."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol, cast

from pydantic import Field, field_validator, model_validator

from datapilot.tools.contracts import (
    ReadOnlyTool,
    ToolArguments,
    ToolRegistry,
    ToolResult,
)
from datapilot.tools.wren_tools import WrenQueryResult

WindowName = Literal[
    "prior_year", "previous_day", "previous_window", "current_window"
]
QoEMetric = Literal[
    "session_count",
    "successful_sessions",
    "failed_sessions",
    "playback_success_rate",
    "average_startup_time_ms",
    "total_buffer_duration_seconds",
    "rebuffer_ratio",
]
Region = Literal["华东", "华南", "华北"]
CDN = Literal["CDN-A", "CDN-B", "CDN-C"]
Device = Literal["mobile", "web", "tv"]
AlarmSeverity = Literal["low", "medium", "high"]
AlarmStatus = Literal["open", "investigating", "resolved"]
LogLevel = Literal["INFO", "WARN", "ERROR"]
Codec = Literal["H.264", "H.265", "AV1"]
TranscodeStatus = Literal["queued", "running", "succeeded", "failed"]

_ALARM_SAMPLE_LIMIT = 3
_ALARM_SAMPLE_FIELDS = (
    "alarm_id",
    "timestamp",
    "window_name",
    "region",
    "cdn",
    "status",
    "severity",
    "error_code",
    "service",
    "message",
)

_QOE_METRIC_CONTRACTS: dict[str, dict[str, Any]] = {
    "session_count": {
        "unit": "count",
        "aggregation_semantics": "count(stream_sessions)",
        "supporting_fields": [],
    },
    "successful_sessions": {
        "unit": "count",
        "aggregation_semantics": "sum(play_success=true)",
        "supporting_fields": ["session_count"],
    },
    "failed_sessions": {
        "unit": "count",
        "aggregation_semantics": "sum(play_success=false)",
        "supporting_fields": ["session_count", "successful_sessions"],
    },
    "playback_success_rate": {
        "unit": "ratio",
        "aggregation_semantics": (
            "sum(successful_sessions)/sum(session_count)"
        ),
        "supporting_fields": [
            "session_count",
            "successful_sessions",
            "failed_sessions",
        ],
    },
    "average_startup_time_ms": {
        "unit": "milliseconds",
        "aggregation_semantics": "avg(startup_time)",
        "supporting_fields": ["session_count"],
    },
    "total_buffer_duration_seconds": {
        "unit": "seconds",
        "aggregation_semantics": "sum(buffer_duration)",
        "supporting_fields": ["session_count"],
    },
    "rebuffer_ratio": {
        "unit": "ratio",
        "aggregation_semantics": (
            "sum(buffer_duration)/sum(playback_duration)"
        ),
        "supporting_fields": [
            "total_buffer_duration_seconds",
            "session_count",
        ],
    },
}


class MediaWrenRuntime(Protocol):
    """Only the existing Wren operations needed by deterministic tools."""

    def dry_plan(self, sql: str) -> str: ...

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult: ...


def _normalize_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("T", " "))
    except ValueError as exc:
        raise ValueError("timestamp must use ISO-8601 date and time") from exc
    if parsed.tzinfo is not None:
        raise ValueError("timestamp must use the synthetic timezone-naive timeline")
    return parsed.isoformat(sep=" ", timespec="seconds")


class TimeWindowArguments(ToolArguments):
    """Shared bounded time/filter inputs for Media event tools."""

    windows: list[WindowName] | None = Field(default=None, min_length=1, max_length=4)
    start_time: str | None = Field(default=None, max_length=32)
    end_time: str | None = Field(default=None, max_length=32)
    region: Region | None = None
    cdn: CDN | None = None
    limit: int = Field(default=100, ge=1, le=100)

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_timestamp(cls, value: str | None) -> str | None:
        return _normalize_timestamp(value)

    @model_validator(mode="after")
    def validate_window(self) -> TimeWindowArguments:
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time and end_time must be supplied together")
        if self.start_time is not None and self.end_time is not None:
            if self.start_time >= self.end_time:
                raise ValueError("start_time must be before end_time")
        if self.windows and len(set(self.windows)) != len(self.windows):
            raise ValueError("windows must not contain duplicates")
        return self


class QoEArguments(TimeWindowArguments):
    device: Device | None = None
    primary_metric: QoEMetric = "playback_success_rate"
    group_by: list[Literal["window_name", "region", "cdn", "device"]] = Field(
        default_factory=list,
        max_length=4,
    )

    @model_validator(mode="after")
    def validate_grouping(self) -> QoEArguments:
        if len(set(self.group_by)) != len(self.group_by):
            raise ValueError("group_by must not contain duplicates")
        return self


class AlarmArguments(TimeWindowArguments):
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][0-9]{3}$")
    severity: AlarmSeverity | None = None
    status: AlarmStatus | None = None


class LogArguments(TimeWindowArguments):
    service: Literal["player-api", "cdn-gateway", "origin-proxy"] | None = None
    level: LogLevel | None = None
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][0-9]{3}$")
    trace_id: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,64}$")
    message_contains: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[\w\s.-]+$",
    )


class TranscodeArguments(ToolArguments):
    windows: list[WindowName] | None = Field(default=None, min_length=1, max_length=4)
    start_time: str | None = Field(default=None, max_length=32)
    end_time: str | None = Field(default=None, max_length=32)
    region: Region | None = None
    codec: Codec | None = None
    status: TranscodeStatus | None = None
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][0-9]{3}$")
    gpu_pool: Literal["gpu-a", "gpu-b", "gpu-c"] | None = None
    limit: int = Field(default=100, ge=1, le=100)

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_timestamp(cls, value: str | None) -> str | None:
        return _normalize_timestamp(value)

    @model_validator(mode="after")
    def validate_window(self) -> TranscodeArguments:
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time and end_time must be supplied together")
        if self.start_time is not None and self.end_time is not None:
            if self.start_time >= self.end_time:
                raise ValueError("start_time must be before end_time")
        if self.windows and len(set(self.windows)) != len(self.windows):
            raise ValueError("windows must not contain duplicates")
        return self


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _where_clauses(arguments: Any) -> list[str]:
    clauses: list[str] = []
    if arguments.windows:
        values = ", ".join(_literal(value) for value in arguments.windows)
        clauses.append(f"window_name IN ({values})")
    if arguments.start_time is not None:
        clauses.append(f"timestamp >= TIMESTAMP {_literal(arguments.start_time)}")
        clauses.append(f"timestamp < TIMESTAMP {_literal(arguments.end_time)}")
    for field in (
        "region",
        "cdn",
        "device",
        "error_code",
        "severity",
        "status",
        "service",
        "level",
        "trace_id",
        "codec",
        "gpu_pool",
    ):
        value = getattr(arguments, field, None)
        if value is not None:
            clauses.append(f"{field} = {_literal(value)}")
    message = getattr(arguments, "message_contains", None)
    if message is not None:
        clauses.append(f"LOWER(message) LIKE {_literal('%' + message.lower() + '%')}")
    return clauses


def _where_sql(arguments: Any) -> str:
    clauses = _where_clauses(arguments)
    return "" if not clauses else "\nWHERE " + " AND ".join(clauses)


def _explicit_times(text: str) -> tuple[str, str] | None:
    matches = re.findall(
        r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?",
        text,
    )
    if len(matches) < 2:
        return None
    start = _normalize_timestamp(matches[0])
    end = _normalize_timestamp(matches[1])
    if start is None or end is None or start >= end:
        return None
    return start, end


def _infer_windows(text: str, *, comparison: bool = False) -> dict[str, Any]:
    explicit = _explicit_times(text)
    if explicit is not None:
        return {"start_time": explicit[0], "end_time": explicit[1]}
    lowered = text.lower()
    windows: list[WindowName] = []
    pairs: tuple[tuple[WindowName, tuple[str, ...]], ...] = (
        ("prior_year", ("prior_year", "去年同期", "上年同期")),
        ("previous_day", ("previous_day", "前一天", "昨日同期")),
        ("previous_window", ("previous_window", "上一窗口", "前一窗口", "前一小时")),
        ("current_window", ("current_window", "当前窗口", "当前时段", "本时段")),
    )
    for window, terms in pairs:
        if any(term.lower() in lowered for term in terms):
            windows.append(window)
    comparison_terms = (
        "比较",
        "对比",
        "下降",
        "变化",
        "环比",
        "compare",
        "drop",
        "decline",
        "change",
        "increase",
        "growth",
    )
    if comparison and any(term in lowered for term in comparison_terms):
        if "previous_window" not in windows:
            windows.insert(0, "previous_window")
        if "current_window" not in windows:
            windows.append("current_window")
    if not windows and any(
        term in lowered
        for term in ("最近", "当前", "同期", "current", "recent")
    ):
        windows.append("current_window")
    return {"windows": windows} if windows else {}


def _infer_common(text: str, *, comparison: bool = False) -> dict[str, Any]:
    arguments = _infer_windows(text, comparison=comparison)
    for region in ("华东", "华南", "华北"):
        if region in text:
            arguments["region"] = region
            break
    match = re.search(r"\bCDN[- ]?([ABC])\b", text, flags=re.IGNORECASE)
    if match:
        arguments["cdn"] = f"CDN-{match.group(1).upper()}"
    return arguments


def _required_group_dimensions(text: str) -> list[str]:
    """Extract supported dimensions from an explicit Planner grouping clause."""

    lowered = text.lower()
    clauses = [
        match.group("dimensions")
        for match in re.finditer(
            r"\bgroup(?:ed)?\s+by\s+(?P<dimensions>.*?)"
            r"(?=\b(?:return|compute|calculate|for|so\s+that|to\s+identify)\b|"
            r"[.;。；]|$)",
            lowered,
        )
    ]
    aliases = {
        "window_name": ("window_name", "time window", "窗口"),
        "region": ("region", "区域"),
        "cdn": ("cdn",),
        "device": ("device", "设备"),
    }
    return [
        dimension
        for dimension, terms in aliases.items()
        if any(term in clause for clause in clauses for term in terms)
    ]


def _error_code(text: str) -> str | None:
    for match in re.finditer(
        r"\b([A-Z][0-9]{3})\b",
        text,
        flags=re.IGNORECASE,
    ):
        candidate = match.group(1).upper()
        prefix = text[max(0, match.start() - 6) : match.start()].lower()
        if prefix.endswith("trace-") or candidate in {"H264", "H265"}:
            continue
        return candidate
    return None


def _categorical_counts(
    rows: Sequence[Mapping[str, Any]],
    field: str,
) -> dict[str, int]:
    counts = Counter(
        str(row[field])
        for row in rows
        if row.get(field) is not None and str(row[field]).strip()
    )
    return {key: counts[key] for key in sorted(counts)}


def build_alarm_evidence(
    rows: Sequence[Mapping[str, Any]],
    *,
    sample_limit: int = _ALARM_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Build a lossless categorical summary plus bounded raw event samples.

    Counts always cover every supplied row. Samples are stable examples only;
    duplicate messages are sampled once and never stand in for a distribution.
    """

    if not 1 <= sample_limit <= 5:
        raise ValueError("sample_limit must be between 1 and 5")
    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (
            str(row.get("timestamp") or ""),
            str(row.get("alarm_id") or ""),
        ),
    )
    sample_events: list[dict[str, Any]] = []
    seen_samples: set[tuple[str, str]] = set()
    for row in ordered:
        message = str(row.get("message") or "").strip()
        identity = str(row.get("alarm_id") or "").strip()
        sample_key = ("message", message) if message else ("alarm_id", identity)
        if sample_key in seen_samples:
            continue
        seen_samples.add(sample_key)
        sample: dict[str, Any] = {}
        for field in _ALARM_SAMPLE_FIELDS:
            value = row.get(field)
            if value is None:
                continue
            if field == "message":
                value = " ".join(str(value).split())[:240]
            sample[field] = value
        if sample:
            sample_events.append(sample)
        if len(sample_events) >= sample_limit:
            break

    affected_objects = {
        field: sorted(
            {
                str(row[field])
                for row in ordered
                if row.get(field) is not None and str(row[field]).strip()
            }
        )
        for field in ("region", "cdn", "service")
    }
    return {
        "version": "1.0",
        "total_count": len(ordered),
        "count_by_status": _categorical_counts(ordered, "status"),
        "count_by_severity": _categorical_counts(ordered, "severity"),
        "count_by_error_code": _categorical_counts(ordered, "error_code"),
        "count_by_cdn": _categorical_counts(ordered, "cdn"),
        "count_by_service": _categorical_counts(ordered, "service"),
        "count_by_window": _categorical_counts(ordered, "window_name"),
        "affected_objects": affected_objects,
        "sample_events": sample_events,
        "sample_limit": sample_limit,
        "sample_semantics": "bounded_examples_not_distribution",
    }


class MediaWrenTool(ReadOnlyTool):
    domain = "media"

    def __init__(self, wren_tools: MediaWrenRuntime) -> None:
        self.wren_tools = wren_tools

    def _query(
        self,
        arguments: ToolArguments,
        sql: str,
        *,
        subject: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        planned_sql = self.wren_tools.dry_plan(sql)
        if not isinstance(planned_sql, str) or not planned_sql.strip():
            raise RuntimeError("Wren dry plan returned no executable plan")
        limit = cast(int, getattr(arguments, "limit", 100))
        result = self.wren_tools.query(sql, limit=limit)
        details = {
            "arguments": arguments.model_dump(mode="json", exclude_none=True),
            "columns": list(result.columns),
            "row_count": result.row_count,
            "dry_plan": "succeeded",
            "executed_query": sql,
            **dict(metadata or {}),
        }
        if result.row_count:
            summary = f"Returned {result.row_count} {subject} row(s)."
        else:
            summary = f"No {subject} rows matched the validated filters."
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=list(result.rows),
            summary=summary,
            metadata=details,
            error=None,
            execution_time_ms=0.0,
        )


class QueryQoEMetricsTool(MediaWrenTool):
    name = "query_qoe_metrics"
    description = (
        "Query playback success, startup time, rebuffer ratio, and failed "
        "sessions with bounded Media dimensions and time filters."
    )
    input_model = QoEArguments

    def routing_score(self, task_description: str) -> int:
        lowered = task_description.lower()
        terms = (
            "播放成功",
            "卡顿",
            "首帧",
            "startup",
            "rebuffer",
            "qoe",
            "failed session",
            "buffer_duration",
            "play_success",
            "playback success",
            "playback_success_rate",
            "success rate",
        )
        return 90 + sum(term in lowered for term in terms) * 5 if any(
            term in lowered for term in terms
        ) else 0

    def infer_arguments(self, task_description: str) -> dict[str, Any]:
        arguments = _infer_common(task_description, comparison=True)
        lowered = task_description.lower()
        metric_terms: tuple[tuple[str, tuple[str, ...]], ...] = (
            (
                "playback_success_rate",
                (
                    "playback_success_rate",
                    "playback success rate",
                    "播放成功率",
                    "播放成功",
                    "success rate",
                ),
            ),
            (
                "failed_sessions",
                (
                    "failed_sessions",
                    "failed sessions",
                    "failed session",
                    "失败会话",
                    "失败数",
                ),
            ),
            (
                "average_startup_time_ms",
                ("average_startup_time_ms", "startup time", "首帧耗时"),
            ),
            (
                "rebuffer_ratio",
                ("rebuffer_ratio", "rebuffer ratio", "卡顿率"),
            ),
            (
                "total_buffer_duration_seconds",
                ("buffer duration", "buffer_duration", "卡顿时长"),
            ),
            (
                "successful_sessions",
                ("successful_sessions", "successful sessions", "成功会话数"),
            ),
            ("session_count", ("session_count", "session count", "会话数")),
        )
        for metric, terms in metric_terms:
            if any(term in lowered for term in terms):
                arguments["primary_metric"] = metric
                break
        device_aliases = {
            "mobile": ("mobile", "移动端", "手机"),
            "web": ("web", "网页"),
            "tv": ("tv", "电视端", "大屏"),
        }
        for device, aliases in device_aliases.items():
            if any(alias in lowered for alias in aliases):
                arguments["device"] = device
                break
        group_by: list[str] = []
        if len(arguments.get("windows", [])) > 1:
            group_by.append("window_name")
        if arguments.get("start_time") and any(
            term in lowered for term in ("比较", "对比", "变化", "下降")
        ):
            group_by.append("window_name")
        dimension_terms = {
            "region": ("各区域", "按区域", "by region", "哪个区域"),
            "cdn": ("各cdn", "按cdn", "by cdn", "哪个cdn", "cdn 贡献"),
            "device": ("各设备", "按设备", "by device", "哪个设备"),
        }
        for dimension, terms in dimension_terms.items():
            if any(term in lowered for term in terms):
                group_by.append(dimension)
        group_by.extend(_required_group_dimensions(task_description))
        if group_by:
            arguments["group_by"] = list(dict.fromkeys(group_by))
        return arguments

    def _execute(self, arguments: ToolArguments) -> ToolResult:
        values = cast(QoEArguments, arguments)
        dimensions = list(values.group_by)
        select_dimensions = ""
        group_sql = ""
        order_sql = ""
        if dimensions:
            joined = ",\n  ".join(dimensions)
            select_dimensions = f"  {joined},\n"
            grouped = ", ".join(dimensions)
            group_sql = f"\nGROUP BY {grouped}"
            order_sql = f"\nORDER BY {grouped}"
        sql = (
            "SELECT\n"
            f"{select_dimensions}"
            "  COUNT(*) AS session_count,\n"
            "  SUM(CASE WHEN play_success THEN 1 ELSE 0 END) AS successful_sessions,\n"
            "  SUM(CASE WHEN play_success THEN 0 ELSE 1 END) AS failed_sessions,\n"
            "  AVG(CASE WHEN play_success THEN 1.0 ELSE 0.0 END) "
            "AS playback_success_rate,\n"
            "  AVG(startup_time) AS average_startup_time_ms,\n"
            "  SUM(buffer_duration) AS total_buffer_duration_seconds,\n"
            "  SUM(buffer_duration) / NULLIF(SUM(playback_duration), 0) "
            "AS rebuffer_ratio\n"
            "FROM stream_sessions"
            f"{_where_sql(values)}{group_sql}{order_sql}"
        )
        metric_contract = _QOE_METRIC_CONTRACTS[values.primary_metric]
        return self._query(
            values,
            sql,
            subject="QoE metric",
            metadata={
                "dimensions": dimensions,
                "metrics": [
                    "session_count",
                    "successful_sessions",
                    "failed_sessions",
                    "playback_success_rate",
                    "average_startup_time_ms",
                    "total_buffer_duration_seconds",
                    "rebuffer_ratio",
                ],
                "metric_binding": {
                    "primary_metric": values.primary_metric,
                    "unit": metric_contract["unit"],
                    "aggregation_semantics": metric_contract[
                        "aggregation_semantics"
                    ],
                    "supporting_fields": list(
                        metric_contract["supporting_fields"]
                    ),
                },
                "units": {
                    metric: contract["unit"]
                    for metric, contract in _QOE_METRIC_CONTRACTS.items()
                },
                "aggregation_semantics": {
                    metric: contract["aggregation_semantics"]
                    for metric, contract in _QOE_METRIC_CONTRACTS.items()
                },
                "rebuffer_contract": (
                    "SUM(buffer_duration) / SUM(playback_duration)"
                ),
            },
        )


class GetAlarmEventsTool(MediaWrenTool):
    name = "get_alarm_events"
    description = (
        "Return individual Media alarm events and the exact mixed-status "
        "distribution for bounded time and categorical filters."
    )
    input_model = AlarmArguments

    def routing_score(self, task_description: str) -> int:
        lowered = task_description.lower()
        return 125 if any(term in lowered for term in ("告警", "alarm")) else 0

    def infer_arguments(self, task_description: str) -> dict[str, Any]:
        arguments = _infer_common(task_description)
        lowered = task_description.lower()
        code = _error_code(task_description)
        if code:
            arguments["error_code"] = code
        for value in ("high", "medium", "low"):
            if value in lowered or {
                "high": "高等级",
                "medium": "中等级",
                "low": "低等级",
            }[value] in task_description:
                arguments["severity"] = value
                break
        status_aliases = {
            "open": ("open", "未处理"),
            "investigating": ("investigating", "调查中"),
            "resolved": ("resolved", "已解决"),
        }
        for status, aliases in status_aliases.items():
            if any(alias in lowered for alias in aliases):
                arguments["status"] = status
                break
        return arguments

    def _execute(self, arguments: ToolArguments) -> ToolResult:
        values = cast(AlarmArguments, arguments)
        sql = (
            "SELECT alarm_id, timestamp, window_name, region, cdn, error_code, "
            "severity, status, message\n"
            "FROM alarm_events"
            f"{_where_sql(values)}\nORDER BY timestamp, alarm_id"
        )
        result = self._query(values, sql, subject="alarm event")
        alarm_evidence = build_alarm_evidence(result.data)
        metadata = {
            **result.metadata,
            "status_distribution": alarm_evidence["count_by_status"],
            "alarm_evidence": alarm_evidence,
        }
        return result.model_copy(update={"metadata": metadata})


class QueryLogsTool(MediaWrenTool):
    name = "query_logs"
    description = (
        "Query deterministic structured Media logs by time, region, CDN, "
        "service, level, error code, trace, or bounded message term."
    )
    input_model = LogArguments

    def routing_score(self, task_description: str) -> int:
        lowered = task_description.lower()
        return 130 if any(
            term in lowered for term in ("日志", " log", "logs", "trace_id", "链路日志")
        ) else 0

    def infer_arguments(self, task_description: str) -> dict[str, Any]:
        arguments = _infer_common(task_description)
        lowered = task_description.lower()
        code = _error_code(task_description)
        if code:
            arguments["error_code"] = code
        services = ("player-api", "cdn-gateway", "origin-proxy")
        for service in services:
            if service in lowered:
                arguments["service"] = service
                break
        for level in ("ERROR", "WARN", "INFO"):
            if level.lower() in lowered:
                arguments["level"] = level
                break
        if "upstream timeout" in lowered:
            arguments["message_contains"] = "upstream timeout"
        trace = re.search(r"\btrace-[a-z0-9-]+\b", lowered)
        if trace:
            arguments["trace_id"] = trace.group(0)
        return arguments

    def _execute(self, arguments: ToolArguments) -> ToolResult:
        values = cast(LogArguments, arguments)
        sql = (
            "SELECT timestamp, window_name, region, cdn, service, level, "
            "error_code, trace_id, message\n"
            "FROM log_events"
            f"{_where_sql(values)}\nORDER BY timestamp, trace_id"
        )
        return self._query(values, sql, subject="structured log")


class GetTranscodeStatusTool(MediaWrenTool):
    name = "get_transcode_status"
    description = (
        "Return deterministic Media transcode job status and failure evidence "
        "by time, region, codec, status, error code, or GPU pool."
    )
    input_model = TranscodeArguments

    def routing_score(self, task_description: str) -> int:
        lowered = task_description.lower()
        return 135 if any(
            term in lowered
            for term in ("转码", "transcode", "h.265", "h265", "h.264", "codec")
        ) else 0

    def infer_arguments(self, task_description: str) -> dict[str, Any]:
        arguments = _infer_windows(task_description)
        lowered = task_description.lower()
        for region in ("华东", "华南", "华北"):
            if region in task_description:
                arguments["region"] = region
                break
        codec_aliases = {
            "H.265": ("h.265", "h265", "hevc"),
            "H.264": ("h.264", "h264", "avc"),
            "AV1": ("av1",),
        }
        for codec, aliases in codec_aliases.items():
            if any(alias in lowered for alias in aliases):
                arguments["codec"] = codec
                break
        statuses = {
            "failed": ("失败", "failed"),
            "succeeded": ("成功", "succeeded"),
            "running": ("运行中", "running"),
            "queued": ("排队", "queued"),
        }
        for status, aliases in statuses.items():
            if any(alias in lowered for alias in aliases):
                arguments["status"] = status
                break
        code = _error_code(task_description)
        if code:
            arguments["error_code"] = code
        for pool in ("gpu-a", "gpu-b", "gpu-c"):
            if pool in lowered:
                arguments["gpu_pool"] = pool
                break
        return arguments

    def _execute(self, arguments: ToolArguments) -> ToolResult:
        values = cast(TranscodeArguments, arguments)
        sql = (
            "SELECT job_id, timestamp, window_name, region, codec, status, "
            "error_code, duration, gpu_pool\n"
            "FROM transcode_jobs"
            f"{_where_sql(values)}\nORDER BY timestamp, job_id"
        )
        result = self._query(values, sql, subject="transcode job")
        distribution = dict(Counter(str(row["status"]) for row in result.data))
        metadata = {**result.metadata, "status_distribution": distribution}
        return result.model_copy(update={"metadata": metadata})


def build_media_tool_registry(wren_tools: MediaWrenRuntime) -> ToolRegistry:
    """Create the shared in-process/MCP registry with no write capability."""

    return ToolRegistry(
        [
            QueryQoEMetricsTool(wren_tools),
            GetAlarmEventsTool(wren_tools),
            QueryLogsTool(wren_tools),
            GetTranscodeStatusTool(wren_tools),
        ]
    )
