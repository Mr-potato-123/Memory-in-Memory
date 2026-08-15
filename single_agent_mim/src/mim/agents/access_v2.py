"""Fixed-topology Access: initial retrieval, A1 plan, one retrieval round, A2 answer."""

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
_TIME_MODES = {"none", "current", "point", "before", "after", "range"}


class StableAccessAgent:
    """Two model decisions with no agent loop and no standalone reranker."""

    def __init__(
        self,
        model: ModelClient,
        store: SQLiteMemoryStore,
        retriever: HybridRetriever,
        *,
        planning_prompt: str,
        answer_prompt: str,
        initial_top_k: int = 16,
        supplemental_top_k: int = 12,
        context_top_k: int = 32,
        max_additional_queries: int = 3,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._model = model
        self._store = store
        self._retriever = retriever
        self._planning_prompt = planning_prompt
        self._answer_prompt = answer_prompt
        self._initial_top_k = max(1, initial_top_k)
        self._supplemental_top_k = max(1, supplemental_top_k)
        self._context_top_k = max(self._initial_top_k, context_top_k)
        self._max_additional_queries = max(0, max_additional_queries)
        self._event_sink = event_sink

    @staticmethod
    def build_query_profile(question: str) -> dict[str, Any]:
        """Stable lexical anchors for the mandatory first retrieval."""
        words = [
            token for token in _TOKENS.findall(question)
            if token.casefold() not in _STOP
        ]
        return {
            "original_query": question.strip(),
            "keywords": list(dict.fromkeys(words))[:12],
            "entities": [
                value for value in dict.fromkeys(_CAPITALIZED.findall(question))
                if value.casefold() not in _STOP
            ][:8],
        }

    # Kept as an alias for old diagnostics/tests.
    build_query_plan = build_query_profile

    def answer(
        self,
        question: Question,
        conversation_id: str,
        snapshot_commit_id: int,
        skills: list[SkillRecord],
        access_run_id: str | None = None,
        recovery_skill_loader: Callable[[dict[str, Any]], list[SkillRecord]] | None = None,
    ) -> AccessResult:
        access_run_id = access_run_id or f"access_v3_{uuid.uuid4().hex[:12]}"
        started = time.time()
        profile = self.build_query_profile(question.question)
        initial = self._search(
            conversation_id, snapshot_commit_id, profile["original_query"],
            profile["keywords"], profile["entities"], self._initial_top_k,
        )
        first_observation = {
            "query_profile": profile,
            "hit_count": len(initial),
            "hits": [self._compact_hit(hit) for hit in initial],
        }

        selected_skills = list(skills)
        if recovery_skill_loader is not None:
            try:
                selected_skills.extend(recovery_skill_loader({
                    "question": question.question,
                    "query_profile": profile,
                    "first_search": first_observation,
                }))
            except Exception as exc:
                self._emit("access_skill_error", qa_id=question.qa_id, error=str(exc))
        selected_skills = list({skill.skill_id: skill for skill in selected_skills}.values())
        selected_skills = self._usable_skills(selected_skills)

        plan, planning_response, planning_error, a1_skill_ids = self._plan(
            question, profile, initial, selected_skills
        )
        if plan["include_sources"] and not plan["additional_queries"]:
            # Reusing the original wording is useful here because the route is
            # different: the mandatory search covered atomic memories, while
            # this request explicitly inspects C1-omitted source messages.
            plan["additional_queries"] = [profile["original_query"]]
        supplemental: list[MemoryHit] = []
        retrieval_keywords = list(dict.fromkeys([
            *profile["keywords"], *plan["keywords"],
        ]))
        retrieval_entities = list(dict.fromkeys([
            *profile["entities"], *plan["entities"],
        ]))
        for query in plan["additional_queries"]:
            supplemental.extend(self._search(
                conversation_id,
                snapshot_commit_id,
                query,
                retrieval_keywords,
                retrieval_entities,
                self._supplemental_top_k,
                include_history=plan["include_history"],
                time_mode=plan["time_mode"],
                target_time=plan["target_time"],
                target_time_end=plan["target_time_end"],
                include_sources=plan["include_sources"],
            ))
        context = self._merge_context(initial, supplemental)
        answer, evidence_ids, answer_response, answer_error, a2_skill_ids = self._answer(
            question, profile, plan, context, selected_skills
        )
        applied_skill_ids = list(dict.fromkeys([*a1_skill_ids, *a2_skill_ids]))
        errors = [error for error in (planning_error, answer_error) if error]
        token_total = sum(
            (response.prompt_tokens or 0) + (response.completion_tokens or 0)
            for response in (planning_response, answer_response) if response is not None
        )

        initial_action = AgentAction(
            action="initial_search",
            arguments={"query": profile["original_query"], "top_k": self._initial_top_k},
            reason="Mandatory retrieval from the unmodified question.",
        )
        supplemental_action = AgentAction(
            action="supplemental_search",
            arguments=plan,
            reason="One bounded retrieval round from the A1 plan.",
        )
        answer_action = AgentAction(
            action="select_evidence_and_answer",
            arguments={"answer": answer, "evidence_version_ids": evidence_ids},
            reason="A2 jointly selected evidence, composed it, and answered.",
        )
        visible = [
            {
                **self._compact_hit(hit),
                "context_index": index,
                "rendered_text": f"[{hit.version_id}] {hit.memory_kind} | {hit.content}",
            }
            for index, hit in enumerate(context)
        ]
        actions = [
            self._action_record(access_run_id, 0, initial_action, initial),
            self._action_record(access_run_id, 1, supplemental_action, supplemental),
            {
                "action_id": f"{access_run_id}_a002", "step_index": 2,
                "action_type": "select_evidence_and_answer",
                "request": answer_action.model_dump(mode="json"),
                "response": {
                    "status": "accepted" if not answer_error else "error",
                    "answer": answer, "evidence_version_ids": evidence_ids,
                },
                "retrieval_hits": [],
            },
        ]
        prompt_hash = hashlib.sha256(json.dumps({
            "question": question.question,
            "profile": profile,
            "plan": plan,
            "context": [hit.version_id for hit in context],
            "skills": applied_skill_ids,
        }, sort_keys=True).encode()).hexdigest()
        return AccessResult(
            answer=answer,
            evidence_ids=evidence_ids,
            search_trace=[initial_action, supplemental_action, answer_action],
            used_skill_ids=applied_skill_ids,
            access_run_id=access_run_id,
            answer_prompt_hash=prompt_hash,
            visible_memories=visible,
            action_records=actions,
            total_tokens=token_total,
            latency_ms=int((time.time() - started) * 1000),
            error="; ".join(errors) or None,
            steps=2,
        )

    def _search(
        self, conversation_id: str, commit_id: int, query: str,
        keywords: list[str], entities: list[str], top_k: int, *,
        include_history: bool = False, time_mode: str = "none",
        target_time: str | None = None, target_time_end: str | None = None,
        include_sources: bool = False,
    ) -> list[MemoryHit]:
        return self._retriever.search(
            conversation_id=conversation_id,
            snapshot_commit_id=commit_id,
            query=query,
            strategy="hybrid",
            filters=SearchFilters(
                conversation_id=conversation_id,
                as_of_commit=commit_id,
                entities=entities,
                include_history=include_history,
                time_mode=time_mode,
                target_time=target_time,
                target_time_end=target_time_end,
                include_sources=include_sources,
            ),
            top_k=top_k,
            keywords=keywords,
            query_expansions=[],
            depth="standard",
        )

    def _plan(self, question, profile, initial, skills):
        response = self._model.generate(
            [
                {"role": "system", "content": self._planning_prompt},
                {"role": "user", "content": json.dumps({
                    "question": question.question,
                    "query_profile": profile,
                    "initial_memories": [self._compact_hit(hit) for hit in initial],
                    "access_skills": self._skill_payload(skills),
                    "limits": {"max_additional_queries": self._max_additional_queries},
                }, ensure_ascii=False)},
            ],
            temperature=0.0, max_tokens=1600, json_mode=True,
        )
        error = None
        try:
            raw = self._parse_json(response.text)
            plan = self._normalize_plan(raw)
            applied = self._applied_skill_ids(raw, skills)
        except (TypeError, ValueError) as exc:
            plan = self._normalize_plan({})
            applied = []
            error = f"Access A1 protocol error: {exc}"
        return plan, response, error, applied

    def _normalize_plan(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise TypeError("plan must be an object")
        queries = self._strings(raw.get("additional_queries"))[:self._max_additional_queries]
        time_mode = str(raw.get("time_mode") or "none").lower()
        return {
            "additional_queries": queries,
            "keywords": self._strings(raw.get("keywords"))[:12],
            "entities": self._strings(raw.get("entities"))[:8],
            "include_history": bool(raw.get("include_history", False)),
            "include_sources": bool(raw.get("include_sources", False)),
            "time_mode": time_mode if time_mode in _TIME_MODES else "none",
            "target_time": self._optional(raw.get("target_time")),
            "target_time_end": self._optional(raw.get("target_time_end")),
            "evidence_requirements": self._strings(raw.get("evidence_requirements"))[:6],
        }

    def _answer(self, question, profile, plan, context, skills):
        response = self._model.generate(
            [
                {"role": "system", "content": self._answer_prompt},
                {"role": "user", "content": json.dumps({
                    "question": question.question,
                    "query_profile": profile,
                    "retrieval_plan": plan,
                    "access_skills": self._skill_payload(skills),
                    "visible_memories": [self._compact_hit(hit) for hit in context],
                }, ensure_ascii=False)},
            ],
            temperature=0.0, max_tokens=1500, json_mode=True,
        )
        try:
            data = self._parse_json(response.text)
            answer = str(data.get("answer") or "").strip()
            allowed = {hit.version_id for hit in context}
            evidence_ids = [
                value for value in self._strings(data.get("selected_evidence_ids"))
                if value in allowed
            ]
            if not answer:
                raise ValueError("empty answer")
            applied = self._applied_skill_ids(data, skills)
            return answer, evidence_ids, response, None, applied
        except (TypeError, ValueError) as exc:
            return (
                "No information available.", [], response,
                f"Access A2 protocol error: {exc}", [],
            )

    def _merge_context(self, initial, supplemental):
        merged: dict[str, MemoryHit] = {}
        for hit in [*initial, *supplemental]:
            merged.setdefault(hit.version_id, hit)
        return list(merged.values())[:self._context_top_k]

    @staticmethod
    def _skill_payload(skills):
        return [
            {
                "skill_id": f"{skill.skill_id}_v{skill.version}",
                "name": skill.name,
                "when": skill.description,
                "guidance": skill.content,
            }
            for skill in skills
        ]

    @staticmethod
    def _usable_skills(skills):
        forbidden = re.compile(
            r"\bno information available\b|\babstain\b|"
            r"\b(?:return|respond|say|state)\s+(?:with\s+)?(?:exactly\s+)?"
            r"(?:the\s+)?(?:answer|response)\b|"
            r"\b(?:known|expected|reference|gold)\s+answer\b",
            re.IGNORECASE,
        )
        usable = []
        for skill in skills:
            items = [item for item in skill.content if not forbidden.search(item)]
            if items:
                usable.append(skill.model_copy(update={"content": items}))
        return usable

    @classmethod
    def _applied_skill_ids(cls, data, skills):
        allowed = {f"{skill.skill_id}_v{skill.version}" for skill in skills}
        return [
            value for value in cls._strings(data.get("applied_skill_ids"))
            if value in allowed
        ]

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise ValueError("response is not JSON")
            value = json.loads(match.group(0))
        if not isinstance(value, dict):
            raise TypeError("response must be an object")
        return value

    @staticmethod
    def _strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    @staticmethod
    def _optional(value: Any) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    @staticmethod
    def _compact_hit(hit: MemoryHit) -> dict[str, Any]:
        return {
            "version_id": hit.version_id, "memory_id": hit.memory_id,
            "content": hit.content, "memory_kind": hit.memory_kind,
            "subject": hit.subject, "predicate": hit.predicate,
            "object_text": hit.object_text, "world_start": hit.world_start,
            "world_end": hit.world_end, "entities": hit.entities,
            "score": round(float(hit.score), 6), "paths": list(hit.matched_paths),
        }

    def _action_record(self, run_id, index, action, hits):
        return {
            "action_id": f"{run_id}_a{index:03d}", "step_index": index,
            "action_type": action.action,
            "request": action.model_dump(mode="json"),
            "response": {"status": "ok", "hits": [self._compact_hit(hit) for hit in hits]},
            "retrieval_hits": [self._compact_hit(hit) for hit in hits],
        }

    def _emit(self, event: str, **payload: Any) -> None:
        if self._event_sink is not None:
            self._event_sink({"event": event, **payload})
