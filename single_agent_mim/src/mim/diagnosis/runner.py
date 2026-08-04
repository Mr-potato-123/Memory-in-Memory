"""Shared bounded-concurrency runner for one diagnosis component."""

from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel

from ..agents.access_failure import AccessFailureAgent
from ..agents.answer_failure import AnswerFailureAgent
from ..agents.cons_failure import ConsFailureAgent
from ..config import MiMConfig, load_config
from ..eval.locomo import load_dataset
from ..llm import create_client
from .artifacts import DiagnosisArtifactStore, utc_now
from .evidence import DiagnosisEvidenceRepository
from .schemas import DiagnosisCase, DiagnosisStatus
from .workflows import (
    AccessDiagnosisWorkflow,
    AnswerDiagnosisWorkflow,
    ConsDiagnosisWorkflow,
)


@dataclass(frozen=True)
class WorkItem:
    conversation_id: str
    qa_id: str
    db_path: Path
    source_runtime_run: str
    question: str
    reference_answer: str
    prediction: str
    judge_label: str
    judge_reason: str
    gold_message_ids: tuple[str, ...]


class ComponentWorker:
    """Thread-local model client plus per-item read-only database."""

    def __init__(
        self,
        *,
        component: str,
        config: MiMConfig,
        prompts: dict[str, str],
        judge_run_id: str,
        diagnosis_run_id: str,
    ):
        self._component = component
        self._config = config
        self._prompts = prompts
        self._judge_run_id = judge_run_id
        self._diagnosis_run_id = diagnosis_run_id
        self._local = threading.local()

    def run(self, item: WorkItem) -> BaseModel:
        last_report: BaseModel | None = None
        for _attempt in range(3):
            conn = self._open_readonly(item.db_path)
            try:
                access_row = conn.execute(
                    """SELECT access_run_id, snapshot_commit_id
                       FROM access_runs
                       WHERE conversation_id=?
                         AND qa_id=?
                         AND status='completed'
                       ORDER BY created_at DESC
                       LIMIT 1""",
                    (item.conversation_id, item.qa_id),
                ).fetchone()
                if access_row is None:
                    raise ValueError(
                        "No completed access run exists for this QA."
                    )
                case = DiagnosisCase(
                    judge_run_id=self._judge_run_id,
                    diagnosis_run_id=self._diagnosis_run_id,
                    source_runtime_run=item.source_runtime_run,
                    conversation_id=item.conversation_id,
                    qa_id=item.qa_id,
                    access_run_id=str(access_row["access_run_id"]),
                    snapshot_commit_id=int(access_row["snapshot_commit_id"]),
                    question=item.question,
                    reference_answer=item.reference_answer,
                    prediction=item.prediction,
                    judge_label=item.judge_label,
                    judge_reason=item.judge_reason,
                    gold_message_ids=list(item.gold_message_ids),
                )
                evidence = DiagnosisEvidenceRepository(conn)
                report = self._workflow(evidence).run(case)
            finally:
                conn.close()
            last_report = report
            if report.status != DiagnosisStatus.MODEL_ERROR:
                return report
        assert last_report is not None
        return last_report

    def _workflow(self, evidence: DiagnosisEvidenceRepository):
        model = self._model()
        if self._component == "answer":
            return AnswerDiagnosisWorkflow(
                agent=AnswerFailureAgent(
                    model,
                    prompt=self._prompts["answer"],
                ),
                evidence=evidence,
            )
        if self._component == "access":
            return AccessDiagnosisWorkflow(
                agent=AccessFailureAgent(
                    model,
                    prompt=self._prompts["access"],
                ),
                evidence=evidence,
            )
        return ConsDiagnosisWorkflow(
            agent=ConsFailureAgent(
                model,
                screening_prompt=self._prompts["cons_screening"],
                trace_prompt=self._prompts["cons_trace"],
            ),
            evidence=evidence,
        )

    def _model(self):
        model = getattr(self._local, "model", None)
        if model is None:
            model_config = copy.deepcopy(self._config.models["maintenance"])
            model_config.supports_json_mode = True
            # Diagnosis is a strict structured classification task. Flash
            # thinking can consume the output budget before emitting JSON.
            model_config.extra_body = {"thinking": {"type": "disabled"}}
            model_config.reasoning_effort = None
            model = create_client(model_config)
            self._local.model = model
        return model

    @staticmethod
    def _open_readonly(path: Path) -> sqlite3.Connection:
        if not path.exists():
            raise FileNotFoundError(f"Memory database not found: {path}")
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn


def build_parser(component: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Run isolated {component} diagnosis."
    )
    parser.add_argument(
        "--config",
        default="configs/qwen3_8b_dashscope.yaml",
    )
    parser.add_argument("--judge-results", required=True)
    parser.add_argument("--diagnosis-run-id", required=True)
    parser.add_argument(
        "--output-root",
        required=True,
        help="Shared v3 root. Each component writes only its own subdirectory.",
    )
    parser.add_argument(
        "--source-run",
        action="append",
        required=True,
        metavar="CONVERSATION_ID=PATH",
        help="Repeat once for every source runtime run.",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    return parser


def run_component(component: str, argv: list[str] | None = None) -> int:
    if component not in {"answer", "access", "cons"}:
        raise ValueError(f"Unknown component: {component}")
    args = build_parser(component).parse_args(argv)
    if not 1 <= args.workers <= 24:
        raise SystemExit("--workers must be between 1 and 24.")

    config = load_config(args.config)
    judge_path = Path(args.judge_results)
    output_root = Path(args.output_root)
    source_runs = _parse_source_runs(args.source_run)
    judge_rows = _load_jsonl(judge_path)
    work = _build_work_items(
        config=config,
        source_runs=source_runs,
        judge_rows=judge_rows,
    )
    if args.max_items > 0:
        work = work[: args.max_items]

    if component != "answer":
        _require_answer_phase(output_root, work)

    store = DiagnosisArtifactStore(
        output_root,
        component=component,
        resume=args.resume,
    )
    completed = store.completed_keys() if args.resume else set()
    pending = [
        item
        for item in work
        if store.key(item.conversation_id, item.qa_id) not in completed
    ]
    prompts = _load_prompts(config)
    worker = ComponentWorker(
        component=component,
        config=config,
        prompts=prompts,
        judge_run_id=judge_path.parent.name,
        diagnosis_run_id=args.diagnosis_run_id,
    )
    store.write_manifest(
        {
            "schema_version": "diagnosis_runner_v3",
            "component": component,
            "diagnosis_run_id": args.diagnosis_run_id,
            "judge_results": str(judge_path),
            "source_runs": {
                key: str(value) for key, value in source_runs.items()
            },
            "workers": args.workers,
            "eligible": len(work),
            "pending": len(pending),
            "started_at": utc_now(),
        }
    )

    processed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for item, outcome in _bounded_map(
            executor,
            worker.run,
            pending,
            max_in_flight=args.workers * 2,
        ):
            processed += 1
            if isinstance(outcome, Exception):
                store.publish_data_error(
                    conversation_id=item.conversation_id,
                    qa_id=item.qa_id,
                    reason=str(outcome),
                )
                status = "data_error"
                problem = False
            else:
                store.publish(outcome)
                status = outcome.status.value
                problem = bool(outcome.problem_found)
            print(
                f"[{component}] completed={processed}/{len(pending)} "
                f"qa={item.qa_id} status={status} problem={problem}",
                flush=True,
            )

    store.write_summary(
        eligible=len(work),
        skipped_resume=len(work) - len(pending),
    )
    return 0


def _bounded_map(
    executor: ThreadPoolExecutor,
    function,
    items: list[WorkItem],
    *,
    max_in_flight: int,
) -> Iterable[tuple[WorkItem, BaseModel | Exception]]:
    iterator = iter(items)
    futures: dict[Future, WorkItem] = {}

    def fill() -> None:
        while len(futures) < max_in_flight:
            try:
                item = next(iterator)
            except StopIteration:
                return
            futures[executor.submit(function, item)] = item

    fill()
    while futures:
        done, _ = wait(futures, return_when=FIRST_COMPLETED)
        for future in done:
            item = futures.pop(future)
            try:
                yield item, future.result()
            except Exception as exc:
                yield item, exc
        fill()


def _parse_source_runs(values: list[str]) -> dict[str, Path]:
    source_runs: dict[str, Path] = {}
    for value in values:
        conversation_id, separator, raw_path = value.partition("=")
        if not separator or not conversation_id.strip() or not raw_path.strip():
            raise SystemExit(
                "--source-run must use CONVERSATION_ID=PATH syntax."
            )
        conversation_id = conversation_id.strip()
        if conversation_id in source_runs:
            raise SystemExit(
                f"Duplicate source run for {conversation_id}."
            )
        source_runs[conversation_id] = Path(raw_path.strip())
    return source_runs


def _build_work_items(
    *,
    config: MiMConfig,
    source_runs: dict[str, Path],
    judge_rows: list[dict[str, Any]],
) -> list[WorkItem]:
    judge_by_qa = {
        str(row["qa_id"]): row
        for row in judge_rows
        if row.get("qa_id") and row.get("label") in {"P", "I"}
    }
    _, questions_by_conversation = load_dataset(config.dataset.path)
    question_by_id = {
        question.qa_id: question
        for questions in questions_by_conversation.values()
        for question in questions
    }

    items: list[WorkItem] = []
    seen: set[tuple[str, str]] = set()
    for conversation_id, source_run in sorted(source_runs.items()):
        prediction_path = source_run / "locomo_predictions.jsonl"
        db_path = source_run / "state" / "memory.sqlite3"
        for prediction in _load_jsonl(prediction_path):
            qa_id = str(prediction.get("qa_id", ""))
            judge = judge_by_qa.get(qa_id)
            if judge is None or prediction.get("error"):
                continue
            question = question_by_id.get(qa_id)
            if question is None:
                raise ValueError(f"Dataset question not found: {qa_id}")
            key = (conversation_id, qa_id)
            if key in seen:
                raise ValueError(f"Duplicate diagnosis item: {key}")
            seen.add(key)
            gold_ids = tuple(
                dict.fromkeys(
                    str(item[-1])
                    for item in question.source_evidence
                    if item and item[-1]
                )
            )
            items.append(
                WorkItem(
                    conversation_id=conversation_id,
                    qa_id=qa_id,
                    db_path=db_path,
                    source_runtime_run=source_run.name,
                    question=question.question,
                    reference_answer=question.reference_answer,
                    prediction=str(prediction.get("prediction", "")),
                    judge_label=str(judge["label"]),
                    judge_reason=str(judge.get("reason", "")),
                    gold_message_ids=gold_ids,
                )
            )
    return sorted(items, key=lambda item: (item.conversation_id, item.qa_id))


def _require_answer_phase(
    output_root: Path,
    work: list[WorkItem],
) -> None:
    progress_path = output_root / "answer_failure" / "progress.jsonl"
    attempted = {
        DiagnosisArtifactStore.key(
            str(row.get("conversation_id", "")),
            str(row.get("qa_id", "")),
        )
        for row in _load_jsonl(progress_path)
        if row.get("status") in {
            DiagnosisStatus.COMPLETED.value,
            DiagnosisStatus.MODEL_ERROR.value,
            DiagnosisStatus.DATA_ERROR.value,
        }
    }
    missing = [
        item.qa_id
        for item in work
        if DiagnosisArtifactStore.key(
            item.conversation_id,
            item.qa_id,
        )
        not in attempted
    ]
    if missing:
        preview = ", ".join(missing[:5])
        raise SystemExit(
            "Answer phase has not attempted every item. "
            "Run run_answer_failure.py first. "
            f"Missing {len(missing)} item(s), including: {preview}"
        )


def _load_prompts(config: MiMConfig) -> dict[str, str]:
    paths = {
        "answer": config.prompts.diagnosis_answer,
        "access": config.prompts.diagnosis_access,
        "cons_screening": config.prompts.diagnosis_cons_screening,
        "cons_trace": config.prompts.diagnosis_cons_trace,
    }
    prompts: dict[str, str] = {}
    for name, value in paths.items():
        path = Path(value)
        if not path.exists():
            raise FileNotFoundError(f"Diagnosis prompt not found: {path}")
        prompts[name] = path.read_text(encoding="utf-8")
    return prompts


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSONL at {path}:{line_number}"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(f"Expected object at {path}:{line_number}")
        rows.append(value)
    return rows
