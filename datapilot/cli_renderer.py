"""Read-only terminal presentation of an already validated final result.

This module never executes, selects new claims, or edits canonical evidence.
The existing deterministic claim renderer remains the authority for claim meaning.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from itertools import groupby

from datapilot.agent.evidence import (
    _bundle_render_prefix,
    _knowledge_signals,
    _project_knowledge_evidence,
    _render_claim_statement,
)
from datapilot.agent.state import FinalAnswerResult

HUMAN_ANSWER_UNAVAILABLE = "未获得校验通过的最终结果，无法展示分析结论。"

_LABELS = {
    "playback_success_rate": "播放成功率",
    "play_success_rate": "播放成功率",
    "previous_window": "上一窗口",
    "previous_day": "前一天同期",
    "current_window": "当前窗口",
    "region": "区域",
    "cdn": "CDN",
    "device": "设备",
    "error_code": "错误码",
    "severity": "告警级别",
    "status": "状态",
    "service": "服务",
    "level": "日志级别",
    "window_name": "时间窗口",
    "session_count": "会话数",
    "successful_sessions": "成功会话数",
    "failed_sessions": "失败会话数",
    "log_count": "日志数",
    "distinct_trace_count": "不同 trace 数",
    "alarm_count": "告警数",
    "open": "待处理",
    "investigating": "排查中",
    "resolved": "已解决",
    "high": "高",
    "medium": "中",
    "low": "低",
    "E302 — CDN Upstream Timeout": "E302：CDN 上游超时",
    "CDN Playback Success Degradation SOP": "CDN 播放成功率下降排查规程",
    "Playback Success Rate / 播放成功率": "播放成功率定义",
}
_LIMITS = {
    "limitation:correlation_not_causation": (
        "时间与维度重合仅支持相关性或候选假设，不能证明因果关系；"
        "当前证据不足以确认根因。"
    ),
    "limitation:knowledge_not_observed_data": "检索到的领域知识是文档说明，不是数据库中已观察到的事实。",
    "limitation:bounded_evidence": "本回答仅限于本次已通过审查的查询结果。",
    "limitation:missing_trace_data": "缺少请求级 trace 关联，尚不能证明 QoE、告警和日志对应同一批请求。",
    "limitation:missing_origin_metrics": "未纳入源站延迟、饱和度和超时指标。",
    "limitation:missing_routing_change_records": "未纳入路由、配置和发布变更记录。",
    "limitation:limited_time_windows": "对比仅覆盖已审查的时间窗口，仍需更多窗口或受控恢复验证。",
}
_RECOMMENDATION_ACTIONS = {
    "limitation:missing_trace_data": (
        "用请求级 trace 关联 QoE、告警和日志，核对是否对应同一批请求。"
    ),
    "limitation:missing_origin_metrics": "检查源站延迟、资源饱和度和超时指标。",
    "limitation:missing_routing_change_records": "核对同期路由、配置和发布变更记录。",
    "limitation:limited_time_windows": "补充更多时间窗口或受控恢复对照。",
}


def _value(value: object) -> str:
    if value is None:
        return "缺失"
    if isinstance(value, bool):
        return "是" if value else "否"
    return _LABELS.get(str(value), str(value))


def _localize(text: str) -> str:
    # Fixed UI vocabulary only; never parse model prose to infer claim meaning.
    for token in ("playback_success_rate", "play_success_rate", "previous_window",
                  "previous_day", "current_window", "cdn="):
        text = text.replace(token, _LABELS.get(token, "CDN="))
    return text


def _scope(item: Mapping, *, exclude: tuple[str, ...] = ()) -> str:
    scope = item.get("scope") or item.get("facts", {}).get("scope") or {}
    parts = []
    for key, value in scope.items():
        if key in ("group_dimensions", *exclude) or value in (None, "", "ALL"):
            continue
        if key == "windows":
            parts.append(" / ".join(_value(window) for window in value))
        elif isinstance(value, (str, int, float)):
            parts.append(f"{_value(key)}：{_value(value)}")
    return "；".join(parts)


def _claim_scope(bundle: Mapping | None) -> str:
    """Display the validated bundle boundary, including its entity target."""
    if not bundle:
        return ""
    scope = bundle.get("scope") or {}
    parts = (
        [str(scope["region"])]
        if scope.get("region") not in (None, "", "ALL")
        else []
    )
    if bundle.get("bundle_type") != "regional":
        targets = bundle.get("target_entities") or {}
        for dimension, values in targets.items():
            if not isinstance(values, (list, tuple)):
                values = [values]
            parts.extend(
                str(value) if dimension == "cdn" else f"{_value(dimension)} {value}"
                for value in values
            )
    windows = scope.get("windows") or []
    if windows:
        parts.append("、".join(_value(window) for window in windows))
    return " / ".join(parts)


def _number(value: int | float, *, scale: int = 1, fixed: bool = False) -> str:
    rendered = f"{value * scale:.2f}"
    return rendered if fixed else rendered.rstrip("0").rstrip(".")


def _comparison(item: Mapping) -> str:
    facts = item["facts"]
    baseline, current, delta = (
        facts.get(key) for key in ("baseline_value", "current_value", "delta")
    )
    if not all(isinstance(value, (int, float)) for value in (baseline, current, delta)):
        return ""  # No comparison can be invented from current-only facts.
    unit = item.get("unit")
    ratio = unit == "ratio"
    percent = unit in {"percent", "percentage"}
    scale = 100 if ratio else 1
    suffix = "%" if ratio or percent else (f" {unit}" if unit else "")
    delta_unit = " 个百分点" if ratio or percent else suffix
    direction = "下降" if delta < 0 else "上升" if delta > 0 else "持平"
    subject = _value(facts.get("group") or "整体")
    line = (
        f"{subject} {_value(facts.get('metric'))}："
        f"{_value(facts.get('baseline_window'))} "
        f"{_number(baseline, scale=scale, fixed=ratio or percent)}{suffix}"
        f" → {_value(facts.get('current_window'))} "
        f"{_number(current, scale=scale, fixed=ratio or percent)}{suffix}，"
        f"{direction} {_number(abs(delta), scale=scale, fixed=ratio or percent)}"
        f"{delta_unit}"
    )
    if facts.get("is_largest_decline") is True:
        line += "（所选成对分组中降幅最大）"
    scope = _scope(item, exclude=("windows", facts.get("dimension", "")))
    return f"{scope}：{line}。" if scope else f"{line}。"


def _comparison_key(item: Mapping) -> str:
    """Identify an existing metric/group and its non-window scope for display."""
    facts = item.get("facts", {})
    scope = {key: value for key, value in item.get("scope", {}).items()
             if key != "windows"}
    return json.dumps((facts.get("metric"), item.get("unit"),
                       item.get("aggregation_semantics"),
                       facts.get("dimension"), facts.get("group"),
                       facts.get("filters"), scope), sort_keys=True, default=str)


def _comparison_trend(item: Mapping) -> str:
    facts = item["facts"]
    if facts.get("classification") in {"declined", "improved", "unchanged"}:
        return facts["classification"]
    delta = facts.get("delta")
    if not isinstance(delta, (int, float)):
        return "unknown"
    return "declined" if delta < 0 else "improved" if delta > 0 else "unchanged"


def _visible_comparisons(data: list[Mapping]) -> tuple[list[Mapping], list[str]]:
    """Compress a secondary baseline only when every scoped trend matches."""
    comparisons = [item for item in data if item.get("kind") in {
        "metric_comparison", "group_comparison"}]
    primary = [item for item in comparisons
               if item.get("facts", {}).get("is_primary_comparison") is True]
    secondary = [item for item in comparisons if item not in primary]
    if not primary or not secondary:
        return comparisons, []
    primary_trends = {
        _comparison_key(item): _comparison_trend(item) for item in primary
    }
    by_baseline: dict[str, list[Mapping]] = {}
    for item in secondary:
        by_baseline.setdefault(
            str(item["facts"].get("baseline_window")), []
        ).append(item)
    retained, notes = list(primary), []
    for baseline, items in by_baseline.items():
        trends = {_comparison_key(item): _comparison_trend(item) for item in items}
        if (len(trends) == len(items) == len(primary_trends)
                and trends == primary_trends and "unknown" not in trends.values()):
            notes.append(f"{_value(baseline)}与主基线对比在同一指标、分组和作用域内呈现相同趋势。")
        else:
            retained.extend(items)
    return retained, notes


def _recommendation_lines(
    claim: Mapping, bundle: Mapping | None, limitation_by_id: Mapping,
) -> list[str]:
    """Turn validated recommendation gaps into short scoped actions."""
    scope = _claim_scope(bundle)
    prefix = f"{scope}：" if scope else ""
    support = claim.get("supporting_evidence_ids", [])
    return [prefix + action for evidence_id in support
            if (action := _RECOMMENDATION_ACTIONS.get(str(evidence_id)))
            and evidence_id in limitation_by_id]


def _distribution(values: object) -> str:
    if isinstance(values, Mapping):
        values = [{"value": key, "count": count} for key, count in values.items()]
    return "、".join(
        f"{_value(entry['value'])} {entry['count']}"
        for entry in values or []
    )


def _data_line(item: Mapping) -> str:
    kind, facts = item.get("kind"), item.get("facts", {})
    if kind in {"metric_comparison", "group_comparison"}:
        return _comparison(item)
    scope = _scope(item)
    prefix = f"{scope}：" if scope else ""
    if kind == "alarm_status_distribution":
        parts = [f"告警共 {facts['total']} 条",
                 f"状态：{_distribution(facts.get('distribution'))}"]
        for key, label in (("severity_distribution", "级别"),
                           ("error_code_distribution", "错误码")):
            if facts.get(key):
                parts.append(f"{label}：{_distribution(facts[key])}")
        return prefix + "；".join(parts) + "。"
    if kind in {"reviewed_tool_result_summary", "reviewed_result_summary"}:
        parts = []
        if facts.get("row_count") is not None:
            subject = "结构化日志" if facts.get("tool_name") == "query_logs" else ""
            parts.append(f"{subject}查询返回 {facts['row_count']} 行")
        scope_values = item.get("scope") or facts.get("scope") or {}
        for column in facts.get("columns", []):
            name = column["column"]
            scoped = scope_values.get(name)
            if name == "window_name" and len(scope_values.get("windows", [])) == 1:
                scoped = scope_values["windows"][0]
            if scoped not in (None, "", "ALL") and (
                column.get("value") == scoped or column.get("values") == [
                    {"value": scoped, "count": facts.get("row_count")}
                ]
            ):
                continue  # Scope already states this value for every returned row.
            label = _value(column["column"])
            if column["kind"] == "scalar":
                parts.append(f"{label}：{_value(column.get('value'))}")
            elif column["kind"] == "numeric_range":
                parts.append(f"{label}范围：{column['minimum']}～{column['maximum']}")
            elif column.get("values"):
                parts.append(f"{label}分布：{_distribution(column['values'])}")
        return prefix + "；".join(parts) + "。" if parts else ""
    return ""


def _selected_ids(result: FinalAnswerResult) -> set[str]:
    """Recover *selection*, not facts, from the renderer's exact citation format.

    FinalAnswerResult retains claims but not the top-level evidence selection.
    In particular knowledge-only answers can have no claims. Match citations
    for known IDs and complete canonical lines; never interpret answer prose.
    """
    pack = result.evidence_pack
    ids = {item["evidence_id"] for item in pack.get("knowledge_evidence", [])}
    exact_lines = {}
    for item in pack.get("data_evidence", []):
        sources = ",".join(map(str, item.get("source_task_ids", [])))
        exact_lines[f"- {item['statement']} [data:{sources}]"] = item["evidence_id"]
    for item in pack.get("limitations", []):
        sources = ", ".join(map(str, item.get("source_identifiers", [])))
        exact_lines[f"- {item['statement']} [sources: {sources}]"] = (
            item["limitation_id"]
        )
    selected = set()
    in_section = False
    for line in result.answer.splitlines():
        if line in {"DATA EVIDENCE", "KNOWLEDGE EVIDENCE", "INFERENCE", "LIMITATION"}:
            in_section = line == "KNOWLEDGE EVIDENCE"
        elif in_section:
            selected.update(eid for eid in ids if line.endswith(f" [{eid}]"))
        elif line in exact_lines:
            selected.add(exact_lines[line])
    return selected


def _wrap(text: str, width: int = 88) -> str:
    """Wrap Chinese as terminal cells, without adding a dependency."""
    lines, current, cells = [], "", 0
    tokens = []
    for ascii_word, characters in groupby(
        text, lambda char: char.isascii() and not char.isspace()
    ):
        word = "".join(characters)
        tokens.extend([word] if ascii_word and len(word) < width - 2 else word)
    for token in tokens:
        size = sum(
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
            for char in token
        )
        if token == "\n" or cells + size > width:
            lines.append(current.rstrip())
            current, cells = "  ", 2
            if token == "\n":
                continue
        current += token
        cells += size
    if current.strip():
        lines.append(current.rstrip())
    return "\n".join(lines)


def _section(title: str, lines: list[str]) -> str:
    lines = list(dict.fromkeys(line for line in lines if line))
    if not lines:
        return ""
    return "## " + title + "\n\n" + "\n".join(
        _wrap("- " + line) for line in lines
    )


def format_human_answer(result: FinalAnswerResult) -> str:
    """Format a completed, validated result without changing Agent semantics."""
    if (not result.success or result.validator_result.get("valid") is not True
            or result.validator_result.get("violations")):
        return HUMAN_ANSWER_UNAVAILABLE
    pack = result.evidence_pack
    if not pack:
        return HUMAN_ANSWER_UNAVAILABLE
    support = {str(eid) for claim in result.claims
               for key in ("supporting_evidence_ids", "subject_evidence_ids")
               for eid in claim.get(key, [])}
    selected = support | _selected_ids(result)
    data = [item for item in pack.get("data_evidence", [])
            if item.get("required") or item["evidence_id"] in selected]
    knowledge = [item for item in pack.get("knowledge_evidence", [])
                 if item["evidence_id"] in selected]
    limitations = [item for item in pack.get("limitations", [])
                   if item.get("required") or item["limitation_id"] in selected]
    # Local copies only: translated labels do not mutate pack/provenance/claims.
    evidence_by_id = {
        item["evidence_id"]: {**item, "title": _value(item.get("title", ""))}
        for item in [*data, *knowledge]
    }
    limit_by_id = {item["limitation_id"]: {**item, "statement": _LIMITS.get(
        item["limitation_id"], item.get("statement", ""))} for item in limitations}
    codes, phrases = _knowledge_signals(pack)
    knowledge_facts = {
        item["evidence_id"]: _project_knowledge_evidence(
            item, codes=codes, phrases=phrases
        )[0]["supported_fact"]
        for item in knowledge
    }
    bundles = {
        item["bundle_id"]: item
        for item in result.evidence_projection.get("evidence_bundles", [])
    }

    visible_comparisons, comparison_notes = _visible_comparisons(data)
    visible_ids = {item["evidence_id"] for item in visible_comparisons}
    evidence_lines, seen = [], set()
    for item in data:
        if (item.get("kind") in {"metric_comparison", "group_comparison"}
                and item["evidence_id"] not in visible_ids):
            continue
        # Ignore representation/lineage IDs only at the UI boundary; retain scope
        # and facts so different windows, subsets and correction values survive.
        facts = {key: value for key, value in item.get("facts", {}).items()
                 if key not in {"provenance", "comparison_id", "execution_source",
                                "evidence_role", "tool_name"}}
        key = json.dumps([item.get("scope"), facts], sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            evidence_lines.append(_data_line(item))
    evidence_lines.extend(comparison_notes)

    conclusions = [_comparison(item) for item in visible_comparisons
                   if item.get("kind") == "metric_comparison"
                   or item.get("facts", {}).get("is_largest_decline") is True]
    if len(evidence_lines) - len(conclusions) >= 4:
        evidence_lines = [line for line in evidence_lines if line not in conclusions]
    causes, recommendations = [], []
    for claim in result.claims:
        claim_type = claim.get("claim_type")
        if (claim_type in {"knowledge", "observation"}
                and claim.get("predicate") != "stable_control"):
            continue  # Facts appear once, rather than repeating observation prose.
        bundle = bundles.get(claim.get("bundle_id"))
        rendered = _localize(_render_claim_statement(
            claim, evidence_by_id, limit_by_id, knowledge_facts, bundle=bundle,
        ))
        core_prefix = _localize(_bundle_render_prefix(bundle))
        if core_prefix and rendered.startswith(core_prefix):
            rendered = rendered[len(core_prefix):]
        scope = _claim_scope(bundle)
        if scope:
            rendered = f"{scope}：{rendered}"
        if claim_type == "recommendation":
            recommendations.extend(
                _recommendation_lines(claim, bundle, limit_by_id) or [rendered]
            )
        else:
            label = {"observation": "已观察事实", "correlation": "相关性",
                     "hypothesis": "待验证假设", "causal_claim": "因果结论"}.get(claim_type, "")
            causes.append(f"{label}：{rendered}")

    knowledge_lines = ["领域知识（非实测数据）：" + fact for fact in knowledge_facts.values()]
    if not data:
        conclusions.extend(knowledge_lines)
    else:
        if knowledge:
            causes.insert(0, "领域知识依据（非实测数据）：" + "；".join(
                dict.fromkeys(_value(item["title"]) for item in knowledge)
            ) + "。")
    if (not any(claim.get("claim_type") == "causal_claim" for claim in result.claims)
            and any(claim.get("claim_type") in {"correlation", "hypothesis"}
                    for claim in result.claims)):
        conclusions.append("当前证据不足以确认根因；相关性与待验证假设不等于因果结论。")
    # Keep all gaps, while grouping the missing telemetry into one boundary item.
    gap_ids = {"limitation:missing_trace_data", "limitation:missing_origin_metrics",
               "limitation:missing_routing_change_records"}
    boundaries = [
        item["statement"]
        for eid, item in limit_by_id.items()
        if eid not in gap_ids
    ]
    gaps = [item["statement"] for eid, item in limit_by_id.items() if eid in gap_ids]
    if gaps:
        boundaries.append(" ".join(gaps))
    if not conclusions and not evidence_lines and not knowledge_lines:
        conclusions.append("本次没有可展示的已验证事实。")
    return "\n\n".join(section for section in (
        _section("分析结论", conclusions), _section("关键证据", evidence_lines),
        _section("可能原因", causes), _section("排查建议", recommendations),
        _section("证据边界", boundaries),
    ) if section)
