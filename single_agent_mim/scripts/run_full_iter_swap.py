"""Full iteration on the swap split.

Standard full iteration: train (already done, or run here) -> judge ->
three-stage diagnosis -> success examples -> candidates -> CRUD -> Bank N+1
-> validation + test build/judge. Every published version gets an evaluation
report (val + test).

Usage:
  python scripts/run_full_iter_swap.py --config configs/qwen3_8b_swap.yaml \
      --output-root outputs/v2c_full_iter --initial-bank <v2_c bank dir> \
      --run-id bank4 [--skip-train]
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
CONFIG = ROOT / "configs/qwen3_8b_swap.yaml"

sys.path.insert(0, str(ROOT / "src"))

from mim.config import load_config


def _jsonl_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip())


def run_or_skip(cmd: list[str], marker: Path | None, label: str) -> None:
    if marker is not None and marker.exists():
        print(f"[skip] {label}: {marker} exists", flush=True)
        return
    print(f"[run ] {label}", flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f"FAILED: {label} (exit {proc.returncode})")
    print(f"[done] {label}", flush=True)


def train_cmd(split: str, run_root: Path, skill_bank: Path | None, *,
              qa_workers: int, smoke_qa: int, max_convs: int, resume: bool) -> list[str]:
    cmd = [sys.executable, str(SCRIPTS / "run_train_iter.py"),
           "--config", str(CONFIG), "--split", split,
           "--run-root", str(run_root),
           "--build-workers", "6", "--qa-workers", str(qa_workers)]
    if skill_bank is not None:
        cmd += ["--skill-bank-dir", str(skill_bank)]
    if resume:
        cmd.append("--resume")
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
            "--workers", "8", *[str(p) for p in pred_files]]


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
        cmd.append("--resume")
    for cid in convs:
        cmd += ["--source-run", f"{cid}={run_root / cid}"]
    return cmd


def candidates_cmd(diagnosis_root: Path, skills_dir: Path,
                   success_examples: Path | None,
                   success_package: Path | None = None,
                   workers: int = 4) -> list[str]:
    cmd = [sys.executable, str(SCRIPTS / "run_candidates_from_diagnosis.py"),
           "--config", str(CONFIG), "--diagnosis-root", str(diagnosis_root),
           "--skills-dir", str(skills_dir), "--workers", str(workers)]
    if success_examples is not None and success_examples.exists():
        cmd += ["--success-examples", str(success_examples)]
    if success_package is not None and success_package.exists():
        cmd += ["--success-package", str(success_package)]
    return cmd


def pipeline_cmd(candidates_root: Path, run_id: str, output_root: Path,
                 initial_bank: Path | None) -> list[str]:
    cmd = [sys.executable, str(SCRIPTS / "run_skill_bank_pipeline_v2.py"),
           "--config", str(CONFIG), "--source-candidates", str(candidates_root),
           "--run-id", run_id, "--output-root", str(output_root),
           "--workers", "6"]
    if initial_bank is not None:
        cmd += ["--initial-skill-bank-dir", str(initial_bank)]
    return cmd


def build_eval_cmd(split: str, run_root: Path, bank: Path, qa_workers: int) -> list[str]:
    return train_cmd(split, run_root, bank, qa_workers=qa_workers,
                     smoke_qa=0, max_convs=0, resume=False)


def main() -> int:
    global CONFIG
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(CONFIG))
    parser.add_argument("--output-root", default="outputs/v2c_full_iter")
    parser.add_argument(
        "--initial-bank",
        help="Published seed Bank. Omit to run the full phase from an empty Bank.",
    )
    parser.add_argument("--run-id", default="bank4",
                        help="Pipeline run id / published bank name.")
    parser.add_argument("--qa-workers", type=int, default=6)
    parser.add_argument("--smoke-qa", type=int, default=0)
    parser.add_argument("--max-convs", type=int, default=0)
    parser.add_argument("--baseline-root", action="append", default=[],
                        help="Empty-bank (or earlier) run root whose "
                             "Judge-correct no-skill questions join the "
                             "default-policy success package (repeatable).")
    parser.add_argument("--skip-train", action="store_true",
                        help="Use an existing train run in <root>/train.")
    parser.add_argument("--skip-eval", action="store_true",
                        help="Publish the bank but skip val/test evaluation.")
    args = parser.parse_args()

    CONFIG = Path(args.config).resolve()

    config = load_config(CONFIG)
    split_path = Path(config.dataset.split)
    if not split_path.is_absolute():
        split_path = ROOT / split_path
    split_data = json.loads(split_path.read_text(encoding="utf-8"))

    def configured_convs(split: str) -> list[str]:
        convs = list(split_data.get(split, []))
        if not convs:
            raise SystemExit(
                f"No conversations configured for split {split!r} in {split_path}"
            )
        return convs[: args.max_convs] if args.max_convs else convs

    root = Path(args.output_root)
    initial_bank = Path(args.initial_bank) if args.initial_bank else None
    train_convs = configured_convs("train")
    val_convs = configured_convs("validation")
    test_convs = configured_convs("test")

    # ── 1. Train with the current bank (or reuse) ─────────────
    if not args.skip_train:
        run_or_skip(
            train_cmd("train", root / "train", initial_bank,
                      qa_workers=args.qa_workers, smoke_qa=args.smoke_qa,
                      max_convs=args.max_convs, resume=False),
            root / "train" / "summary.json", "train",
        )

    # ── 2. Judge train ────────────────────────────────────────
    pred_files = [root / "train" / cid / "locomo_predictions.jsonl"
                  for cid in train_convs]
    run_or_skip(judge_cmd(pred_files, root / "judge"),
                root / "judge" / "summary.json", "judge (train)")

    # ── 3. Diagnosis V3 (answer then access+cons concurrent) ──
    run_or_skip(
        diagnosis_cmd("answer", root / "judge", root / "train",
                      root / "diagnosis", train_convs, args.run_id),
        root / "diagnosis" / "answer_failure" / "summary.json",
        "diagnosis: answer",
    )
    print("[run ] diagnosis: access + cons (concurrent)", flush=True)
    p_a = subprocess.Popen(
        diagnosis_cmd("access", root / "judge", root / "train",
                      root / "diagnosis", train_convs, args.run_id),
        cwd=str(ROOT))
    p_c = subprocess.Popen(
        diagnosis_cmd("cons", root / "judge", root / "train",
                      root / "diagnosis", train_convs, args.run_id),
        cwd=str(ROOT))
    if p_a.wait() != 0 or p_c.wait() != 0:
        raise SystemExit("FAILED: diagnosis access/cons")
    if not (root / "diagnosis" / "cons_failure" / "summary.json").exists():
        raise SystemExit("cons diagnosis produced no summary")

    # ── 4. Success examples (evidence_ids compatible) ─────────
    # Skill-use trajectories require that Skills were actually selected.
    # An empty-bank start has none by definition; that is fine because the
    # default-policy success package (4b) still provides calibration.
    success = root / "success_examples.jsonl"
    if not success.exists():
        print("[run ] success examples", flush=True)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "build_successful_skill_traces.py"),
             "--runtime-root", str(root / "train"),
             "--judgments", str(root / "judge" / "judgments.jsonl"),
             "--output", str(success)],
            cwd=str(ROOT),
        )
        if proc.returncode != 0:
            if success.exists() and _jsonl_count(success) == 0:
                print("[warn] no skill-use success trajectories (expected for "
                      "an empty-bank start); continuing without them",
                      flush=True)
                success.unlink(missing_ok=True)
            else:
                raise SystemExit(
                    f"FAILED: success examples (exit {proc.returncode})"
                )
        else:
            print("[done] success examples", flush=True)
    else:
        print(f"[skip] success examples: {success} exists", flush=True)

    # ── 4b. Default-policy success package (no-skill successes) ─
    # Judge-correct questions answered with NO Skill: evidence that the
    # default policy suffices for that pattern, so new Skills must not
    # change behaviour there. Sources: this train run's no-skill C rows
    # plus any baseline (empty-bank) runs supplied via --baseline-root.
    success_package = root / "success_package.jsonl"
    package_cmd = [
        sys.executable, str(SCRIPTS / "build_success_package.py"),
        "--config", str(CONFIG),
        "--runtime-root", str(root / "train"),
        "--judgments", str(root / "judge" / "judgments.jsonl"),
        "--output", str(success_package),
    ]
    for baseline in (args.baseline_root or []):
        baseline_dir = Path(baseline)
        package_cmd += ["--runtime-root", str(baseline_dir)]
        judge_files = sorted(baseline_dir.glob("*/judge_binary/judgments.jsonl"))
        if not judge_files:
            judge_files = sorted(baseline_dir.glob("*/judge*/judgments.jsonl"))
        for judge_file in judge_files:
            package_cmd += ["--judgments", str(judge_file)]
    run_or_skip(
        package_cmd,
        success_package, "default-policy success package",
    )

    # ── 5. Candidates ─────────────────────────────────────────
    run_or_skip(
        candidates_cmd(root / "diagnosis", root / "skills", success,
                       success_package),
        root / "skills" / "candidates" / "generation_summary.json",
        "candidates",
    )

    # ── 6. CRUD -> Bank N+1 ───────────────────────────────────
    run_or_skip(
        pipeline_cmd(root / "skills" / "candidates", args.run_id,
                     root, initial_bank),
        root / args.run_id / "summary.json", f"skill bank ({args.run_id})",
    )
    new_bank = root / args.run_id / "skills" / f"published_{args.run_id}_full"
    if not new_bank.exists():
        # Pipeline may name the dir differently; discover it.
        candidates = sorted(
            (root / args.run_id / "skills").glob("published_*_full")
        )
        if not candidates:
            raise SystemExit(f"Published bank not found under {root / args.run_id / 'skills'}")
        new_bank = candidates[-1]

    if args.skip_eval:
        print(f"\n=== {args.run_id} published: {new_bank} (eval skipped) ===")
        return 0

    # ── 7. Validation + test evaluation ───────────────────────
    for split, convs, label in (("validation", val_convs, "val"),
                                ("test", test_convs, "test")):
        run_or_skip(
            build_eval_cmd(split, root / label, new_bank, args.qa_workers),
            root / label / "summary.json", f"{label} build",
        )
        preds = [root / label / cid / "locomo_predictions.jsonl"
                 for cid in convs]
        run_or_skip(judge_cmd(preds, root / label / "judge"),
                    root / label / "judge" / "summary.json", f"{label} judge")

    # ── 8. Report ─────────────────────────────────────────────
    print(f"\n=== {args.run_id} EVAL REPORT ===")
    for label in ("val", "test"):
        summary = root / label / "judge" / "summary.json"
        if summary.exists():
            data = json.loads(summary.read_text(encoding="utf-8"))
            labels = data.get("labels", {})
            c, w = labels.get("C", 0), labels.get("W", 0)
            print(f"  {label}: C={c} W={w}  C-rate={c / data.get('total', 1):.1%} "
                  f"(total {data.get('total', '?')})")
            for cid, stats in data.get("by_conversation", {}).items():
                cc, ww = stats.get("C", 0), stats.get("W", 0)
                print(f"    {cid}: {cc}/{cc + ww} = {cc / (cc + ww):.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
