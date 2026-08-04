"""LEGACY V2 concurrent runner; do not use for Diagnosis V3.

Use the three isolated V3 runners documented in
``docs/CLAUDE_RUN_DIAGNOSIS_V3.md``.
"""

from __future__ import annotations

import argparse
import copy as _copy
import json
import sqlite3
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.agents.access_diagnosis import AccessDiagnosisAgent
from mim.agents.construction_diagnosis import ConstructionDiagnosisAgent
from mim.agents.failure import AnswerCheckAgent
from mim.config import load_config
from mim.eval.locomo import load_dataset
from mim.failure.provenance import ProvenanceService
from mim.failure.workflow import FailureWorkflow
from mim.llm import create_client

FROZEN_TRAIN = [
    "conv-30", "conv-42", "conv-43", "conv-44", "conv-48", "conv-49",
]

SOURCE_RUNS = {
    "conv-30": "outputs/nsc_train/nsc_train_conv30_v1",
    "conv-42": "outputs/nsc_train/nsc_train_conv42_v1",
    "conv-43": "outputs/nsc_train/nsc_train_conv43_v1",
    "conv-44": "outputs/nsc_train/nsc_train_conv44_v1",
    "conv-48": "outputs/nsc_train/nsc_train_conv48_v1",
    "conv-49": "outputs/nsc_train/nsc_train_conv49_v1",
}


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def arguments() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    p.add_argument("--judge-results", required=True)
    p.add_argument("--output-dir", default="outputs/diagnosis")
    p.add_argument("--run-id", required=True)
    p.add_argument("--workers", type=int, default=4,
                   help="Threads per conversation")
    p.add_argument("--conversations", type=int, default=6,
                   help="Conversations to run concurrently (0=all)")
    p.add_argument("--max-failures", type=int, default=0)
    p.add_argument("--resume", action="store_true")
    return p.parse_args()


def diagnose_one(
    pred_row: dict,
    question: Any,
    cid: str,
    db_file: Path,
    access_agent: AccessDiagnosisAgent,
    construction_agent: ConstructionDiagnosisAgent,
    answer_check_agent: AnswerCheckAgent,
    failures_dir: Path,
    run_id: str,
) -> dict:
    """Run full diagnosis for a single QA. Returns result dict."""
    qa_id = pred_row["qa_id"]
    failure_id = f"failure_{cid}_{qa_id}"

    # Skip if already done
    diag_file = failures_dir / f"{failure_id}_diagnoses.json"
    if diag_file.exists():
        return {"status": "skipped", "qa_id": qa_id}

    gold_message_ids = [
        item[-1] for item in question.source_evidence
        if item and item[-1]
    ]

    conn = _open_readonly(db_file)
    try:
        # Find access run
        access_row = conn.execute(
            """SELECT access_run_id, snapshot_commit_id, prediction
               FROM access_runs
               WHERE conversation_id = ? AND qa_id = ?
                 AND status = 'completed'
               ORDER BY created_at DESC LIMIT 1""",
            (cid, qa_id),
        ).fetchone()

        if not access_row:
            return {"status": "engineering_error", "qa_id": qa_id,
                    "reason": "no_access_run"}

        access_run_id = access_row["access_run_id"]
        snapshot_commit_id = access_row["snapshot_commit_id"]

        # Returned memories
        mem_rows = conn.execute(
            """SELECT c.version_id, c.context_index, c.rendered_text,
                      v.memory_id, v.content, v.memory_kind, v.subject,
                      v.world_start, v.world_end
               FROM access_answer_context c
               JOIN memory_versions v ON v.version_id = c.version_id
               WHERE c.access_run_id = ?
               ORDER BY c.context_index""",
            (access_run_id,),
        ).fetchall()
        returned_memories = [dict(r) for r in mem_rows]

        # Source messages
        if gold_message_ids:
            placeholders = ",".join("?" for _ in gold_message_ids)
            src_rows = conn.execute(
                f"""SELECT message_id, session_id, turn_index,
                           role, speaker, content, occurred_at
                    FROM messages
                    WHERE conversation_id = ?
                      AND message_id IN ({placeholders})
                    ORDER BY session_id, turn_index""",
                (cid, *gold_message_ids),
            ).fetchall()
            source_messages = [dict(r) for r in src_rows]
            found = {r["message_id"] for r in src_rows}
            if any(mid not in found for mid in gold_message_ids):
                return {"status": "engineering_error", "qa_id": qa_id,
                        "reason": "gold_message_missing"}
        else:
            source_messages = []

        # Provenance + Workflow (per-thread, not shared)
        provenance = ProvenanceService(conn)
        workflow = FailureWorkflow(
            access_agent=access_agent,
            construction_agent=construction_agent,
            answer_check_agent=answer_check_agent,
            provenance=provenance,
            output_dir=str(failures_dir),
        )

        result = workflow.analyze(
            failure_id=failure_id,
            run_id=run_id,
            conversation_id=cid,
            qa_id=qa_id,
            snapshot_commit_id=int(snapshot_commit_id),
            access_run_id=access_run_id,
            question=question.question,
            prediction=pred_row["prediction"],
            reference_answer=question.reference_answer,
            gold_message_ids=gold_message_ids,
            returned_memories=returned_memories,
            source_messages=source_messages,
        )

        ar = result.access
        cr = result.construction
        ac = result.answer_check

        return {
            "status": "completed",
            "qa_id": qa_id,
            "failure_id": failure_id,
            "access_problem": getattr(ar, "problem_found", False),
            "access_subtype": getattr(ar, "primary_subtype", "unknown"),
            "construction_problem": getattr(cr, "problem_found", False),
            "construction_subtype": getattr(cr, "primary_subtype", "unknown"),
            "answer_check_correct": ac.get("correct"),
            "answer_check_status": ac.get("status"),
        }
    except Exception as exc:
        return {"status": "error", "qa_id": qa_id,
                "reason": str(exc)[:200]}
    finally:
        conn.close()


def run_conversation(
    cid: str,
    src: str,
    judge_map: dict,
    config: Any,
    access_agent: AccessDiagnosisAgent,
    construction_agent: ConstructionDiagnosisAgent,
    answer_check_agent: AnswerCheckAgent,
    qa_map: dict,
    base_dir: Path,
    run_id: str,
    workers: int,
) -> dict:
    """Run diagnosis for one conversation using thread pool."""
    src_path = Path(src)
    pred_file = src_path / "locomo_predictions.jsonl"
    db_file = src_path / "state" / "memory.sqlite3"

    predictions = _load_jsonl(pred_file)

    # Filter by judge (P or I)
    eligible = []
    for row in predictions:
        label = judge_map.get(row["qa_id"])
        if label is None or label == "C":
            continue
        if row.get("error"):
            continue
        eligible.append(row)

    conv_stats = {
        "source_qa": len(predictions),
        "eligible": len(eligible),
        "diagnosed": 0, "engineering_issues": 0,
        "access_problems": 0, "construction_problems": 0,
        "answer_check_correct": 0, "answer_check_incorrect": 0,
        "answer_check_error": 0,
    }

    if not eligible:
        return conv_stats

    failures_dir = base_dir / "failures" / cid
    failures_dir.mkdir(parents=True, exist_ok=True)
    progress_file = base_dir / "progress.jsonl"
    progress_lock = threading.Lock()

    cid_qas = {q.qa_id: q for q in qa_map.get(cid, [])}

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for pred_row in eligible:
            question = cid_qas.get(pred_row["qa_id"])
            if question is None:
                continue
            fut = pool.submit(
                diagnose_one,
                pred_row, question, cid, db_file,
                access_agent, construction_agent, answer_check_agent,
                failures_dir, run_id,
            )
            futures[fut] = pred_row["qa_id"]

        for fut in as_completed(futures):
            qa_id = futures[fut]
            try:
                r = fut.result()
            except Exception as exc:
                r = {"status": "error", "qa_id": qa_id,
                     "reason": str(exc)[:200]}

            # Thread-safe progress append
            with progress_lock:
                entry = {
                    "failure_id": r.get("failure_id", f"failure_{cid}_{qa_id}"),
                    "conversation_id": cid,
                    "qa_id": qa_id,
                    "status": r["status"],
                    "timestamp": _ts(),
                }
                with open(progress_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            if r["status"] == "completed":
                done += 1
                conv_stats["diagnosed"] += 1
                if r["access_problem"]:
                    conv_stats["access_problems"] += 1
                if r["construction_problem"]:
                    conv_stats["construction_problems"] += 1
                if r["answer_check_correct"] is True:
                    conv_stats["answer_check_correct"] += 1
                elif r["answer_check_correct"] is False:
                    conv_stats["answer_check_incorrect"] += 1
                else:
                    conv_stats["answer_check_error"] += 1
            elif r["status"] in ("engineering_error", "error"):
                conv_stats["engineering_issues"] += 1

            if done % 10 == 0:
                print(f"[{cid}] {done}/{len(eligible)} "
                      f"access={conv_stats['access_problems']} "
                      f"const={conv_stats['construction_problems']} "
                      f"ans(C/I/E)={conv_stats['answer_check_correct']}/"
                      f"{conv_stats['answer_check_incorrect']}/"
                      f"{conv_stats['answer_check_error']}",
                      flush=True)

    return conv_stats


def main() -> int:
    args = arguments()

    judge_path = Path(args.judge_results)
    if not judge_path.exists():
        print(f"ERROR: judge results not found: {judge_path}")
        return 2

    judge_map: dict[str, str] = {}
    for row in _load_jsonl(judge_path):
        judge_map[row["qa_id"]] = row["label"]

    labels = {"C": 0, "P": 0, "I": 0}
    for v in judge_map.values():
        labels[v] = labels.get(v, 0) + 1
    print(f"Judge: C={labels['C']} P={labels['P']} I={labels['I']} "
          f"→ diagnose {labels['P'] + labels['I']} (P+I)")

    config = load_config(args.config)
    base_dir = Path(args.output_dir) / args.run_id
    resume = args.resume
    if not resume:
        if base_dir.exists():
            print(f"ERROR: {base_dir} exists. Use --resume or new --run-id.")
            return 2
        base_dir.mkdir(parents=True, exist_ok=False)
    else:
        base_dir.mkdir(parents=True, exist_ok=True)

    # Shared client + agents (stateless, thread-safe)
    _maint_cfg = _copy.deepcopy(config.models["maintenance"])
    _maint_cfg.max_tokens = 8000
    _maint_cfg.supports_json_mode = False
    maintenance_model = create_client(_maint_cfg)

    access_prompt = Path(config.prompts.failure_access_diagnosis).read_text(
        encoding="utf-8")
    construction_prompt = Path(
        config.prompts.failure_construction_diagnosis).read_text(
        encoding="utf-8")
    answer_prompt = Path(config.prompts.failure_blind_reanswer).read_text(
        encoding="utf-8")

    access_agent = AccessDiagnosisAgent(maintenance_model, prompt=access_prompt)
    construction_agent = ConstructionDiagnosisAgent(
        maintenance_model, prompt=construction_prompt)
    answer_check_agent = AnswerCheckAgent(
        maintenance_model, blind_reanswer_prompt=answer_prompt)

    conversations, questions_map = load_dataset(config.dataset.path)

    work = [(cid, src) for cid, src in SOURCE_RUNS.items()]
    if args.conversations > 0:
        work = work[:args.conversations]

    # Run conversations in parallel via thread pool
    all_stats = {
        "judge_correct": labels["C"],
        "judge_partial": labels["P"],
        "judge_incorrect": labels["I"],
        "workers_per_conv": args.workers,
        "diagnosed_total": 0,
        "engineering_issue_total": 0,
        "access_problem_total": 0,
        "construction_problem_total": 0,
        "answer_check_correct_total": 0,
        "answer_check_incorrect_total": 0,
        "answer_check_error_total": 0,
        "by_conversation": {},
    }

    print(f"Starting {len(work)} conversations × {args.workers} workers...")

    conv_futures = {}
    with ThreadPoolExecutor(max_workers=len(work)) as conv_pool:
        for cid, src in work:
            fut = conv_pool.submit(
                run_conversation,
                cid, src, judge_map, config,
                access_agent, construction_agent, answer_check_agent,
                questions_map, base_dir, args.run_id, args.workers,
            )
            conv_futures[fut] = cid

        for fut in as_completed(conv_futures):
            cid = conv_futures[fut]
            try:
                cs = fut.result()
            except Exception as exc:
                print(f"ERROR {cid}: {exc}")
                cs = {"diagnosed": 0, "error": str(exc)}
            all_stats["by_conversation"][cid] = cs
            all_stats["diagnosed_total"] += cs.get("diagnosed", 0)
            all_stats["engineering_issue_total"] += cs.get("engineering_issues", 0)
            all_stats["access_problem_total"] += cs.get("access_problems", 0)
            all_stats["construction_problem_total"] += cs.get("construction_problems", 0)
            all_stats["answer_check_correct_total"] += cs.get("answer_check_correct", 0)
            all_stats["answer_check_incorrect_total"] += cs.get("answer_check_incorrect", 0)
            all_stats["answer_check_error_total"] += cs.get("answer_check_error", 0)
            print(f"[DONE] {cid}: {cs.get('diagnosed',0)}/{cs.get('eligible',0)} "
                  f"access={cs.get('access_problems',0)} "
                  f"const={cs.get('construction_problems',0)}")

    # Summary
    import json as _json
    summary_path = base_dir / "summary.json"
    tmp = summary_path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(all_stats, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(summary_path)

    print(f"\nALL DONE. {all_stats['diagnosed_total']} diagnosed.")
    print(f"Access problems: {all_stats['access_problem_total']}")
    print(f"Construction problems: {all_stats['construction_problem_total']}")
    print(f"Answer C/I/E: {all_stats['answer_check_correct_total']}/"
          f"{all_stats['answer_check_incorrect_total']}/"
          f"{all_stats['answer_check_error_total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
