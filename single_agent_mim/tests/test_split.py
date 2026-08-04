"""Tests for the fixed LoCoMo conversation-level split."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mim.eval.split import (
    build_balanced_conversation_split,
    validate_split,
)
from mim.schemas import DatasetSplit


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATASET = PROJECT_ROOT.parent / "LoCoMo" / "data" / "locomo10.json"
SPLIT_FILE = PROJECT_ROOT / "data" / "splits" / "locomo_6_2_2.json"


def test_fixed_split_is_complete_disjoint_and_bound_to_dataset():
    split = DatasetSplit(
        **json.loads(SPLIT_FILE.read_text(encoding="utf-8"))
    )
    assert validate_split(split, DATASET) == []
    assert (len(split.train), len(split.validation), len(split.test)) == (
        6,
        2,
        2,
    )


def test_fixed_split_matches_deterministic_balanced_allocation():
    expected, report = build_balanced_conversation_split(DATASET, seed=42)
    current = DatasetSplit(
        **json.loads(SPLIT_FILE.read_text(encoding="utf-8"))
    )
    assert current == expected
    assert report["splits"]["train"]["qa_count"] == 1200
    assert report["splits"]["validation"]["qa_count"] == 392
    assert report["splits"]["test"]["qa_count"] == 394
