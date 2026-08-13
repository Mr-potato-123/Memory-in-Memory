"""Stable retrieve-rerank-answer access path.

Unlike the legacy agent loop, this path has a deterministic retrieval plan and
exactly two model decisions: evidence reranking and final answering.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any, Callable

from ..llm.base import ModelClient
from ..retrieval.hybrid import HybridRetriever
from ..schemas import AccessResult, AgentAction, Question, SkillRecord
from ..storage.sqlite_store import MemoryHit, SearchFilters, SQLiteMemoryStore


_TOKENS = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")
_CAPITALIZED = re.compile(r"\b[A-Z][A-Za-z'-]+\b")
_STOP = {
    "a", "an", "and", "are", "at", "be", "did", "do", "does", "for",
    "from", "had", "has", "have", "how", "in", "is", "it", "of", "on",
    "or", "the", "their", "they", "to", "was", "were", "what", "when",
    "where", "which", "who", "why", "with", "would",
}


class StableAccessAgent:
    """Deterministic retrieval followed by one rerank and one answer call."""

    def __init__(
        self,
        model: ModelClient,
        store: SQLiteMemoryStore,
        retriever: HybridRetriever,
        *,
        prompt_template: str,
        candidate_top_k: int = 60,
        evidence_top_k: int = 16,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._model = model
        self._store = store
        self._retriever = retriever
        self._prompt = prompt_template
        self._candidate_top_k = max(1, candidate_top_k)
        self._evidence_top_k = max(1, min(evidence_top_k, candidate_top_k))
        self._event_sink = event_sink

    @staticmethod
    def build_query_plan(question: str) -> dict[str, Any]:
        """Build the same plan for the same question without an LLM call."""
        words = [token for token in _TOKENS.findall(question) if token.casefold() not in _STOP]
        keywords = list(dict.fromkeys(words))[:12]
        entities = [
            value for value in dict.fromkeys(_CAPITALIZED.findall(question))
            if value.casefold() not in _STOP
        ][:8]
        lowered = question.casefold()
        if lowered.startswith("when"):
            answer_type = "date"
        elif lowered.startswith("who"):
            answer_type = "person"
        elif lowered.startswith("where"):
            answer_type = "place"
        elif "how many" in lowered:
            answer_type = "count"
        elif lowered.startswith(("why", "how")):
            answer_type = "explanation"
        else:
            answer_type = "fact"
        return {
            "query": question.strip(),
            "keywords": keywords,
            "entities": entities,
            "answer_type": answer_type,
            "requires_broad_coverage": answer_type in {"count", "explanation"}
                or any(term in lowered for term in ("all ", "things", "names", "kinds")),
        }

    def answer(
        self,
        question: Question,
        conversation_id: str,
        snapshot_commit_id: int,
        skills: list[SkillRecord],
        access_run_id: str | None = None,
        recovery_skill_loader: Callable[[dict[str, Any]], list[SkillRecord]] | None = None,
    ) -> AccessResult:
        access_run_id = access_run_id or f"access_v2_{uuid.uuid4().hex[:12]}"
        started = time.time()
        plan = self.build_query_plan(question.question)
        filters = SearchFilters(
            conversation_id=conversation_id,
            as_of_commit=snapshot_commit_id,
            entities=plan["entities"],
            include_history=False,
        )
        candidates = self._retriever.search(
            conversation_id=conversation_id,
            snapshot_commit_id=snapshot_commit_id,
            query=plan["query"],
            strategy="hybrid",
            filters=filters,
            top_k=self._candidate_top_k,
            keywords=plan["keywords"],
            query_expansions=[],
            depth="deep",
        )
        first_observation = {
            "strategy": "hybrid",
            "query": plan["query"],
            "keywords": plan["keywords"],
            "hit_count": len(candidates),
            "hits": [self._compact_hit(hit) for hit in candidates[:8]],
        }

        selected_skills = list(skills)
        if recovery_skill_loader is not None:
            try:
                selected_skills.extend(recovery_skill_loader({
                    "question": question.question,
                    "query_plan": plan,
                    "first_search": first_observation,
                }))
            except Exception as exc:
                self._emit("access_v2_skill_error", qa_id=question.qa_id, error=str(exc))
        selected_skills = list({skill.skill_id: skill for skill in selected_skills}.values())
        # A routed legacy Skill is not "used" when all of its content is an
        # answer/abstention command rejected by the Access V2 adapter.
        selected_skills = [
            skill for skill in selected_skills if self._retrieval_guidance(skill)
        ]

        ranked, rerank_response = self._rerank(question, plan, candidates, selected_skills)
        answer, evidence_ids, answer_response, error = self._answer(question, plan, ranked)
        token_total = sum(
            (response.prompt_tokens or 0) + (response.completion_tokens or 0)
            for response in (rerank_response, answer_response)
            if response is not None
        )
        latency_ms = int((time.time() - started) * 1000)
        search_action = AgentAction(
            action="search_memory",
            arguments={
                "strategy": "hybrid", "query": plan["query"],
                "keywords": plan["keywords"], "entities": plan["entities"],
                "depth": "deep", "top_k": self._candidate_top_k,
            },
            reason="Deterministic Access V2 candidate retrieval.",
        )
        answer_action = AgentAction(
            action="answer",
            arguments={"answer": answer, "evidence_version_ids": evidence_ids},
            reason="Single grounded answer over reranked evidence.",
        )
        visible = [
            {
                **self._compact_hit(hit),
                "context_index": index,
                "rendered_text": f"[{hit.version_id}] {hit.memory_kind} | {hit.content}",
            }
            for index, hit in enumerate(ranked)
        ]
        actions = [
            {
                "action_id": f"{access_run_id}_a000", "step_index": 0,
                "action_type": "search_memory",
                "request": search_action.model_dump(mode="json"),
                "response": {"status": "ok", "query_plan": plan,
                             "hits": [self._compact_hit(hit) for hit in candidates]},
                "retrieval_hits": [self._compact_hit(hit) for hit in candidates],
            },
            {
                "action_id": f"{access_run_id}_a001", "step_index": 1,
                "action_type": "answer",
                "request": answer_action.model_dump(mode="json"),
                "response": {"status": "accepted" if not error else "error",
                             "answer": answer, "evidence_version_ids": evidence_ids},
                "retrieval_hits": [],
            },
        ]
        prompt_hash = hashlib.sha256(
            json.dumps({"question": question.question, "plan": plan,
                        "evidence": [hit.version_id for hit in ranked]},
                       sort_keys=True).encode("utf-8")
        ).hexdigest()
        return AccessResult(
            answer=answer,
            evidence_ids=evidence_ids,
            search_trace=[search_action, answer_action],
            used_skill_ids=[f"{skill.skill_id}_v{skill.version}" for skill in selected_skills],
            access_run_id=access_run_id,
            answer_prompt_hash=prompt_hash,
            visible_memories=visible,
            action_records=actions,
            total_tokens=token_total,
            latency_ms=latency_ms,
            error=error,
            steps=2,
        )

    def _rerank(self, question, plan, candidates, skills):
        if not candidates:
            return [], None
        skill_policy = [
            {"name": skill.name, "when": skill.description,
             "retrieval_guidance": self._retrieval_guidance(skill)}
            for skill in skills
            if self._retrieval_guidance(skill)
        ]
        payload = {
            "question": question.question,
            "query_plan": plan,
            "retrieval_skills": skill_policy,
            "candidates": [self._compact_hit(hit) for hit in candidates],
        }
        prompt = (
            "Rerank candidate memories for answering the question. Skills are optional "
            "retrieval hints only: they may affect evidence ranking, but must never force "
            "an answer or abstention. Prefer direct subject, time, relation, and list-hop "
            "coverage. Return JSON {\"ranked_version_ids\":[...]} with at most "
            f"{self._evidence_top_k} unique IDs from candidates."
        )
        response = self._model.generate(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            temperature=0.0, max_tokens=1200, json_mode=True,
        )
        requested: list[str] = []
        try:
            data = json.loads(response.text)
            values = data.get("ranked_version_ids", [])
            if isinstance(values, list):
                requested = [str(value) for value in values]
        except (json.JSONDecodeError, TypeError, ValueError):
            requested = []
        by_id = {hit.version_id: hit for hit in candidates}
        ordered = [by_id[value] for value in requested if value in by_id]
        seen = {hit.version_id for hit in ordered}
        ordered.extend(hit for hit in candidates if hit.version_id not in seen)
        return ordered[:self._evidence_top_k], response

    @staticmethod
    def _retrieval_guidance(skill: SkillRecord) -> list[str]:
        """Drop legacy answer/abstention commands at the adapter boundary."""
        allowed = re.compile(
            r"\b(search|re-search|retriev|query|keyword|rank|filter|evidence|"
            r"subject|time|date|memory kind|candidate)\b",
            re.IGNORECASE,
        )
        forbidden = re.compile(
            r"\b(no information available|abstain|answer|respond|state that|"
            r"say that|infer|guess)\b",
            re.IGNORECASE,
        )
        return [
            instruction
            for instruction in skill.content
            if allowed.search(instruction) and not forbidden.search(instruction)
        ]

    def _answer(self, question, plan, evidence):
        evidence_payload = [self._compact_hit(hit) for hit in evidence]
        response = self._model.generate(
            [
                {"role": "system", "content": self._prompt},
                {"role": "user", "content": json.dumps({
                    "question": question.question,
                    "query_plan": plan,
                    "evidence": evidence_payload,
                }, ensure_ascii=False)},
            ],
            temperature=0.0, max_tokens=1200, json_mode=True,
        )
        try:
            data = json.loads(response.text)
            answer = str(data.get("answer") or "").strip()
            requested = data.get("evidence_version_ids", [])
            allowed = {hit.version_id for hit in evidence}
            evidence_ids = [str(value) for value in requested if str(value) in allowed]
            if not answer:
                raise ValueError("empty answer")
            return answer, evidence_ids, response, None
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return "No information available.", [], response, f"Access V2 answer protocol error: {exc}"

    @staticmethod
    def _compact_hit(hit: MemoryHit) -> dict[str, Any]:
        return {
            "version_id": hit.version_id,
            "memory_id": hit.memory_id,
            "content": hit.content,
            "memory_kind": hit.memory_kind,
            "subject": hit.subject,
            "world_start": hit.world_start,
            "world_end": hit.world_end,
            "entities": hit.entities,
            "score": round(float(hit.score), 6),
            "paths": list(hit.matched_paths),
        }

    def _emit(self, event: str, **payload: Any) -> None:
        if self._event_sink is not None:
            self._event_sink({"event": event, **payload})
