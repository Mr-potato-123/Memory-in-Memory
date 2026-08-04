"""Candidate clustering, batch retrieval, and transactional Skill CRUD."""

from __future__ import annotations

import copy
import math
import re
import uuid
from collections import Counter
from typing import Protocol

import numpy as np

from .models import (
    SkillBatchPlan,
    SkillCandidate,
    SkillCandidateBatch,
    SkillOperationType,
    SkillRetrievalRelation,
)
from .repository import SkillRecord, SkillRepository
from .validator import SkillPayloadValidator


class EmbedderLike(Protocol):
    def encode(self, texts: list[str]) -> np.ndarray:
        ...


def _normalize(matrix: np.ndarray) -> np.ndarray:
    array = np.asarray(matrix, dtype=np.float32)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    return array / np.maximum(norms, 1e-12)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", text.lower())


class CandidateClusterer:
    """Small deterministic spherical K-means implementation.

    Embeddings use the agreed field weights. BM25 and shared official-bank
    anchors are deliberately handled after K-means because they are pairwise
    relations rather than fixed dense vectors.
    """

    def __init__(
        self,
        embedder: EmbedderLike,
        *,
        target_cluster_size: int = 8,
        max_batch_size: int = 10,
        description_weight: float = 0.45,
        content_weight: float = 0.35,
        solves_weight: float = 0.20,
        max_iterations: int = 50,
    ):
        self._embedder = embedder
        self._target = max(1, target_cluster_size)
        self._max_batch = max(1, max_batch_size)
        self._weights = (
            description_weight,
            content_weight,
            solves_weight,
        )
        self._max_iterations = max(1, max_iterations)

    def cluster(
        self, candidates: list[SkillCandidate]
    ) -> list[list[SkillCandidate]]:
        if not candidates:
            return []
        sides = {candidate.side for candidate in candidates}
        if len(sides) != 1:
            raise ValueError(
                "Access and Construction candidates must be clustered separately"
            )
        if len(candidates) <= self._max_batch:
            return [list(candidates)]

        vectors = self._candidate_vectors(candidates)
        cluster_count = min(
            len(candidates),
            max(1, math.ceil(len(candidates) / self._target)),
        )
        labels = self._spherical_kmeans(vectors, cluster_count)
        groups = [
            [
                candidate
                for candidate, label in zip(candidates, labels)
                if label == cluster_index
            ]
            for cluster_index in range(cluster_count)
        ]
        return [g for g in groups if g]

    def _candidate_vectors(
        self, candidates: list[SkillCandidate]
    ) -> np.ndarray:
        descriptions = _normalize(
            self._embedder.encode(
                [candidate.payload.description for candidate in candidates]
            )
        )
        contents = _normalize(
            self._embedder.encode(
                [candidate.payload.content_text() for candidate in candidates]
            )
        )
        solves = _normalize(
            self._embedder.encode(
                [candidate.solves for candidate in candidates]
            )
        )
        d_weight, c_weight, s_weight = self._weights
        return _normalize(
            d_weight * descriptions
            + c_weight * contents
            + s_weight * solves
        )

    def _spherical_kmeans(
        self, vectors: np.ndarray, cluster_count: int
    ) -> np.ndarray:
        # Deterministic farthest-first initialization keeps tests and
        # experiments reproducible without adding a scikit-learn dependency.
        centroids = [vectors[0]]
        while len(centroids) < cluster_count:
            current = np.vstack(centroids)
            best_similarity = np.max(vectors @ current.T, axis=1)
            centroids.append(vectors[int(np.argmin(best_similarity))])
        centroid_matrix = _normalize(np.vstack(centroids))
        labels = np.zeros(len(vectors), dtype=np.int32)
        for _ in range(self._max_iterations):
            new_labels = np.argmax(vectors @ centroid_matrix.T, axis=1)
            if np.array_equal(labels, new_labels) and _ > 0:
                break
            labels = new_labels
            updated = []
            for index in range(cluster_count):
                members = vectors[labels == index]
                updated.append(
                    centroid_matrix[index]
                    if not len(members)
                    else np.mean(members, axis=0)
                )
            centroid_matrix = _normalize(np.vstack(updated))
        return labels


class BatchSkillRetriever:
    """Exact candidate-by-official-bank retrieval for one semantic group."""

    def __init__(
        self,
        embedder: EmbedderLike,
        *,
        per_candidate_k: int = 5,
        guaranteed_per_candidate: int = 2,
        max_bank_context: int = 25,
        description_weight: float = 0.50,
        content_weight: float = 0.30,
        lexical_weight: float = 0.20,
    ):
        self._embedder = embedder
        self._per_candidate_k = max(1, per_candidate_k)
        self._guaranteed = max(1, guaranteed_per_candidate)
        self._max_context = max(1, max_bank_context)
        self._weights = (
            description_weight,
            content_weight,
            lexical_weight,
        )

    def retrieve(
        self,
        *,
        batch_id: str,
        candidates: list[SkillCandidate],
        repository: SkillRepository,
        excluded_skill_ids: set[str] | None = None,
    ) -> SkillCandidateBatch:
        if not candidates:
            raise ValueError("A candidate batch cannot be empty")
        side = candidates[0].side
        if any(candidate.side != side for candidate in candidates):
            raise ValueError("A batch cannot mix Access and Construction")
        excluded = excluded_skill_ids or set()
        records = [
            record
            for record in repository.list_active(side)
            if record.skill_id not in excluded
        ]
        if not records:
            return SkillCandidateBatch(
                batch_id=batch_id,
                side=side,
                base_bank_version=repository.current_version,
                candidates=candidates,
            )

        description_scores = self._semantic_matrix(
            [candidate.payload.description for candidate in candidates],
            [record.payload.description for record in records],
        )
        content_scores = self._semantic_matrix(
            [candidate.payload.content_text() for candidate in candidates],
            [record.payload.content_text() for record in records],
        )
        lexical_scores = self._bm25_matrix(
            [
                " ".join(
                    [
                        candidate.payload.description,
                        candidate.payload.content_text(),
                        candidate.solves,
                    ]
                )
                for candidate in candidates
            ],
            [
                " ".join(
                    [
                        record.payload.name,
                        record.payload.description,
                        record.payload.content_text(),
                    ]
                )
                for record in records
            ],
        )
        d_weight, c_weight, l_weight = self._weights
        combined = (
            d_weight * description_scores
            + c_weight * content_scores
            + l_weight * lexical_scores
        )
        record_index = {
            record.skill_id: index for index, record in enumerate(records)
        }

        selected: set[str] = set()
        per_candidate_order: list[np.ndarray] = []
        for candidate_index, candidate in enumerate(candidates):
            order = np.argsort(-combined[candidate_index])
            per_candidate_order.append(order)
            for index in order[: self._guaranteed]:
                selected.add(records[int(index)].skill_id)
            for skill_id in candidate.related_existing_skill_ids:
                if skill_id in record_index:
                    selected.add(skill_id)

        # Fill the remaining context with each candidate's next-best records.
        # This keeps the relation table and the actual LLM-visible records in
        # sync; the previous implementation exposed relation IDs whose Skill
        # bodies were not supplied to CRUD.
        ranked_extras: list[tuple[float, str]] = []
        for candidate_index, order in enumerate(per_candidate_order):
            for index in order[: self._per_candidate_k]:
                record = records[int(index)]
                if record.skill_id not in selected:
                    ranked_extras.append(
                        (float(combined[candidate_index, int(index)]), record.skill_id)
                    )
        for _, skill_id in sorted(ranked_extras, reverse=True):
            if len(selected) >= self._max_context:
                break
            selected.add(skill_id)

        # Cap at max_bank_context, preferring higher-coverage skills.  The
        # normal draft-at-a-time path never reaches this branch, but batch
        # callers remain bounded.
        if len(selected) > self._max_context:
            coverage = np.mean(combined >= 0.55, axis=0)
            ranked = sorted(
                selected,
                key=lambda sid: coverage[record_index.get(sid, 0)],
                reverse=True,
            )
            selected = set(ranked[: self._max_context])

        relations: list[SkillRetrievalRelation] = []
        for candidate_index, candidate in enumerate(candidates):
            visible_indices = {
                record_index[skill_id]
                for skill_id in selected
                if skill_id in record_index
            }
            for index in sorted(
                visible_indices,
                key=lambda value: -combined[candidate_index, value],
            ):
                record = records[index]
                relations.append(
                    SkillRetrievalRelation(
                        candidate_id=candidate.candidate_id,
                        skill_id=record.skill_id,
                        description_similarity=float(
                            description_scores[candidate_index, index]
                        ),
                        content_similarity=float(
                            content_scores[candidate_index, index]
                        ),
                        lexical_similarity=float(
                            lexical_scores[candidate_index, index]
                        ),
                        combined_score=float(
                            combined[candidate_index, index]
                        ),
                        forced_by_candidate=(
                            record.skill_id
                            in candidate.related_existing_skill_ids
                        ),
                    )
                )

        return SkillCandidateBatch(
            batch_id=batch_id,
            side=side,
            base_bank_version=repository.current_version,
            candidates=candidates,
            retrieved_skill_ids=[
                record.skill_id
                for record in records
                if record.skill_id in selected
            ],
            relations=relations,
        )

    def _semantic_matrix(
        self, queries: list[str], documents: list[str]
    ) -> np.ndarray:
        vectors = _normalize(self._embedder.encode([*queries, *documents]))
        query_vectors = vectors[: len(queries)]
        document_vectors = vectors[len(queries):]
        return np.clip(query_vectors @ document_vectors.T, -1.0, 1.0)

    @staticmethod
    def _bm25_matrix(
        queries: list[str], documents: list[str]
    ) -> np.ndarray:
        tokenized_docs = [_tokens(document) for document in documents]
        doc_count = max(len(tokenized_docs), 1)
        avg_len = (
            sum(len(tokens) for tokens in tokenized_docs) / doc_count
        ) or 1.0
        document_frequency = Counter(
            token
            for tokens in tokenized_docs
            for token in set(tokens)
        )
        matrix = np.zeros((len(queries), len(documents)), dtype=np.float32)
        for query_index, query in enumerate(queries):
            query_tokens = set(_tokens(query))
            for doc_index, tokens in enumerate(tokenized_docs):
                frequencies = Counter(tokens)
                score = 0.0
                for token in query_tokens:
                    frequency = frequencies[token]
                    if not frequency:
                        continue
                    inverse_frequency = math.log(
                        1.0
                        + (doc_count - document_frequency[token] + 0.5)
                        / (document_frequency[token] + 0.5)
                    )
                    denominator = frequency + 1.5 * (
                        1.0 - 0.75 + 0.75 * len(tokens) / avg_len
                    )
                    score += inverse_frequency * (
                        frequency * 2.5 / max(denominator, 1e-12)
                    )
                matrix[query_index, doc_index] = score
        row_max = np.max(matrix, axis=1, keepdims=True)
        return matrix / np.maximum(row_max, 1e-12)


class SkillCrudExecutor:
    """Validate and apply a multi-operation batch transaction."""

    def __init__(self, repository: SkillRepository):
        self._repository = repository
        self._validator = SkillPayloadValidator()

    def apply(
        self,
        batch: SkillCandidateBatch,
        plan: SkillBatchPlan,
    ) -> str:
        self._validate_plan(batch, plan)
        working = {
            record.skill_id: copy.deepcopy(record)
            for record in self._repository.list_active(batch.side)
        }
        original = {
            skill_id: copy.deepcopy(record)
            for skill_id, record in working.items()
        }
        deleted: set[str] = set()
        modified: set[str] = set()

        for operation in plan.operations:
            kind = operation.operation
            if kind == SkillOperationType.ADD_SKILL:
                skill_id = operation.skill_id or (
                    f"sk_{batch.side}_{uuid.uuid4().hex[:10]}"
                )
                if skill_id in working:
                    # Two groups or conflict-replanned batches may independently
                    # propose the same skill_id.  Resolve deterministically.
                    skill_id = (
                        f"sk_{batch.side}_{uuid.uuid4().hex[:10]}"
                    )
                if not operation.name or not operation.description:
                    raise ValueError("add_skill requires name and description")
                working[skill_id] = SkillRecord(
                    skill_id=skill_id,
                    version=1,
                    side=batch.side,
                    status="staged",
                    payload=self._payload(
                        operation.name,
                        operation.description,
                        operation.content,
                    ),
                    created_from_failure_id=",".join(
                        operation.source_candidate_ids
                    ),
                )
                modified.add(skill_id)
                continue

            record = working.get(operation.skill_id)
            if record is None:
                raise ValueError(
                    f"CRUD target is not active: {operation.skill_id}"
                )
            if record.side != batch.side:
                raise ValueError("Cross-side Skill mutation is forbidden")
            if (
                operation.expected_skill_version is not None
                and record.version != operation.expected_skill_version
            ):
                raise RuntimeError(
                    f"Stale Skill revision for {record.skill_id}: expected "
                    f"{operation.expected_skill_version}, got {record.version}"
                )

            if kind == SkillOperationType.RENAME_SKILL:
                record.payload.name = operation.name.strip()
            elif kind == SkillOperationType.UPDATE_DESCRIPTION:
                record.payload.description = operation.description.strip()
            elif kind == SkillOperationType.ADD_CONTENT:
                for text in operation.content or [operation.new_content]:
                    text = text.strip()
                    if text and text not in record.payload.content:
                        record.payload.content.append(text)
            elif kind == SkillOperationType.UPDATE_CONTENT:
                index = self._content_index(record, operation)
                record.payload.content[index] = operation.new_content.strip()
            elif kind == SkillOperationType.DELETE_CONTENT:
                index = self._content_index(record, operation)
                record.payload.content.pop(index)
            elif kind == SkillOperationType.MOVE_CONTENT:
                index = self._content_index(record, operation)
                text = record.payload.content.pop(index)
                target = working.get(operation.target_skill_id)
                if target is None or target.side != batch.side:
                    raise ValueError("move_content target is invalid")
                if text not in target.payload.content:
                    target.payload.content.append(text)
                modified.add(target.skill_id)
            elif kind == SkillOperationType.DELETE_SKILL:
                deleted.add(record.skill_id)
            else:
                raise ValueError(f"Unsupported Skill operation: {kind}")
            modified.add(record.skill_id)

        staged: list[SkillRecord] = []
        for skill_id in sorted(modified - deleted):
            record = working[skill_id]
            valid, errors = self._validator.validate(
                record.payload,
                side=batch.side,
            )
            if not valid:
                raise ValueError(
                    f"Invalid final Skill {skill_id}: {'; '.join(errors)}"
                )
            previous = original.get(skill_id)
            if previous is not None:
                record.version = previous.version + 1
                record.parent_version_id = previous.version_id
            record.status = "staged"
            staged.append(record)
        return self._repository.publish_batch(
            staged,
            transaction_id=plan.transaction_id,
            tombstone_skill_ids=sorted(deleted),
            transaction_payload=plan.model_dump(mode="json"),
        )

    def _validate_plan(
        self,
        batch: SkillCandidateBatch,
        plan: SkillBatchPlan,
    ) -> None:
        if plan.side != batch.side:
            raise ValueError("CRUD plan side does not match candidate batch")
        if plan.base_bank_version != self._repository.current_version:
            raise RuntimeError(
                "Official Skill Bank changed after batch retrieval; rebuild "
                "the CRUD plan against the new frozen version"
            )
        candidate_ids = {
            candidate.candidate_id for candidate in batch.candidates
        }
        resolved_ids = [
            resolution.candidate_id
            for resolution in plan.candidate_resolutions
        ]
        if set(resolved_ids) != candidate_ids or len(resolved_ids) != len(
            candidate_ids
        ):
            raise ValueError(
                "Every candidate must have exactly one CRUD resolution"
            )
        for operation in plan.operations:
            if operation.side != batch.side:
                raise ValueError(
                    "CRUD operation side does not match candidate batch: "
                    f"{operation.side} != {batch.side}"
                )
            unknown = set(operation.source_candidate_ids) - candidate_ids
            if unknown:
                raise ValueError(
                    f"Operation references unknown candidates: {unknown}"
                )
            if operation.operation != SkillOperationType.ADD_SKILL:
                if operation.skill_id not in batch.retrieved_skill_ids:
                    raise ValueError(
                        "CRUD operation targets a Skill that was not supplied "
                        f"to the model: {operation.skill_id}"
                    )
            if (
                operation.operation == SkillOperationType.MOVE_CONTENT
                and operation.target_skill_id not in batch.retrieved_skill_ids
            ):
                raise ValueError(
                    "move_content target was not supplied to the model: "
                    f"{operation.target_skill_id}"
                )

    @staticmethod
    def _payload(
        name: str, description: str, content: list[str]
    ):
        from .models import SkillPayload

        return SkillPayload(
            name=name.strip(),
            description=description.strip(),
            content=content,
        )

    @staticmethod
    def _content_index(record: SkillRecord, operation) -> int:
        # ``content_index`` is only a hint into the snapshot shown to the
        # planner.  Earlier operations in the same atomic plan may insert or
        # delete content and therefore shift every later index.  When the
        # planner also supplies the original text, use that stable reference
        # first and fall back to the numeric index only when no text reference
        # is available.
        if operation.expected_content is not None:
            matches = [
                index
                for index, text in enumerate(record.payload.content)
                if text == operation.expected_content
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise RuntimeError(
                    "expected_content is ambiguous for "
                    f"{record.skill_id}: {len(matches)} exact matches"
                )
        if operation.content_index is None:
            if len(record.payload.content) == 1:
                return 0
            raise ValueError(
                f"{operation.operation} requires content_index when the "
                f"target has {len(record.payload.content)} content items"
            )
        index = operation.content_index
        if index < 0 or index >= len(record.payload.content):
            raise IndexError(
                f"content_index out of range for {record.skill_id}: {index}"
            )
        if operation.expected_content is not None:
            raise RuntimeError(
                "Expected content is no longer present before CRUD for "
                f"{record.skill_id}; the plan contains overlapping edits"
            )
        return index
