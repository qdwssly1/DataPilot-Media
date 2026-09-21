from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Mapping

import pytest

from datapilot.agent.analyst import (
    FINAL_ANSWER_RESPONSE_SCHEMA,
    AnalystOutputError,
    _parse_final_selection,
)
from datapilot.agent.evidence import (
    build_final_evidence_pack,
    final_claim_violations,
    render_final_answer,
)
from domains.media.runtime.tools import build_alarm_evidence
from tests.media.test_evidence_accuracy import (
    _alarm_result,
    _qoe_result,
    _raw_alarm_tool_result,
    _synthetic_knowledge,
)
from tests.media.test_metric_window_binding import _bound_result, _roles


def _payload(claim: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "data_evidence_ids": [
            "data:comparison:1:group:2",
            "data:status:1",
        ],
        "knowledge_evidence_ids": ["knowledge:synthetic::e302"],
        "inferences": [dict(claim)],
        "limitation_ids": ["limitation:correlation_not_causation"],
        "source_task_ids": ["analysis"],
    }


def _general_claim(
    *,
    claim_type: str = "observation",
    support: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "bundle_id": "bundle:test",
        "claim_type": claim_type,
        "predicate": "general",
        "polarity": "neutral",
        "subject_evidence_ids": [],
        "supporting_evidence_ids": support
        or ["data:comparison:1:group:2"],
    }


def _parse(claim: Mapping[str, Any]) -> dict[str, Any]:
    return _parse_final_selection(
        json.dumps(_payload(claim), ensure_ascii=False),
        task_id="answer",
        allowed_source_ids={"analysis"},
    )


def _selection(
    pack: Mapping[str, Any],
    claim: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "data_evidence_ids": [
            str(item["evidence_id"]) for item in pack["data_evidence"]
        ],
        "knowledge_evidence_ids": [
            str(item["evidence_id"]) for item in pack["knowledge_evidence"]
        ],
        "inferences": [dict(claim)],
        "limitation_ids": [
            str(item["limitation_id"]) for item in pack["limitations"]
        ],
        "source_task_ids": ["analysis"],
    }


def _multi_baseline_pack() -> dict[str, Any]:
    roles = _roles(["previous_day", "previous_window"], primary=None)
    baseline = _bound_result(
        "baseline",
        ["previous_day", "previous_window"],
        roles=roles,
    )
    current = _bound_result("current", ["current_window"], roles=roles)
    return build_final_evidence_pack(
        [baseline, current],
        _synthetic_knowledge(),
    )


def test_schema_is_a_general_or_stable_control_discriminated_union() -> None:
    variants = FINAL_ANSWER_RESPONSE_SCHEMA["properties"]["inferences"][
        "items"
    ]["oneOf"]

    assert len(variants) == 2
    assert variants[0]["properties"]["predicate"] == {"type": "string", "const": "general"}
    assert variants[0]["properties"]["polarity"]["const"] == "neutral"
    assert variants[0]["properties"]["subject_evidence_ids"]["maxItems"] == 0
    assert variants[1]["properties"]["predicate"]["const"] == "stable_control"
    assert variants[1]["properties"]["subject_evidence_ids"]["minItems"] == 1
    assert "statement" not in variants[0]["properties"]
    assert "statement" not in variants[1]["properties"]
    assert "bundle_id" in variants[0]["required"]
    assert "bundle_id" in variants[1]["required"]


def test_general_neutral_without_subjects_parses() -> None:
    parsed = _parse(_general_claim())

    assert parsed["inferences"][0]["predicate"] == "general"


@pytest.mark.parametrize("polarity", ["positive", "negative"])
def test_general_non_neutral_is_rejected_during_parsing(polarity: str) -> None:
    claim = _general_claim()
    claim["polarity"] = polarity

    with pytest.raises(AnalystOutputError, match="neutral polarity"):
        _parse(claim)


def test_general_subjects_are_rejected_during_parsing() -> None:
    claim = _general_claim()
    claim["subject_evidence_ids"] = ["data:comparison:1:group:2"]

    with pytest.raises(AnalystOutputError, match="cannot identify subjects"):
        _parse(claim)


def test_free_statement_is_rejected_during_parsing() -> None:
    claim = _general_claim()
    claim["statement"] = "999 sessions prove this is the root cause."

    with pytest.raises(AnalystOutputError, match="fields do not match"):
        _parse(claim)


def test_negative_stable_control_with_subject_parses() -> None:
    claim = {
        "bundle_id": "bundle:test",
        "claim_type": "observation",
        "predicate": "stable_control",
        "polarity": "negative",
        "subject_evidence_ids": ["data:comparison:1:group:2"],
        "supporting_evidence_ids": ["data:comparison:1:group:2"],
    }

    assert _parse(claim)["inferences"][0] == claim


def test_stable_control_neutral_is_rejected_during_parsing() -> None:
    claim = {
        "bundle_id": "bundle:test",
        "claim_type": "observation",
        "predicate": "stable_control",
        "polarity": "neutral",
        "subject_evidence_ids": ["data:comparison:1:group:2"],
        "supporting_evidence_ids": ["data:comparison:1:group:2"],
    }

    with pytest.raises(AnalystOutputError, match="positive or negative"):
        _parse(claim)


def test_stable_control_without_subjects_is_rejected_during_parsing() -> None:
    claim = {
        "bundle_id": "bundle:test",
        "claim_type": "observation",
        "predicate": "stable_control",
        "polarity": "negative",
        "subject_evidence_ids": [],
        "supporting_evidence_ids": ["data:comparison:1:group:2"],
    }

    with pytest.raises(AnalystOutputError, match="requires subject evidence"):
        _parse(claim)


def test_stable_control_non_comparison_subject_is_rejected_by_full_pack() -> None:
    pack = build_final_evidence_pack([_alarm_result()], [])
    claim = {
        "claim_type": "observation",
        "predicate": "stable_control",
        "polarity": "negative",
        "subject_evidence_ids": ["data:status:1"],
        "supporting_evidence_ids": ["data:status:1"],
    }

    violations = final_claim_violations(_selection(pack, claim), pack)

    assert any("not group comparison evidence" in item for item in violations)


@pytest.mark.parametrize(
    ("evidence_id", "baseline"),
    [
        ("data:comparison:1:group:2", "previous_day"),
        ("data:comparison:2:group:2", "previous_window"),
    ],
)
def test_single_comparison_scope_is_rendered_from_evidence(
    evidence_id: str,
    baseline: str,
) -> None:
    pack = _multi_baseline_pack()
    selection = _selection(
        pack,
        _general_claim(support=[evidence_id]),
    )

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert f"以 {baseline} 为基线" in answer
    assert "CDN-B" in answer


def test_consistent_multi_baseline_largest_decline_summary_is_valid() -> None:
    pack = _multi_baseline_pack()
    selection = _selection(
        pack,
        _general_claim(
            support=[
                "data:comparison:1:group:2",
                "data:comparison:2:group:2",
            ]
        ),
    )

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert "previous_day 和 previous_window" in answer
    assert "CDN-B" in answer
    assert "下降幅度最大的分组" in answer


def test_negative_stable_control_is_scoped_across_both_baselines() -> None:
    pack = _multi_baseline_pack()
    subjects = [
        "data:comparison:1:group:2",
        "data:comparison:2:group:2",
    ]
    claim = {
        "claim_type": "observation",
        "predicate": "stable_control",
        "polarity": "negative",
        "subject_evidence_ids": subjects,
        "supporting_evidence_ids": subjects,
    }
    selection = _selection(pack, claim)

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert "previous_day 和 previous_window" in answer
    assert "CDN-B 均出现下降" in answer
    assert "不能作为稳定、未受影响的对照组" in answer


def test_inconsistent_multi_baseline_summary_is_rejected() -> None:
    pack = deepcopy(_multi_baseline_pack())
    second = next(
        item
        for item in pack["data_evidence"]
        if item["evidence_id"] == "data:comparison:2:group:2"
    )
    second["facts"]["is_largest_decline"] = False
    selection = _selection(
        pack,
        _general_claim(
            support=[
                "data:comparison:1:group:2",
                "data:comparison:2:group:2",
            ]
        ),
    )

    violations = final_claim_violations(selection, pack)

    assert any("inconsistent multi-baseline" in item for item in violations)


def test_debug_window_wording_does_not_change_evidence_scope() -> None:
    pack = _multi_baseline_pack()
    claim = _general_claim(support=["data:comparison:1:group:2"])
    claim["statement"] = "Use previous_year as the baseline."
    selection = _selection(pack, claim)

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert "previous_year" not in answer
    assert "previous_window" in answer


def test_scope_incompatible_alarm_evidence_is_rejected() -> None:
    left = _raw_alarm_tool_result()
    right = deepcopy(left)
    right.task_id = "north_alarms"
    right.tool_input["region"] = "华北"
    for row in right.rows:
        row["region"] = "华北"
    right.tool_metadata["alarm_evidence"] = build_alarm_evidence(right.rows)
    pack = build_final_evidence_pack([left, right], [])
    status_ids = [
        str(item["evidence_id"])
        for item in pack["data_evidence"]
        if item["kind"] == "alarm_status_distribution"
    ]
    selection = _selection(
        pack,
        _general_claim(claim_type="correlation", support=status_ids),
    )

    violations = final_claim_violations(selection, pack)

    assert any("incompatible region" in item for item in violations)


def test_knowledge_claim_is_valid_without_data() -> None:
    pack = build_final_evidence_pack([], _synthetic_knowledge())
    knowledge_id = str(pack["knowledge_evidence"][0]["evidence_id"])
    selection = _selection(
        pack,
        _general_claim(claim_type="knowledge", support=[knowledge_id]),
    )

    assert final_claim_violations(selection, pack) == []


def test_knowledge_renderer_uses_supported_fact_not_debug_wording() -> None:
    knowledge = [
        {
            "chunk_id": "typed::e302",
            "title": "E302",
            "category": "error_code",
            "source": "synthetic.md",
            "text": (
                "E302表示CDN upstream timeout类症状。 "
                "检查上游链路和超时指标。 "
                "FULL_CHUNK_TAIL_MUST_NOT_RENDER。"
            ),
        }
    ]
    pack = build_final_evidence_pack([], knowledge)
    knowledge_id = str(pack["knowledge_evidence"][0]["evidence_id"])
    claim = _general_claim(claim_type="knowledge", support=[knowledge_id])
    claim["statement"] = "E302 is the proven root cause."
    selection = _selection(pack, claim)

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert "upstream timeout类症状" in answer
    assert "proven root cause" not in answer
    assert "FULL_CHUNK_TAIL_MUST_NOT_RENDER" not in answer


def test_causal_claim_without_causal_evidence_is_rejected() -> None:
    pack = build_final_evidence_pack([_qoe_result()], [])
    claim = _general_claim(
        claim_type="causal_claim",
        support=["data:comparison:1:group:2"],
    )

    violations = final_claim_violations(_selection(pack, claim), pack)

    assert any("lacks approved causal evidence" in item for item in violations)


def test_hypothesis_debug_causal_wording_never_reaches_final_output() -> None:
    pack = build_final_evidence_pack([_qoe_result(), _alarm_result()], [])
    claim = _general_claim(
        claim_type="hypothesis",
        support=[
            "data:comparison:1:group:2",
            "limitation:missing_origin_metrics",
        ],
    )
    claim["statement"] = "Origin pressure caused the incident."
    selection = _selection(pack, claim)

    answer = render_final_answer(pack, selection)

    assert final_claim_violations(selection, pack) == []
    assert "caused the incident" not in answer
    assert "尚不能确认其为根因" in answer


@pytest.mark.parametrize(
    ("claim_type", "support"),
    [
        ("observation", ["data:comparison:1:group:2"]),
        (
            "correlation",
            ["data:comparison:1:group:2", "data:status:1"],
        ),
        (
            "hypothesis",
            [
                "data:comparison:1:group:2",
                "limitation:missing_origin_metrics",
            ],
        ),
        (
            "recommendation",
            [
                "knowledge:synthetic::e302",
                "limitation:missing_origin_metrics",
            ],
        ),
    ],
)
def test_typed_claim_taxonomy_contracts_remain_valid(
    claim_type: str,
    support: list[str],
) -> None:
    pack = build_final_evidence_pack(
        [_qoe_result(), _alarm_result()],
        _synthetic_knowledge(),
    )
    selection = _selection(
        pack,
        _general_claim(claim_type=claim_type, support=support),
    )

    assert final_claim_violations(selection, pack) == []


def test_phase39_real_shape_b3_offline_replay_reaches_memory() -> None:
    from evals.media.run_tool_offline_cases import (  # noqa: PLC0415
        _run_composite,
        _runtime,
    )

    wren, router = _runtime()
    result = _run_composite(
        wren,
        router,
        planner_shape="multi_baseline_no_primary",
    )

    pack = result["evidence_pack"]
    stats = result["evidence_projection_stats"]
    lineage = {
        item["task_id"]: item for item in pack["reviewed_evidence"]
    }
    comparisons = [
        item
        for item in pack["data_evidence"]
        if item["kind"] == "group_comparison"
    ]

    assert result["success"] is True
    assert result["validator_result"] == {
        "valid": True,
        "violations": [],
        "retry_count": 0,
        "projection_guard_valid": True,
        "bundle_guard_valid": True,
        "bundle_violations": [],
        "validated_against": "full_evidence_pack",
    }
    assert result["memory_updated"] is True
    assert result["reviewer_decisions"].count("retry") == 1
    assert result["model_calls"].count("sql_agent") == 1
    assert {
        source["role"]
        for source in lineage["logs"]["evidence_sources"]
    } == {"base_tool_evidence", "correction_evidence"}
    assert {item["facts"]["baseline_window"] for item in comparisons} == {
        "previous_day",
        "previous_window",
    }
    assert all(
        item["facts"]["is_primary_comparison"] is False
        for item in comparisons
    )
    assert len(pack["knowledge_evidence"]) == 5
    assert len(pack["limitations"]) == 5
    assert stats["final_prompt_chars"] < stats["budget_limit"] == 20_000
    assert stats["budget_remaining"] > 0
    assert "previous_day 和 previous_window" in result["final_answer"]
    assert all("statement" not in claim for claim in result["final_claims"])
