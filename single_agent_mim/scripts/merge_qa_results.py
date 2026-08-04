"""Merge resumable/sharded QA JSONL artifacts into one validated result."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--split-name", default="validation")
    parser.add_argument("--bank", default="bank1")
    parser.add_argument("--output-file", default="all.jsonl")
    parser.add_argument("inputs", nargs="+")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    rows: dict[str, dict] = {}
    for raw_path in args.inputs:
        path = Path(raw_path)
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            qa_id = str(row.get("qa_id", ""))
            if not qa_id:
                continue
            previous = rows.get(qa_id)
            if previous is None or (previous.get("error") and not row.get("error")):
                rows[qa_id] = row

    ordered = sorted(
        rows.values(),
        key=lambda row: (str(row.get("conversation_id", "")), str(row["qa_id"])),
    )
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    result_path = output / args.output_file
    summary_path = output / "summary.json"
    if result_path.exists() or summary_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite {result_path} or {summary_path}"
        )
    with result_path.open("w", encoding="utf-8") as handle:
        for row in ordered:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    category_scores: dict[int, list[float]] = defaultdict(list)
    conversation_scores: dict[str, list[float]] = defaultdict(list)
    for row in ordered:
        score = float(row.get("f1", 0.0))
        category_scores[int(row["category"])].append(score)
        conversation_scores[str(row["conversation_id"])].append(score)
    summary = {
        "model": "qwen3-8b",
        "mode": "base" if args.bank == "bank0" else "mim",
        "bank": args.bank,
        "thinking": False,
        "split_name": args.split_name,
        "total_qa": len(ordered),
        "unique_qa_ids": len(rows),
        "conversation_count": len(conversation_scores),
        "overall_f1": (
            sum(float(row.get("f1", 0.0)) for row in ordered) / len(ordered)
            if ordered else 0.0
        ),
        "overall_f1_percent": (
            100 * sum(float(row.get("f1", 0.0)) for row in ordered) / len(ordered)
            if ordered else 0.0
        ),
        "protocol_errors": sum(bool(row.get("error")) for row in ordered),
        "conversation_f1": {
            conversation_id: sum(scores) / len(scores)
            for conversation_id, scores in sorted(conversation_scores.items())
        },
        "conversation_qa_count": {
            conversation_id: len(scores)
            for conversation_id, scores in sorted(conversation_scores.items())
        },
        "category_f1": {
            str(category): sum(scores) / len(scores)
            for category, scores in sorted(category_scores.items())
        },
        "category_count": {
            str(category): len(scores)
            for category, scores in sorted(category_scores.items())
        },
        "skill_usage": Counter(
            skill_id
            for row in ordered
            for skill_id in (row.get("skill_ids") or [])
        ),
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["protocol_errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
