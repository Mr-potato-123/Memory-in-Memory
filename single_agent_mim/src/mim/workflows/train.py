"""Training workflow using the single SQLite Runtime and new maintenance side."""

from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

from ..agents.access_diagnosis import AccessDiagnosisAgent
from ..agents.construction_diagnosis import ConstructionDiagnosisAgent
from ..agents.failure import AnswerCheckAgent
from ..agents.skill_learning import (
    BatchSkillCrudAgent,
    CandidateSkillAgent,
)
from ..artifacts import RunDir
from ..config import MiMConfig
from ..eval.metrics import compute_f1
from ..failure import ProvenanceService
from ..diagnosis.evidence import DiagnosisEvidenceRepository
from ..failure.workflow import FailureWorkflow
from ..failure.schemas import DiagnosisStatus, LearningRoute
from ..llm import create_client
from ..retrieval.embedder import Embedder
from ..schemas import Conversation, QAResult, Question, TrainResult
from ..skill_maker import SkillRepository
from ..skill_maker.batch import (
    BatchSkillRetriever,
    CandidateClusterer,
    SkillCrudExecutor,
)
from ..skill_maker.models import SkillBatchPlan, SkillCandidateBatch
from ..skill_maker.pipeline import SkillBankPipeline
from ..skills import SkillBank
from .evaluate import MiMEvaluator
from .use import MiMRuntime


def _read_prompt(path: str) -> str:
    prompt_path = Path(path)
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file does not exist: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


class MiMTrainer:
    """Serial, conversation-level Skill learning.

    Runtime facts live in ``state/memory.sqlite3``. Failure reports, replay
    traces and Skill versions are separate artifacts under the same run.
    """

    def __init__(
        self,
        config: MiMConfig,
        run_dir: RunDir,
        *,
        runtime_model=None,
        maintenance_model=None,
        embedder: Embedder | None = None,
    ):
        self._config = config
        self._run_dir = run_dir
        self._runtime_model = runtime_model or create_client(
            config.models["runtime"]
        )
        self._maintenance_model = maintenance_model or create_client(
            config.models["maintenance"]
        )
        self._embedder = embedder or Embedder(
            model_name=config.embedding.model,
            device=config.embedding.device,
        )

        self._repository = SkillRepository(run_dir.skills_dir())
        self._bank = SkillBank.from_repository(self._repository)

        self._runtime = MiMRuntime(
            config=config,
            mode="mim",
            skill_bank=self._bank,
            run_dir=run_dir,
            embedder=self._embedder,
            runtime_model=self._runtime_model,
            phase="train",
        )

        self._access_diagnosis_agent = AccessDiagnosisAgent(
            self._maintenance_model,
            prompt=_read_prompt(config.prompts.failure_access_diagnosis),
        )
        self._construction_diagnosis_agent = ConstructionDiagnosisAgent(
            self._maintenance_model,
            prompt=_read_prompt(
                config.prompts.failure_construction_diagnosis
            ),
        )
        self._answer_check_agent = AnswerCheckAgent(
            self._maintenance_model,
            blind_reanswer_prompt=_read_prompt(
                config.prompts.failure_blind_reanswer
            ),
        )
        self._candidate_skill_agent_access = CandidateSkillAgent(
            self._maintenance_model,
            prompt=_read_prompt(
                config.prompts.skill_candidate_generation_access
            ),
        )
        self._candidate_skill_agent_construction = CandidateSkillAgent(
            self._maintenance_model,
            prompt=_read_prompt(
                config.prompts.skill_candidate_generation_construction
            ),
        )
        self._batch_crud_agent_access = BatchSkillCrudAgent(
            self._maintenance_model,
            prompt=_read_prompt(config.prompts.skill_batch_crud_access),
        )
        self._batch_crud_agent_construction = BatchSkillCrudAgent(
            self._maintenance_model,
            prompt=_read_prompt(config.prompts.skill_batch_crud_construction),
        )
        self._candidate_clusterer = CandidateClusterer(
            self._embedder,
            target_cluster_size=config.training.skill_cluster_target_size,
            max_batch_size=config.training.skill_crud_batch_size,
        )
        self._batch_skill_retriever = BatchSkillRetriever(
            self._embedder,
            max_bank_context=config.training.skill_batch_bank_context,
        )
        self._skill_crud_executor = SkillCrudExecutor(self._repository)

    def train(
        self,
        conversations: list[Conversation],
        questions: dict[str, list[Question]],
        train_ids: list[str],
        validation_ids: list[str],
        initial_skill_bank: str | None = None,
    ) -> TrainResult:
        if initial_skill_bank:
            self._import_initial_bank(initial_skill_bank)

        result = TrainResult(
            run_id=self._run_dir.run_id,
            conversations_processed=0,
            total_qa=0,
            failures_detected=0,
            output_dir=str(self._run_dir.path),
        )
        conversation_map = {
            conversation.conversation_id: conversation
            for conversation in conversations
        }

        # Collect candidate generation jobs for parallel execution
        pending_candidates: list[tuple[str, dict, CandidateSkillAgent]] = []

        for conversation_id in tqdm(train_ids, desc="Train conversations"):
            conversation = conversation_map.get(conversation_id)
            if conversation is None:
                continue
            self._runtime.ingest(conversation)
            result.conversations_processed += 1

            for question in questions.get(conversation_id, []):
                result.total_qa += 1
                access = self._runtime.ask(question)
                f1 = (
                    compute_f1(access.answer, question.reference_answer)
                    if not access.error else 0.0
                )
                qa_result = QAResult(
                    conversation_id=conversation_id,
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
                self._run_dir.append_jsonl(
                    "qa_results.jsonl",
                    qa_result.model_dump(mode="json"),
                )
                if access.error or f1 >= 0.5:
                    continue

                result.failures_detected += 1
                gold_message_ids = [
                    item[-1]
                    for item in question.source_evidence
                    if item and item[-1]
                ]
                source_messages = self._runtime.store.get_source_messages(
                    conversation_id,
                    gold_message_ids,
                )

                connection = self._runtime.store.open_read_connection()
                try:
                    workflow = FailureWorkflow(
                        access_agent=self._access_diagnosis_agent,
                        construction_agent=(
                            self._construction_diagnosis_agent
                        ),
                        answer_check_agent=self._answer_check_agent,
                        provenance=ProvenanceService(connection),
                        output_dir=self._run_dir.failures_dir(),
                    )
                    diagnoses = workflow.analyze(
                        failure_id=(
                            f"failure_{conversation_id}_{question.qa_id}"
                        ),
                        run_id=self._run_dir.run_id,
                        conversation_id=conversation_id,
                        qa_id=question.qa_id,
                        snapshot_commit_id=self._runtime.latest_commit_id or 0,
                        access_run_id=access.access_run_id,
                        question=question.question,
                        prediction=access.answer,
                        reference_answer=question.reference_answer,
                        gold_message_ids=gold_message_ids,
                        returned_memories=access.visible_memories,
                        source_messages=source_messages,
                    )
                    construction_skill_traces = (
                        DiagnosisEvidenceRepository(
                            connection
                        ).construction_skill_traces(
                            conversation_id=conversation_id,
                            message_ids=gold_message_ids,
                            snapshot_commit_id=(
                                self._runtime.latest_commit_id or 0
                            ),
                        )
                    )
                finally:
                    connection.close()

                diagnoses.access.skill_trace = (
                    access.skill_trace.model_dump(mode="json")
                    if access.skill_trace
                    else {}
                )
                diagnoses.construction.construction_skill_traces = (
                    construction_skill_traces
                )

                reports = (
                    ("access", diagnoses.access),
                    ("construction", diagnoses.construction),
                )
                for side, report in reports:
                    self._run_dir.append_jsonl(
                        "failures/index.jsonl",
                        {
                            "side": side,
                            **report.model_dump(mode="json"),
                        },
                    )
                    if report.problem_found:
                        if side == "access":
                            result.access_failures += 1
                        else:
                            result.construction_failures += 1
                    elif (
                        side == "construction"
                        and report.status == DiagnosisStatus.DATA_ISSUE
                    ):
                        result.invalid_failures += 1

                    if report.recommended_route not in {
                        LearningRoute.ACCESS_SKILL_MAKER,
                        LearningRoute.CONSTRUCTION_SKILL_MAKER,
                    }:
                        continue

                    agent = (
                        self._candidate_skill_agent_access
                        if side == "access"
                        else self._candidate_skill_agent_construction
                    )
                    pending_candidates.append(
                        (side, report.model_dump(mode="json"), agent)
                    )

                if (
                    not diagnoses.access.problem_found
                    and not diagnoses.construction.problem_found
                    and diagnoses.answer_check.get("correct") is True
                ):
                    result.other_failures += 1

        # ── Parallel candidate generation ──────────────────────────
        if pending_candidates:
            print(
                f"\nGenerating {len(pending_candidates)} candidates "
                f"with {getattr(self._maintenance_model, '_clients', [self._maintenance_model])} "
                f"parallel workers...",
                flush=True,
            )
            # Determine worker count from the round-robin client pool
            if hasattr(self._maintenance_model, '_clients'):
                workers = min(len(self._maintenance_model._clients) * 2, 8)
            else:
                workers = 2

            def _gen_candidate(
                side: str, diagnosis: dict, agent: CandidateSkillAgent,
            ) -> dict:
                try:
                    candidate = agent.generate(diagnosis=diagnosis, side=side)
                    return {"status": "ok", "candidate": candidate}
                except Exception as exc:
                    return {"status": "error", "error": str(exc)}

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_gen_candidate, side, diag, agent): (side, diag)
                    for side, diag, agent in pending_candidates
                }
                for future in tqdm(
                    as_completed(futures),
                    total=len(futures),
                    desc="Candidate generation",
                ):
                    side, diagnosis = futures[future]
                    outcome = future.result()
                    diag_id = str(
                        diagnosis.get("diagnosis_id")
                        or diagnosis.get("failure_id")
                        or "unknown"
                    )
                    if outcome["status"] == "error":
                        result.candidates_rejected += 1
                        self._run_dir.append_jsonl(
                            f"skills/candidates/{side}/generation_errors.jsonl",
                            {"diagnosis_id": diag_id, "error": outcome["error"]},
                        )
                    elif outcome["candidate"] is None:
                        self._run_dir.append_jsonl(
                            f"skills/candidates/{side}/no_change.jsonl",
                            {"diagnosis_id": diag_id,
                             "reason": "Candidate Agent returned no change."},
                        )
                    else:
                        self._repository.save_candidate(outcome["candidate"])
                        result.candidates_generated += 1
                        self._run_dir.append_jsonl(
                            f"skills/candidates/{side}/index.jsonl",
                            outcome["candidate"].model_dump(mode="json"),
                        )

        self._consolidate_candidates(result)

        if validation_ids:
            best_version, best_f1, scores = self._validate_versions(
                conversations,
                questions,
                validation_ids,
            )
            result.selected_version = best_version
            result.validation_best_f1 = best_f1
            selected_path = self._repository.select_version(best_version)
            self._run_dir.write_json(
                "skills/validation_scores.json",
                scores,
            )
        else:
            selected = int(self._repository.current_version.removeprefix("v"))
            selected_path = self._repository.select_version(selected)
            result.selected_version = selected

        SkillBank.export_published(
            selected_path,
            self._run_dir.skills_dir() / "published_bank1",
            bank_number=1,
        )

        result.bank_versions = sorted(set(result.bank_versions))
        self._run_dir.write_json(
            "summary.json",
            result.model_dump(mode="json"),
        )
        return result

    def _consolidate_candidates(self, result: TrainResult) -> None:
        """Plan semantic batches against one frozen bank, then publish once.

        Delegates to the reusable ``SkillBankPipeline`` shared with the
        standalone Judge-first entry point.
        """
        pipeline = SkillBankPipeline(
            repository=self._repository,
            clusterer=self._candidate_clusterer,
            retriever=self._batch_skill_retriever,
            executor=self._skill_crud_executor,
            run_id=self._run_dir.run_id,
        )
        for side in ("access", "construction"):
            batch_agent = (
                self._batch_crud_agent_access
                if side == "access"
                else self._batch_crud_agent_construction
            )
            outcome = pipeline.consolidate(
                side=side,
                batch_crud_agent=batch_agent,
                artifact_writer=(
                    lambda path, data: self._run_dir.write_json(path, data)
                ),
            )
            result.candidates_accepted += outcome["accepted"]
            result.candidates_rejected += outcome["rejected"]
            if outcome["published"] and outcome["new_version"] is not None:
                result.bank_versions.append(
                    int(outcome["new_version"].removeprefix("v"))
                )
                self._write_crud_errors(side, outcome, result)

    def _write_crud_errors(
        self,
        side: str,
        outcome: dict,
        result: TrainResult,
    ) -> None:
        """Record any CRUD or conflict errors in the run directory."""
        for error in outcome.get("errors", []):
            self._run_dir.append_jsonl(
                f"skills/transactions/{side}/errors.jsonl",
                error,
            )

    def _validate_versions(
        self,
        conversations: list[Conversation],
        questions: dict[str, list[Question]],
        validation_ids: list[str],
    ) -> tuple[int, float, list[dict]]:
        current = int(self._repository.current_version.removeprefix("v"))
        best_version = 0
        best_f1 = -1.0
        scores: list[dict] = []
        for version in range(current + 1):
            selected_path = self._repository.select_version(version)
            selected_data = json.loads(
                selected_path.read_text(encoding="utf-8")
            )
            runtime_bank = SkillBank.from_records(
                selected_data.get("skills", []),
                bank_name=f"training_candidate_{version:03d}",
            )
            with tempfile.TemporaryDirectory(prefix="mim_validation_") as temp:
                validation_run = RunDir.create(
                    f"bank_v{version:03d}",
                    temp,
                )
                evaluator = MiMEvaluator(
                    self._config,
                    validation_run,
                    runtime_model=self._runtime_model,
                    embedder=self._embedder,
                )
                report = evaluator.evaluate(
                    conversations=conversations,
                    questions=questions,
                    eval_ids=validation_ids,
                    mode="mim",
                    runtime_skill_bank=runtime_bank,
                    split_name="validation",
                )
            scores.append({
                "bank_version": version,
                "overall_f1": report.overall_f1,
                "total_qa": report.total_qa,
            })
            if report.overall_f1 > best_f1:
                best_version = version
                best_f1 = report.overall_f1
        return best_version, max(best_f1, 0.0), scores

    def _import_initial_bank(self, path: str):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if self._repository.current_version != "v000":
            raise RuntimeError(
                "Initial Skill Bank can only be imported into an empty run."
            )
        # New-format banks can be copied by publishing each active payload.
        from ..skill_maker.models import SkillCandidate, SkillPayload
        for item in data.get("skills", []):
            payload_data = item.get("payload") or {
                key: item.get(key, "") for key in ("name", "description", "content")
            }
            candidate = SkillCandidate(
                candidate_id=f"import_{item.get('skill_id', 'skill')}",
                skill_id=item.get("skill_id", ""),
                version=int(item.get("version", 1)),
                side=item.get("side", "access"),
                payload=SkillPayload(**payload_data),
                source_failure_id="initial_import",
            )
            self._repository.publish(
                self._repository.stage_create(candidate)
            )
