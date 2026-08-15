"""Candidate/official isolation and batch Skill maintenance tests."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.diagnosis.evidence import DiagnosisEvidenceRepository
from mim.agents.skill_learning import (
    BatchSkillCrudAgent,
    CandidateSkillAgent,
    DirectCaseCrudAgent,
)
from mim.retrieval.embedder import Embedder
from mim.config import ModelConfig
from mim.llm.mock_client import MockClient
from mim.schemas import Side
from mim.skill_maker.batch import (
    BatchSkillRetriever,
    CandidateClusterer,
    SkillCrudExecutor,
)
from mim.skill_maker.models import SkillOperation
from mim.skill_maker.cluster_v2 import cluster_v2
from mim.skill_maker.models import (
    CandidateResolution,
    SkillBatchPlan,
    SkillCandidate,
    SkillOperation,
    SkillPayload,
)
from mim.skill_maker.success_examples import SuccessfulSkillExampleIndex
from mim.skill_maker.repository import SkillRepository
from mim.skill_maker.pipeline import SkillBankPipeline
from mim.skill_maker.validator import SkillPayloadValidator
from mim.skills import SkillBank
from mim.storage.sqlite_store import SQLiteMemoryStore


def _candidate(index: int, side: str = "access") -> SkillCandidate:
    return SkillCandidate(
        candidate_id=f"cand_{side}_{index}",
        skill_id=f"sk_{side}_{index}",
        side=side,
        payload=SkillPayload(
            name=f"Skill {index}",
            description=(
                "Use when one requested item has stronger direct evidence."
            ),
            content=[f"Prefer direct supported item {index} over tangential facts."],
        ),
        solves="Prevents tangential facts from displacing direct evidence.",
        source_diagnosis_id=f"diag_{index}",
    )


def test_runtime_trace_discloses_nearby_official_skills(tmp_path: Path):
    repository = SkillRepository(tmp_path / "skills")
    for index in range(3):
        candidate = _candidate(index)
        repository.publish(repository.stage_create(candidate))

    bank = SkillBank.from_repository(repository)
    selected, trace = bank.retrieve_with_trace(
        "related retrieval expression",
        Side.ACCESS,
        Embedder("deterministic-hash"),
        top_k=1,
        disclose_k=2,
        trace_id="trace_test",
    )

    assert len(selected) == 1
    assert len(trace.selected) == 1
    assert len(trace.nearby_not_selected) == 2
    assert trace.bank_version == "v003"
    assert trace.selected[0].content


def test_empty_official_bank_keeps_candidates_physically_separate(
    tmp_path: Path,
):
    repository = SkillRepository(tmp_path / "skills")
    candidates = [_candidate(1), _candidate(2)]
    for candidate in candidates:
        repository.save_candidate(candidate)

    batch = BatchSkillRetriever(
        Embedder("deterministic-hash")
    ).retrieve(
        batch_id="batch_empty",
        candidates=candidates,
        repository=repository,
    )

    assert batch.retrieved_skill_ids == []
    assert repository.list_active("access") == []
    assert len(repository.list_candidates("access")) == 2
    assert not list(
        (tmp_path / "skills" / "official" / "banks").glob(
            "bank_v001.json"
        )
    )


def test_diagnosis_candidate_crud_publish_and_runtime_load_end_to_end(
    tmp_path: Path,
):
    """A completed diagnosis must become a loadable official Skill."""
    diagnosis = {
        "diagnosis_id": "diag_access_e2e",
        "diagnosis_type": "ANSWER_FAILURE",
        "status": "completed",
        "problem_found": True,
        "review_required": False,
        "retrieved_context_sufficient": True,
        "skill_learnable": True,
        "repair_package": {
            "eligible_for_skill_generation": True,
            "failure_mode": "TEMPORAL_REASONING",
        },
    }
    candidate_response = {
        "decision": "PROPOSE_SKILL",
        "maintenance_intent": "ADD",
        "related_existing_skill_ids": [],
        "mechanism_signature": {
            "observable_trigger": "The question specifies a dated period.",
            "evidence_precondition": "Returned memories contain several dated events.",
            "failed_behavior": "The answer mixes events across periods.",
            "corrective_operation": "Select evidence inside the requested period.",
            "safety_boundary": "Do not infer dates missing from memories."
        },
        "skill": {
            "name": "Restore explicit time scope",
            "description": (
                "Use when a question specifies a dated period and returned "
                "memories contain events from multiple periods."
            ),
            "content": [
                "Use only events whose recorded dates fall inside the requested period."
            ],
        },
        "solves": "Prevents answers from mixing returned events across time periods.",
    }
    model = SimpleNamespace(
        generate=lambda *args, **kwargs: SimpleNamespace(
            text=json.dumps(candidate_response)
        )
    )
    candidate = CandidateSkillAgent(model, prompt="test").generate(
        diagnosis=diagnosis,
        side="access",
    )
    assert candidate is not None
    valid, errors = SkillPayloadValidator().validate(
        candidate.payload, side="access"
    )
    assert valid, errors

    repository = SkillRepository(tmp_path / "skills")
    repository.save_candidate(candidate)

    class _CreateCrudAgent:
        @staticmethod
        def plan(*, batch, official_records):
            draft = batch.candidates[0]
            return SkillBatchPlan(
                transaction_id="tx_access_e2e",
                side="access",
                base_bank_version=batch.base_bank_version,
                candidate_resolutions=[CandidateResolution(
                    candidate_id=draft.candidate_id,
                    resolution="CREATED",
                    target_skill_ids=[draft.skill_id],
                )],
                operations=[SkillOperation(
                    operation="add_skill",
                    skill_id=draft.skill_id,
                    side="access",
                    name=draft.payload.name,
                    description=draft.payload.description,
                    content=draft.payload.content,
                    source_candidate_ids=[draft.candidate_id],
                )],
            )

    embedder = Embedder("deterministic-hash")
    outcome = SkillBankPipeline(
        repository=repository,
        clusterer=CandidateClusterer(embedder),
        retriever=BatchSkillRetriever(embedder),
        executor=SkillCrudExecutor(repository),
        run_id="e2e",
        min_candidate_support=1,
    ).consolidate(side="access", batch_crud_agent=_CreateCrudAgent())

    assert outcome["published"] is True
    assert outcome["new_version"] == "v001"
    bank = SkillBank.from_repository(repository)
    selected, trace = bank.retrieve_with_trace(
        "What happened during that dated period?",
        Side.ACCESS,
        embedder,
        top_k=1,
        disclose_k=1,
        trace_id="trace_e2e",
    )
    assert [skill.skill_id for skill in selected] == [candidate.skill_id]
    assert trace.selected[0].version_id.endswith("_v1")


def test_candidate_agent_repairs_overlong_instruction_without_truncation():
    overlong = {
        "decision": "PROPOSE_SKILL",
        "skill": {
            "name": "Time scope",
            "description": "Use when returned dated events span multiple periods.",
            "content": ["Keep the instruction complete. " * 12],
        },
        "solves": "Prevents time-scope answer errors.",
        "mechanism_signature": {
            "observable_trigger": "A dated period is requested.",
            "evidence_precondition": "Returned events span periods.",
            "failed_behavior": "The answer mixes periods.",
            "corrective_operation": "Filter evidence by stated period.",
            "safety_boundary": "Do not infer missing dates."
        },
    }
    repaired = {
        "decision": "PROPOSE_SKILL",
        "skill": {
            "name": "Time scope",
            "description": "Use when returned dated events span multiple periods.",
            "content": ["Use only returned events inside the stated period."],
        },
        "solves": "Prevents time-scope answer errors.",
        "mechanism_signature": overlong["mechanism_signature"],
    }
    model = MockClient(ModelConfig(provider="mock", model="mock"))
    model.set_script([
        model._make_resp(json.dumps(overlong)),
        model._make_resp(json.dumps(repaired)),
    ])

    candidate = CandidateSkillAgent(model, prompt="test").generate(
        diagnosis={
            "diagnosis_id": "diag_retry",
            "diagnosis_type": "ANSWER_FAILURE",
            "retrieved_context_sufficient": True,
            "skill_learnable": True,
            "repair_package": {"eligible_for_skill_generation": True},
        },
        side="access",
    )

    assert candidate is not None
    assert candidate.payload.content == [
        "Use only returned events inside the stated period."
    ]
    assert not candidate.payload.content[0].endswith("…")


def test_access_candidate_agent_rejects_fixed_search_failure_without_calling_model():
    model = MockClient(ModelConfig(provider="mock", model="mock"))

    candidate = CandidateSkillAgent(model, prompt="test").generate(
        diagnosis={
            "diagnosis_id": "fixed_search_failure",
            "diagnosis_type": "ACCESS_FAILURE",
            "problem_found": True,
            "repair_package": {
                "eligible_for_skill_generation": False,
                "failure_owner": "mem0_retrieval",
            },
        },
        side="access",
    )

    assert candidate is None

def test_one_crud_transaction_can_create_multiple_official_skills(
    tmp_path: Path,
):
    repository = SkillRepository(tmp_path / "skills")
    candidates = [_candidate(1), _candidate(2)]
    for candidate in candidates:
        repository.save_candidate(candidate)
    batch = BatchSkillRetriever(
        Embedder("deterministic-hash")
    ).retrieve(
        batch_id="batch_create",
        candidates=candidates,
        repository=repository,
    )
    plan = SkillBatchPlan(
        transaction_id="tx_multi_create",
        side="access",
        base_bank_version="v000",
        candidate_resolutions=[
            CandidateResolution(
                candidate_id=candidate.candidate_id,
                resolution="CREATED",
                target_skill_ids=[candidate.skill_id],
            )
            for candidate in candidates
        ],
        operations=[
            SkillOperation(
                operation="add_skill",
                skill_id=candidate.skill_id,
                side="access",
                name=candidate.payload.name,
                description=candidate.payload.description,
                content=candidate.payload.content,
                source_candidate_ids=[candidate.candidate_id],
            )
            for candidate in candidates
        ],
    )

    version = SkillCrudExecutor(repository).apply(batch, plan)

    assert version == "v001"
    assert len(repository.list_active("access")) == 2
    assert (
        tmp_path
        / "skills"
        / "transactions"
        / "tx_multi_create.json"
    ).exists()


class _TopicEmbedder:
    def encode(self, texts: list[str]) -> np.ndarray:
        rows = []
        for text in texts:
            lowered = text.lower()
            rows.append([1.0, 0.0] if "alpha" in lowered else [0.0, 1.0])
        return np.asarray(rows, dtype=np.float32)


def test_v2_clustering_is_semantic_before_size_bounding():
    candidates = []
    for index in range(12):
        topic = "alpha" if index % 2 == 0 else "beta"
        candidates.append(
            SkillCandidate(
                candidate_id=f"cand_access_{index:02d}",
                side="access",
                payload=SkillPayload(
                    name=topic,
                    description=f"Use when {topic} evidence is directly relevant.",
                    content=[f"Prefer directly supported {topic} evidence."],
                ),
                solves=f"solve {topic}",
                target_first_break="EVIDENCE_SELECTION",
            )
        )

    groups = cluster_v2(
        candidates,
        _TopicEmbedder(),
        target_cluster_size=3,
        max_cluster_size=4,
    )

    assert max(map(len, groups)) <= 4
    assert all(
        len({candidate.payload.name for candidate in group}) == 1
        for group in groups
    )


def test_v2_clustering_never_mixes_answer_failure_modes():
    candidates = [
        SkillCandidate(
            candidate_id="cand_access_over",
            side="access",
            payload=SkillPayload(
                name="favorite",
                description="Use when one favorite is requested.",
                content=["Return only the directly supported favorite."],
            ),
            solves="Avoid extra alternatives.",
            target_first_break="OVER_INCLUSION",
        ),
        SkillCandidate(
            candidate_id="cand_access_time",
            side="access",
            payload=SkillPayload(
                name="favorite",
                description="Use when a dated favorite is requested.",
                content=["Respect the requested time period."],
            ),
            solves="Avoid mixing periods.",
            target_first_break="TEMPORAL_REASONING",
        ),
    ]

    groups = cluster_v2(candidates, _TopicEmbedder())

    assert len(groups) == 2


def test_crud_relation_ids_are_all_llm_visible(tmp_path: Path):
    repository = SkillRepository(tmp_path / "skills")
    for index in range(5):
        candidate = _candidate(index)
        repository.publish(repository.stage_create(candidate))
    draft = _candidate(99)

    batch = BatchSkillRetriever(
        Embedder("deterministic-hash"),
        per_candidate_k=3,
        guaranteed_per_candidate=2,
        max_bank_context=3,
    ).retrieve(
        batch_id="visible_relations",
        candidates=[draft],
        repository=repository,
    )

    assert {relation.skill_id for relation in batch.relations} <= set(
        batch.retrieved_skill_ids
    )


def test_crud_maps_cluster_provenance_to_current_draft(tmp_path: Path):
    draft = _candidate(99, side="construction")
    draft.source_candidate_ids = ["cand_source_a", "cand_source_b"]
    repository = SkillRepository(tmp_path / "skills")
    batch = BatchSkillRetriever(Embedder("deterministic-hash")).retrieve(
        batch_id="cluster_provenance",
        candidates=[draft],
        repository=repository,
    )
    response = {
        "transaction_id": "tx_cluster_provenance",
        "candidate_resolutions": [
            {
                "candidate_id": draft.candidate_id,
                "resolution": "CREATED",
                "target_skill_ids": [draft.skill_id],
                "reason": "create",
            }
        ],
        "operations": [
            {
                "operation": "add_skill",
                "skill_id": draft.skill_id,
                "side": "construction",
                "name": draft.payload.name,
                "description": draft.payload.description,
                "content": draft.payload.content,
                # This is the model error observed in the full run: it copied
                # nested provenance instead of the current draft ID.
                "source_candidate_ids": [
                    "cand_source_a", "cand_source_b"
                ],
                "reason": "create",
            }
        ],
    }
    model = SimpleNamespace(
        generate=lambda *args, **kwargs: SimpleNamespace(
            text=json.dumps(response)
        )
    )

    plan = BatchSkillCrudAgent(model, prompt="test").plan(
        batch=batch,
        official_records=[],
    )

    assert plan.operations[0].source_candidate_ids == [draft.candidate_id]
    SkillCrudExecutor(repository).apply(batch, plan)


def test_direct_case_crud_uses_case_provenance_and_answer_side(tmp_path: Path):
    repository = SkillRepository(tmp_path / "skills")
    batch = BatchSkillRetriever(Embedder("deterministic-hash")).retrieve(
        batch_id="direct_answer",
        candidates=[_candidate(77, side="access")],
        repository=repository,
    )
    case_id = batch.candidates[0].candidate_id
    response = {
        "transaction_id": "tx_direct",
        "decision": "APPLY",
        "reason": "Learn an evidence-bound abstention rule.",
        "operations": [
            {
                "operation": "add_skill",
                "skill_id": "sk_access_abstain",
                "side": "construction",
                "name": "Evidence-bound abstention",
                "description": "When no retrieved evidence supports the asked claim; not when direct support exists.",
                "content": ["Abstain instead of substituting a related entity or event."],
                "source_candidate_ids": ["trace_id_that_must_not_escape"],
            }
        ],
    }
    model = SimpleNamespace(
        generate=lambda *args, **kwargs: SimpleNamespace(text=json.dumps(response))
    )

    plan = DirectCaseCrudAgent(model, prompt="test").plan(
        case_id=case_id,
        side="access",
        direction="C2W",
        diagnosis={"stage": "answer"},
        batch=batch,
        official_records=[],
    )

    assert plan.side == "access"
    assert plan.operations[0].side == "access"
    assert plan.operations[0].source_candidate_ids == [case_id]
    with pytest.raises(ValueError, match="bypasses evidence-grounded retrieval"):
        SkillCrudExecutor(repository).apply(batch, plan)


def test_skill_operation_accepts_null_new_content_for_retryable_model_output():
    operation = SkillOperation(
        operation="update_content",
        skill_id="sk_access_example",
        new_content=None,
    )
    assert operation.new_content == ""
    assert operation.expected_content is None


def test_crud_cannot_mutate_unseen_official_skill(tmp_path: Path):
    repository = SkillRepository(tmp_path / "skills")
    for index in range(2):
        candidate = _candidate(index)
        repository.publish(repository.stage_create(candidate))
    draft = _candidate(99)
    batch = BatchSkillRetriever(
        Embedder("deterministic-hash"),
        per_candidate_k=1,
        guaranteed_per_candidate=1,
        max_bank_context=1,
    ).retrieve(
        batch_id="unseen_target",
        candidates=[draft],
        repository=repository,
    )
    unseen = next(
        record.skill_id
        for record in repository.list_active("access")
        if record.skill_id not in batch.retrieved_skill_ids
    )
    plan = SkillBatchPlan(
        transaction_id="tx_unseen",
        side="access",
        base_bank_version=repository.current_version,
        candidate_resolutions=[
            CandidateResolution(
                candidate_id=draft.candidate_id,
                resolution="MERGED_INTO_EXISTING",
                target_skill_ids=[unseen],
            )
        ],
        operations=[
            SkillOperation(
                operation="update_description",
                skill_id=unseen,
                side="access",
                description="Use when this unseen Skill should be changed.",
                source_candidate_ids=[draft.candidate_id],
            )
        ],
    )

    with pytest.raises(ValueError, match="not supplied"):
        SkillCrudExecutor(repository).apply(batch, plan)


def test_skill_operation_normalizes_model_array_for_new_content():
    operation = SkillOperation(
        operation="update_content",
        skill_id="sk_access_1",
        side="access",
        content="one appended rule",
        new_content=["first replacement", "second replacement"],
    )

    assert operation.content == ["one appended rule"]
    assert operation.new_content == "first replacement\nsecond replacement"


def test_crud_content_reference_survives_earlier_index_shift():
    record = SimpleNamespace(
        skill_id="sk_access_1",
        payload=SimpleNamespace(content=["first", "target", "last"]),
    )
    operation = SkillOperation(
        operation="update_content",
        skill_id="sk_access_1",
        side="access",
        content_index=2,  # Index from the pre-mutation planning snapshot.
        expected_content="target",
        new_content="replacement",
    )

    # A previous operation removed an item before ``target``.
    record.payload.content.pop(0)

    assert SkillCrudExecutor._content_index(record, operation) == 0


def test_diagnosis_reads_persisted_access_skill_trace(tmp_path: Path):
    store = SQLiteMemoryStore(
        tmp_path / "memory.sqlite3",
        embedding_dim=8,
        embedding_model="test",
    )
    trace = {
        "trace_id": "trace_access",
        "side": "access",
        "selected": [{"skill_id": "sk_1", "score": 0.9}],
        "nearby_not_selected": [{"skill_id": "sk_2", "score": 0.8}],
    }
    with store._conn() as connection:
        store.record_access_run(
            connection,
            access_run_id="access_1",
            run_id="run",
            conversation_id="conv",
            qa_id="qa",
            snapshot_commit_id=0,
            question="question",
            skill_trace=trace,
        )

    connection = sqlite3.connect(store.database_path)
    connection.row_factory = sqlite3.Row
    try:
        loaded = DiagnosisEvidenceRepository(
            connection
        ).access_skill_trace("access_1")
    finally:
        connection.close()

    assert loaded == trace


def test_success_example_prefers_same_selected_skill():
    index = SuccessfulSkillExampleIndex([
        {
            "side": "access",
            "judge_label": "C",
            "qa_id": "qa_fallback",
            "skill_ids": ["sk_other"],
        },
        {
            "side": "access",
            "judge_label": "C",
            "qa_id": "qa_exact",
            "skill_ids": ["sk_target"],
        },
        {
            "side": "access",
            "judge_label": "I",
            "qa_id": "qa_wrong",
            "skill_ids": ["sk_target"],
        },
    ])

    selected = index.select(
        side="access",
        official_skill_trace={
            "selected": [{"skill_id": "sk_target"}],
            "nearby_not_selected": [],
        },
    )

    assert selected is not None
    assert selected["qa_id"] == "qa_exact"
    assert selected["relationship_to_diagnosis"] == "same_selected_skill"


def test_success_example_uses_labelled_same_side_fallback():
    index = SuccessfulSkillExampleIndex([
        {
            "side": "construction",
            "judge_label": "C",
            "qa_id": "qa_calibration",
            "skill_ids": ["sk_cons"],
        }
    ])

    selected = index.select(
        side="construction",
        official_skill_trace={"selected": [{"skill_id": "sk_missing"}]},
    )

    assert selected is not None
    assert selected["qa_id"] == "qa_calibration"
    assert selected["relationship_to_diagnosis"] == (
        "same_side_calibration_only"
    )
