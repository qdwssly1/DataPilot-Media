"""Run the deterministic Media knowledge retrieval golden set."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any

from datapilot.retrieval import KnowledgeRetriever

ROOT = Path(__file__).resolve().parents[2]


def evaluate_mode(
    retriever: KnowledgeRetriever,
    cases: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    relevant_cases = [case for case in cases if case["expected_chunk_ids"]]
    recalls_at_1: list[float] = []
    recalls_at_3: list[float] = []
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    details: list[dict[str, Any]] = []
    unrelated_correct = 0
    unrelated_count = 0
    for case in cases:
        started_at = perf_counter()
        results = retriever.search(case["query"], top_k=3, mode=mode)
        latencies.append((perf_counter() - started_at) * 1000)
        returned = [result.chunk_id for result in results]
        expected = set(case["expected_chunk_ids"])
        if not expected:
            unrelated_count += 1
            unrelated_correct += int(not returned)
        else:
            recalls_at_1.append(len(expected & set(returned[:1])) / len(expected))
            recalls_at_3.append(len(expected & set(returned[:3])) / len(expected))
            first_rank = next(
                (index for index, chunk_id in enumerate(returned, start=1) if chunk_id in expected),
                None,
            )
            reciprocal_ranks.append(0.0 if first_rank is None else 1 / first_rank)
        details.append(
            {
                "id": case["id"],
                "returned": returned,
                "expected": sorted(expected),
            }
        )
    return {
        "recall_at_1": mean(recalls_at_1),
        "recall_at_3": mean(recalls_at_3),
        "mrr": mean(reciprocal_ranks),
        "unrelated_no_result_accuracy": (
            unrelated_correct / unrelated_count if unrelated_count else None
        ),
        "mean_latency_ms": mean(latencies),
        "max_latency_ms": max(latencies),
        "details": details,
    }


def run_evaluation() -> dict[str, Any]:
    corpus = ROOT / "domains" / "media" / "knowledge"
    cases = json.loads(
        (ROOT / "evals" / "media" / "retrieval_cases.json").read_text(
            encoding="utf-8"
        )
    )
    retriever = KnowledgeRetriever.from_directory(corpus)
    return {
        "corpus": {
            "chunks": retriever.chunk_count,
            "average_chunk_length": retriever.average_chunk_length,
            "semantic_backend": "local concept-aware dense feature hashing",
        },
        "cases": len(cases),
        "lexical": evaluate_mode(retriever, cases, "lexical"),
        "semantic": evaluate_mode(retriever, cases, "semantic"),
        "hybrid": evaluate_mode(retriever, cases, "hybrid"),
        "hybrid_rerank": evaluate_mode(retriever, cases, "hybrid_rerank"),
        "llm_calls": 0,
        "retries": 0,
    }


if __name__ == "__main__":
    print(json.dumps(run_evaluation(), ensure_ascii=False, indent=2))
