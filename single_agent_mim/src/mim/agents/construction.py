"""Fixed-topology, model-driven, append-only memory construction.

C1 extracts evidence-bound facts. C2 compares each candidate with a bounded
old-memory pool and decides ADD/SKIP plus semantic relations. Storage remains
append-only: neither stage can overwrite or delete history.
"""

from __future__ import annotations

import json
import re
from typing import Callable
import numpy as np

from ..llm.base import ModelClient
from ..retrieval.embedder import Embedder
from ..schemas import SkillRecord
from ..storage.sqlite_store import (
    ConstructionDecision,
    ConstructionPlan,
    MemoryCandidate,
    MemoryHit,
    MemoryRelation,
    SQLiteMemoryStore,
)


ALLOWED_MEMORY_KINDS = {
    "profile",
    "preference",
    "state",
    "event",
    "plan",
    "relationship",
}
ALLOWED_ACTIONS = {"ADD", "SKIP"}
ALLOWED_RELATIONS = {
    "duplicate_of", "supports", "contradicts", "supersedes", "refines",
    "unrelated",
}


def _safe_format(template: str, **kwargs: str) -> str:
    """Replace only named runtime placeholders.

    Prompts contain JSON examples with braces, so ``str.format`` is unsafe.
    """
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", value)
    return result


EXTRACTION_SYSTEM = """\
You are a long-term Memory Extractor. Convert one conversation session into a
compact collection of rich, standalone memories.

Principles:
1. Preserve every durable personal fact, event, preference, relationship,
   plan, named entity, number, negation, and state transition.
2. A memory is a coherent topic or event, not an artificial one-triple atom.
   Combine tightly related details so the memory is useful on its own.
3. Use explicit speaker names and resolve pronouns.
4. Resolve relative dates from the supplied session/message time. Store the
   event time in world_start/world_end, not merely the observation time.
5. Stay evidence-bound. Do not add unsupported facts.
6. Exclude pure greetings, acknowledgements, and duplicate paraphrases.
7. Use only these memory kinds: profile, preference, state, event, plan,
   relationship.
8. Copy source_message_ids exactly from the input. A synthesized memory may
   cite multiple messages.

Return one JSON object with a candidates array and applied_skill_ids. Each candidate has:
memory_kind, subject, predicate (short optional retrieval label), object_text
(optional), content (1-3 complete sentences), world_start, world_end,
source_message_ids, entities, keywords, importance, confidence.
List a supplied Skill version ID only if its guidance materially changed C1.

Construction Skills:
{skills_section}

Session time: {session_time}

Messages:
{session_messages}
"""


BATCH_DECISION_SYSTEM = """\
You are the C2 change-linking stage of an append-only long-term memory system.
For every new candidate, compare only with its bounded related old memories.

Actions:
- ADD: append the candidate as a new memory version.
- SKIP: do not append it, only when an old memory already expresses the same
  durable fact. A SKIP decision must include a duplicate_of relation.

Relations describe meaning; they never mutate old memories:
- duplicate_of: materially the same fact.
- supports: compatible evidence for the same proposition.
- contradicts: incompatible claims whose temporal ordering is insufficient.
- supersedes: a later state or corrected value replaces an earlier one.
- refines: a narrower or more detailed compatible fact.
- unrelated: retrieved context is not meaningfully related.

Rules:
1. Return exactly one decision for each candidate_id and no extra decisions.
2. Use only ADD or SKIP. Never rewrite, merge, update, delete, or target storage.
3. Relation targets must be version_ids listed in that candidate's
   allowed_related_version_ids. Do not invent IDs.
4. Prefer ADD plus supersedes/contradicts/refines for changed information, so
   history remains auditable. Use SKIP only for a true duplicate.
5. Similar topic, entity, or embedding score alone does not prove a relation.
6. Skills are optional process references. Report a Skill ID in
   applied_skill_ids only when it materially changed this judgment.
7. Return JSON only:
{"decisions":[{"candidate_id":"...","action":"ADD|SKIP",
"reason":"brief evidence-based reason","relations":[{"relation_type":
"duplicate_of|supports|contradicts|supersedes|refines|unrelated",
"target_version_id":"..."}]}],"applied_skill_ids":[]}

Construction Skills:
{skills_section}

New Candidates:
{candidates_json}

Relevant Existing Memory Pool:
{related_memories}
"""


class ConstructionAgent:
    """C1 extraction followed by C2 change/link judgment."""

    def __init__(
        self,
        model: ModelClient,
        store: SQLiteMemoryStore,
        embedder: Embedder,
        extraction_prompt: str = EXTRACTION_SYSTEM,
        decision_prompt: str = BATCH_DECISION_SYSTEM,
        max_candidates_per_session: int = 30,
        related_memory_limit: int = 10,
        max_related_pool: int = 24,
    ):
        self._model = model
        self._store = store
        self._embedder = embedder
        self._extraction_prompt = extraction_prompt
        self._decision_prompt = decision_prompt
        self._max_candidates = max_candidates_per_session
        self._related_limit = related_memory_limit
        self._max_related_pool = max_related_pool
        self._applied_skill_version_ids: list[str] = []

    def extract_candidates(
        self,
        session_id: str,
        conversation_id: str,
        session_messages: list[dict],
        session_time: str | None,
        skills: list[SkillRecord],
        base_commit_id: int | None = None,
    ) -> list[MemoryCandidate]:
        """C1: extract durable facts from only the current session evidence."""
        self._applied_skill_version_ids = []
        skills = self._usable_skills(skills)
        skill_text = self._render_skills(
            skills, "(No construction skills. Use the default extraction policy.)"
        )
        message_text = "\n".join(
            (
                f"[message_id={message.get('message_id', '')}] "
                f"[time={message.get('occurred_at') or session_time or 'unknown'}] "
                f"{message.get('speaker') or message.get('role', 'user')}: "
                f"{message.get('content', '')}"
            )
            for message in session_messages
        )
        prompt = _safe_format(
            self._extraction_prompt,
            skills_section=skill_text,
            session_time=session_time or "unknown",
            session_messages=message_text,
            existing_memories="[]",
        )
        data = self._generate_valid_json(
            [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": (
                        "Extract compact, context-rich memories from every "
                        "durable topic in this session. Return JSON only."
                    ),
                },
            ],
            max_tokens=8000,
            validate=lambda value: (
                None if isinstance(value.get("candidates"), list)
                else "candidates must be a JSON array"
            ),
            stage=f"Construction C1 for {session_id}",
        )
        self._applied_skill_version_ids = self._validated_applied_skills(data, skills)
        raw_candidates = data.get("candidates")
        assert isinstance(raw_candidates, list)

        allowed_sources = {
            str(message["message_id"])
            for message in session_messages
            if message.get("message_id")
        }
        candidates: list[MemoryCandidate] = []
        seen_content: set[str] = set()
        for raw in raw_candidates[: self._max_candidates]:
            if not isinstance(raw, dict):
                continue
            content = str(raw.get("content") or "").strip()
            sources = [
                source
                for source in dict.fromkeys(raw.get("source_message_ids") or [])
                if source in allowed_sources
            ]
            if not content or not sources:
                continue

            memory_kind = str(raw.get("memory_kind") or "event").lower()
            if memory_kind not in ALLOWED_MEMORY_KINDS:
                memory_kind = "event"
            subject = str(raw.get("subject") or "conversation participants").strip()
            candidate = MemoryCandidate(
                candidate_id=(
                    f"cand_{conversation_id}_{session_id}_{len(candidates):03d}"
                ),
                memory_kind=memory_kind,
                subject=subject,
                predicate=self._optional_text(raw.get("predicate")),
                object_text=self._optional_text(raw.get("object_text")),
                content=content,
                world_start=self._optional_text(raw.get("world_start")),
                world_end=self._optional_text(raw.get("world_end")),
                source_message_ids=sources,
                entities=self._clean_string_list(raw.get("entities")),
                keywords=self._clean_string_list(raw.get("keywords")),
                importance=self._bounded_float(raw.get("importance"), 0.5),
                confidence=self._bounded_float(raw.get("confidence"), 0.8),
            )
            content_hash = candidate.content_hash()
            if content_hash in seen_content:
                continue
            seen_content.add(content_hash)
            encode_documents = getattr(self._embedder, "encode_documents", None)
            candidate.embedding = (
                encode_documents([candidate.content])[0]
                if callable(encode_documents)
                else self._embedder.encode([candidate.content])[0]
            )
            candidates.append(candidate)

        if raw_candidates and not candidates:
            raise RuntimeError(
                f"All {len(raw_candidates)} candidates were rejected for {session_id}; "
                "check source_message_ids and content."
            )
        return candidates

    def build_plan(
        self,
        base_commit_id: int | None,
        conversation_id: str,
        candidates: list[MemoryCandidate],
        skills: list[SkillRecord],
    ) -> ConstructionPlan:
        """C2: judge duplication and semantic change against bounded old memory."""
        skills = self._usable_skills(skills)
        if not candidates:
            return ConstructionPlan(
                base_commit_id=base_commit_id, candidates=[], decisions=[]
            )

        related_by_candidate: dict[str, list[MemoryHit]] = {}
        pool: dict[str, MemoryHit] = {}
        for candidate in candidates:
            related = self._related_memories(
                conversation_id=conversation_id,
                base_commit_id=base_commit_id,
                candidate=candidate,
            )
            related_by_candidate[candidate.candidate_id] = related
            pool.update({hit.version_id: hit for hit in related})

        selected_pool = self._select_related_pool(
            candidates, related_by_candidate, pool
        )
        visible_version_ids = {hit.version_id for hit in selected_pool}
        visible_related_by_candidate = {
            candidate_id: [
                hit for hit in hits if hit.version_id in visible_version_ids
            ]
            for candidate_id, hits in related_by_candidate.items()
        }
        skill_text = self._render_skills(
            skills,
            "(No construction skills. Use the default change-linking policy.)",
        )
        prompt = _safe_format(
            self._decision_prompt,
            skills_section=skill_text,
            candidates_json=json.dumps(
                [
                    self._candidate_payload(
                        candidate,
                        visible_related_by_candidate[candidate.candidate_id],
                    )
                    for candidate in candidates
                ],
                ensure_ascii=False,
                indent=2,
            ),
            related_memories=self._render_related_pool(
                selected_pool,
                visible_related_by_candidate,
            ),
        )
        expected_ids = {candidate.candidate_id for candidate in candidates}

        def validate_c2(value: dict) -> str | None:
            items = value.get("decisions")
            if not isinstance(items, list):
                return "decisions must be a JSON array"
            ids = [
                str(item.get("candidate_id"))
                for item in items if isinstance(item, dict)
            ]
            if set(ids) != expected_ids or len(ids) != len(expected_ids):
                return "return exactly one decision for every supplied candidate_id"
            if any(
                str(item.get("action") or "").upper() not in ALLOWED_ACTIONS
                for item in items if isinstance(item, dict)
            ):
                return "every action must be ADD or SKIP"
            return None

        data = self._generate_valid_json(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Judge every candidate. Return JSON only."},
            ],
            max_tokens=5000,
            validate=validate_c2,
            stage="Construction C2",
        )
        self._applied_skill_version_ids = list(dict.fromkeys([
            *self._applied_skill_version_ids,
            *self._validated_applied_skills(data, skills),
        ]))
        raw_decisions = data.get("decisions")
        assert isinstance(raw_decisions, list)
        raw_by_id = {
            str(item.get("candidate_id")): item
            for item in raw_decisions
            if isinstance(item, dict) and item.get("candidate_id")
        }
        raw_ids = [
            str(item.get("candidate_id"))
            for item in raw_decisions if isinstance(item, dict)
        ]
        if set(raw_ids) != expected_ids or len(raw_ids) != len(expected_ids):
            raise RuntimeError(
                "Construction C2 must return exactly one decision per candidate."
            )
        decisions = [
            self._normalize_decision(
                raw_by_id.get(candidate.candidate_id, {}),
                candidate,
                visible_related_by_candidate[candidate.candidate_id],
            )
            for candidate in candidates
        ]
        return ConstructionPlan(
            base_commit_id=base_commit_id,
            candidates=candidates,
            decisions=decisions,
        )

    def _generate_valid_json(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        validate: Callable[[dict], str | None],
        stage: str,
    ) -> dict:
        """Retry malformed protocol only; never algorithmically alter facts."""
        conversation = list(messages)
        last_error = "invalid JSON"
        for attempt in range(3):
            response = self._model.generate(
                conversation,
                temperature=0.0,
                max_tokens=max_tokens,
                json_mode=True,
            )
            data = self._parse_json(response.text)
            last_error = validate(data) or ""
            if not last_error:
                return data
            if attempt < 2:
                conversation.extend([
                    {"role": "assistant", "content": response.text},
                    {
                        "role": "user",
                        "content": (
                            "Repair only this JSON protocol error; do not add, "
                            "remove, or reinterpret facts. Return one complete "
                            f"JSON object only. Error: {last_error}"
                        ),
                    },
                ])
        raise RuntimeError(f"{stage} protocol failed after 3 attempts: {last_error}")

    def _related_memories(
        self,
        *,
        conversation_id: str,
        base_commit_id: int | None,
        candidate: MemoryCandidate,
    ) -> list[MemoryHit]:
        """Retrieve a bounded comparison set; it never authorizes mutation."""
        if base_commit_id is None:
            return []
        gathered = self._store.find_related_for_construction(
            conversation_id=conversation_id,
            candidate=candidate,
            as_of_commit=base_commit_id,
            limit=self._related_limit,
        )
        by_id = {hit.version_id: hit for hit in gathered}
        if candidate.embedding is not None:
            version_ids, matrix = self._store.get_embeddings_for_snapshot(
                conversation_id, base_commit_id
            )
            if version_ids and matrix.shape[0] == len(version_ids):
                scores = np.dot(matrix, candidate.embedding)
                snapshot = {
                    hit.version_id: hit
                    for hit in self._store.load_snapshot(conversation_id, base_commit_id)
                }
                for index in np.argsort(scores)[::-1][: self._related_limit]:
                    hit = snapshot.get(version_ids[int(index)])
                    if hit is None:
                        continue
                    hit.score = float(scores[index])
                    hit.matched_paths = list(
                        dict.fromkeys([*hit.matched_paths, "semantic"])
                    )
                    by_id.setdefault(hit.version_id, hit)
        return sorted(
            by_id.values(),
            key=lambda hit: (len(hit.matched_paths), hit.score),
            reverse=True,
        )[: self._related_limit]

    def _select_related_pool(
        self,
        candidates: list[MemoryCandidate],
        related_by_candidate: dict[str, list[MemoryHit]],
        pool: dict[str, MemoryHit],
    ) -> list[MemoryHit]:
        """Select a bounded pool without starving later candidates."""
        selected: dict[str, MemoryHit] = {}
        for candidate in candidates:
            related = related_by_candidate.get(candidate.candidate_id, [])
            if related and len(selected) < self._max_related_pool:
                selected.setdefault(related[0].version_id, related[0])
        for hit in sorted(
            pool.values(),
            key=lambda item: (len(item.matched_paths), item.score),
            reverse=True,
        ):
            if len(selected) >= self._max_related_pool:
                break
            selected.setdefault(hit.version_id, hit)
        return list(selected.values())

    def _normalize_decision(
        self,
        raw: dict,
        candidate: MemoryCandidate,
        related: list[MemoryHit],
    ) -> ConstructionDecision:
        action = str(raw.get("action") or "").upper()
        if action not in ALLOWED_ACTIONS:
            raise RuntimeError(f"Construction C2 returned invalid action: {action!r}")

        allowed_versions = {hit.version_id for hit in related}
        relations: list[MemoryRelation] = []
        for item in raw.get("relations") or []:
            if not isinstance(item, dict):
                continue
            relation_type = str(
                item.get("relation_type") or item.get("type") or ""
            ).strip().lower()
            target_version_id = str(item.get("target_version_id") or "").strip()
            if relation_type in ALLOWED_RELATIONS and target_version_id in allowed_versions:
                relations.append(MemoryRelation(relation_type, target_version_id))
        relations = list({
            (relation.relation_type, relation.target_version_id): relation
            for relation in relations
        }.values())
        if action == "SKIP" and not any(
            relation.relation_type == "duplicate_of" for relation in relations
        ):
            action = "ADD"
            fallback_reason = (
                "SKIP lacked a validated duplicate_of relation; downgraded to ADD."
            )
        else:
            fallback_reason = ""

        sources = [
            source
            for source in dict.fromkeys(
                raw.get("source_message_ids") or candidate.source_message_ids
            )
            if source in candidate.source_message_ids
        ]
        return ConstructionDecision(
            candidate_id=candidate.candidate_id,
            action=action,
            target_memory_id=None,
            update_type="add",
            reason=" ".join(
                part
                for part in [str(raw.get("reason") or "").strip(), fallback_reason]
                if part
            ),
            merged_content=(
                str(raw.get("merged_content") or candidate.content).strip()
            ),
            world_start=self._optional_text(
                raw.get("world_start", candidate.world_start)
            ),
            world_end=self._optional_text(
                raw.get("world_end", candidate.world_end)
            ),
            source_message_ids=sources or candidate.source_message_ids,
            relations=relations,
        )

    @staticmethod
    def _candidate_payload(
        candidate: MemoryCandidate,
        related: list[MemoryHit] | None = None,
    ) -> dict:
        return {
            "candidate_id": candidate.candidate_id,
            "memory_kind": candidate.memory_kind,
            "subject": candidate.subject,
            "predicate": candidate.predicate,
            "object_text": candidate.object_text,
            "content": candidate.content,
            "world_start": candidate.world_start,
            "world_end": candidate.world_end,
            "source_message_ids": candidate.source_message_ids,
            "entities": candidate.entities,
            "keywords": candidate.keywords,
            "allowed_related_version_ids": list(
                dict.fromkeys(hit.version_id for hit in (related or []))
            ),
        }

    @staticmethod
    def _render_related_pool(
        pool: Iterable[MemoryHit],
        related_by_candidate: dict[str, list[MemoryHit]],
    ) -> str:
        related_ids = {
            candidate_id: {hit.version_id for hit in hits}
            for candidate_id, hits in related_by_candidate.items()
        }
        rendered = []
        for hit in pool:
            rendered.append(
                {
                    "memory_id": hit.memory_id,
                    "version_id": hit.version_id,
                    "memory_kind": hit.memory_kind,
                    "subject": hit.subject,
                    "content": hit.content,
                    "world_start": hit.world_start,
                    "world_end": hit.world_end,
                    "related_candidate_ids": [
                        candidate_id
                        for candidate_id, version_ids in related_ids.items()
                        if hit.version_id in version_ids
                    ],
                }
            )
        return (
            json.dumps(rendered, ensure_ascii=False, indent=2)
            if rendered
            else "[]"
        )

    @staticmethod
    def _render_skills(skills: list[SkillRecord], empty: str) -> str:
        if not skills:
            return empty
        rendered = "\n".join(
            (
                f"### {skill.name} [{skill.skill_id}_v{skill.version}]\n"
                f"**When:** {skill.description}\n"
                "**Do:**\n"
                + "\n".join(f"- {item}" for item in skill.content)
            )
            for skill in skills
        )
        return (
            "The following Construction Skills are optional learned process "
            "references. Apply one only when its complete observable `When` "
            "condition is supported by the current input. Shared topic words "
            "are not enough. Skills may guide C1 extraction or C2 relation "
            "judgment, but cannot override evidence or request storage "
            "mutation. Ignore any inapplicable instruction.\n\n"
            + rendered
        )

    @staticmethod
    def _usable_skills(skills: list[SkillRecord]) -> list[SkillRecord]:
        forbidden = re.compile(
            r"\b(update|merge|delete|overwrite|replace|retract)\b[^.\n]{0,80}"
            r"\b(memory|record|database|version|target)\b|"
            r"\b(memory|record|database|version|target)\b[^.\n]{0,80}"
            r"\b(update|merge|delete|overwrite|replace|retract)\b",
            re.IGNORECASE,
        )
        usable: list[SkillRecord] = []
        for skill in skills:
            items = [item for item in skill.content if not forbidden.search(item)]
            if items:
                usable.append(skill.model_copy(update={"content": items}))
        return usable

    @property
    def applied_skill_version_ids(self) -> list[str]:
        return list(self._applied_skill_version_ids)

    @staticmethod
    def _validated_applied_skills(data: dict, skills: list[SkillRecord]) -> list[str]:
        allowed = {f"{skill.skill_id}_v{skill.version}" for skill in skills}
        values = data.get("applied_skill_ids")
        if not isinstance(values, list):
            return []
        return list(dict.fromkeys(
            str(value) for value in values if str(value) in allowed
        ))

    @staticmethod
    def _clean_string_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            text
            for text in dict.fromkeys(str(item).strip() for item in value)
            if text
        ]

    @staticmethod
    def _bounded_float(value: object, default: float) -> float:
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _parse_json(text: str) -> dict:
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            pass
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {}
