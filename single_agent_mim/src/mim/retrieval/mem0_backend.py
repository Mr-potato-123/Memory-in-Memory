"""Mem0 factual-memory adapter for MiM.

Mem0 owns fact extraction, persistence, and retrieval.  MiM deliberately
normalizes Mem0 results into its existing read-only ``MemoryHit`` contract so
the Skill runtime does not depend on Mem0 internals.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any

from ..storage.sqlite_store import MemoryHit, SearchFilters


class Mem0Backend:
    """Thin OSS Mem0 adapter with an injectable client for tests."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        config: dict[str, Any] | None = None,
        runtime_model_config: Any | None = None,
        storage_dir: str | None = None,
        namespace: str = "",
        threshold: float = 0.0,
        rerank: bool = False,
    ) -> None:
        if client is None:
            try:
                # Mem0's OSS telemetry creates a process-global local Qdrant
                # ``migrations_qdrant`` store.  MiM runs isolated snapshots
                # and often evaluates several copies sequentially or in
                # parallel; telemetry is not part of the experiment and its
                # lock can make otherwise independent runs fail.  Respect an
                # explicit opt-in, but default it off for this adapter.
                os.environ.setdefault("MEM0_TELEMETRY", "false")
                from mem0 import Memory
            except ImportError as exc:  # pragma: no cover - environment path
                raise RuntimeError(
                    "storage.backend=mem0 requires the 'mem0ai' package"
                ) from exc
            resolved_config = deepcopy(config or {})
            llm = resolved_config.get("llm")
            if isinstance(llm, dict) and llm.get("provider") == "deepseek":
                llm_config = llm.setdefault("config", {})
                # ``extra_body`` belongs to MiM's OpenAI-compatible answer
                # client.  Mem0's OSS DeepSeekConfig (including v2.0.x)
                # exposes only typed fields and rejects this provider-agnostic
                # mapping during Memory.from_config().  Mem0 is only used for
                # the factual add/search plane here, so drop it at the adapter
                # boundary while preserving all supported fields below.
                llm_config.pop("extra_body", None)
                if runtime_model_config is not None:
                    llm_config.setdefault("model", runtime_model_config.model)
                    runtime_api_key = runtime_model_config.api_key or os.getenv(
                        runtime_model_config.api_key_env or "RUNTIME_API_KEY"
                    )
                    if runtime_api_key:
                        llm_config.setdefault("api_key", runtime_api_key)
                    llm_config.setdefault(
                        "deepseek_base_url", runtime_model_config.base_url
                    )
            if storage_dir:
                from pathlib import Path

                local_dir = Path(storage_dir)
                local_dir.mkdir(parents=True, exist_ok=True)
                resolved_config.setdefault("history_db_path", str(local_dir / "history.db"))
                vector_store = resolved_config.setdefault("vector_store", {})
                if vector_store.get("provider", "qdrant") == "qdrant":
                    vector_store.setdefault("provider", "qdrant")
                    vector_store.setdefault("config", {})["path"] = str(
                        local_dir / "qdrant"
                    )
            client = Memory.from_config(resolved_config) if resolved_config else Memory()
        self._client = client
        self._namespace = namespace.strip()
        self._threshold = max(0.0, min(float(threshold), 1.0))
        self._rerank = bool(rerank)

    @property
    def client(self) -> Any:
        return self._client

    def add_session(
        self,
        *,
        conversation_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
        session_time: str | None,
        skill_instructions: str | None = None,
    ) -> dict[str, Any]:
        mem0_messages = []
        message_ids = []
        for message in messages:
            item = {
                "role": message.get("role", "user"),
                "content": str(message.get("content", "")),
            }
            speaker = message.get("speaker")
            if speaker:
                item["name"] = str(speaker)
            mem0_messages.append(item)
            if message.get("message_id"):
                source_message_id = str(message["message_id"])
                message_ids.append(source_message_id)
                # Mem0 consumes this internal field while building the
                # extraction prompt. It is never rendered to the answer model.
                item["source_message_id"] = source_message_id

        kwargs: dict[str, Any] = {
            "messages": mem0_messages,
            "user_id": self.scope_id(conversation_id),
            "metadata": {
                "mim_conversation_id": conversation_id,
                "mim_session_id": session_id,
                "mim_session_time": session_time,
                "mim_session_source_message_ids": message_ids,
            },
        }
        if skill_instructions:
            kwargs["prompt"] = skill_instructions
        result = self._client.add(**kwargs)
        return result if isinstance(result, dict) else {"results": result or []}

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
        del snapshot_commit_id, strategy, depth
        filters = filters or SearchFilters(conversation_id=conversation_id)
        query_parts = [query.strip()]
        query_parts.extend(
            value.strip() for value in (query_expansions or []) if value.strip()
        )
        # Exact anchors complement Mem0's own semantic/BM25/entity pipeline.
        anchors = [value.strip() for value in (keywords or []) if value.strip()]
        anchors.extend(value for value in (filters.entities or []) if value)
        if anchors:
            query_parts.append("Exact anchors: " + ", ".join(dict.fromkeys(anchors)))
        if filters.target_time:
            query_parts.append("Target time: " + filters.target_time)
        if filters.target_time_end:
            query_parts.append("Target time end: " + filters.target_time_end)
        effective_query = "\n".join(dict.fromkeys(part for part in query_parts if part))

        kwargs: dict[str, Any] = {
            "query": effective_query,
            "filters": {"user_id": self.scope_id(conversation_id)},
            "top_k": max(1, int(top_k or 10)),
            "threshold": self._threshold,
            "rerank": self._rerank,
        }
        try:
            raw = self._client.search(**kwargs)
        except TypeError:
            # Older/local clients may not expose the request-level rerank flag.
            kwargs.pop("rerank")
            raw = self._client.search(**kwargs)
        rows = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
        return [self._to_hit(row, rank) for rank, row in enumerate(rows, 1)]

    def list_memories(self, conversation_id: str) -> list[MemoryHit]:
        raw = self._get_all(filters={"user_id": self.scope_id(conversation_id)})
        rows = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
        return [self._to_hit(row, rank) for rank, row in enumerate(rows, 1)]

    def has_memories(self, conversation_id: str) -> bool:
        return bool(self.list_memories(conversation_id))

    def has_session(self, conversation_id: str, session_id: str) -> bool:
        # OSS Mem0's local get_all path requires a top-level scope key and
        # does not consistently accept platform-style AND filters.
        raw = self._get_all(filters={"user_id": self.scope_id(conversation_id)})
        rows = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
        return any(
            isinstance(row, dict)
            and isinstance(row.get("metadata"), dict)
            and row["metadata"].get("mim_session_id") == session_id
            for row in rows
        )

    def _get_all(self, *, filters: dict[str, Any]) -> Any:
        try:
            return self._client.get_all(filters=filters, top_k=10000)
        except TypeError:
            return self._client.get_all(filters=filters)

    def scope_id(self, conversation_id: str) -> str:
        return (
            f"{self._namespace}:{conversation_id}"
            if self._namespace
            else conversation_id
        )

    @staticmethod
    def _to_hit(row: Any, rank: int) -> MemoryHit:
        if not isinstance(row, dict):
            row = vars(row)
        memory_id = str(row.get("id") or row.get("memory_id") or f"rank-{rank}")
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        source_ids = metadata.get("mim_source_message_ids", [])
        if isinstance(source_ids, str):
            try:
                source_ids = json.loads(source_ids)
            except json.JSONDecodeError:
                source_ids = [source_ids]
        categories = row.get("categories") or metadata.get("categories") or []
        if isinstance(categories, str):
            categories = [categories]
        return MemoryHit(
            rank=rank,
            version_id=f"mem0:{memory_id}",
            memory_id=memory_id,
            content=str(row.get("memory") or row.get("data") or ""),
            memory_kind=str(metadata.get("memory_kind") or "fact"),
            subject=str(metadata.get("subject") or ""),
            predicate=metadata.get("predicate"),
            object_text=metadata.get("object_text"),
            world_start=(
                metadata.get("world_start")
                or metadata.get("mim_session_time")
                or row.get("created_at")
            ),
            world_end=metadata.get("world_end"),
            entities=[str(value) for value in categories],
            source_message_ids=[str(value) for value in source_ids],
            score=float(row.get("score") or 0.0),
            matched_paths=["mem0_v3"],
            confidence=float(row.get("score") or 0.5),
        )
