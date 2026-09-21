from __future__ import annotations

from copy import deepcopy

import pytest

from datapilot.agent.evidence import (
    EvidenceConflictError,
    build_final_evidence_pack,
    build_final_evidence_projection,
    evidence_bundle_claim_violations,
    evidence_projection_claim_violations,
    final_claim_violations,
    render_final_answer,
)
from datapilot.tools.integration import (
    mark_evidence_review_status,
    merge_correction_evidence,
)
from tests.media.test_evidence_accuracy import (
    _log_correction_result,
    _log_tool_result,
    _raw_alarm_tool_result,
    _synthetic_knowledge,
)
from tests.media.test_metric_window_binding import (
    PLAYBACK_BINDING,
    _bound_result,
    _roles,
)


def _overlapping_qoe_results():
    roles = _roles(
        ["previous_window", "previous_day"],
        primary=None,
    )
    current = _bound_result(
        "q_current_qoe",
        ["previous_window", "current_window"],
        roles=roles,
        execution_source="tool",
    )
    baseline = _bound_result(
        "q_baseline_qoe",
        ["previous_day", "previous_window", "current_window"],
        roles=roles,
        execution_source="tool",
    )
    return current, baseline


def _comparisons(pack):
    return [
        item
        for item in pack["data_evidence"]
        if item["kind"] in {"metric_comparison", "group_comparison"}
    ]


def _comparison(pack, *, baseline: str, group: str | None):
    return next(
        item
        for item in _comparisons(pack)
        if item["facts"]["baseline_window"] == baseline
        and item["facts"].get("group") == group
    )


def _full_real_shape_pack():
    current, baseline = _overlapping_qoe_results()
    alarms = _raw_alarm_tool_result()
    mark_evidence_review_status(alarms, "approve")
    logs = merge_correction_evidence(
        _log_tool_result(),
        _log_correction_result(),
    )
    mark_evidence_review_status(logs, "approve")
    return build_final_evidence_pack(
        [current, baseline, alarms, logs],
        _synthetic_knowledge(),
    )


def test_equal_facts_merge_and_keep_all_provenance_paths() -> None:
    current, baseline = _overlapping_qoe_results()
    pack = build_final_evidence_pack([current, baseline], [])
    stats = pack["comparison_canonicalization"]

    assert stats == {
        "comparison_records_before_dedup": 6,
        "comparison_count_before_dedup": 24,
        "canonical_comparison_count": 8,
        "duplicate_comparison_count_removed": 16,
        "overall_before_dedup": 6,
        "overall_after_dedup": 2,
        "group_before_dedup": 18,
        "group_after_dedup": 6,
        "provenance_sources_retained": 24,
    }
    previous_window = _comparison(
        pack,
        baseline="previous_window",
        group="CDN-B",
    )
    provenance = previous_window["facts"]["provenance"]

    assert previous_window["facts"]["baseline_value"] == pytest.approx(0.95)
    assert previous_window["facts"]["current_value"] == pytest.approx(0.50)
    assert previous_window["facts"]["delta"] == pytest.approx(-0.45)
    assert len(previous_window["provenance_sources"]) == 4
    assert set(previous_window["derivation_types"]) == {
        "deterministic_single_task_pair",
        "deterministic_cross_task_pair",
    }
    assert provenance["source_task_ids"] == [
        "q_baseline_qoe",
        "q_current_qoe",
    ]
    assert set(provenance["source_evidence_ids"]) == {
        "source:q_baseline_qoe:tool:query_qoe_metrics",
        "source:q_current_qoe:tool:query_qoe_metrics",
    }
    assert len(provenance["baseline_source_ids"]) == 2
    assert len(provenance["current_source_ids"]) == 2


def test_canonical_ids_do_not_change_when_duplicate_paths_are_added() -> None:
    current, baseline = _overlapping_qoe_results()
    single_source_pack = build_final_evidence_pack([baseline], [])
    overlapping_pack = build_final_evidence_pack([current, baseline], [])

    def ids(pack):
        return {
            (
                item["kind"],
                item["facts"]["baseline_window"],
                item["facts"].get("group"),
            ): (item["evidence_id"], item["canonical_key"])
            for item in _comparisons(pack)
        }

    assert ids(single_source_pack) == ids(overlapping_pack)


def test_same_identity_with_conflicting_values_fails_closed() -> None:
    current, baseline = _overlapping_qoe_results()
    conflicting = deepcopy(baseline)
    target = next(
        row
        for row in conflicting.rows
        if row["window_name"] == "previous_window" and row["cdn"] == "CDN-B"
    )
    target["playback_success_rate"] = 0.90

    with pytest.raises(EvidenceConflictError, match="conflicting canonical"):
        build_final_evidence_pack([current, conflicting], [])


def test_floating_representation_noise_merges_deterministically() -> None:
    current, baseline = _overlapping_qoe_results()
    target = next(
        row
        for row in current.rows
        if row["window_name"] == "current_window" and row["cdn"] == "CDN-B"
    )
    target["playback_success_rate"] = 0.5000000000000001

    pack = build_final_evidence_pack([current, baseline], [])

    assert pack["comparison_canonicalization"]["canonical_comparison_count"] == 8
    assert _comparison(
        pack,
        baseline="previous_window",
        group="CDN-B",
    )["facts"]["delta"] == pytest.approx(-0.45)


def test_different_baselines_and_cdns_remain_distinct() -> None:
    current, baseline = _overlapping_qoe_results()
    pack = build_final_evidence_pack([current, baseline], [])

    assert {
        item["facts"]["baseline_window"] for item in _comparisons(pack)
    } == {"previous_day", "previous_window"}
    ids = {
        _comparison(pack, baseline="previous_window", group=cdn)["evidence_id"]
        for cdn in ("CDN-A", "CDN-B", "CDN-C")
    }
    assert len(ids) == 3


@pytest.mark.parametrize("difference", ["metric", "unit", "aggregation"])
def test_different_metric_contracts_do_not_merge(difference: str) -> None:
    current, baseline = _overlapping_qoe_results()
    binding = deepcopy(PLAYBACK_BINDING)
    if difference == "metric":
        binding["primary_metric"] = "alternate_success_rate"
        for row in baseline.rows:
            row["alternate_success_rate"] = row.pop("playback_success_rate")
        baseline.columns[-1] = "alternate_success_rate"
        baseline.tool_input["primary_metric"] = "alternate_success_rate"
    elif difference == "unit":
        binding["unit"] = "percent"
    else:
        binding["aggregation_semantics"] = "avg(preaggregated_rate)"
    baseline.tool_metadata["metric_binding"] = binding

    pack = build_final_evidence_pack([current, baseline], [])

    assert pack["comparison_canonicalization"]["canonical_comparison_count"] == 12
    assert pack["comparison_canonicalization"]["duplicate_comparison_count_removed"] == 0


def test_raw_tool_and_sql_correction_lineage_survives_comparison_dedup() -> None:
    pack = _full_real_shape_pack()
    lineage = {
        item["task_id"]: item for item in pack["reviewed_evidence"]
    }
    log_roles = {
        item["role"] for item in lineage["logs"]["evidence_sources"]
    }
    log_kinds = {
        item["kind"]
        for item in pack["data_evidence"]
        if "logs" in item["source_task_ids"]
    }

    assert log_roles == {"base_tool_evidence", "correction_evidence"}
    assert {
        "reviewed_tool_result_summary",
        "reviewed_result_summary",
    } <= log_kinds


def test_projection_bundles_guards_validator_and_renderer_use_canonical_ids() -> None:
    pack = _full_real_shape_pack()
    projection, stats = build_final_evidence_projection(
        pack,
        max_chars=13_214,
    )
    canonical_ids = {
        item["evidence_id"] for item in _comparisons(pack)
    }
    projected_comparisons = [
        item
        for item in projection["data_evidence"]
        if item["type"] in {"metric_comparison", "group_comparison"}
    ]
    projected_ids = {item["id"] for item in projected_comparisons}
    regional = next(
        item
        for item in projection["evidence_bundles"]
        if item["bundle_type"] == "regional"
    )
    overall_ids = [
        item["id"]
        for item in projected_comparisons
        if item["type"] == "metric_comparison"
    ]
    claim = {
        "bundle_id": regional["bundle_id"],
        "claim_type": "observation",
        "predicate": "general",
        "polarity": "neutral",
        "subject_evidence_ids": [],
        "supporting_evidence_ids": overall_ids,
    }
    selection = {
        "data_evidence_ids": [
            item["id"] for item in projection["data_evidence"]
        ],
        "knowledge_evidence_ids": [
            item["id"] for item in projection["knowledge_evidence"]
        ],
        "inferences": [claim],
        "limitation_ids": [
            item["id"] for item in projection["limitations"]
        ],
        "source_task_ids": ["analysis"],
    }

    assert projected_ids == canonical_ids
    assert len(projected_ids) == 8
    assert len(regional["allowed_evidence_ids"]) == len(
        set(regional["allowed_evidence_ids"])
    )
    assert canonical_ids <= set(regional["allowed_evidence_ids"])
    assert evidence_projection_claim_violations(selection, projection) == []
    assert evidence_bundle_claim_violations(selection, projection) == []
    assert final_claim_violations(selection, pack) == []
    assert "DATA EVIDENCE" in render_final_answer(pack, selection, projection)
    assert stats["canonical_comparison_count"] == 8
    assert stats["duplicate_comparison_count_removed"] == 16
    assert stats["projected_evidence_chars"] <= 13_214


def test_latest_overlapping_real_shape_reaches_final_answer_and_memory() -> None:
    from evals.media.run_tool_offline_cases import (  # noqa: PLC0415
        _run_composite,
        _runtime,
    )

    wren, router = _runtime()
    result = _run_composite(
        wren,
        router,
        planner_shape="overlapping_real_shape",
    )
    pack = result["evidence_pack"]
    stats = result["evidence_projection_stats"]
    comparisons = _comparisons(pack)

    assert result["success"] is True
    assert pack["comparison_canonicalization"] == {
        "comparison_records_before_dedup": 6,
        "comparison_count_before_dedup": 24,
        "canonical_comparison_count": 8,
        "duplicate_comparison_count_removed": 16,
        "overall_before_dedup": 6,
        "overall_after_dedup": 2,
        "group_before_dedup": 18,
        "group_after_dedup": 6,
        "provenance_sources_retained": 24,
    }
    assert sum(item["kind"] == "metric_comparison" for item in comparisons) == 2
    assert sum(item["kind"] == "group_comparison" for item in comparisons) == 6
    assert {
        (item["facts"]["baseline_window"], item["facts"]["group"]): item[
            "facts"
        ]["delta"]
        for item in comparisons
        if item["kind"] == "group_comparison"
    } == pytest.approx(
        {
            ("previous_window", "CDN-A"): -0.10,
            ("previous_window", "CDN-B"): -0.45,
            ("previous_window", "CDN-C"): -0.10,
            ("previous_day", "CDN-A"): -0.10,
            ("previous_day", "CDN-B"): -0.40,
            ("previous_day", "CDN-C"): -0.10,
        }
    )
    assert all(
        _comparison(pack, baseline=baseline, group="CDN-B")["facts"][
            "is_largest_decline"
        ]
        for baseline in ("previous_window", "previous_day")
    )
    assert result["analyst_corrected_log_evidence_visible"] is True
    assert all(result["analyst_log_evidence_markers"].values())
    assert result["validator_result"] == {
        "valid": True,
        "violations": [],
        "retry_count": 0,
        "projection_guard_valid": True,
        "bundle_guard_valid": True,
        "bundle_violations": [],
        "validated_against": "full_evidence_pack",
    }
    assert stats["final_prompt_chars"] < stats["budget_limit"] == 20_000
    assert stats["budget_remaining"] > 0
    assert stats["bundle_metadata_chars"] > 0
    assert result["final_answer"]
    assert all(
        heading in result["final_answer"]
        for heading in (
            "DATA EVIDENCE",
            "KNOWLEDGE EVIDENCE",
            "INFERENCE",
            "LIMITATION",
        )
    )
    assert result["memory_updated"] is True
