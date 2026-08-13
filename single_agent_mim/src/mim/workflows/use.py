"""Use Workflow — ingest conversations and answer questions (SQLite runtime).

Full pipeline:
  1. Save raw messages to SQLite
  2. Construction Agent Stage A → candidates → Stage B → plan → commit
  3. Access & Answer Agent → iterative retrieval → answer + evidence
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import MiMConfig
from ..schemas import (
    AccessResult,
    Conversation,
    Question,
    Session,
    Side,
)
from ..storage.sqlite_store import SQLiteMemoryStore
from ..retrieval.embedder import Embedder
from ..retrieval.hybrid import HybridRetriever
from ..skills import RuntimeSkillQueryBuilder, SkillBank
from ..llm import create_client
from ..llm.base import ModelClient
from ..agents.construction import ConstructionAgent
from ..agents.access_v2 import StableAccessAgent
from ..artifacts import RunDir
from ..tracing import TraceRecorder, ConstructionTrace, AccessTrace


class MiMRuntime:
    """SQLite-backed runtime for demo / interactive use.

    Usage:
        runtime = MiMRuntime(config, mode="mim", skill_bank=bank, run_dir=run_dir)
        runtime.ingest(conversation)
        result = runtime.ask(question)
    """

    def __init__(
        self,
        config: MiMConfig,
        mode: str = "mim",
        skill_bank: Optional[SkillBank] = None,
        run_dir: Optional[RunDir] = None,
        store: SQLiteMemoryStore | None = None,
        embedder: Embedder | None = None,
        runtime_model: ModelClient | None = None,
        phase: str = "use",
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        strict_construction: bool = False,
        persist_access: bool = True,
    ):
        self._cfg = config
        self._mode = mode
        self._run_dir = run_dir
        self._phase = phase
        self._event_sink = event_sink
        self._strict_construction = strict_construction
        self._persist_access = persist_access
        self._construction_errors: list[dict[str, Any]] = []

        # DB path
        configured_store_path = Path(config.storage.path)
        if configured_store_path.is_absolute() or ".." in configured_store_path.parts:
            raise ValueError(
                "storage.path must be a safe path relative to the run directory"
            )
        if config.storage.backend.lower() != "sqlite":
            raise ValueError("This MVP supports only storage.backend=sqlite")
        if run_dir:
            db_path = run_dir.path / configured_store_path
        else:
            import tempfile
            db_path = Path(tempfile.mkdtemp()) / configured_store_path

        # Embedder
        self._embedder = embedder or Embedder(
            model_name=config.embedding.model,
            device=config.embedding.device,
        )

        # Store
        self._store = store or SQLiteMemoryStore(
            db_path=db_path,
            embedding_dim=self._embedder.dim,
            embedding_model=self._embedder.model_name,
        )

        # Retriever
        self._retriever = HybridRetriever(
            store=self._store,
            embedder=self._embedder,
            semantic_candidate_k=config.retrieval.semantic_candidate_k,
            bm25_candidate_k=config.retrieval.bm25_candidate_k,
            keyword_candidate_k=config.retrieval.keyword_candidate_k,
            structured_candidate_k=config.retrieval.structured_candidate_k,
            result_top_k=config.retrieval.result_top_k,
            max_result_top_k=config.retrieval.max_result_top_k,
            max_query_expansions=config.retrieval.max_query_expansions,
            max_depth=config.retrieval.max_depth,
            rrf_k=config.retrieval.rrf_k,
            semantic_weight=config.retrieval.semantic_weight,
            bm25_weight=config.retrieval.bm25_weight,
            keyword_weight=config.retrieval.keyword_weight,
            structured_weight=config.retrieval.structured_weight,
            entity_match_multiplier=config.retrieval.entity_match_multiplier,
            time_valid_multiplier=config.retrieval.time_valid_multiplier,
            current_active_multiplier=config.retrieval.current_active_multiplier,
            temporal_mismatch_multiplier=(
                config.retrieval.temporal_mismatch_multiplier
            ),
            bm25_k1=config.retrieval.bm25_k1,
            bm25_b=config.retrieval.bm25_b,
        )

        # LLM clients
        runtime_cfg = config.models["runtime"]
        self._runtime_model = runtime_model or create_client(runtime_cfg)

        construction_extraction_prompt = _load_prompt(
            config.prompts.construction_extraction,
        )
        construction_decision_prompt = _load_prompt(
            config.prompts.construction_decision,
        )
        access_mode = config.access.mode.casefold()
        if access_mode != "plan_then_answer":
            raise ValueError(f"Unsupported access.mode: {config.access.mode}")
        access_plan_prompt = _load_prompt(config.prompts.access_plan)
        access_answer_prompt = _load_prompt(config.prompts.access_answer)

        # Construction Agent
        self._construction_agent = ConstructionAgent(
            model=self._runtime_model,
            store=self._store,
            embedder=self._embedder,
            extraction_prompt=construction_extraction_prompt,
            decision_prompt=construction_decision_prompt,
            max_candidates_per_session=getattr(config.construction, 'max_candidates_per_session', 30),
            related_memory_limit=getattr(config.construction, 'related_memory_limit', 10),
            max_related_pool=getattr(config.construction, 'max_related_pool', 24),
            max_decisions_per_call=getattr(
                config.construction, 'max_decisions_per_call', 10
            ),
        )

        # Access Agent
        self._access_agent = StableAccessAgent(
            model=self._runtime_model,
            store=self._store,
            retriever=self._retriever,
            planning_prompt=access_plan_prompt,
            answer_prompt=access_answer_prompt,
            initial_top_k=config.access.initial_top_k,
            supplemental_top_k=config.access.supplemental_top_k,
            context_top_k=config.access.context_top_k,
            max_additional_queries=config.access.max_additional_queries,
            event_sink=event_sink,
        )

        # Skill Bank
        self._skill_bank = skill_bank
        self._skill_query_builder = RuntimeSkillQueryBuilder()

        # Trace
        self._tracer: Optional[TraceRecorder] = None
        if run_dir:
            self._tracer = TraceRecorder(run_dir.path / "traces")

        # State
        self._conversation_id: str = ""
        self._latest_commit_id: int | None = None
        self._message_index: int = 0
        self._run_id: str = run_dir.run_id if run_dir else "use_demo"
        self._last_construction_steps: int = 0

    def ingest(
        self,
        conversation: Conversation,
        *,
        resume_existing: bool = False,
    ):
        """Build Memory session by session, optionally resuming commits."""
        self._conversation_id = conversation.conversation_id
        self._latest_commit_id = self._store.latest_commit_id(
            conversation.conversation_id
        )
        self._last_construction_steps = 0
        self._construction_errors = []
        self._emit(
            "ingestion_start",
            conversation_id=conversation.conversation_id,
            session_count=len(conversation.sessions),
        )

        # Ensure conversation exists in DB
        self._store.ensure_conversation(conversation.conversation_id, self._phase)

        # Pre compute prompt hash
        import hashlib
        prompt_hash = hashlib.sha256(
            (
                self._construction_agent._extraction_prompt
                + "\n--- C2 ---\n"
                + self._construction_agent._decision_prompt
            ).encode("utf-8")
        ).hexdigest()[:16]

        for s_idx, session in enumerate(conversation.sessions):
            if resume_existing:
                committed_id = self._store.committed_session_id(
                    conversation.conversation_id,
                    session.session_id,
                )
                if committed_id is not None:
                    self._latest_commit_id = max(
                        self._latest_commit_id or 0,
                        committed_id,
                    )
                    self._emit(
                        "construction_session_resumed",
                        conversation_id=conversation.conversation_id,
                        session_id=session.session_id,
                        session_index=s_idx,
                        commit_id=committed_id,
                    )
                    continue
            self._emit(
                "construction_session_start",
                conversation_id=conversation.conversation_id,
                session_id=session.session_id,
                session_index=s_idx,
                message_count=len(session.messages),
            )
            # Save raw messages
            self._store.save_session(
                session_id=session.session_id,
                conversation_id=conversation.conversation_id,
                session_index=s_idx,
                occurred_at=session.time,
            )

            msgs: list[dict] = []
            for m_idx, msg in enumerate(session.messages):
                msgs.append({
                    "message_id": msg.message_id,
                    "conversation_id": conversation.conversation_id,
                    "session_id": session.session_id,
                    "turn_index": m_idx,
                    "role": msg.role,
                    "speaker": msg.speaker or msg.role,
                    "content": msg.content,
                    "occurred_at": msg.time,
                })
            self._store.save_messages(msgs)

            # Retrieve Construction Skills
            skills: list = []
            construction_skill_trace = None
            if self._mode == "mim" and self._skill_bank:
                session_text = self._skill_query_builder.for_construction(
                    msgs
                )
                session_segments = (
                    self._skill_query_builder.for_construction_segments(msgs)
                )
                skills, construction_skill_trace = (
                    self._skill_bank.retrieve_with_trace(
                    query=session_text,
                    side=Side.CONSTRUCTION,
                    embedding_index=self._embedder,  # type: ignore[arg-type]
                    # Deterministic routing keeps fixed C1/C2 stages; C2 may
                    # use bounded batches for large candidate sets.
                    top_k=min(3, self._cfg.construction.skill_top_k),
                    candidate_k=self._cfg.construction.skill_candidate_k,
                    disclose_k=self._cfg.construction.skill_disclose_k,
                    min_score=self._cfg.construction.skill_min_score,
                    reranker=None,
                    query_segments=session_segments,
                    trace_id=(
                        f"skilltrace_construction_{self._run_id}_"
                        f"{conversation.conversation_id}_{session.session_id}"
                    ),
                    )
                )

            # Trace
            ct = ConstructionTrace(
                conversation_id=conversation.conversation_id,
                session_id=session.session_id,
                base_commit_id=self._latest_commit_id,
                skill_ids=[s.skill_id for s in skills],
                skill_trace=(
                    construction_skill_trace.model_dump(mode="json")
                    if construction_skill_trace
                    else {}
                ),
            )

            try:
                # Stage A: Extract candidates
                candidates = self._construction_agent.extract_candidates(
                    session_id=session.session_id,
                    conversation_id=conversation.conversation_id,
                    session_messages=msgs,
                    session_time=session.time,
                    skills=skills,
                    base_commit_id=self._latest_commit_id,
                )
                self._last_construction_steps += 1
                applied_construction_skill_ids = (
                    self._construction_agent.applied_skill_version_ids
                )
                ct.skill_ids = applied_construction_skill_ids
                ct.candidates_count = len(candidates)
                self._emit(
                    "construction_candidates",
                    conversation_id=conversation.conversation_id,
                    session_id=session.session_id,
                    candidate_count=len(candidates),
                )

                # C2 is the fixed second stage. It judges ADD/SKIP and
                # append-only relations in bounded batches; Skills never
                # mutate storage directly.
                plan = self._construction_agent.build_plan(
                    base_commit_id=self._latest_commit_id,
                    conversation_id=conversation.conversation_id,
                    candidates=candidates,
                    skills=skills,
                )
                self._last_construction_steps += (
                    self._construction_agent.last_decision_call_count
                )
                # C2 may apply a routed Skill that C1 did not use. Refresh
                # after both stages so provenance and later attribution see
                # the complete, truthful set.
                applied_construction_skill_ids = (
                    self._construction_agent.applied_skill_version_ids
                )
                ct.skill_ids = applied_construction_skill_ids
                ct.decisions = [
                    {"candidate_id": d.candidate_id, "action": d.action,
                     "target_memory_id": d.target_memory_id, "update_type": d.update_type,
                     "reason": d.reason,
                     "relations": [
                         {"type": relation.relation_type,
                          "target_version_id": relation.target_version_id}
                         for relation in d.relations
                     ]}
                    for d in plan.decisions
                ]

                # Apply plan
                commit = self._store.apply_construction_plan(
                    conversation_id=conversation.conversation_id,
                    session_id=session.session_id,
                    base_commit_id=self._latest_commit_id,
                    plan=plan,
                    run_id=self._run_id,
                    runtime_model=self._cfg.models["runtime"].model,
                    prompt_hash=prompt_hash,
                    skill_version_ids=applied_construction_skill_ids,
                    skill_trace=(
                        construction_skill_trace.model_dump(mode="json")
                        if construction_skill_trace
                        else {}
                    ),
                    input_message_ids=[m["message_id"] for m in msgs],
                )
                self._latest_commit_id = commit.commit_id
                ct.commit_id = commit.commit_id
                ct.commit_status = "committed"
                self._emit(
                    "construction_commit",
                    conversation_id=conversation.conversation_id,
                    session_id=session.session_id,
                    commit_id=commit.commit_id,
                    decision_count=len(plan.decisions),
                )

            except Exception as exc:
                ct.commit_status = "failed"
                ct.error_message = str(exc)
                failure = {
                    "conversation_id": conversation.conversation_id,
                    "session_id": session.session_id,
                    "session_index": s_idx,
                    "error": str(exc),
                }
                self._construction_errors.append(failure)
                self._emit("construction_session_error", **failure)

            if self._tracer:
                self._tracer.record_construction(ct)
            if ct.commit_status == "failed" and self._strict_construction:
                raise RuntimeError(
                    f"Construction failed for {session.session_id}: {ct.error_message}"
                )

        # Save final snapshot info
        if self._run_dir:
            final = self._store.load_snapshot(self._conversation_id, self._latest_commit_id)
            self._run_dir.write_json(
                f"memory/{self._conversation_id}/final.json",
                {
                    "conversation_id": self._conversation_id,
                    "latest_commit_id": self._latest_commit_id,
                    "active_memories": [
                        {"version_id": h.version_id, "memory_id": h.memory_id,
                         "content": h.content, "memory_kind": h.memory_kind}
                        for h in final
                    ],
                },
            )
        self._emit(
            "ingestion_complete",
            conversation_id=conversation.conversation_id,
            latest_commit_id=self._latest_commit_id,
            construction_errors=len(self._construction_errors),
        )

    def ask(self, question: Question) -> AccessResult:
        """Answer a question using the built Memory."""
        if not self._conversation_id:
            raise RuntimeError("No conversation ingested. Call ingest() first.")

        # Access Skills are deliberately not retrieved before the first
        # search.  AccessAgent runs one default-policy retrieval, then invokes
        # this loader with the observed evidence state.
        skills: list = []
        access_skill_trace = None
        recovery_skills: list = []

        def load_recovery_skills(context: dict) -> list:
            nonlocal access_skill_trace, recovery_skills
            if self._mode != "mim" or not self._skill_bank:
                return []
            query = self._skill_query_builder.for_access_recovery(context)
            selected, access_skill_trace = self._skill_bank.retrieve_with_trace(
                query=query,
                side=Side.ACCESS,
                embedding_index=self._embedder,  # type: ignore[arg-type]
                # Retrieve at most three candidate Skills after the mandatory
                # first search. A1/A2 decide whether each is applicable.
                top_k=min(3, self._cfg.access.skill_top_k),
                candidate_k=self._cfg.access.skill_candidate_k,
                disclose_k=self._cfg.access.skill_disclose_k,
                min_score=self._cfg.access.skill_min_score,
                # Fixed-topology Access uses deterministic Skill routing; A1
                # decides applicability inside its one planning call.
                reranker=None,
                trace_id=(
                    f"skilltrace_access_recovery_{self._run_id}_"
                    f"{self._conversation_id}_{question.qa_id}"
                ),
            )
            recovery_skills = selected
            return selected

        at = AccessTrace(
            conversation_id=self._conversation_id,
            qa_id=question.qa_id,
            snapshot_commit_id=self._latest_commit_id or 0,
            question=question.question,
            skill_ids=[s.skill_id for s in skills],
            skill_trace=(
                access_skill_trace.model_dump(mode="json")
                if access_skill_trace
                else {}
            ),
            reference=question.reference_answer,
        )

        result = self._access_agent.answer(
            question=question,
            conversation_id=self._conversation_id,
            snapshot_commit_id=self._latest_commit_id or 0,
            skills=skills,
            access_run_id=(
                f"access_{self._run_id}_{self._conversation_id}_{question.qa_id}"
            ),
            recovery_skill_loader=load_recovery_skills,
        )
        result.skill_trace = access_skill_trace
        at.skill_ids = [skill.skill_id for skill in recovery_skills]
        at.skill_trace = (
            access_skill_trace.model_dump(mode="json")
            if access_skill_trace
            else {}
        )

        gold_message_ids = [
            evidence[-1]
            for evidence in question.source_evidence
            if evidence and evidence[-1]
        ]
        existing_gold_ids = {
            item["message_id"]
            for item in self._store.get_source_messages(
                self._conversation_id,
                gold_message_ids,
            )
        }
        missing_gold_ids = [
            message_id
            for message_id in gold_message_ids
            if message_id not in existing_gold_ids
        ]
        if missing_gold_ids:
            self._emit(
                "qa_gold_source_missing",
                conversation_id=self._conversation_id,
                qa_id=question.qa_id,
                missing_message_ids=missing_gold_ids,
            )
        gold_message_ids = [
            message_id
            for message_id in gold_message_ids
            if message_id in existing_gold_ids
        ]
        if self._persist_access:
            self._store.save_qa_case(
                qa_id=question.qa_id,
                conversation_id=self._conversation_id,
                question=question.question,
                reference_answer=question.reference_answer,
                category=question.category,
                gold_message_ids=gold_message_ids,
            )
            self._store.save_access_trace(
                access_run_id=result.access_run_id,
                run_id=self._run_id,
                conversation_id=self._conversation_id,
                qa_id=question.qa_id,
                snapshot_commit_id=self._latest_commit_id or 0,
                question=question.question,
                prediction=result.answer,
                skill_version_ids=result.used_skill_ids,
                skill_trace=(
                    access_skill_trace.model_dump(mode="json")
                    if access_skill_trace
                    else {}
                ),
                answer_prompt_hash=result.answer_prompt_hash,
                action_records=result.action_records,
                visible_memories=result.visible_memories,
                evidence_ids=result.evidence_ids,
            )

        at.actions = [a.model_dump(mode="json") for a in result.search_trace]
        at.final_evidence_ids = result.evidence_ids
        at.visible_evidence_ids = [
            memory["version_id"] for memory in result.visible_memories
        ]
        at.answer = result.answer
        at.token_usage = {"total": result.total_tokens}
        at.latency_ms = result.latency_ms
        at.error = result.error or ""

        if self._tracer:
            self._tracer.record_access(at)

        return result

    def attach(self, conversation_id: str) -> None:
        """Attach to an already constructed conversation without re-ingesting."""
        latest_commit_id = self._store.latest_commit_id(conversation_id)
        if latest_commit_id is None:
            raise RuntimeError(
                f"No committed memory found for conversation {conversation_id}"
            )
        self._conversation_id = conversation_id
        self._latest_commit_id = latest_commit_id
        self._construction_errors = []
        self._emit(
            "memory_attached",
            conversation_id=conversation_id,
            latest_commit_id=latest_commit_id,
        )

    @property
    def latest_commit_id(self) -> int | None:
        return self._latest_commit_id

    @property
    def store(self) -> SQLiteMemoryStore:
        return self._store

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def last_construction_steps(self) -> int:
        """Runtime-model calls used by the most recent ingest."""
        return self._last_construction_steps

    @property
    def construction_errors(self) -> list[dict[str, Any]]:
        return list(self._construction_errors)

    def _emit(self, event: str, **payload: Any) -> None:
        if self._event_sink is not None:
            self._event_sink({"event": event, **payload})


def _load_prompt(path: str) -> str:
    prompt_path = Path(path)
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file does not exist: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")
