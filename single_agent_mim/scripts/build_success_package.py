"""Build a default-policy success package: Judge-correct questions answered
with NO Skill selected.

Sources (any combination):
  * baseline runs (empty Skill Bank, e.g. swap_val_base / swap_test_base):
    every Judge-correct question is a default-policy success.
  * skill runs: Judge-correct questions whose qa_results show an empty
    skill_ids list — the model answered correctly without following any
    Skill.

The package is consumed by ``run_candidates_from_diagnosis.py
--success-package`` to calibrate candidate generation: a matching
default-policy success is evidence that the default behaviour must be
preserved, so proposed Skills must be conditioned on default failure.

Usage:
  python scripts/build_success_package.py \
      --runtime-root outputs/swap_val_base outputs/swap_test_base \
      --judgments outputs/swap_val_base/judge_binary/judgments.jsonl \
      --output outputs/success_package.jsonl [--min-similarity 0.15]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.config import load_config
from mim.eval.locomo import load_dataset


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    parser.add_argument("--runtime-root", action="append", required=True,
                        help="Runtime run directory (repeatable). Each must "
                             "contain conv-*/qa_results.jsonl.")
    parser.add_argument("--judgments", action="append", required=True,
                        help="Binary judge judgments.jsonl (repeatable).")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    config = load_config(args.config)
    _, questions_map = load_dataset(config.dataset.path)

    judged = set()
    for path in args.judgments:
        for row in _load_jsonl(Path(path)):
            if str(row.get("label", "")).upper() == "C":
                judged.add(str(row.get("qa_id", "")))

    question_by_id = {}
    for questions in questions_map.values():
        for question in questions:
            question_by_id[question.qa_id] = question

    examples = []
    seen = set()
    for run_root in args.runtime_root:
        root = Path(run_root)
        for qa_file in sorted(root.glob("*/qa_results.jsonl")):
            for row in _load_jsonl(qa_file):
                qa_id = str(row.get("qa_id", ""))
                if qa_id not in judged or qa_id in seen:
                    continue
                if row.get("error"):
                    continue
                skill_ids = row.get("skill_ids") or []
                if skill_ids:
                    continue  # skill was selected: not a default-policy run
                question = question_by_id.get(qa_id)
                seen.add(qa_id)
                examples.append(
                    {
                        "qa_id": qa_id,
                        "conversation_id": row.get("conversation_id", ""),
                        "category": row.get("category", ""),
                        "question": row.get("question", ""),
                        "reference_answer": row.get("reference", ""),
                        "prediction": row.get("prediction", ""),
                        "skill_ids": [],
                        "judge_label": "C",
                        "source": str(root),
                    }
                )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        for example in examples:
            handle.write(json.dumps(example, ensure_ascii=False) + "\n")
    print(
        f"Default-policy success package: {len(examples)} examples -> {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
