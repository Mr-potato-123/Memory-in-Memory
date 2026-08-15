"""Runtime-facing Skill Bank.

The maintenance side owns versioned CRUD in ``skill_maker.repository``.
This module is the only runtime adapter: it exposes active three-field Skills
and uses the same retrieval implementation in use/train/evaluate/replay.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
import threading
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .schemas import (
    Side,
    SkillRecord as RuntimeSkillRecord,
    SkillRetrievalTrace,
    SkillStatus,
    SkillTraceItem,
)
from .llm.base import ModelClient
from .skill_maker.repository import SkillRepository, SkillRecord


class EmbedderLike(Protocol):
    def encode(self, texts: list[str]) -> np.ndarray:
        ...

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        ...

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        ...


@dataclass(frozen=True)
class RankedSkill:
    record: SkillRecord
    score: float
    semantic_score: float
    lexical_score: float


@dataclass(frozen=True)
class SkillRerankResult:
    selected_ids: list[str]
    reasons: dict[str, str]
    error: str = ""


class SkillRerankerLike(Protocol):
    name: str

    def rerank(
        self,
        *,
        query: str,
        side: Side,
        candidates: list[RankedSkill],
        max_selected: int,
    ) -> SkillRerankResult:
        ...


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", text.lower())


def _bm25_scores(queries: list[str], documents: list[str]) -> np.ndarray:
    """Return per-document BM25 scores, max-pooled over query segments."""
    if not documents:
        return np.empty((0,), dtype=np.float32)
    tokenized_docs = [_tokens(document) for document in documents]
    document_count = len(tokenized_docs)
    average_length = (
        sum(len(tokens) for tokens in tokenized_docs) / max(document_count, 1)
    ) or 1.0
    document_frequency = Counter(
        token for tokens in tokenized_docs for token in set(tokens)
    )
    pooled = np.zeros(document_count, dtype=np.float32)
    for query in queries:
        row = np.zeros(document_count, dtype=np.float32)
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
                    + (document_count - document_frequency[token] + 0.5)
                    / (document_frequency[token] + 0.5)
                )
                denominator = frequency + 1.5 * (
                    1.0 - 0.75 + 0.75 * len(tokens) / average_length
                )
                score += inverse_frequency * (
                    frequency * 2.5 / max(denominator, 1e-12)
                )
            row[doc_index] = score
        # Keep lexical scores comparable across queries.  Max-normalising
        # every row made the best Skill score 1.0 even when it matched only a
        # generic word, which in turn forced one Construction Skill to fire
        # for virtually every session.  Average over query terms and apply a
        # bounded transform instead of ranking-relative normalisation.
        row /= max(len(query_tokens), 1)
        row = 1.0 - np.exp(-row)
        pooled = np.maximum(pooled, row)
    return pooled


def _rank_records(
    query: str,
    records: list[SkillRecord],
    embedder: EmbedderLike,
    top_k: int | None,
    query_segments: list[str] | None = None,
    document_vectors: np.ndarray | None = None,
) -> list[RankedSkill]:
    if not records:
        return []
    # Runtime matching is trigger matching.  ``description`` defines when a
    # Skill applies; ``content`` is an instruction to execute only after that
    # trigger has matched.  Including content here made broad operational
    # words (search, memory, person, event) dominate and caused narrow Skills
    # to fire on unrelated questions.
    texts = [f"{r.payload.name}. {r.payload.description}" for r in records]
    queries = [
        item.strip()
        for item in (query_segments or [query])
        if item and item.strip()
    ] or [query]
    lexical = _bm25_scores(queries, texts)
    try:
        encode_queries = getattr(embedder, "encode_queries", embedder.encode)
        encode_documents = getattr(embedder, "encode_documents", embedder.encode)
        query_vecs = encode_queries(queries)
        doc_vecs = (
            document_vectors
            if document_vectors is not None
            else encode_documents(texts)
        )
        query_norms = np.linalg.norm(query_vecs, axis=1, keepdims=True)
        doc_norms = np.linalg.norm(doc_vecs, axis=1, keepdims=True)
        normalized_queries = query_vecs / np.maximum(query_norms, 1e-12)
        normalized_docs = doc_vecs / np.maximum(doc_norms, 1e-12)
        semantic = np.max(normalized_queries @ normalized_docs.T, axis=0)
        scores = 0.70 * semantic + 0.30 * lexical
    except Exception:
        semantic = np.zeros_like(lexical)
        scores = lexical
    order = np.argsort(-scores)
    if top_k is not None:
        order = order[:top_k]
    return [
        RankedSkill(
            record=records[int(index)],
            score=float(scores[int(index)]),
            semantic_score=float(semantic[int(index)]),
            lexical_score=float(lexical[int(index)]),
        )
        for index in order
    ]


class LLMSkillApplicabilityReranker:
    """Strict second-stage router over a small hybrid-retrieved candidate set."""

    name = "bank1_applicability_router"

    def __init__(self, model: ModelClient):
        self._model = model

    def rerank(
        self,
        *,
        query: str,
        side: Side,
        candidates: list[RankedSkill],
        max_selected: int,
    ) -> SkillRerankResult:
        if not candidates or max_selected <= 0:
            return SkillRerankResult([], {})
        side_guidance = (
            "The task includes a question and its FIRST DEFAULT SEARCH result. "
            "Select a Skill only when that result is demonstrably incomplete "
            "and the Skill's full trigger identifies the missing recovery action. "
            "If the result directly supports a complete answer, select nothing. "
            "Topic overlap alone is not enough. Never select a Skill for "
            "questions that are unanswerable or adversarial (no memory supports "
            "the claim): Skills must not encourage guessing or inference."
            if side == Side.ACCESS
            else
            "Select a Skill only when the session contains its trigger and its "
            "procedure would materially change extraction or memory CRUD. Topic "
            "overlap is not enough."
        )
        candidate_payload = [
            {
                "skill_id": item.record.skill_id,
                "name": item.record.payload.name,
                "description": item.record.payload.description,
                "content": list(item.record.payload.content),
                "first_stage_score": round(item.score, 6),
            }
            for item in candidates
        ]
        prompt = f"""You are a strict Skill applicability router.

{side_guidance}

Rules:
1. Select zero to {max_selected} Skills. A matching learned Skill may guide the
   next recovery action; it never replaces the first default search.
2. Judge procedural applicability, not shared nouns or broad semantic similarity.
3. Reject redundant, conflicting, over-broad, or merely topical Skills.
4. Do not answer the question and do not invent a new Skill.
5. Every selection requires a short quote/paraphrase from the task proving
   both the trigger and the unresolved evidence gap.
6. Return JSON only: {{"selected":[{{"skill_id":"...","reason":"...",
   "trigger_evidence":"...","unresolved_gap":"..."}}]}}.

Side: {side.value}
Task/session:
{query}

Candidates:
{json.dumps(candidate_payload, ensure_ascii=False)}
"""
        try:
            response = self._model.generate(
                [
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": "Select only genuinely applicable Skills or abstain.",
                    },
                ],
                temperature=0.0,
                max_tokens=600,
                json_mode=True,
            )
            data = self._parse_json(response.text)
            raw_selected = data.get("selected", [])
            if not isinstance(raw_selected, list):
                raise ValueError("selected must be a list")
            allowed = {item.record.skill_id for item in candidates}
            selected_ids: list[str] = []
            reasons: dict[str, str] = {}
            for raw in raw_selected:
                if isinstance(raw, str):
                    skill_id, reason = raw, "Selected by applicability reranker."
                elif isinstance(raw, dict):
                    skill_id = str(raw.get("skill_id") or "")
                    reason = str(raw.get("reason") or "").strip()
                    trigger_evidence = str(
                        raw.get("trigger_evidence") or ""
                    ).strip()
                    unresolved_gap = str(raw.get("unresolved_gap") or "").strip()
                else:
                    continue
                if (
                    skill_id not in allowed
                    or skill_id in selected_ids
                    or not reason
                    or not trigger_evidence
                    or (side == Side.ACCESS and not unresolved_gap)
                    or re.search(
                        r"\b(?:does not|doesn't|not applicable|no gap|already "
                        r"complete|fully supports)\b",
                        " ".join([reason, trigger_evidence, unresolved_gap]),
                        re.IGNORECASE,
                    )
                ):
                    continue
                selected_ids.append(skill_id)
                reasons[skill_id] = reason
                if len(selected_ids) >= max_selected:
                    break
            return SkillRerankResult(selected_ids, reasons)
        except Exception as exc:
            # Router failure must not inject learned behaviour. The default
            # policy remains available, so abstention is the safe fallback.
            fallback: list[str] = []
            return SkillRerankResult(
                fallback,
                {
                    skill_id: "Conservative first-stage fallback after reranker error."
                    for skill_id in fallback
                },
                error=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _parse_json(text: str) -> dict:
        clean = text.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.I)
            clean = re.sub(r"\s*```$", "", clean)
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            start, end = clean.find("{"), clean.rfind("}")
            if start < 0 or end <= start:
                raise
            data = json.loads(clean[start:end + 1])
        if not isinstance(data, dict):
            raise ValueError("Reranker output must be a JSON object")
        return data


class SkillBank:
    """Read-only Runtime view of either an internal repository or Bank1.

    ``bank0`` is represented by running Runtime in ``base`` mode, so it has no
    Skill files at all.  Every learned Bank is deliberately split into one
    Access file and one Construction file.  Runtime never loads the combined
    maintenance-side ``selected.json`` format.
    """

    @staticmethod
    def published_filename(side: Side | str, bank_number: int) -> str:
        side_value = side.value if isinstance(side, Side) else str(side)
        if side_value not in {Side.ACCESS.value, Side.CONSTRUCTION.value}:
            raise ValueError(f"Unknown Skill side: {side_value}")
        if bank_number < 1:
            raise ValueError("Published Bank number must be at least 1.")
        return f"{side_value}_skill_bank_v{bank_number}.json"

    def __init__(self, skills_dir: str | Path):
        self._dir = Path(skills_dir)
        self._repo = SkillRepository(self._dir)
        self._frozen = False
        self._selected: list[SkillRecord] | None = None
        self._published_name = self._repo.current_version
        self._embedding_cache: dict[tuple[int, tuple[str, ...]], np.ndarray] = {}
        self._embedding_cache_lock = threading.Lock()
        self._persistent_embedding_cache: dict[tuple[str, tuple[str, ...]], np.ndarray] = {}

    @classmethod
    def load_or_create(cls, skills_dir: str | Path) -> "SkillBank":
        return cls(skills_dir)

    @classmethod
    def from_repository(cls, repository: SkillRepository) -> "SkillBank":
        bank = cls(repository.directory)
        bank._repo = repository
        bank._published_name = repository.current_version
        return bank

    @classmethod
    def from_records(
        cls,
        records: list[dict],
        *,
        bank_name: str,
    ) -> "SkillBank":
        """Build a frozen in-memory view for maintenance-side validation."""
        bank = cls.__new__(cls)
        bank._dir = Path(".")
        bank._repo = None
        bank._frozen = True
        bank._selected = [
            SkillRecord.from_dict(item)
            for item in records
            if item.get("status", "active") == "active"
        ]
        bank._published_name = bank_name
        bank._embedding_cache = {}
        bank._embedding_cache_lock = threading.Lock()
        bank._persistent_embedding_cache = {}
        match = re.fullmatch(r"bank([1-9][0-9]*)", bank_name)
        bank._published_number = int(match.group(1)) if match else 0
        return bank

    @classmethod
    def read_published_records(
        cls,
        bank_dir: str | Path,
    ) -> tuple[str, int, list[dict]]:
        """Read and validate one physically isolated published Bank."""
        directory = Path(bank_dir)
        discovered: dict[str, tuple[int, Path]] = {}
        pattern = re.compile(
            r"^(access|construction)_skill_bank_v([1-9][0-9]*)\.json$"
        )
        for path in directory.glob("*_skill_bank_v*.json"):
            match = pattern.match(path.name)
            if not match:
                continue
            side, raw_number = match.groups()
            if side in discovered:
                raise ValueError(
                    f"Multiple published {side} files found in {directory}."
                )
            discovered[side] = (int(raw_number), path)
        if set(discovered) != {Side.ACCESS.value, Side.CONSTRUCTION.value}:
            raise FileNotFoundError(
                "Published Bank requires exactly one Access file and one "
                f"Construction file: {directory}"
            )
        numbers = {item[0] for item in discovered.values()}
        if len(numbers) != 1:
            raise ValueError(
                f"Access and Construction Bank versions differ: {numbers}"
            )
        bank_number = numbers.pop()
        bank_name = f"bank{bank_number}"
        records: list[dict] = []
        for side in (Side.ACCESS.value, Side.CONSTRUCTION.value):
            _, path = discovered[side]
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("bank") != bank_name or data.get("side") != side:
                raise ValueError(
                    f"Invalid {path.name}: expected bank={bank_name} "
                    f"and side={side}."
                )
            side_records = data.get("skills", [])
            wrong_side = [
                item.get("skill_id", "")
                for item in side_records
                if item.get("side") != side
            ]
            if wrong_side:
                raise ValueError(
                    f"{path.name} contains wrong-side Skills: {wrong_side[:5]}"
                )
            records.extend(side_records)
        return bank_name, bank_number, records

    @classmethod
    def load_published(cls, bank_dir: str | Path) -> "SkillBank":
        """Load one published Bank without exposing maintenance snapshots."""
        bank_name, _, records = cls.read_published_records(bank_dir)
        bank = cls.from_records(records, bank_name=bank_name)
        # ``from_records`` is also used for ephemeral validation views.  A
        # published view, however, must retain its directory so the optional
        # persistent embedding index can be loaded by every evaluator process.
        bank._dir = Path(bank_dir)
        return bank

    @staticmethod
    def _embedding_text(record: SkillRecord) -> str:
        return f"{record.payload.name}. {record.payload.description}"

    @staticmethod
    def _answer_side_compatible(record: SkillRecord) -> bool:
        """Return whether an Access Skill fits the fixed Mem0 topology.

        Mem0-native Access performs exactly one default search followed by one
        answer call.  A large part of the first Bank was learned from the
        older iterative Access agent and contains instructions such as
        ``perform a supplemental search``.  Injecting those instructions into
        a single-pass answer prompt makes the model invent a second search or
        treat a topical match as a recovery trigger.  Keep only procedures
        that can be executed over the already returned evidence.
        """
        text = " ".join(
            [
                record.payload.description,
                *record.payload.content,
            ]
        ).casefold()
        retrieval_actions = (
            r"\ba[12]\b",
            r"\b(?:supplemental|additional|extra|another)\s+(?:search|retrieval)\b",
            r"\b(?:increase|raise|change|adjust|set)\b[^.]{0,60}\b(?:top[ -]?k|depth)\b",
            r"\b(?:perform|do|issue|run|conduct)\b[^.]{0,80}\b(?:search|retrieval)\b",
            r"\b(?:search|retrieve|reformulate|expand)\b[^.]{0,80}\b(?:query|using|with)\b",
            r"\bsearch\s+again\b",
            r"\bretrieve\s+both\b",
        )
        return not any(re.search(pattern, text) for pattern in retrieval_actions)

    @classmethod
    def _records_fingerprint(cls, records: list[SkillRecord]) -> str:
        payload = [
            {
                "version_id": record.version_id,
                "text": cls._embedding_text(record),
            }
            for record in records
        ]
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _embedder_name(embedding_index: EmbedderLike) -> str:
        return str(getattr(embedding_index, "model_name", ""))

    @staticmethod
    def _cache_path(directory: Path) -> Path:
        return directory / "skill_embeddings.npz"

    def precompute_embeddings(
        self,
        embedding_index: EmbedderLike,
        *,
        force: bool = False,
    ) -> Path:
        """Persist document-side Skill vectors for reuse across processes.

        The cache is deliberately tied to the exact active Skill texts and the
        embedding model identity.  It is therefore safe for a newly published
        bank to coexist with older caches, and stale caches are ignored rather
        than silently used.
        """
        if self._selected is not None:
            records = list(self._selected)
        else:
            records = self.repository.list_active()
        records = list(records)
        texts = [self._embedding_text(record) for record in records]
        version_ids = [record.version_id for record in records]
        fingerprint = self._records_fingerprint(records)
        model_name = self._embedder_name(embedding_index)
        path = self._cache_path(self._dir)
        if not force:
            loaded = self._load_persistent_embeddings(
                embedding_index, records, allow_missing=True
            )
            if loaded is not None:
                return path
        encode_documents = getattr(
            embedding_index, "encode_documents", embedding_index.encode
        )
        vectors = encode_documents(texts)
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[0] != len(records):
            raise ValueError(
                "Skill embedding encoder returned an invalid shape: "
                f"{vectors.shape}; expected ({len(records)}, dim)."
            )
        metadata = {
            "schema_version": 1,
            "model_name": model_name,
            "dimension": int(vectors.shape[1]),
            "records_sha256": fingerprint,
            "version_ids": version_ids,
        }
        self._dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("wb") as handle:
            np.savez_compressed(
                handle,
                vectors=vectors,
                metadata=np.asarray(json.dumps(metadata, ensure_ascii=False)),
            )
        temporary.replace(path)
        key = (model_name, tuple(version_ids))
        with self._embedding_cache_lock:
            self._persistent_embedding_cache[key] = vectors
        return path

    def _load_persistent_embeddings(
        self,
        embedding_index: EmbedderLike,
        records: list[SkillRecord],
        *,
        allow_missing: bool = False,
    ) -> np.ndarray | None:
        """Load and validate the on-disk document vectors for ``records``."""
        version_ids = tuple(record.version_id for record in records)
        model_name = self._embedder_name(embedding_index)
        key = (model_name, version_ids)
        with self._embedding_cache_lock:
            cached = self._persistent_embedding_cache.get(key)
            if cached is not None:
                return cached
        path = self._cache_path(self._dir)
        if not path.exists():
            if allow_missing:
                return None
            return None
        try:
            with np.load(path, allow_pickle=False) as payload:
                vectors = np.asarray(payload["vectors"], dtype=np.float32)
                raw_metadata = payload["metadata"]
                metadata_text = str(raw_metadata.item())
                metadata = json.loads(metadata_text)
            expected_ids = [record.version_id for record in records]
            if metadata.get("schema_version") != 1:
                return None
            if metadata.get("model_name", "") != model_name:
                return None
            if metadata.get("version_ids") != expected_ids:
                return None
            if metadata.get("records_sha256") != self._records_fingerprint(records):
                return None
            if vectors.ndim != 2 or vectors.shape[0] != len(records):
                return None
            expected_dim = getattr(embedding_index, "dim", None)
            if expected_dim is not None and int(expected_dim) != vectors.shape[1]:
                return None
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None
        with self._embedding_cache_lock:
            self._persistent_embedding_cache[key] = vectors
        return vectors

    @classmethod
    def export_published(
        cls,
        selected_snapshot: str | Path,
        bank_dir: str | Path,
        *,
        bank_number: int,
    ) -> Path:
        """Export one snapshot as two Runtime-only published Bank files."""
        if bank_number < 1:
            raise ValueError("Published Bank number must be at least 1.")
        bank_name = f"bank{bank_number}"
        snapshot_path = Path(selected_snapshot)
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        records = [
            item
            for item in data.get("skills", [])
            if item.get("status", "active") == "active"
        ]
        directory = Path(bank_dir)
        directory.mkdir(parents=True, exist_ok=True)
        for side in (Side.ACCESS.value, Side.CONSTRUCTION.value):
            filename = cls.published_filename(side, bank_number)
            payload = {
                "schema_version": 1,
                "bank": bank_name,
                "side": side,
                "created_at": data.get("created_at", ""),
                "source_snapshot": snapshot_path.name,
                "skills": [item for item in records if item.get("side") == side],
            }
            destination = directory / filename
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(destination)
        # Loading the publication is the final schema and isolation check.
        cls.load_published(directory)
        return directory

    @property
    def version(self) -> int:
        if self._repo is None:
            return self._published_number
        return int(self._repo.current_version.removeprefix("v"))

    @property
    def repository(self) -> SkillRepository:
        if self._repo is None:
            raise RuntimeError("Published Bank1 has no mutable repository.")
        return self._repo

    def _active_records(self, side: Side | str | None = None) -> list[SkillRecord]:
        side_value = side.value if isinstance(side, Side) else side
        records = (
            self._selected
            if self._selected is not None
            else self.repository.list_active()
        )
        if side_value:
            records = [r for r in records if r.side == side_value]
        return list(records)

    @staticmethod
    def _to_runtime(record: SkillRecord) -> RuntimeSkillRecord:
        return RuntimeSkillRecord(
            skill_id=record.skill_id,
            version=record.version,
            side=Side(record.side),
            name=record.payload.name,
            description=record.payload.description,
            content=record.payload.content,
            status=SkillStatus.ACTIVE,
            parent_versions=(
                [record.parent_version_id] if record.parent_version_id else []
            ),
            created_from_failures=(
                [record.created_from_failure_id]
                if record.created_from_failure_id else []
            ),
        )

    def retrieve_with_trace(
        self,
        query: str,
        side: Side,
        embedding_index: EmbedderLike,
        top_k: int = 3,
        candidate_k: int = 10,
        disclose_k: int = 5,
        min_score: float = 0.0,
        min_semantic_score: float = 0.0,
        min_score_margin: float = 0.0,
        answer_only: bool = False,
        reranker: SkillRerankerLike | None = None,
        query_segments: list[str] | None = None,
        trace_id: str | None = None,
    ) -> tuple[list[RuntimeSkillRecord], SkillRetrievalTrace]:
        """Hybrid-retrieve official Skills, then optionally rerank applicability."""
        first_stage_k = max(
            max(0, candidate_k),
            max(0, top_k) + max(0, disclose_k),
        )
        active_records = self._active_records(side)
        texts = [
            self._embedding_text(record)
            for record in active_records
        ]
        cache_key = (
            id(embedding_index),
            tuple(record.version_id for record in active_records),
        )
        with self._embedding_cache_lock:
            document_vectors = self._embedding_cache.get(cache_key)
        if document_vectors is None and texts:
            # Prefer the bank-local persistent index.  It is generated once at
            # publication time and shared by all evaluator processes; a
            # missing/stale index falls back to the existing in-process cache.
            all_records = self._active_records()
            all_vectors = self._load_persistent_embeddings(
                embedding_index, all_records, allow_missing=True
            )
            if all_vectors is not None:
                positions = {
                    record.version_id: index
                    for index, record in enumerate(all_records)
                }
                indices = [
                    positions[record.version_id] for record in active_records
                ]
                document_vectors = all_vectors[indices]
            else:
                encode_documents = getattr(
                    embedding_index, "encode_documents", embedding_index.encode
                )
                document_vectors = encode_documents(texts)
            with self._embedding_cache_lock:
                self._embedding_cache[cache_key] = document_vectors
        ranked = _rank_records(
            query,
            active_records,
            embedding_index,
            first_stage_k,
            query_segments=query_segments,
            document_vectors=document_vectors,
        )
        candidates = [
            item
            for item in ranked
            if item.score >= min_score
            and item.semantic_score >= min_semantic_score
        ][: max(0, candidate_k)]
        if answer_only and side == Side.ACCESS:
            candidates = [
                item
                for item in candidates
                if self._answer_side_compatible(item.record)
            ]
        # A narrow procedural trigger should be clearly more applicable than
        # its nearest alternative.  Ambiguity means abstain, not arbitrarily
        # inject the top-ranked instruction into Runtime.
        if candidates and min_score_margin > 0.0 and len(ranked) > 1:
            if candidates[0].score - ranked[1].score < min_score_margin:
                candidates = []
        rerank_result = (
            reranker.rerank(
                query=query,
                side=side,
                candidates=candidates,
                max_selected=max(0, top_k),
            )
            if reranker is not None
            else SkillRerankResult(
                [item.record.skill_id for item in candidates[: max(0, top_k)]],
                {},
            )
        )
        by_skill_id = {item.record.skill_id: item for item in candidates}
        selected_ranked = [
            by_skill_id[skill_id]
            for skill_id in rerank_result.selected_ids
            if skill_id in by_skill_id
        ]
        selected_versions = {
            item.record.version_id for item in selected_ranked
        }
        nearby_ranked = [
            item
            for item in ranked
            if item.record.version_id not in selected_versions
        ][: max(0, disclose_k)]
        raw_ranks = {
            item.record.version_id: rank
            for rank, item in enumerate(ranked, 1)
        }
        rerank_ranks = {
            skill_id: rank
            for rank, skill_id in enumerate(rerank_result.selected_ids, 1)
        }

        def item(
            ranked_skill: RankedSkill,
            rank: int,
            *,
            selected: bool,
        ) -> SkillTraceItem:
            record = ranked_skill.record
            return SkillTraceItem(
                skill_id=record.skill_id,
                version_id=record.version_id,
                rank=rank,
                score=ranked_skill.score,
                semantic_score=ranked_skill.semantic_score,
                lexical_score=ranked_skill.lexical_score,
                name=record.payload.name,
                description=record.payload.description,
                content=list(record.payload.content),
                selected=selected,
                rerank_rank=rerank_ranks.get(record.skill_id),
                rerank_reason=rerank_result.reasons.get(record.skill_id, ""),
            )

        trace = SkillRetrievalTrace(
            trace_id=trace_id or f"skilltrace_{uuid.uuid4().hex[:12]}",
            side=side,
            bank_version=self._published_name,
            query=query,
            top_k=max(0, top_k),
            disclose_k=max(0, disclose_k),
            min_score=float(min_score),
            min_semantic_score=float(min_semantic_score),
            min_score_margin=float(min_score_margin),
            scoring_weights={"semantic": 0.70, "bm25": 0.30},
            retrieval_method="bank1_hybrid_router",
            candidate_k=max(0, candidate_k),
            reranker=reranker.name if reranker is not None else "none",
            reranker_error=rerank_result.error,
            selected=[
                item(
                    ranked_skill,
                    raw_ranks[ranked_skill.record.version_id],
                    selected=True,
                )
                for ranked_skill in selected_ranked
            ],
            nearby_not_selected=[
                item(
                    ranked_skill,
                    raw_ranks[ranked_skill.record.version_id],
                    selected=False,
                )
                for ranked_skill in nearby_ranked
            ],
        )
        return (
            [self._to_runtime(item.record) for item in selected_ranked],
            trace,
        )

    def list_active(self, side: Side | None = None) -> list[RuntimeSkillRecord]:
        return [self._to_runtime(r) for r in self._active_records(side)]

    def freeze(self):
        self._frozen = True

    def select_version(self, version: int) -> Path:
        if self._frozen:
            raise RuntimeError("SkillBank is frozen.")
        return self.repository.select_version(version)


class RuntimeSkillQueryBuilder:
    """Explicit query contract for the two Runtime agents."""

    def build_query(self, question: str, side: str) -> str:
        return question.strip()

    def for_access(self, question: str) -> str:
        """Access Skills are triggered by the exact QA question."""
        return question.strip()

    def for_access_recovery(self, context: dict) -> str:
        """Route only after the first default search is observable."""
        question = str(context.get("question", "")).strip()
        observation = context.get("first_search", {})
        hits = observation.get("hits", []) if isinstance(observation, dict) else []
        lines = [
            f"Question: {question}",
            f"First retrieval returned {len(hits)} memories.",
            "Returned memory contents:",
        ]
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            # Routing needs the evidence state, not storage IDs, scores, or
            # JSON field names.  Removing that metadata keeps the asymmetric
            # query embedding focused on the Skill trigger and answer gap.
            content = " ".join(str(hit.get("content", "")).split())
            if content:
                lines.append(f"- {content[:320]}")
        if len(lines) == 3:
            lines.append("- (none)")
        return "\n".join(lines)

    def for_construction(self, messages: list[dict]) -> str:
        """Construction Skills are triggered by the complete incoming session."""
        return "\n".join(
            f"{item.get('speaker') or item.get('role', 'unknown')}: "
            f"{item.get('content', '')}"
            for item in messages
            if str(item.get("content", "")).strip()
        ).strip()

    def for_construction_segments(
        self,
        messages: list[dict],
        *,
        messages_per_segment: int = 4,
        max_segment_chars: int = 1400,
    ) -> list[str]:
        """Build short overlapping-free segments for max-pooled Skill recall.

        The configured MiniLM encoder truncates long inputs. Segment-level
        encoding ensures that a trigger near the end of a session contributes
        to first-stage retrieval instead of being silently discarded.
        """
        rendered = [
            (
                f"{item.get('speaker') or item.get('role', 'unknown')}: "
                f"{item.get('content', '')}"
            ).strip()
            for item in messages
            if str(item.get("content", "")).strip()
        ]
        segments: list[str] = []
        current: list[str] = []
        current_chars = 0
        for line in rendered:
            would_exceed = (
                current
                and current_chars + len(line) + 1 > max_segment_chars
            )
            if current and (
                len(current) >= max(1, messages_per_segment) or would_exceed
            ):
                segments.append("\n".join(current))
                current, current_chars = [], 0
            current.append(line)
            current_chars += len(line) + 1
        if current:
            segments.append("\n".join(current))
        return segments
