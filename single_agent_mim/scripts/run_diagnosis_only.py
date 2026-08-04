"""LEGACY V2 runner; do not use for Diagnosis V3.

Kept only so older experiment artifacts remain reproducible. Use
``run_answer_failure.py``, then run ``run_access_failure.py`` and
``run_cons_failure.py`` in parallel.
"""

from __future__ import annotations

import argparse
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

# ── Verbatim import check ──────────────────────────────────────────
_FORBIDDEN_IMPORTS = (
    "mim.agents.skill_maker",
    "mim.skill_maker",
    "MiMTrainer",
    "SkillMakerWorkflow",
    "SkillRepository",
    "repair_access_failure",
    "repair_construction_failure",
    "stage_create",
    "stage_update",
    "publish",
    "MiMRuntime",
)

FROZEN_TRAIN = [
    "conv-30", "conv-42", "conv-43", "conv-44", "conv-48", "conv-49",
]

_MISSING = object()


# ── Helpers ────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
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


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _load_progress(path: Path) -> set[str]:
    completed: set[str] = set()
    for row in _load_jsonl(path):
        if row.get("status") == "completed":
            completed.add(row["failure_id"])
    return completed


# ── Main ───────────────────────────────────────────────────────────

def arguments() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    p.add_argument("--source-run", help="Path to a single source run directory")
    p.add_argument("--conversation-id")
    p.add_argument("--output-dir", default="outputs")
    p.add_argument("--diagnosis-run-id", required=True)
    p.add_argument("--max-failures", type=int, default=0,
                   help="Cap number of failures to diagnose (0 = unlimited)")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--all-train", action="store_true")
    return p.parse_args()


def main() -> int:
    args = arguments()

    if args.all_train and (args.source_run or args.conversation_id):
        print("ERROR: --all-train cannot be combined with --source-run or "
              "--conversation-id")
        return 2

    if not args.all_train and not args.source_run:
        print("ERROR: specify --source-run (single) or --all-train (all six)")
        return 2

    config = load_config(args.config)
    run_dir = Path(args.output_dir) / args.diagnosis_run_id

    # ── Open logs ──────────────────────────────────────────────
    resume = args.resume
    if not resume:
        if run_dir.exists():
            print(f"ERROR: {run_dir} already exists. Use --resume or "
                  f"a different --diagnosis-run-id.")
            return 2
        run_dir.mkdir(parents=True, exist_ok=False)
    else:
        if not run_dir.exists():
            run_dir.mkdir(parents=True, exist_ok=False)

    events_path = run_dir / "events.jsonl"
    progress_path = run_dir / "diagnosis_progress.jsonl"
    issues_path = run_dir / "engineering_issues.jsonl"
    index_path = run_dir / "failures" / "index.jsonl"
    failures_base = run_dir / "failures"

    completed_ids = _load_progress(progress_path) if resume else set()

    def emit(event: str, **kw: Any) -> None:
        row = {"timestamp": _ts(), "event": event, **kw}
        _append_jsonl(events_path, row)
        fields = " ".join(f"{k}={v}" for k, v in row.items()
                          if k not in ("timestamp", "event") and v not in (None, ""))
        print(f"[{row['timestamp']}] {event} {fields}".rstrip(), flush=True)

    emit("diagnosis_run_start", diagnosis_run_id=args.diagnosis_run_id)

    # ── Overwrite protection ───────────────────────────────────
    for forbidden in ("skills", "candidates", "replays"):
        if (run_dir / forbidden).exists():
            emit("abort", reason=f"forbidden directory exists: {forbidden}")
            return 3

    # ── Maintenance client (shared, created once) ──────────────
    # NOTE: reasoning_effort and thinking extra_body cause DeepSeek to
    # consume output tokens for internal reasoning. The config supplies
    # max_tokens=4000 which is sufficient for diagnosis calls but we
    # raise it to 8000 as a safety margin for answer_check (which passes
    # max_tokens=700 per call — thinking may use a portion of that).
    # We also disable supports_json_mode because the DeepSeek endpoint
    # requires the prompt to contain "json" for json_object format,
    # and the answer judge prompt does not include that word.
    import copy as _copy
    _maint_cfg = _copy.deepcopy(config.models["maintenance"])
    _maint_cfg.max_tokens = 8000
    _maint_cfg.supports_json_mode = False
    maintenance_model = create_client(_maint_cfg)
    emit("maintenance_client_ready", model=_maint_cfg.model,
         max_tokens=_maint_cfg.max_tokens)

    # ── Prompts ────────────────────────────────────────────────
    access_prompt = Path(config.prompts.failure_access_diagnosis).read_text(
        encoding="utf-8")
    construction_prompt = Path(
        config.prompts.failure_construction_diagnosis).read_text(
        encoding="utf-8")
    answer_prompt = Path(config.prompts.failure_blind_reanswer).read_text(
        encoding="utf-8")

    # ── Shared agents ──────────────────────────────────────────
    access_agent = AccessDiagnosisAgent(maintenance_model, prompt=access_prompt)
    construction_agent = ConstructionDiagnosisAgent(
        maintenance_model, prompt=construction_prompt)
    answer_check_agent = AnswerCheckAgent(
        maintenance_model, blind_reanswer_prompt=answer_prompt)
    emit("agents_initialized")

    # ── Dataset (once) ─────────────────────────────────────────
    conversations, questions_map = load_dataset(config.dataset.path)
    conv_map = {c.conversation_id: c for c in conversations}
    emit("dataset_loaded", conversation_count=len(conversations))

    # ── Determine work list ────────────────────────────────────
    if args.all_train:
        work: list[tuple[str, str]] = []
        for cid in FROZEN_TRAIN:
            cid_flat = cid.replace("-", "")
            src = f"outputs/nsc_train/nsc_train_{cid_flat}_v1"
            work.append((cid, src))
    else:
        cid = args.conversation_id
        if not cid:
            cid = Path(args.source_run).name.replace("nsc_train_", "").replace("_v1", "")
            if cid.startswith("conv-"):
                cid = cid[:7]  # "conv-XX"
        work = [(cid, args.source_run)]

    # ── Validate source directories ────────────────────────────
    for cid, src in work:
        src_path = Path(src)
        pred_file = src_path / "locomo_predictions.jsonl"
        db_file = src_path / "state" / "memory.sqlite3"
        if not pred_file.exists():
            emit("source_validation_error", conversation_id=cid,
                 reason=f"missing predictions: {pred_file}")
            return 3
        if not db_file.exists():
            emit("source_validation_error", conversation_id=cid,
                 reason=f"missing sqlite: {db_file}")
            return 3
    emit("source_validation_passed")

    # ── Manifest ───────────────────────────────────────────────
    manifest: dict[str, Any] = {
        "diagnosis_run_id": args.diagnosis_run_id,
        "diagnosis_model": config.models["maintenance"].model,
        "runtime_model": "qwen3-8b",
        "runtime_reexecuted": False,
        "skill_maker_executed": False,
        "split": "train",
        "created_at": _ts(),
        "source_runs": {cid: src for cid, src in work},
    }
    _write_json(run_dir / "manifest.json", manifest)

    # ── Global stats ───────────────────────────────────────────
    stats = {
        "diagnosis_model": config.models["maintenance"].model,
        "runtime_model": "qwen3-8b",
        "runtime_reexecuted": False,
        "skill_maker_executed": False,
        "split": "train",
        "source_qa_total": 0,
        "eligible_failure_total": 0,
        "diagnosed_total": 0,
        "engineering_issue_total": 0,
        "access_problem_total": 0,
        "construction_problem_total": 0,
        "answer_check_correct_total": 0,
        "answer_check_incorrect_total": 0,
        "answer_check_model_error_total": 0,
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
        stats["source_qa_total"] += len(predictions)

        # Select eligible failures
        eligible: list[dict] = []
        for row in predictions:
            if row.get("error"):
                continue
            if float(row.get("f1", 0.0)) >= 0.5:
                continue
            eligible.append(row)
        stats["eligible_failure_total"] += len(eligible)

        conv_stats = {
            "source_qa": len(predictions),
            "eligible_failures": len(eligible),
            "diagnosed": 0,
            "engineering_issues": 0,
            "access_problems": 0,
            "construction_problems": 0,
            "answer_check_correct": 0,
            "answer_check_incorrect": 0,
            "answer_check_model_error": 0,
        }

        if not eligible:
            emit("conversation_complete", conversation_id=cid, diagnosed=0)
            stats["by_conversation"][cid] = conv_stats
            continue

        # Open source DB read-only
        conn = _open_readonly(db_file)

        # Failure output directory for this conversation
        conv_failure_dir = failures_base / cid
        conv_failure_dir.mkdir(parents=True, exist_ok=True)

        # Provenance service per DB
        provenance = ProvenanceService(conn)
        workflow = FailureWorkflow(
            access_agent=access_agent,
            construction_agent=construction_agent,
            answer_check_agent=answer_check_agent,
            provenance=provenance,
            output_dir=str(conv_failure_dir),
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
                # Check if reports exist
                if ((conv_failure_dir / f"{failure_id}_report.json").exists() or
                    (conv_failure_dir / f"{failure_id}_diagnoses.json").exists()):
                    continue

            question = qa_map.get(qa_id)
            if question is None:
                _append_jsonl(issues_path, {
                    "timestamp": _ts(),
                    "conversation_id": cid,
                    "qa_id": qa_id,
                    "reason": "qa_not_found_in_dataset",
                })
                stats["engineering_issue_total"] += 1
                conv_stats["engineering_issues"] += 1
                continue

            # ── 8.2 Find access run ──────────────────────────
            gold_message_ids = [
                item[-1] for item in question.source_evidence
                if item and item[-1]
            ]

            access_row = conn.execute(
                """SELECT access_run_id, run_id, conversation_id, qa_id,
                          snapshot_commit_id, prediction, status
                   FROM access_runs
                   WHERE conversation_id = ? AND qa_id = ?
                     AND status = 'completed'
                   ORDER BY created_at DESC, access_run_id DESC
                   LIMIT 1""",
                (cid, qa_id),
            ).fetchone()

            if access_row is None:
                _append_jsonl(issues_path, {
                    "timestamp": _ts(),
                    "conversation_id": cid,
                    "qa_id": qa_id,
                    "reason": "no_completed_access_run",
                })
                stats["engineering_issue_total"] += 1
                conv_stats["engineering_issues"] += 1
                continue

            if access_row["prediction"] != pred_row.get("prediction", ""):
                _append_jsonl(issues_path, {
                    "timestamp": _ts(),
                    "conversation_id": cid,
                    "qa_id": qa_id,
                    "reason": "prediction_mismatch",
                })
                stats["engineering_issue_total"] += 1
                conv_stats["engineering_issues"] += 1
                continue

            snapshot_commit_id = access_row["snapshot_commit_id"]
            access_run_id = access_row["access_run_id"]
            if not snapshot_commit_id or not access_run_id:
                _append_jsonl(issues_path, {
                    "timestamp": _ts(),
                    "conversation_id": cid,
                    "qa_id": qa_id,
                    "reason": "missing_commit_or_access_run_id",
                })
                stats["engineering_issue_total"] += 1
                conv_stats["engineering_issues"] += 1
                continue

            # ── 8.3 Returned memories (access_answer_context) ─
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

            # ── 8.4 Source messages (gold evidence) ───────────
            if gold_message_ids:
                placeholders = ",".join("?" for _ in gold_message_ids)
                src_rows = conn.execute(
                    f"""SELECT message_id, conversation_id, session_id,
                               turn_index, role, speaker, content, occurred_at
                        FROM messages
                        WHERE conversation_id = ?
                          AND message_id IN ({placeholders})
                        ORDER BY session_id, turn_index""",
                    (cid, *gold_message_ids),
                ).fetchall()
                source_messages = [dict(r) for r in src_rows]

                found_ids = {r["message_id"] for r in src_rows}
                missing = [mid for mid in gold_message_ids if mid not in found_ids]
                if missing:
                    _append_jsonl(issues_path, {
                        "timestamp": _ts(),
                        "conversation_id": cid,
                        "qa_id": qa_id,
                        "reason": "gold_message_missing",
                        "missing_message_ids": missing,
                    })
                    stats["engineering_issue_total"] += 1
                    conv_stats["engineering_issues"] += 1
                    continue
            else:
                source_messages = []

            # ── Run diagnosis ─────────────────────────────────
            emit("failure_start", conversation_id=cid, qa_id=qa_id,
                 index=f"{conv_diagnosed + 1}/{len(eligible)}")

            try:
                result = workflow.analyze(
                    failure_id=failure_id,
                    run_id=args.diagnosis_run_id,
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
                _append_jsonl(issues_path, {
                    "timestamp": _ts(),
                    "conversation_id": cid,
                    "qa_id": qa_id,
                    "reason": "model_error",
                    "error": str(exc),
                })
                _append_jsonl(progress_path, {
                    "failure_id": failure_id,
                    "conversation_id": cid,
                    "qa_id": qa_id,
                    "status": "failed",
                    "timestamp": _ts(),
                })
                stats["engineering_issue_total"] += 1
                conv_stats["engineering_issues"] += 1
                emit("diagnosis_error", conversation_id=cid, qa_id=qa_id,
                     error=str(exc)[:120])
                conn.close()
                continue

            # ── Index entry ───────────────────────────────────
            access_report = result.access
            construction_report = result.construction
            answer_check = result.answer_check

            has_access_problem = getattr(
                access_report, "problem_found", False)
            has_construction_problem = getattr(
                construction_report, "problem_found", False)
            access_subtype = getattr(
                access_report, "primary_subtype", "unknown")
            construction_subtype = getattr(
                construction_report, "primary_subtype", "unknown")
            access_route = getattr(
                access_report, "recommended_route", "record_only")
            construction_route = getattr(
                construction_report, "recommended_route", "record_only")
            answer_correct = answer_check.get("correct")
            answer_status = answer_check.get("status", "unknown")
            component_errors = [
                name
                for name, status in (
                    (
                        "access",
                        getattr(
                            access_report.status,
                            "value",
                            str(access_report.status),
                        ),
                    ),
                    (
                        "construction",
                        getattr(
                            construction_report.status,
                            "value",
                            str(construction_report.status),
                        ),
                    ),
                    ("answer_check", str(answer_status)),
                )
                if status == "model_error"
            ]

            index_entry = {
                "failure_id": failure_id,
                "conversation_id": cid,
                "qa_id": qa_id,
                "category": pred_row.get("category"),
                "f1": pred_row.get("f1"),
                "prediction": pred_row.get("prediction"),
                "reference_answer": question.reference_answer,
                "source_run": src,
                "access_problem_found": has_access_problem,
                "access_subtype": access_subtype,
                "access_route": access_route,
                "construction_problem_found": has_construction_problem,
                "construction_subtype": construction_subtype,
                "construction_route": construction_route,
                "answer_check_correct": answer_correct,
                "answer_check_status": answer_status,
                "access_report_path": str(
                    conv_failure_dir / f"{failure_id}_access_report.json"),
                "construction_report_path": str(
                    conv_failure_dir / f"{failure_id}_construction_report.json"),
                "combined_report_path": str(
                    conv_failure_dir / f"{failure_id}_diagnoses.json"),
            }
            _append_jsonl(index_path, index_entry)

            # ── Progress ──────────────────────────────────────
            _append_jsonl(progress_path, {
                "failure_id": failure_id,
                "conversation_id": cid,
                "qa_id": qa_id,
                "status": (
                    "partial_model_error"
                    if component_errors
                    else "completed"
                ),
                "component_errors": component_errors,
                "timestamp": _ts(),
            })

            if component_errors:
                _append_jsonl(issues_path, {
                    "timestamp": _ts(),
                    "conversation_id": cid,
                    "qa_id": qa_id,
                    "reason": "component_model_error",
                    "components": component_errors,
                })
                stats["engineering_issue_total"] += 1
                conv_stats["engineering_issues"] += 1

            # ── Update stats ──────────────────────────────────
            if has_access_problem:
                stats["access_problem_total"] += 1
                conv_stats["access_problems"] += 1
                stats["access_subtypes"][access_subtype] += 1
            if has_construction_problem:
                stats["construction_problem_total"] += 1
                conv_stats["construction_problems"] += 1
                stats["construction_subtypes"][construction_subtype] += 1
            if answer_correct is True:
                stats["answer_check_correct_total"] += 1
                conv_stats["answer_check_correct"] += 1
            elif answer_correct is False:
                stats["answer_check_incorrect_total"] += 1
                conv_stats["answer_check_incorrect"] += 1
            else:
                stats["answer_check_model_error_total"] += 1
                conv_stats["answer_check_model_error"] += 1

            stats["diagnosed_total"] += 1
            conv_stats["diagnosed"] += 1
            conv_diagnosed += 1
            total_diagnosed += 1

            emit("failure_complete",
                 conversation_id=cid, qa_id=qa_id,
                 access=("problem" if has_access_problem else "clean"),
                 construction=("problem" if has_construction_problem else "clean"),
                 answer_check=("correct" if answer_correct else
                               "incorrect" if answer_correct is False else "error"))

            print(f"[diagnosis] {cid} {conv_diagnosed}/{len(eligible)} "
                  f"qa_id={qa_id} "
                  f"access={'PROBLEM' if has_access_problem else 'ok'} "
                  f"construction={'PROBLEM' if has_construction_problem else 'ok'} "
                  f"answer_check={'correct' if answer_correct else 'incorrect' if answer_correct is False else 'error'}",
                  flush=True)

        conn.close()
        stats["by_conversation"][cid] = conv_stats
        emit("conversation_complete", conversation_id=cid,
             diagnosed=conv_diagnosed)

    # ── Final checks ──────────────────────────────────────────
    forbidden_dirs = ["skills", "candidates", "replays"]
    for d in forbidden_dirs:
        if (run_dir / d).exists():
            emit("abort", reason=f"forbidden directory detected: {d}")
            return 3

    # ── Dedup index ───────────────────────────────────────────
    all_index = _load_jsonl(index_path)
    seen = set()
    deduped = []
    for row in all_index:
        if row["failure_id"] not in seen:
            seen.add(row["failure_id"])
            deduped.append(row)
    if len(deduped) != len(all_index):
        # Rewrite deduped index
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with open(index_path, "w", encoding="utf-8") as f:
            for row in deduped:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ── Summary ───────────────────────────────────────────────
    stats["access_subtypes"] = dict(stats["access_subtypes"])
    stats["construction_subtypes"] = dict(stats["construction_subtypes"])
    _write_json(run_dir / "diagnosis_summary.json", stats)

    # ── Verification ──────────────────────────────────────────
    ok = True
    for d in forbidden_dirs:
        assert not (run_dir / d).exists(), f"{d} exists!"
    failure_ids = [r["failure_id"] for r in deduped]
    assert len(failure_ids) == len(set(failure_ids)), "duplicate failure IDs"
    for row in deduped:
        assert Path(row["access_report_path"]).exists(), \
            f"missing: {row['access_report_path']}"
        assert Path(row["construction_report_path"]).exists(), \
            f"missing: {row['construction_report_path']}"
        assert Path(row["combined_report_path"]).exists(), \
            f"missing: {row['combined_report_path']}"
    emit("verification_passed", failures_checked=len(deduped))

    emit("diagnosis_run_complete",
         diagnosed_total=stats["diagnosed_total"],
         engineering_issues=stats["engineering_issue_total"],
         access_problems=stats["access_problem_total"],
         construction_problems=stats["construction_problem_total"])

    print(f"\nDone. {stats['diagnosed_total']} diagnosed, "
          f"{stats['engineering_issue_total']} engineering issues.")
    print(f"Access problems: {stats['access_problem_total']}")
    print(f"Construction problems: {stats['construction_problem_total']}")
    print(f"Answer check correct: {stats['answer_check_correct_total']}")
    print(f"Answer check incorrect: {stats['answer_check_incorrect_total']}")
    print(f"Answer check errors: {stats['answer_check_model_error_total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
