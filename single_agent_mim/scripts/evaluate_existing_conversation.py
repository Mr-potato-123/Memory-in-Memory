"""Evaluate all remaining QAs against one already-built conversation memory."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.artifacts import RunDir
from mim.config import load_config
from mim.eval.locomo import load_dataset
from mim.eval.metrics import compute_f1
from mim.llm import create_client
from mim.retrieval.embedder import Embedder
from mim.workflows.use import MiMRuntime

from run_qwen3_8b_baseline import Monitor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    run_dir = RunDir(args.run_id, args.output_dir)
    if not run_dir.path.exists():
        raise FileNotFoundError(run_dir.path)
    monitor = Monitor(run_dir)

    model = create_client(config.models["runtime"])
    embedder = Embedder(
        config.embedding.model,
        config.embedding.device,
        config.embedding.normalize,
        config.embedding.batch_size,
    )
    if embedder.backend != "sentence-transformers":
        raise RuntimeError(f"Embedding model failed: {embedder.load_error}")

    runtime = MiMRuntime(
        config,
        mode="base",
        run_dir=run_dir,
        runtime_model=model,
        embedder=embedder,
        phase="test",
        event_sink=monitor,
        strict_construction=True,
    )
    runtime.attach(args.conversation_id)

    _, questions = load_dataset(config.dataset.path)
    qas = questions[args.conversation_id]
    result_path = run_dir.path / "locomo_predictions.jsonl"
    existing: list[dict] = []
    if result_path.exists():
        existing = [
            json.loads(line)
            for line in result_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    completed_ids = {
        row["qa_id"]
        for row in existing
        if row.get("conversation_id") == args.conversation_id
    }
    monitor(
        {
            "event": "evaluation_resumed",
            "conversation_id": args.conversation_id,
            "completed": len(completed_ids),
            "remaining": len(qas) - len(completed_ids),
        }
    )

    for index, question in enumerate(qas, start=1):
        if question.qa_id in completed_ids:
            continue
        access = runtime.ask(question)
        if access.error:
            raise RuntimeError(
                f"Access failed for {question.qa_id}: {access.error}"
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
            "f1": score,
            "runtime_tokens": access.total_tokens,
            "access_steps": access.steps,
            "error": access.error,
        }
        run_dir.append_jsonl("locomo_predictions.jsonl", row)
        existing.append(row)
        monitor(
            {
                "event": "qa_complete",
                "conversation_id": args.conversation_id,
                "qa_index": index,
                "qa_total": len(qas),
                "f1": round(score, 6),
            }
        )

    results = [
        row for row in existing
        if row.get("conversation_id") == args.conversation_id
    ]
    by_category: dict[int, list[float]] = defaultdict(list)
    for row in results:
        by_category[int(row["category"])].append(float(row["f1"]))
    summary = {
        "model": "Qwen3-8B",
        "mode": "Base",
        "thinking": False,
        "conversation_id": args.conversation_id,
        "total_qa": len(results),
        "overall_f1": sum(row["f1"] for row in results) / len(results),
        "category_f1": {
            str(category): sum(scores) / len(scores)
            for category, scores in sorted(by_category.items())
        },
        "category_count": {
            str(category): len(scores)
            for category, scores in sorted(by_category.items())
        },
        "protocol_errors": sum(bool(row.get("error")) for row in results),
        "total_runtime_tokens": sum(row["runtime_tokens"] for row in results),
        "avg_access_steps": (
            sum(row["access_steps"] for row in results) / len(results)
        ),
    }
    run_dir.write_json(f"{args.conversation_id}_summary.json", summary)
    monitor(
        {
            "event": "conversation_evaluation_complete",
            "conversation_id": args.conversation_id,
            "total_qa": len(results),
            "overall_f1": summary["overall_f1"],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
