"""Access & Answer Agent (SQLite runtime) — iterative retrieval + answer in one loop.

Key features:
  - Every model action and complete tool result remains in message history
  - search_memory (hybrid/semantic/keyword/temporal) + inspect_memory + answer
  - Evidence validation before accepting answer
  - Budget fallback (forced answer when steps exhausted)
"""

from __future__ import annotations

import json
import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..llm.base import ModelClient
from ..schemas import (
    AccessResult,
    AgentAction,
    Question,
    SkillRecord,
)
from ..storage.sqlite_store import (
    SQLiteMemoryStore,
    MemoryHit,
    MemoryInspection,
    SearchFilters,
    SearchCall,
)
from ..retrieval.hybrid import HybridRetriever


ALLOWED_MEMORY_KINDS = {
    "profile",
    "preference",
    "state",
    "event",
    "plan",
    "relationship",
}
ALLOWED_SEARCH_STRATEGIES = {
    "hybrid",
    "semantic",
    "bm25",
    "keyword",
    "structured",
    "temporal",
}


def _safe_format(template: str, **kwargs: str) -> str:
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", value)
    return result


# ── Evidence Workspace ──────────────────────────────────────────

@dataclass
class SearchChain:
    """Bookkeeping for results actually returned during one question."""

    question: str
    snapshot_commit_id: int
    search_history: list[dict] = field(default_factory=list)
    returned_hits: dict[str, MemoryHit] = field(default_factory=dict)
    inspected: dict[str, MemoryInspection] = field(default_factory=dict)
    used_skill_ids: list[str] = field(default_factory=list)
    remaining_steps: int = 6

    def add_hits(self, hits: list[MemoryHit]):
        """Record every returned memory, without summarizing or evicting it."""
        for hit in hits:
            self.returned_hits.setdefault(hit.version_id, hit)


# Import compatibility for older extensions; runtime code uses SearchChain.
EvidenceWorkspace = SearchChain


# ── Access & Answer prompt ──────────────────────────────────────

ACCESS_SYSTEM = """\
You are a Memory Access & Answer Agent. Plan retrieval, gather all required
evidence, and then answer directly.

search_memory accepts:
- strategy: hybrid | semantic | bm25 | keyword | structured
- query: semantic intent
- query_expansions: up to 4 alternate meanings or individual hops
- keywords: exact names, titles, dates, or rare terms
- depth: shallow | standard | deep
- entities, memory_kinds, time_mode, target_time, target_time_end,
  include_history, top_k

Use only these memory_kinds: profile, preference, state, event, plan,
relationship. Default to an empty list and filter only when the required kind
is unambiguous. For a month or interval, use an explicit range rather than a
point on its first day.

Example:
{"action":"search_memory","arguments":{"strategy":"hybrid",
"query":"places where James and John planned to meet",
"query_expansions":["VR meeting plan","pub meeting plan","baseball invitation"],
"keywords":["VR","McGee's","baseball"],"depth":"deep","entities":["James","John"],
"memory_kinds":[],"time_mode":"none","include_history":false,"top_k":12},
"reason":"Collect each hop of the list question."}

inspect_memory accepts memory_id, include_versions, and include_sources.
answer accepts answer, evidence_version_ids, and confidence.

ANSWER FORMAT IS PART OF THE METRIC:
- Direct fact extraction: output only the minimal requested answer span, never
  a sentence.
- Named-entity/world-knowledge inference: output only the canonical answer.
- Lists: output all and only requested supported items, comma-separated.
- Direct yes/no: output only Yes or No. If an inferred qualification is needed
  for a comparison or judgment, add one short decisive clause.
- Why/how: output one concise causal or method phrase without restating the
  question.
- Use absolute world_start/world_end dates, never relative time.
- Never add conversational preambles, retrieval commentary, dates or related
  facts that were not requested.
- Validate the question's subject, object, relation, and event. If evidence
  only supports a swapped entity or similar but different event, answer
  exactly No information available.
Examples: "bowling"; "February 2022"; "Canada, Greenland";
"UNO"; "Because he preferred having a beer on his day off.";
"No. James supports Liverpool, while John supports Manchester City.".

Workflow:
1. Identify the answer type, each required claim/hop, and target time.
2. Start with hybrid/standard. Use deep or expansions for multi-hop and list
   questions; bm25/keyword for exact names; semantic for paraphrases;
   structured for entity/time constraints.
3. For multi-hop/list questions, enumerate all required components. One hit
   does not make a list complete. Check visible evidence for completeness and
   search again with a materially different query/route for every missing
   claim. Never repeat an identical failed query.
4. Follow ReAct autonomously after every observation. Judge evidence as FULL,
   PARTIAL, or NONE. FULL means answer now; PARTIAL means search/inspect for
   missing claims; NONE means change query/route/filters while a useful
   alternative remains. There is no fixed minimum search count.
5. Lists, counts, multi-hop, aggregation, comparison, and broad temporal
   questions may need separate searches, but stop once evidence is sufficient.
6. Use include_history or inspect_memory for prior/changed state.
7. You may perform evidence-grounded geographic inference, canonical entity
   recognition, date arithmetic, list union, and comparison. Use world
   knowledge only to transform visible evidence; do not invent conversation
   facts.
8. Cite only visible version IDs. If evidence remains insufficient after useful
   searches, answer exactly `No information available.`

Skills:
{skills_section}

The question arrives as the first user message. Every later tool result remains
in this same message history. Read the complete history before choosing the
next action. Maximum actions for this question: {max_steps}.
Return exactly one JSON action.
"""


class AccessAgent:
    """Access & Answer Agent with SQLite-backed retrieval."""

    def __init__(
        self,
        model: ModelClient,
        store: SQLiteMemoryStore,
        retriever: HybridRetriever,
        prompt_template: str = ACCESS_SYSTEM,
        max_steps: int = 6,
        max_search_calls: int = 4,
        max_inspect_calls: int = 2,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._model = model
        self._store = store
        self._retriever = retriever
        self._prompt = prompt_template
        self._max_steps = max_steps
        self._max_search = max_search_calls
        self._max_inspect = max_inspect_calls
        self._event_sink = event_sink

    def answer(
        self,
        question: Question,
        conversation_id: str,
        snapshot_commit_id: int,
        skills: list[SkillRecord],
        access_run_id: str | None = None,
    ) -> AccessResult:
        """Run the full Access & Answer loop."""
        access_run_id = access_run_id or f"access_{uuid.uuid4().hex[:12]}"
        chain = SearchChain(
            question=question.question,
            snapshot_commit_id=snapshot_commit_id,
            remaining_steps=self._max_steps,
        )
        # Track skill IDs
        for s in skills:
            chain.used_skill_ids.append(f"{s.skill_id}_v{s.version}")

        search_count = 0
        inspect_count = 0
        total_tokens = 0
        total_latency = 0
        action_trace: list[AgentAction] = []
        action_records: list[dict] = []
        final_answer = ""
        final_evidence: list[str] = []
        answer_prompt_hash = ""
        error: Optional[str] = None

        t0 = time.time()
        self._emit(
            "access_start",
            conversation_id=conversation_id,
            qa_id=question.qa_id,
            snapshot_commit_id=snapshot_commit_id,
        )

        system_msg = self._build_system(skills)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_msg},
            {
                "role": "user",
                "content": (
                    f"Question:\n{question.question}\n\n"
                    "Choose the next action."
                ),
            },
        ]

        for step in range(self._max_steps):
            chain.remaining_steps = self._max_steps - step
            self._emit(
                "access_step_start",
                conversation_id=conversation_id,
                qa_id=question.qa_id,
                step=step,
                remaining_steps=chain.remaining_steps,
            )

            try:
                resp = self._model.generate(
                    messages, temperature=0.0, max_tokens=1200, json_mode=True,
                )
            except Exception as exc:
                error = f"Model call failed at step {step}: {exc}"
                self._emit(
                    "access_model_error",
                    conversation_id=conversation_id,
                    qa_id=question.qa_id,
                    step=step,
                    error=str(exc),
                )
                break

            total_tokens += (resp.prompt_tokens or 0) + (resp.completion_tokens or 0)
            total_latency += resp.latency_ms

            action = self._parse_action(resp.text)
            if action is None:
                # One repair retry: some providers (e.g. DeepSeek without
                # thinking) occasionally emit text that is not valid JSON.
                # Ask for a strict JSON-only retry before giving up on the QA.
                repair_messages = list(messages)
                repair_messages.append(
                    {"role": "assistant", "content": resp.text}
                )
                repair_messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous output was not valid JSON. Reply "
                            "with exactly one JSON object containing only the "
                            '"action" and "arguments" keys, no prose, no '
                            "markdown fences."
                        ),
                    }
                )
                try:
                    repair_resp = self._model.generate(
                        repair_messages,
                        temperature=0.0,
                        max_tokens=1200,
                        json_mode=True,
                    )
                except Exception:
                    repair_resp = None
                if repair_resp is not None:
                    repaired = self._parse_action(repair_resp.text)
                    if repaired is not None:
                        resp = repair_resp
                        action = repaired

            if action is None:
                error = f"Protocol error at step {step}: could not parse JSON"
                self._emit(
                    "access_protocol_error",
                    conversation_id=conversation_id,
                    qa_id=question.qa_id,
                    step=step,
                    response_preview=resp.text[:500],
                )
                break

            # Preserve the exact action. Later calls receive this action and
            # the complete tool observation that follows it.
            messages.append({"role": "assistant", "content": resp.text})
            action_trace.append(action)
            self._emit(
                "access_action",
                conversation_id=conversation_id,
                qa_id=question.qa_id,
                step=step,
                action=action.action,
            )
            action_id = f"{access_run_id}_a{step:03d}"
            action_record = {
                "action_id": action_id,
                "step_index": step,
                "action_type": action.action,
                "request": action.model_dump(mode="json"),
                "response": {},
                "retrieval_hits": [],
            }

            if action.action == "answer":
                final_answer = str(
                    action.arguments.get("answer", "")
                ).strip()
                if not final_answer:
                    # Some providers emit an empty answer while their reason
                    # says the evidence is insufficient. Normalize that to the
                    # protocol's canonical unanswerable response so one item
                    # cannot abort an otherwise valid full evaluation run.
                    final_answer = "No information available."
                final_evidence = action.arguments.get(
                    "evidence_version_ids",
                    action.arguments.get("evidence_ids", []),
                )
                final_evidence = self._normalize_evidence_ids(
                    final_evidence, chain
                )
                answer_prompt_hash = hashlib.sha256(
                    json.dumps(
                        messages,
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()

                # Validate evidence
                validation_err = self._validate_evidence(
                    final_evidence, chain, conversation_id
                )
                if validation_err:
                    action_record["response"] = {
                        "status": "validation_error",
                        "error": validation_err,
                    }
                    action_records.append(action_record)
                    # Allow one retry
                    if step + 1 < self._max_steps:
                        self._append_observation(
                            messages,
                            "answer",
                            {
                                "status": "validation_error",
                                "error": validation_err,
                                "instruction": (
                                    "Fix evidence_version_ids and choose the "
                                    "next action."
                                ),
                            },
                            self._max_steps - step - 1,
                        )
                        final_answer = ""
                        final_evidence = []
                        continue
                    else:
                        error = f"Evidence validation failed: {validation_err}"
                        break
                action_record["response"] = {
                    "status": "accepted",
                    "answer": final_answer,
                    "evidence_version_ids": final_evidence,
                }
                action_records.append(action_record)
                break

            elif action.action == "search_memory":
                if search_count >= self._max_search:
                    observation = {
                        "status": "budget_exhausted",
                        "kind": "search",
                        "instruction": (
                            "Answer now using the search results already in "
                            "this conversation."
                        ),
                    }
                    action_record["response"] = observation
                    action_records.append(action_record)
                    self._append_observation(
                        messages,
                        "search_memory",
                        observation,
                        self._max_steps - step - 1,
                    )
                    continue

                search_count += 1
                observation = self._execute_search(
                    action, chain, conversation_id
                )
                action_record["response"] = observation
                action_record["retrieval_hits"] = observation.get("hits", [])
                action_records.append(action_record)

                chain.search_history.append({
                    "strategy": observation.get("strategy", "hybrid"),
                    "query": observation.get("query", ""),
                    "keywords": observation.get("keywords", []),
                    "depth": observation.get("depth", 1),
                    "hit_count": len(observation.get("hits", [])),
                })
                self._append_observation(
                    messages,
                    "search_memory",
                    observation,
                    self._max_steps - step - 1,
                )

            elif action.action == "inspect_memory":
                if inspect_count >= self._max_inspect:
                    observation = {
                        "status": "budget_exhausted",
                        "kind": "inspect",
                        "instruction": (
                            "Use the search and inspection results already in "
                            "this conversation."
                        ),
                    }
                    action_record["response"] = observation
                    action_records.append(action_record)
                    self._append_observation(
                        messages,
                        "inspect_memory",
                        observation,
                        self._max_steps - step - 1,
                    )
                    continue

                inspect_count += 1
                observation = self._execute_inspect(
                    action, chain, conversation_id
                )
                action_record["response"] = observation
                action_records.append(action_record)
                self._append_observation(
                    messages,
                    "inspect_memory",
                    observation,
                    self._max_steps - step - 1,
                )

            else:
                observation = {
                    "status": "unknown_action",
                    "error": (
                        f"Unknown action: {action.action}. Use search_memory, "
                        "inspect_memory, or answer."
                    ),
                }
                action_record["response"] = observation
                action_records.append(action_record)
                self._append_observation(
                    messages,
                    action.action,
                    observation,
                    self._max_steps - step - 1,
                )

        elapsed_ms = int((time.time() - t0) * 1000)

        # Retrieval and inspection actions are bounded, but exhausting that
        # budget must not turn an otherwise valid item into a protocol error.
        # Give the model one final, tool-free turn over the accumulated ReAct
        # history. This is an answer turn, not another retrieval step.
        if not final_answer and not error:
            messages.append({
                "role": "user",
                "content": (
                    "The search and inspection action budget is exhausted. "
                    "Do not call a tool. Return exactly one `answer` JSON "
                    "action now using the evidence already visible in this "
                    "conversation. If it is insufficient, answer exactly "
                    "`No information available.`"
                ),
            })
            try:
                resp = self._model.generate(
                    messages,
                    temperature=0.0,
                    max_tokens=1200,
                    json_mode=True,
                )
                total_tokens += (
                    (resp.prompt_tokens or 0) + (resp.completion_tokens or 0)
                )
                total_latency += resp.latency_ms
                forced_action = self._parse_action(resp.text)
                messages.append({"role": "assistant", "content": resp.text})
                if forced_action is not None and forced_action.action == "answer":
                    action_trace.append(forced_action)
                    final_answer = str(
                        forced_action.arguments.get("answer", "")
                    ).strip() or "No information available."
                    final_evidence = self._normalize_evidence_ids(
                        forced_action.arguments.get(
                            "evidence_version_ids",
                            forced_action.arguments.get("evidence_ids", []),
                        ),
                        chain,
                    )
                    validation_err = self._validate_evidence(
                        final_evidence, chain, conversation_id
                    )
                    if validation_err:
                        final_answer = "No information available."
                        final_evidence = []
                    request = forced_action.model_dump(mode="json")
                    status = "accepted_forced_answer"
                else:
                    # If the explicit answer-only instruction is violated,
                    # abstain rather than inventing an answer or failing the
                    # complete evaluation run.
                    final_answer = "No information available."
                    final_evidence = []
                    request = {
                        "action": "answer",
                        "arguments": {
                            "answer": final_answer,
                            "evidence_version_ids": [],
                        },
                        "reason": "Answer-only fallback after budget exhaustion.",
                    }
                    status = "canonical_abstention_after_budget"
                action_records.append({
                    "action_id": f"{access_run_id}_a{self._max_steps:03d}",
                    "step_index": self._max_steps,
                    "action_type": "answer",
                    "request": request,
                    "response": {
                        "status": status,
                        "answer": final_answer,
                        "evidence_version_ids": final_evidence,
                    },
                    "retrieval_hits": [],
                })
                answer_prompt_hash = hashlib.sha256(
                    json.dumps(
                        messages,
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
            except Exception as exc:
                error = f"Forced answer model call failed: {exc}"

        # Force answer if budget exhausted without answer
        if not final_answer and not error:
            final_answer = "(forced — budget exhausted)"
            error = "Budget exhausted without answer."

        visible_memories = [
            {
                "version_id": hit.version_id,
                "memory_id": hit.memory_id,
                "content": hit.content,
                "memory_kind": hit.memory_kind,
                "subject": hit.subject,
                "world_start": hit.world_start,
                "world_end": hit.world_end,
                "context_index": index,
                "rendered_text": (
                    f"[{hit.version_id}] {hit.memory_kind} | {hit.content}"
                ),
            }
            for index, hit in enumerate(chain.returned_hits.values())
        ]
        self._emit(
            "access_complete",
            conversation_id=conversation_id,
            qa_id=question.qa_id,
            steps=len(action_trace),
            error=error,
            answer_empty=not bool(final_answer),
            latency_ms=elapsed_ms,
        )

        return AccessResult(
            answer=final_answer or "(no answer)",
            evidence_ids=final_evidence,
            search_trace=action_trace,
            used_skill_ids=chain.used_skill_ids,
            access_run_id=access_run_id,
            answer_prompt_hash=answer_prompt_hash,
            visible_memories=visible_memories,
            action_records=action_records,
            total_tokens=total_tokens,
            latency_ms=elapsed_ms,
            error=error,
            steps=len(action_trace),
        )

    def _emit(self, event: str, **payload: Any) -> None:
        if self._event_sink is not None:
            self._event_sink({"event": event, **payload})

    # ── System message ────────────────────────────────────────

    def _build_system(self, skills: list[SkillRecord]) -> str:
        if skills:
            skill_text = "\n".join(
                (
                    f"### {s.name}\n**When:** {s.description}\n**Do:**\n"
                    + "\n".join(f"- {item}" for item in s.content)
                )
                for s in skills
            )
        else:
            skill_text = "(No access skills. Use default retrieval strategy.)"

        # Skills are advisory references, not commands.  The model must weigh
        # them against question difficulty and actual evidence; a simple
        # direct lookup keeps the default strategy even when a Skill's topic
        # overlaps, while complex questions may combine construction and
        # access guidance as needed.  Evidence always beats Skill instructions.
        skills_section = (
            "## Access Skills (advisory references, not commands)\n"
            "Below are reference strategies. Judge applicability against the "
            "question's actual difficulty and the evidence you find:\n"
            "- Simple direct questions: keep the default retrieval strategy; "
            "do not follow a Skill just because its topic overlaps.\n"
            "- Complex questions (multi-hop, lists, indirect evidence, broad "
            "temporal ranges): a Skill may guide extra searches, but never at "
            "the cost of skipping standard retrieval or ignoring evidence.\n"
            "- Evidence and basic retrieval always override Skill "
            "instructions. If a Skill's procedure does not fit the situation, "
            "ignore it.\n\n"
            + skill_text
        )

        return _safe_format(
            self._prompt,
            skills_section=skills_section,
            workspace_summary=(
                "Tool results arrive as later messages and remain available "
                "for the whole search chain."
            ),
            question=(
                "The question arrives separately as the first user message."
            ),
            max_steps=str(self._max_steps),
        )

    @staticmethod
    def _append_observation(
        messages: list[dict[str, str]],
        tool_name: str,
        observation: dict[str, Any],
        remaining_actions: int,
    ) -> None:
        """Append one complete tool result to the persistent message history."""
        body = {
            "tool": tool_name,
            "result": observation,
            "remaining_actions": max(0, remaining_actions),
        }
        messages.append(
            {
                "role": "user",
                "content": (
                    "Tool result:\n"
                    + json.dumps(body, ensure_ascii=False, sort_keys=True)
                    + "\nRead it together with every earlier result, then "
                    "choose the next action."
                ),
            }
        )

    # ── Action execution ──────────────────────────────────────

    def _execute_search(
        self, action: AgentAction, chain: SearchChain, conversation_id: str,
    ) -> dict:
        args = action.arguments
        query = str(args.get("query", "")).strip()
        strategy = str(
            args.get("strategy", args.get("method", "hybrid"))
        ).lower()
        if strategy not in ALLOWED_SEARCH_STRATEGIES:
            strategy = "hybrid"
        entities = self._clean_list(args.get("entities"), limit=12)
        keywords = self._clean_list(args.get("keywords"), limit=12)
        query_expansions = self._clean_list(
            args.get("query_expansions"), limit=4
        )
        requested_kinds = self._clean_list(args.get("memory_kinds"), limit=6)
        # Invalid free-form kinds previously caused guaranteed empty result
        # sets (e.g. "travel" against event/state memories). Keep only the
        # documented enum; an all-invalid request becomes no kind filter.
        memory_kinds = [
            kind for kind in requested_kinds if kind in ALLOWED_MEMORY_KINDS
        ] or None
        time_mode = str(args.get("time_mode", "none")).lower()
        if time_mode not in {
            "none", "current", "point", "before", "after", "range"
        }:
            time_mode = "none"
        target_time = args.get("target_time")
        target_time_end = args.get("target_time_end")
        if (
            time_mode == "range"
            and isinstance(target_time, str)
            and "/" in target_time
            and not target_time_end
        ):
            target_time, target_time_end = target_time.split("/", 1)
        include_history = args.get("include_history", False)
        try:
            top_k = int(args.get("top_k", 8))
        except (TypeError, ValueError):
            top_k = 8
        depth = args.get("depth", args.get("retrieval_depth", 1))

        filters = SearchFilters(
            conversation_id=conversation_id,
            as_of_commit=chain.snapshot_commit_id,
            memory_kinds=memory_kinds,
            entities=entities,
            time_mode=time_mode,
            target_time=target_time,
            target_time_end=target_time_end,
            include_history=include_history,
        )

        hits = self._retriever.search(
            conversation_id=conversation_id,
            snapshot_commit_id=chain.snapshot_commit_id,
            query=query,
            strategy=strategy,
            filters=filters,
            top_k=top_k,
            keywords=keywords,
            query_expansions=query_expansions,
            depth=depth,
        )

        returned_hits = hits[:top_k]
        chain.add_hits(returned_hits)

        return {
            "status": "ok",
            "strategy": strategy,
            "query": query,
            "query_expansions": query_expansions,
            "keywords": keywords,
            "depth": depth,
            "memory_kinds": memory_kinds or [],
            "ignored_memory_kinds": [
                kind
                for kind in requested_kinds
                if kind not in ALLOWED_MEMORY_KINDS
            ],
            "hits": [
                {
                    "version_id": h.version_id,
                    "memory_id": h.memory_id,
                    "content": h.content,
                    "memory_kind": h.memory_kind,
                    "subject": h.subject,
                    "world_start": h.world_start,
                    "world_end": h.world_end,
                    "entities": h.entities,
                    "score": round(h.score, 4),
                    "paths": h.matched_paths,
                }
                for h in returned_hits
            ],
            "total_returned_in_chain": len(chain.returned_hits),
        }

    @staticmethod
    def _clean_list(value: object, *, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            item
            for item in dict.fromkeys(str(raw).strip() for raw in value)
            if item
        ][:limit]

    def _execute_inspect(
        self, action: AgentAction, chain: SearchChain, conversation_id: str,
    ) -> dict:
        memory_id = action.arguments.get("memory_id", "")
        include_versions = action.arguments.get("include_versions", True)
        include_sources = action.arguments.get("include_sources", True)

        inspection = self._store.inspect_memory(
            conversation_id=conversation_id,
            memory_id=memory_id,
            snapshot_commit_id=chain.snapshot_commit_id,
            include_versions=include_versions,
            include_sources=include_sources,
        )
        chain.inspected[memory_id] = inspection
        chain.add_hits(inspection.versions)

        return {
            "status": "ok",
            "memory_id": memory_id,
            "versions": [
                {
                    "version_id": v.version_id,
                    "version_no": v.version_no,
                    "content": v.content,
                    "world_start": v.world_start,
                    "world_end": v.world_end,
                    "close_reason": v.close_reason,
                    "update_type": getattr(v, 'update_type', 'unknown'),
                }
                for v in inspection.versions
            ],
            "source_count": len(inspection.sources),
            "sources": [
                {
                    "message_id": source.get("message_id", ""),
                    "content": source.get("content", ""),
                }
                for source in inspection.sources
            ],
        }

    # ── Evidence validation ───────────────────────────────────

    def _validate_evidence(
        self,
        evidence_ids: list[str],
        chain: SearchChain,
        conversation_id: str,
    ) -> str | None:
        """Return error message if evidence is invalid, None if OK."""
        if not evidence_ids:
            return None  # "no answer" is valid for unanswerable questions

        for vid in evidence_ids:
            # It must have been returned in this search chain.
            if vid not in chain.returned_hits:
                # Check inspected
                found = False
                for insp in chain.inspected.values():
                    for v in insp.versions:
                        if v.version_id == vid:
                            found = True
                            break
                    if found:
                        break
                if not found:
                    return f"Evidence {vid} was not retrieved in this session."

        return None

    @staticmethod
    def _normalize_evidence_ids(
        evidence_ids: list[str] | object,
        chain: SearchChain,
    ) -> list[str]:
        """Resolve a visible logical memory_id to its returned version_id.

        Small runtime models occasionally cite the displayed memory_id even
        though the answer contract asks for version IDs. This is an
        unambiguous formatting repair when that logical memory was actually
        returned; unknown IDs still fail normal evidence validation.
        """
        if not isinstance(evidence_ids, list):
            return []
        memory_to_version: dict[str, str] = {}
        for hit in chain.returned_hits.values():
            memory_to_version.setdefault(hit.memory_id, hit.version_id)
        normalized: list[str] = []
        for raw in evidence_ids:
            value = str(raw)
            resolved = (
                value
                if value in chain.returned_hits
                else memory_to_version.get(value, value)
            )
            if resolved not in normalized:
                normalized.append(resolved)
        return normalized

    # ── JSON parsing ──────────────────────────────────────────

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Parse JSON from model output with multiple fallback strategies."""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return {}

    @staticmethod
    def _parse_action(text: str) -> Optional[AgentAction]:
        data = AccessAgent._parse_json(text)
        if not data:
            return None
        try:
            return AgentAction(**data)
        except Exception:
            return None
