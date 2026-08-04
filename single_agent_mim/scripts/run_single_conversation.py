"""Run Construction + Access for any single LoCoMo conversation.

Minimal runner — validates against the split file, then runs the full
MiMRuntime pipeline (ingest + answer all QAs) with Token-F1 scoring.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
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
from mim.workflows.use import MiMRuntime


def arguments() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    p.add_argument("--conversation-id", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--output-dir", default="outputs")
    p.add_argument("--phase", default="eval",
                   help="Phase label for storage (train/test/eval)")
    return p.parse_args()


class Monitor:
    def __init__(self, run_dir: RunDir):
        self.run_dir = run_dir

    def __call__(self, item: dict[str, Any]) -> None:
        row = {"timestamp": datetime.now(timezone.utc).isoformat(), **item}
        self.run_dir.append_jsonl("events.jsonl", row)
        fields = " ".join(
            f"{k}={v}" for k, v in row.items()
            if k not in {"timestamp", "event"} and v not in (None, "")
        )
        print(f"[{row['timestamp']}] {row['event']} {fields}".rstrip(), flush=True)


def api_smoke(model) -> dict[str, Any]:
    response = model.generate(
        [
            {"role": "system", "content": "Return only JSON. Do not include reasoning."},
            {"role": "user", "content": '{"status":"ok"}'},
        ],
        max_tokens=32,
        json_mode=True,
    )
    body = json.loads(response.text)
    return {
        "response": body, "latency_ms": response.latency_ms,
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
    }


def main() -> int:
    args = arguments()
    config = load_config(args.config)

    dataset_path = Path(config.dataset.path)
    split_path = Path(config.dataset.split)
    split = DatasetSplit(**json.loads(split_path.read_text(encoding="utf-8")))

    if sha256_file(dataset_path) != split.dataset_sha256:
        raise RuntimeError("Split hash mismatch")

    # Validate conversation exists in dataset
    all_ids = split.train + split.validation + split.test
    if args.conversation_id not in all_ids:
        raise ValueError(
            f"{args.conversation_id!r} not in split. "
            f"Available: train={split.train} val={split.validation} test={split.test}"
        )

    run_dir = RunDir.create(args.run_id, args.output_dir)
    monitor = Monitor(run_dir)
    monitor({"event": "run_start", "run_id": args.run_id, "phase": args.phase})

    # Model
    model = create_client(config.models["runtime"])
    smoke = api_smoke(model)
    monitor({"event": "api_smoke_passed", **smoke})

    # Embedder
    embedder = Embedder(
        config.embedding.model, config.embedding.device,
        config.embedding.normalize, config.embedding.batch_size,
    )
    if embedder.backend != "sentence-transformers":
        raise RuntimeError(f"Embedder failed: {embedder.load_error}")
    monitor({"event": "embedding_ready", "model": embedder.model_name})

    # Dataset
    conversations, questions = load_dataset(dataset_path)
    conv_map = {c.conversation_id: c for c in conversations}
    qas = questions[args.conversation_id]

    # Runtime
    runtime = MiMRuntime(
        config, mode="base", run_dir=run_dir, runtime_model=model,
        embedder=embedder, phase=args.phase, event_sink=monitor,
        strict_construction=True,
    )

    monitor({
        "event": "conversation_start", "conversation_id": args.conversation_id,
        "qa_total": len(qas),
    })

    runtime.ingest(conv_map[args.conversation_id])

    results: list[dict] = []
    for idx, q in enumerate(qas, start=1):
        access = runtime.ask(q)
        if access.error:
            raise RuntimeError(f"Access failed: {q.qa_id}: {access.error}")
        score = compute_f1(access.answer, q.reference_answer, q.category)
        row = {
            "conversation_id": args.conversation_id,
            "qa_id": q.qa_id, "question": q.question,
            "answer": q.reference_answer, "category": q.category,
            "prediction": access.answer, "evidence": access.evidence_ids,
            "f1": score, "runtime_tokens": access.total_tokens,
            "access_steps": access.steps, "error": access.error,
        }
        results.append(row)
        run_dir.append_jsonl("locomo_predictions.jsonl", row)
        monitor({"event": "qa_complete", "qa_index": idx, "qa_total": len(qas),
                 "f1": round(score, 6)})

    monitor({"event": "conversation_complete", "conversation_id": args.conversation_id})

    # Summary
    groups: dict[int, list[float]] = defaultdict(list)
    for r in results:
        groups[r["category"]].append(r["f1"])
    overall = sum(r["f1"] for r in results) / len(results)
    summary = {
        "model": "qwen3-8b", "mode": "base", "thinking": False,
        "phase": args.phase, "conversation_id": args.conversation_id,
        "total_qa": len(results), "overall_f1": overall,
        "overall_f1_percent": 100 * overall,
        "category_f1": {str(c): sum(s)/len(s) for c, s in sorted(groups.items())},
        "category_count": {str(c): len(s) for c, s in sorted(groups.items())},
        "protocol_errors": sum(bool(r.get("error")) for r in results),
        "runtime_tokens": sum(r["runtime_tokens"] for r in results),
    }
    run_dir.write_json("summary.json", summary)
    run_dir.update_manifest(model="qwen3-8b", mode="base", phase=args.phase,
                            conversation_id=args.conversation_id,
                            total_qa=len(results))
    run_dir.save_manifest()
    monitor({"event": "run_complete", "overall_f1": overall, "total_qa": len(results)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
