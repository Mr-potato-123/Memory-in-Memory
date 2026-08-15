"""Versioned Skill Repository and runtime retrieval tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mim.retrieval.embedder import Embedder
from mim.schemas import ModelResponse, Side
from mim.skill_maker.models import SkillCandidate, SkillPayload
from mim.skill_maker.repository import SkillRepository
from mim.skill_maker.validator import SkillPayloadValidator
from mim.skills import (
    LLMSkillApplicabilityReranker,
    RuntimeSkillQueryBuilder,
    SkillBank,
)


class _RouterModel:
    def __init__(self, text: str):
        self.text = text

    def generate(self, messages, **kwargs):
        return ModelResponse(
            text=self.text,
            provider="mock",
            model="router",
        )


class _CountingEmbedder:
    model_name = "deterministic-hash"

    def __init__(self):
        self._inner = Embedder(self.model_name)
        self.document_calls = 0
        self.query_calls = 0

    @property
    def dim(self):
        return self._inner.dim

    def encode_documents(self, texts):
        self.document_calls += 1
        return self._inner.encode_documents(texts)

    def encode_queries(self, texts):
        self.query_calls += 1
        return self._inner.encode_queries(texts)

    def encode(self, texts):
        return self._inner.encode(texts)


def _candidate(skill_id: str, side: str = "access") -> SkillCandidate:
    return SkillCandidate(
        candidate_id=f"cand_{skill_id}",
        skill_id=skill_id,
        side=side,
        payload=SkillPayload(
            name="Inspect state history",
            description="Use when a question asks for a prior or changed state.",
            content="Search with history enabled, then inspect the logical memory versions.",
        ),
        source_failure_id="failure_train_001",
    )


def test_publish_create_is_visible_to_runtime_and_selected_file(tmp_path: Path):
    repo = SkillRepository(tmp_path / "skills")
    candidate = _candidate("sk_access_history")
    staged = repo.stage_create(candidate)
    bank_version = repo.publish(staged)

    bank = SkillBank.from_repository(repo)
    active = bank.list_active(Side.ACCESS)
    assert bank_version == "v001"
    assert [skill.skill_id for skill in active] == ["sk_access_history"]
    assert (
        tmp_path
        / "skills"
        / "official"
        / "banks"
        / "bank_v001.json"
    ).exists()
    assert (
        tmp_path / "skills" / "official" / "selected.json"
    ).exists()
    assert (
        tmp_path
        / "skills"
        / "candidates"
        / "access"
        / candidate.candidate_id
        / "candidate.json"
    ).exists()


def test_update_creates_new_immutable_skill_version(tmp_path: Path):
    repo = SkillRepository(tmp_path / "skills")
    first = _candidate("sk_access_history")
    repo.publish(repo.stage_create(first))

    updated = _candidate("sk_access_history")
    updated.payload.content = "Inspect all versions and align them to the requested time."
    staged = repo.stage_update("sk_access_history", updated)
    repo.publish(staged)

    assert staged.version_id == "sk_access_history_v2"
    assert staged.parent_version_id == "sk_access_history_v1"
    assert repo.get("sk_access_history", 1) is not None
    assert repo.get("sk_access_history").version == 2


def test_construction_and_access_skills_are_filtered_by_side(tmp_path: Path):
    repo = SkillRepository(tmp_path / "skills")
    repo.publish(repo.stage_create(_candidate("sk_access", "access")))
    repo.publish(repo.stage_create(_candidate("sk_construction", "construction")))
    bank = SkillBank.from_repository(repo)

    assert [item.skill_id for item in bank.list_active(Side.ACCESS)] == [
        "sk_access"
    ]
    assert [
        item.skill_id for item in bank.list_active(Side.CONSTRUCTION)
    ] == ["sk_construction"]


def test_staging_bank_is_isolated_and_cleaned(tmp_path: Path):
    repo = SkillRepository(tmp_path / "skills")
    candidate = _candidate("sk_candidate")
    staging_path = None
    with repo.staging_bank(candidate) as staging:
        staging_path = staging.directory
        assert staging.get("sk_candidate") is not None
        assert repo.get("sk_candidate") is None
    assert staging_path is not None and not staging_path.exists()


def test_published_bank_loads_physically_isolated_files(
    tmp_path: Path,
):
    repo = SkillRepository(tmp_path / "skills")
    repo.publish(repo.stage_create(_candidate("sk_access", "access")))
    repo.publish(
        repo.stage_create(_candidate("sk_construction", "construction"))
    )
    records = [item.to_dict() for item in repo.list_active()]
    bank_dir = tmp_path / "bank2"
    bank_dir.mkdir()
    for side, filename in (
        ("access", SkillBank.published_filename("access", 2)),
        ("construction", SkillBank.published_filename("construction", 2)),
    ):
        payload = {
            "bank": "bank2",
            "side": side,
            "skills": [item for item in records if item["side"] == side],
        }
        (bank_dir / filename).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    bank = SkillBank.load_published(bank_dir)

    assert bank.version == 2
    assert [item.skill_id for item in bank.list_active(Side.ACCESS)] == [
        "sk_access"
    ]
    assert [
        item.skill_id for item in bank.list_active(Side.CONSTRUCTION)
    ] == ["sk_construction"]


def test_published_bank_reuses_persistent_skill_embeddings(tmp_path: Path):
    repo = SkillRepository(tmp_path / "skills")
    repo.publish(repo.stage_create(_candidate("sk_access", "access")))
    repo.publish(
        repo.stage_create(_candidate("sk_construction", "construction"))
    )
    records = [item.to_dict() for item in repo.list_active()]
    bank_dir = tmp_path / "bank1"
    bank_dir.mkdir()
    for side in ("access", "construction"):
        (bank_dir / SkillBank.published_filename(side, 1)).write_text(
            json.dumps({
                "bank": "bank1",
                "side": side,
                "skills": [item for item in records if item["side"] == side],
            }),
            encoding="utf-8",
        )

    writer = _CountingEmbedder()
    bank = SkillBank.load_published(bank_dir)
    cache = bank.precompute_embeddings(writer)
    assert cache.exists()
    assert writer.document_calls == 1

    reader = _CountingEmbedder()
    reloaded = SkillBank.load_published(bank_dir)
    selected, _ = reloaded.retrieve_with_trace(
        "prior or changed state",
        Side.ACCESS,
        reader,
        top_k=1,
        candidate_k=1,
        min_score=0.0,
    )
    assert selected
    assert reader.document_calls == 0
    assert reader.query_calls == 1


def test_initial_bank_is_seeded_as_one_version_zero_snapshot(tmp_path: Path):
    source = SkillRepository(tmp_path / "source")
    source.publish(source.stage_create(_candidate("sk_access", "access")))
    source.publish(
        source.stage_create(_candidate("sk_construction", "construction"))
    )
    records = [item for item in source.list_active()]

    target = SkillRepository(tmp_path / "target")
    target.seed_initial(records)

    assert target.current_version == "v000"
    assert {item.skill_id for item in target.list_active()} == {
        "sk_access",
        "sk_construction",
    }


def test_runtime_skill_retrieval_can_abstain_and_disclose_nearby(
    tmp_path: Path,
):
    repo = SkillRepository(tmp_path / "skills")
    repo.publish(repo.stage_create(_candidate("sk_temporal")))
    bank = SkillBank.from_repository(repo)

    selected, trace = bank.retrieve_with_trace(
        "unrelated question",
        side=Side.ACCESS,
        embedding_index=Embedder("deterministic-hash"),
        top_k=3,
        disclose_k=5,
        min_score=2.0,
    )

    assert selected == []
    assert trace.selected == []
    assert [item.skill_id for item in trace.nearby_not_selected] == [
        "sk_temporal"
    ]
    assert trace.min_score == 2.0


def test_runtime_skill_retrieval_abstains_when_top_scores_are_ambiguous(
    tmp_path: Path,
):
    repo = SkillRepository(tmp_path / "skills")
    repo.publish(repo.stage_create(_candidate("sk_first")))
    repo.publish(repo.stage_create(_candidate("sk_second")))
    bank = SkillBank.from_repository(repo)

    selected, trace = bank.retrieve_with_trace(
        "prior changed state",
        side=Side.ACCESS,
        embedding_index=Embedder("deterministic-hash"),
        top_k=2,
        disclose_k=5,
        min_score=0.0,
        min_semantic_score=0.0,
        min_score_margin=0.001,
    )

    assert selected == []
    assert trace.selected == []
    assert len(trace.nearby_not_selected) == 2
    assert trace.min_score_margin == 0.001


def test_mem0_access_excludes_skills_that_require_second_retrieval(
    tmp_path: Path,
):
    repo = SkillRepository(tmp_path / "skills")
    repo.publish(repo.stage_create(_candidate("sk_access_search")))
    bank = SkillBank.from_repository(repo)

    selected, trace = bank.retrieve_with_trace(
        "Where does Alice live?",
        side=Side.ACCESS,
        embedding_index=Embedder("deterministic-hash"),
        top_k=1,
        min_score=0.0,
        answer_only=True,
    )

    assert selected == []
    assert trace.selected == []


def test_access_recovery_query_keeps_evidence_content_without_storage_noise():
    query = RuntimeSkillQueryBuilder().for_access_recovery({
        "question": "Where does Alice live?",
        "first_search": {
            "hit_count": 1,
            "hits": [{
                "version_id": "mem0:secret-internal-id",
                "score": 0.93,
                "content": "Alice lives in Seattle.",
            }],
        },
    })

    assert "Question: Where does Alice live?" in query
    assert "Alice lives in Seattle." in query
    assert "secret-internal-id" not in query
    assert '"score"' not in query


def test_two_stage_runtime_retrieval_uses_applicability_reranker(
    tmp_path: Path,
):
    repo = SkillRepository(tmp_path / "skills")
    first = _candidate("sk_access_history")
    second = _candidate("sk_access_list")
    second.payload.name = "Complete list coverage"
    second.payload.description = (
        "Use for multi-part list questions requiring every supported item."
    )
    second.payload.content = "Search separately for every missing list component."
    repo.publish(repo.stage_create(first))
    repo.publish(repo.stage_create(second))
    bank = SkillBank.from_repository(repo)
    reranker = LLMSkillApplicabilityReranker(
        _RouterModel(
            '{"selected":[{"skill_id":"sk_access_list",'
            '"reason":"The first result covers only one requested item.",'
            '"trigger_evidence":"The question asks for activities, plural.",'
            '"unresolved_gap":"Other supported activities are missing."}]}'
        )
    )

    selected, trace = bank.retrieve_with_trace(
        "Which outdoor activities did they do together?",
        side=Side.ACCESS,
        embedding_index=Embedder("deterministic-hash"),
        candidate_k=10,
        top_k=2,
        min_score=0.0,
        reranker=reranker,
    )

    assert [skill.skill_id for skill in selected] == ["sk_access_list"]
    assert trace.reranker == "bank1_applicability_router"
    assert trace.candidate_k == 10
    assert trace.selected[0].rerank_rank == 1
    assert "first result" in trace.selected[0].rerank_reason


def test_two_stage_runtime_retrieval_can_reranker_abstain(tmp_path: Path):
    repo = SkillRepository(tmp_path / "skills")
    repo.publish(repo.stage_create(_candidate("sk_access_history")))
    bank = SkillBank.from_repository(repo)

    selected, trace = bank.retrieve_with_trace(
        "Where was the person born?",
        side=Side.ACCESS,
        embedding_index=Embedder("deterministic-hash"),
        candidate_k=10,
        top_k=2,
        min_score=0.0,
        reranker=LLMSkillApplicabilityReranker(
            _RouterModel('{"selected":[]}')
        ),
    )

    assert selected == []
    assert trace.selected == []
    assert trace.nearby_not_selected


def test_construction_skill_query_segments_preserve_late_messages():
    messages = [
        {"speaker": "A", "content": f"early message {index}"}
        for index in range(8)
    ] + [
        {"speaker": "B", "content": "late unique relationship correction"}
    ]

    segments = RuntimeSkillQueryBuilder().for_construction_segments(
        messages,
        messages_per_segment=4,
    )

    assert len(segments) == 3
    assert "late unique relationship correction" in segments[-1]


def test_payload_validator_rejects_case_specific_answer_and_ids():
    payload = SkillPayload(
        name="Seattle answer",
        description="Use when msg_001 asks about residence.",
        content="Return Seattle.",
    )
    valid, errors = SkillPayloadValidator().validate(
        payload,
        reference_answer="Seattle",
        gold_message_ids=["msg_001"],
    )
    assert not valid
    assert any("reference answer" in error for error in errors)
    assert any("system IDs" in error for error in errors)


def test_payload_validator_allows_reference_concept_in_general_rule():
    payload = SkillPayload(
        name="Preserve recreational activities",
        description="Use when a message names a concrete activity.",
        content="Store bowling or another named activity as evidence states it.",
    )

    valid, errors = SkillPayloadValidator().validate(
        payload,
        side="construction",
        reference_answer="bowling",
    )

    assert valid, errors


def test_payload_validator_rejects_unsupported_construction_contract():
    payload = SkillPayload(
        name="Track totals",
        description="Use when an ordinal achievement appears.",
        content=[
            "Store an additional memory_kind 'metric' for the total."
        ],
    )

    valid, errors = SkillPayloadValidator().validate(
        payload,
        side="construction",
    )

    assert not valid
    assert any("unsupported memory kind" in error for error in errors)


def test_payload_validator_rejects_construction_storage_mutation():
    payload = SkillPayload(
        name="Rewrite related memory",
        description="When a later statement changes a preference.",
        content=["Update the existing memory record with the new value."],
    )

    valid, errors = SkillPayloadValidator().validate(
        payload, side="construction"
    )

    assert not valid
    assert any("forbidden storage mutation" in error for error in errors)


def test_payload_validator_rejects_obsolete_mem0_retrieval_skill():
    payload = SkillPayload(
        name="Expand preference lookup",
        description="When the first search misses a favorite activity.",
        content=["In A1, increase top-k and run a supplemental query."],
    )

    valid, errors = SkillPayloadValidator().validate(payload, side="access")

    assert not valid
    assert any("fixed Mem0 retrieval" in error for error in errors)
