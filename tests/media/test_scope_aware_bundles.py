"""Phase 3.10 deterministic scope-aware evidence bundle contracts."""

from __future__ import annotations

from copy import deepcopy

from datapilot.agent.evidence import (
    build_final_evidence_projection,
    evidence_bundle_claim_violations,
    final_claim_violations,
    render_final_answer,
)
from evals.media.run_tool_offline_cases import _run_composite, _runtime


def _comparison(
    evidence_id: str,
    *,
    group: str | None,
    baseline: str,
    baseline_value: float,
    current_value: float,
    largest: bool | None = None,
) -> dict:
    kind = "group_comparison" if group is not None else "metric_comparison"
    facts = {
        "metric": "playback_success_rate",
        "baseline_window": baseline,
        "baseline_value": baseline_value,
        "current_window": "current_window",
        "current_value": current_value,
        "delta": current_value - baseline_value,
        "relative_change": (current_value - baseline_value) / baseline_value,
        "filters": {"region": "华南"},
    }
    scope = {
        "windows": [baseline, "current_window"],
        "region": "华南",
        "cdn": group or "ALL",
        "error_code": "ALL",
        "severity": "ALL",
        "level": "ALL",
        "group_dimensions": ["window_name", "region", "cdn"],
    }
    if group is not None:
        facts.update(
            {
                "dimension": "cdn",
                "group": group,
                "classification": "declined",
                "is_largest_decline": bool(largest),
            }
        )
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "statement": f"{group or 'overall'} comparison",
        "facts": facts,
        "source_task_ids": ["qoe"],
        "source_evidence_ids": ["source:qoe:tool"],
        "scope": scope,
        "required": True,
    }


def _pack(*, anomalous_cdn: str = "CDN-B") -> dict:
    data: list[dict] = []
    values = {
        "CDN-A": (0.95, 0.85),
        anomalous_cdn: (0.95, 0.50),
        "CDN-C": (0.90, 0.80),
    }
    for comparison_index, baseline in enumerate(
        ("previous_window", "previous_day"),
        start=1,
    ):
        data.append(
            _comparison(
                f"data:comparison:{comparison_index}:overall",
                group=None,
                baseline=baseline,
                baseline_value=0.9333 if comparison_index == 1 else 0.9167,
                current_value=0.7167,
            )
        )
        for group_index, (group, (baseline_value, current_value)) in enumerate(
            values.items(),
            start=1,
        ):
            data.append(
                _comparison(
                    f"data:comparison:{comparison_index}:group:{group_index}",
                    group=group,
                    baseline=baseline,
                    baseline_value=(
                        0.90
                        if group == anomalous_cdn and baseline == "previous_day"
                        else baseline_value
                    ),
                    current_value=current_value,
                    largest=group == anomalous_cdn,
                )
            )
    incident_scope = {
        "windows": ["current_window"],
        "region": "华南",
        "cdn": anomalous_cdn,
        "error_code": "E302",
        "severity": "high",
        "level": "ALL",
        "group_dimensions": [],
    }
    data.extend(
        [
            {
                "evidence_id": "data:status:1",
                "kind": "alarm_status_distribution",
                "statement": "mixed E302 alarm status",
                "facts": {
                    "distribution": [
                        {"value": "open", "count": 1},
                        {"value": "investigating", "count": 1},
                        {"value": "resolved", "count": 1},
                    ],
                    "mixed": True,
                    "total": 3,
                    "severity_distribution": {"high": 3},
                    "error_code_distribution": {"E302": 3},
                    "cdn_distribution": {anomalous_cdn: 3},
                    "service_distribution": {},
                    "affected_objects": {
                        "region": ["华南"],
                        "cdn": [anomalous_cdn],
                    },
                    "sample_events": [],
                    "sample_semantics": "bounded_examples_not_distribution",
                },
                "source_task_ids": ["alarms"],
                "source_evidence_ids": ["source:alarms:tool"],
                "scope": incident_scope,
                "required": True,
            },
            {
                "evidence_id": "data:logs:1",
                "kind": "reviewed_result_summary",
                "statement": "four structured E302 logs",
                "facts": {
                    "row_count": 4,
                    "execution_source": "sql",
                    "tool_name": None,
                    "evidence_role": "correction_evidence",
                    "columns": [],
                },
                "source_task_ids": ["logs"],
                "source_evidence_ids": ["source:logs:sql-correction:1"],
                "scope": {**incident_scope, "severity": "ALL"},
                "required": True,
            },
        ]
    )
    knowledge = [
        {
            "evidence_id": "knowledge:sop",
            "kind": "retrieved_knowledge",
            "title": "CDN Playback Success Degradation SOP",
            "supported_statement": "Inspect upstream latency, origin health, and routing.",
            "source_category": "troubleshooting_sop",
            "source_identifiers": ["knowledge:sop", "sop"],
            "required": False,
        },
        {
            "evidence_id": "knowledge:e302",
            "kind": "retrieved_knowledge",
            "title": "E302 CDN Upstream Timeout",
            "supported_statement": "E302 is an upstream-timeout symptom.",
            "source_category": "error_code",
            "source_identifiers": ["knowledge:e302", "e302"],
            "required": False,
        },
        {
            "evidence_id": "knowledge:qoe",
            "kind": "retrieved_knowledge",
            "title": "Playback Success Rate",
            "supported_statement": "Successful sessions divided by attempted sessions.",
            "source_category": "qoe_metric",
            "source_identifiers": ["knowledge:qoe", "qoe"],
            "required": False,
        },
    ]
    limitations = [
        {
            "limitation_id": "limitation:correlation_not_causation",
            "statement": "Correlation does not prove causation.",
            "source_identifiers": ["policy:causal-boundary"],
            "required": True,
        },
        {
            "limitation_id": "limitation:missing_trace_data",
            "statement": "Request-level traces are missing.",
            "source_identifiers": ["qoe", "alarms", "logs"],
            "required": True,
        },
        {
            "limitation_id": "limitation:missing_origin_metrics",
            "statement": "Origin metrics are missing.",
            "source_identifiers": ["qoe", "alarms", "logs"],
            "required": True,
        },
        {
            "limitation_id": "limitation:missing_routing_change_records",
            "statement": "Routing change records are missing.",
            "source_identifiers": ["qoe", "alarms", "logs"],
            "required": True,
        },
        {
            "limitation_id": "limitation:limited_time_windows",
            "statement": "More time windows are required.",
            "source_identifiers": ["qoe"],
            "required": True,
        },
    ]
    return {
        "version": "1.0",
        "reviewed_task_ids": ["qoe", "alarms", "logs"],
        "reviewed_evidence": [
            {
                "task_id": "qoe",
                "evidence_sources": [{"source_id": "source:qoe:tool"}],
            },
            {
                "task_id": "alarms",
                "evidence_sources": [{"source_id": "source:alarms:tool"}],
            },
            {
                "task_id": "logs",
                "evidence_sources": [
                    {"source_id": "source:logs:sql-correction:1"}
                ],
            },
        ],
        "data_evidence": data,
        "knowledge_evidence": knowledge,
        "inference_policy": {
            "allowed_claim_types": [
                "observation",
                "knowledge",
                "correlation",
                "hypothesis",
                "recommendation",
                "causal_claim",
            ],
            "allowed_predicates": ["general", "stable_control"],
            "allowed_polarities": ["neutral", "positive", "negative"],
            "causal_evidence_ids": [],
        },
        "limitations": limitations,
    }


def _projection(pack: dict | None = None) -> tuple[dict, dict]:
    return build_final_evidence_projection(pack or _pack(), max_chars=100_000)


def _bundle(projection: dict, bundle_type: str) -> dict:
    return next(
        item
        for item in projection["evidence_bundles"]
        if item["bundle_type"] == bundle_type
    )


def _claim(bundle_id: str, claim_type: str, support: list[str]) -> dict:
    return {
        "bundle_id": bundle_id,
        "claim_type": claim_type,
        "predicate": "general",
        "polarity": "neutral",
        "subject_evidence_ids": [],
        "supporting_evidence_ids": support,
    }


def _selection(pack: dict, claims: list[dict]) -> dict:
    return {
        "data_evidence_ids": [item["evidence_id"] for item in pack["data_evidence"]],
        "knowledge_evidence_ids": [
            item["evidence_id"] for item in pack["knowledge_evidence"]
        ],
        "inferences": claims,
        "limitation_ids": [item["limitation_id"] for item in pack["limitations"]],
        "source_task_ids": ["response"],
    }


def test_regional_bundle_contains_all_group_comparisons() -> None:
    projection, _ = _projection()
    regional = _bundle(projection, "regional")
    assert len([item for item in regional["allowed_evidence_ids"] if ":group:" in item]) == 6


def test_regional_bundle_excludes_entity_alarm_and_logs() -> None:
    projection, _ = _projection()
    allowed = set(_bundle(projection, "regional")["allowed_evidence_ids"])
    assert "data:status:1" not in allowed
    assert "data:logs:1" not in allowed


def test_entity_bundle_contains_entity_comparisons_alarm_and_logs() -> None:
    projection, _ = _projection()
    allowed = set(_bundle(projection, "entity")["allowed_evidence_ids"])
    assert {"data:comparison:1:group:2", "data:comparison:2:group:2"} <= allowed
    assert {"data:status:1", "data:logs:1"} <= allowed


def test_entity_bundle_excludes_other_entity_comparisons() -> None:
    projection, _ = _projection()
    allowed = set(_bundle(projection, "entity")["allowed_evidence_ids"])
    assert "data:comparison:1:group:1" not in allowed
    assert "data:comparison:1:group:3" not in allowed


def test_multi_group_bundle_contains_all_group_comparisons() -> None:
    projection, _ = _projection()
    allowed = _bundle(projection, "multi_group")["allowed_evidence_ids"]
    assert len(allowed) == 6
    assert all(":group:" in evidence_id for evidence_id in allowed)


def test_multi_group_bundle_excludes_entity_only_evidence() -> None:
    projection, _ = _projection()
    allowed = set(_bundle(projection, "multi_group")["allowed_evidence_ids"])
    assert {"data:status:1", "data:logs:1"}.isdisjoint(allowed)


def test_recommendation_cross_bundle_reference_is_invalid() -> None:
    projection, _ = _projection()
    entity = _bundle(projection, "entity")
    claim = _claim(
        entity["bundle_id"],
        "recommendation",
        ["knowledge:sop", "data:comparison:1:group:1"],
    )
    assert "outside bundle" in evidence_bundle_claim_violations(
        {"inferences": [claim]}, projection
    )[0]


def test_entity_recommendation_with_compatible_evidence_is_valid() -> None:
    projection, _ = _projection()
    entity = _bundle(projection, "entity")
    claim = _claim(
        entity["bundle_id"],
        "recommendation",
        [
            "knowledge:sop",
            "data:comparison:1:group:2",
            "data:status:1",
            "data:logs:1",
            "limitation:missing_origin_metrics",
        ],
    )
    assert evidence_bundle_claim_violations({"inferences": [claim]}, projection) == []


def test_correlation_bundle_guard_rejects_other_entity() -> None:
    projection, _ = _projection()
    entity = _bundle(projection, "entity")
    claim = _claim(
        entity["bundle_id"],
        "correlation",
        ["data:comparison:1:group:1", "data:status:1"],
    )
    assert evidence_bundle_claim_violations({"inferences": [claim]}, projection)


def test_hypothesis_bundle_guard_rejects_other_entity() -> None:
    projection, _ = _projection()
    entity = _bundle(projection, "entity")
    claim = _claim(
        entity["bundle_id"],
        "hypothesis",
        ["data:comparison:1:group:3", "knowledge:e302"],
    )
    assert evidence_bundle_claim_violations({"inferences": [claim]}, projection)


def test_stable_control_uses_multi_group_bundle() -> None:
    projection, _ = _projection()
    multi = _bundle(projection, "multi_group")
    subjects = ["data:comparison:1:group:1", "data:comparison:1:group:3"]
    claim = {
        "bundle_id": multi["bundle_id"],
        "claim_type": "observation",
        "predicate": "stable_control",
        "polarity": "negative",
        "subject_evidence_ids": subjects,
        "supporting_evidence_ids": subjects,
    }
    assert evidence_bundle_claim_violations({"inferences": [claim]}, projection) == []


def test_global_knowledge_bundle_supports_knowledge_claim() -> None:
    projection, _ = _projection()
    global_bundle = _bundle(projection, "global_knowledge")
    claim = _claim(global_bundle["bundle_id"], "knowledge", ["knowledge:e302"])
    assert evidence_bundle_claim_violations({"inferences": [claim]}, projection) == []


def test_unknown_bundle_id_is_invalid() -> None:
    projection, _ = _projection()
    claim = _claim("bundle:missing", "knowledge", ["knowledge:e302"])
    assert "unknown evidence bundle" in evidence_bundle_claim_violations(
        {"inferences": [claim]}, projection
    )[0]


def test_evidence_id_not_exposed_by_bundle_is_invalid() -> None:
    projection, _ = _projection()
    global_bundle = _bundle(projection, "global_knowledge")
    claim = _claim(
        global_bundle["bundle_id"],
        "knowledge",
        ["knowledge:e302", "data:status:1"],
    )
    assert evidence_bundle_claim_violations({"inferences": [claim]}, projection)


def test_renderer_uses_entity_bundle_scope() -> None:
    pack = _pack()
    projection, _ = _projection(pack)
    entity = _bundle(projection, "entity")
    claim = _claim(
        entity["bundle_id"],
        "recommendation",
        ["knowledge:sop", "data:status:1"],
    )
    answer = render_final_answer(pack, _selection(pack, [claim]), projection)
    assert "针对 cdn=CDN-B" in answer


def test_regional_and_entity_recommendations_render_separately() -> None:
    pack = _pack()
    projection, _ = _projection(pack)
    regional = _bundle(projection, "regional")
    entity = _bundle(projection, "entity")
    claims = [
        _claim(
            entity["bundle_id"],
            "recommendation",
            ["knowledge:sop", "data:status:1"],
        ),
        _claim(
            regional["bundle_id"],
            "recommendation",
            [
                "knowledge:sop",
                "data:comparison:1:group:1",
                "data:comparison:1:group:2",
                "data:comparison:1:group:3",
            ],
        ),
    ]
    assert evidence_bundle_claim_violations({"inferences": claims}, projection) == []
    answer = render_final_answer(pack, _selection(pack, claims), projection)
    assert "针对 cdn=CDN-B" in answer
    assert "针对华南区域" in answer


def test_two_recommendations_do_not_mix_scopes() -> None:
    pack = _pack()
    projection, _ = _projection(pack)
    regional = _bundle(projection, "regional")
    entity = _bundle(projection, "entity")
    claims = [
        _claim(
            entity["bundle_id"],
            "recommendation",
            ["knowledge:sop", "data:comparison:1:group:2", "data:status:1"],
        ),
        _claim(
            regional["bundle_id"],
            "recommendation",
            [
                "knowledge:sop",
                "data:comparison:1:group:1",
                "data:comparison:1:group:2",
                "data:comparison:1:group:3",
            ],
        ),
    ]
    selection = _selection(pack, claims)
    assert evidence_bundle_claim_violations(selection, projection) == []
    assert final_claim_violations(selection, pack) == []


def test_entity_bundle_is_generated_for_arbitrary_cdn_x() -> None:
    projection, _ = _projection(_pack(anomalous_cdn="CDN-X"))
    entity = _bundle(projection, "entity")
    assert entity["target_entities"] == {"cdn": ["CDN-X"]}
    assert entity["bundle_id"].endswith(":CDN-X")


def test_causal_claim_without_causal_evidence_remains_invalid() -> None:
    pack = _pack()
    projection, _ = _projection(pack)
    entity = _bundle(projection, "entity")
    claim = _claim(
        entity["bundle_id"],
        "causal_claim",
        ["data:comparison:1:group:2", "knowledge:e302"],
    )
    violations = final_claim_violations(_selection(pack, [claim]), pack)
    assert any("lacks approved causal evidence" in item for item in violations)


def test_bundle_generation_is_deterministic() -> None:
    first, _ = _projection()
    second, _ = _projection(deepcopy(_pack()))
    assert first["evidence_bundles"] == second["evidence_bundles"]


def test_latest_real_shape_offline_replay_reaches_memory_commit() -> None:
    wren, router = _runtime()
    result = _run_composite(
        wren,
        router,
        planner_shape="single_task_multi_baseline",
    )
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
    assert result["evidence_projection_stats"]["final_prompt_chars"] < 20_000
    claims = result["final_claims"]
    assert all(claim.get("bundle_id") for claim in claims)
    assert sum(claim["claim_type"] == "recommendation" for claim in claims) == 2
    answer = result["final_answer"]
    assert "针对 cdn=CDN-B" in answer
    assert "针对华南区域" in answer
