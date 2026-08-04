"""Deterministic conversation-level split construction for LoCoMo.

The official LoCoMo file contains only ten conversations but nearly two
thousand QA cases.  We keep conversations indivisible, then exhaustively find
the requested conversation counts whose QA volume and category distribution
best match the target ratio.  No question text, answer text, model output, or
evaluation score participates in split selection.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections import Counter
from pathlib import Path
from typing import Any

from ..schemas import DatasetSplit


def sha256_file(path: str | Path) -> str:
    """Return the full SHA-256 digest used to bind a split to one dataset."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_statistics(
    dataset_path: str | Path,
) -> tuple[list[str], dict[str, int], dict[str, Counter[str]]]:
    data = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("LoCoMo dataset must be a non-empty JSON list.")

    ids: list[str] = []
    qa_counts: dict[str, int] = {}
    category_counts: dict[str, Counter[str]] = {}
    for index, sample in enumerate(data):
        if not isinstance(sample, dict):
            raise ValueError(f"Dataset item {index} is not an object.")
        conversation_id = str(
            sample.get("sample_id") or f"conv_{index}"
        )
        if conversation_id in qa_counts:
            raise ValueError(
                f"Duplicate conversation ID: {conversation_id}"
            )
        qas = sample.get("qa", [])
        if not isinstance(qas, list):
            raise ValueError(
                f"qa must be a list for {conversation_id}"
            )
        ids.append(conversation_id)
        qa_counts[conversation_id] = len(qas)
        category_counts[conversation_id] = Counter(
            str(question.get("category", "unknown"))
            for question in qas
            if isinstance(question, dict)
        )
    return ids, qa_counts, category_counts


def _sum_categories(
    ids: tuple[str, ...] | list[str],
    category_counts: dict[str, Counter[str]],
) -> Counter[str]:
    return sum(
        (category_counts[conversation_id] for conversation_id in ids),
        Counter(),
    )


def build_balanced_conversation_split(
    dataset_path: str | Path,
    *,
    train_size: int = 6,
    validation_size: int = 2,
    test_size: int = 2,
    seed: int = 42,
) -> tuple[DatasetSplit, dict[str, Any]]:
    """Build a deterministic, leakage-safe LoCoMo split.

    Conversation counts are hard constraints.  Among all valid allocations,
    the objective minimizes deviation from the target ratio for:

    1. total QA volume; and
    2. each annotated QA category.

    ``seed`` is used only to break exactly equal objective scores, not to
    search until a favorable test result is found.
    """
    ids, qa_counts, category_counts = _load_statistics(dataset_path)
    requested_total = train_size + validation_size + test_size
    if requested_total != len(ids):
        raise ValueError(
            "Split sizes must cover every conversation exactly: "
            f"requested={requested_total}, dataset={len(ids)}"
        )
    if min(train_size, validation_size, test_size) <= 0:
        raise ValueError("Every split must contain at least one conversation.")

    total_qas = sum(qa_counts.values())
    total_categories = _sum_categories(ids, category_counts)
    target_ratios = {
        "train": train_size / requested_total,
        "validation": validation_size / requested_total,
        "test": test_size / requested_total,
    }

    def group_stats(group: tuple[str, ...]) -> tuple[int, Counter[str]]:
        return (
            sum(qa_counts[conversation_id] for conversation_id in group),
            _sum_categories(group, category_counts),
        )

    def objective(groups: dict[str, tuple[str, ...]]) -> float:
        score = 0.0
        for name, group in groups.items():
            target = target_ratios[name]
            qa_count, categories = group_stats(group)
            score += (qa_count / max(total_qas, 1) - target) ** 2
            if total_categories:
                score += sum(
                    (
                        categories[category] / category_total - target
                    ) ** 2
                    for category, category_total in total_categories.items()
                    if category_total > 0
                ) / len(total_categories)
        return score

    def tie_break(groups: dict[str, tuple[str, ...]]) -> str:
        payload = json.dumps(
            {"seed": seed, **groups},
            ensure_ascii=True,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    best_key: tuple[float, str] | None = None
    best_groups: dict[str, tuple[str, ...]] | None = None
    for train in itertools.combinations(ids, train_size):
        remainder = [
            conversation_id
            for conversation_id in ids
            if conversation_id not in train
        ]
        for validation in itertools.combinations(
            remainder, validation_size
        ):
            test = tuple(
                conversation_id
                for conversation_id in remainder
                if conversation_id not in validation
            )
            groups = {
                "train": tuple(sorted(train)),
                "validation": tuple(sorted(validation)),
                "test": tuple(sorted(test)),
            }
            key = (objective(groups), tie_break(groups))
            if best_key is None or key < best_key:
                best_key = key
                best_groups = groups

    if best_groups is None or best_key is None:
        raise RuntimeError("No valid split allocation was found.")

    split = DatasetSplit(
        dataset_sha256=sha256_file(dataset_path),
        seed=seed,
        train=list(best_groups["train"]),
        validation=list(best_groups["validation"]),
        test=list(best_groups["test"]),
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "method": "exhaustive_conversation_level_balance",
        "selection_inputs": [
            "conversation_id",
            "qa_count",
            "qa_category_count",
        ],
        "selection_excludes": [
            "question_text",
            "answer_text",
            "model_output",
            "evaluation_metric",
        ],
        "requested_conversation_ratio": {
            "train": train_size,
            "validation": validation_size,
            "test": test_size,
        },
        "seed_for_exact_ties": seed,
        "dataset_sha256": split.dataset_sha256,
        "objective_score": best_key[0],
        "total": {
            "conversations": len(ids),
            "qa": total_qas,
            "categories": dict(sorted(total_categories.items())),
        },
        "splits": {},
    }
    for name, group in best_groups.items():
        qa_count, categories = group_stats(group)
        report["splits"][name] = {
            "conversation_ids": list(group),
            "conversation_count": len(group),
            "qa_count": qa_count,
            "qa_fraction": qa_count / max(total_qas, 1),
            "category_counts": dict(sorted(categories.items())),
        }
    return split, report


def validate_split(
    split: DatasetSplit,
    dataset_path: str | Path,
) -> list[str]:
    """Return deterministic validation errors; an empty list means valid."""
    ids, _, _ = _load_statistics(dataset_path)
    expected = set(ids)
    groups = {
        "train": split.train,
        "validation": split.validation,
        "test": split.test,
    }
    errors: list[str] = []
    for name, group in groups.items():
        if len(group) != len(set(group)):
            errors.append(f"{name} contains duplicate conversation IDs")
    all_ids = split.train + split.validation + split.test
    if len(all_ids) != len(set(all_ids)):
        errors.append("train/validation/test overlap")
    actual = set(all_ids)
    if actual != expected:
        errors.append(
            "split coverage mismatch: "
            f"missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )
    actual_hash = sha256_file(dataset_path)
    if split.dataset_sha256.lower() != actual_hash.lower():
        errors.append(
            "dataset SHA-256 mismatch: "
            f"split={split.dataset_sha256}, actual={actual_hash}"
        )
    return errors
