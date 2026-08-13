"""Minimal, append-only Construction Agent.

One model call turns a session into self-contained memories.  A small set of
relevant existing memories is shown only to avoid restating facts that are
already stored.  The program then applies two deterministic operations:

* ADD a genuinely new extracted memory;
* SKIP an exact duplicate.

State changes are appended with their event time instead of asking an LLM to
rewrite history.  This deliberately small write contract gives Construction
Skills one clear extension point: they may change evidence-bound extraction,
but they never issue database CRUD commands.
"""

from __future__ import annotations

import json
import re
import numpy as np

from ..llm.base import ModelClient
from ..retrieval.embedder import Embedder
from ..schemas import SkillRecord
from ..storage.sqlite_store import (
    ConstructionDecision,
    ConstructionPlan,
    MemoryCandidate,
    MemoryHit,
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

Return one JSON object with a candidates array. Each candidate has:
memory_kind, subject, predicate (short optional retrieval label), object_text
(optional), content (1-3 complete sentences), world_start, world_end,
source_message_ids, entities, keywords, importance, confidence.

Construction Skills:
{skills_section}

Session time: {session_time}

Messages:
{session_messages}
"""


BATCH_DECISION_SYSTEM = """\
You are a long-term Memory Manager. This is the CRUD/consolidation stage of a
Mem0-style memory pipeline. Review all new candidates together with the
retrieved existing-memory pool and return exactly one decision per candidate.

Actions:
- ADD: genuinely new memory.
- UPDATE: revise one logical memory while keeping its memory_id.
- MERGE: consolidate a candidate into one overlapping logical memory.
- DELETE: retract an existing memory when new evidence shows it is false and
  there is no useful replacement. Prefer UPDATE when a replacement exists.
- SKIP: duplicate, transient, unsupported, or already fully represented.

Rules:
1. Preserve the candidate_id exactly.
2. UPDATE, MERGE, and DELETE require target_memory_id copied from that
   candidate's allowed_target_memory_ids. Never use a version_id as the target.
   An empty list means only ADD or SKIP is allowed.
3. For UPDATE/MERGE, merged_content must be a standalone 1-3 sentence memory
   containing all still-valid details from both old and new evidence.
4. Use state_change when an old state was once true; correction when it was
   wrong; enrichment when adding compatible detail; merge for consolidation;
   retraction for DELETE.
5. Do not create separate fragments for details that belong to the same
   entity, event, preference, list, or ongoing plan.
6. Do not alter source IDs. The runtime computes inherited provenance.
7. Retrieval similarity does not mean identity. Never UPDATE a general profile
   merely because a new event involves the same person or broad topic.
8. Each existing memory_id may be targeted by at most one candidate in this
   batch.
9. Return JSON only:
{"decisions":[{"candidate_id":"...","action":"ADD|UPDATE|MERGE|DELETE|SKIP",
"target_memory_id":null,"update_type":"add|state_change|correction|enrichment|merge|retraction",
"reason":"short operational reason","merged_content":"...",
"world_start":null,"world_end":null,"source_message_ids":["..."]}]}

Construction Skills:
{skills_section}

New Candidates:
{candidates_json}

Relevant Existing Memory Pool:
{related_memories}
"""


class ConstructionAgent:
    """Session-level extraction followed by deterministic ADD/SKIP."""

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
        max_search_more_calls: int = 0,
        semantic_crud_threshold: float = 0.88,
    ):
        self._model = model
        self._store = store
        self._embedder = embedder
        self._extraction_prompt = extraction_prompt
        # Kept only so older configs/extensions can still instantiate the
        # class.  The minimal runtime never sends this prompt to a model.
        self._decision_prompt = decision_prompt
        self._max_candidates = max_candidates_per_session
        self._related_limit = related_memory_limit
        self._max_related_pool = max_related_pool
        self._semantic_crud_threshold = semantic_crud_threshold
        # Retained in the signature for config/backward compatibility. The new
        # manager performs one deterministic related-memory gathering pass.
        self._max_search_more = max_search_more_calls

    def extract_candidates(
        self,
        session_id: str,
        conversation_id: str,
        session_messages: list[dict],
        session_time: str | None,
        skills: list[SkillRecord],
        base_commit_id: int | None = None,
    ) -> list[MemoryCandidate]:
        """Extract dense memories in one call, with bounded dedup context."""
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
        existing_memories = self._relevant_existing_for_session(
            conversation_id=conversation_id,
            base_commit_id=base_commit_id,
            session_text=message_text,
        )
        prompt = _safe_format(
            self._extraction_prompt,
            skills_section=skill_text,
            session_time=session_time or "unknown",
            session_messages=message_text,
            existing_memories=json.dumps(
                existing_memories, ensure_ascii=False, indent=2
            ),
        )
        response = self._model.generate(
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
            temperature=0.0,
            max_tokens=8000,
            json_mode=True,
        )
        data = self._parse_json(response.text)
        raw_candidates = data.get("candidates")
        if not isinstance(raw_candidates, list):
            raise RuntimeError(
                f"Candidate extraction returned invalid schema for {session_id}: "
                f"{response.text[:1200]!r}"
            )

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
        """Create an ADD/SKIP plan without a second model call.

        ``skills`` remains in the signature for extension compatibility.  It
        has already influenced extraction and cannot mutate storage directly.
        """
        if not candidates:
            return ConstructionPlan(
                base_commit_id=base_commit_id, candidates=[], decisions=[]
            )

        decisions: list[ConstructionDecision] = []
        for candidate in candidates:
            related = self._related_memories(
                conversation_id=conversation_id,
                base_commit_id=base_commit_id,
                candidate=candidate,
            )
            exact_duplicate = next(
                (hit for hit in related if "exact" in hit.matched_paths),
                None,
            )
            decisions.append(ConstructionDecision(
                candidate_id=candidate.candidate_id,
                action="SKIP" if exact_duplicate else "ADD",
                update_type="add",
                reason=(
                    "Exact content is already active."
                    if exact_duplicate
                    else "New evidence-bound memory."
                ),
                merged_content=candidate.content,
                world_start=candidate.world_start,
                world_end=candidate.world_end,
                source_message_ids=candidate.source_message_ids,
            ))
        return ConstructionPlan(
            base_commit_id=base_commit_id,
            candidates=candidates,
            decisions=decisions,
        )

    def _relevant_existing_for_session(
        self,
        *,
        conversation_id: str,
        base_commit_id: int | None,
        session_text: str,
    ) -> list[dict]:
        """Return a bounded, read-only snapshot context for deduplication."""
        if base_commit_id is None:
            return []
        version_ids, matrix = self._store.get_embeddings_for_snapshot(
            conversation_id, base_commit_id
        )
        if not version_ids or matrix.shape[0] != len(version_ids):
            return []
        encode_queries = getattr(self._embedder, "encode_queries", None)
        query = (
            encode_queries([session_text])[0]
            if callable(encode_queries)
            else self._embedder.encode([session_text])[0]
        )
        scores = np.dot(matrix, query)
        best = np.argsort(scores)[::-1][: self._related_limit]
        snapshot = {
            hit.version_id: hit
            for hit in self._store.load_snapshot(conversation_id, base_commit_id)
        }
        return [
            {
                "memory_kind": hit.memory_kind,
                "subject": hit.subject,
                "content": hit.content,
                "world_start": hit.world_start,
                "world_end": hit.world_end,
            }
            for index in best
            if float(scores[index]) > 0
            for hit in [snapshot.get(version_ids[int(index)])]
            if hit is not None
        ]

    def _related_memories(
        self,
        *,
        conversation_id: str,
        base_commit_id: int | None,
        candidate: MemoryCandidate,
    ) -> list[MemoryHit]:
        """Return exact active duplicates; similarity never authorizes writes."""
        if base_commit_id is None:
            return []
        gathered = self._store.find_related_for_construction(
            conversation_id=conversation_id,
            candidate=candidate,
            as_of_commit=base_commit_id,
            limit=self._related_limit,
        )
        return [
            hit for hit in gathered if "exact" in hit.matched_paths
        ][: self._related_limit]

    def _is_crud_compatible(
        self,
        candidate: MemoryCandidate,
        hit: MemoryHit,
    ) -> bool:
        """Conservatively decide whether a memory may be mutated.

        Retrieval similarity means "useful context", not "same logical
        memory". Mutation therefore needs a stronger algorithmic gate.
        """
        if "exact" in hit.matched_paths:
            return True

        candidate_subject = self._normalized_key(candidate.subject)
        hit_subject = self._normalized_key(hit.subject)
        if not candidate_subject or candidate_subject != hit_subject:
            return False

        if hit.score >= self._semantic_crud_threshold:
            return True

        candidate_predicate = self._normalized_key(candidate.predicate)
        hit_predicate = self._normalized_key(hit.predicate)
        if not candidate_predicate or candidate_predicate != hit_predicate:
            return False

        # Stable attributes can use subject+predicate as a logical key.
        if candidate.memory_kind in {
            "profile",
            "preference",
            "state",
            "plan",
            "relationship",
        }:
            return True

        # Event predicates such as "activity" or "recreation" are too broad.
        # Require a shared non-subject entity before mutating an event.
        subject_key = candidate_subject
        candidate_entities = {
            self._normalized_key(entity)
            for entity in candidate.entities
            if self._normalized_key(entity) not in {"", subject_key}
        }
        hit_entities = {
            self._normalized_key(entity)
            for entity in hit.entities
            if self._normalized_key(entity) not in {"", subject_key}
        }
        return bool(candidate_entities & hit_entities)

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
        action = str(raw.get("action") or "ADD").upper()
        if action == "NONE":
            action = "SKIP"
        if action not in ALLOWED_ACTIONS:
            action = "ADD"

        target = self._logical_memory_id(raw.get("target_memory_id"), related)
        allowed_targets = {hit.memory_id for hit in related}
        if action in {"UPDATE", "MERGE", "DELETE"} and target not in allowed_targets:
            action = "ADD"
            target = None
            fallback_reason = "Invalid or unobserved target; downgraded to ADD."
        else:
            fallback_reason = ""

        update_type = str(raw.get("update_type") or "").lower()
        default_type = {
            "ADD": "add",
            "UPDATE": "enrichment",
            "MERGE": "merge",
            "DELETE": "retraction",
            "SKIP": "add",
        }[action]
        if update_type not in ALLOWED_UPDATE_TYPES:
            update_type = default_type

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
            target_memory_id=target,
            update_type=update_type,
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
            "allowed_target_memory_ids": list(
                dict.fromkeys(hit.memory_id for hit in (related or []))
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
                f"### {skill.name}\n"
                f"**When:** {skill.description}\n"
                "**Do:**\n"
                + "\n".join(f"- {item}" for item in skill.content)
            )
            for skill in skills
        )
        return (
            "The following Construction Skills are learned behavioral priors "
            "internalized from earlier runtime experience. A retrieved Skill "
            "may guide extraction only when the current messages "
            "contain direct evidence for its complete observable `When` trigger. "
            "A shared topic, entity type, or activity word is not a trigger. "
            "System rules, the source messages, and evidence fidelity always "
            "override a Skill. Skills cannot request UPDATE, MERGE, DELETE, or "
            "other database operations. Never invent facts, and ignore a Skill "
            "when its trigger or premise does not fit the current session.\n\n"
            + rendered
        )

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
    def _normalized_key(value: object) -> str:
        return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", str(value or "").lower())

    @staticmethod
    def _logical_memory_id(
        target_id: str | None,
        related: list[MemoryHit],
    ) -> str | None:
        """Normalize a displayed version ID to its logical memory ID."""
        for hit in related:
            if target_id == hit.version_id:
                return hit.memory_id
        return target_id

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
