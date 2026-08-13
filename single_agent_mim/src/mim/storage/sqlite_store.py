"""SQLite-backed Memory Store with full versioning, commit history, and FTS5.

Replaces the JSON-file-based JsonMemoryStore with real queries, transactions,
and hybrid retrieval support. Agents never hold a DB connection directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

import numpy as np

from .vector_codec import encode_vector, decode_vector, decode_vectors

# ── Domain types ─────────────────────────────────────────────────

@dataclass
class MemoryCandidate:
    """A candidate memory extracted from a session."""
    candidate_id: str
    memory_kind: str
    subject: str
    predicate: str | None
    object_text: str | None
    content: str
    world_start: str | None
    world_end: str | None
    source_message_ids: list[str]
    entities: list[str]
    keywords: list[str]
    importance: float = 0.5
    confidence: float = 0.5
    embedding: np.ndarray | None = None

    def content_hash(self) -> str:
        return _sha256(self.content)


@dataclass
class ConstructionDecision:
    candidate_id: str
    action: str  # ADD | UPDATE | MERGE | DELETE | SKIP
    target_memory_id: str | None = None
    update_type: str = "add"
    reason: str = ""
    merged_content: str = ""
    world_start: str | None = None
    world_end: str | None = None
    source_message_ids: list[str] = field(default_factory=list)


@dataclass
class ConstructionPlan:
    base_commit_id: int | None
    decisions: list[ConstructionDecision]
    candidates: list[MemoryCandidate] = field(default_factory=list)


@dataclass
class ConstructionCommit:
    commit_id: int
    conversation_id: str
    session_id: str
    base_commit_id: int | None
    status: str


@dataclass
class MemoryHit:
    """A retrieved memory record with metadata."""
    rank: int = 0
    version_id: str = ""
    memory_id: str = ""
    version_no: int = 1
    content: str = ""
    memory_kind: str = "event"
    subject: str = ""
    predicate: str | None = None
    world_start: str | None = None
    world_end: str | None = None
    entities: list[str] = field(default_factory=list)
    source_message_ids: list[str] = field(default_factory=list)
    score: float = 0.0
    matched_paths: list[str] = field(default_factory=list)
    embedding: np.ndarray | None = None
    system_from_commit: int = 0
    system_to_commit: int | None = None
    close_reason: str | None = None
    confidence: float = 0.5


@dataclass
class MemoryInspection:
    memory_id: str
    versions: list[MemoryHit]
    sources: list[dict]  # [{message_id, content, occurred_at}]


@dataclass
class SearchFilters:
    conversation_id: str
    as_of_commit: int | None = None
    memory_kinds: list[str] | None = None
    entities: list[str] | None = None
    time_mode: str = "none"  # none|current|point|before|after|range
    target_time: str | None = None
    target_time_end: str | None = None
    include_history: bool = False


@dataclass
class SearchCall:
    query: str
    strategy: str
    filters: SearchFilters
    hits: list[MemoryHit]


# ── Helpers ────────────────────────────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


# ── Store ──────────────────────────────────────────────────────

class SQLiteMemoryStore:
    """Full SQLite memory store with commit and versioning.

    One instance per run. Each run has its own SQLite file.
    """

    def __init__(self, db_path: str | Path, embedding_dim: int, embedding_model: str):
        self._path = Path(db_path)
        self._embedding_dim = embedding_dim
        self._embedding_model = embedding_model
        os.makedirs(self._path.parent, exist_ok=True)
        self._init_db()
        self._validate_embedding_compatibility()

    # ── Init ────────────────────────────────────────────────

    def _init_db(self):
        """Run schema DDL."""
        schema_path = Path(__file__).parent / "schema.sql"
        with open(schema_path, "r", encoding="utf-8") as f:
            ddl = f.read()
        with self._conn() as conn:
            conn.executescript(ddl)
            self._ensure_column(
                conn,
                "construction_commits",
                "skill_trace_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                conn,
                "access_runs",
                "skill_trace_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    def _validate_embedding_compatibility(self) -> None:
        """Refuse to query a snapshot encoded by a different embedding space."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT embedding_dim, embedding_model
                FROM memory_versions
                WHERE embedding_blob IS NOT NULL
                LIMIT 3
                """
            ).fetchall()
        incompatible = [
            (int(row["embedding_dim"]), str(row["embedding_model"]))
            for row in rows
            if int(row["embedding_dim"]) != self._embedding_dim
            or str(row["embedding_model"]) != self._embedding_model
        ]
        if incompatible:
            found = ", ".join(f"{model} ({dim}d)" for dim, model in incompatible)
            raise ValueError(
                "Embedding space mismatch: this snapshot contains "
                f"{found}, but runtime requests {self._embedding_model} "
                f"({self._embedding_dim}d). Use a fresh database or explicitly "
                "re-embed every stored memory before retrieval."
            )

    @property
    def database_path(self) -> Path:
        return self._path

    def open_read_connection(self) -> sqlite3.Connection:
        """Open a caller-owned read connection for the Failure subsystem."""
        uri = f"{self._path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _conn(self):
        """Get a new connection (WAL, foreign keys on)."""
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Session / Message persistence ───────────────────────

    def ensure_conversation(self, conversation_id: str, split_name: str = ""):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversations (conversation_id, split_name) VALUES (?, ?)",
                (conversation_id, split_name),
            )

    def save_session(self, *, session_id: str, conversation_id: str,
                     session_index: int, occurred_at: str | None = None):
        ch = _sha256(f"{session_id}|{conversation_id}|{session_index}")
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO sessions
                   (session_id, conversation_id, session_index, occurred_at, content_hash)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, conversation_id, session_index, occurred_at, ch),
            )

    def save_messages(self, messages: list[dict]):
        """Save raw messages. Each dict: {message_id, conversation_id, session_id,
        turn_index, role, speaker, content, occurred_at}."""
        with self._conn() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO messages
                   (message_id, conversation_id, session_id, turn_index,
                    role, speaker, content, occurred_at, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        m["message_id"], m["conversation_id"], m["session_id"],
                        m["turn_index"], m["role"], m.get("speaker"),
                        m["content"], m.get("occurred_at"),
                        _sha256(m["content"]),
                    )
                    for m in messages
                ],
            )

    def source_exists(self, message_id: str, conversation_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE message_id=? AND conversation_id=?",
                (message_id, conversation_id),
            ).fetchone()
            return row is not None

    # ── Commit management ───────────────────────────────────

    def latest_commit_id(self, conversation_id: str) -> int | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT MAX(commit_id) FROM construction_commits
                   WHERE conversation_id=? AND status='committed'""",
                (conversation_id,),
            ).fetchone()
            return row[0] if row and row[0] is not None else None

    def committed_session_id(
        self,
        conversation_id: str,
        session_id: str,
    ) -> int | None:
        """Return the committed construction ID for one session, if any."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT commit_id FROM construction_commits
                   WHERE conversation_id=? AND session_id=?
                     AND status='committed'
                   ORDER BY commit_id DESC LIMIT 1""",
                (conversation_id, session_id),
            ).fetchone()
            return int(row[0]) if row else None

    def committed_session_count(self, conversation_id: str) -> int:
        """Count distinct sessions with a committed construction result."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT session_id)
                   FROM construction_commits
                   WHERE conversation_id=? AND status='committed'""",
                (conversation_id,),
            ).fetchone()
            return int(row[0]) if row else 0

    def create_pending_commit(
        self, *, conversation_id: str, session_id: str,
        base_commit_id: int | None, run_id: str,
        runtime_model: str, prompt_hash: str,
        skill_version_ids: list[str], plan_json: str,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO construction_commits
                   (conversation_id, session_id, base_commit_id, run_id,
                    status, runtime_model, prompt_hash, skill_version_ids, plan_json)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (
                    conversation_id, session_id, base_commit_id, run_id,
                    runtime_model, prompt_hash,
                    json.dumps(skill_version_ids), plan_json,
                ),
            )
            return cur.lastrowid

    def mark_commit_status(self, commit_id: int, status: str, error_message: str = ""):
        with self._conn() as conn:
            conn.execute(
                """UPDATE construction_commits
                   SET status=?, completed_at=?, error_message=?
                   WHERE commit_id=?""",
                (status, _now_iso(), error_message, commit_id),
            )

    # ── Memory version operations ───────────────────────────

    def insert_memory_version(
        self,
        memory_id: str,
        version_no: int,
        conversation_id: str,
        memory_kind: str,
        subject: str,
        content: str,
        source_message_ids: list[str],
        entities: list[str],
        keywords: list[str],
        embedding: np.ndarray,
        *,
        predicate: str | None = None,
        object_text: str | None = None,
        world_start: str | None = None,
        world_end: str | None = None,
        system_from_commit: int,
        importance: float = 0.5,
        confidence: float = 0.5,
        parent_version_id: str | None = None,
        update_type: str = "add",
        created_by_skill_ids: list[str] | None = None,
        related_memory_ids: list[str] | None = None,
    ) -> str:
        """Insert a new memory version and its FTS entry. Returns version_id."""
        version_id = f"{memory_id}_v{version_no}"
        content_hash = _sha256(content)

        with self._conn() as conn:
            # Check for duplicate content hash in same conversation
            dup = conn.execute(
                """SELECT version_id FROM memory_versions
                   WHERE conversation_id=? AND content_hash=? AND system_to_commit IS NULL""",
                (conversation_id, content_hash),
            ).fetchone()
            if dup:
                raise ValueError(
                    f"Duplicate content hash {content_hash} already exists as {dup['version_id']}"
                )

            conn.execute(
                """INSERT INTO memory_versions
                   (version_id, memory_id, version_no, conversation_id,
                    memory_kind, subject, predicate, object_text, content,
                    world_start, world_end, recorded_at,
                    system_from_commit,
                    source_message_ids, entities_json, keywords_json,
                    related_memory_ids,
                    importance, confidence,
                    content_hash, embedding_blob, embedding_dim, embedding_model,
                    parent_version_id, update_type, created_by_skill_ids)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    version_id, memory_id, version_no, conversation_id,
                    memory_kind, subject, predicate, object_text, content,
                    world_start, world_end, _now_iso(),
                    system_from_commit,
                    json.dumps(source_message_ids),
                    json.dumps(entities),
                    json.dumps(keywords),
                    json.dumps(related_memory_ids or []),
                    importance, confidence,
                    content_hash,
                    encode_vector(embedding), self._embedding_dim, self._embedding_model,
                    parent_version_id, update_type,
                    json.dumps(created_by_skill_ids or []),
                ),
            )
            # FTS
            conn.execute(
                """INSERT INTO memory_fts (version_id, conversation_id, content, subject, predicate, object_text, keywords)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    version_id, conversation_id, content,
                    subject, predicate or "", object_text or "",
                    " ".join(keywords),
                ),
            )
        return version_id

    def close_memory_version(
        self,
        version_id: str,
        system_to_commit: int,
        close_reason: str,
        world_end: str | None = None,
    ):
        """Close a memory version (superseded/retracted/merged)."""
        with self._conn() as conn:
            conn.execute(
                """UPDATE memory_versions
                   SET system_to_commit=?, close_reason=?, world_end=COALESCE(?, world_end)
                   WHERE version_id=?""",
                (system_to_commit, close_reason, world_end, version_id),
            )

    # ── Snapshot queries ────────────────────────────────────

    def load_snapshot(
        self,
        conversation_id: str,
        as_of_commit: int | None = None,
        include_history: bool = False,
    ) -> list[MemoryHit]:
        """Load all visible memory at a given commit point."""
        with self._conn() as conn:
            if as_of_commit is not None:
                if include_history:
                    # All versions the system knew at commit time
                    rows = conn.execute(
                        """SELECT * FROM memory_versions
                           WHERE conversation_id=?
                             AND system_from_commit <= ?
                           ORDER BY memory_id, version_no""",
                        (conversation_id, as_of_commit),
                    ).fetchall()
                else:
                    # Only currently-active versions at that commit
                    rows = conn.execute(
                        """SELECT * FROM memory_versions
                           WHERE conversation_id=?
                             AND system_from_commit <= ?
                             AND (system_to_commit IS NULL OR system_to_commit > ?)
                           ORDER BY memory_id, version_no""",
                        (conversation_id, as_of_commit, as_of_commit),
                    ).fetchall()
            else:
                if include_history:
                    rows = conn.execute(
                        """SELECT * FROM memory_versions
                           WHERE conversation_id=?
                           ORDER BY memory_id, version_no""",
                        (conversation_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT * FROM memory_versions
                           WHERE conversation_id=?
                             AND system_to_commit IS NULL
                           ORDER BY memory_id, version_no""",
                        (conversation_id,),
                    ).fetchall()
            return [_row_to_hit(r) for r in rows]

    def get_embeddings_for_snapshot(
        self,
        conversation_id: str,
        as_of_commit: int | None = None,
        include_history: bool = False,
    ) -> tuple[list[str], np.ndarray]:
        """Return version IDs and embeddings visible at a snapshot."""
        with self._conn() as conn:
            if as_of_commit is not None:
                if include_history:
                    rows = conn.execute(
                        """SELECT version_id, embedding_blob FROM memory_versions
                           WHERE conversation_id=?
                             AND system_from_commit <= ?""",
                        (conversation_id, as_of_commit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT version_id, embedding_blob FROM memory_versions
                           WHERE conversation_id=?
                             AND system_from_commit <= ?
                             AND (system_to_commit IS NULL OR system_to_commit > ?)""",
                        (conversation_id, as_of_commit, as_of_commit),
                    ).fetchall()
            else:
                if include_history:
                    rows = conn.execute(
                        """SELECT version_id, embedding_blob FROM memory_versions
                           WHERE conversation_id=?""",
                        (conversation_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT version_id, embedding_blob FROM memory_versions
                           WHERE conversation_id=?
                             AND system_to_commit IS NULL""",
                        (conversation_id,),
                    ).fetchall()
        if not rows:
            return [], np.empty((0, self._embedding_dim), dtype=np.float32)
        ids = [r[0] for r in rows]
        blobs = [r[1] for r in rows]
        return ids, decode_vectors(blobs, self._embedding_dim)

    # ── Retrieval helpers ───────────────────────────────────

    def find_related_for_construction(
        self,
        conversation_id: str,
        candidate: MemoryCandidate,
        as_of_commit: int,
        limit: int = 8,
    ) -> list[MemoryHit]:
        """Multi-path lookup for existing memories related to a candidate."""
        hits: dict[str, MemoryHit] = {}

        with self._conn() as conn:
            # 1) Exact: same content hash
            ch = candidate.content_hash()
            exact = conn.execute(
                """SELECT * FROM memory_versions
                   WHERE conversation_id=? AND content_hash=?
                     AND system_from_commit <= ?
                     AND (system_to_commit IS NULL OR system_to_commit > ?)
                   LIMIT ?""",
                (conversation_id, ch, as_of_commit, as_of_commit, limit),
            ).fetchall()
            for r in exact:
                h = _row_to_hit(r)
                h.matched_paths = ["exact"]
                hits[h.version_id] = h

            # 2) Key: same subject + predicate
            if candidate.subject and candidate.predicate:
                key_rows = conn.execute(
                    """SELECT * FROM memory_versions
                       WHERE conversation_id=? AND subject=? AND predicate=?
                         AND system_from_commit <= ?
                         AND (system_to_commit IS NULL OR system_to_commit > ?)
                       LIMIT ?""",
                    (conversation_id, candidate.subject, candidate.predicate,
                     as_of_commit, as_of_commit, limit),
                ).fetchall()
                for r in key_rows:
                    h = _row_to_hit(r)
                    h.matched_paths = h.matched_paths or []
                    if "key" not in h.matched_paths:
                        h.matched_paths.append("key")
                    if h.version_id not in hits:
                        hits[h.version_id] = h

            # 3) Semantic: handled externally (caller provides embedding)
            # 4) Entity-Time: entities overlap
            if candidate.entities:
                for ent in candidate.entities[:5]:
                    et_rows = conn.execute(
                        """SELECT * FROM memory_versions
                           WHERE conversation_id=?
                             AND entities_json LIKE ?
                             AND system_from_commit <= ?
                             AND (system_to_commit IS NULL OR system_to_commit > ?)
                           LIMIT ?""",
                        (conversation_id, f"%{ent}%", as_of_commit, as_of_commit, limit),
                    ).fetchall()
                    for r in et_rows:
                        h = _row_to_hit(r)
                        h.matched_paths = h.matched_paths or []
                        if "entity_time" not in h.matched_paths:
                            h.matched_paths.append("entity_time")
                        if h.version_id not in hits:
                            hits[h.version_id] = h

        # Deduplicate and limit
        result = sorted(hits.values(), key=lambda h: len(h.matched_paths), reverse=True)
        return result[:limit]

    def inspect_memory(
        self,
        *,
        conversation_id: str,
        memory_id: str,
        snapshot_commit_id: int,
        include_versions: bool = True,
        include_sources: bool = True,
    ) -> MemoryInspection:
        """Inspect a memory's version chain and source messages."""
        with self._conn() as conn:
            if include_versions:
                rows = conn.execute(
                    """SELECT * FROM memory_versions
                       WHERE conversation_id=? AND memory_id=?
                         AND system_from_commit <= ?
                       ORDER BY version_no""",
                    (conversation_id, memory_id, snapshot_commit_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM memory_versions
                       WHERE conversation_id=? AND memory_id=?
                         AND system_from_commit <= ?
                         AND (system_to_commit IS NULL OR system_to_commit > ?)
                       ORDER BY version_no""",
                    (conversation_id, memory_id, snapshot_commit_id, snapshot_commit_id),
                ).fetchall()

            versions = [_row_to_hit(r) for r in rows]

            sources: list[dict] = []
            if include_sources and versions:
                all_src_ids = set()
                for v in versions:
                    all_src_ids.update(v.source_message_ids)
                if all_src_ids:
                    placeholders = ",".join("?" * len(all_src_ids))
                    src_rows = conn.execute(
                        f"""SELECT message_id, content, occurred_at FROM messages
                            WHERE conversation_id=? AND message_id IN ({placeholders})""",
                        [conversation_id] + list(all_src_ids),
                    ).fetchall()
                    sources = [dict(r) for r in src_rows]

        return MemoryInspection(
            memory_id=memory_id,
            versions=versions,
            sources=sources,
        )

    # ── FTS5 keyword search ─────────────────────────────────

    def fts_search(
        self,
        conversation_id: str,
        query: str,
        as_of_commit: int | None,
        include_history: bool = False,
        limit: int = 30,
    ) -> list[MemoryHit]:
        """FTS5 keyword search with commit-time filtering."""
        # Escape FTS5 special chars in query
        safe_query = query.replace('"', '""')
        fts_query = f'"{safe_query}"'

        with self._conn() as conn:
            bm25_sql = f"""
                SELECT
                    f.version_id,
                    bm25(memory_fts) AS bm25_score
                FROM memory_fts AS f
                JOIN memory_versions AS m ON m.version_id = f.version_id
                WHERE memory_fts MATCH ?
                  AND m.conversation_id = ?
                  AND m.system_from_commit <= ?
                  AND (? = 1 OR m.system_to_commit IS NULL OR m.system_to_commit > ?)
                ORDER BY bm25_score
                LIMIT ?
            """
            try:
                rows = conn.execute(
                    bm25_sql,
                    (fts_query, conversation_id,
                     as_of_commit or 999999, 1 if include_history else 0,
                     as_of_commit or 999999, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                # FTS may fail on malformed queries
                return []
            if not rows:
                return []
            version_ids = [r[0] for r in rows]
            scores = {r[0]: float(r[1]) for r in rows}
            placeholders = ",".join("?" * len(version_ids))
            memory_rows = conn.execute(
                f"""SELECT * FROM memory_versions
                    WHERE version_id IN ({placeholders})""",
                version_ids,
            ).fetchall()
            by_id = {row["version_id"]: row for row in memory_rows}

        hits = []
        for vid in version_ids:
            row = by_id.get(vid)
            if row is not None:
                hit = _row_to_hit(row)
                hit.score = scores.get(vid, 0.0)
                hit.matched_paths = ["keyword"]
                hits.append(hit)
        return hits

    # ── Apply Construction Plan (transactional) ──────────────

    def apply_construction_plan(
        self,
        conversation_id: str,
        session_id: str,
        base_commit_id: int | None,
        plan: ConstructionPlan,
        run_id: str,
        runtime_model: str,
        prompt_hash: str,
        skill_version_ids: list[str],
        skill_trace: dict[str, Any] | None = None,
        input_message_ids: list[str] | None = None,
    ) -> ConstructionCommit:
        """Atomically apply a validated Construction Plan.

        This runs in a single transaction — any failure rolls back everything.
        """
        plan_json_str = json.dumps({
            "base_commit_id": base_commit_id,
            "decisions": [
                {
                    "candidate_id": d.candidate_id,
                    "action": d.action,
                    "target_memory_id": d.target_memory_id,
                    "update_type": d.update_type,
                    "reason": d.reason,
                    "merged_content": d.merged_content,
                    "world_start": d.world_start,
                    "world_end": d.world_end,
                    "source_message_ids": d.source_message_ids,
                }
                for d in plan.decisions
            ],
        })

        with self._conn() as conn:
            # Double-check base commit hasn't changed
            if base_commit_id is not None:
                latest = conn.execute(
                    """SELECT MAX(commit_id) FROM construction_commits
                       WHERE conversation_id=? AND status='committed'""",
                    (conversation_id,),
                ).fetchone()[0]
                if latest != base_commit_id:
                    raise RuntimeError(
                        f"Base commit changed: expected {base_commit_id}, got {latest}. Rebuild plan."
                    )

            # Create pending commit
            cur = conn.execute(
                """INSERT INTO construction_commits
                   (conversation_id, session_id, base_commit_id, run_id,
                    status, runtime_model, prompt_hash, skill_version_ids,
                    skill_trace_json, plan_json)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
                (
                    conversation_id, session_id, base_commit_id, run_id,
                    runtime_model,
                    prompt_hash,
                    json.dumps(skill_version_ids),
                    json.dumps(skill_trace or {}, ensure_ascii=False),
                    plan_json_str,
                ),
            )
            commit_id = cur.lastrowid

            # Generate new memory_ids
            next_mem_num = 1
            max_row = conn.execute(
                "SELECT MAX(row_id) FROM memory_versions WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            if max_row:
                next_mem_num = max_row + 1

            errors: list[str] = []
            candidate_by_id = {c.candidate_id: c for c in plan.candidates}

            # Record every source message presented to Construction, including
            # messages from which no candidate was extracted.
            self.record_construction_inputs(
                conn,
                commit_id,
                input_message_ids or [
                    mid
                    for cand in plan.candidates
                    for mid in cand.source_message_ids
                ],
            )
            for cand in plan.candidates:
                self.record_candidate(conn, commit_id, cand, conversation_id)

            for decision_index, dec in enumerate(plan.decisions):
                try:
                    cand = candidate_by_id.get(dec.candidate_id)
                    direct_sources = list(dict.fromkeys(
                        dec.source_message_ids
                        or (cand.source_message_ids if cand else [])
                    ))
                    result_version_id = ""
                    old_version_data: dict | None = None
                    parent_version_ids: list[str] = []

                    if dec.action == "ADD":
                        if cand is None:
                            raise ValueError(f"Candidate not found: {dec.candidate_id}")
                        mem_id = f"mem_{conversation_id}_{next_mem_num:04d}"
                        next_mem_num += 1
                        content = dec.merged_content or cand.content
                        emb = cand.embedding
                        if emb is None:
                            emb = np.zeros(self._embedding_dim, dtype=np.float32)
                        result_version_id = _insert_version(
                            conn, mem_id, 1, conversation_id,
                            memory_kind=cand.memory_kind,
                            subject=cand.subject,
                            predicate=cand.predicate,
                            object_text=cand.object_text,
                            content=content,
                            source_message_ids=direct_sources,
                            entities=cand.entities,
                            keywords=cand.keywords,
                            embedding=emb, embedding_dim=self._embedding_dim,
                            embedding_model=self._embedding_model,
                            system_from_commit=commit_id,
                            update_type=dec.update_type,
                            world_start=dec.world_start or cand.world_start,
                            world_end=dec.world_end if dec.world_end is not None else cand.world_end,
                            importance=cand.importance,
                            confidence=cand.confidence,
                            created_by_skill_ids=skill_version_ids,
                        )

                    elif dec.action == "UPDATE":
                        if not dec.target_memory_id:
                            errors.append(f"UPDATE without target_memory_id: {dec.candidate_id}")
                            continue
                        # Get current version
                        current = conn.execute(
                            """SELECT * FROM memory_versions
                               WHERE memory_id=? AND conversation_id=?
                                 AND system_to_commit IS NULL
                               ORDER BY version_no DESC LIMIT 1""",
                            (dec.target_memory_id, conversation_id),
                        ).fetchone()
                        if not current:
                            errors.append(f"Target memory not found: {dec.target_memory_id}")
                            continue
                        old_version_data = dict(current)
                        parent_version_ids = [current["version_id"]]

                        # Close old version
                        conn.execute(
                            """UPDATE memory_versions
                               SET system_to_commit=?, close_reason='superseded'
                               WHERE version_id=?""",
                            (commit_id, current["version_id"]),
                        )

                        # Create new version
                        new_ver = current["version_no"] + 1
                        content = dec.merged_content or (
                            cand.content if cand is not None else current["content"]
                        )
                        emb = (
                            cand.embedding
                            if cand is not None and cand.embedding is not None
                            else decode_vector(current["embedding_blob"], self._embedding_dim)
                        )
                        result_version_id = _insert_version(
                            conn, dec.target_memory_id, new_ver, conversation_id,
                            memory_kind=cand.memory_kind if cand else current["memory_kind"],
                            subject=cand.subject if cand else current["subject"],
                            predicate=cand.predicate if cand else current["predicate"],
                            object_text=cand.object_text if cand else current["object_text"],
                            content=content,
                            source_message_ids=direct_sources,
                            entities=cand.entities if cand else json.loads(current["entities_json"]),
                            keywords=cand.keywords if cand else json.loads(current["keywords_json"]),
                            embedding=emb, embedding_dim=self._embedding_dim,
                            embedding_model=self._embedding_model,
                            system_from_commit=commit_id,
                            parent_version_id=current["version_id"],
                            update_type=dec.update_type,
                            world_start=dec.world_start or current["world_start"],
                            world_end=dec.world_end,
                            importance=cand.importance if cand else current["importance"],
                            confidence=cand.confidence if cand else current["confidence"],
                            created_by_skill_ids=skill_version_ids,
                        )

                    elif dec.action == "MERGE":
                        if not dec.target_memory_id:
                            errors.append(f"MERGE without target_memory_id: {dec.candidate_id}")
                            continue
                        current = conn.execute(
                            """SELECT * FROM memory_versions
                               WHERE memory_id=? AND conversation_id=?
                                 AND system_to_commit IS NULL
                               ORDER BY version_no DESC LIMIT 1""",
                            (dec.target_memory_id, conversation_id),
                        ).fetchone()
                        if not current:
                            errors.append(f"Target memory not found: {dec.target_memory_id}")
                            continue
                        old_version_data = dict(current)
                        parent_version_ids = [current["version_id"]]
                        # Close old, create merged
                        conn.execute(
                            """UPDATE memory_versions
                               SET system_to_commit=?, close_reason='merged'
                               WHERE version_id=?""",
                            (commit_id, current["version_id"]),
                        )
                        new_ver = current["version_no"] + 1
                        content = dec.merged_content or (
                            cand.content if cand is not None else current["content"]
                        )
                        emb = (
                            cand.embedding
                            if cand is not None and cand.embedding is not None
                            else decode_vector(current["embedding_blob"], self._embedding_dim)
                        )
                        result_version_id = _insert_version(
                            conn, dec.target_memory_id, new_ver, conversation_id,
                            memory_kind=current["memory_kind"],
                            subject=current["subject"],
                            content=content,
                            source_message_ids=direct_sources,
                            entities=(
                                list(dict.fromkeys(json.loads(current["entities_json"]) + cand.entities))
                                if cand else json.loads(current["entities_json"])
                            ),
                            keywords=(
                                list(dict.fromkeys(json.loads(current["keywords_json"]) + cand.keywords))
                                if cand else json.loads(current["keywords_json"])
                            ),
                            embedding=emb, embedding_dim=self._embedding_dim,
                            embedding_model=self._embedding_model,
                            system_from_commit=commit_id,
                            parent_version_id=current["version_id"],
                            update_type="merge",
                            world_start=dec.world_start or current["world_start"],
                            world_end=dec.world_end,
                            predicate=current["predicate"],
                            object_text=cand.object_text if cand else current["object_text"],
                            importance=max(
                                float(current["importance"]),
                                cand.importance if cand else 0.5,
                            ),
                            confidence=max(
                                float(current["confidence"]),
                                cand.confidence if cand else 0.5,
                            ),
                            created_by_skill_ids=skill_version_ids,
                        )

                    elif dec.action == "DELETE":
                        if not dec.target_memory_id:
                            errors.append(
                                f"DELETE without target_memory_id: {dec.candidate_id}"
                            )
                            continue
                        current = conn.execute(
                            """SELECT * FROM memory_versions
                               WHERE memory_id=? AND conversation_id=?
                                 AND system_to_commit IS NULL
                               ORDER BY version_no DESC LIMIT 1""",
                            (dec.target_memory_id, conversation_id),
                        ).fetchone()
                        if not current:
                            errors.append(
                                f"Target memory not found: {dec.target_memory_id}"
                            )
                            continue
                        old_version_data = dict(current)
                        parent_version_ids = [current["version_id"]]
                        conn.execute(
                            """UPDATE memory_versions
                               SET system_to_commit=?, close_reason='retracted',
                                   world_end=COALESCE(?, world_end)
                               WHERE version_id=?""",
                            (commit_id, dec.world_end, current["version_id"]),
                        )

                    elif dec.action == "SKIP":
                        pass

                    decision_id = self.record_decision(
                        conn,
                        dec,
                        commit_id,
                        decision_index,
                        result_version_id=result_version_id,
                    )

                    if result_version_id:
                        self.record_version_message_edges(
                            conn, result_version_id, direct_sources,
                        )
                        for parent_id in parent_version_ids:
                            self.record_parent_edge(
                                conn,
                                child_version_id=result_version_id,
                                parent_version_id=parent_id,
                                relation=dec.update_type or dec.action.lower(),
                            )
                        self.build_lineage_closure(
                            conn,
                            child_version_id=result_version_id,
                            direct_message_ids=set(direct_sources),
                            parent_version_ids=parent_version_ids,
                        )
                        new_row = conn.execute(
                            "SELECT * FROM memory_versions WHERE version_id=?",
                            (result_version_id,),
                        ).fetchone()
                        inherited_sources = {
                            row["message_id"]
                            for row in conn.execute(
                                """SELECT message_id FROM memory_lineage_messages
                                   WHERE version_id=?""",
                                (result_version_id,),
                            ).fetchall()
                        }
                        self.record_change_event(
                            conn,
                            change_id=f"change_{commit_id}_{decision_index:03d}",
                            commit_id=commit_id,
                            decision_id=decision_id,
                            operation=dec.action,
                            new_version_id=result_version_id,
                            old_version_data=old_version_data,
                            new_version_data=dict(new_row) if new_row else {},
                            direct_message_ids=direct_sources,
                            affected_message_ids=sorted(inherited_sources),
                        )
                    elif dec.action == "DELETE" and old_version_data is not None:
                        inherited_sources = {
                            row["message_id"]
                            for row in conn.execute(
                                """SELECT message_id FROM memory_lineage_messages
                                   WHERE version_id=?""",
                                (old_version_data["version_id"],),
                            ).fetchall()
                        }
                        self.record_change_event(
                            conn,
                            change_id=f"change_{commit_id}_{decision_index:03d}",
                            commit_id=commit_id,
                            decision_id=decision_id,
                            operation="DELETE",
                            new_version_id=None,
                            old_version_data=old_version_data,
                            new_version_data={},
                            direct_message_ids=direct_sources,
                            affected_message_ids=sorted(
                                inherited_sources | set(direct_sources)
                            ),
                        )

                except Exception as exc:
                    errors.append(f"{dec.action} failed for {dec.candidate_id}: {exc}")

            if errors:
                conn.execute(
                    "UPDATE construction_commits SET status='failed', error_message=?, completed_at=? WHERE commit_id=?",
                    ("; ".join(errors), _now_iso(), commit_id),
                )
                raise RuntimeError(f"Construction plan failed: {'; '.join(errors)}")

            conn.execute(
                "UPDATE construction_commits SET status='committed', completed_at=? WHERE commit_id=?",
                (_now_iso(), commit_id),
            )

        return ConstructionCommit(
            commit_id=commit_id,
            conversation_id=conversation_id,
            session_id=session_id,
            base_commit_id=base_commit_id,
            status="committed",
        )

    # ── Provenance: Candidates ───────────────────────────────

    def record_candidate(
        self, conn: sqlite3.Connection, commit_id: int,
        candidate: MemoryCandidate, conversation_id: str,
    ) -> str:
        """Record a Candidate and its message edges. Returns candidate_id."""
        cid = candidate.candidate_id
        conn.execute(
            """INSERT OR REPLACE INTO memory_candidates
               (candidate_id, commit_id, conversation_id, memory_kind,
                subject, predicate, object_text, content,
                world_start, world_end, entities_json, keywords_json,
                importance, confidence, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cid, commit_id, conversation_id, candidate.memory_kind,
             candidate.subject, candidate.predicate, candidate.object_text,
             candidate.content, candidate.world_start, candidate.world_end,
             json.dumps(candidate.entities), json.dumps(candidate.keywords),
             candidate.importance, candidate.confidence, candidate.content_hash()),
        )
        # Message edges
        for msg_id in candidate.source_message_ids:
            conn.execute(
                """INSERT OR IGNORE INTO candidate_message_edges
                   (candidate_id, message_id, relation) VALUES (?, ?, 'direct_support')""",
                (cid, msg_id),
            )
        return cid

    # ── Provenance: Decisions ────────────────────────────────

    def record_decision(
        self, conn: sqlite3.Connection,
        dec: ConstructionDecision, commit_id: int,
        decision_index: int, result_version_id: str = "",
    ) -> str:
        """Record a Construction Decision. Returns decision_id."""
        did = f"decision_{commit_id}_{decision_index:03d}"
        conn.execute(
            """INSERT OR REPLACE INTO construction_decisions
               (decision_id, commit_id, candidate_id, decision_index,
                action, target_memory_id, update_type, result_version_id,
                reason, validation_status, validation_errors)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', '[]')""",
            (did, commit_id, dec.candidate_id, decision_index,
             dec.action, dec.target_memory_id, dec.update_type,
             result_version_id, dec.reason),
        )
        return did

    # ── Provenance: Construction Inputs ──────────────────────

    def record_construction_inputs(
        self, conn: sqlite3.Connection, commit_id: int,
        message_ids: list[str],
    ):
        """Record which messages were processed in this commit."""
        for mid in message_ids:
            conn.execute(
                "INSERT OR IGNORE INTO construction_inputs (commit_id, message_id) VALUES (?, ?)",
                (commit_id, mid),
            )

    # ── Provenance: Memory Version ↔ Message Edges ──────────

    def record_version_message_edges(
        self, conn: sqlite3.Connection, version_id: str,
        message_ids: list[str],
    ):
        """Record direct source edges between a version and its source messages."""
        for mid in message_ids:
            conn.execute(
                """INSERT OR IGNORE INTO memory_version_message_edges
                   (version_id, message_id, relation) VALUES (?, ?, 'direct_support')""",
                (version_id, mid),
            )

    # ── Provenance: Parent Edges ────────────────────────────

    def record_parent_edge(
        self, conn: sqlite3.Connection, child_version_id: str,
        parent_version_id: str, relation: str,
    ):
        """Record a parent-child relationship between versions."""
        conn.execute(
            """INSERT OR IGNORE INTO memory_version_parent_edges
               (child_version_id, parent_version_id, relation) VALUES (?, ?, ?)""",
            (child_version_id, parent_version_id, relation),
        )

    # ── Provenance: Lineage Closure ──────────────────────────

    def build_lineage_closure(
        self, conn: sqlite3.Connection, child_version_id: str,
        direct_message_ids: set[str], parent_version_ids: list[str],
    ):
        """Build materialized lineage closure for a new child version.

        Algorithm:
          1. Direct sources: origin=child, depth=0
          2. For each parent: copy parent's lineage rows with depth+1
          3. Keep minimum depth per (version, message, origin) key
        """
        edges: dict[tuple[str, str, str], int] = {}

        # Direct sources
        for mid in direct_message_ids:
            key = (child_version_id, mid, child_version_id)
            edges[key] = 0

        # Inherit from parents
        for parent_id in parent_version_ids:
            parent_rows = conn.execute(
                """SELECT version_id, message_id, origin_version_id, min_depth
                   FROM memory_lineage_messages WHERE version_id=?""",
                (parent_id,),
            ).fetchall()
            for row in parent_rows:
                key = (child_version_id, row["message_id"], row["origin_version_id"])
                depth = row["min_depth"] + 1
                edges[key] = min(edges.get(key, depth), depth)

        # Insert
        for (vid, mid, origin), depth in edges.items():
            conn.execute(
                """INSERT OR REPLACE INTO memory_lineage_messages
                   (version_id, message_id, origin_version_id, min_depth)
                   VALUES (?, ?, ?, ?)""",
                (vid, mid, origin, depth),
            )

    # ── Provenance: Change Events ────────────────────────────

    def record_change_event(
        self, conn: sqlite3.Connection, *,
        change_id: str, commit_id: int, decision_id: str,
        operation: str, new_version_id: str | None,
        old_version_data: dict | None,
        new_version_data: dict,
        direct_message_ids: list[str],
        affected_message_ids: list[str],
    ):
        """Record a change event with field-level diff."""
        diff = {}
        if old_version_data:
            for field in ["memory_kind", "subject", "predicate", "object_text",
                          "content", "world_start", "world_end", "entities", "keywords"]:
                old_val = old_version_data.get(field)
                new_val = new_version_data.get(field)
                if str(old_val) != str(new_val):
                    diff[field] = {"before": old_val, "after": new_val}

        conn.execute(
            """INSERT OR REPLACE INTO memory_change_events
               (change_id, commit_id, decision_id, operation, new_version_id,
                changed_fields_json, direct_message_ids, affected_message_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (change_id, commit_id, decision_id, operation, new_version_id,
             json.dumps(diff), json.dumps(direct_message_ids),
             json.dumps(affected_message_ids)),
        )

    # ── Access Trace Recording ───────────────────────────────

    def record_access_run(
        self, conn: sqlite3.Connection, *,
        access_run_id: str, run_id: str, conversation_id: str,
        qa_id: str, snapshot_commit_id: int, question: str,
        prediction: str = "", skill_version_ids: list[str] | None = None,
        skill_trace: dict[str, Any] | None = None,
        answer_prompt_hash: str = "",
    ):
        conn.execute(
            """INSERT OR REPLACE INTO access_runs
               (access_run_id, run_id, conversation_id, qa_id, snapshot_commit_id,
                question, prediction, skill_version_ids, skill_trace_json,
                answer_prompt_hash, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed')""",
            (access_run_id, run_id, conversation_id, qa_id, snapshot_commit_id,
             question, prediction, json.dumps(skill_version_ids or []),
             json.dumps(skill_trace or {}, ensure_ascii=False),
             answer_prompt_hash),
        )

    def record_retrieval_hits(
        self, conn: sqlite3.Connection, action_id: str,
        hits: list[MemoryHit],
    ):
        for i, h in enumerate(hits):
            conn.execute(
                """INSERT OR REPLACE INTO access_retrieval_hits
                   (action_id, version_id, raw_rank, final_rank, fused_score,
                    returned_to_agent, kept_in_workspace)
                   VALUES (?, ?, ?, ?, ?, 1, 1)""",
                (action_id, h.version_id, i + 1, i + 1, h.score),
            )

    def record_answer_context(
        self, conn: sqlite3.Connection, access_run_id: str,
        memories: list[dict],  # [{version_id, rendered_text, context_index, token_count}]
    ):
        for m in memories:
            conn.execute(
                """INSERT OR REPLACE INTO access_answer_context
                   (access_run_id, version_id, context_index, rendered_text, token_count)
                   VALUES (?, ?, ?, ?, ?)""",
                (access_run_id, m["version_id"], m["context_index"],
                 m["rendered_text"], m.get("token_count")),
            )

    def record_final_evidence(
        self, conn: sqlite3.Connection, access_run_id: str,
        evidence_ids: list[str],
    ):
        for i, vid in enumerate(evidence_ids):
            # Provider output is validated against the in-memory search chain,
            # but a copied/resumed evaluation database can still lack an old
            # inspected version. Final-evidence persistence is diagnostic
            # metadata and must never abort an otherwise valid QA result.
            if conn.execute(
                "SELECT 1 FROM memory_versions WHERE version_id=?",
                (vid,),
            ).fetchone() is None:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO access_final_evidence
                   (access_run_id, version_id, evidence_index) VALUES (?, ?, ?)""",
                (access_run_id, vid, i),
            )

    # ── QA Cases ─────────────────────────────────────────────

    def record_qa_case(
        self, conn: sqlite3.Connection, qa_id: str,
        conversation_id: str, question: str, reference_answer: str,
        category: int | None = None, gold_message_ids: list[str] | None = None,
    ):
        conn.execute(
            """INSERT OR REPLACE INTO qa_cases
               (qa_id, conversation_id, category, question, reference_answer)
               VALUES (?, ?, ?, ?, ?)""",
            (qa_id, conversation_id, category, question, reference_answer),
        )
        if gold_message_ids:
            for mid in gold_message_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO qa_gold_sources (qa_id, message_id) VALUES (?, ?)",
                    (qa_id, mid),
                )

    def save_qa_case(
        self,
        *,
        qa_id: str,
        conversation_id: str,
        question: str,
        reference_answer: str,
        category: int | None = None,
        gold_message_ids: list[str] | None = None,
    ):
        with self._conn() as conn:
            self.record_qa_case(
                conn,
                qa_id,
                conversation_id,
                question,
                reference_answer,
                category,
                gold_message_ids,
            )

    def save_access_trace(
        self,
        *,
        access_run_id: str,
        run_id: str,
        conversation_id: str,
        qa_id: str,
        snapshot_commit_id: int,
        question: str,
        prediction: str,
        skill_version_ids: list[str],
        skill_trace: dict[str, Any] | None = None,
        answer_prompt_hash: str,
        action_records: list[dict],
        visible_memories: list[dict],
        evidence_ids: list[str],
    ):
        """Atomically persist the complete Access trace used by Failure."""
        with self._conn() as conn:
            self.record_access_run(
                conn,
                access_run_id=access_run_id,
                run_id=run_id,
                conversation_id=conversation_id,
                qa_id=qa_id,
                snapshot_commit_id=snapshot_commit_id,
                question=question,
                prediction=prediction,
                skill_version_ids=skill_version_ids,
                skill_trace=skill_trace,
                answer_prompt_hash=answer_prompt_hash,
            )
            for record in action_records:
                conn.execute(
                    """INSERT OR REPLACE INTO access_actions
                       (action_id, access_run_id, step_index, action_type,
                        request_json, response_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        record["action_id"],
                        access_run_id,
                        int(record["step_index"]),
                        record["action_type"],
                        json.dumps(record.get("request", {}), ensure_ascii=False),
                        json.dumps(record.get("response", {}), ensure_ascii=False),
                    ),
                )
                for rank, hit in enumerate(record.get("retrieval_hits", []), 1):
                    version_id = hit.get("version_id")
                    if not version_id:
                        continue
                    conn.execute(
                        """INSERT OR REPLACE INTO access_retrieval_hits
                           (action_id, version_id, raw_rank, final_rank,
                            fused_score, returned_to_agent, kept_in_workspace)
                           VALUES (?, ?, ?, ?, ?, 1, ?)""",
                        (
                            record["action_id"],
                            version_id,
                            rank,
                            rank,
                            hit.get("score"),
                            int(any(
                                m.get("version_id") == version_id
                                for m in visible_memories
                            )),
                        ),
                    )

            self.record_answer_context(
                conn,
                access_run_id,
                [
                    {
                        "version_id": m["version_id"],
                        "context_index": m.get("context_index", i),
                        "rendered_text": m.get("rendered_text", m.get("content", "")),
                        "token_count": m.get("token_count"),
                    }
                    for i, m in enumerate(visible_memories)
                ],
            )
            self.record_final_evidence(conn, access_run_id, evidence_ids)

    def get_source_messages(
        self,
        conversation_id: str,
        message_ids: list[str],
    ) -> list[dict]:
        if not message_ids:
            return []
        placeholders = ",".join("?" for _ in message_ids)
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT message_id, role, content, occurred_at, session_id, turn_index
                    FROM messages
                    WHERE conversation_id=? AND message_id IN ({placeholders})
                    ORDER BY session_id, turn_index""",
                [conversation_id, *message_ids],
            ).fetchall()
        return [dict(row) for row in rows]

    def get_answer_context(self, access_run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT c.version_id, c.context_index, c.rendered_text,
                          v.content, v.memory_kind, v.subject,
                          v.world_start, v.world_end
                   FROM access_answer_context c
                   JOIN memory_versions v ON v.version_id=c.version_id
                   WHERE c.access_run_id=?
                   ORDER BY c.context_index""",
                (access_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]


# ── Internal helpers ─────────────────────────────────────────

def _insert_version(
    conn: sqlite3.Connection,
    memory_id: str, version_no: int, conversation_id: str,
    memory_kind: str, subject: str, content: str,
    source_message_ids: list[str],
    entities: list[str], keywords: list[str],
    embedding: np.ndarray, embedding_dim: int, embedding_model: str,
    system_from_commit: int,
    update_type: str = "add",
    parent_version_id: str | None = None,
    world_start: str | None = None,
    world_end: str | None = None,
    predicate: str | None = None,
    object_text: str | None = None,
    importance: float = 0.5,
    confidence: float = 0.5,
    created_by_skill_ids: list[str] | None = None,
    related_memory_ids: list[str] | None = None,
) -> str:
    version_id = f"{memory_id}_v{version_no}"
    content_hash = _sha256(content)
    conn.execute(
        """INSERT INTO memory_versions
           (version_id, memory_id, version_no, conversation_id,
            memory_kind, subject, predicate, object_text, content,
            world_start, world_end, recorded_at,
            system_from_commit,
            source_message_ids, entities_json, keywords_json,
            related_memory_ids,
            importance, confidence,
            content_hash, embedding_blob, embedding_dim, embedding_model,
            parent_version_id, update_type, created_by_skill_ids)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            version_id, memory_id, version_no, conversation_id,
            memory_kind, subject, predicate, object_text, content,
            world_start, world_end, _now_iso(),
            system_from_commit,
            json.dumps(source_message_ids), json.dumps(entities), json.dumps(keywords),
            json.dumps(related_memory_ids or []),
            importance, confidence,
            content_hash, encode_vector(embedding), embedding_dim, embedding_model,
            parent_version_id, update_type, json.dumps(created_by_skill_ids or []),
        ),
    )
    conn.execute(
        """INSERT INTO memory_fts (version_id, conversation_id, content, subject, predicate, object_text, keywords)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (version_id, conversation_id, content, subject, predicate or "", object_text or "", " ".join(keywords)),
    )
    return version_id


def _row_to_hit(row: sqlite3.Row | dict) -> MemoryHit:
    """Convert a DB row to a MemoryHit."""
    d = dict(row) if hasattr(row, "keys") else row
    return MemoryHit(
        version_id=d.get("version_id", ""),
        memory_id=d.get("memory_id", ""),
        version_no=d.get("version_no", 1),
        content=d.get("content", ""),
        memory_kind=d.get("memory_kind", "event"),
        subject=d.get("subject", ""),
        predicate=d.get("predicate"),
        world_start=d.get("world_start"),
        world_end=d.get("world_end"),
        entities=_parse_json_array(d.get("entities_json", "[]")),
        source_message_ids=_parse_json_array(d.get("source_message_ids", "[]")),
        system_from_commit=d.get("system_from_commit", 0),
        system_to_commit=d.get("system_to_commit"),
        close_reason=d.get("close_reason"),
        confidence=d.get("confidence", 0.5),
    )


def _parse_json_array(val: str) -> list[str]:
    try:
        return json.loads(val) if val else []
    except (json.JSONDecodeError, TypeError):
        return []
