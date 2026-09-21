"""Run the deterministic Media knowledge retrieval golden sets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any

from datapilot.retrieval import KnowledgeRetriever

ROOT = Path(__file__).resolve().parents[2]


def _relevant_chunk_ids(case: dict[str, Any]) -> list[str]:
    """Read either the original or expanded ground-truth contract."""

    values = case.get("relevant_set", case.get("expected_chunk_ids", []))
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ValueError(f"invalid relevant chunk IDs for case {case.get('id')}")
    return values


def evaluate_mode(
    retriever: KnowledgeRetriever,
    cases: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    relevant_cases = [case for case in cases if _relevant_chunk_ids(case)]
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
        expected = set(_relevant_chunk_ids(case))
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
        detail = {
            "id": case["id"],
            "returned": returned,
            "expected": sorted(expected),
        }
        for key in ("expected_document", "topic", "expected_section"):
            if key in case:
                detail[key] = case[key]
        details.append(detail)
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


def _run_evaluation(cases_filename: str, *, suite: str) -> dict[str, Any]:
    corpus = ROOT / "domains" / "media" / "knowledge"
    cases = json.loads(
        (ROOT / "evals" / "media" / cases_filename).read_text(
            encoding="utf-8"
        )
    )
    retriever = KnowledgeRetriever.from_directory(corpus)
    relevant = [_relevant_chunk_ids(case) for case in cases]
    return {
        "suite": suite,
        "corpus": {
            "documents": len({chunk.document_id for chunk in retriever.chunks}),
            "chunks": retriever.chunk_count,
            "average_chunk_length": retriever.average_chunk_length,
            "semantic_backend": "local concept-aware dense feature hashing",
        },
        "cases": len(cases),
        "positive_cases": sum(bool(item) for item in relevant),
        "negative_cases": sum(not item for item in relevant),
        "lexical": evaluate_mode(retriever, cases, "lexical"),
        "semantic": evaluate_mode(retriever, cases, "semantic"),
        "hybrid": evaluate_mode(retriever, cases, "hybrid"),
        "hybrid_rerank": evaluate_mode(retriever, cases, "hybrid_rerank"),
        "llm_calls": 0,
        "retries": 0,
    }


def run_evaluation() -> dict[str, Any]:
    """Run the unchanged 15-case original baseline."""

    return _run_evaluation("retrieval_cases.json", suite="original")


def run_expanded_evaluation() -> dict[str, Any]:
    """Run the separately versioned knowledge-expansion ground truth."""

    return _run_evaluation(
        "expanded_retrieval_cases.json",
        suite="knowledge_expansion",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expanded",
        action="store_true",
        help="run the separate knowledge-expansion evaluation",
    )
    args = parser.parse_args()
    report = run_expanded_evaluation() if args.expanded else run_evaluation()
    print(json.dumps(report, ensure_ascii=False, indent=2))
