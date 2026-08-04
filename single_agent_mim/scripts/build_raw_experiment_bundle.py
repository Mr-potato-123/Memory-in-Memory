"""Build the compact final-data bundle for a two-conversation experiment."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any


CATEGORY_NAMES = {
    1: "multi_hop",
    2: "temporal",
    3: "open_domain",
    4: "single_hop",
    5: "adversarial",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--judge", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--judge-model", default="qwen-plus")
    parser.add_argument("predictions", nargs="+")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def summarize(
    rows: list[dict[str, Any]],
    judgments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    def block(items: list[dict[str, Any]]) -> dict[str, Any]:
        labels = Counter(judgments[str(row["qa_id"])]["label"] for row in items)
        return {
            "count": len(items),
            "token_f1": sum(float(row["f1"]) for row in items) / len(items),
            "llm_judge_strict": (
                sum(
                    float(judgments[str(row["qa_id"])]["strict_score"])
                    for row in items
                )
                / len(items)
            ),
            "llm_judge_partial_aware": (
                sum(
                    float(
                        judgments[str(row["qa_id"])]["partial_aware_score"]
                    )
                    for row in items
                )
                / len(items)
            ),
            "judge_labels": {
                "correct": labels["C"],
                "partial": labels["P"],
                "incorrect": labels["I"],
            },
            "protocol_errors": sum(bool(row.get("error")) for row in items),
            "runtime_tokens": sum(int(row.get("runtime_tokens", 0)) for row in items),
            "average_access_steps": (
                sum(int(row.get("access_steps", 0)) for row in items)
                / len(items)
            ),
        }

    result = block(rows)
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[int(row["category"])].append(row)
    result["categories"] = {
        CATEGORY_NAMES[category]: block(items)
        for category, items in sorted(groups.items())
    }
    return result


def percent(value: float) -> str:
    return f"{100 * value:.2f}%"


def report(metrics: dict[str, Any]) -> str:
    combined = metrics["combined"]
    lines = [
        "# Qwen3-8B Non-Thinking: Two-Conversation LoCoMo Experiment",
        "",
        "## Overall Results",
        "",
        "| Scope | Questions | Token-F1 | LLM Judge | Partial-aware |",
        "|---|---:|---:|---:|---:|",
    ]
    for key in ["conv-47", "conv-50", "combined"]:
        item = metrics[key]
        name = "Combined" if key == "combined" else key
        lines.append(
            f"| {name} | {item['count']} | {percent(item['token_f1'])} | "
            f"{percent(item['llm_judge_strict'])} | "
            f"{percent(item['llm_judge_partial_aware'])} |"
        )
    lines.extend(
        [
            "",
            "## Combined Results by Category",
            "",
            "| Category | Questions | Token-F1 | LLM Judge | Partial-aware |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    display = {
        "multi_hop": "Multi-hop",
        "temporal": "Temporal",
        "open_domain": "Open-domain",
        "single_hop": "Single-hop",
        "adversarial": "Adversarial",
    }
    for key in [
        "multi_hop",
        "temporal",
        "open_domain",
        "single_hop",
        "adversarial",
    ]:
        item = combined["categories"][key]
        lines.append(
            f"| {display[key]} | {item['count']} | "
            f"{percent(item['token_f1'])} | "
            f"{percent(item['llm_judge_strict'])} | "
            f"{percent(item['llm_judge_partial_aware'])} |"
        )
    counts = combined["judge_labels"]
    lines.extend(
        [
            "",
            "## Judge Definition",
            "",
            "- Judge model: `qwen-plus`, temperature 0.",
            "- `C`: fully correct; `P`: partially correct; `I`: incorrect.",
            "- Strict LLM Judge = `C / Total`.",
            "- Partial-aware = `(C + 0.5 × P) / Total`.",
            f"- Combined: C={counts['correct']}, P={counts['partial']}, "
            f"I={counts['incorrect']}.",
            "",
            "The two conversations use isolated memory, SQLite, retrieval, and "
            "prediction runs. Metrics are combined only during evaluation.",
            "",
            "## Files",
            "",
            "- `conv-47.jsonl`, `conv-50.jsonl`: per-question judged data.",
            "- `all.jsonl`: all 394 questions.",
            "- `llm_judge.jsonl`: raw judge labels, scores, and reasons.",
            "- `metrics.json`: machine-readable metrics under both policies.",
            "- `access_prompt.md`: frozen Access prompt.",
            "- `manifest.json`: models, prompt hashes, and source runs.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = arguments()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    prompt_path = Path(args.prompt)
    prompt_text = prompt_path.read_text(encoding="utf-8")
    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    shutil.copy2(prompt_path, output / "access_prompt.md")

    judge_path = Path(args.judge)
    judge_rows = read_jsonl(judge_path)
    judgments = {str(row["qa_id"]): row for row in judge_rows}
    judge_destination = output / "llm_judge.jsonl"
    if judge_path.resolve() != judge_destination.resolve():
        shutil.copy2(judge_path, judge_destination)

    by_conversation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_rows: list[dict[str, Any]] = []
    source_files: list[str] = []
    for raw_path in args.predictions:
        path = Path(raw_path)
        source_files.append(str(path.resolve()))
        for row in read_jsonl(path):
            qa_id = str(row["qa_id"])
            if qa_id not in judgments:
                raise ValueError(f"Missing judge result for {qa_id}")
            enriched = {
                **row,
                "llm_judge": judgments[qa_id],
            }
            conversation_id = str(row["conversation_id"])
            by_conversation[conversation_id].append(enriched)
            all_rows.append(enriched)

    if len(all_rows) != len(judge_rows):
        raise ValueError(
            f"Prediction/judge count mismatch: {len(all_rows)} != "
            f"{len(judge_rows)}"
        )
    for conversation_id, rows in sorted(by_conversation.items()):
        write_jsonl(output / f"{conversation_id}.jsonl", rows)
    write_jsonl(output / "all.jsonl", all_rows)

    metrics = {
        conversation_id: summarize(rows, judgments)
        for conversation_id, rows in sorted(by_conversation.items())
    }
    metrics["combined"] = summarize(all_rows, judgments)
    write_json(output / "metrics.json", metrics)
    (output / "report.md").write_text(report(metrics), encoding="utf-8")
    write_json(
        output / "manifest.json",
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "experiment": "qwen3_8b_f1_balanced_two_conversation",
            "runtime_model": "qwen3-8b",
            "runtime_thinking": False,
            "judge_model": args.judge_model,
            "judge_temperature": 0.0,
            "conversation_ids": sorted(by_conversation),
            "total_qa": len(all_rows),
            "category_mapping": CATEGORY_NAMES,
            "access_prompt_sha256": prompt_hash,
            "prediction_sources": source_files,
        },
    )
    print(json.dumps(metrics, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
