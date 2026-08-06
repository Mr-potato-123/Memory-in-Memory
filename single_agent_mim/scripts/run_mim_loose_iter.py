"""Single-round V2 experiment.

One training round over the existing Bank1 (23A+26C):

  train (6-way parallel build + parallel QA with Bank1)
    -> LLM Judge
    -> V3 Diagnosis (answer -> access+cons, with skill traces)
    -> candidates (drafts) from repair packages
    -> usage stats: Skills never selected during train are dropped;
       the remaining Skills join the drafts in one V2 draft-first CRUD round
    -> V2 pipeline (seed = filtered Bank1) -> Bank2
    -> prune training memory
    -> validation: build once with Bank2, three variants
       (full / acc-only / cons-only) over the same memory + Judge

Usage:
  python scripts/run_mim_v2_single.py --config configs/qwen3_8b_dashscope.yaml \
      --output-root outputs/v2_iter [--smoke-qa 0] [--max-convs 0]
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
CONFIG = ROOT / "configs/qwen3_8b_dashscope.yaml"

TRAIN_CONVS = ["conv-30", "conv-42", "conv-43", "conv-44", "conv-48", "conv-49"]
VAL_CONVS = ["conv-26", "conv-41"]
# The existing published Bank1 used as the starting skill set.
BANK1_PUBLISHED = (
    Path(r"D:/Documents/Project/Memory_in_Memory/exp/single-agent/bank_v1/banks")
)


def run_or_skip(cmd: list[str], marker: Path | None, label: str) -> None:
    if marker is not None and marker.exists():
        print(f"[skip] {label}: {marker} exists", flush=True)
        return
    print(f"[run ] {label}", flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f"FAILED: {label} (exit {proc.returncode})")
    print(f"[done] {label}", flush=True)


def train_cmd(split: str, run_root: Path, *,
              skill_bank: Path | None = None,
              resume_db_from: Path | None = None,
              qa_workers: int, smoke_qa: int, max_convs: int) -> list[str]:
    cmd = [sys.executable, str(SCRIPTS / "run_train_iter.py"),
           "--config", str(CONFIG), "--split", split,
           "--run-root", str(run_root), "--qa-workers", str(qa_workers)]
    if skill_bank is not None:
        cmd += ["--skill-bank-dir", str(skill_bank)]
    if resume_db_from is not None:
        cmd += ["--resume-db-from", str(resume_db_from)]
    if smoke_qa:
        cmd += ["--smoke-qa", str(smoke_qa)]
    if max_convs:
        cmd += ["--max-convs", str(max_convs)]
    return cmd


def judge_cmd(pred_files: list[Path], output_dir: Path) -> list[str]:
    if output_dir.exists() and not (output_dir / "summary.json").exists():
        shutil.rmtree(output_dir, ignore_errors=True)
    return [sys.executable, str(SCRIPTS / "judge_binary.py"),
            "--config", str(CONFIG), "--output-dir", str(output_dir),
            "--workers", "12", *[str(p) for p in pred_files]]


def diagnosis_cmd(component: str, judge_dir: Path, run_root: Path,
                  output_root: Path, convs: list[str], run_id: str) -> list[str]:
    cmd = [sys.executable, str(SCRIPTS / f"run_{component}_failure.py"),
           "--config", str(CONFIG),
           "--judge-results", str(judge_dir / "judgments.jsonl"),
           "--diagnosis-run-id", run_id,
           "--output-root", str(output_root), "--workers", "4"]
    component_dir = {"answer": "answer_failure", "access": "access_failure",
                     "cons": "cons_failure"}[component]
    if (output_root / component_dir).exists():
        cmd += ["--resume"]
    for cid in convs:
        cmd += ["--source-run", f"{cid}={run_root / cid}"]
    return cmd


def candidates_cmd(diagnosis_root: Path, skills_dir: Path, workers: int) -> list[str]:
    return [sys.executable, str(SCRIPTS / "run_candidates_from_diagnosis.py"),
            "--config", str(CONFIG), "--diagnosis-root", str(diagnosis_root),
            "--skills-dir", str(skills_dir), "--workers", str(workers)]


def pipeline_cmd(candidates_root: Path, run_id: str, output_root: Path,
                 initial_bank: Path) -> list[str]:
    cmd = [sys.executable, str(SCRIPTS / "run_skill_bank_pipeline_v2.py"),
           "--config", str(CONFIG), "--source-candidates", str(candidates_root),
           "--run-id", run_id, "--output-root", str(output_root),
           "--workers", "6", "--initial-skill-bank-dir", str(initial_bank)]
    if (output_root / run_id).exists():
        cmd += ["--resume"]
    return cmd


def prune_cmd(run_root: Path, convs: list[str]) -> list[str]:
    cmd = [sys.executable, str(SCRIPTS / "prune_memory.py"),
           "--run-root", str(run_root)]
    for cid in convs:
        cmd += ["--conversation-id", cid]
    return cmd


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _base_id(sid: str) -> str:
    """Strip the trailing _v{version} from a runtime skill id."""
    return sid.split("_v")[0] if "_v" in sid else sid


def analyse_used_skills(train_root: Path) -> tuple[set[str], set[str]]:
    """Return (used_access, used_construction) skill-id sets over training.

    Access usage comes from ``qa_results.jsonl`` (skill_ids actually selected
    per question). Construction usage comes from construction traces
    (skill_ids selected per session during ingest).
    """
    used_access: set[str] = set()
    for qa_file in sorted(train_root.glob("*/qa_results.jsonl")):
        for qa in _load_jsonl(qa_file):
            used_access.update(_base_id(s) for s in qa.get("skill_ids") or [])

    used_cons: set[str] = set()
    for trace_file in sorted(train_root.glob("*/traces/construction_traces.jsonl")):
        for row in _load_jsonl(trace_file):
            used_cons.update(_base_id(s) for s in row.get("skill_ids") or [])
    return used_access, used_cons


def filter_bank(published_dir: Path, used_access: set[str],
                used_cons: set[str], out_dir: Path) -> dict:
    """Export a published Bank containing only used Skills.

    Unused Skills are dropped before the CRUD round so the drafts merge
    exclusively against Skills that were actually exercised during training.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    kept = {}
    for side, used in (("access", used_access), ("construction", used_cons)):
        for path in published_dir.glob(f"{side}_skill_bank_v*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            skills = data["skills"]
            remaining = [s for s in skills
                         if s["skill_id"] in used
                         and s.get("status", "active") == "active"]
            data["skills"] = remaining
            data["source"] = str(path)
            destination = out_dir / path.name
            destination.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            kept[side] = {
                "total": len(skills), "kept": len(remaining),
                "dropped": len(skills) - len(remaining),
                "dropped_ids": [s["skill_id"] for s in skills
                                if s["skill_id"] not in used],
            }
    report = {"access": kept.get("access"), "construction": kept.get("construction")}
    print("\nSkill usage filter (unused skills dropped before CRUD):", flush=True)
    for side, stats in report.items():
        print(f"  {side:13s}: kept={stats['kept']}/{stats['total']} "
              f"dropped={stats['dropped']}", flush=True)
        if stats["dropped_ids"]:
            print(f"     dropped: {stats['dropped_ids']}", flush=True)
    (out_dir / "filter_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(CONFIG))
    parser.add_argument("--output-root", default="outputs/v2_iter")
    parser.add_argument("--initial-bank", default=str(BANK1_PUBLISHED),
                        help="Starting published Skill Bank for this iteration.")
    parser.add_argument("--qa-workers", type=int, default=6)
    parser.add_argument("--smoke-qa", type=int, default=0)
    parser.add_argument("--max-convs", type=int, default=0)
    args = parser.parse_args()

    root = Path(args.output_root)
    initial_bank = Path(args.initial_bank)
    train_convs = TRAIN_CONVS[: args.max_convs] if args.max_convs else TRAIN_CONVS
    val_convs = VAL_CONVS[: args.max_convs] if args.max_convs else VAL_CONVS

    # ── 1. Train with the starting Bank ─────────────────────────
    run_or_skip(
        train_cmd("train", root / "train", skill_bank=initial_bank,
                  qa_workers=args.qa_workers, smoke_qa=args.smoke_qa,
                  max_convs=args.max_convs),
        root / "train" / "summary.json", "train (initial bank)",
    )
    # ── 2. Judge ───────────────────────────────────────────────
    pred_files = [root / "train" / cid / "locomo_predictions.jsonl"
                  for cid in train_convs]
    run_or_skip(judge_cmd(pred_files, root / "judge"),
                root / "judge" / "summary.json", "judge (train)")
    # ── 3. Diagnosis V3 ────────────────────────────────────────
    run_or_skip(
        diagnosis_cmd("answer", root / "judge", root / "train",
                      root / "diagnosis", train_convs, "v2_single_diag"),
        root / "diagnosis" / "answer_failure" / "summary.json",
        "diagnosis: answer",
    )
    print("[run ] diagnosis: access + cons (concurrent)", flush=True)
    p_a = subprocess.Popen(
        diagnosis_cmd("access", root / "judge", root / "train",
                      root / "diagnosis", train_convs, "v2_single_diag"),
        cwd=str(ROOT))
    p_c = subprocess.Popen(
        diagnosis_cmd("cons", root / "judge", root / "train",
                      root / "diagnosis", train_convs, "v2_single_diag"),
        cwd=str(ROOT))
    if p_a.wait() != 0 or p_c.wait() != 0:
        raise SystemExit("FAILED: diagnosis access/cons")
    if not (root / "diagnosis" / "cons_failure" / "summary.json").exists():
        raise SystemExit("cons diagnosis produced no summary")

    # ── 4. Candidates (drafts) ─────────────────────────────────
    run_or_skip(
        candidates_cmd(root / "diagnosis", root / "skills", workers=8),
        root / "skills" / "candidates" / "generation_summary.json",
        "candidates",
    )
    # ── 5. Drop unused Skills; keep used ones for CRUD ─────────
    used_access, used_cons = analyse_used_skills(root / "train")
    filtered_bank = root / "filtered_bank"
    marker = filtered_bank / "filter_report.json"
    if not marker.exists():
        print(f"[run ] usage stats: access={len(used_access)} "
              f"construction={len(used_cons)}", flush=True)
        filter_bank(initial_bank, used_access, used_cons, filtered_bank)
    else:
        report = json.loads(marker.read_text(encoding="utf-8"))
        print(f"[skip] skill filter already done: "
              f"access kept={report['access']['kept']}, "
              f"construction kept={report['construction']['kept']}", flush=True)

    # ── 6. V2 pipeline: drafts CRUD against filtered bank ──────
    run_or_skip(
        pipeline_cmd(root / "skills" / "candidates", "bank2",
                     root, filtered_bank),
        root / "bank2" / "summary.json", "skill bank (Bank2)",
    )

    # ── 7. Prune training memory ───────────────────────────────
    run_or_skip(prune_cmd(root / "train", train_convs),
                root / "train" / "prune_summary.json", "prune train memory")

    # ── 8. Validation: build once, FULL only ───────────────────
    val = root / "val_eval"
    bank2_published = root / "bank2" / "skills" / "published_bank2_full"
    if not bank2_published.exists():
        raise SystemExit(f"Bank2 full missing: {bank2_published}")
    run_or_skip(
        train_cmd("validation", val / "memory", skill_bank=bank2_published,
                  qa_workers=args.qa_workers, smoke_qa=args.smoke_qa,
                  max_convs=args.max_convs),
        val / "memory" / "summary.json", "val build + full",
    )
    run_or_skip(prune_cmd(val / "memory", val_convs),
                val / "memory" / "prune_summary.json", "val prune")
    preds_full = [val / "memory" / cid / "locomo_predictions.jsonl"
                  for cid in val_convs]
    run_or_skip(judge_cmd(preds_full, val / "full" / "judge"),
                val / "full" / "judge" / "summary.json", "val full judge")

    print("\n=== VALIDATION FULL JUDGE SUMMARY ===")
    summary = val / "full" / "judge" / "summary.json"
    if summary.exists():
        data = json.loads(summary.read_text(encoding="utf-8"))
        labels = data.get("labels", {})
        c, w = (labels.get("C", 0), labels.get("W", 0))
        print(f"  full: C={c} W={w}  C-rate={c / data.get('total', 1):.1%} "
              f"(total {data.get('total', '?')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
