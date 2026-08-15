"""Permission-enforcing orchestration for the three diagnosis workflows."""

from __future__ import annotations

from ..agents.access_failure import AccessFailureAgent
from ..agents.answer_failure import AnswerFailureAgent
from ..agents.cons_failure import ConsFailureAgent
from .evidence import DiagnosisEvidenceRepository
from .schemas import (
    AccessDiagnosisReport,
    AnswerDiagnosisReport,
    ConsDiagnosisReport,
    DiagnosisCase,
    DiagnosisStatus,
)


class AnswerDiagnosisWorkflow:
    """Run first, using only the context visible to the runtime answer model."""

    def __init__(
        self,
        *,
        agent: AnswerFailureAgent,
        evidence: DiagnosisEvidenceRepository,
    ):
        self._agent = agent
        self._evidence = evidence

    def run(self, case: DiagnosisCase) -> AnswerDiagnosisReport:
        exact_steps = self._evidence.exact_runtime_search_chain(
            case.access_run_id
        )
        report = self._agent.diagnose(
            case,
            exact_search_steps=exact_steps,
        )
        trace_loader = getattr(self._evidence, "access_skill_trace", None)
        report.skill_trace = (
            trace_loader(case.access_run_id)
            if callable(trace_loader)
            else {}
        )
        if report.repair_package is not None:
            report.repair_package["skill_trace"] = report.skill_trace
        return report


class AccessDiagnosisWorkflow:
    """Run independently, with current memory and no construction history."""

    def __init__(
        self,
        *,
        agent: AccessFailureAgent,
        evidence: DiagnosisEvidenceRepository,
    ):
        self._agent = agent
        self._evidence = evidence

    def run(
        self,
        case: DiagnosisCase,
        *,
        answer_context_sufficient: bool = False,
    ) -> AccessDiagnosisReport:
        current_memories = self._evidence.current_related_memories(
            conversation_id=case.conversation_id,
            message_ids=case.gold_message_ids,
            snapshot_commit_id=case.snapshot_commit_id,
        )
        current_steps = self._evidence.current_access_search_chain(
            access_run_id=case.access_run_id,
            conversation_id=case.conversation_id,
            snapshot_commit_id=case.snapshot_commit_id,
        )
        report = self._agent.diagnose(
            case,
            current_related_memories=current_memories,
            current_search_steps=current_steps,
            answer_context_sufficient=answer_context_sufficient,
        )
        trace_loader = getattr(self._evidence, "access_skill_trace", None)
        report.skill_trace = (
            trace_loader(case.access_run_id)
            if callable(trace_loader)
            else {}
        )
        if report.repair_package is not None:
            report.repair_package["skill_trace"] = report.skill_trace
        return report


class ConsDiagnosisWorkflow:
    """Screen current memory, then load raw/history only for candidates."""

    def __init__(
        self,
        *,
        agent: ConsFailureAgent,
        evidence: DiagnosisEvidenceRepository,
    ):
        self._agent = agent
        self._evidence = evidence

    def run(self, case: DiagnosisCase) -> ConsDiagnosisReport:
        trace_loader = getattr(
            self._evidence, "construction_skill_traces", None
        )
        skill_traces = (
            trace_loader(
                conversation_id=case.conversation_id,
                message_ids=case.gold_message_ids,
                snapshot_commit_id=case.snapshot_commit_id,
            )
            if callable(trace_loader)
            else []
        )
        current_memories = self._evidence.current_related_memories(
            conversation_id=case.conversation_id,
            message_ids=case.gold_message_ids,
            snapshot_commit_id=case.snapshot_commit_id,
        )
        screening = self._agent.screen(
            case,
            current_related_memories=current_memories,
        )
        if screening.status != DiagnosisStatus.COMPLETED:
            report = self._agent.no_problem(case, screening)
            report.construction_skill_traces = skill_traces
            report.status = screening.status
            return report
        if not screening.cons_candidate:
            report = self._agent.no_problem(case, screening)
            report.construction_skill_traces = skill_traces
            return report

        # Progressive disclosure boundary: raw messages and version history
        # are not even loaded until the current-memory screening finds a gap.
        source_messages = self._evidence.source_messages(
            conversation_id=case.conversation_id,
            message_ids=case.gold_message_ids,
        )
        if len(source_messages) != len(set(case.gold_message_ids)):
            report = self._agent.no_problem(case, screening)
            report.status = DiagnosisStatus.DATA_ERROR
            report.reason = (
                "One or more annotated evidence messages could not be resolved."
            )
            report.review_required = True
            report.construction_skill_traces = skill_traces
            return report

        history = self._evidence.construction_history(
            conversation_id=case.conversation_id,
            message_ids=case.gold_message_ids,
            snapshot_commit_id=case.snapshot_commit_id,
        )
        report = self._agent.trace(
            case,
            screening=screening,
            current_related_memories=current_memories,
            source_messages=source_messages,
            construction_history=history,
        )
        report.construction_skill_traces = skill_traces
        if report.repair_package is not None:
            report.repair_package["skill_traces"] = skill_traces
        return report
