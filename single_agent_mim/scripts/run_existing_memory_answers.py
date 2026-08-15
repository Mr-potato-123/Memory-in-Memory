"""Answer one conversation against an already-built memory snapshot.

Fresh answer-only runs never import historical ``access_runs`` from SQLite.
Resume is scoped exclusively to the checkpoint created by this script.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
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


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _validate_rows(rows: list[dict], conversation_id: str, expected_ids: set[str]) -> None:
    ids = [str(row.get("qa_id", "")) for row in rows]
    duplicates = sorted(qid for qid, count in Counter(ids).items() if count > 1)
    wrong_conversation = sorted({
        str(row.get("conversation_id", ""))
        for row in rows
        if row.get("conversation_id") != conversation_id
    })
    extras = sorted(set(ids) - expected_ids)
    if duplicates or wrong_conversation or extras:
        raise RuntimeError(
            "Invalid answer checkpoint: "
            f"duplicates={duplicates[:5]}, "
            f"wrong_conversation={wrong_conversation}, extras={extras[:5]}"
        )


def _atomic_write_jsonl(path: Path, rows: list[dict]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(path)


def _export_historical(run: RunDir, conversation_id: str) -> int:
    """Explicit legacy export; never used by fresh or resumed evaluation."""
    db = sqlite3.connect(run.path / "state" / "memory.sqlite3")
    qrows = {
        row[0]: row
        for row in db.execute(
            "select qa_id,category,question,reference_answer from qa_cases"
        )
    }
    rows = []
    for access_id, qid, prediction, skills in db.execute(
        "select access_run_id,qa_id,prediction,skill_version_ids "
        "from access_runs where status='completed'"
    ):
        if qid not in qrows:
            continue
        question = qrows[qid]
        evidence = [
            item[0]
            for item in db.execute(
                "select version_id from access_final_evidence "
                "where access_run_id=? order by evidence_index",
                (access_id,),
            )
        ]
        rows.append({
            "conversation_id": conversation_id,
            "qa_id": qid,
            "category": question[1],
            "question": question[2],
            "reference": question[3],
            "prediction": prediction or "",
            "evidence_ids": evidence,
            "skill_ids": json.loads(skills or "[]"),
            "f1": float(compute_f1(prediction or "", question[3], question[1])),
            "runtime_tokens": 0,
            "access_steps": 0,
            "error": "",
            "answer_source": "historical_sqlite_export",
        })
    db.close()
    _atomic_write_jsonl(run.path / "qa_results.historical.jsonl", rows)
    print(f"{conversation_id} historical_export {len(rows)}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--skill-bank-dir")
    parser.add_argument("--question-retries", type=int, default=3)
    parser.add_argument("--qa-ids-file")
    parser.add_argument("--max-per-category", type=int)
    parser.add_argument("--qa-workers", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--allow-cpu-embedding-concurrency", action="store_true")
    parser.add_argument("--export-historical-access", action="store_true")
    args = parser.parse_args()

    if args.resume and args.overwrite:
        parser.error("--resume and --overwrite are mutually exclusive")
    if args.qa_workers < 1:
        parser.error("--qa-workers must be positive")

    config = load_config(args.config)
    run = RunDir(args.run_id, args.output_dir)
    if args.export_historical_access:
        return _export_historical(run, args.conversation_id)

    if (
        config.embedding.device.casefold() == "cpu"
        and args.qa_workers > 1
        and not args.allow_cpu_embedding_concurrency
    ):
        parser.error(
            "CPU embedding evaluation defaults to one QA worker per conversation. "
            "Use multiple conversation processes for parallelism, or explicitly pass "
            "--allow-cpu-embedding-concurrency after benchmarking."
        )

    _, questions = load_dataset(config.dataset.path)
    if args.conversation_id not in questions:
        raise RuntimeError(f"Unknown conversation: {args.conversation_id}")
    qas = list(questions[args.conversation_id])
    if args.qa_ids_file:
        wanted = {
            value.strip()
            for value in Path(args.qa_ids_file).read_text(
                encoding="utf-8-sig"
            ).splitlines()
            if value.strip()
        }
        qas = [question for question in qas if question.qa_id in wanted]
    if args.max_per_category is not None:
        if args.max_per_category < 1:
            parser.error("--max-per-category must be positive")
        counts: defaultdict[int, int] = defaultdict(int)
        selected = []
        for question in qas:
            if counts[question.category] < args.max_per_category:
                selected.append(question)
                counts[question.category] += 1
        qas = selected

    expected_ids = {question.qa_id for question in qas}
    if len(expected_ids) != len(qas):
        raise RuntimeError("Dataset contains duplicate qa_id values")

    final_path = run.path / "qa_results.jsonl"
    checkpoint_path = run.path / "qa_results.partial.jsonl"
    summary_path = run.path / "summary.json"
    manifest_path = run.path / "answer_run_manifest.json"

    if args.overwrite:
        for path in (final_path, checkpoint_path, summary_path, manifest_path):
            path.unlink(missing_ok=True)
    elif not args.resume and any(
        path.exists() for path in (final_path, checkpoint_path, manifest_path)
    ):
        raise RuntimeError(
            "Answer artifacts already exist. Use a new --run-id, --resume for this "
            "run's checkpoint, or --overwrite for an explicit fresh evaluation."
        )

    if args.resume and final_path.exists():
        completed = _read_jsonl(final_path)
        _validate_rows(completed, args.conversation_id, expected_ids)
        if {row["qa_id"] for row in completed} != expected_ids:
            raise RuntimeError(
                "Existing final file is incomplete; only partial checkpoints may be "
                "resumed. Use --overwrite to start clean."
            )
        print(f"{args.conversation_id} already_complete {len(completed)}", flush=True)
        return 0

    rows = _read_jsonl(checkpoint_path) if args.resume else []
    _validate_rows(rows, args.conversation_id, expected_ids)
    completed_ids = {row["qa_id"] for row in rows}
    qas = [question for question in qas if question.qa_id not in completed_ids]

    if args.resume:
        if not manifest_path.exists():
            raise RuntimeError("Cannot resume without answer_run_manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        evaluation_run_id = str(manifest["evaluation_run_id"])
        if manifest.get("conversation_id") != args.conversation_id:
            raise RuntimeError("Resume manifest conversation does not match")
    else:
        evaluation_run_id = f"answer_{uuid.uuid4().hex}"
        manifest = {
            "evaluation_run_id": evaluation_run_id,
            "conversation_id": args.conversation_id,
            "expected_qa": len(expected_ids),
            "mode": "mim" if args.skill_bank_dir else "base",
            "skill_bank_dir": args.skill_bank_dir or "",
            "config": str(args.config),
            "checkpoint_source": "this_run_only",
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    raw_pool = os.environ.get("DEEPSEEK_API_KEYS", "")
    if raw_pool:
        config.models["runtime"].api_keys = [
            key.strip() for key in raw_pool.split(",") if key.strip()
        ]
    model = create_client(config.models["runtime"])
    embedder = Embedder(
        config.embedding.model,
        config.embedding.device,
        config.embedding.normalize,
        config.embedding.batch_size,
    )
    bank = None
    mode = "base"
    if args.skill_bank_dir:
        bank = SkillBank.load_published(args.skill_bank_dir)
        bank.freeze()
        mode = "mim"
    runtime = MiMRuntime(
        config,
        mode=mode,
        skill_bank=bank,
        run_dir=run,
        runtime_model=model,
        embedder=embedder,
        phase="eval_answer_only",
        strict_construction=True,
        persist_access=False,
    )
    runtime.attach(args.conversation_id)

    def answer_one(question):
        access = None
        for _ in range(max(1, args.question_retries)):
            access = runtime.ask(question)
            if not access.error:
                break
        assert access is not None
        return {
            "evaluation_run_id": evaluation_run_id,
            "answer_source": "fresh_runtime_call",
            "conversation_id": args.conversation_id,
            "qa_id": question.qa_id,
            "category": question.category,
            "question": question.question,
            "reference": question.reference_answer,
            "prediction": access.answer,
            "evidence_ids": access.evidence_ids,
            "skill_ids": access.used_skill_ids,
            "f1": float(
                compute_f1(access.answer, question.reference_answer, question.category)
                if not access.error else 0.0
            ),
            "runtime_tokens": access.total_tokens,
            "access_steps": access.steps,
            "error": access.error or "",
        }

    with ThreadPoolExecutor(max_workers=args.qa_workers) as executor:
        futures = [executor.submit(answer_one, question) for question in qas]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            with checkpoint_path.open("a", encoding="utf-8") as checkpoint:
                checkpoint.write(json.dumps(row, ensure_ascii=False) + "\n")
                checkpoint.flush()
                os.fsync(checkpoint.fileno())
            print(
                f"{args.conversation_id} progress {len(rows)}/{len(expected_ids)} "
                f"qa_id={row['qa_id']} error={bool(row['error'])}",
                flush=True,
            )

    _validate_rows(rows, args.conversation_id, expected_ids)
    actual_ids = {row["qa_id"] for row in rows}
    if actual_ids != expected_ids:
        raise RuntimeError(
            f"Incomplete answer run: expected={len(expected_ids)}, actual={len(actual_ids)}"
        )
    rows.sort(key=lambda row: row["qa_id"])
    _atomic_write_jsonl(final_path, rows)

    groups: defaultdict[str, list[float]] = defaultdict(list)
    for row in rows:
        groups[str(row["category"])].append(row["f1"])
    summary = {
        "evaluation_run_id": evaluation_run_id,
        "answer_source": "fresh_runtime_call",
        "mode": mode,
        "conversation_id": args.conversation_id,
        "expected_qa": len(expected_ids),
        "total_qa": len(rows),
        "unique_qa": len(actual_ids),
        "overall_f1": sum(row["f1"] for row in rows) / len(rows) if rows else 0.0,
        "category_f1": {key: sum(values) / len(values) for key, values in groups.items()},
        "protocol_errors": sum(bool(row["error"]) for row in rows),
        "avg_access_steps": (
            sum(row["access_steps"] for row in rows) / len(rows) if rows else 0.0
        ),
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    checkpoint_path.unlink(missing_ok=True)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
