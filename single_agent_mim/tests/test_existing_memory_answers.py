"""Regression tests for clean answer-only checkpoint handling."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.run_existing_memory_answers import _validate_rows


def test_answer_checkpoint_accepts_one_fresh_row_per_expected_question():
    rows = [
        {"conversation_id": "conv-1", "qa_id": "q1"},
        {"conversation_id": "conv-1", "qa_id": "q2"},
    ]
    _validate_rows(rows, "conv-1", {"q1", "q2"})


@pytest.mark.parametrize(
    "rows",
    [
        [
            {"conversation_id": "conv-1", "qa_id": "q1"},
            {"conversation_id": "conv-1", "qa_id": "q1"},
        ],
        [{"conversation_id": "other", "qa_id": "q1"}],
        [{"conversation_id": "conv-1", "qa_id": "historical-extra"}],
    ],
)
def test_answer_checkpoint_rejects_duplicates_cross_conversation_and_extras(rows):
    with pytest.raises(RuntimeError, match="Invalid answer checkpoint"):
        _validate_rows(rows, "conv-1", {"q1", "q2"})
