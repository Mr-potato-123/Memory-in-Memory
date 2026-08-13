"""SQLite Memory Store tests for persistence, versioning and provenance."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mim.failure.provenance import ProvenanceService
from mim.storage.sqlite_store import (
    ConstructionDecision,
    ConstructionPlan,
    MemoryCandidate,
    SQLiteMemoryStore,
)


def _store(tmp_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(
        tmp_path / "memory.sqlite3",
        embedding_dim=32,
        embedding_model="test-hash",
    )


def test_invalid_final_evidence_does_not_abort_trace_persistence(
    tmp_path: Path,
):
    store = _store(tmp_path)

    store.save_access_trace(
        access_run_id="access_invalid_evidence",
        run_id="run_test",
        conversation_id="conv_test",
        qa_id="qa_test",
        snapshot_commit_id=0,
        question="Where?",
        prediction="Unknown",
        skill_version_ids=[],
        answer_prompt_hash="hash",
        action_records=[],
        visible_memories=[],
        evidence_ids=["missing_version_v1"],
    )

    with store._conn() as conn:
        assert conn.execute(
            "SELECT 1 FROM access_runs WHERE access_run_id=?",
            ("access_invalid_evidence",),
        ).fetchone() is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM access_final_evidence WHERE access_run_id=?",
            ("access_invalid_evidence",),
        ).fetchone()[0] == 0


def _save_input(
    store: SQLiteMemoryStore,
    conversation_id: str,
    session_id: str,
    message_id: str,
    content: str,
    session_index: int,
) -> None:
    store.ensure_conversation(conversation_id)
    store.save_session(
        session_id=session_id,
        conversation_id=conversation_id,
        session_index=session_index,
    )
    store.save_messages([{
        "message_id": message_id,
        "conversation_id": conversation_id,
        "session_id": session_id,
        "turn_index": 0,
        "role": "user",
        "speaker": "Alice",
        "content": content,
        "occurred_at": "2024-01-01",
    }])


def _candidate(
    candidate_id: str,
    message_id: str,
    content: str,
    object_text: str,
) -> MemoryCandidate:
    return MemoryCandidate(
        candidate_id=candidate_id,
        memory_kind="state",
        subject="Alice",
        predicate="residence",
        object_text=object_text,
        content=content,
        world_start="2024-01-01",
        world_end=None,
        source_message_ids=[message_id],
        entities=["Alice", object_text],
        keywords=["Alice", object_text, "residence"],
        importance=0.8,
        confidence=0.95,
        embedding=np.ones(32, dtype=np.float32),
    )


def test_add_persists_complete_construction_trace(tmp_path: Path):
    store = _store(tmp_path)
    _save_input(store, "conv_a", "s1", "conv_a:D1:1", "I live in Boston.", 0)
    candidate = _candidate(
        "cand_conv_a_s1_000", "conv_a:D1:1", "Alice lives in Boston.", "Boston"
    )
    plan = ConstructionPlan(
        base_commit_id=None,
        candidates=[candidate],
        decisions=[ConstructionDecision(
            candidate_id=candidate.candidate_id,
            action="ADD",
            update_type="add",
            merged_content=candidate.content,
            source_message_ids=candidate.source_message_ids,
            reason="New durable state.",
        )],
    )

    commit = store.apply_construction_plan(
        conversation_id="conv_a",
        session_id="s1",
        base_commit_id=None,
        plan=plan,
        run_id="run_test",
        runtime_model="mock",
        prompt_hash="prompt",
        skill_version_ids=["sk_c_v1"],
        input_message_ids=["conv_a:D1:1"],
    )

    memories = store.load_snapshot("conv_a", commit.commit_id)
    assert len(memories) == 1
    assert memories[0].content == "Alice lives in Boston."
    conn = store.open_read_connection()
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "construction_inputs",
                "memory_candidates",
                "candidate_message_edges",
                "construction_decisions",
                "memory_versions",
                "memory_change_events",
                "memory_lineage_messages",
            )
        }
    finally:
        conn.close()
    assert all(value == 1 for value in counts.values())


def test_store_rejects_snapshot_from_a_different_embedding_space(tmp_path: Path):
    path = tmp_path / "memory.sqlite3"
    store = SQLiteMemoryStore(path, embedding_dim=32, embedding_model="old-model")
    _save_input(store, "conv_a", "s1", "conv_a:D1:1", "I live in Boston.", 0)
    candidate = _candidate(
        "cand_conv_a_s1_000", "conv_a:D1:1", "Alice lives in Boston.", "Boston"
    )
    store.apply_construction_plan(
        conversation_id="conv_a",
        session_id="s1",
        base_commit_id=None,
        plan=ConstructionPlan(
            base_commit_id=None,
            candidates=[candidate],
            decisions=[ConstructionDecision(
                candidate_id=candidate.candidate_id,
                action="ADD",
                merged_content=candidate.content,
                source_message_ids=candidate.source_message_ids,
            )],
        ),
        run_id="run_test",
        runtime_model="mock",
        prompt_hash="prompt",
        skill_version_ids=[],
        input_message_ids=candidate.source_message_ids,
    )

    with pytest.raises(ValueError, match="Embedding space mismatch"):
        SQLiteMemoryStore(path, embedding_dim=32, embedding_model="new-model")


def test_add_persists_append_only_memory_relation(tmp_path: Path):
    path = tmp_path / "memory.sqlite3"
    store = SQLiteMemoryStore(path, embedding_dim=32, embedding_model="test-hash")
    _save_input(store, "conv_a", "s1", "conv_a:D1:1", "Alice lived in Boston.", 0)
    old = _candidate("cand_old", "conv_a:D1:1", "Alice lived in Boston.", "Boston")
    first = store.apply_construction_plan(
        "conv_a", "s1", None,
        ConstructionPlan(base_commit_id=None, candidates=[old], decisions=[
            ConstructionDecision(
                candidate_id="cand_old", action="ADD",
                merged_content=old.content, source_message_ids=old.source_message_ids,
            )
        ]),
        "run", "mock", "p1", [], input_message_ids=old.source_message_ids,
    )
    old_version = store.load_snapshot("conv_a", first.commit_id)[0].version_id
    _save_input(store, "conv_a", "s2", "conv_a:D2:1", "Alice moved to Seattle.", 1)
    new = _candidate("cand_new", "conv_a:D2:1", "Alice moved to Seattle.", "Seattle")
    from mim.storage.sqlite_store import MemoryRelation
    second = store.apply_construction_plan(
        "conv_a", "s2", first.commit_id,
        ConstructionPlan(base_commit_id=first.commit_id, candidates=[new], decisions=[
            ConstructionDecision(
                candidate_id="cand_new", action="ADD",
                merged_content=new.content, source_message_ids=new.source_message_ids,
                relations=[MemoryRelation("supersedes", old_version)],
            )
        ]),
        "run", "mock", "p2", [], input_message_ids=new.source_message_ids,
    )

    assert len(store.load_snapshot("conv_a", second.commit_id)) == 2
    with store.open_read_connection() as conn:
        edge = conn.execute(
            "SELECT target_version_id, relation_type FROM memory_relation_edges"
        ).fetchone()
    assert tuple(edge) == (old_version, "supersedes")


def test_skip_persists_duplicate_relation_for_diagnosis(tmp_path: Path):
    path = tmp_path / "memory.sqlite3"
    store = SQLiteMemoryStore(path, embedding_dim=32, embedding_model="test-hash")
    _save_input(store, "conv_a", "s1", "conv_a:D1:1", "Alice lives in Boston.", 0)
    old = _candidate("cand_old", "conv_a:D1:1", "Alice lives in Boston.", "Boston")
    first = store.apply_construction_plan(
        "conv_a", "s1", None,
        ConstructionPlan(base_commit_id=None, candidates=[old], decisions=[
            ConstructionDecision(
                candidate_id="cand_old", action="ADD", merged_content=old.content,
                source_message_ids=old.source_message_ids,
            )
        ]),
        "run", "mock", "p1", [], input_message_ids=old.source_message_ids,
    )
    old_version = store.load_snapshot("conv_a", first.commit_id)[0].version_id
    _save_input(store, "conv_a", "s2", "conv_a:D2:1", "Alice lives in Boston.", 1)
    duplicate = _candidate(
        "cand_duplicate", "conv_a:D2:1", "Alice lives in Boston.", "Boston"
    )
    from mim.storage.sqlite_store import MemoryRelation
    store.apply_construction_plan(
        "conv_a", "s2", first.commit_id,
        ConstructionPlan(
            base_commit_id=first.commit_id,
            candidates=[duplicate],
            decisions=[ConstructionDecision(
                candidate_id="cand_duplicate", action="SKIP",
                source_message_ids=duplicate.source_message_ids,
                relations=[MemoryRelation("duplicate_of", old_version)],
            )],
        ),
        "run", "mock", "p2", [], input_message_ids=duplicate.source_message_ids,
    )

    with store.open_read_connection() as conn:
        edge = conn.execute(
            """SELECT source_version_id, target_version_id, relation_type
               FROM construction_relation_edges"""
        ).fetchone()
    assert tuple(edge) == (None, old_version, "duplicate_of")


def test_update_preserves_old_version_and_inherited_message_lineage(tmp_path: Path):
    store = _store(tmp_path)
    _save_input(store, "conv_a", "s1", "conv_a:D1:1", "I live in Boston.", 0)
    first = _candidate(
        "cand_conv_a_s1_000", "conv_a:D1:1", "Alice lives in Boston.", "Boston"
    )
    first_commit = store.apply_construction_plan(
        conversation_id="conv_a",
        session_id="s1",
        base_commit_id=None,
        plan=ConstructionPlan(
            base_commit_id=None,
            candidates=[first],
            decisions=[ConstructionDecision(
                candidate_id=first.candidate_id,
                action="ADD",
                merged_content=first.content,
                source_message_ids=first.source_message_ids,
                reason="Initial residence.",
            )],
        ),
        run_id="run_test",
        runtime_model="mock",
        prompt_hash="p1",
        skill_version_ids=[],
        input_message_ids=first.source_message_ids,
    )
    memory_id = store.load_snapshot("conv_a")[0].memory_id

    _save_input(store, "conv_a", "s2", "conv_a:D2:1", "I moved to Seattle.", 1)
    second = _candidate(
        "cand_conv_a_s2_000", "conv_a:D2:1", "Alice lives in Seattle.", "Seattle"
    )
    second_commit = store.apply_construction_plan(
        conversation_id="conv_a",
        session_id="s2",
        base_commit_id=first_commit.commit_id,
        plan=ConstructionPlan(
            base_commit_id=first_commit.commit_id,
            candidates=[second],
            decisions=[ConstructionDecision(
                candidate_id=second.candidate_id,
                action="UPDATE",
                target_memory_id=memory_id,
                update_type="state_change",
                merged_content=second.content,
                source_message_ids=second.source_message_ids,
                reason="Residence changed.",
            )],
        ),
        run_id="run_test",
        runtime_model="mock",
        prompt_hash="p2",
        skill_version_ids=["sk_update_v1"],
        input_message_ids=second.source_message_ids,
    )

    active = store.load_snapshot("conv_a", second_commit.commit_id)
    history = store.load_snapshot(
        "conv_a", second_commit.commit_id, include_history=True
    )
    assert [item.content for item in active] == ["Alice lives in Seattle."]
    assert [item.content for item in history] == [
        "Alice lives in Boston.",
        "Alice lives in Seattle.",
    ]

    conn = store.open_read_connection()
    try:
        provenance = ProvenanceService(conn)
        construction_history = provenance.construction_history(
            conversation_id="conv_a",
            message_ids=["conv_a:D1:1"],
            snapshot_commit_id=second_commit.commit_id,
        )
        assert [
            item["version_id"]
            for item in construction_history["snapshot_memories"]
        ] == [
            f"{memory_id}_v2"
        ]
        changes = construction_history["change_events"]
    finally:
        conn.close()
    assert len(changes) == 2
    assert set(changes[-1]["affected_message_ids"]) == {
        "conv_a:D1:1",
        "conv_a:D2:1",
    }


def test_failed_plan_rolls_back_everything(tmp_path: Path):
    store = _store(tmp_path)
    _save_input(store, "conv_a", "s1", "conv_a:D1:1", "A fact.", 0)
    candidate = _candidate(
        "cand_conv_a_s1_000", "conv_a:D1:1", "Alice has a fact.", "fact"
    )
    with pytest.raises(RuntimeError, match="Target memory not found"):
        store.apply_construction_plan(
            conversation_id="conv_a",
            session_id="s1",
            base_commit_id=None,
            plan=ConstructionPlan(
                base_commit_id=None,
                candidates=[candidate],
                decisions=[ConstructionDecision(
                    candidate_id=candidate.candidate_id,
                    action="UPDATE",
                    target_memory_id="mem_other_9999",
                    reason="Invalid target.",
                )],
            ),
            run_id="run_test",
            runtime_model="mock",
            prompt_hash="p",
            skill_version_ids=[],
            input_message_ids=candidate.source_message_ids,
        )
    conn = store.open_read_connection()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM construction_commits"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_candidates"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_delete_retracts_active_memory_but_preserves_history(tmp_path: Path):
    store = _store(tmp_path)
    _save_input(store, "conv_a", "s1", "conv_a:D1:1", "I own a dog.", 0)
    first = _candidate(
        "cand_conv_a_s1_000",
        "conv_a:D1:1",
        "Alice owns a dog.",
        "dog",
    )
    initial = store.apply_construction_plan(
        conversation_id="conv_a",
        session_id="s1",
        base_commit_id=None,
        plan=ConstructionPlan(
            base_commit_id=None,
            candidates=[first],
            decisions=[ConstructionDecision(
                candidate_id=first.candidate_id,
                action="ADD",
                merged_content=first.content,
                source_message_ids=first.source_message_ids,
            )],
        ),
        run_id="run_test",
        runtime_model="mock",
        prompt_hash="p1",
        skill_version_ids=[],
        input_message_ids=first.source_message_ids,
    )
    target = store.load_snapshot("conv_a", initial.commit_id)[0].memory_id

    _save_input(
        store,
        "conv_a",
        "s2",
        "conv_a:D2:1",
        "Correction: I have never owned a dog.",
        1,
    )
    correction = _candidate(
        "cand_conv_a_s2_000",
        "conv_a:D2:1",
        "Alice says the earlier dog ownership claim was false.",
        "dog",
    )
    deleted = store.apply_construction_plan(
        conversation_id="conv_a",
        session_id="s2",
        base_commit_id=initial.commit_id,
        plan=ConstructionPlan(
            base_commit_id=initial.commit_id,
            candidates=[correction],
            decisions=[ConstructionDecision(
                candidate_id=correction.candidate_id,
                action="DELETE",
                target_memory_id=target,
                update_type="retraction",
                source_message_ids=correction.source_message_ids,
            )],
        ),
        run_id="run_test",
        runtime_model="mock",
        prompt_hash="p2",
        skill_version_ids=[],
        input_message_ids=correction.source_message_ids,
    )

    assert store.load_snapshot("conv_a", deleted.commit_id) == []
    history = store.load_snapshot(
        "conv_a", deleted.commit_id, include_history=True
    )
    assert [item.content for item in history] == ["Alice owns a dog."]
    assert history[0].close_reason == "retracted"
    conn = store.open_read_connection()
    try:
        event = conn.execute(
            "SELECT operation, new_version_id FROM memory_change_events "
            "WHERE operation='DELETE' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(event) == ("DELETE", None)


def test_keyword_search_uses_live_connection_and_returns_hit(tmp_path: Path):
    store = _store(tmp_path)
    store.ensure_conversation("conv_a")
    store.insert_memory_version(
        memory_id="mem_conv_a_0001",
        version_no=1,
        conversation_id="conv_a",
        memory_kind="profile",
        subject="Alice",
        predicate="profession",
        object_text="ceramic artist",
        content="Alice works as a ceramic artist.",
        source_message_ids=[],
        entities=["Alice"],
        keywords=["ceramic", "artist"],
        embedding=np.ones(32, dtype=np.float32),
        system_from_commit=0,
    )
    hits = store.fts_search(
        "conv_a", "ceramic artist", as_of_commit=0, limit=5
    )
    assert [hit.version_id for hit in hits] == ["mem_conv_a_0001_v1"]
