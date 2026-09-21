from __future__ import annotations

import json
from pathlib import Path

import pytest

from datapilot.retrieval import (
    KnowledgeRetriever,
    load_knowledge_chunks,
    retrieve_knowledge,
)
from evals.media.run_retrieval_eval import (
    run_evaluation,
    run_expanded_evaluation,
)

ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = ROOT / "domains" / "media" / "knowledge"


@pytest.fixture(scope="module")
def retriever() -> KnowledgeRetriever:
    return KnowledgeRetriever.from_directory(KNOWLEDGE)


def test_document_loader_and_chunk_metadata() -> None:
    chunks = load_knowledge_chunks(KNOWLEDGE)

    assert len(chunks) == 86
    assert len({chunk.document_id for chunk in chunks}) == 14
    assert all(chunk.document_id for chunk in chunks)
    assert all(chunk.title and chunk.category for chunk in chunks)
    assert all(chunk.source_path.endswith(".md") for chunk in chunks)
    assert all(chunk.chunk_id.startswith(f"{chunk.document_id}::") for chunk in chunks)
    assert all(chunk.domain == "media" for chunk in chunks)
    assert all(chunk.text.startswith("## ") for chunk in chunks)
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)


def test_chunking_is_heading_aware_not_fixed_character_slicing(
    retriever: KnowledgeRetriever,
) -> None:
    lengths = {len(chunk.text) for chunk in retriever.chunks}

    assert 250 < retriever.average_chunk_length < 600
    assert len(lengths) > 8
    assert max(lengths) < 1600


def test_reference_subsections_remain_outside_retrieval_chunks(
    retriever: KnowledgeRetriever,
) -> None:
    assert all("### 8. References" not in chunk.text for chunk in retriever.chunks)
    assert all("https://" not in chunk.text for chunk in retriever.chunks)


def test_lexical_retrieval_finds_exact_error_code(
    retriever: KnowledgeRetriever,
) -> None:
    result = retriever.search("E302 是什么", top_k=3, mode="lexical")

    assert result[0].chunk_id == "media-error-codes::01-e302-cdn-upstream-timeout"
    assert result[0].lexical_score > 0


def test_semantic_retrieval_resolves_bilingual_metric_alias(
    retriever: KnowledgeRetriever,
) -> None:
    result = retriever.search(
        "time to first frame percentile",
        top_k=3,
        mode="semantic",
    )

    assert any(
        item.chunk_id == "media-qoe-metrics::03-startup-time-and-startup-time-p95"
        for item in result
    )
    assert all(item.semantic_score > 0 for item in result)


def test_hybrid_rrf_exposes_component_and_fusion_scores(
    retriever: KnowledgeRetriever,
) -> None:
    result = retriever.search("E302 upstream timeout", top_k=3, mode="hybrid")

    assert result[0].lexical_score > 0
    assert result[0].semantic_score > 0
    assert result[0].fused_score > 0
    assert result[0].rerank_score >= result[0].fused_score
    assert "RRF" in result[0].retrieved_reason[0]


def test_rerank_prioritizes_exact_code_and_metric(
    retriever: KnowledgeRetriever,
) -> None:
    error_result = retriever.retrieve("出现 E302 告警如何排查", top_k=3)
    metric_result = retriever.retrieve("播放成功率聚合口径", top_k=3)

    assert error_result[0].chunk_id == "media-error-codes::01-e302-cdn-upstream-timeout"
    assert any("exact error-code match" in item for item in error_result[0].retrieved_reason)
    assert metric_result[0].chunk_id == "media-qoe-metrics::01-playback-success-rate"
    assert any("metric match" in item for item in metric_result[0].retrieved_reason)


def test_unrelated_query_returns_no_knowledge(
    retriever: KnowledgeRetriever,
) -> None:
    assert retriever.retrieve("员工差旅报销政策是什么？", top_k=5) == []


def test_retrieval_is_deterministic(retriever: KnowledgeRetriever) -> None:
    first = retriever.retrieve("CDN 播放成功率下降排查", top_k=5)
    second = retriever.retrieve("CDN 播放成功率下降排查", top_k=5)

    assert [item.to_dict() for item in first] == [item.to_dict() for item in second]


def test_public_retrieval_api_returns_structured_result() -> None:
    result = retrieve_knowledge("E302", top_k=1, knowledge_dir=KNOWLEDGE)[0]
    payload = result.to_dict()

    assert set(payload) == {
        "text",
        "title",
        "category",
        "source",
        "document_id",
        "chunk_id",
        "domain",
        "tags",
        "lexical_score",
        "semantic_score",
        "fused_score",
        "rerank_score",
        "retrieved_reason",
    }


@pytest.mark.parametrize(
    ("query", "expected_document"),
    [
        ("为什么视频打开后很久才出画面", "media-startup-latency-diagnostics"),
        ("画面一直转圈，视频播放一会就停一下", "media-buffering-rebuffering"),
        ("CDN 为什么一直去源站拿内容", "media-cdn-cache-origin-flow"),
        ("回源连接超时应该怎么排查", "media-origin-timeout"),
        ("华南某 CDN 区域退化和路由切换怎么查", "media-dns-routing-failover"),
        ("直播从推流到播放经过哪些环节", "media-live-streaming-pipeline"),
        ("推流正常但观众播放失败怎么排查", "media-live-stream-failure-sop"),
        ("转码任务失败但源文件正常应该检查什么", "media-transcode-failure"),
        ("指标告警日志同时出现能证明因果吗", "media-metric-alarm-log-correlation"),
        ("点播放后黑屏并报错", "media-playback-failure"),
    ],
)
def test_expanded_topics_are_retrievable_in_top_three(
    retriever: KnowledgeRetriever,
    query: str,
    expected_document: str,
) -> None:
    results = retriever.retrieve(query, top_k=3)

    assert expected_document in {result.document_id for result in results}


def test_golden_set_and_report_shape() -> None:
    cases = json.loads(
        (ROOT / "evals" / "media" / "retrieval_cases.json").read_text(
            encoding="utf-8"
        )
    )
    report = run_evaluation()

    assert 10 <= len(cases) <= 15
    assert {"recall_at_1", "recall_at_3", "mrr"} <= set(report["hybrid"])
    assert report["suite"] == "original"
    assert report["lexical"]["recall_at_3"] >= 0.75
    assert report["hybrid_rerank"]["recall_at_3"] >= 0.9
    assert report["hybrid_rerank"]["unrelated_no_result_accuracy"] == 1.0
    assert report["llm_calls"] == 0


def test_expanded_ground_truth_and_report() -> None:
    cases = json.loads(
        (ROOT / "evals" / "media" / "expanded_retrieval_cases.json").read_text(
            encoding="utf-8"
        )
    )
    chunks = {chunk.chunk_id for chunk in load_knowledge_chunks(KNOWLEDGE)}

    assert len(cases) == 22
    assert sum(bool(case["relevant_set"]) for case in cases) == 19
    assert sum(not case["relevant_set"] for case in cases) == 3
    assert all(
        {"id", "query", "expected_document", "topic", "expected_section", "relevant_set"}
        <= case.keys()
        for case in cases
    )
    assert all(set(case["relevant_set"]) <= chunks for case in cases)

    report = run_expanded_evaluation()

    assert report["suite"] == "knowledge_expansion"
    assert report["positive_cases"] == 19
    assert report["negative_cases"] == 3
    assert report["hybrid_rerank"]["recall_at_3"] >= 0.9
    assert report["hybrid_rerank"]["mrr"] >= 0.8
    assert report["llm_calls"] == 0
    assert report["retries"] == 0
