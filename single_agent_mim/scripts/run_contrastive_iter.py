"""Run the full contrastive half of the two-segment MiM training loop.

Input is a completed empty-Bank full iteration (Bank1).  This script reruns
the full train split with Bank1, builds C2W/W2C pairs, diagnoses both wrong
sides with the standard pipeline, emits one contrastive core plus routed
Access/Answer/Construction projections, updates Bank1 to Bank2, and evaluates
Bank2 on the configured validation and test splits.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT / "src"))

from mim.config import load_config


def run(cmd: list[str], marker: Path | None, label: str) -> None:
    if marker is not None and marker.exists():
        print(f"[skip] {label}: {marker} exists", flush=True)
        return
    print(f"[run ] {label}", flush=True)
    completed = subprocess.run(cmd, cwd=str(ROOT))
    if completed.returncode:
        raise SystemExit(f"FAILED: {label} (exit {completed.returncode})")
    print(f"[done] {label}", flush=True)


def published_bank(run_root: Path, run_id: str) -> Path:
    exact = run_root / run_id / "skills" / f"published_{run_id}_full"
    if exact.exists():
        return exact
    found = sorted((run_root / run_id / "skills").glob("published_*_full"))
    if not found:
        raise SystemExit(f"Published {run_id} not found under {run_root}")
    return found[-1]


def train_cmd(
    config: Path, split: str, run_root: Path, bank: Path, qa_workers: int
) -> list[str]:
    return [
        sys.executable, str(SCRIPTS / "run_train_iter.py"),
        "--config", str(config), "--split", split,
        "--run-root", str(run_root), "--skill-bank-dir", str(bank),
        "--build-workers", "6", "--qa-workers", str(qa_workers),
    ]


def judge_cmd(config: Path, run_root: Path, convs: list[str], out: Path) -> list[str]:
    return [
        sys.executable, str(SCRIPTS / "judge_binary.py"),
        "--config", str(config), "--output-dir", str(out), "--workers", "8",
        *[
            str(run_root / cid / "locomo_predictions.jsonl")
            for cid in convs
        ],
    ]


def diagnosis_cmd(
    component: str,
    config: Path,
    judge_dir: Path,
    run_root: Path,
    diagnosis_root: Path,
    convs: list[str],
) -> list[str]:
    cmd = [
        sys.executable, str(SCRIPTS / f"run_{component}_failure.py"),
        "--config", str(config),
        "--judge-results", str(judge_dir / "judgments.jsonl"),
        "--diagnosis-run-id", "bank1_contrastive_prep",
        "--output-root", str(diagnosis_root), "--workers", "4",
    ]
    if (diagnosis_root / f"{component}_failure").exists():
        cmd.append("--resume")
    for cid in convs:
        cmd += ["--source-run", f"{cid}={run_root / cid}"]
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/qwen3_8b_swap.yaml")
    parser.add_argument("--phase1-root", required=True)
    parser.add_argument("--output-root")
    parser.add_argument("--qa-workers", type=int, default=6)
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    split_path = Path(config.dataset.split)
    if not split_path.is_absolute():
        split_path = ROOT / split_path
    splits = json.loads(split_path.read_text(encoding="utf-8"))
    train_convs = list(splits["train"])
    val_convs = list(splits["validation"])
    test_convs = list(splits["test"])

    phase1 = Path(args.phase1_root).resolve()
    out = (
        Path(args.output_root).resolve()
        if args.output_root
        else phase1 / "contrastive"
    )
    bank1 = published_bank(phase1, "bank1")
    empty_judgments = phase1 / "judge" / "judgments.jsonl"
    empty_run = phase1 / "train"
    empty_diagnosis = phase1 / "diagnosis"
    for required in (empty_judgments, empty_run, empty_diagnosis, bank1):
        if not required.exists():
            raise SystemExit(f"Required phase-1 artifact is missing: {required}")

    bank1_train = out / "bank1_train"
    bank1_judge = out / "bank1_judge"
    run(
        train_cmd(config_path, "train", bank1_train, bank1, args.qa_workers),
        bank1_train / "summary.json", "Bank1 full train",
    )
    run(
        judge_cmd(config_path, bank1_train, train_convs, bank1_judge),
        bank1_judge / "summary.json", "Bank1 train judge",
    )

    # Prepare standard diagnoses for the Bank1 wrong side.  Empty-Bank wrong
    # cases reuse the phase-1 standard diagnoses.  Answer must finish first;
    # Access and Construction remain isolated and run concurrently.
    standard = out / "bank1_standard_diagnosis"
    run(
        diagnosis_cmd(
            "answer", config_path, bank1_judge, bank1_train, standard, train_convs
        ),
        standard / "answer_failure" / "summary.json",
        "Bank1 standard diagnosis: answer",
    )
    pending: list[tuple[str, subprocess.Popen]] = []
    for component in ("access", "cons"):
        marker = standard / f"{component}_failure" / "summary.json"
        if marker.exists():
            print(f"[skip] Bank1 standard diagnosis: {component}", flush=True)
            continue
        print(f"[run ] Bank1 standard diagnosis: {component}", flush=True)
        pending.append(
            (
                component,
                subprocess.Popen(
                    diagnosis_cmd(
                        component, config_path, bank1_judge, bank1_train,
                        standard, train_convs,
                    ),
                    cwd=str(ROOT),
                ),
            )
        )
    for component, process in pending:
        if process.wait():
            raise SystemExit(f"FAILED: Bank1 standard diagnosis: {component}")
        print(f"[done] Bank1 standard diagnosis: {component}", flush=True)

    pairs = out / "contrastive_pairs.json"
    run(
        [
            sys.executable, str(SCRIPTS / "build_contrastive_pairs.py"),
            "--from-judgments", str(empty_judgments),
            "--to-judgments", str(bank1_judge / "judgments.jsonl"),
            "--chain-id", "empty_to_bank1",
            "--from-bank", "bank_empty", "--to-bank", "bank1",
            "--from-run", "empty_train", "--to-run", "bank1_train",
            "--output", str(pairs),
        ],
        pairs, "build C2W/W2C pairs",
    )

    packages = out / "contrastive_packages"
    run(
        [
            sys.executable, str(SCRIPTS / "run_flip_diagnosis.py"),
            "--config", str(config_path), "--flips", str(pairs),
            "--output-root", str(packages), "--workers", "8",
            "--run-dir", f"empty_train={empty_run}",
            "--run-dir", f"bank1_train={bank1_train}",
            "--diagnosis-dir", f"empty_train={empty_diagnosis}",
            "--diagnosis-dir", f"bank1_train={standard}",
        ],
        packages / "flip_diagnosis_summary.json", "contrastive diagnosis",
    )

    skills = out / "skills"
    run(
        [
            sys.executable, str(SCRIPTS / "run_candidates_from_diagnosis.py"),
            "--config", str(config_path), "--diagnosis-root", str(packages),
            "--skills-dir", str(skills), "--workers", "8",
        ],
        skills / "candidates" / "generation_summary.json",
        "contrastive candidates (Access + Construction generators)",
    )
    run(
        [
            sys.executable, str(SCRIPTS / "run_skill_bank_pipeline_v2.py"),
            "--config", str(config_path),
            "--source-candidates", str(skills / "candidates"),
            "--run-id", "bank2", "--output-root", str(out),
            "--initial-skill-bank-dir", str(bank1), "--workers", "6",
        ],
        out / "bank2" / "summary.json", "publish Bank2",
    )
    bank2 = published_bank(out, "bank2")

    evaluation: dict[str, dict] = {}
    for split, convs, label in (
        ("validation", val_convs, "val"), ("test", test_convs, "test")
    ):
        eval_root = out / label
        run(
            train_cmd(config_path, split, eval_root, bank2, args.qa_workers),
            eval_root / "summary.json", f"Bank2 {label} build",
        )
        run(
            judge_cmd(config_path, eval_root, convs, eval_root / "judge"),
            eval_root / "judge" / "summary.json", f"Bank2 {label} judge",
        )
        evaluation[label] = json.loads(
            (eval_root / "judge" / "summary.json").read_text(encoding="utf-8")
        )

    summary = {
        "schema_version": "two_segment_contrastive_iter_v1",
        "phase1_root": str(phase1),
        "bank1": str(bank1),
        "bank2": str(bank2),
        "pairs": json.loads(pairs.read_text(encoding="utf-8")),
        "flip_diagnosis": json.loads(
            (packages / "flip_diagnosis_summary.json").read_text(encoding="utf-8")
        ),
        "evaluation": evaluation,
    }
    (out / "contrastive_iteration_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\n=== Bank2 published and evaluated: {bank2} ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
