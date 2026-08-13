"""Construction and multi-route memory retrieval regression tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mim.agents.access import AccessAgent, EvidenceWorkspace
from mim.agents.access_v2 import StableAccessAgent
from mim.agents.construction import ConstructionAgent
from mim.config import ModelConfig
from mim.llm.mock_client import MockClient
from mim.retrieval.embedder import Embedder
from mim.retrieval.hybrid import HybridRetriever
from mim.schemas import AgentAction, Question, Side, SkillRecord
from mim.storage.sqlite_store import (
    MemoryCandidate,
    MemoryHit,
    SearchFilters,
    SQLiteMemoryStore,
)


def _components(tmp_path: Path):
    embedder = Embedder("deterministic-hash")
    store = SQLiteMemoryStore(
        tmp_path / "memory.sqlite3",
        embedding_dim=embedder.dim,
        embedding_model=embedder.model_name,
    )
    store.ensure_conversation("conv")
    return store, embedder, HybridRetriever(store, embedder)


def _insert(
    store: SQLiteMemoryStore,
    embedder: Embedder,
    number: int,
    content: str,
    *,
    subject: str,
    kind: str = "event",
    entities: list[str] | None = None,
    world_start: str | None = None,
) -> None:
    store.insert_memory_version(
        memory_id=f"mem_conv_{number:04d}",
        version_no=1,
        conversation_id="conv",
        memory_kind=kind,
        subject=subject,
        predicate=None,
        object_text=None,
        content=content,
        source_message_ids=[],
        entities=entities or [subject],
        keywords=[],
        embedding=embedder.encode([content])[0],
        system_from_commit=0,
        world_start=world_start,
    )


def test_semantic_route_uses_the_requested_conversation(tmp_path: Path):
    store, embedder, retriever = _components(tmp_path)
    _insert(
        store,
        embedder,
        1,
        "Alice enjoys ceramic painting.",
        subject="Alice",
    )

    hits = retriever.search(
        conversation_id="conv",
        snapshot_commit_id=0,
        query="Alice enjoys ceramic painting",
        strategy="semantic",
        top_k=3,
    )

    assert [hit.memory_id for hit in hits] == ["mem_conv_0001"]
    assert hits[0].matched_paths == ["semantic"]


def test_bm25_and_keyword_routes_are_independently_available(tmp_path: Path):
    store, embedder, retriever = _components(tmp_path)
    _insert(
        store,
        embedder,
        1,
        "James visited Nuuk during his trip.",
        subject="James",
        entities=["James", "Nuuk"],
    )
    _insert(
        store,
        embedder,
        2,
        "James visited a city during his trip.",
        subject="James",
        entities=["James"],
    )

    filters = SearchFilters(conversation_id="conv", as_of_commit=0)
    bm25_hits = retriever.search(
        conversation_id="conv",
        snapshot_commit_id=0,
        query="James Nuuk trip",
        strategy="bm25",
        filters=filters,
        top_k=2,
    )
    keyword_hits = retriever.search(
        conversation_id="conv",
        snapshot_commit_id=0,
        query="additional country",
        keywords=["Nuuk"],
        strategy="keyword",
        filters=filters,
        top_k=2,
    )

    assert bm25_hits[0].memory_id == "mem_conv_0001"
    assert keyword_hits[0].memory_id == "mem_conv_0001"
    assert bm25_hits[0].matched_paths == ["bm25"]
    assert keyword_hits[0].matched_paths == ["keyword"]


def test_access_drops_unknown_memory_kind_filters(tmp_path: Path):
    store, embedder, retriever = _components(tmp_path)
    _insert(
        store,
        embedder,
        1,
        "James bought tickets to Toronto.",
        subject="James",
        kind="plan",
        entities=["James", "Toronto"],
    )
    agent = AccessAgent(
        MockClient(ModelConfig(provider="mock", model="mock")),
        store,
        retriever,
    )
    workspace = EvidenceWorkspace(
        question="Where did James plan to travel?",
        snapshot_commit_id=0,
    )
    observation = agent._execute_search(
        AgentAction(
            action="search_memory",
            arguments={
                "query": "James travel tickets",
                "strategy": "hybrid",
                "keywords": ["Toronto"],
                "memory_kinds": ["travel", "personal"],
                "depth": "deep",
                "top_k": 5,
            },
            reason="Find the destination.",
        ),
        workspace,
        "conv",
    )

    assert observation["memory_kinds"] == []
    assert observation["ignored_memory_kinds"] == ["travel", "personal"]
    assert observation["hits"][0]["memory_id"] == "mem_conv_0001"


def test_access_exposes_world_time_in_search_results(tmp_path: Path):
    store, embedder, retriever = _components(tmp_path)
    _insert(
        store,
        embedder,
        1,
        "John resumed playing drums a month ago.",
        subject="John",
        world_start="2022-02",
    )
    agent = AccessAgent(
        MockClient(ModelConfig(provider="mock", model="mock")),
        store,
        retriever,
    )
    workspace = EvidenceWorkspace(
        question="When did John resume playing drums?",
        snapshot_commit_id=0,
    )

    observation = agent._execute_search(
        AgentAction(
            action="search_memory",
            arguments={
                "query": "John resumed playing drums",
                "strategy": "hybrid",
                "top_k": 3,
            },
            reason="Find the date.",
        ),
        workspace,
        "conv",
    )

    assert observation["hits"][0]["world_start"] == "2022-02"


def test_access_v2_query_plan_is_deterministic():
    question = "When did Evan start working out at the gym?"
    first = StableAccessAgent.build_query_plan(question)
    second = StableAccessAgent.build_query_plan(question)
    assert first == second
    assert first["entities"] == ["Evan"]
    assert first["original_query"] == question


def test_access_v2_filters_case_answering_but_keeps_retrieval_guidance():
    skill = SkillRecord(
        skill_id="legacy", version=1, side=Side.ACCESS,
        name="Legacy subject guard",
        description="When subject evidence may be incomplete.",
        content=[
            "Re-search with the named subject and exact date.",
            "If no direct memory exists, answer No information available.",
        ],
    )
    usable = StableAccessAgent._usable_skills([skill])
    assert StableAccessAgent._skill_payload(usable) == [{
        "skill_id": "legacy_v1",
        "name": "Legacy subject guard",
        "when": "When subject evidence may be incomplete.",
        "guidance": ["Re-search with the named subject and exact date."],
    }]


def test_access_v2_counts_only_explicitly_applied_skills():
    skill = SkillRecord(
        skill_id="coverage", version=2, side=Side.ACCESS,
        name="Coverage", description="When a list requires distinct evidence.",
        content=["Preserve evidence for every distinct item."],
    )
    assert StableAccessAgent._applied_skill_ids(
        {"applied_skill_ids": ["coverage_v2", "unknown_v1"]}, [skill]
    ) == ["coverage_v2"]


def test_access_may_answer_after_one_search_when_model_judges_full(
    tmp_path: Path,
):
    store, embedder, retriever = _components(tmp_path)
    _insert(
        store,
        embedder,
        1,
        "James has three dogs.",
        subject="James",
        entities=["James", "dogs"],
    )
    model = MockClient(ModelConfig(provider="mock", model="mock"))
    search = {
        "action": "search_memory",
        "arguments": {
            "strategy": "hybrid",
            "query": "James pets",
            "keywords": ["James", "dogs"],
            "depth": "standard",
            "top_k": 3,
        },
        "reason": "Find the pet count.",
    }
    model.set_script(
        [
            model._make_resp(json.dumps(search)),
            model._make_resp(
                json.dumps(
                    {
                        "action": "answer",
                        "arguments": {
                            "answer": "three",
                            "evidence_version_ids": ["mem_conv_0001_v1"],
                        },
                        "reason": "One memory says three.",
                    }
                )
            ),
        ]
    )
    agent = AccessAgent(
        model,
        store,
        retriever,
        max_steps=4,
    )

    result = agent.answer(
        Question(
            qa_id="qa_count",
            question="How many pets does James have?",
            reference_answer="Three dogs.",
            category=3,
        ),
        conversation_id="conv",
        snapshot_commit_id=0,
        skills=[],
    )

    assert result.answer == "three"
    assert result.steps == 2


def test_access_loads_recovery_skill_only_after_first_default_search(
    tmp_path: Path,
):
    store, embedder, retriever = _components(tmp_path)
    _insert(store, embedder, 1, "James has three dogs.", subject="James")
    model = MockClient(ModelConfig(provider="mock", model="mock"))
    model.set_script([
        model._make_resp(json.dumps({
            "action": "search_memory",
            "arguments": {"strategy": "hybrid", "query": "James pets",
                          "keywords": ["James"], "top_k": 3},
            "reason": "Run the default lookup.",
        })),
        model._make_resp(json.dumps({
            "action": "answer",
            "arguments": {"answer": "three",
                          "evidence_version_ids": ["mem_conv_0001_v1"]},
            "reason": "The first result is complete.",
        })),
    ])
    calls = []
    skill = SkillRecord(
        skill_id="skill_recovery", version=1, side=Side.ACCESS,
        name="Recover a missing count",
        description="When the first result omits the requested count.",
        content=["Search for an explicit quantity."],
    )

    result = AccessAgent(model, store, retriever, max_steps=3).answer(
        Question(qa_id="qa_recovery", question="How many pets?",
                 reference_answer="three", category=1),
        conversation_id="conv", snapshot_commit_id=0, skills=[],
        recovery_skill_loader=lambda context: calls.append(context) or [skill],
    )

    assert len(calls) == 1
    assert calls[0]["first_search"]["hit_count"] == 1
    assert result.used_skill_ids == ["skill_recovery_v1"]


def test_access_normalizes_empty_answer_to_unanswerable(tmp_path: Path):
    store, _, retriever = _components(tmp_path)
    model = MockClient(ModelConfig(provider="mock", model="mock"))
    model.set_script(
        [
            model._make_resp(
                json.dumps(
                    {
                        "action": "answer",
                        "arguments": {
                            "answer": "",
                            "evidence_version_ids": [],
                        },
                        "reason": "The evidence is insufficient.",
                    }
                )
            )
        ]
    )
    agent = AccessAgent(model, store, retriever, max_steps=2)

    result = agent.answer(
        Question(
            qa_id="qa_empty",
            question="What is not present?",
            reference_answer="unknown",
            category=4,
        ),
        conversation_id="conv",
        snapshot_commit_id=0,
        skills=[],
    )

    assert result.answer == "No information available."
    assert result.error is None


def test_access_budget_exhaustion_gets_answer_only_final_turn(tmp_path: Path):
    store, embedder, retriever = _components(tmp_path)
    _insert(
        store,
        embedder,
        1,
        "James has three dogs.",
        subject="James",
    )
    model = MockClient(ModelConfig(provider="mock", model="mock"))
    model.set_script(
        [
            model._make_resp(
                json.dumps(
                    {
                        "action": "search_memory",
                        "arguments": {
                            "strategy": "hybrid",
                            "query": "James dogs",
                            "keywords": ["James", "dogs"],
                            "depth": "standard",
                            "top_k": 3,
                        },
                        "reason": "Retrieve the count.",
                    }
                )
            ),
            model._make_resp(
                json.dumps(
                    {
                        "action": "answer",
                        "arguments": {
                            "answer": "three",
                            "evidence_version_ids": ["mem_conv_0001_v1"],
                        },
                        "reason": "Answer from the accumulated result.",
                    }
                )
            ),
        ]
    )
    agent = AccessAgent(model, store, retriever, max_steps=1)

    result = agent.answer(
        Question(
            qa_id="qa_forced_answer",
            question="How many dogs does James have?",
            reference_answer="three",
            category=1,
        ),
        conversation_id="conv",
        snapshot_commit_id=0,
        skills=[],
    )

    assert result.answer == "three"
    assert result.error is None
    assert result.action_records[-1]["response"]["status"] == (
        "accepted_forced_answer"
    )


class _CountingMock(MockClient):
    def __init__(self):
        super().__init__(ModelConfig(provider="mock", model="mock"))
        self.calls = 0

    def generate(self, messages, **kwargs):
        self.calls += 1
        return super().generate(messages, **kwargs)


def test_construction_c2_adds_new_candidates_in_one_model_call(tmp_path: Path):
    store, embedder, _ = _components(tmp_path)
    model = _CountingMock()
    candidates = [
        MemoryCandidate(
            candidate_id=f"cand_{index}",
            memory_kind="event",
            subject="James",
            predicate=None,
            object_text=None,
            content=content,
            world_start="2022-03-16",
            world_end=None,
            source_message_ids=[f"conv:D1:{index}"],
            entities=["James"],
            keywords=[],
            confidence=0.9,
            embedding=embedder.encode([content])[0],
        )
        for index, content in enumerate(
            ["James went bowling.", "James scored two strikes."], 1
        )
    ]
    agent = ConstructionAgent(model, store, embedder)
    model.set_script([model._make_resp(json.dumps({"decisions": [
        {"candidate_id": "cand_1", "action": "ADD", "relations": []},
        {"candidate_id": "cand_2", "action": "ADD", "relations": []},
    ]}))])

    plan = agent.build_plan(
        base_commit_id=None,
        conversation_id="conv",
        candidates=candidates,
        skills=[],
    )

    assert model.calls == 1
    assert len(plan.decisions) == 2
    assert all(decision.action == "ADD" for decision in plan.decisions)


def test_construction_plan_skips_only_exact_active_duplicate(
    tmp_path: Path,
    monkeypatch,
):
    store, embedder, _ = _components(tmp_path)
    model = _CountingMock()
    candidate = MemoryCandidate(
        candidate_id="cand_exact",
        memory_kind="preference",
        subject="James",
        predicate="likes",
        object_text="bowling",
        content="James likes bowling.",
        world_start=None,
        world_end=None,
        source_message_ids=["conv:D1:1"],
        entities=["James", "bowling"],
        keywords=["bowling"],
        embedding=embedder.encode(["James likes bowling."])[0],
    )
    exact = MemoryHit(
        memory_id="mem_james",
        version_id="mem_james_v1",
        content=candidate.content,
        matched_paths=["exact"],
    )
    agent = ConstructionAgent(model, store, embedder)
    monkeypatch.setattr(agent, "_related_memories", lambda **_: [exact])
    model.set_script([model._make_resp(json.dumps({"decisions": [{
        "candidate_id": "cand_exact",
        "action": "SKIP",
        "relations": [{"type": "duplicate_of", "target_version_id": "mem_james_v1"}],
    }]}))])

    plan = agent.build_plan(0, "conv", [candidate], [])

    assert plan.decisions[0].action == "SKIP"
    assert plan.decisions[0].relations[0].relation_type == "duplicate_of"
    assert model.calls == 1


def test_construction_plan_does_not_mutate_similar_existing_memory(
    tmp_path: Path,
    monkeypatch,
):
    store, embedder, _ = _components(tmp_path)
    model = _CountingMock()
    candidates = [
        MemoryCandidate(
            candidate_id=f"cand_{index}",
            memory_kind="event",
            subject="James",
            predicate="gaming",
            object_text=None,
            content=content,
            world_start=None,
            world_end=None,
            source_message_ids=[f"conv:D1:{index}"],
            entities=["James", "gaming"],
            keywords=["gaming"],
            embedding=embedder.encode([content])[0],
        )
        for index, content in enumerate(
            [
                "James entered an online tournament.",
                "James bought a new gaming system.",
            ],
            1,
        )
    ]
    related = MemoryHit(
        memory_id="mem_james",
        version_id="mem_james_v1",
        subject="James",
        predicate="gaming",
        memory_kind="event",
        content="James enjoys video games.",
        entities=["James", "gaming"],
        score=0.95,
        matched_paths=["semantic"],
    )
    agent = ConstructionAgent(model, store, embedder)
    monkeypatch.setattr(
        agent,
        "_related_memories",
        lambda **_: [related],
    )
    model.set_script([model._make_resp(json.dumps({"decisions": [
        {"candidate_id": "cand_1", "action": "ADD", "relations": [
            {"type": "unrelated", "target_version_id": "mem_james_v1"}
        ]},
        {"candidate_id": "cand_2", "action": "ADD", "relations": [
            {"type": "unrelated", "target_version_id": "mem_james_v1"}
        ]},
    ]}))])

    plan = agent.build_plan(
        base_commit_id=0,
        conversation_id="conv",
        candidates=candidates,
        skills=[],
    )

    assert [decision.action for decision in plan.decisions] == ["ADD", "ADD"]
    assert all(decision.target_memory_id is None for decision in plan.decisions)
    assert model.calls == 1
