"""Frozen evaluation using the single production SQLite Runtime."""

from __future__ import annotations

from tqdm import tqdm

from ..artifacts import RunDir
from ..config import MiMConfig
from ..eval.metrics import compute_f1
from ..schemas import Conversation, EvalReport, QAResult, Question
from ..skills import SkillBank
from .use import MiMRuntime


class MiMEvaluator:
    """Evaluate Base or MiM without invoking maintenance-side components."""

    def __init__(
        self,
        config: MiMConfig,
        run_dir: RunDir,
        *,
        runtime_model=None,
        embedder=None,
    ):
        self._config = config
        self._run_dir = run_dir
        self._runtime_model = runtime_model
        self._embedder = embedder

    def evaluate(
        self,
        conversations: list[Conversation],
        questions: dict[str, list[Question]],
        eval_ids: list[str],
        mode: str = "base",
        skill_bank_dir: str | None = None,
        runtime_skill_bank: SkillBank | None = None,
        split_name: str = "test",
    ) -> EvalReport:
        if mode == "mim" and not (skill_bank_dir or runtime_skill_bank):
            raise ValueError("MiM evaluation requires --skill-bank-dir.")

        bank = None
        if mode == "mim":
            bank = (
                runtime_skill_bank
                if runtime_skill_bank is not None
                else SkillBank.load_published(skill_bank_dir)
            )
            bank.freeze()

        runtime = MiMRuntime(
            config=self._config,
            mode=mode,
            skill_bank=bank,
            run_dir=self._run_dir,
            runtime_model=self._runtime_model,
            embedder=self._embedder,
            phase=split_name,
        )
        report = EvalReport(
            run_id=self._run_dir.run_id,
            mode=mode,
            split_name=split_name,
            output_dir=str(self._run_dir.path),
        )

        results: list[QAResult] = []
        construction_steps: list[int] = []
        for conversation in tqdm(conversations, desc=f"Evaluating ({mode})"):
            if conversation.conversation_id not in eval_ids:
                continue
            runtime.ingest(conversation)
            construction_steps.append(runtime.last_construction_steps)
            for question in questions.get(conversation.conversation_id, []):
                access = runtime.ask(question)
                f1 = (
                    compute_f1(
                        access.answer,
                        question.reference_answer,
                        question.category,
                    )
                    if not access.error else 0.0
                )
                result = QAResult(
                    conversation_id=conversation.conversation_id,
                    qa_id=question.qa_id,
                    category=question.category,
                    question=question.question,
                    reference=question.reference_answer,
                    prediction=access.answer,
                    evidence_ids=access.evidence_ids,
                    skill_ids=access.used_skill_ids,
                    f1=float(f1),
                    runtime_tokens=access.total_tokens,
                    access_steps=access.steps,
                    error=access.error,
                )
                results.append(result)
                self._run_dir.append_jsonl(
                    "qa_results.jsonl",
                    result.model_dump(mode="json"),
                )

        report.total_qa = len(results)
        report.protocol_errors = sum(1 for item in results if item.error)
        report.total_runtime_tokens = sum(item.runtime_tokens for item in results)
        report.overall_f1 = (
            sum(item.f1 for item in results) / len(results) if results else 0.0
        )
        report.avg_access_steps = (
            sum(item.access_steps for item in results) / len(results)
            if results else 0.0
        )
        report.avg_construction_steps = (
            sum(construction_steps) / len(construction_steps)
            if construction_steps else 0.0
        )

        by_category: dict[int, list[float]] = {}
        for item in results:
            if item.category is not None:
                by_category.setdefault(item.category, []).append(item.f1)
        report.category_f1 = {
            category: sum(scores) / len(scores)
            for category, scores in by_category.items()
        }

        self._run_dir.write_json(
            "summary.json",
            report.model_dump(mode="json"),
        )
        return report
