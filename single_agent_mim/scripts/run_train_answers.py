"""Generate LoCoMo train-split answers with Qwen3-8B non-thinking.

Natural search chain — each QA gets a continuous message chain where the
model sees full tool results without truncation. Supports --resume for
restarting interrupted runs whose ingestion already completed.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.artifacts import RunDir
from mim.config import load_config
from mim.eval.locomo import load_dataset
from mim.eval.metrics import compute_f1
from mim.eval.split import sha256_file
from mim.llm import create_client
from mim.retrieval.embedder import Embedder
from mim.schemas import DatasetSplit
from mim.skills import SkillBank
from mim.workflows.use import MiMRuntime

FROZEN_TRAIN = [
    "conv-30",
    "conv-42",
    "conv-43",
    "conv-44",
    "conv-48",
    "conv-49",
]


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", default="configs/qwen3_8b_dashscope.yaml"
    )
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--conversation-id",
        required=True,
        help="Train conversation: conv-30/42/43/44/48/49.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing run whose ingestion already completed.",
    )
    parser.add_argument(
        "--skill-bank-dir",
        default=None,
        help="Published Bank directory. Omit only for the Bank0 baseline.",
    )
    parser.add_argument(
        "--question-retries",
        type=int,
        default=3,
        help="Retry one QA when the bounded Access chain ends without an answer.",
    )
    return parser.parse_args()


class Monitor:
    """Print and persist every stage as it happens."""

    def __init__(self, run_dir: RunDir):
        self.run_dir = run_dir

    def __call__(self, item: dict[str, Any]) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **item,
        }
        self.run_dir.append_jsonl("events.jsonl", row)
        fields = " ".join(
            f"{key}={value}"
            for key, value in row.items()
            if key not in {"timestamp", "event"} and value not in (None, "")
        )
        print(
            f"[{row['timestamp']}] {row['event']} {fields}".rstrip(),
            flush=True,
        )


def api_smoke(model) -> dict[str, Any]:
    response = model.generate(
        [
            {
                "role": "system",
                "content": "Return only JSON. Do not include reasoning.",
            },
            {"role": "user", "content": '{"status":"ok"}'},
        ],
        max_tokens=32,
        json_mode=True,
    )
    try:
        body = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Smoke response is not JSON: {response.text!r}"
        ) from exc
    return {
        "response": body,
        "latency_ms": response.latency_ms,
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
    }


def _check_ingestion_complete(events_path: Path) -> bool:
    if not events_path.exists():
        return False
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "ingestion_complete":
            return True
    return False


def _load_completed_ids(predictions_path: Path) -> set[str]:
    if not predictions_path.exists():
        return set()
    completed: set[str] = set()
    for line in predictions_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        completed.add(row.get("qa_id", ""))
    return completed


def summarize(
    results: list[dict[str, Any]],
    *,
    mode: str,
    bank_name: str,
) -> dict[str, Any]:
    groups: dict[int, list[float]] = defaultdict(list)
    for row in results:
        groups[row["category"]].append(row["f1"])
    overall = (
        sum(row["f1"] for row in results) / len(results) if results else 0.0
    )
    return {
        "model": "qwen3-8b",
        "mode": mode,
        "bank": bank_name,
        "thinking": False,
        "split": "train",
        "total_qa": len(results),
        "overall_f1": overall,
        "overall_f1_percent": 100 * overall,
        "category_f1": {
            str(cat): sum(scores) / len(scores)
            for cat, scores in sorted(groups.items())
        },
        "category_count": {
            str(cat): len(scores)
            for cat, scores in sorted(groups.items())
        },
        "protocol_errors": sum(bool(row.get("error")) for row in results),
        "runtime_tokens": sum(row["runtime_tokens"] for row in results),
        "avg_access_steps": (
            sum(row["access_steps"] for row in results) / len(results)
            if results
            else 0.0
        ),
    }


def _prompt_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    args = arguments()
    config = load_config(args.config)
    model_config = config.models["runtime"]
    key_name = model_config.api_key_env or "RUNTIME_API_KEY"
    if not model_config.api_key and not os.environ.get(key_name):
        raise RuntimeError(
            f"{key_name} is not set. The key is never written to outputs."
        )

    # ── Validate conversation-id ─────────────────────────────────
    if args.conversation_id not in FROZEN_TRAIN:
        raise ValueError(
            f"{args.conversation_id!r} is not in the frozen train split. "
            f"Allowed: {FROZEN_TRAIN}"
        )

    dataset_path = Path(config.dataset.path)
    split_path = Path(config.dataset.split)
    split = DatasetSplit(**json.loads(split_path.read_text(encoding="utf-8")))
    if sha256_file(dataset_path) != split.dataset_sha256:
        raise RuntimeError("Frozen split does not match the LoCoMo dataset")

    if args.conversation_id not in split.train:
        raise ValueError(
            f"{args.conversation_id!r} is not in split.train. "
            f"Split train: {list(split.train)}"
        )

    # ── Resolve run directory and resume state ───────────────────
    run_dir = RunDir(args.run_id, args.output_dir)
    events_path = run_dir.path / "events.jsonl"
    predictions_path = run_dir.path / "locomo_predictions.jsonl"

    if args.resume:
        if not run_dir.path.exists():
            raise FileNotFoundError(
                f"Run directory does not exist: {run_dir.path}. "
                f"Cannot resume. Omit --resume to start a new run."
            )
        if not _check_ingestion_complete(events_path):
            raise RuntimeError(
                f"No ingestion_complete found in {events_path}. "
                f"Ingestion may be incomplete. Use a new --run-id."
            )
        print(
            f"[resume] Attaching to existing memory for {args.conversation_id}",
            flush=True,
        )
    else:
        if run_dir.path.exists():
            if _check_ingestion_complete(events_path):
                print(
                    f"[warning] Run directory {run_dir.path} exists and has "
                    f"ingestion_complete. Consider --resume to continue.",
                    flush=True,
                )
            else:
                raise FileExistsError(
                    f"Run directory already exists without ingestion_complete: "
                    f"{run_dir.path}. Use a different --run-id."
                )
        else:
            os.makedirs(run_dir.path, exist_ok=False)

    monitor = Monitor(run_dir)
    monitor(
        {
            "event": "run_start",
            "run_id": args.run_id,
            "phase": "train_answer_generation",
        }
    )

    if not args.resume:
        monitor(
            {
                "event": "prompt_hashes",
                "access": _prompt_sha256(config.prompts.access),
                "construction_extraction": _prompt_sha256(
                    config.prompts.construction_extraction
                ),
                "construction_decision": _prompt_sha256(
                    config.prompts.construction_decision
                ),
            }
        )

    # ── API smoke test ───────────────────────────────────────────
    model = create_client(model_config)
    smoke = api_smoke(model)
    run_dir.write_json("api_smoke.json", smoke)
    monitor({"event": "api_smoke_passed", **smoke})

    # ── Embedding check ──────────────────────────────────────────
    embedder = Embedder(
        config.embedding.model,
        config.embedding.device,
        config.embedding.normalize,
        config.embedding.batch_size,
    )
    if embedder.backend != "sentence-transformers":
        raise RuntimeError(
            "Embedding model failed; hash fallback is forbidden: "
            f"{embedder.load_error}"
        )
    monitor(
        {
            "event": "embedding_ready",
            "model": embedder.model_name,
            "dimension": embedder.dim,
        }
    )

    # ── Load dataset ─────────────────────────────────────────────
    conversations, questions = load_dataset(dataset_path)
    conversation_map = {
        c.conversation_id: c for c in conversations
    }
    qas = questions[args.conversation_id]

    # ── Runtime ──────────────────────────────────────────────────
    bank = None
    mode = "base"
    bank_name = "bank0"
    if args.skill_bank_dir:
        bank = SkillBank.load_published(args.skill_bank_dir)
        mode = "mim"
        bank_name = f"bank{bank.version}"
    runtime = MiMRuntime(
        config,
        mode=mode,
        skill_bank=bank,
        run_dir=run_dir,
        runtime_model=model,
        embedder=embedder,
        phase="train_answer_generation",
        event_sink=monitor,
        strict_construction=True,
    )

    results: list[dict[str, Any]] = []
    completed_ids: set[str] = set()

    if args.resume:
        runtime.attach(args.conversation_id)
        completed_ids = _load_completed_ids(predictions_path)
        # Rebuild results from existing predictions
        if predictions_path.exists():
            for line in predictions_path.read_text(
                encoding="utf-8"
            ).splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("conversation_id") == args.conversation_id:
                    results.append(row)
        monitor(
            {
                "event": "evaluation_resumed",
                "conversation_id": args.conversation_id,
                "completed": len(completed_ids),
                "remaining": len(qas) - len(completed_ids),
            }
        )
        print(
            f"completed={len(completed_ids)} "
            f"remaining={len(qas) - len(completed_ids)}",
            flush=True,
        )
    else:
        monitor(
            {
                "event": "conversation_start",
                "conversation_id": args.conversation_id,
                "qa_total": len(qas),
            }
        )
        runtime.ingest(conversation_map[args.conversation_id])

    # ── Answer questions ─────────────────────────────────────────
    for index, question in enumerate(qas, start=1):
        if question.qa_id in completed_ids:
            continue
        access = None
        for attempt in range(1, max(1, args.question_retries) + 1):
            access = runtime.ask(question)
            if not access.error:
                break
            monitor(
                {
                    "event": "qa_retry",
                    "conversation_id": args.conversation_id,
                    "qa_id": question.qa_id,
                    "attempt": attempt,
                    "error": access.error,
                }
            )
        assert access is not None
        if access.error:
            raise RuntimeError(
                f"Access failed for {question.qa_id} after "
                f"{max(1, args.question_retries)} attempts: {access.error}"
            )
        score = compute_f1(
            access.answer,
            question.reference_answer,
            question.category,
        )
        row = {
            "conversation_id": args.conversation_id,
            "qa_id": question.qa_id,
            "question": question.question,
            "answer": question.reference_answer,
            "category": question.category,
            "prediction": access.answer,
            "evidence": access.evidence_ids,
            "skill_ids": access.used_skill_ids,
            "f1": score,
            "runtime_tokens": access.total_tokens,
            "access_steps": access.steps,
            "error": access.error,
        }
        results.append(row)
        run_dir.append_jsonl("locomo_predictions.jsonl", row)
        monitor(
            {
                "event": "qa_complete",
                "conversation_id": args.conversation_id,
                "qa_id": question.qa_id,
                "qa_index": index,
                "qa_total": len(qas),
                "f1": round(score, 6),
            }
        )

    monitor(
        {
            "event": "conversation_complete",
            "conversation_id": args.conversation_id,
        }
    )

    # ── Summary ──────────────────────────────────────────────────
    summary = summarize(results, mode=mode, bank_name=bank_name)
    run_dir.write_json("summary.json", summary)
    run_dir.update_manifest(
        model="qwen3-8b",
        mode=mode,
        bank=bank_name,
        skill_bank_dir=args.skill_bank_dir,
        enable_thinking=False,
        split="train",
        conversation_id=args.conversation_id,
        dataset_sha256=split.dataset_sha256,
        split_sha256=sha256_file(split_path),
        access_prompt_sha256=_prompt_sha256(config.prompts.access),
        construction_extraction_prompt_sha256=_prompt_sha256(
            config.prompts.construction_extraction
        ),
        construction_decision_prompt_sha256=_prompt_sha256(
            config.prompts.construction_decision
        ),
        config_path=args.config,
        llm_judge_run=False,
        total_qa=len(results),
    )
    run_dir.save_manifest()
    monitor(
        {
            "event": "run_complete",
            "overall_f1": summary["overall_f1"],
            "total_qa": len(results),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
