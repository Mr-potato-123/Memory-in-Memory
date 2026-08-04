"""Iterative V2 experiment orchestrator.

Pipeline per iteration:
  train(6-way parallel build + parallel QA) -> LLM Judge -> V3 Diagnosis
  (answer -> access+cons) -> candidates -> V2 draft-first Skill Bank -> prune.

Iteration 2 reuses the pruned iteration-1 memory (no rebuild) with the Bank1
Skill Bank, giving a same-memory / different-skill clean comparison. After the
second iteration the validation set is built once with Bank2 and evaluated as
four variants (bank0 / full / acc-only / cons-only) over the same memory.

Usage:
  python scripts/run_mim_iterative_v2.py --config configs/qwen3_8b_dashscope.yaml \
      --output-root outputs/v2_iter [--smoke-qa 8] [--max-convs 1]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

TRAIN_CONVS = ["conv-30", "conv-42", "conv-43", "conv-44", "conv-48", "conv-49"]
VAL_CONVS = ["conv-26", "conv-41"]


def run_or_skip(cmd: list[str], marker: Path | None, label: str) -> None:
    if marker is not None and marker.exists():
        print(f"[skip] {label}: {marker} exists", flush=True)
        return
    print(f"[run ] {label}: {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f"FAILED: {label} (exit {proc.returncode})")
    print(f"[done] {label}", flush=True)


def train_iter_cmd(
    split: str,
    run_root: Path,
    *,
    skill_bank_dir: Path | None = None,
    resume_db_from: Path | None = None,
    qa_workers: int,
    smoke_qa: int,
    max_convs: int,
) -> list[str]:
    cmd = [
        sys.executable, str(SCRIPTS / "run_train_iter.py"),
        "--config", str(ROOT / "configs/qwen3_8b_dashscope.yaml"),
        "--split", split,
        "--run-root", str(run_root),
        "--qa-workers", str(qa_workers),
    ]
    if skill_bank_dir is not None:
        cmd += ["--skill-bank-dir", str(skill_bank_dir)]
    if resume_db_from is not None:
        cmd += ["--resume-db-from", str(resume_db_from)]
    if smoke_qa:
        cmd += ["--smoke-qa", str(smoke_qa)]
    if max_convs:
        cmd += ["--max-convs", str(max_convs)]
    return cmd


def judge_cmd(pred_files: list[Path], output_dir: Path) -> list[str]:
    # judge refuses an existing output dir; clean stale partial runs first.
    if output_dir.exists() and not (output_dir / "summary.json").exists():
        shutil.rmtree(output_dir, ignore_errors=True)
    return [
        sys.executable, str(SCRIPTS / "judge_predictions.py"),
        "--config", str(ROOT / "configs/qwen3_8b_dashscope.yaml"),
        "--output-dir", str(output_dir),
        "--workers", "12",
        *[str(p) for p in pred_files],
    ]


def diagnosis_cmds(
    component: str,
    judge_dir: Path,
    run_root: Path,
    output_root: Path,
    convs: list[str],
    run_id: str,
) -> list[str]:
    cmd = [
        sys.executable, str(SCRIPTS / f"run_{component}_failure.py"),
        "--config", str(ROOT / "configs/qwen3_8b_dashscope.yaml"),
        "--judge-results", str(judge_dir / "judgments.jsonl"),
        "--diagnosis-run-id", run_id,
        "--output-root", str(output_root),
        "--workers", "4",
    ]
    component_dir = {
        "answer": "answer_failure",
        "access": "access_failure",
        "cons": "cons_failure",
    }[component]
    if (output_root / component_dir).exists():
        cmd += ["--resume"]
    for cid in convs:
        cmd += ["--source-run", f"{cid}={run_root / cid}"]
    return cmd


def candidates_cmd(
    diagnosis_root: Path,
    skills_dir: Path,
    workers: int,
) -> list[str]:
    return [
        sys.executable, str(SCRIPTS / "run_candidates_from_diagnosis.py"),
        "--config", str(ROOT / "configs/qwen3_8b_dashscope.yaml"),
        "--diagnosis-root", str(diagnosis_root),
        "--skills-dir", str(skills_dir),
        "--workers", str(workers),
    ]


def pipeline_cmd(
    candidates_root: Path,
    run_id: str,
    output_root: Path,
    initial_bank: Path | None,
) -> list[str]:
    cmd = [
        sys.executable, str(SCRIPTS / "run_skill_bank_pipeline_v2.py"),
        "--config", str(ROOT / "configs/qwen3_8b_dashscope.yaml"),
        "--source-candidates", str(candidates_root),
        "--run-id", run_id,
        "--output-root", str(output_root),
        "--workers", "6",
    ]
    if (output_root / run_id).exists():
        cmd += ["--resume"]
    if initial_bank is not None:
        cmd += ["--initial-skill-bank-dir", str(initial_bank)]
    return cmd


def prune_cmd(run_root: Path, convs: list[str]) -> list[str]:
    cmd = [
        sys.executable, str(SCRIPTS / "prune_memory.py"),
        "--run-root", str(run_root),
    ]
    for cid in convs:
        cmd += ["--conversation-id", cid]
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    parser.add_argument("--output-root", default="outputs/v2_iter")
    parser.add_argument("--qa-workers", type=int, default=6)
    parser.add_argument("--smoke-qa", type=int, default=0,
                        help="Answer only the first N questions per conversation.")
    parser.add_argument("--max-convs", type=int, default=0,
                        help="Cap conversations per stage (smoke testing).")
    args = parser.parse_args()

    root = Path(args.output_root)
    train_convs = TRAIN_CONVS[: args.max_convs] if args.max_convs else TRAIN_CONVS
    val_convs = VAL_CONVS[: args.max_convs] if args.max_convs else VAL_CONVS

    # ── Iteration 1: Bank0 train ──────────────────────────────
    iter1 = root / "iter1"
    run_or_skip(
        train_iter_cmd("train", iter1 / "train", qa_workers=args.qa_workers,
                       smoke_qa=args.smoke_qa, max_convs=args.max_convs),
        iter1 / "train" / "summary.json", "iter1 train",
    )
    judge_marker = iter1 / "judge" / "summary.json"
    pred_files = [iter1 / "train" / cid / "locomo_predictions.jsonl"
                  for cid in train_convs]
    run_or_skip(
        judge_cmd(pred_files, iter1 / "judge"),
        judge_marker, "iter1 judge",
    )
    diag_marker = iter1 / "diagnosis" / "cons_failure" / "summary.json"
    run_or_skip(
        diagnosis_cmds("answer", iter1 / "judge", iter1 / "train",
                       iter1 / "diagnosis", train_convs, "v2_iter_iter1_diag"),
        iter1 / "diagnosis" / "answer_failure" / "summary.json",
        "iter1 diagnosis: answer",
    )
    # access + cons run concurrently after answer phase.
    print("[run ] iter1 diagnosis: access + cons (concurrent)", flush=True)
    proc_a = subprocess.Popen(
        diagnosis_cmds("access", iter1 / "judge", iter1 / "train",
                       iter1 / "diagnosis", train_convs, "v2_iter_iter1_diag"),
        cwd=str(ROOT),
    )
    proc_c = subprocess.Popen(
        diagnosis_cmds("cons", iter1 / "judge", iter1 / "train",
                       iter1 / "diagnosis", train_convs, "v2_iter_iter1_diag"),
        cwd=str(ROOT),
    )
    if proc_a.wait() != 0 or proc_c.wait() != 0:
        raise SystemExit("FAILED: iter1 diagnosis access/cons")
    if not diag_marker.exists():
        raise SystemExit("iter1 cons diagnosis produced no summary")

    run_or_skip(
        candidates_cmd(iter1 / "diagnosis", iter1 / "skills", workers=8),
        iter1 / "skills" / "candidates" / "generation_summary.json",
        "iter1 candidates",
    )
    run_or_skip(
        pipeline_cmd(iter1 / "skills" / "candidates", "bank1",
                     iter1, None),
        iter1 / "bank1" / "summary.json",
        "iter1 skill bank (bank1)",
    )
    run_or_skip(
        prune_cmd(iter1 / "train", train_convs),
        iter1 / "train" / "prune_summary.json",
        "iter1 prune",
    )

    # ── Iteration 2: Bank1 train over pruned memory ───────────
    iter2 = root / "iter2"
    bank1_published = iter1 / "bank1" / "skills" / "published_bank1_full"
    run_or_skip(
        train_iter_cmd("train", iter2 / "train",
                       skill_bank_dir=bank1_published,
                       resume_db_from=iter1 / "train",
                       qa_workers=args.qa_workers,
                       smoke_qa=args.smoke_qa, max_convs=args.max_convs),
        iter2 / "train" / "summary.json", "iter2 train",
    )
    pred_files2 = [iter2 / "train" / cid / "locomo_predictions.jsonl"
                   for cid in train_convs]
    run_or_skip(
        judge_cmd(pred_files2, iter2 / "judge"),
        iter2 / "judge" / "summary.json", "iter2 judge",
    )
    run_or_skip(
        diagnosis_cmds("answer", iter2 / "judge", iter2 / "train",
                       iter2 / "diagnosis", train_convs, "v2_iter_iter2_diag"),
        iter2 / "diagnosis" / "answer_failure" / "summary.json",
        "iter2 diagnosis: answer",
    )
    print("[run ] iter2 diagnosis: access + cons (concurrent)", flush=True)
    proc_a2 = subprocess.Popen(
        diagnosis_cmds("access", iter2 / "judge", iter2 / "train",
                       iter2 / "diagnosis", train_convs, "v2_iter_iter2_diag"),
        cwd=str(ROOT),
    )
    proc_c2 = subprocess.Popen(
        diagnosis_cmds("cons", iter2 / "judge", iter2 / "train",
                       iter2 / "diagnosis", train_convs, "v2_iter_iter2_diag"),
        cwd=str(ROOT),
    )
    if proc_a2.wait() != 0 or proc_c2.wait() != 0:
        raise SystemExit("FAILED: iter2 diagnosis access/cons")
    if not (iter2 / "diagnosis" / "cons_failure" / "summary.json").exists():
        raise SystemExit("iter2 cons diagnosis produced no summary")

    run_or_skip(
        candidates_cmd(iter2 / "diagnosis", iter2 / "skills", workers=8),
        iter2 / "skills" / "candidates" / "generation_summary.json",
        "iter2 candidates",
    )
    run_or_skip(
        pipeline_cmd(iter2 / "skills" / "candidates", "bank2",
                     iter2, bank1_published),
        iter2 / "bank2" / "summary.json",
        "iter2 skill bank (bank2)",
    )
    run_or_skip(
        prune_cmd(iter2 / "train", train_convs),
        iter2 / "train" / "prune_summary.json",
        "iter2 prune",
    )

    # ── Validation: build once with Bank2, evaluate four variants ──
    val = root / "val_eval"
    bank2_published = iter2 / "bank2" / "skills" / "published_bank2_full"
    run_or_skip(
        train_iter_cmd("validation", val / "memory",
                       skill_bank_dir=bank2_published,
                       qa_workers=args.qa_workers,
                       smoke_qa=args.smoke_qa, max_convs=args.max_convs),
        val / "memory" / "summary.json", "val build+full",
    )
    run_or_skip(
        prune_cmd(val / "memory", val_convs),
        val / "memory" / "prune_summary.json",
        "val prune",
    )

    variants = {
        "bank0": None,
        "acc": iter2 / "bank2" / "skills" / "published_bank2_access_only",
        "cons": iter2 / "bank2" / "skills" / "published_bank2_construction_only",
    }
    for name, bank_dir in variants.items():
        out = val / name
        run_or_skip(
            train_iter_cmd("validation", out,
                           skill_bank_dir=bank_dir,
                           resume_db_from=val / "memory",
                           qa_workers=args.qa_workers,
                           smoke_qa=args.smoke_qa, max_convs=args.max_convs),
            out / "summary.json", f"val {name}",
        )
    for name in ["bank0", "acc", "cons"]:
        preds = [val / name / cid / "locomo_predictions.jsonl"
                 for cid in val_convs]
        run_or_skip(
            judge_cmd(preds, val / name / "judge"),
            val / name / "judge" / "summary.json", f"val {name} judge",
        )
    # Full already judged as part of build? No: judge full separately.
    preds_full = [val / "memory" / cid / "locomo_predictions.jsonl"
                  for cid in val_convs]
    run_or_skip(
        judge_cmd(preds_full, val / "full" / "judge"),
        val / "full" / "judge" / "summary.json", "val full judge",
    )

    print("\n=== VALIDATION VARIANT JUDGE SUMMARIES ===")
    for name in ["bank0", "full", "acc", "cons"]:
        summary = val / name / "judge" / "summary.json"
        if summary.exists():
            data = json.loads(summary.read_text(encoding="utf-8"))
            labels = data.get("labels", {})
            c, p, i = labels.get("C", 0), labels.get("P", 0), labels.get("I", 0)
            print(f"  {name:6s}: C={c} P={p} I={i}  C+P={c + p} "
                  f"(total {data.get('total', '?')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
