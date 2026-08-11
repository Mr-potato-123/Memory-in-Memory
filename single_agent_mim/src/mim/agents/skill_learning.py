"""Maintenance agents for candidate generation and batch CRUD planning."""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Literal

from ..llm.base import ModelClient
from ..skill_maker.models import (
    CandidateResolution,
    SkillBatchPlan,
    SkillCandidate,
    SkillCandidateBatch,
    SkillOperation,
    SkillPayload,
)
from ..skill_maker.repository import SkillRecord
from ..skill_maker.success_examples import (
    NoSkillSuccessIndex,
    SuccessfulSkillExampleIndex,
)
from ..skill_maker.validator import SkillPayloadValidator


class CandidateSkillAgent:
    """Generate an unpublished Skill from one completed diagnosis package."""

    def __init__(
        self,
        model: ModelClient,
        *,
        prompt: str,
        success_examples: SuccessfulSkillExampleIndex | None = None,
        default_policy_examples: NoSkillSuccessIndex | None = None,
    ):
        self._model = model
        self._prompt = prompt
        self._validator = SkillPayloadValidator()
        self._success_examples = success_examples
        self._default_policy_examples = default_policy_examples

    def generate(
        self,
        *,
        diagnosis: dict[str, Any],
        side: Literal["access", "construction"],
    ) -> SkillCandidate | None:
        diagnosis_id = str(
            diagnosis.get("diagnosis_id")
            or diagnosis.get("failure_id")
            or f"diagnosis_{uuid.uuid4().hex[:10]}"
        )
        skill_trace = (
            diagnosis.get("skill_trace")
            if side == "access"
            else diagnosis.get("construction_skill_traces", [])
        )
        payload = {
            "side": side,
            "diagnosis": diagnosis,
            "official_skill_trace": skill_trace,
            "successful_skill_use_example": (
                self._success_examples.select(
                    side=side,
                    official_skill_trace=skill_trace,
                )
                if self._success_examples is not None
                else None
            ),
            "default_policy_success_example": (
                self._default_policy_examples.select(diagnosis)
                if self._default_policy_examples is not None
                else None
            ),
            "instruction": (
                "Generate one narrowly applicable, generalized candidate "
                "Skill, or state that the official Skill Bank already "
                "contains sufficient guidance. In solves, name the concrete "
                "future problem pattern and its non-applicable boundary."
            ),
        }
        messages = [
            {"role": "system", "content": self._prompt},
            {
                "role": "user",
                "content": json.dumps(
                    payload, ensure_ascii=False, indent=2
                ),
            },
        ]
        data: dict[str, Any] = {}
        decision = ""
        response_text = ""
        last_error = ""
        # DeepSeek V4 thinking tokens share the output budget.  The old 1800
        # token cap occasionally ended after reasoning but before final JSON.
        # Retry only protocol-empty responses; semantic validation remains a
        # separate deterministic gate below.
        for attempt in range(3):
            response = self._model.generate(
                messages,
                temperature=0.0,
                max_tokens=12000,
                json_mode=True,
            )
            response_text = response.text
            data = self._parse_json(response_text)
            decision = str(data.get("decision", "")).upper()
            if decision in {
                "NO_CHANGE_ALREADY_COVERED",
                "NO_CHANGE_NOT_A_SKILL_PROBLEM",
            }:
                return None
            if decision == "PROPOSE_SKILL":
                try:
                    return self._build_candidate(
                        data=data,
                        side=side,
                        diagnosis_id=diagnosis_id,
                        diagnosis=diagnosis,
                    )
                except ValueError as exc:
                    last_error = str(exc)
            else:
                last_error = (
                    "decision must be PROPOSE_SKILL, "
                    "NO_CHANGE_ALREADY_COVERED, or "
                    "NO_CHANGE_NOT_A_SKILL_PROBLEM"
                )

            if attempt < 2:
                messages.extend([
                    {"role": "assistant", "content": response_text},
                    {
                        "role": "user",
                        "content": (
                            "Return a corrected complete JSON object. Preserve "
                            "the same mechanism and narrow applicability "
                            "boundary; only repair the listed protocol or "
                            "length violations. Compress wording instead of "
                            "broadening scope. Validation errors: "
                            f"{last_error}"
                        ),
                    },
                ])

        raise ValueError(
            "Candidate Skill Agent failed after bounded repair retries: "
            f"{last_error or decision or '<empty>'}; "
            f"response_chars={len(response_text)}"
        )

    def _build_candidate(
        self,
        *,
        data: dict[str, Any],
        side: Literal["access", "construction"],
        diagnosis_id: str,
        diagnosis: dict[str, Any],
    ) -> SkillCandidate:
        skill = data.get("skill")
        if not isinstance(skill, dict):
            raise ValueError("PROPOSE_SKILL requires a skill object")
        related = [
            str(skill_id)
            for skill_id in data.get("related_existing_skill_ids", [])
            if str(skill_id)
        ]
        transition = str(
            diagnosis.get("transition")
            or (diagnosis.get("flip") or {}).get("direction")
            or ""
        ).upper()
        intent = str(
            data.get("maintenance_intent")
            or diagnosis.get("maintenance_intent_hint")
            or ""
        ).upper()
        if intent not in {"ADD", "REVISE", "REMOVE", "PRESERVE"}:
            if transition == "W2C":
                intent = "PRESERVE"
            elif transition in {"C2W", "W2W"} and related:
                intent = "REVISE"
            else:
                intent = "ADD"
        failure_to_repair = diagnosis.get("failure_to_repair")
        failure_to_repair = (
            failure_to_repair if isinstance(failure_to_repair, dict) else {}
        )
        candidate = SkillCandidate(
            candidate_id=f"cand_{side}_{uuid.uuid4().hex[:12]}",
            skill_id=f"sk_{side}_{uuid.uuid4().hex[:10]}",
            side=side,
            payload=SkillPayload(
                name=skill.get("name", ""),
                description=skill.get("description", ""),
                content=skill.get("content", []),
            ),
            solves=str(data.get("solves", "")).strip(),
            related_existing_skill_ids=list(dict.fromkeys(related)),
            source_diagnosis_id=diagnosis_id,
            source_failure_id=diagnosis_id,
            transition=transition,
            failure_age=max(0, int(diagnosis.get("failure_age") or 0)),
            maintenance_intent=intent,
            why_previous_round_failed=str(
                data.get("why_previous_round_failed")
                or failure_to_repair.get("why_previous_round_failed")
                or ""
            ).strip(),
            created_at=time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
        )
        # Models frequently miss a hard character limit by a few characters
        # despite the bounded repair turn.  Deterministically compress only
        # over-limit prose at a word/punctuation boundary; never broaden or
        # synthesize a mechanism.  The strict validator remains the final gate.
        candidate.payload.description = self._compact_text(
            candidate.payload.description, 200
        )
        candidate.payload.content = [
            self._compact_text(item, 200)
            for item in candidate.payload.content[:3]
        ]
        while len(candidate.payload.content_text()) > 600 and candidate.payload.content:
            candidate.payload.content[-1] = self._compact_text(
                candidate.payload.content[-1],
                max(1, 600 - len("\n".join(candidate.payload.content[:-1])) - 1),
            )
            if len(candidate.payload.content_text()) > 600 and len(candidate.payload.content) > 1:
                candidate.payload.content.pop()
        valid, errors = self._validator.validate(
            candidate.payload,
            side=side,
        )
        if not candidate.solves:
            errors.append("solves is empty")
            valid = False
        if len(candidate.solves) > 600:
            errors.append("solves is longer than 600 characters")
            valid = False
        if not valid:
            raise ValueError(
                "Invalid generated candidate Skill: " + "; ".join(errors)
            )
        return candidate

    @staticmethod
    def _compact_text(value: str, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        clipped = text[: max(1, limit - 1)]
        boundary = max(
            clipped.rfind("."), clipped.rfind(";"), clipped.rfind(":"),
            clipped.rfind(" "),
        )
        if boundary >= max(20, limit // 2):
            clipped = clipped[:boundary].rstrip()
        return clipped + "…"

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            pass

        # Attempt: extract outermost JSON object and repair common issues
        repaired = CandidateSkillAgent._repair_json(text)
        if repaired is not None:
            return repaired
        return {}

    @staticmethod
    def _repair_json(text: str) -> dict[str, Any] | None:
        """Attempt to repair malformed JSON from DeepSeek.

        Common issues: missing commas between members, trailing commas,
        markdown fences, extra text before/after the object.
        """
        # Strip markdown fences
        cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)

        # Try to find the outermost object
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        raw = match.group(0)

        # Repair 1: missing comma before a newline + quote (key or string value)
        # Pattern: "..."\n  "..."  -> need comma between them
        raw = re.sub(r'("(?:\\.|[^"\\])*")\s*\n\s*(")', r'\1,\n\2', raw)
        # Repair 2: missing comma before closing bracket after a value
        raw = re.sub(r'((?:"(?:\\.|[^"\\])*"|\d+|true|false|null))\s*\n\s*(\]|\})', r'\1,\n\2', raw)
        # Repair 3: trailing comma before closing bracket/brace
        raw = re.sub(r',\s*(\]|\})', r'\1', raw)
        # Repair 4: missing comma between a closing brace/bracket and next member
        raw = re.sub(r'(\]|\})\s*\n\s*(")', r'\1,\n\2', raw)

        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            pass

        # Second pass: more aggressive comma insertion
        # Between a value and the next quote
        raw2 = re.sub(
            r'((?:\d+|true|false|null|"[^"]*"|\]|\}))\s*\n\s*(")',
            r'\1,\n\2', raw,
        )
        try:
            value = json.loads(raw2)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None


class BatchSkillCrudAgent:
    """Plan multiple atomic operations without seeing diagnosis packages."""

    def __init__(self, model: ModelClient, *, prompt: str):
        self._model = model
        self._prompt = prompt

    def plan(
        self,
        *,
        batch: SkillCandidateBatch,
        official_records: list[SkillRecord],
        validation_feedback: str = "",
    ) -> SkillBatchPlan:
        allowed_ids = set(batch.retrieved_skill_ids)
        official = [
            record.to_dict()
            for record in official_records
            if record.skill_id in allowed_ids
        ]
        payload = {
            "candidate_batch": batch.model_dump(mode="json"),
            "retrieved_official_skills": official,
            "important_boundary": (
                "Diagnosis packages and runtime traces are intentionally not "
                "available here. Use each candidate's solves field."
            ),
        }
        if validation_feedback:
            payload["previous_validation_error"] = validation_feedback
            repair_instruction = (
                "Return a corrected complete plan. For update_content, "
                "delete_content, or move_content, include content_index and "
                "expected_content copied exactly from the supplied target "
                "Skill. Do not repeat the reported protocol error."
            )
            if "content too long" in validation_feedback.lower():
                repair_instruction += (
                    " The previous plan made a Skill too long. Consolidate "
                    "overlapping rules with update_content/delete_content, or "
                    "create a separate narrowly-triggered Skill; do not append "
                    "the candidate wholesale. Keep total content under 2000 "
                    "characters."
                )
            payload["repair_instruction"] = repair_instruction
        # CRUD needs structured JSON output. Thinking tokens consume the
        # output budget before emitting JSON, especially with large batches.
        response = self._model.generate(
            [
                {"role": "system", "content": self._prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        payload, ensure_ascii=False, indent=2
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=max(12000, 3000 * len(batch.candidates)),
            json_mode=True,
        )
        data = CandidateSkillAgent._parse_json(response.text)
        operations = [SkillOperation(**item) for item in data.get("operations", [])]
        # The operation-level side is redundant with the batch side.  Models
        # occasionally copy the other prompt's literal example; the batch is
        # authoritative and keeps the two physical banks isolated.
        current_candidate_ids = {
            candidate.candidate_id for candidate in batch.candidates
        }
        provenance_owner: dict[str, set[str]] = {}
        for candidate in batch.candidates:
            for source_id in candidate.source_candidate_ids:
                provenance_owner.setdefault(str(source_id), set()).add(
                    candidate.candidate_id
                )
        for operation in operations:
            operation.side = batch.side
            # Cluster drafts retain diagnosis-level candidate IDs as
            # provenance.  A model may copy those nested IDs into a CRUD
            # operation even though the atomic transaction is over the draft.
            # Remap only explicit, unique ownership; genuinely unknown IDs
            # remain untouched for the executor's strict validator to reject.
            normalized_sources: list[str] = []
            for source_id in operation.source_candidate_ids:
                source_id = str(source_id)
                if source_id in current_candidate_ids:
                    normalized_sources.append(source_id)
                    continue
                owners = provenance_owner.get(source_id, set())
                if len(owners) == 1:
                    normalized_sources.extend(owners)
                else:
                    normalized_sources.append(source_id)
            operation.source_candidate_ids = list(
                dict.fromkeys(normalized_sources)
            )
        plan = SkillBatchPlan(
            transaction_id=str(
                data.get("transaction_id")
                or f"tx_{batch.batch_id}_{uuid.uuid4().hex[:8]}"
            ),
            side=batch.side,
            base_bank_version=batch.base_bank_version,
            candidate_resolutions=[
                CandidateResolution(**item)
                for item in data.get("candidate_resolutions", [])
            ],
            operations=operations,
        )
        return plan


class DirectCaseCrudAgent:
    """Plan one evidence-grounded CRUD transaction without a Skill candidate.

    The diagnosis package and the small set of retrieved official Skills are
    shown together.  Candidate generation, clustering, and draft
    summarization are deliberately absent from this path.
    """

    def __init__(self, model: ModelClient, *, prompt: str):
        self._model = model
        self._prompt = prompt

    def plan(
        self,
        *,
        case_id: str,
        side: Literal["access", "construction"],
        direction: str,
        diagnosis: dict[str, Any],
        batch: SkillCandidateBatch,
        official_records: list[SkillRecord],
        validation_feedback: str = "",
    ) -> SkillBatchPlan:
        allowed_ids = set(batch.retrieved_skill_ids)
        payload: dict[str, Any] = {
            "case_id": case_id,
            "side": side,
            "direction": direction,
            "diagnosis": diagnosis,
            "retrieved_official_skills": [
                record.to_dict()
                for record in official_records
                if record.skill_id in allowed_ids
            ],
            "bank_version": batch.base_bank_version,
        }
        if validation_feedback:
            payload["previous_validation_error"] = validation_feedback
        response = self._model.generate(
            [
                {"role": "system", "content": self._prompt},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                },
            ],
            temperature=0.0,
            max_tokens=8000,
            json_mode=True,
        )
        data = CandidateSkillAgent._parse_json(response.text)
        operations: list[SkillOperation] = []
        for item in data.get("operations", []):
            if not isinstance(item, dict):
                continue
            # A missing add payload is not a valid CREATE proposal.  Treat it
            # as NOOP rather than allowing a malformed model row to abort an
            # otherwise resumable per-question stream.
            if (
                str(item.get("operation", "")) == "add_skill"
                and (
                    not str(item.get("name", "")).strip()
                    or not str(item.get("description", "")).strip()
                )
            ):
                continue
            if (
                str(item.get("operation", "")) in {"rename_skill", "update_description"}
                and not str(item.get("description", "") or item.get("name", "")).strip()
            ):
                continue
            if (
                str(item.get("operation", "")) == "update_content"
                and not str(item.get("new_content", "") or "").strip()
            ):
                continue
            operation = SkillOperation(**item)
            if operation.operation.value in {"add_skill", "add_content"}:
                if len("\n".join(operation.content)) > 2400:
                    continue
            if operation.operation.value == "update_content" and len(operation.new_content) > 2400:
                continue
            operations.append(operation)
        for operation in operations:
            operation.side = side
            if len(operation.content) > 8:
                # Keep the Skill validator's compactness contract when a JSON
                # model emits many tiny bullets.  One joined instruction is
                # still auditable and can be revised atomically later.
                operation.content = [" ".join(operation.content)]
            if (
                operation.operation.value == "add_skill"
                and len("\n".join(operation.content)) > 2400
            ):
                continue
            # The provenance unit is the contrastive case, never a generated
            # Candidate Skill or a nested trace identifier.
            operation.source_candidate_ids = [case_id]
        visible_by_id = {record.skill_id: record for record in official_records}
        seen_content_targets: set[tuple[str, str]] = set()
        filtered_operations: list[SkillOperation] = []
        for operation in operations:
            if operation.name and len(operation.name) > 80:
                continue
            if operation.description and len(operation.description) > 400:
                continue
            if operation.expected_content is not None:
                key = (operation.skill_id, operation.expected_content)
                if key in seen_content_targets:
                    continue
                seen_content_targets.add(key)
                record = visible_by_id.get(operation.skill_id)
                if record is None or operation.expected_content not in record.payload.content:
                    continue
            record = visible_by_id.get(operation.skill_id)
            if record is not None:
                current_content = list(record.payload.content)
                if operation.operation.value == "add_content":
                    current_content.extend(operation.content)
                elif operation.operation.value == "update_content":
                    if operation.expected_content in current_content:
                        index = current_content.index(operation.expected_content)
                        current_content[index] = operation.new_content
                if len("\n".join(current_content)) > 2400 or len(current_content) > 8:
                    continue
            filtered_operations.append(operation)
        operations = filtered_operations
        decision = str(data.get("decision", "APPLY" if operations else "NOOP")).upper()
        if decision == "NOOP":
            operations = []
        resolution = (
            "CREATED"
            if any(item.operation.value == "add_skill" for item in operations)
            else "MERGED_INTO_EXISTING"
            if operations
            else "ALREADY_COVERED"
        )
        target_ids = list(
            dict.fromkeys(item.skill_id for item in operations if item.skill_id)
        )
        return SkillBatchPlan(
            transaction_id=str(
                data.get("transaction_id")
                or f"tx_direct_{case_id}_{side}_{uuid.uuid4().hex[:8]}"
            ),
            side=side,
            base_bank_version=batch.base_bank_version,
            candidate_resolutions=[
                CandidateResolution(
                    candidate_id=case_id,
                    resolution=resolution,
                    target_skill_ids=target_ids,
                    reason=str(data.get("reason", "")).strip(),
                )
            ],
            operations=operations,
        )
