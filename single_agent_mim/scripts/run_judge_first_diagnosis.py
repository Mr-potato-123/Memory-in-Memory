"""LEGACY V2 runner; do not use for Diagnosis V3.

Kept only for reproducibility of old artifacts. It uses the retired combined
FailureWorkflow and AnswerCheckAgent.
"""

from __future__ import annotations

import argparse
import copy as _copy
import json
import sqlite3
import sys
from collections import defaultdict
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


def _append_jsonl(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


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
    p.add_argument("--judge-results", required=True,
                   help="Path to LLM judge JSONL (from judge_predictions.py)")
    p.add_argument("--output-dir", default="outputs/diagnosis")
    p.add_argument("--run-id", required=True)
    p.add_argument("--max-failures", type=int, default=0)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--all-train", action="store_true")
    p.add_argument("--source-run", help="Single source run dir")
    p.add_argument("--conversation-id")
    return p.parse_args()


def main() -> int:
    args = arguments()

    # ── Load judge results ────────────────────────────────────
    judge_path = Path(args.judge_results)
    if not judge_path.exists():
        print(f"ERROR: judge results not found: {judge_path}")
        return 2

    judge_map: dict[str, str] = {}  # qa_id -> label (C/P/I)
    for row in _load_jsonl(judge_path):
        judge_map[row["qa_id"]] = row["label"]

    labels = {"C": 0, "P": 0, "I": 0}
    for v in judge_map.values():
        labels[v] = labels.get(v, 0) + 1
    print(f"Judge: C={labels['C']} P={labels['P']} I={labels['I']} "
          f"→ diagnose {labels['P'] + labels['I']} (P+I)")

    # ── Work list ─────────────────────────────────────────────
    if args.all_train:
        work = [(cid, src) for cid, src in SOURCE_RUNS.items()]
    elif args.source_run:
        cid = args.conversation_id or Path(args.source_run).name
        work = [(cid, args.source_run)]
    else:
        print("ERROR: specify --all-train or --source-run")
        return 2

    # ── Setup ─────────────────────────────────────────────────
    config = load_config(args.config)
    run_dir = Path(args.output_dir) / args.run_id
    resume = args.resume

    if not resume:
        if run_dir.exists():
            print(f"ERROR: {run_dir} exists. Use --resume or new --run-id.")
            return 2
        run_dir.mkdir(parents=True, exist_ok=False)
    else:
        run_dir.mkdir(parents=True, exist_ok=True)

    events_path = run_dir / "events.jsonl"
    progress_path = run_dir / "progress.jsonl"

    # Load completed from previous run
    completed_ids: set[str] = set()
    if resume:
        for row in _load_jsonl(progress_path):
            if row.get("status") == "completed":
                completed_ids.add(row["failure_id"])

    def emit(event: str, **kw: Any) -> None:
        row = {"timestamp": _ts(), "event": event, **kw}
        _append_jsonl(events_path, row)
        fields = " ".join(f"{k}={v}" for k, v in row.items()
                          if k not in ("timestamp", "event") and v not in (None, ""))
        print(f"[{row['timestamp']}] {event} {fields}".rstrip(), flush=True)

    emit("run_start", run_id=args.run_id)

    # Forbidden dirs check
    for d in ("skills", "candidates", "replays"):
        if (run_dir / d).exists():
            emit("abort", reason=f"forbidden: {d}")
            return 3

    # ── Maintenance client ────────────────────────────────────
    _maint_cfg = _copy.deepcopy(config.models["maintenance"])
    _maint_cfg.max_tokens = 8000
    _maint_cfg.supports_json_mode = False
    maintenance_model = create_client(_maint_cfg)
    emit("maintenance_ready", model=_maint_cfg.model)

    # ── Agents ────────────────────────────────────────────────
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
    emit("agents_ready")

    # ── Dataset ───────────────────────────────────────────────
    conversations, questions_map = load_dataset(config.dataset.path)
    emit("dataset_loaded", conversations=len(conversations))

    # ── Stats ─────────────────────────────────────────────────
    stats = {
        "judge_correct": labels["C"],
        "judge_partial": labels["P"],
        "judge_incorrect": labels["I"],
        "diagnosed_total": 0,
        "engineering_issue_total": 0,
        "access_problem_total": 0,
        "construction_problem_total": 0,
        "answer_check_correct_total": 0,
        "answer_check_incorrect_total": 0,
        "answer_check_error_total": 0,
        "access_subtypes": defaultdict(int),
        "construction_subtypes": defaultdict(int),
        "by_conversation": {},
    }

    total_diagnosed = 0
    max_failures = args.max_failures or 99999

    for cid, src in work:
        if total_diagnosed >= max_failures:
            break

        emit("conversation_start", conversation_id=cid)
        src_path = Path(src)
        pred_file = src_path / "locomo_predictions.jsonl"
        db_file = src_path / "state" / "memory.sqlite3"

        predictions = _load_jsonl(pred_file)

        # ── Filter by judge (P or I only) ──────────────────
        eligible = []
        skipped_correct = 0
        skipped_no_judge = 0
        for row in predictions:
            qa_id = row["qa_id"]
            label = judge_map.get(qa_id)
            if label is None:
                skipped_no_judge += 1
                continue
            if label == "C":
                skipped_correct += 1
                continue
            if row.get("error"):
                continue  # Runtime error, not a diagnosis candidate
            eligible.append(row)

        print(f"  {cid}: {len(predictions)} QA → {len(eligible)} to diagnose "
              f"(skipped C={skipped_correct} no_judge={skipped_no_judge})")

        conv_stats = {
            "source_qa": len(predictions),
            "eligible": len(eligible),
            "diagnosed": 0,
            "engineering_issues": 0,
            "access_problems": 0,
            "construction_problems": 0,
            "answer_check_correct": 0,
            "answer_check_incorrect": 0,
            "answer_check_error": 0,
        }

        if not eligible:
            emit("conversation_complete", conversation_id=cid, diagnosed=0)
            stats["by_conversation"][cid] = conv_stats
            continue

        conn = _open_readonly(db_file)
        failures_dir = run_dir / "failures" / cid
        failures_dir.mkdir(parents=True, exist_ok=True)

        provenance = ProvenanceService(conn)
        workflow = FailureWorkflow(
            access_agent=access_agent,
            construction_agent=construction_agent,
            answer_check_agent=answer_check_agent,
            provenance=provenance,
            output_dir=str(failures_dir),
        )

        qas = questions_map.get(cid, [])
        qa_map = {q.qa_id: q for q in qas}

        conv_diagnosed = 0
        for failure_idx, pred_row in enumerate(eligible):
            if total_diagnosed >= max_failures:
                break

            qa_id = pred_row["qa_id"]
            failure_id = f"failure_{cid}_{qa_id}"

            if failure_id in completed_ids:
                if (failures_dir / f"{failure_id}_diagnoses.json").exists():
                    continue

            question = qa_map.get(qa_id)
            if question is None:
                stats["engineering_issue_total"] += 1
                conv_stats["engineering_issues"] += 1
                continue

            gold_message_ids = [
                item[-1] for item in question.source_evidence
                if item and item[-1]
            ]

            # ── Find access run ──────────────────────────────
            access_row = conn.execute(
                """SELECT access_run_id, snapshot_commit_id, prediction
                   FROM access_runs
                   WHERE conversation_id = ? AND qa_id = ?
                     AND status = 'completed'
                   ORDER BY created_at DESC LIMIT 1""",
                (cid, qa_id),
            ).fetchone()

            if not access_row:
                stats["engineering_issue_total"] += 1
                conv_stats["engineering_issues"] += 1
                continue

            access_run_id = access_row["access_run_id"]
            snapshot_commit_id = access_row["snapshot_commit_id"]

            # ── Returned memories ────────────────────────────
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

            # ── Source messages ──────────────────────────────
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
                    stats["engineering_issue_total"] += 1
                    conv_stats["engineering_issues"] += 1
                    continue
            else:
                source_messages = []

            # ── Run diagnosis ────────────────────────────────
            emit("failure_start", conversation_id=cid, qa_id=qa_id,
                 index=f"{conv_diagnosed + 1}/{len(eligible)}")

            try:
                result = workflow.analyze(
                    failure_id=failure_id,
                    run_id=args.run_id,
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
            except Exception as exc:
                _append_jsonl(progress_path, {
                    "failure_id": failure_id, "conversation_id": cid,
                    "qa_id": qa_id, "status": "failed",
                    "error": str(exc)[:200], "timestamp": _ts(),
                })
                stats["engineering_issue_total"] += 1
                conv_stats["engineering_issues"] += 1
                emit("diagnosis_error", conversation_id=cid, qa_id=qa_id)
                continue

            # ── Record ───────────────────────────────────────
            ar = result.access
            cr = result.construction
            ac = result.answer_check

            has_ap = getattr(ar, "problem_found", False)
            has_cp = getattr(cr, "problem_found", False)
            ap_sub = getattr(ar, "primary_subtype", "unknown")
            cp_sub = getattr(cr, "primary_subtype", "unknown")
            ac_correct = ac.get("correct")

            _append_jsonl(progress_path, {
                "failure_id": failure_id, "conversation_id": cid,
                "qa_id": qa_id, "status": "completed", "timestamp": _ts(),
            })

            if has_ap:
                stats["access_problem_total"] += 1
                conv_stats["access_problems"] += 1
                stats["access_subtypes"][ap_sub] += 1
            if has_cp:
                stats["construction_problem_total"] += 1
                conv_stats["construction_problems"] += 1
                stats["construction_subtypes"][cp_sub] += 1
            if ac_correct is True:
                stats["answer_check_correct_total"] += 1
                conv_stats["answer_check_correct"] += 1
            elif ac_correct is False:
                stats["answer_check_incorrect_total"] += 1
                conv_stats["answer_check_incorrect"] += 1
            else:
                stats["answer_check_error_total"] += 1
                conv_stats["answer_check_error"] += 1

            stats["diagnosed_total"] += 1
            conv_stats["diagnosed"] += 1
            conv_diagnosed += 1
            total_diagnosed += 1

            emit("failure_complete", conversation_id=cid, qa_id=qa_id,
                 access="problem" if has_ap else "clean",
                 construction="problem" if has_cp else "clean",
                 answer_check="correct" if ac_correct else
                 "incorrect" if ac_correct is False else "error")

            if conv_diagnosed % 10 == 0:
                print(f"[progress] {cid} {conv_diagnosed}/{len(eligible)} "
                      f"access={conv_stats['access_problems']} "
                      f"const={conv_stats['construction_problems']} "
                      f"ans(C/I/E)={conv_stats['answer_check_correct']}/"
                      f"{conv_stats['answer_check_incorrect']}/"
                      f"{conv_stats['answer_check_error']}",
                      flush=True)

        conn.close()
        stats["by_conversation"][cid] = conv_stats
        emit("conversation_complete", conversation_id=cid,
             diagnosed=conv_diagnosed)

    # ── Summary ──────────────────────────────────────────────
    stats["access_subtypes"] = dict(stats["access_subtypes"])
    stats["construction_subtypes"] = dict(stats["construction_subtypes"])
    _write_json(run_dir / "summary.json", stats)
    _write_json(run_dir / "manifest.json", {
        "run_id": args.run_id,
        "filter": "judge_label_P_or_I",
        "judge_source": str(judge_path),
        "created_at": _ts(),
    })

    emit("run_complete", diagnosed_total=stats["diagnosed_total"],
         access_problems=stats["access_problem_total"],
         construction_problems=stats["construction_problem_total"])

    print(f"\nDone. {stats['diagnosed_total']} diagnosed.")
    print(f"Access problems: {stats['access_problem_total']}")
    print(f"Construction problems: {stats['construction_problem_total']}")
    print(f"Answer check C/I/E: {stats['answer_check_correct_total']}/"
          f"{stats['answer_check_incorrect_total']}/"
          f"{stats['answer_check_error_total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
