from __future__ import annotations

import json
from pathlib import Path

import pytest

from datapilot.retrieval import (
    KnowledgeRetriever,
    load_knowledge_chunks,
    retrieve_knowledge,
)
from evals.media.run_retrieval_eval import run_evaluation

ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = ROOT / "domains" / "media" / "knowledge"


@pytest.fixture(scope="module")
def retriever() -> KnowledgeRetriever:
    return KnowledgeRetriever.from_directory(KNOWLEDGE)


def test_document_loader_and_chunk_metadata() -> None:
    chunks = load_knowledge_chunks(KNOWLEDGE)

    assert len(chunks) == 16
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

    assert retriever.average_chunk_length == pytest.approx(643.75)
    assert len(lengths) > 8
    assert max(lengths) < 1600


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


def test_golden_set_and_report_shape() -> None:
    cases = json.loads(
        (ROOT / "evals" / "media" / "retrieval_cases.json").read_text(
            encoding="utf-8"
        )
    )
    report = run_evaluation()

    assert 10 <= len(cases) <= 15
    assert {"recall_at_1", "recall_at_3", "mrr"} <= set(report["hybrid"])
    assert report["lexical"]["recall_at_3"] >= 0.9
    assert report["hybrid"]["recall_at_3"] >= 0.9
    assert report["hybrid_rerank"]["recall_at_3"] >= 0.9
    assert report["llm_calls"] == 0
