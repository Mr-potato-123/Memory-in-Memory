"""Evaluate one LoCoMo conversation with resumable per-question output.

The runner can attach to a previously completed memory database, keeps only
successful QA rows, and retries failed/missing questions without rebuilding
the conversation memory.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.artifacts import RunDir
from mim.config import load_config
from mim.eval.locomo import load_dataset
from mim.eval.metrics import compute_f1
from mim.llm import create_client
from mim.retrieval.embedder import Embedder
from mim.schemas import Side
from mim.skills import SkillBank
from mim.workflows.use import MiMRuntime


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument(
        "--skill-bank-dir",
        required=True,
        help="Directory containing access_skill_bank_v1.json and "
        "construction_skill_bank_v1.json",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--question-retries", type=int, default=3)
    parser.add_argument("--num-shards", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    return parser.parse_args()


def load_successes(path: Path, conversation_id: str) -> dict[str, dict]:
    successes: dict[str, dict] = {}
    if not path.exists():
        return successes
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            row.get("conversation_id") == conversation_id
            and row.get("qa_id")
            and not row.get("error")
        ):
            successes[str(row["qa_id"])] = row
    return successes


def summarize(rows: list[dict], run_dir: RunDir) -> None:
    groups: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        groups[int(row["category"])].append(float(row["f1"]))
    run_dir.write_json(
        "summary.json",
        {
            "model": "qwen3-8b",
            "mode": "mim",
            "thinking": False,
            "conversation_id": rows[0]["conversation_id"] if rows else "",
            "total_qa": len(rows),
            "overall_f1": (
                sum(float(row["f1"]) for row in rows) / len(rows)
                if rows else 0.0
            ),
            "overall_f1_percent": (
                100 * sum(float(row["f1"]) for row in rows) / len(rows)
                if rows else 0.0
            ),
            "category_f1": {
                str(category): sum(scores) / len(scores)
                for category, scores in sorted(groups.items())
            },
            "category_count": {
                str(category): len(scores)
                for category, scores in sorted(groups.items())
            },
            "protocol_errors": sum(bool(row.get("error")) for row in rows),
            "runtime_tokens": sum(int(row.get("runtime_tokens", 0)) for row in rows),
            "avg_access_steps": (
                sum(int(row.get("access_steps", 0)) for row in rows) / len(rows)
                if rows else 0.0
            ),
        },
    )


def main() -> int:
    args = arguments()
    if args.num_shards < 1:
        raise ValueError("--num-shards must be at least 1")
    if not 0 <= args.shard_index < args.num_shards:
        raise ValueError("--shard-index must be in [0, num-shards)")
    config = load_config(args.config)
    run_dir = RunDir(args.run_id, args.output_dir)
    qa_path = run_dir.path / "qa_results.jsonl"

    if args.resume:
        if not run_dir.path.exists():
            raise FileNotFoundError(f"Cannot resume missing run: {run_dir.path}")
        if not (run_dir.path / "state" / "memory.sqlite3").exists():
            raise FileNotFoundError("Cannot resume without state/memory.sqlite3")
    else:
        if run_dir.path.exists():
            raise FileExistsError(f"Run already exists: {run_dir.path}")
        run_dir.path.mkdir(parents=True)

    conversations, questions = load_dataset(config.dataset.path)
    conversation_map = {item.conversation_id: item for item in conversations}
    if args.conversation_id not in conversation_map:
        raise ValueError(f"Unknown conversation: {args.conversation_id}")
    qas = questions[args.conversation_id]

    model = create_client(config.models["runtime"])
    embedder = Embedder(
        config.embedding.model,
        config.embedding.device,
        config.embedding.normalize,
        config.embedding.batch_size,
    )
    bank = SkillBank.load_published(args.skill_bank_dir)
    bank.freeze()
    runtime = MiMRuntime(
        config,
        mode="mim",
        skill_bank=bank,
        run_dir=run_dir,
        runtime_model=model,
        embedder=embedder,
        phase="validation",
        strict_construction=True,
    )

    successful = load_successes(qa_path, args.conversation_id)
    if args.resume:
        committed_sessions = runtime.store.committed_session_count(
            args.conversation_id
        )
        expected_sessions = len(
            conversation_map[args.conversation_id].sessions
        )
        if committed_sessions < expected_sessions:
            print(
                f"[resume-ingestion] {args.conversation_id}: "
                f"committed={committed_sessions}/{expected_sessions}",
                flush=True,
            )
            runtime.ingest(
                conversation_map[args.conversation_id],
                resume_existing=True,
            )
        else:
            runtime.attach(args.conversation_id)
        # Remove stale failed/duplicate rows before appending new results.
        run_dir.write_jsonl("qa_results.jsonl", list(successful.values()))
        print(
            f"[resume] {args.conversation_id}: completed={len(successful)} "
            f"remaining={len(qas) - len(successful)}",
            flush=True,
        )
    else:
        runtime.ingest(conversation_map[args.conversation_id])

    failed: dict[str, dict] = {}
    for index, question in enumerate(qas, 1):
        if (index - 1) % args.num_shards != args.shard_index:
            continue
        if question.qa_id in successful:
            continue
        last_row: dict | None = None
        for attempt in range(1, max(1, args.question_retries) + 1):
            access = runtime.ask(question)
            score = (
                compute_f1(
                    access.answer,
                    question.reference_answer,
                    question.category,
                )
                if not access.error else 0.0
            )
            last_row = {
                "conversation_id": args.conversation_id,
                "qa_id": question.qa_id,
                "category": question.category,
                "question": question.question,
                "reference": question.reference_answer,
                "prediction": access.answer,
                "evidence_ids": access.evidence_ids,
                "skill_ids": access.used_skill_ids,
                "f1": float(score),
                "runtime_tokens": access.total_tokens,
                "access_steps": access.steps,
                "error": access.error,
            }
            if not access.error:
                successful[question.qa_id] = last_row
                run_dir.append_jsonl("qa_results.jsonl", last_row)
                print(
                    f"[{args.conversation_id}] {index}/{len(qas)} "
                    f"F1={score:.4f}",
                    flush=True,
                )
                break
            print(
                f"[{args.conversation_id}] {question.qa_id} attempt "
                f"{attempt} failed: {access.error}",
                flush=True,
            )
        else:
            assert last_row is not None
            failed[question.qa_id] = last_row

    final_rows = [
        successful[q.qa_id]
        for q in qas
        if q.qa_id in successful
    ] + [failed[key] for key in sorted(failed)]
    run_dir.write_jsonl("qa_results.jsonl", final_rows)
    summarize(final_rows, run_dir)
    print(
        f"[complete] {args.conversation_id}: rows={len(final_rows)} "
        f"failed={len(failed)}",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
