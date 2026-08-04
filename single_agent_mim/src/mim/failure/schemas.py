"""Data contracts for two independent diagnosis reports."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class LearningRoute(str, Enum):
    CONSTRUCTION_SKILL_MAKER = "construction_skill_maker"
    ACCESS_SKILL_MAKER = "access_skill_maker"
    ENGINEERING_ISSUE = "engineering_issue"
    RECORD_ONLY = "record_only"


class DiagnosisStatus(str, Enum):
    COMPLETED = "completed"
    DATA_ISSUE = "data_issue"
    ENGINEERING_ISSUE = "engineering_issue"
    MODEL_ERROR = "model_error"


class AccessDiagnosisReport(BaseModel):
    """Independent result from the retrieval diagnosis agent."""

    failure_id: str
    run_id: str = ""
    conversation_id: str = ""
    qa_id: str = ""
    access_run_id: str = ""
    snapshot_commit_id: int = 0
    question: str = ""
    prediction: str = ""
    reference_answer: str = ""

    status: DiagnosisStatus = DiagnosisStatus.COMPLETED
    problem_found: bool = False
    confidence: float = 0.5
    primary_subtype: Optional[str] = None
    first_broken_edge: Optional[str] = None
    reason: str = ""
    review_required: bool = False

    relevant_snapshot_memories: list[dict[str, Any]] = Field(
        default_factory=list
    )
    search_steps: list[dict[str, Any]] = Field(default_factory=list)
    necessary_available_version_ids: list[str] = Field(default_factory=list)
    returned_necessary_version_ids: list[str] = Field(default_factory=list)
    missing_necessary_version_ids: list[str] = Field(default_factory=list)
    conflicting_returned_version_ids: list[str] = Field(default_factory=list)
    repair_package: dict[str, Any] = Field(default_factory=dict)
    skill_trace: dict[str, Any] = Field(default_factory=dict)
    recommended_route: LearningRoute = LearningRoute.RECORD_ONLY


class ConstructionDiagnosisReport(BaseModel):
    """Independent result from the memory-construction diagnosis agent."""

    failure_id: str
    run_id: str = ""
    conversation_id: str = ""
    qa_id: str = ""
    snapshot_commit_id: int = 0
    question: str = ""
    prediction: str = ""
    reference_answer: str = ""

    status: DiagnosisStatus = DiagnosisStatus.COMPLETED
    problem_found: bool = False
    confidence: float = 0.5
    primary_subtype: Optional[str] = None
    first_broken_edge: Optional[str] = None
    reason: str = ""
    review_required: bool = False
    raw_support: str = "UNKNOWN"

    raw_message_ids: list[str] = Field(default_factory=list)
    source_messages: list[dict[str, Any]] = Field(default_factory=list)
    construction_history: dict[str, Any] = Field(default_factory=dict)
    first_error: dict[str, Any] = Field(default_factory=dict)
    repair_package: dict[str, Any] = Field(default_factory=dict)
    construction_skill_traces: list[dict[str, Any]] = Field(
        default_factory=list
    )
    recommended_route: LearningRoute = LearningRoute.RECORD_ONLY


class IndependentDiagnosisResult(BaseModel):
    """Container only; intentionally no combined failure label or route."""

    failure_id: str
    access: AccessDiagnosisReport
    construction: ConstructionDiagnosisReport
    answer_check: dict[str, Any] = Field(default_factory=dict)
