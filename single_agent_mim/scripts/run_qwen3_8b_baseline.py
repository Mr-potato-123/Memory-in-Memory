"""Run the two-conversation LoCoMo baseline with Qwen3-8B non-thinking."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
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
from mim.workflows.use import MiMRuntime


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", default="configs/qwen3_8b_dashscope.yaml"
    )
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--conversation-id",
        help="Run only one conversation from the frozen test split.",
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
        print(f"[{row['timestamp']}] {row['event']} {fields}".rstrip(), flush=True)


def api_smoke(model) -> dict[str, Any]:
    """One JSON request; client rejects any leaked thinking output."""
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
        raise RuntimeError(f"Smoke response is not JSON: {response.text!r}") from exc
    return {
        "response": body,
        "latency_ms": response.latency_ms,
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[int, list[float]] = defaultdict(list)
    for row in results:
        groups[row["category"]].append(row["f1"])
    overall = sum(row["f1"] for row in results) / len(results)
    return {
        "model": "Qwen3-8B",
        "mode": "Base",
        "thinking": False,
        "split": "test",
        "conversation_ids": sorted({row["conversation_id"] for row in results}),
        "total_qa": len(results),
        "overall_f1": overall,
        "overall_f1_percent": 100 * overall,
        "category_f1": {
            str(category): sum(scores) / len(scores)
            for category, scores in sorted(groups.items())
        },
        "category_count": {
            str(category): len(scores)
            for category, scores in sorted(groups.items())
        },
        "protocol_errors": sum(bool(row["error"]) for row in results),
        "runtime_tokens": sum(row["runtime_tokens"] for row in results),
    }


def paper_table(summary: dict[str, Any]) -> str:
    category = summary["category_f1"]
    # LoCoMo IDs: 1 multi-hop, 2 temporal, 3 open-domain,
    # 4 single-hop, 5 adversarial. The paper table presents single-hop first.
    scores = [
        f"{100 * category.get(str(index), 0.0):.2f}"
        for index in (1, 2, 4, 3, 5)
    ]
    return (
        "# LoCoMo QA baseline\n\n"
        "| Model | Mode | Overall | Multi-hop | Temporal | Single-hop | "
        "Open-domain | Adversarial |\n"
        "|---|---|---:|---:|---:|---:|---:|---:|\n"
        f"| Qwen3-8B | Base / non-thinking | "
        f"{summary['overall_f1_percent']:.2f} | "
        + " | ".join(scores)
        + " |\n\n"
        "Official category-aware LoCoMo token F1; values are percentages.\n"
    )


def main() -> int:
    args = arguments()
    config = load_config(args.config)
    model_config = config.models["runtime"]
    key_name = model_config.api_key_env or "RUNTIME_API_KEY"
    if not model_config.api_key and not os.environ.get(key_name):
        raise RuntimeError(
            f"{key_name} is not set in this process. "
            "The key is never written to output files."
        )

    dataset_path = Path(config.dataset.path)
    split_path = Path(config.dataset.split)
    split = DatasetSplit(
        **json.loads(split_path.read_text(encoding="utf-8"))
    )
    if sha256_file(dataset_path) != split.dataset_sha256:
        raise RuntimeError("Frozen split does not match the LoCoMo dataset")

    run_id = args.run_id or (
        "qwen3_8b_nonthinking_"
        + datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    run_dir = RunDir.create(run_id, args.output_dir)
    monitor = Monitor(run_dir)
    monitor({"event": "run_start", "run_id": run_id})

    model = create_client(model_config)
    smoke = api_smoke(model)
    run_dir.write_json("api_smoke.json", smoke)
    monitor({"event": "api_smoke_passed", **smoke})

    embedder = Embedder(
        config.embedding.model,
        config.embedding.device,
        config.embedding.normalize,
        config.embedding.batch_size,
    )
    if embedder.backend != "sentence-transformers":
        raise RuntimeError(
            "Embedding model failed; hash fallback is forbidden in the baseline: "
            f"{embedder.load_error}"
        )
    monitor(
        {
            "event": "embedding_ready",
            "model": embedder.model_name,
            "dimension": embedder.dim,
        }
    )

    conversations, questions = load_dataset(dataset_path)
    conversation_map = {
        conversation.conversation_id: conversation
        for conversation in conversations
    }
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

    results: list[dict[str, Any]] = []
    selected_conversations = list(split.test)
    if args.conversation_id:
        if args.conversation_id not in selected_conversations:
            raise ValueError(
                f"{args.conversation_id!r} is not in the frozen test split: "
                f"{selected_conversations}"
            )
        selected_conversations = [args.conversation_id]

    for conversation_id in selected_conversations:
        qas = questions[conversation_id]
        monitor(
            {
                "event": "conversation_start",
                "conversation_id": conversation_id,
                "qa_total": len(qas),
            }
        )
        runtime.ingest(conversation_map[conversation_id])
        for index, question in enumerate(qas, start=1):
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
                "conversation_id": conversation_id,
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
            results.append(row)
            run_dir.append_jsonl("locomo_predictions.jsonl", row)
            monitor(
                {
                    "event": "qa_complete",
                    "conversation_id": conversation_id,
                    "qa_index": index,
                    "qa_total": len(qas),
                    "f1": round(score, 6),
                }
            )
        monitor(
            {
                "event": "conversation_complete",
                "conversation_id": conversation_id,
            }
        )

    summary = summarize(results)
    run_dir.write_json("summary.json", summary)
    run_dir.write_text("paper_table.md", paper_table(summary))
    run_dir.update_manifest(
        model="qwen3-8b",
        mode="base",
        enable_thinking=False,
        dataset_sha256=split.dataset_sha256,
        split_sha256=sha256_file(split_path),
        test_conversations=selected_conversations,
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
