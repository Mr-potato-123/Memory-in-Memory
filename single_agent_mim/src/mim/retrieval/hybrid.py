"""Pluggable multi-route memory retrieval.

The Access Agent may independently choose:

- ``semantic``: embedding similarity;
- ``bm25``: Okapi BM25 over the complete memory document;
- ``keyword``: exact phrase and token matching;
- ``structured`` / ``temporal``: entities, kinds, and event time;
- ``hybrid``: weighted reciprocal-rank fusion across all routes.

A single call can include semantic query expansions, a distinct keyword list,
and an explicit retrieval depth. This follows Mem0's simple memory collection
model while keeping retrieval policy available to the Access Agent/skills.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

import numpy as np

from .embedder import Embedder
from ..storage.sqlite_store import MemoryHit, SearchFilters, SQLiteMemoryStore


_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:['_-][A-Za-z0-9]+)*|[\u3400-\u4dbf\u4e00-\u9fff]"
)
_DEPTH_MAP = {
    "shallow": 1,
    "standard": 2,
    "deep": 3,
}
_STRATEGY_ALIASES = {
    "vector": "semantic",
    "lexical": "bm25",
    "exact": "keyword",
    "temporal": "structured",
}
_VALID_STRATEGIES = {"semantic", "bm25", "keyword", "structured", "hybrid"}


class HybridRetriever:
    """Search a versioned memory collection through independently usable paths."""

    def __init__(
        self,
        store: SQLiteMemoryStore,
        embedder: Embedder,
        *,
        semantic_candidate_k: int = 30,
        bm25_candidate_k: int = 30,
        keyword_candidate_k: int = 30,
        structured_candidate_k: int = 30,
        result_top_k: int = 8,
        max_result_top_k: int = 24,
        max_query_expansions: int = 4,
        max_depth: int = 3,
        rrf_k: int = 60,
        semantic_weight: float = 0.40,
        bm25_weight: float = 0.30,
        keyword_weight: float = 0.15,
        structured_weight: float = 0.15,
        entity_match_multiplier: float = 1.10,
        time_valid_multiplier: float = 1.20,
        current_active_multiplier: float = 1.05,
        temporal_mismatch_multiplier: float = 0.50,
        bm25_k1: float = 1.5,
        bm25_b: float = 0.75,
    ):
        self._store = store
        self._embedder = embedder
        self._sem_k = semantic_candidate_k
        self._bm25_k = bm25_candidate_k
        self._key_k = keyword_candidate_k
        self._struct_k = structured_candidate_k
        self._top_k = result_top_k
        self._max_top_k = max_result_top_k
        self._max_expansions = max_query_expansions
        self._max_depth = max_depth
        self._rrf_k = rrf_k
        self._weights = {
            "semantic": semantic_weight,
            "bm25": bm25_weight,
            "keyword": keyword_weight,
            "structured": structured_weight,
        }
        self._entity_mul = entity_match_multiplier
        self._time_valid_mul = time_valid_multiplier
        self._current_active_mul = current_active_multiplier
        self._temporal_mismatch_mul = temporal_mismatch_multiplier
        self._bm25_k1 = bm25_k1
        self._bm25_b = bm25_b

    def search(
        self,
        *,
        conversation_id: str,
        snapshot_commit_id: int | None,
        query: str,
        strategy: str = "hybrid",
        filters: SearchFilters | None = None,
        top_k: int | None = None,
        keywords: list[str] | None = None,
        query_expansions: list[str] | None = None,
        depth: int | str = 1,
    ) -> list[MemoryHit]:
        """Execute one agent-planned retrieval call.

        ``query`` is optimized for semantic intent. ``keywords`` carry exact
        names/titles/dates. ``query_expansions`` express alternate meanings or
        individual hops. ``depth`` expands candidate pools, not answer context.
        """
        strategy = _STRATEGY_ALIASES.get(strategy.lower(), strategy.lower())
        if strategy not in _VALID_STRATEGIES:
            strategy = "hybrid"
        filters = filters or SearchFilters(
            conversation_id=conversation_id,
            as_of_commit=snapshot_commit_id,
        )
        depth_value = self._normalize_depth(depth)
        result_k = max(1, min(int(top_k or self._top_k), self._max_top_k))
        semantic_queries = self._clean_queries(query, query_expansions)
        exact_terms = self._clean_terms(keywords)
        lexical_query = " ".join(exact_terms) if exact_terms else query

        if strategy == "semantic":
            hits = self._multi_semantic(
                semantic_queries, filters, self._sem_k * depth_value
            )
        elif strategy == "bm25":
            hits = self._bm25_search(
                lexical_query, filters, self._bm25_k * depth_value
            )
        elif strategy == "keyword":
            hits = self._keyword_search(
                query, exact_terms, filters, self._key_k * depth_value
            )
        elif strategy == "structured":
            hits = self._structured_search(
                query, exact_terms, filters, self._struct_k * depth_value
            )
        else:
            hits = self._hybrid_search(
                semantic_queries=semantic_queries,
                lexical_query=lexical_query,
                exact_query=query,
                exact_terms=exact_terms,
                filters=filters,
                depth=depth_value,
            )

        hits = self.deduplicate(hits, include_history=filters.include_history)
        hits = self._apply_multipliers(hits, filters)
        for rank, hit in enumerate(hits[:result_k], 1):
            hit.rank = rank
        return hits[:result_k]

    def _multi_semantic(
        self,
        queries: list[str],
        filters: SearchFilters,
        candidate_k: int,
    ) -> list[MemoryHit]:
        rankings = [
            self._semantic_search(query, filters, candidate_k)
            for query in queries
            if query
        ]
        return self._fuse_rankings(
            [(f"semantic:q{index}", hits, 1.0) for index, hits in enumerate(rankings)]
        )

    def _semantic_search(
        self,
        query: str,
        filters: SearchFilters,
        candidate_k: int,
    ) -> list[MemoryHit]:
        version_ids, matrix = self._store.get_embeddings_for_snapshot(
            filters.conversation_id,
            filters.as_of_commit,
            include_history=filters.include_history,
        )
        if not version_ids or matrix.shape[0] == 0:
            return []
        snapshot = self._snapshot_map(filters)
        encode_queries = getattr(self._embedder, "encode_queries", None)
        query_vector = (
            encode_queries([query])[0]
            if callable(encode_queries)
            else self._embedder.encode([query])[0]
        )
        scores = np.dot(matrix, query_vector)
        indices = np.argsort(scores)[::-1]
        hits: list[MemoryHit] = []
        for index in indices:
            hit = snapshot.get(version_ids[int(index)])
            if hit is None or not self._passes_hard_filters(hit, filters):
                continue
            hit.score = float(scores[index])
            hit.matched_paths = ["semantic"]
            hits.append(hit)
            if len(hits) >= candidate_k:
                break
        return hits

    def _bm25_search(
        self,
        query: str,
        filters: SearchFilters,
        candidate_k: int,
    ) -> list[MemoryHit]:
        memories = [
            hit
            for hit in self._store.load_snapshot(
                filters.conversation_id,
                filters.as_of_commit,
                include_history=filters.include_history,
            )
            if self._passes_hard_filters(hit, filters)
        ]
        query_tokens = _tokenize(query)
        if not memories or not query_tokens:
            return []

        documents = [_tokenize(_search_text(hit)) for hit in memories]
        doc_freq = Counter()
        for document in documents:
            doc_freq.update(set(document))
        average_length = sum(map(len, documents)) / max(len(documents), 1)
        query_counts = Counter(query_tokens)
        scored: list[tuple[MemoryHit, float]] = []

        for hit, document in zip(memories, documents):
            frequencies = Counter(document)
            length = len(document)
            score = 0.0
            for token, query_frequency in query_counts.items():
                frequency = frequencies.get(token, 0)
                if not frequency:
                    continue
                document_frequency = doc_freq[token]
                inverse_document_frequency = math.log(
                    1
                    + (
                        len(documents)
                        - document_frequency
                        + 0.5
                    )
                    / (document_frequency + 0.5)
                )
                denominator = frequency + self._bm25_k1 * (
                    1
                    - self._bm25_b
                    + self._bm25_b * length / max(average_length, 1.0)
                )
                score += (
                    inverse_document_frequency
                    * frequency
                    * (self._bm25_k1 + 1)
                    / denominator
                    * (1 + math.log(query_frequency))
                )
            if score > 0:
                hit.score = score
                hit.matched_paths = ["bm25"]
                scored.append((hit, score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [hit for hit, _ in scored[:candidate_k]]

    def _keyword_search(
        self,
        query: str,
        keywords: list[str],
        filters: SearchFilters,
        candidate_k: int,
    ) -> list[MemoryHit]:
        phrases = self._clean_terms([*keywords, query])
        query_tokens = set(_tokenize(" ".join(phrases)))
        scored: list[tuple[MemoryHit, float]] = []
        for hit in self._store.load_snapshot(
            filters.conversation_id,
            filters.as_of_commit,
            include_history=filters.include_history,
        ):
            if not self._passes_hard_filters(hit, filters):
                continue
            text = _search_text(hit).casefold()
            tokens = set(_tokenize(text))
            exact_score = sum(
                2.0 + min(len(phrase.split()), 4) * 0.25
                for phrase in phrases
                if phrase.casefold() in text
            )
            overlap_score = len(query_tokens & tokens) / max(len(query_tokens), 1)
            score = exact_score + overlap_score
            if score > 0:
                hit.score = score
                hit.matched_paths = ["keyword"]
                scored.append((hit, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [hit for hit, _ in scored[:candidate_k]]

    def _structured_search(
        self,
        query: str,
        keywords: list[str],
        filters: SearchFilters,
        candidate_k: int,
    ) -> list[MemoryHit]:
        query_tokens = set(_tokenize(" ".join([query, *keywords])))
        scored: list[tuple[MemoryHit, float]] = []
        for hit in self._store.load_snapshot(
            filters.conversation_id,
            filters.as_of_commit,
            include_history=filters.include_history,
        ):
            if not self._passes_hard_filters(hit, filters):
                continue
            score = 0.0
            hit_entities = {entity.casefold() for entity in hit.entities}
            for entity in filters.entities or []:
                entity_key = entity.casefold()
                if entity_key in hit_entities:
                    score += 1.0
                elif entity_key in hit.subject.casefold():
                    score += 0.8
                elif entity_key in hit.content.casefold():
                    score += 0.4
            content_tokens = set(_tokenize(_search_text(hit)))
            score += len(query_tokens & content_tokens) / max(len(query_tokens), 1)
            score += _time_score(hit, filters)
            if score > 0:
                hit.score = score
                hit.matched_paths = ["structured"]
                scored.append((hit, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [hit for hit, _ in scored[:candidate_k]]

    def _hybrid_search(
        self,
        *,
        semantic_queries: list[str],
        lexical_query: str,
        exact_query: str,
        exact_terms: list[str],
        filters: SearchFilters,
        depth: int,
    ) -> list[MemoryHit]:
        rankings: list[tuple[str, list[MemoryHit], float]] = []
        for index, semantic_query in enumerate(semantic_queries):
            rankings.append(
                (
                    f"semantic:q{index}",
                    self._semantic_search(
                        semantic_query, filters, self._sem_k * depth
                    ),
                    self._weights["semantic"] / len(semantic_queries),
                )
            )
        rankings.extend(
            [
                (
                    "bm25",
                    self._bm25_search(
                        lexical_query, filters, self._bm25_k * depth
                    ),
                    self._weights["bm25"],
                ),
                (
                    "keyword",
                    self._keyword_search(
                        exact_query,
                        exact_terms,
                        filters,
                        self._key_k * depth,
                    ),
                    self._weights["keyword"],
                ),
                (
                    "structured",
                    self._structured_search(
                        exact_query,
                        exact_terms,
                        filters,
                        self._struct_k * depth,
                    ),
                    self._weights["structured"],
                ),
            ]
        )
        return self._fuse_rankings(rankings)

    def _fuse_rankings(
        self,
        rankings: Iterable[tuple[str, list[MemoryHit], float]],
    ) -> list[MemoryHit]:
        scores: dict[str, float] = {}
        hits_by_id: dict[str, MemoryHit] = {}
        paths: dict[str, list[str]] = {}
        for path, hits, weight in rankings:
            for rank, hit in enumerate(hits, 1):
                scores[hit.version_id] = scores.get(hit.version_id, 0.0) + (
                    weight / (self._rrf_k + rank)
                )
                hits_by_id.setdefault(hit.version_id, hit)
                base_path = path.split(":", 1)[0]
                if base_path not in paths.setdefault(hit.version_id, []):
                    paths[hit.version_id].append(base_path)

        fused: list[MemoryHit] = []
        for version_id in sorted(
            scores, key=lambda item: scores[item], reverse=True
        ):
            hit = hits_by_id[version_id]
            hit.score = scores[version_id]
            hit.matched_paths = paths[version_id]
            fused.append(hit)
        return fused

    @staticmethod
    def deduplicate(
        hits: list[MemoryHit],
        include_history: bool = False,
    ) -> list[MemoryHit]:
        if include_history:
            return sorted(hits, key=lambda item: item.score, reverse=True)
        logical: dict[str, MemoryHit] = {}
        for hit in sorted(hits, key=lambda item: item.score, reverse=True):
            logical.setdefault(hit.memory_id, hit)
        return sorted(logical.values(), key=lambda item: item.score, reverse=True)

    def _snapshot_map(self, filters: SearchFilters) -> dict[str, MemoryHit]:
        return {
            hit.version_id: hit
            for hit in self._store.load_snapshot(
                filters.conversation_id,
                filters.as_of_commit,
                include_history=filters.include_history,
            )
        }

    @staticmethod
    def _passes_hard_filters(
        hit: MemoryHit,
        filters: SearchFilters,
    ) -> bool:
        if filters.memory_kinds and hit.memory_kind not in filters.memory_kinds:
            return False
        return True

    def _apply_multipliers(
        self,
        hits: list[MemoryHit],
        filters: SearchFilters,
    ) -> list[MemoryHit]:
        for hit in hits:
            if filters.entities:
                hit_entities = {entity.casefold() for entity in hit.entities}
                filter_entities = {
                    entity.casefold() for entity in filters.entities
                }
                if hit_entities & filter_entities:
                    hit.score *= self._entity_mul
            if filters.time_mode == "current" and hit.system_to_commit is None:
                hit.score *= self._current_active_mul
            if filters.time_mode != "none" and filters.target_time:
                hit.score *= (
                    self._time_valid_mul
                    if _time_valid(hit, filters)
                    else self._temporal_mismatch_mul
                )
        return sorted(hits, key=lambda item: item.score, reverse=True)

    def _normalize_depth(self, depth: int | str) -> int:
        if isinstance(depth, str):
            if depth.isdigit():
                depth = int(depth)
            else:
                depth = _DEPTH_MAP.get(depth.casefold(), 1)
        try:
            return max(1, min(int(depth), self._max_depth))
        except (TypeError, ValueError):
            return 1

    def _clean_queries(
        self,
        query: str,
        expansions: list[str] | None,
    ) -> list[str]:
        values = [query, *(expansions or [])]
        return [
            value
            for value in dict.fromkeys(
                str(item).strip() for item in values if item
            )
            if value
        ][: 1 + self._max_expansions]

    @staticmethod
    def _clean_terms(terms: list[str] | None) -> list[str]:
        return [
            value
            for value in dict.fromkeys(
                str(item).strip() for item in (terms or []) if item
            )
            if value
        ]


def _tokenize(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(text or "")]


def _search_text(hit: MemoryHit) -> str:
    return " ".join(
        value
        for value in [
            hit.content,
            hit.subject,
            hit.predicate or "",
            " ".join(hit.entities),
        ]
        if value
    )


def _time_score(hit: MemoryHit, filters: SearchFilters) -> float:
    if filters.time_mode == "none":
        return 0.0
    if filters.time_mode == "current":
        return 1.0 if hit.system_to_commit is None else 0.0
    if not filters.target_time:
        return 0.1
    if filters.time_mode == "point":
        return 1.0 if _time_valid(hit, filters) else 0.0
    if filters.time_mode == "before":
        return (
            0.8
            if hit.world_start and hit.world_start < filters.target_time
            else 0.0
        )
    if filters.time_mode == "after":
        return (
            0.8
            if hit.world_start and hit.world_start >= filters.target_time
            else 0.0
        )
    if filters.time_mode == "range":
        return 1.0 if _time_valid(hit, filters) else 0.0
    return 0.0


def _time_valid(hit: MemoryHit, filters: SearchFilters) -> bool:
    if filters.time_mode == "none":
        return True
    if filters.time_mode == "current":
        return hit.system_to_commit is None
    if not filters.target_time:
        return True
    if filters.time_mode == "point":
        world_start = hit.world_start or ""
        world_end = hit.world_end or "z"
        return world_start <= filters.target_time < world_end
    if filters.time_mode == "before":
        return bool(hit.world_start and hit.world_start < filters.target_time)
    if filters.time_mode == "after":
        return bool(hit.world_start and hit.world_start >= filters.target_time)
    if filters.time_mode == "range":
        query_end = filters.target_time_end or filters.target_time
        memory_start = hit.world_start or ""
        memory_end = hit.world_end or "z"
        return memory_start <= query_end and memory_end >= filters.target_time
    return True
