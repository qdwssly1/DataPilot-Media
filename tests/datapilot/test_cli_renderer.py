from copy import deepcopy

import pytest

from datapilot.agent.evidence import (
    build_final_evidence_pack,
    build_final_evidence_projection,
    evidence_bundle_claim_violations,
    final_claim_violations,
    render_final_answer,
)
from datapilot.agent.state import FinalAnswerResult, SQLResult
from datapilot.cli_renderer import HUMAN_ANSWER_UNAVAILABLE, format_human_answer
from tests.media.test_evidence_accuracy import (
    _log_tool_result,
    _qoe_result,
    _raw_alarm_tool_result,
)
from tests.media.test_metric_window_binding import _bound_result, _roles
from tests.media.test_canonical_evidence_deduplication import _overlapping_qoe_results


def _validated(pack, claims=(), knowledge_ids=None):
    selection = {
        "data_evidence_ids": [item["evidence_id"] for item in pack["data_evidence"]],
        "knowledge_evidence_ids": knowledge_ids if knowledge_ids is not None else [
            item["evidence_id"] for item in pack["knowledge_evidence"]
        ],
        "inferences": list(claims),
        "limitation_ids": [item["limitation_id"] for item in pack["limitations"]],
        "source_task_ids": [],
    }
    assert final_claim_violations(selection, pack) == []
    return FinalAnswerResult(
        task_id="final", answer=render_final_answer(pack, selection),
        evidence_pack=pack, claims=list(claims),
        validator_result={"valid": True, "violations": []},
    )


def _claim(kind, support, **kwargs):
    return {"claim_type": kind, "predicate": "general", "polarity": "neutral",
            "supporting_evidence_ids": support, "subject_evidence_ids": [], **kwargs}


def test_comparisons_statuses_and_logs_preserve_validated_values_without_ids():
    logs = SQLResult(task_id="logs", sql="SELECT COUNT(*) AS log_count FROM log_events",
                     rows=[{"log_count": 4, "distinct_trace_count": 3}],
                     columns=["log_count", "distinct_trace_count"], row_count=1)
    pack = build_final_evidence_pack(
        [_qoe_result(), _raw_alarm_tool_result(), logs], [],
    )
    result = _validated(pack)
    before = deepcopy(result)
    text = format_human_answer(result)
    assert result == before
    assert "分析结论" in text and "关键证据" in text
    flat = text.replace("\n", "").replace("  ", "")
    assert "93.33%" in text and "71.67%" in text and "21.67 个百分点" in flat
    for group, baseline, current, delta in (
        ("CDN-A", "95.00%", "85.00%", "10.00"),
        ("CDN-B", "95.00%", "50.00%", "45.00"),
        ("CDN-C", "90.00%", "80.00%", "10.00"),
    ):
        matching = [line for line in text.split("。") if f"{group} 播放成功率" in line]
        assert matching
        assert all(baseline in line and current in line for line in matching)
        assert f"下降 {delta} 个百分点" in flat
    assert "告警共 3 条" in flat
    assert "待处理 1" in flat and "排查中 1" in flat and "已解决 1" in flat
    assert "日志数：4" in flat and "不同 trace 数：3" in flat
    for hidden in ("data:", "knowledge:", "supports:", "SELECT", "limitation:"):
        assert hidden not in text
    assert "排查建议" not in text and "可能原因" not in text


def test_claim_types_use_structured_semantics_not_model_debug_text():
    knowledge = [{"chunk_id": "sop", "title": "CDN 排查规程", "source": "sop.md",
                  "category": "sop", "text": "检查源站超时指标；相关性不代表因果。"}]
    pack = build_final_evidence_pack([_qoe_result()], knowledge)
    groups = {item["facts"].get("group"): item["evidence_id"]
              for item in pack["data_evidence"] if item["kind"] == "group_comparison"}
    knowledge_id = pack["knowledge_evidence"][0]["evidence_id"]
    limitation_id = pack["limitations"][0]["limitation_id"]
    claims = [
        _claim("observation", [groups["CDN-A"], groups["CDN-C"]],
               predicate="stable_control", polarity="negative",
               subject_evidence_ids=[groups["CDN-A"], groups["CDN-C"]]),
        _claim("correlation", [groups["CDN-A"], groups["CDN-B"]]),
        _claim("hypothesis", [groups["CDN-B"], knowledge_id, limitation_id]),
        _claim("recommendation", [groups["CDN-B"], knowledge_id, limitation_id]),
    ]
    result = _validated(pack, claims)
    for claim in result.claims:
        claim["statement"] = "已确认根因。CDN-A 健康。新增 999999 条日志。"
    text = format_human_answer(result)
    flat = text.replace("\n", "").replace("  ", "")
    assert "已观察事实：" in text and "相关性：" in text and "待验证假设：" in text
    assert "排查建议" in text
    assert "不能作为稳定、未受影响的对照组" in flat
    assert "当前证据不足以确认根因" in flat
    assert "999999" not in text and "已确认根因" not in text


def test_knowledge_only_uses_exact_selected_ids_not_every_retrieval_hit():
    pack = build_final_evidence_pack([], [
        {"chunk_id": "definition", "title": "示例错误码定义", "source": "a.md",
         "category": "error_code", "text": "本合成域错误码表示上游超时，并非行业统一标准。"},
        {"chunk_id": "unselected", "title": "未选中的知识", "source": "b.md",
         "category": "metric", "text": "不应自动展示全部检索命中。"},
    ])
    result = _validated(
        pack, knowledge_ids=[pack["knowledge_evidence"][0]["evidence_id"]]
    )
    text = format_human_answer(result)
    assert "分析结论" in text and "领域知识（非实测数据）" in text
    assert "上游超时" in text and "不是数据库中已观察到的事实" in text
    assert "未选中的知识" not in text
    assert "关键证据" not in text and "可能原因" not in text


@pytest.mark.parametrize("rows", [[], [{"count": 5}]])
def test_empty_or_single_query_does_not_invent_comparisons_or_other_sections(rows):
    pack = build_final_evidence_pack([SQLResult(
        task_id="q1", sql="SELECT COUNT(*) AS count FROM example",
        rows=rows, columns=["count"], row_count=len(rows),
    )], [])
    text = format_human_answer(_validated(pack))
    assert f"查询返回 {len(rows)} 行" in text
    assert "降幅最大" not in text and "排查建议" not in text and "告警共" not in text
    assert "None" not in text and "{}" not in text


def test_current_only_cannot_be_presented_as_largest_decline():
    result = _qoe_result()
    result.rows = [row for row in result.rows if row["window_name"] == "current_window"]
    result.row_count = len(result.rows)
    text = format_human_answer(_validated(build_final_evidence_pack([result], [])))
    assert "降幅最大" not in text and "百分点" not in text


def test_primary_baseline_compresses_only_the_same_scoped_trend():
    result = _bound_result(
        "qoe", ["previous_day", "previous_window", "current_window"],
        roles=_roles(["previous_day", "previous_window"], primary="previous_window"),
    )
    pack = build_final_evidence_pack([result], [])
    before = deepcopy(pack)
    text = format_human_answer(_validated(pack))
    assert "前一天同期与主基线对比" in text
    assert "CDN-B 播放成功率：上一窗口 95.00% → 当前窗口 50.00%" in text.replace("\n", "")
    assert "CDN-B 播放成功率：前一天同期" not in text
    assert pack == before  # The secondary comparison is still canonical evidence.
    assert len([item for item in pack["data_evidence"]
                if item["kind"] in {"metric_comparison", "group_comparison"}]) == 8


def test_multiple_baselines_without_primary_stay_explicit_and_separate():
    result = _bound_result(
        "qoe", ["previous_day", "previous_window", "current_window"],
        roles=_roles(["previous_day", "previous_window"], primary=None),
    )
    text = format_human_answer(_validated(build_final_evidence_pack([result], [])))
    flat = text.replace("\n", "")
    assert "前一天同期与主基线对比" not in text
    assert "CDN-B 播放成功率：前一天同期 90.00% → 当前窗口 50.00%" in flat
    assert "CDN-B 播放成功率：上一窗口 95.00% → 当前窗口 50.00%" in flat


def test_secondary_baseline_with_different_trend_is_not_compressed():
    result = _bound_result(
        "qoe", ["previous_day", "previous_window", "current_window"],
        roles=_roles(["previous_day", "previous_window"], primary="previous_window"),
    )
    for row in result.rows:
        if row["window_name"] == "previous_day" and row["cdn"] == "CDN-A":
            row["successful_sessions"] = 16
            row["failed_sessions"] = 4
            row["playback_success_rate"] = 0.80
    text = format_human_answer(_validated(build_final_evidence_pack([result], [])))
    flat = text.replace("\n", "")
    assert "前一天同期与主基线对比" not in text
    assert "CDN-A 播放成功率：前一天同期 80.00% → 当前窗口 85.00%" in flat
    assert "CDN-A 播放成功率：上一窗口 95.00% → 当前窗口 85.00%" in flat


def test_alarm_error_code_and_window_scopes_are_not_merged_in_display():
    e302 = _raw_alarm_tool_result()
    e401 = deepcopy(e302)
    e401.task_id = "other-code"
    e401.tool_input["error_code"] = "E401"
    for row in e401.rows:
        row["error_code"] = "E401"
    pack = build_final_evidence_pack([e302, e401], [])
    text = format_human_answer(_validated(pack)).replace("\n", "")
    assert text.count("告警共 3 条") == 2
    assert "错误码：E302" in text and "错误码：E401" in text
    assert "告警共 6 条" not in text


def test_recommendation_actions_keep_entity_and_region_bundles_separate():
    pack = build_final_evidence_pack([
        *_overlapping_qoe_results(), _raw_alarm_tool_result(), _log_tool_result(),
    ], [
        {"chunk_id": "e302", "title": "E302 upstream timeout", "source": "codes.md",
         "category": "error_code", "text": "E302 表示上游超时。"},
        {"chunk_id": "cdn-sop", "title": "CDN Playback Success Degradation SOP",
         "source": "sop.md", "category": "troubleshooting_sop",
         "text": "按区域与 CDN 比较播放成功率，并检查源站延迟和路由变更。"},
    ])
    projection, _ = build_final_evidence_projection(pack, max_chars=20_000)
    entity = next(item for item in projection["evidence_bundles"]
                  if item["bundle_type"] == "entity")
    regional = next(item for item in projection["evidence_bundles"]
                    if item["bundle_type"] == "regional")
    knowledge_id = next(item["evidence_id"] for item in pack["knowledge_evidence"]
                        if item["title"] == "CDN Playback Success Degradation SOP")
    data_by_id = {item["evidence_id"]: item for item in pack["data_evidence"]}
    entity_data = next(eid for eid in entity["allowed_evidence_ids"]
                       if data_by_id[eid]["kind"] == "group_comparison")
    regional_data = next(eid for eid in regional["allowed_evidence_ids"]
                         if data_by_id[eid]["kind"] == "metric_comparison")
    entity_claim = _claim("recommendation", [
        entity_data, knowledge_id, *entity["allowed_limitation_ids"]])
    entity_claim["bundle_id"] = entity["bundle_id"]
    regional_claim = _claim("recommendation", [
        regional_data, knowledge_id, *regional["allowed_limitation_ids"]])
    regional_claim["bundle_id"] = regional["bundle_id"]
    claims = [entity_claim, regional_claim]
    assert evidence_bundle_claim_violations({"inferences": claims}, projection) == []
    pack_result = _validated(pack, claims)
    pack_result.evidence_projection = projection
    text = format_human_answer(pack_result)
    assert "华南 / CDN-B" in text
    assert "用请求级 trace" in text
    assert "华南 / 当前窗口" in text
    assert "排查建议" in text
    assert "源站延迟" in text and "路由" in text


def test_ui_only_dedup_retains_different_scope_and_all_underlying_evidence():
    pack = build_final_evidence_pack([_log_tool_result()], [])
    original = pack["data_evidence"][0]
    original["scope"]["region"] = "华南"
    original["facts"]["scope"]["region"] = "华南"
    duplicate = deepcopy(original)
    duplicate["evidence_id"] += ":duplicate"
    duplicate["facts"]["evidence_role"] = "correction_evidence"
    another_region = deepcopy(original)
    another_region["evidence_id"] += ":different-scope"
    another_region["scope"]["region"] = "华北"
    another_region["facts"]["scope"]["region"] = "华北"
    pack["data_evidence"].extend([duplicate, another_region])
    result = _validated(pack)
    before = deepcopy(result)
    text = format_human_answer(result)
    assert text.count("查询返回 4 行") == 2
    assert "华南" in text and "华北" in text
    assert result == before
    assert "不同 trace 数" not in text  # Never count raw samples in the UI.


@pytest.mark.parametrize("success, validator", [
    (False, {"valid": True}), (True, {}),
    (True, {"valid": False}), (True, {"valid": True, "violations": ["scope"]}),
])
def test_unvalidated_result_does_not_leak_arbitrary_final_text(success, validator):
    result = FinalAnswerResult(task_id="a", answer="SELECT secret supports: data:q1",
                               success=success, validator_result=validator)
    assert format_human_answer(result) == HUMAN_ANSWER_UNAVAILABLE
