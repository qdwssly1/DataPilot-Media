"""Safe adapters between the retrieval boundary and AgentState."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from datapilot.agent.state import AgentState, get_effective_query
from datapilot.retrieval.knowledge import KnowledgeRetriever, RetrievalResult


class KnowledgeRetrieverProtocol(Protocol):
    """Narrow injection boundary used by the CLI and tests."""

    def retrieve(self, query: str, *, top_k: int = 5) -> list[RetrievalResult]: ...


def build_domain_retriever(
    environ: Mapping[str, str] | None,
) -> KnowledgeRetriever | None:
    """Discover a domain-owned corpus from the configured Wren project."""

    if environ is None:
        import os  # noqa: PLC0415

        values: Mapping[str, str] = os.environ
    else:
        values = environ
    explicit = values.get("DATAPILOT_KNOWLEDGE_PATH", "").strip()
    if explicit:
        directory = Path(explicit)
    else:
        project_value = values.get("WREN_PROJECT_PATH", "").strip()
        if not project_value:
            return None
        project = Path(project_value)
        directory = (project.parent if project.suffix else project) / "knowledge"
    if not directory.is_dir():
        return None
    return KnowledgeRetriever.from_directory(directory)


def retrieve_into_state(
    state: AgentState,
    retriever: KnowledgeRetrieverProtocol | None,
    *,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Retrieve once, store separate evidence, and fail open without data claims."""

    state["knowledge_evidence"] = []
    state["knowledge_retrieval_error"] = None
    state["knowledge_retrieval_latency_ms"] = 0.0
    if retriever is None:
        state["knowledge_retrieval_error"] = "knowledge retriever is unavailable"
        return []
    started_at = perf_counter()
    try:
        results = retriever.retrieve(get_effective_query(state), top_k=top_k)
    except Exception as exc:
        state["knowledge_retrieval_latency_ms"] = (
            perf_counter() - started_at
        ) * 1000
        state["knowledge_retrieval_error"] = (
            f"knowledge retrieval failed: {type(exc).__name__}"
        )
        return []
    state["knowledge_retrieval_latency_ms"] = (perf_counter() - started_at) * 1000
    evidence = [result.to_dict() for result in results]
    state["knowledge_evidence"] = evidence
    return evidence


def compact_knowledge_evidence(
    state: AgentState,
    *,
    max_items: int = 5,
    max_text_chars: int = 1200,
) -> list[dict[str, Any]]:
    """Return bounded prompt context while preserving provenance and scores."""

    output: list[dict[str, Any]] = []
    for item in state["knowledge_evidence"][:max_items]:
        output.append(
            {
                "chunk_id": item["chunk_id"],
                "title": item["title"],
                "category": item["category"],
                "source": item["source"],
                "text": str(item["text"])[:max_text_chars],
                "retrieved_reason": item["retrieved_reason"],
            }
        )
    return output
