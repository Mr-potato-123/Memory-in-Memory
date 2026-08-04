"""Structured trace recording for Construction and Access operations.

Traces are saved as JSONL files per run, providing the evidence chain
that Failure Agent needs for attribution.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


@dataclass
class ConstructionTrace:
    """Record of one session's Construction run."""
    conversation_id: str
    session_id: str
    base_commit_id: int | None
    commit_id: int | None = None
    commit_status: str = "pending"  # pending|committed|failed
    skill_ids: list[str] = field(default_factory=list)
    skill_trace: dict[str, Any] = field(default_factory=dict)
    candidates_count: int = 0
    decisions: list[dict] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    error_message: str = ""
    started_at: str = field(default_factory=_now_iso)
    finished_at: str = ""

    def to_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "base_commit_id": self.base_commit_id,
            "commit_id": self.commit_id,
            "commit_status": self.commit_status,
            "skill_ids": self.skill_ids,
            "skill_trace": self.skill_trace,
            "candidates_count": self.candidates_count,
            "decisions": self.decisions,
            "validation_errors": self.validation_errors,
            "token_usage": self.token_usage,
            "latency_ms": self.latency_ms,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at or _now_iso(),
        }


@dataclass
class AccessTrace:
    """Record of one QA's Access run."""
    conversation_id: str
    qa_id: str
    snapshot_commit_id: int
    question: str
    skill_ids: list[str] = field(default_factory=list)
    skill_trace: dict[str, Any] = field(default_factory=dict)
    actions: list[dict] = field(default_factory=list)
    visible_evidence_ids: list[str] = field(default_factory=list)
    final_evidence_ids: list[str] = field(default_factory=list)
    answer: str = ""
    reference: str = ""
    f1: float = 0.0
    token_usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    error: str = ""
    started_at: str = field(default_factory=_now_iso)
    finished_at: str = ""

    def to_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "qa_id": self.qa_id,
            "snapshot_commit_id": self.snapshot_commit_id,
            "question": self.question,
            "skill_ids": self.skill_ids,
            "skill_trace": self.skill_trace,
            "actions": self.actions,
            "visible_evidence_ids": self.visible_evidence_ids,
            "final_evidence_ids": self.final_evidence_ids,
            "answer": self.answer,
            "reference": self.reference,
            "f1": self.f1,
            "token_usage": self.token_usage,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at or _now_iso(),
        }


class TraceRecorder:
    """Append-only JSONL trace writer (thread-safe for parallel answering)."""

    _WRITE_LOCK = threading.Lock()

    def __init__(self, output_dir: str | Path):
        self._dir = Path(output_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def record_construction(self, trace: ConstructionTrace):
        trace.finished_at = _now_iso()
        self._append("construction_traces.jsonl", trace.to_dict())

    def record_access(self, trace: AccessTrace):
        trace.finished_at = _now_iso()
        self._append("access_traces.jsonl", trace.to_dict())

    def _append(self, filename: str, record: dict):
        path = self._dir / filename
        with TraceRecorder._WRITE_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
