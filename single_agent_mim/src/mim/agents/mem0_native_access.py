"""Mem0-native single-pass retrieval and answer baseline."""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Callable

from ..llm.base import ModelClient
from ..schemas import AccessResult, AgentAction, Question, Side, SkillRecord
from ..storage.sqlite_store import SearchFilters


MEM0_ANSWER_PROMPT = """
You are an expert at answering questions based on the provided memories. Your
task is to provide accurate and concise answers to the questions by leveraging
the information given in the memories.

Guidelines:
- Extract relevant information from the memories based on the question.
- If no relevant information is found, do not say no information is found.
  Instead, accept the question and provide a general response.
- Ensure that answers are clear, concise, and directly address the question.
""".strip()


class Mem0NativeAccessAgent:
    """One original-query search followed by one answer call."""

    def __init__(
        self,
        model: ModelClient,
        retriever: Any,
        *,
        top_k: int = 20,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._model = model
        self._retriever = retriever
        self._top_k = max(1, int(top_k))
        self._event_sink = event_sink

    def answer(
        self,
        question: Question,
        conversation_id: str,
        snapshot_commit_id: int,
        skills: list,
        access_run_id: str | None = None,
        recovery_skill_loader=None,
    ) -> AccessResult:
        run_id = access_run_id or f"mem0_native_{uuid.uuid4().hex[:12]}"
        started = time.time()
        hits = self._retriever.search(
            conversation_id=conversation_id,
            snapshot_commit_id=snapshot_commit_id,
            query=question.question,
            strategy="hybrid",
            filters=SearchFilters(conversation_id=conversation_id),
            top_k=self._top_k,
            keywords=[],
            query_expansions=[],
            depth=1,
        )
        visible = [
            {
                "version_id": hit.version_id,
                "memory_id": hit.memory_id,
                "content": hit.content,
                "score": round(float(hit.score), 6),
                "context_index": index,
                "rendered_text": f"[{hit.version_id}] {hit.content}",
            }
            for index, hit in enumerate(hits)
        ]
        selected_skills = list(skills)
        if recovery_skill_loader is not None:
            try:
                selected_skills.extend(recovery_skill_loader({
                    "question": question.question,
                    "query_profile": {"original_query": question.question},
                    "first_search": {
                        "hit_count": len(hits),
                        "hits": visible,
                    },
                }))
            except Exception as exc:
                self._emit(
                    "access_skill_error",
                    qa_id=question.qa_id,
                    error=str(exc),
                )
        selected_skills = list({
            skill.skill_id: skill
            for skill in selected_skills
            if skill.side == Side.ACCESS and skill.content
        }.values())
        used_skill_ids = [
            f"{skill.skill_id}_v{skill.version}" for skill in selected_skills
        ]
        system_prompt = MEM0_ANSWER_PROMPT
        if selected_skills:
            system_prompt += self._render_answer_skills(selected_skills)
        memories_text = "\n".join(hit.content for hit in hits)
        user_prompt = (
            f"- Relevant Memories/Facts:\n{memories_text}\n\n"
            f"- Entities: []\n\n- User Question: {question.question}"
        )
        response = self._model.generate(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=256,
            json_mode=False,
        )
        answer = response.text.strip()
        search_action = AgentAction(
            action="initial_search",
            arguments={"query": question.question, "top_k": self._top_k},
            reason="Mem0-native search over the unmodified question.",
        )
        answer_action = AgentAction(
            action="answer",
            arguments={"answer": answer, "skill_ids": used_skill_ids},
            reason=(
                "Mem0-native answer over the returned memories, with only "
                "applicable learned answer guidance injected."
            ),
        )
        action_records = [
            {
                "action_id": f"{run_id}_a000",
                "step_index": 0,
                "action_type": "initial_search",
                "request": search_action.model_dump(mode="json"),
                "response": {"status": "ok", "hits": visible},
                "retrieval_hits": visible,
            },
            {
                "action_id": f"{run_id}_a001",
                "step_index": 1,
                "action_type": "answer",
                "request": answer_action.model_dump(mode="json"),
                "response": {"status": "ok", "answer": answer},
                "retrieval_hits": [],
            },
        ]
        prompt_hash = hashlib.sha256(
            (system_prompt + "\n" + user_prompt).encode("utf-8")
        ).hexdigest()
        total_tokens = (response.prompt_tokens or 0) + (response.completion_tokens or 0)
        return AccessResult(
            answer=answer,
            evidence_ids=[],
            search_trace=[search_action, answer_action],
            used_skill_ids=used_skill_ids,
            access_run_id=run_id,
            answer_prompt_hash=prompt_hash,
            visible_memories=visible,
            action_records=action_records,
            total_tokens=total_tokens,
            latency_ms=int((time.time() - started) * 1000),
            error=None,
            steps=1,
        )

    @staticmethod
    def _render_answer_skills(skills: list[SkillRecord]) -> str:
        lines = [
            "\n\nLearned answer-side procedures (conditional):",
            "- The memory search is already complete. Do not request, simulate, "
            "or assume another retrieval.",
            "- Apply a procedure only when its full 'Use when' trigger is "
            "satisfied by the question and returned memories.",
            "- Procedures guide evidence interpretation and answer composition; "
            "they never override memory evidence or authorize guessing.",
        ]
        for skill in skills:
            lines.append(
                f"\n[{skill.skill_id}_v{skill.version}] {skill.name}"
            )
            lines.append(f"Use when: {skill.description}")
            lines.extend(f"- {instruction}" for instruction in skill.content)
        return "\n".join(lines)

    def _emit(self, event: str, **payload: Any) -> None:
        if self._event_sink is not None:
            self._event_sink({"event": event, **payload})
