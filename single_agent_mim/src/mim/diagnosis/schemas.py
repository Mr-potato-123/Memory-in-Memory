"""Stable data contracts for the three diagnosis workflows."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DiagnosisStatus(str, Enum):
    COMPLETED = "completed"
    DATA_ERROR = "data_error"
    MODEL_ERROR = "model_error"


class DiagnosisType(str, Enum):
    ANSWER_FAILURE = "ANSWER_FAILURE"
    NO_ANSWER_FAILURE = "NO_ANSWER_FAILURE"
    ACCESS_FAILURE = "ACCESS_FAILURE"
    NO_ACCESS_FAILURE = "NO_ACCESS_FAILURE"
    CONS_FAILURE = "CONS_FAILURE"
    NO_CONS_FAILURE = "NO_CONS_FAILURE"


class ClaimCoverage(str, Enum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    INCORRECT = "INCORRECT"


class DiagnosisCase(BaseModel):
    """Immutable identity and question data shared by all workflows."""

    judge_run_id: str
    diagnosis_run_id: str
    source_runtime_run: str
    conversation_id: str
    qa_id: str
    access_run_id: str
    snapshot_commit_id: int
    question: str
    reference_answer: str
    prediction: str
    judge_label: str
    judge_reason: str = ""
    gold_message_ids: list[str] = Field(default_factory=list)


class ClaimSupport(BaseModel):
    claim: str
    supporting_version_ids: list[str] = Field(default_factory=list)
    coverage: ClaimCoverage | None = None


class AccessClaimSupport(ClaimSupport):
    """Required snapshot evidence and the subset returned by fixed Mem0 search."""

    retrieved_supporting_version_ids: list[str] = Field(default_factory=list)


class BaseDiagnosisReport(BaseModel):
    schema_version: str
    diagnosis_id: str
    diagnosis_type: DiagnosisType
    status: DiagnosisStatus = DiagnosisStatus.COMPLETED
    judge_run_id: str
    diagnosis_run_id: str
    source_runtime_run: str
    conversation_id: str
    qa_id: str
    snapshot_commit_id: int
    question: str
    reference_answer: str
    problem_found: bool = False
    reason: str = ""
    confidence: float = 0.5
    review_required: bool = False
    skill_trace: dict[str, Any] | None = None
    repair_package: dict[str, Any] | None = None


class AnswerDiagnosisReport(BaseDiagnosisReport):
    """Judgment of the context actually seen by the answer model."""

    schema_version: str = "answer_diagnosis_v3"
    prediction: str = ""
    claims: list[ClaimSupport] = Field(default_factory=list)
    retrieved_version_ids: list[str] = Field(default_factory=list)
    retrieved_context_sufficient: bool = False
    unresolved_material_contradiction: bool = False
    failure_mode: str = ""
    skill_learnable: bool = False
    observable_trigger: str = ""
    corrective_operation: str = ""
    search_steps: list[dict[str, Any]] = Field(default_factory=list)


class AccessDiagnosisReport(BaseDiagnosisReport):
    """Whether useful current memories were absent from the search chain."""

    schema_version: str = "access_diagnosis_v3"
    access_run_id: str = ""
    claims: list[AccessClaimSupport] = Field(default_factory=list)
    skill_learnable: bool = False
    current_related_memories: list[dict[str, Any]] = Field(default_factory=list)
    useful_current_version_ids: list[str] = Field(default_factory=list)
    retrieved_current_version_ids: list[str] = Field(default_factory=list)
    missing_useful_current_version_ids: list[str] = Field(default_factory=list)
    search_steps: list[dict[str, Any]] = Field(default_factory=list)


class ConsScreeningReport(BaseModel):
    """Stage-A result. It never contains raw messages or construction history."""

    schema_version: str = "cons_screening_v3"
    status: DiagnosisStatus = DiagnosisStatus.COMPLETED
    claims: list[ClaimSupport] = Field(default_factory=list)
    cons_candidate: bool = False
    reason: str = ""
    confidence: float = 0.5
    review_required: bool = False


class ConsDiagnosisReport(BaseDiagnosisReport):
    """Construction diagnosis with optional Stage-B provenance."""

    schema_version: str = "cons_diagnosis_v3"
    screening: ConsScreeningReport
    raw_support: str = "NOT_CHECKED"
    source_message_ids: list[str] = Field(default_factory=list)
    affected_memory_ids: list[str] = Field(default_factory=list)
    primary_subtype: str | None = None
    first_error: dict[str, Any] | None = None
    construction_history: dict[str, Any] | None = None
    construction_skill_traces: list[dict[str, Any]] = Field(
        default_factory=list
    )
