"""Parallel training iteration.

One worker per conversation (parallel construction), then per-conversation
parallel answering. Each conversation gets a persistent run directory so the
Judge-first Diagnosis V3 pipeline can read ``locomo_predictions.jsonl`` and
``state/memory.sqlite3`` afterwards.

Modes:
  * fresh run: full construction + answering for every conversation.
  * resume-from-db: copy a previous iteration's (pruned) SQLite database into
    each run directory and answer with the supplied Skill Bank without
    rebuilding memory (``resume_existing=True``).

Usage:
  python scripts/run_train_iter.py --config configs/qwen3_8b_dashscope.yaml \
      --split train --run-root outputs/v2_iter/iter1/train \
      [--skill-bank-dir ...] [--resume-db-from ...] \
      [--build-workers 6] [--qa-workers 6] [--smoke-qa 0] [--max-convs 0]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.artifacts import RunDir
from mim.config import load_config
from mim.eval.locomo import load_dataset
from mim.eval.metrics import compute_f1
from mim.llm import create_client
from mim.retrieval.embedder import Embedder
from mim.skills import SkillBank
from mim.workflows.use import MiMRuntime


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def train_one_conversation(
    config,
    conversation,
    questions,
    *,
    skill_bank_dir,
    run_dir: Path,
    embedder,
    runtime_model,
    qa_workers: int,
    split_name: str,
    resume_db_from: Path | None,
    smoke_qa: int,
) -> dict:
    run = RunDir(conversation.conversation_id, run_dir)
    db_path = run.path / config.storage.path

    if resume_db_from is not None:
        source_db = resume_db_from / conversation.conversation_id / config.storage.path
        if not source_db.exists():
            raise FileNotFoundError(
                f"Resume DB not found: {source_db}"
            )
        run.path.mkdir(parents=True, exist_ok=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_db, db_path)
        # WAL/SHM leftovers of the source DB must not follow the copy.
        for suffix in ("-wal", "-shm"):
            leftover = Path(str(source_db) + suffix)
            if leftover.exists():
                shutil.copy2(leftover, Path(str(db_path) + suffix))

    bank = (
        SkillBank.load_published(skill_bank_dir)
        if skill_bank_dir is not None
        else None
    )
    if bank is not None:
        bank.freeze()

    mode = "mim" if bank is not None else "base"
    runtime = MiMRuntime(
        config=config,
        mode=mode,
        skill_bank=bank,
        run_dir=run,
        runtime_model=runtime_model,
        embedder=embedder,
        phase=split_name,
    )

    resume_existing = resume_db_from is not None
    runtime.ingest(conversation, resume_existing=resume_existing)

    completed = {
        row["qa_id"]
        for row in _load_jsonl(run.path / "qa_results.jsonl")
    }

    def ask_one(question) -> dict:
        try:
            access = runtime.ask(question)
            f1 = (
                compute_f1(
                    access.answer,
                    question.reference_answer,
                    question.category,
                )
                if not access.error
                else 0.0
            )
            return {
                "conversation_id": conversation.conversation_id,
                "qa_id": question.qa_id,
                "category": question.category,
                "question": question.question,
                "reference": question.reference_answer,
                "prediction": access.answer,
                "evidence_ids": access.evidence_ids,
                "skill_ids": access.used_skill_ids,
                "f1": float(f1),
                "runtime_tokens": access.total_tokens,
                "access_steps": access.steps,
                "error": access.error,
            }
        except Exception as exc:  # never lose a row
            return {
                "conversation_id": conversation.conversation_id,
                "qa_id": question.qa_id,
                "category": question.category,
                "question": question.question,
                "reference": question.reference_answer,
                "prediction": "",
                "evidence_ids": [],
                "skill_ids": [],
                "f1": 0.0,
                "runtime_tokens": 0,
                "access_steps": 0,
                "error": f"ask_failed: {str(exc)[:200]}",
            }

    pending = [q for q in questions if q.qa_id not in completed]
    if smoke_qa > 0:
        pending = pending[:smoke_qa]

    results: list[dict] = []
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, qa_workers)) as pool:
            futures = {
                pool.submit(ask_one, question): question.qa_id
                for question in pending
            }
            for future in as_completed(futures):
                row = future.result()
                results.append(row)
                _append_jsonl(run.path / "qa_results.jsonl", row)
                _append_jsonl(
                    run.path / "locomo_predictions.jsonl",
                    row,
                )

    stats = {
        "conversation_id": conversation.conversation_id,
        "mode": mode,
        "skill_bank": skill_bank_dir,
        "total_qa": len(completed) + len(results),
        "newly_answered": len(results),
        "resumed_from": str(resume_db_from) if resume_db_from else None,
        "construction_errors": len(runtime.construction_errors),
        "construction_steps": runtime.last_construction_steps,
        "errors": [row["error"] for row in results if row["error"]],
    }
    (run.path / "summary.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    parser.add_argument("--split", default="train")
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--skill-bank-dir")
    parser.add_argument("--resume-db-from")
    parser.add_argument("--build-workers", type=int, default=6)
    parser.add_argument("--qa-workers", type=int, default=6)
    parser.add_argument("--max-convs", type=int, default=0)
    parser.add_argument("--smoke-qa", type=int, default=0,
                        help="Answer only the first N questions per conversation.")
    args = parser.parse_args()

    config = load_config(args.config)
    root = Path(args.run_root)
    root.mkdir(parents=True, exist_ok=True)

    conversations, questions_map = load_dataset(config.dataset.path)
    with open(config.dataset.split, encoding="utf-8") as handle:
        split_data = json.load(handle)
    conv_ids = split_data.get(args.split, [])
    if args.max_convs > 0:
        conv_ids = conv_ids[: args.max_convs]

    embedder = Embedder(
        model_name=config.embedding.model,
        device=config.embedding.device,
    )
    runtime_model = create_client(config.models["runtime"])

    resume_db_from = Path(args.resume_db_from) if args.resume_db_from else None
    all_stats = []
    with ThreadPoolExecutor(
        max_workers=min(args.build_workers, len(conv_ids))
    ) as pool:
        futures = {}
        for cid in conv_ids:
            conversation = next(
                c for c in conversations if c.conversation_id == cid
            )
            future = pool.submit(
                train_one_conversation,
                config,
                conversation,
                questions_map.get(cid, []),
                skill_bank_dir=args.skill_bank_dir,
                run_dir=root,
                embedder=embedder,
                runtime_model=runtime_model,
                qa_workers=args.qa_workers,
                split_name=args.split,
                resume_db_from=resume_db_from,
                smoke_qa=args.smoke_qa,
            )
            futures[future] = cid
        for future in as_completed(futures):
            cid = futures[future]
            try:
                stats = future.result()
            except Exception as exc:
                stats = {"conversation_id": cid, "error": str(exc)[:300]}
            all_stats.append(stats)
            print(f"[DONE] {cid}: {stats}", flush=True)

    total_qa = sum(s.get("total_qa", 0) for s in all_stats)
    (root / "summary.json").write_text(
        json.dumps(
            {
                "split": args.split,
                "skill_bank": args.skill_bank_dir,
                "total_qa": total_qa,
                "conversations": all_stats,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nTotal QA answered: {total_qa}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
