"""Public data types for the MiM system.

All Agent I/O, Memory, Skill, and Workflow structures are defined here.
No business logic lives in this module.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ── Timestamp helper ──────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "") -> str:
    return (prefix + uuid.uuid4().hex[:12]) if prefix else uuid.uuid4().hex[:12]


# ── Conversation primitives ───────────────────────────────────────

class Message(BaseModel):
    message_id: str = Field(default_factory=lambda: _new_id("msg_"))
    role: str  # "user" | "assistant"
    speaker: Optional[str] = None
    content: str
    time: Optional[str] = None  # ISO-format timestamp


class Session(BaseModel):
    session_id: str = Field(default_factory=lambda: _new_id("sess_"))
    messages: list[Message] = Field(default_factory=list)
    time: Optional[str] = None


class Conversation(BaseModel):
    conversation_id: str = Field(default_factory=lambda: _new_id("conv_"))
    sessions: list[Session] = Field(default_factory=list)


class Question(BaseModel):
    qa_id: str = Field(default_factory=lambda: _new_id("qa_"))
    question: str
    reference_answer: str
    category: Optional[int] = None  # LoCoMo category
    source_evidence: list[list[str]] = Field(default_factory=list)
    # Each entry is [session_id, message_id]


# ── Skill ─────────────────────────────────────────────────────────

class Side(str, Enum):
    CONSTRUCTION = "construction"
    ACCESS = "access"


class SkillStatus(str, Enum):
    ACTIVE = "active"
    CANDIDATE = "candidate"
    REJECTED = "rejected"


class SkillRecord(BaseModel):
    skill_id: str = Field(default_factory=lambda: _new_id("skill_"))
    version: int = 1
    side: Side
    name: str
    description: str  # for retrieval
    content: list[str] = Field(default_factory=list)
    status: SkillStatus = SkillStatus.ACTIVE
    parent_versions: list[str] = Field(default_factory=list)
    created_from_failures: list[str] = Field(default_factory=list)

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        return [str(item).strip() for item in value if str(item).strip()]


class SkillTraceItem(BaseModel):
    """Immutable snapshot of one official Skill at retrieval time."""

    skill_id: str
    version_id: str
    rank: int
    score: float
    semantic_score: float = 0.0
    lexical_score: float = 0.0
    name: str
    description: str
    content: list[str] = Field(default_factory=list)
    selected: bool = False
    rerank_rank: Optional[int] = None
    rerank_reason: str = ""


class SkillRetrievalTrace(BaseModel):
    """Official-bank retrieval observed by one Runtime Agent invocation."""

    trace_id: str
    side: Side
    bank_version: str
    query: str
    top_k: int
    disclose_k: int
    min_score: float = 0.0
    scored_fields: list[str] = Field(
        default_factory=lambda: ["name", "description"]
    )
    scoring_weights: dict[str, float] = Field(
        default_factory=lambda: {"semantic": 0.70, "bm25": 0.30}
    )
    retrieval_method: str = "bank1_hybrid_router"
    candidate_k: int = 0
    reranker: str = "none"
    reranker_error: str = ""
    selected: list[SkillTraceItem] = Field(default_factory=list)
    nearby_not_selected: list[SkillTraceItem] = Field(default_factory=list)


class SkillBankManifest(BaseModel):
    version: int
    side: str  # "construction" | "access" | "joint"
    skills: list[SkillRecord] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now_utc)
    previous_version: Optional[int] = None


# ── Agent actions ─────────────────────────────────────────────────

class AgentAction(BaseModel):
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class AccessResult(BaseModel):
    answer: str
    evidence_ids: list[str] = Field(default_factory=list)
    search_trace: list[AgentAction] = Field(default_factory=list)
    used_skill_ids: list[str] = Field(default_factory=list)
    skill_trace: SkillRetrievalTrace | None = None
    access_run_id: str = ""
    answer_prompt_hash: str = ""
    visible_memories: list[dict[str, Any]] = Field(default_factory=list)
    action_records: list[dict[str, Any]] = Field(default_factory=list)
    total_tokens: int = 0
    latency_ms: int = 0
    error: Optional[str] = None
    steps: int = 0


class QAResult(BaseModel):
    conversation_id: str
    qa_id: str
    category: Optional[int] = None
    question: str
    reference: str
    prediction: str
    evidence_ids: list[str] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)
    f1: float = 0.0
    runtime_tokens: int = 0
    access_steps: int = 0
    error: Optional[str] = None


# ── Model response ────────────────────────────────────────────────

class ModelResponse(BaseModel):
    text: str
    provider: str
    model: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    latency_ms: int = 0
    finish_reason: Optional[str] = None


# ── Dataset splits ────────────────────────────────────────────────

class DatasetSplit(BaseModel):
    dataset_sha256: str
    seed: int
    train: list[str] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    test: list[str] = Field(default_factory=list)


# ── Training summary ──────────────────────────────────────────────

class TrainResult(BaseModel):
    run_id: str
    conversations_processed: int
    total_qa: int
    failures_detected: int
    construction_failures: int = 0
    access_failures: int = 0
    other_failures: int = 0
    invalid_failures: int = 0
    candidates_generated: int = 0
    candidates_accepted: int = 0
    candidates_rejected: int = 0
    bank_versions: list[int] = Field(default_factory=list)
    selected_version: Optional[int] = None
    validation_best_f1: float = 0.0
    output_dir: str = ""


class EvalReport(BaseModel):
    run_id: str
    mode: str  # "base" | "mim"
    split_name: str  # "test" | "validation"
    overall_f1: float = 0.0
    category_f1: dict[int, float] = Field(default_factory=dict)
    total_qa: int = 0
    protocol_errors: int = 0
    total_runtime_tokens: int = 0
    total_maintenance_tokens: int = 0
    avg_construction_steps: float = 0.0
    avg_access_steps: float = 0.0
    output_dir: str = ""


# ── Hash helpers ──────────────────────────────────────────────────

def hash_dict(d: dict) -> str:
    """Stable SHA-256 hash of a JSON-serializable dict."""
    raw = json.dumps(d, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def hash_file(path: str) -> str:
    """SHA-256 of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]
