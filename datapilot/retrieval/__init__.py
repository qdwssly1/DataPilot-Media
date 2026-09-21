"""Small, dependency-free knowledge retrieval boundary for DataPilot."""

from datapilot.retrieval.knowledge import (
    KnowledgeChunk,
    KnowledgeRetriever,
    RetrievalResult,
    load_knowledge_chunks,
    retrieve_knowledge,
)

__all__ = [
    "KnowledgeChunk",
    "KnowledgeRetriever",
    "RetrievalResult",
    "load_knowledge_chunks",
    "retrieve_knowledge",
]
