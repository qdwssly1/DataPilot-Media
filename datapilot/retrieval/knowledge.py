"""Deterministic local hybrid retrieval for small domain knowledge corpora.

The implementation deliberately avoids a framework or external service. It
combines BM25 lexical retrieval with concept-aware dense feature embeddings,
then applies reciprocal-rank fusion and a configured deterministic reranker.
Domain vocabulary and reranking hints live beside each domain corpus.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Mapping, Sequence

RetrievalMode = Literal["lexical", "semantic", "hybrid", "hybrid_rerank"]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TOKEN_RE = re.compile(
    r"[A-Za-z]\d{2,}|[A-Za-z]+(?:[_-][A-Za-z0-9]+)*|\d+(?:\.\d+)?|[\u4e00-\u9fff]+"
)
_DEFAULT_ERROR_CODE_RE = re.compile(r"\b[A-Z]\d{3}\b", re.IGNORECASE)
_REFERENCE_HEADING_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s*)?(?:references?|参考资料|参考来源|资料来源)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    """One heading-aware knowledge unit and its provenance."""

    document_id: str
    title: str
    category: str
    source_path: str
    chunk_id: str
    domain: str
    text: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Structured result returned by every retrieval mode."""

    text: str
    title: str
    category: str
    source: str
    document_id: str
    chunk_id: str
    domain: str
    tags: tuple[str, ...]
    lexical_score: float
    semantic_score: float
    fused_score: float
    rerank_score: float
    retrieved_reason: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible result without losing score provenance."""

        value = asdict(self)
        value["tags"] = list(self.tags)
        value["retrieved_reason"] = list(self.retrieved_reason)
        return value


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """Generic configuration loaded from a domain-owned JSON file."""

    domain: str
    semantic_concepts: Mapping[str, tuple[str, ...]]
    category_hints: Mapping[str, tuple[str, ...]]
    metric_concepts: tuple[str, ...]
    rrf_k: int = 60
    embedding_dimensions: int = 256

    @classmethod
    def from_directory(cls, directory: Path) -> RetrievalConfig:
        path = directory / "retrieval_config.json"
        raw: Mapping[str, Any] = {}
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, Mapping):
                raise ValueError("retrieval_config.json must contain an object")
            raw = loaded

        def _string_tuple_map(key: str) -> dict[str, tuple[str, ...]]:
            value = raw.get(key, {})
            if not isinstance(value, Mapping):
                raise ValueError(f"{key} must be an object")
            result: dict[str, tuple[str, ...]] = {}
            for name, aliases in value.items():
                if not isinstance(name, str) or not isinstance(aliases, list):
                    raise ValueError(f"{key} entries must be string lists")
                if not all(isinstance(item, str) and item.strip() for item in aliases):
                    raise ValueError(f"{key} aliases must be non-empty strings")
                result[name] = tuple(item.strip() for item in aliases)
            return result

        metrics = raw.get("metric_concepts", [])
        if not isinstance(metrics, list) or not all(
            isinstance(item, str) for item in metrics
        ):
            raise ValueError("metric_concepts must be a string list")
        return cls(
            domain=str(raw.get("domain", directory.parent.name or "unknown")),
            semantic_concepts=_string_tuple_map("semantic_concepts"),
            category_hints=_string_tuple_map("category_hints"),
            metric_concepts=tuple(metrics),
            rrf_k=int(raw.get("rrf_k", 60)),
            embedding_dimensions=int(raw.get("embedding_dimensions", 256)),
        )


def _parse_front_matter(text: str, *, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ValueError(f"knowledge document has no front matter: {path}")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"knowledge document has invalid front matter: {path}")
    metadata: dict[str, Any] = {}
    for raw_line in text[4:end].splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        key, separator, raw_value = raw_line.partition(":")
        if not separator:
            raise ValueError(f"invalid front matter line in {path}: {raw_line}")
        value = raw_value.strip()
        if value.startswith("["):
            try:
                metadata[key.strip()] = json.loads(value)
            except json.JSONDecodeError:
                metadata[key.strip()] = [
                    item.strip().strip('"\'')
                    for item in value[1:-1].split(",")
                    if item.strip()
                ]
        else:
            metadata[key.strip()] = value.strip('"\'')
    return metadata, text[end + 5 :].strip()


def _slug(value: str) -> str:
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if ascii_slug:
        return ascii_slug
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"section-{digest}"


def _is_reference_heading(value: str) -> bool:
    return _REFERENCE_HEADING_RE.fullmatch(value.strip()) is not None


def _without_reference_subsections(lines: list[str]) -> list[str]:
    """Keep source references in Markdown while excluding them from retrieval."""

    output: list[str] = []
    skipping = False
    for line in lines:
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) >= 3:
            if _is_reference_heading(match.group(2)):
                skipping = True
                continue
            if skipping:
                skipping = False
        if not skipping:
            output.append(line)
    return output


def _split_paragraphs(text: str, *, max_chars: int) -> list[str]:
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    groups: list[str] = []
    current: list[str] = []
    current_length = 0
    for paragraph in paragraphs:
        extra = len(paragraph) + (2 if current else 0)
        if current and current_length + extra > max_chars:
            groups.append("\n\n".join(current))
            current = []
            current_length = 0
        current.append(paragraph)
        current_length += extra
    if current:
        groups.append("\n\n".join(current))
    return groups


def _document_sections(body: str, *, max_chars: int) -> list[tuple[str, str]]:
    """Split at level-two headings, then subordinate headings/paragraphs."""

    lines = body.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title = "Overview"
    current_lines: list[str] = []
    for line in lines:
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == 2:
            if current_lines and any(item.strip() for item in current_lines):
                sections.append((current_title, current_lines))
            current_title = match.group(2).strip()
            current_lines = [line]
        elif not (match and len(match.group(1)) == 1):
            current_lines.append(line)
    if current_lines and any(item.strip() for item in current_lines):
        sections.append((current_title, current_lines))

    output: list[tuple[str, str]] = []
    for section_title, section_lines in sections:
        if _is_reference_heading(section_title):
            continue
        section_lines = _without_reference_subsections(section_lines)
        text = "\n".join(section_lines).strip()
        if not text:
            continue
        if len(text) <= max_chars:
            output.append((section_title, text))
            continue
        subsection_starts = [
            index
            for index, line in enumerate(section_lines)
            if (match := _HEADING_RE.match(line)) and len(match.group(1)) == 3
        ]
        if subsection_starts:
            prefix = "\n".join(section_lines[: subsection_starts[0]]).strip()
            boundaries = subsection_starts + [len(section_lines)]
            for position in range(len(subsection_starts)):
                start, end = boundaries[position], boundaries[position + 1]
                subsection = "\n".join(section_lines[start:end]).strip()
                match = _HEADING_RE.match(section_lines[start])
                title = f"{section_title} / {match.group(2).strip()}"
                candidate = f"{prefix}\n{subsection}".strip()
                for part_index, part in enumerate(
                    _split_paragraphs(candidate, max_chars=max_chars), start=1
                ):
                    suffix = f" / Part {part_index}" if len(candidate) > max_chars else ""
                    output.append((f"{title}{suffix}", part))
        else:
            for part_index, part in enumerate(
                _split_paragraphs(text, max_chars=max_chars), start=1
            ):
                output.append((f"{section_title} / Part {part_index}", part))
    return output


def load_knowledge_chunks(
    directory: str | Path,
    *,
    max_chars: int = 1600,
) -> list[KnowledgeChunk]:
    """Load Markdown documents and create deterministic heading-aware chunks."""

    root = Path(directory).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"knowledge directory does not exist: {root}")
    chunks: list[KnowledgeChunk] = []
    seen_ids: set[str] = set()
    for path in sorted(root.glob("*.md")):
        metadata, body = _parse_front_matter(
            path.read_text(encoding="utf-8"), path=path
        )
        required = {"document_id", "title", "category", "domain"}
        missing = required - metadata.keys()
        if missing:
            raise ValueError(f"missing metadata in {path}: {sorted(missing)}")
        tags = metadata.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
            raise ValueError(f"tags must be a string list in {path}")
        document_id = str(metadata["document_id"])
        for section_index, (section_title, text) in enumerate(
            _document_sections(body, max_chars=max_chars), start=1
        ):
            chunk_id = f"{document_id}::{section_index:02d}-{_slug(section_title)}"
            if chunk_id in seen_ids:
                raise ValueError(f"duplicate chunk_id: {chunk_id}")
            seen_ids.add(chunk_id)
            chunks.append(
                KnowledgeChunk(
                    document_id=document_id,
                    title=section_title,
                    category=str(metadata["category"]),
                    source_path=path.relative_to(root).as_posix(),
                    chunk_id=chunk_id,
                    domain=str(metadata["domain"]),
                    text=text,
                    tags=tuple(tags),
                )
            )
    if not chunks:
        raise ValueError(f"knowledge directory contains no Markdown chunks: {root}")
    return chunks


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        token = match.group(0)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            tokens.append(token)
            tokens.extend(token[index : index + 2] for index in range(len(token) - 1))
            tokens.extend(token[index : index + 3] for index in range(len(token) - 2))
        else:
            tokens.append(token)
    return tokens


def _alias_present(text: str, alias: str) -> bool:
    normalized = text.casefold()
    candidate = alias.casefold().strip()
    if not candidate:
        return False
    if candidate.isascii() and candidate.replace("_", "").replace("-", "").isalnum():
        return re.search(rf"(?<!\w){re.escape(candidate)}(?!\w)", normalized) is not None
    return candidate in normalized


class KnowledgeRetriever:
    """In-memory BM25 + local semantic embedding retrieval."""

    def __init__(
        self,
        chunks: Sequence[KnowledgeChunk],
        config: RetrievalConfig,
    ) -> None:
        if not chunks:
            raise ValueError("chunks must not be empty")
        if config.rrf_k < 1 or config.embedding_dimensions < 32:
            raise ValueError("invalid retrieval configuration")
        self.chunks = tuple(chunks)
        self.config = config
        self.last_latency_ms = 0.0
        self._tokens = [self._expanded_tokens(chunk.text + " " + chunk.title) for chunk in chunks]
        self._term_frequencies = [Counter(tokens) for tokens in self._tokens]
        self._document_frequency: Counter[str] = Counter()
        for tokens in self._tokens:
            self._document_frequency.update(set(tokens))
        self._average_length = sum(map(len, self._tokens)) / len(self._tokens)
        self._idf = {
            token: math.log(1 + (len(self.chunks) - count + 0.5) / (count + 0.5))
            for token, count in self._document_frequency.items()
        }
        self._embeddings = [self._embed(tokens) for tokens in self._tokens]
        self._token_sets = [set(tokens) for tokens in self._tokens]

    @classmethod
    def from_directory(cls, directory: str | Path) -> KnowledgeRetriever:
        root = Path(directory).resolve()
        return cls(load_knowledge_chunks(root), RetrievalConfig.from_directory(root))

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @property
    def average_chunk_length(self) -> float:
        return sum(len(chunk.text) for chunk in self.chunks) / len(self.chunks)

    def _expanded_tokens(self, text: str) -> list[str]:
        tokens = _tokenize(text)
        for concept, aliases in self.config.semantic_concepts.items():
            if _alias_present(text, concept) or any(
                _alias_present(text, alias) for alias in aliases
            ):
                tokens.extend((f"concept:{concept}", f"concept:{concept}"))
        return tokens

    def _embed(self, tokens: Sequence[str]) -> tuple[float, ...]:
        """Create a deterministic dense TF-IDF feature-hash embedding."""

        vector = [0.0] * self.config.embedding_dimensions
        for token, count in Counter(tokens).items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            raw = int.from_bytes(digest, "big")
            index = raw % self.config.embedding_dimensions
            sign = 1.0 if (raw >> 8) & 1 else -1.0
            idf = self._idf.get(token, math.log(1 + len(self.chunks) / 0.5))
            vector[index] += sign * (1 + math.log(count)) * idf
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return tuple(vector)

    def _lexical_scores(self, query: str) -> list[float]:
        query_terms = self._expanded_tokens(query)
        k1, b = 1.5, 0.75
        scores: list[float] = []
        for terms, frequencies in zip(self._tokens, self._term_frequencies, strict=True):
            length_adjustment = k1 * (
                1 - b + b * len(terms) / max(self._average_length, 1)
            )
            score = 0.0
            for term in query_terms:
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                score += self._idf.get(term, 0.0) * (
                    frequency * (k1 + 1) / (frequency + length_adjustment)
                )
            scores.append(score)
        return scores

    def _semantic_scores(self, query: str) -> list[float]:
        query_tokens = self._expanded_tokens(query)
        query_embedding = self._embed(query_tokens)
        query_set = set(query_tokens)
        query_concepts = {token for token in query_set if token.startswith("concept:")}
        scores: list[float] = []
        for embedding, token_set in zip(
            self._embeddings, self._token_sets, strict=True
        ):
            shared_concepts = query_concepts & token_set
            surface_overlap = {
                token for token in query_set & token_set if not token.startswith("concept:")
            }
            if not shared_concepts and not surface_overlap:
                scores.append(0.0)
                continue
            cosine = sum(
                left * right
                for left, right in zip(query_embedding, embedding, strict=True)
            )
            scores.append(max(0.0, cosine))
        return scores

    @staticmethod
    def _positive_ranks(scores: Sequence[float]) -> dict[int, int]:
        ordered = sorted(
            (index for index, score in enumerate(scores) if score > 1e-12),
            key=lambda index: (-scores[index], index),
        )
        return {index: rank for rank, index in enumerate(ordered, start=1)}

    def _rerank(
        self,
        query: str,
        chunk: KnowledgeChunk,
        fused_score: float,
    ) -> tuple[float, tuple[str, ...]]:
        score = fused_score
        reasons = ["RRF combined lexical and semantic ranks"]
        query_codes = {item.upper() for item in _DEFAULT_ERROR_CODE_RE.findall(query)}
        title_codes = {
            item.upper() for item in _DEFAULT_ERROR_CODE_RE.findall(chunk.title)
        }
        chunk_codes = {
            item.upper()
            for item in _DEFAULT_ERROR_CODE_RE.findall(
                f"{chunk.title} {chunk.text} {' '.join(chunk.tags)}"
            )
        }
        title_matches = sorted(query_codes & title_codes)
        body_matches = sorted((query_codes & chunk_codes) - title_codes)
        if title_matches:
            score += 0.65
            reasons.append(f"exact error-code match: {', '.join(title_matches)}")
        elif body_matches:
            score += 0.10
            reasons.append(f"error-code cross-reference: {', '.join(body_matches)}")

        for concept in self.config.metric_concepts:
            aliases = self.config.semantic_concepts.get(concept, ())
            query_matches = _alias_present(query, concept) or any(
                _alias_present(query, alias) for alias in aliases
            )
            chunk_matches = _alias_present(chunk.text, concept) or any(
                _alias_present(chunk.text, alias) for alias in aliases
            )
            if query_matches and chunk_matches:
                score += 0.20
                reasons.append(f"metric match: {concept}")
                break

        for category, hints in self.config.category_hints.items():
            if category == chunk.category and any(
                _alias_present(query, hint) for hint in hints
            ):
                score += 0.10
                reasons.append(f"category match: {category}")
                break

        title_tokens = set(_tokenize(chunk.title))
        query_tokens = set(_tokenize(query))
        if title_tokens & query_tokens:
            score += 0.05
            reasons.append("heading overlap")
        return score, tuple(reasons)

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        mode: RetrievalMode = "hybrid",
    ) -> list[RetrievalResult]:
        """Retrieve ranked chunks using one explicit, testable mode."""

        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= top_k <= 50:
            raise ValueError("top_k must be between 1 and 50")
        if mode not in {"lexical", "semantic", "hybrid", "hybrid_rerank"}:
            raise ValueError(f"unsupported retrieval mode: {mode}")
        started_at = perf_counter()
        lexical = self._lexical_scores(query)
        semantic = self._semantic_scores(query)
        lexical_ranks = self._positive_ranks(lexical)
        semantic_ranks = self._positive_ranks(semantic)

        if mode == "lexical":
            candidates = set(lexical_ranks)
            base_scores = {index: lexical[index] for index in candidates}
        elif mode == "semantic":
            candidates = set(semantic_ranks)
            base_scores = {index: semantic[index] for index in candidates}
        else:
            candidates = set(lexical_ranks) | set(semantic_ranks)
            raw_fused = {
                index: (
                    (1 / (self.config.rrf_k + lexical_ranks[index]))
                    if index in lexical_ranks
                    else 0.0
                )
                + (
                    (1 / (self.config.rrf_k + semantic_ranks[index]))
                    if index in semantic_ranks
                    else 0.0
                )
                for index in candidates
            }
            maximum = max(raw_fused.values(), default=1.0)
            base_scores = {
                index: value / maximum for index, value in raw_fused.items()
            }

        ranked: list[RetrievalResult] = []
        for index in candidates:
            chunk = self.chunks[index]
            base = base_scores[index]
            if mode == "hybrid_rerank":
                rerank_score, reasons = self._rerank(query, chunk, base)
                fused_score = base
            elif mode == "hybrid":
                rerank_score = base
                fused_score = base
                reasons = ("RRF combined lexical and semantic ranks",)
            else:
                rerank_score = base
                fused_score = 0.0
                reasons = (f"{mode} retrieval score",)
            ranked.append(
                RetrievalResult(
                    text=chunk.text,
                    title=chunk.title,
                    category=chunk.category,
                    source=chunk.source_path,
                    document_id=chunk.document_id,
                    chunk_id=chunk.chunk_id,
                    domain=chunk.domain,
                    tags=chunk.tags,
                    lexical_score=lexical[index],
                    semantic_score=semantic[index],
                    fused_score=fused_score,
                    rerank_score=rerank_score,
                    retrieved_reason=reasons,
                )
            )
        ranked.sort(
            key=lambda item: (
                -item.rerank_score,
                -item.fused_score,
                -item.semantic_score,
                -item.lexical_score,
                item.chunk_id,
            )
        )
        self.last_latency_ms = (perf_counter() - started_at) * 1000
        return ranked[:top_k]

    def retrieve(self, query: str, *, top_k: int = 5) -> list[RetrievalResult]:
        """Public default: hybrid retrieval plus deterministic reranking."""

        return self.search(query, top_k=top_k, mode="hybrid_rerank")


def _default_knowledge_directory(
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    explicit = values.get("DATAPILOT_KNOWLEDGE_PATH", "").strip()
    if explicit:
        return Path(explicit)
    project_value = values.get("WREN_PROJECT_PATH", "").strip()
    if not project_value:
        raise FileNotFoundError(
            "DATAPILOT_KNOWLEDGE_PATH or WREN_PROJECT_PATH is required"
        )
    project = Path(project_value)
    project_directory = project.parent if project.suffix else project
    return project_directory / "knowledge"


def retrieve_knowledge(
    query: str,
    top_k: int = 5,
    *,
    knowledge_dir: str | Path | None = None,
    retriever: KnowledgeRetriever | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[RetrievalResult]:
    """Minimal generic retrieval API requested by the agent boundary."""

    active = retriever or KnowledgeRetriever.from_directory(
        knowledge_dir or _default_knowledge_directory(environ)
    )
    return active.retrieve(query, top_k=top_k)
