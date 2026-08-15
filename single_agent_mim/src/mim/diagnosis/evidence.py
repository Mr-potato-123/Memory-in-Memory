"""Read-only, permission-specific data preparation for diagnosis."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..failure.provenance import ProvenanceService


class DiagnosisEvidenceRepository:
    """Prepare different views without leaking cross-diagnosis evidence."""

    _mem0_cache: dict[str, "_Mem0Index"] = {}
    _mem0_cache_lock = threading.Lock()

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        mem0_qdrant_path: Path | None = None,
    ):
        self._conn = conn
        self._legacy = ProvenanceService(conn)
        self._mem0 = self._load_mem0_index(mem0_qdrant_path)

    def exact_runtime_search_chain(
        self, access_run_id: str
    ) -> list[dict[str, Any]]:
        """Return exactly what the runtime search/inspect loop recorded."""
        return self._legacy.access_search_chain(access_run_id)

    def access_skill_trace(self, access_run_id: str) -> dict[str, Any]:
        """Return selected official Skills and disclosed near misses."""
        try:
            row = self._conn.execute(
                """SELECT skill_trace_json
                   FROM access_runs
                   WHERE access_run_id=?""",
                (access_run_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return {}
        if row is None:
            return {}
        return self._decode_json_object(row["skill_trace_json"])

    def construction_skill_traces(
        self,
        *,
        conversation_id: str,
        message_ids: list[str],
        snapshot_commit_id: int,
    ) -> list[dict[str, Any]]:
        """Return Construction Skill retrievals for evidence-bearing commits."""
        try:
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                rows = self._conn.execute(
                    f"""SELECT DISTINCT c.commit_id, c.session_id,
                               c.skill_trace_json
                        FROM construction_commits c
                        JOIN construction_inputs i
                          ON i.commit_id=c.commit_id
                        WHERE c.conversation_id=?
                          AND c.commit_id<=?
                          AND i.message_id IN ({placeholders})
                        ORDER BY c.commit_id""",
                    [
                        conversation_id,
                        snapshot_commit_id,
                        *message_ids,
                    ],
                ).fetchall()
            else:
                rows = []
        except sqlite3.OperationalError:
            return []
        traces: list[dict[str, Any]] = []
        for row in rows:
            trace = self._decode_json_object(row["skill_trace_json"])
            if not trace:
                continue
            trace.setdefault("commit_id", row["commit_id"])
            trace.setdefault("session_id", row["session_id"])
            traces.append(trace)
        return traces

    def current_related_memories(
        self,
        *,
        conversation_id: str,
        message_ids: list[str],
        snapshot_commit_id: int,
    ) -> list[dict[str, Any]]:
        """Resolve evidence IDs directly to active rows at the snapshot."""
        if self._mem0 is not None:
            return self._mem0.related_memories(message_ids)
        if not message_ids:
            return []
        placeholders = ",".join("?" for _ in message_ids)
        rows = self._conn.execute(
            f"""SELECT DISTINCT
                       mv.version_id, mv.memory_id, mv.version_no,
                       mv.content, mv.memory_kind, mv.subject, mv.predicate,
                       mv.object_text, mv.world_start, mv.world_end,
                       mv.update_type
                FROM memory_lineage_messages mlm
                JOIN memory_versions mv ON mv.version_id=mlm.version_id
                WHERE mv.conversation_id=?
                  AND mlm.message_id IN ({placeholders})
                  AND mv.system_from_commit<=?
                  AND (
                    mv.system_to_commit IS NULL
                    OR mv.system_to_commit>?
                  )
                ORDER BY mv.memory_id, mv.version_no""",
            [
                conversation_id,
                *message_ids,
                snapshot_commit_id,
                snapshot_commit_id,
            ],
        ).fetchall()
        return [dict(row) for row in rows]

    def current_access_search_chain(
        self,
        *,
        access_run_id: str,
        conversation_id: str,
        snapshot_commit_id: int,
    ) -> list[dict[str, Any]]:
        """Return a sanitized chain containing current memory versions only."""
        exact = self.exact_runtime_search_chain(access_run_id)
        if self._mem0 is not None:
            current_ids = self._mem0.version_ids
            return [
                {
                    "action_id": step.get("action_id"),
                    "step_index": step.get("step_index"),
                    "action_type": step.get("action_type"),
                    "request": step.get("request", {}),
                    "returned_memories": [
                        self._sanitize_memory(item)
                        for item in step.get("returned_memories", [])
                        if str(item.get("version_id", "")) in current_ids
                    ],
                    "returned_version_ids": [
                        str(item["version_id"])
                        for item in step.get("returned_memories", [])
                        if str(item.get("version_id", "")) in current_ids
                    ],
                }
                for step in exact
            ]
        returned_ids = {
            str(version_id)
            for step in exact
            for version_id in step.get("returned_version_ids", [])
            if version_id
        }
        current_ids = self._current_ids(
            conversation_id=conversation_id,
            snapshot_commit_id=snapshot_commit_id,
            candidate_ids=returned_ids,
        )

        sanitized: list[dict[str, Any]] = []
        for step in exact:
            current_memories = [
                self._sanitize_memory(item)
                for item in step.get("returned_memories", [])
                if str(item.get("version_id", "")) in current_ids
            ]
            sanitized.append(
                {
                    "action_id": step.get("action_id"),
                    "step_index": step.get("step_index"),
                    "action_type": step.get("action_type"),
                    "request": step.get("request", {}),
                    "returned_memories": current_memories,
                    "returned_version_ids": [
                        str(item["version_id"])
                        for item in current_memories
                    ],
                }
            )
        return sanitized

    def source_messages(
        self,
        *,
        conversation_id: str,
        message_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not message_ids:
            return []
        placeholders = ",".join("?" for _ in message_ids)
        rows = self._conn.execute(
            f"""SELECT message_id, session_id, turn_index, role, speaker,
                       content, occurred_at
                FROM messages
                WHERE conversation_id=?
                  AND message_id IN ({placeholders})
                ORDER BY session_id, turn_index""",
            [conversation_id, *message_ids],
        ).fetchall()
        return [dict(row) for row in rows]

    def construction_history(
        self,
        *,
        conversation_id: str,
        message_ids: list[str],
        snapshot_commit_id: int,
    ) -> dict[str, Any]:
        if self._mem0 is not None:
            memories = self._mem0.related_memories(message_ids)
            candidates = []
            for memory in memories:
                source_ids = list(memory.get("source_message_ids", []))
                candidates.append(
                    {
                        "message_id": source_ids[0] if source_ids else None,
                        "candidate_id": f"mem0_candidate:{memory['memory_id']}",
                        "commit_id": None,
                        "memory_kind": memory.get("memory_kind", "mem0_fact"),
                        "subject": memory.get("subject", ""),
                        "content": memory.get("content", ""),
                        "world_start": memory.get("world_start"),
                        "world_end": memory.get("world_end"),
                        "decision_id": None,
                        "decision_index": None,
                        "action": "ADD",
                        "target_memory_id": None,
                        "update_type": "ADD",
                        "result_version_id": memory.get("version_id"),
                        "reason": "Mem0 add-only fact",
                        "validation_status": "not_recorded",
                        "validation_errors": None,
                        "relations": [],
                    }
                )
            return {
                "backend": "mem0_add_only",
                "snapshot_commit_id": snapshot_commit_id,
                "processed_commits": [],
                "candidates": candidates,
                "change_events": [],
                "snapshot_memories": memories,
                "note": (
                    "Mem0 exposes final ADD facts and exact source provenance; "
                    "it does not expose the legacy C1/C2 candidate/decision log."
                ),
            }
        return self._legacy.construction_history(
            conversation_id=conversation_id,
            message_ids=message_ids,
            snapshot_commit_id=snapshot_commit_id,
        )

    def _current_ids(
        self,
        *,
        conversation_id: str,
        snapshot_commit_id: int,
        candidate_ids: set[str],
    ) -> set[str]:
        if not candidate_ids:
            return set()
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = self._conn.execute(
            f"""SELECT version_id
                FROM memory_versions
                WHERE conversation_id=?
                  AND version_id IN ({placeholders})
                  AND system_from_commit<=?
                  AND (
                    system_to_commit IS NULL
                    OR system_to_commit>?
                  )""",
            [
                conversation_id,
                *sorted(candidate_ids),
                snapshot_commit_id,
                snapshot_commit_id,
            ],
        ).fetchall()
        return {str(row["version_id"]) for row in rows}

    @classmethod
    def _load_mem0_index(cls, path: Path | None) -> "_Mem0Index | None":
        if path is None or not path.exists():
            return None
        key = str(path.resolve())
        with cls._mem0_cache_lock:
            cached = cls._mem0_cache.get(key)
            if cached is None:
                cached = _Mem0Index.load(path)
                cls._mem0_cache[key] = cached
            return cached

    @staticmethod
    def _sanitize_memory(item: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "version_id",
            "memory_id",
            "version_no",
            "content",
            "rendered_text",
            "memory_kind",
            "subject",
            "predicate",
            "object_text",
            "world_start",
            "world_end",
            "score",
            "paths",
            "source_message_ids",
            "mim_source_message_ids",
        }
        return {key: value for key, value in item.items() if key in allowed}

    @staticmethod
    def _decode_json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            decoded = json.loads(str(value))
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}


class _Mem0Index:
    """Read-only index over the local Mem0 Qdrant payloads."""

    def __init__(self, memories: list[dict[str, Any]]):
        self._memories = memories
        self._by_message: dict[str, list[dict[str, Any]]] = {}
        self._by_version = {
            str(item["version_id"]): item for item in memories
        }
        for item in memories:
            for message_id in item.get("source_message_ids", []):
                self._by_message.setdefault(str(message_id), []).append(item)

    @classmethod
    def load(cls, path: Path) -> "_Mem0Index":
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:  # pragma: no cover - Mem0-only runtime
            raise RuntimeError("qdrant-client is required for Mem0 diagnosis") from exc
        client = QdrantClient(path=str(path))
        memories: list[dict[str, Any]] = []
        offset = None
        try:
            while True:
                points, offset = client.scroll(
                    collection_name="mim_mem0_v3",
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for point in points:
                    payload = dict(point.payload or {})
                    memory_id = str(point.id)
                    content = str(payload.get("data", ""))
                    source_ids = [
                        str(value)
                        for value in payload.get("mim_source_message_ids", [])
                        if str(value).strip()
                    ]
                    memories.append(
                        {
                            "version_id": f"mem0:{memory_id}",
                            "memory_id": memory_id,
                            "version_no": 1,
                            "content": content,
                            "rendered_text": f"[mem0:{memory_id}] {content}",
                            "memory_kind": "mem0_fact",
                            "subject": str(payload.get("attributed_to", "")),
                            "predicate": "",
                            "object_text": "",
                            "world_start": payload.get("mim_session_time"),
                            "world_end": None,
                            "update_type": "ADD",
                            "source_message_ids": source_ids,
                            "mim_source_message_ids": source_ids,
                            "mim_session_id": payload.get("mim_session_id"),
                        }
                    )
                if offset is None:
                    break
        finally:
            client.close()
        return cls(memories)

    @property
    def version_ids(self) -> set[str]:
        return set(self._by_version)

    def related_memories(self, message_ids: list[str]) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for message_id in message_ids:
            for item in self._by_message.get(str(message_id), []):
                found[str(item["version_id"])] = dict(item)
        return sorted(
            found.values(),
            key=lambda item: str(item.get("version_id", "")),
        )
