"""Legacy V2 workflow kept for existing online-training compatibility.

New offline diagnosis must use :mod:`mim.diagnosis.workflows`. This module
still performs the retired re-answer check and does not implement V3 evidence
isolation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..agents.access_diagnosis import AccessDiagnosisAgent
from ..agents.construction_diagnosis import ConstructionDiagnosisAgent
from ..agents.failure import AnswerCheckAgent
from .provenance import ProvenanceService
from .schemas import IndependentDiagnosisResult


class FailureWorkflow:
    """Prepare deterministic evidence and run both diagnosis agents."""

    def __init__(
        self,
        *,
        access_agent: AccessDiagnosisAgent,
        construction_agent: ConstructionDiagnosisAgent,
        answer_check_agent: AnswerCheckAgent,
        provenance: ProvenanceService,
        output_dir: str | Path = "",
    ):
        self._access_agent = access_agent
        self._construction_agent = construction_agent
        self._answer_check_agent = answer_check_agent
        self._provenance = provenance
        self._output_dir = Path(output_dir) if output_dir else None
        if self._output_dir is not None:
            self._output_dir.mkdir(parents=True, exist_ok=True)

    def analyze(
        self,
        *,
        failure_id: str,
        conversation_id: str,
        qa_id: str,
        snapshot_commit_id: int,
        access_run_id: str,
        question: str,
        prediction: str,
        reference_answer: str,
        gold_message_ids: list[str],
        returned_memories: list[dict[str, Any]],
        run_id: str = "",
        source_messages: list[dict[str, Any]] | None = None,
    ) -> IndependentDiagnosisResult:
        """Return two reports without reducing them to one failure label."""
        search_steps = self._provenance.access_search_chain(access_run_id)
        construction_history = self._provenance.construction_history(
            conversation_id=conversation_id,
            message_ids=gold_message_ids,
            snapshot_commit_id=snapshot_commit_id,
        )
        relevant_snapshot_memories = construction_history.get(
            "snapshot_memories", []
        )

        access_report = self._access_agent.diagnose(
            failure_id=failure_id,
            run_id=run_id,
            conversation_id=conversation_id,
            qa_id=qa_id,
            access_run_id=access_run_id,
            snapshot_commit_id=snapshot_commit_id,
            question=question,
            prediction=prediction,
            reference_answer=reference_answer,
            relevant_snapshot_memories=relevant_snapshot_memories,
            search_steps=search_steps,
        )

        construction_report = self._construction_agent.diagnose(
            failure_id=failure_id,
            run_id=run_id,
            conversation_id=conversation_id,
            qa_id=qa_id,
            snapshot_commit_id=snapshot_commit_id,
            question=question,
            prediction=prediction,
            reference_answer=reference_answer,
            raw_message_ids=gold_message_ids,
            source_messages=source_messages or [],
            construction_history=construction_history,
        )

        # This check is informational only. It does not alter either diagnosis.
        answer_check = self._answer_check_agent.check(
            question=question,
            reference_answer=reference_answer,
            returned_memories=returned_memories,
        )

        result = IndependentDiagnosisResult(
            failure_id=failure_id,
            access=access_report,
            construction=construction_report,
            answer_check=answer_check,
        )
        self._save(result)
        return result

    def _save(self, result: IndependentDiagnosisResult) -> None:
        if self._output_dir is None:
            return
        self._write_json(
            self._output_dir / f"{result.access.failure_id}_report.json",
            result.access.model_dump(mode="json"),
        )
        self._write_json(
            self._output_dir
            / f"{result.construction.failure_id}_report.json",
            result.construction.model_dump(mode="json"),
        )
        self._write_json(
            self._output_dir / f"{result.failure_id}_diagnoses.json",
            result.model_dump(mode="json"),
        )

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
