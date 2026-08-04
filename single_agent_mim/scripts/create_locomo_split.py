"""Create or verify the fixed LoCoMo conversation-level split."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mim.eval.split import (  # noqa: E402
    build_balanced_conversation_split,
    validate_split,
)
from mim.schemas import DatasetSplit  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic, conversation-level, QA/category-balanced "
            "LoCoMo 6:2:2 split."
        )
    )
    parser.add_argument(
        "--dataset",
        default="../LoCoMo/data/locomo10.json",
    )
    parser.add_argument(
        "--output",
        default="data/splits/locomo_6_2_2.json",
    )
    parser.add_argument(
        "--stats-output",
        default="data/splits/locomo_6_2_2.stats.json",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the existing output instead of rewriting it.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    dataset_path = Path(args.dataset)
    output_path = Path(args.output)
    if args.check:
        current = DatasetSplit(
            **json.loads(output_path.read_text(encoding="utf-8"))
        )
        errors = validate_split(current, dataset_path)
        expected, _ = build_balanced_conversation_split(
            dataset_path, seed=args.seed
        )
        if current.model_dump(mode="json") != expected.model_dump(mode="json"):
            errors.append(
                "existing split is valid but is not the deterministic "
                "balanced allocation"
            )
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print("Split is valid and matches the deterministic allocation.")
        return 0

    split, report = build_balanced_conversation_split(
        dataset_path, seed=args.seed
    )
    errors = validate_split(split, dataset_path)
    if errors:
        raise RuntimeError("; ".join(errors))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            split.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    stats_path = Path(args.stats_output)
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["splits"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
