"""Stable contracts for candidate Skills and batch CRUD."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SkillPayload(BaseModel):
    """Minimal Runtime-visible Skill body."""

    model_config = ConfigDict(validate_assignment=True)

    name: str = ""
    description: str = ""
    content: list[str] = Field(default_factory=list)

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, list):
            return [
                str(item).strip()
                for item in value
                if str(item).strip()
            ]
        raise TypeError("content must be a string or a list of strings")

    def content_text(self) -> str:
        return "\n".join(self.content)


class CandidateStatus(str, Enum):
    DRAFT = "draft"
    STAGED = "staged"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SkillCandidate(BaseModel):
    """Unpublished proposal physically isolated from the official bank."""

    candidate_id: str = ""
    skill_id: str = ""
    version: int = 1
    side: Literal["access", "construction"] = "access"
    payload: SkillPayload = Field(default_factory=SkillPayload)
    solves: str = ""
    related_existing_skill_ids: list[str] = Field(default_factory=list)
    # Draft Skills produced after semantic clustering retain explicit
    # provenance instead of hiding source IDs inside ``solves``.  Ordinary
    # diagnosis-level candidates leave these fields empty.
    source_candidate_ids: list[str] = Field(default_factory=list)
    source_cluster_id: str = ""
    source_diagnosis_id: str = ""
    source_failure_id: str = ""
    # Iteration-only maintenance metadata stays outside the Runtime-visible
    # Skill payload.  This lets W2W/C2W carry rich lineage without making the
    # final Skill longer or case-specific.
    transition: str = ""
    failure_age: int = 0
    maintenance_intent: Literal[
        "ADD", "REVISE", "REMOVE", "PRESERVE"
    ] = "ADD"
    why_previous_round_failed: str = ""
    target_first_break: str | None = None
    parent_version_id: str | None = None
    status: CandidateStatus = CandidateStatus.DRAFT
    created_at: str = ""


class SkillOperationType(str, Enum):
    ADD_SKILL = "add_skill"
    RENAME_SKILL = "rename_skill"
    UPDATE_DESCRIPTION = "update_description"
    ADD_CONTENT = "add_content"
    UPDATE_CONTENT = "update_content"
    DELETE_CONTENT = "delete_content"
    MOVE_CONTENT = "move_content"
    DELETE_SKILL = "delete_skill"


class SkillOperation(BaseModel):
    """One atomic operation in a batch transaction."""

    operation: SkillOperationType
    skill_id: str = ""
    target_skill_id: str = ""
    expected_skill_version: int | None = None
    name: str = ""
    description: str = ""
    content: list[str] = Field(default_factory=list)
    content_index: int | None = None
    expected_content: str | None = None
    new_content: str = ""
    side: Literal["access", "construction"] = "access"
    source_candidate_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    hard_delete: bool = False

    @field_validator("content", mode="before")
    @classmethod
    def normalize_operation_content(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        raise TypeError("operation content must be a string or list of strings")

    @field_validator("new_content", mode="before")
    @classmethod
    def normalize_new_content(cls, value: Any) -> str:
        if value is None:
            # A malformed model response must not make the whole direct CRUD
            # run unresumable.  The executor will validate the resulting
            # payload and the planner can retry with the surfaced error.
            return ""
        if isinstance(value, list):
            # Some JSON-mode models emit a one-item array for a field whose
            # schema is scalar.  Joining preserves all text while keeping the
            # atomic update_content contract deterministic.
            return "\n".join(str(item).strip() for item in value if str(item).strip())
        return str(value).strip()

    @field_validator("expected_content", mode="before")
    @classmethod
    def normalize_expected_content(cls, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, list):
            return "\n".join(str(item).strip() for item in value if str(item).strip())
        return str(value).strip()


class CandidateResolution(BaseModel):
    candidate_id: str
    resolution: Literal[
        "CREATED",
        "MERGED_INTO_EXISTING",
        "MERGED_INTO_CANDIDATE",
        "ALREADY_COVERED",
        "NOT_A_SKILL_PROBLEM",
        "REJECTED",
    ]
    target_skill_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class SkillBatchPlan(BaseModel):
    transaction_id: str
    side: Literal["access", "construction"]
    base_bank_version: str
    candidate_resolutions: list[CandidateResolution] = Field(
        default_factory=list
    )
    operations: list[SkillOperation] = Field(default_factory=list)


class SkillRetrievalRelation(BaseModel):
    candidate_id: str
    skill_id: str
    description_similarity: float = 0.0
    content_similarity: float = 0.0
    lexical_similarity: float = 0.0
    combined_score: float = 0.0
    forced_by_candidate: bool = False


class SkillCandidateBatch(BaseModel):
    batch_id: str
    side: Literal["access", "construction"]
    base_bank_version: str
    candidates: list[SkillCandidate] = Field(default_factory=list)
    retrieved_skill_ids: list[str] = Field(default_factory=list)
    relations: list[SkillRetrievalRelation] = Field(default_factory=list)
