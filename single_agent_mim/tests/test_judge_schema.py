"""Regression tests for Judge prediction schema compatibility.

Section 13 of the Skill Bank guide requires the Judge loader to accept both
the current schema (``reference`` / ``evidence_ids``) and the legacy schema
(``answer`` / ``evidence``).
"""

import pytest


def _normalize_row(row: dict) -> dict:
    """Mirror of the normalisation function in scripts/judge_predictions.py."""
    normalized = dict(row)
    if "reference" in row and row["reference"]:
        normalized["reference"] = row["reference"]
    elif "answer" in row and row["answer"]:
        normalized["reference"] = row["answer"]
    else:
        raise KeyError(
            f"Row {row.get('qa_id', 'unknown')}: "
            "neither 'reference' nor 'answer' is present"
        )
    if "evidence_ids" in row:
        normalized["evidence_ids"] = row["evidence_ids"]
    elif "evidence" in row:
        normalized["evidence_ids"] = row.get("evidence", [])
    else:
        normalized["evidence_ids"] = []
    return normalized


class TestJudgeSchemaNormalisation:
    """Verify that both legacy and current schemas are accepted."""

    def test_current_schema_with_reference_and_evidence_ids(self):
        """Current schema: reference + evidence_ids."""
        row = {
            "qa_id": "conv-48_qa_0002",
            "conversation_id": "conv-48",
            "category": 1,
            "question": "What is this?",
            "reference": "A thing",
            "prediction": "A thing",
            "evidence_ids": ["msg1", "msg2"],
        }
        result = _normalize_row(row)
        assert result["reference"] == "A thing"
        assert result["evidence_ids"] == ["msg1", "msg2"]

    def test_legacy_schema_with_answer_and_evidence(self):
        """Legacy schema: answer + evidence."""
        row = {
            "qa_id": "conv-30_qa_0006",
            "conversation_id": "conv-30",
            "category": 2,
            "question": "What is that?",
            "answer": "That thing",
            "prediction": "That thing",
            "evidence": ["msg_a", "msg_b"],
        }
        result = _normalize_row(row)
        assert result["reference"] == "That thing"
        assert result["evidence_ids"] == ["msg_a", "msg_b"]

    def test_reference_takes_priority_over_answer(self):
        """When both fields exist, reference wins."""
        row = {
            "qa_id": "conv-42_qa_0003",
            "conversation_id": "conv-42",
            "category": 3,
            "question": "Why?",
            "reference": "Primary answer",
            "answer": "Secondary answer",
            "prediction": "Pred",
            "evidence_ids": [],
        }
        result = _normalize_row(row)
        assert result["reference"] == "Primary answer"

    def test_missing_both_raises_key_error(self):
        """Reject a row if neither reference nor answer is present."""
        row = {
            "qa_id": "conv-44_qa_0001",
            "conversation_id": "conv-44",
            "category": 1,
            "question": "What?",
            "prediction": "Something",
        }
        with pytest.raises(KeyError, match="neither 'reference' nor 'answer'"):
            _normalize_row(row)

    def test_no_evidence_field_yields_empty_list(self):
        """Rows without evidence or evidence_ids get an empty list."""
        row = {
            "qa_id": "conv-48_qa_0099",
            "conversation_id": "conv-48",
            "category": 4,
            "question": "Where?",
            "reference": "Here",
            "prediction": "There",
        }
        result = _normalize_row(row)
        assert result["evidence_ids"] == []

    def test_empty_reference_string_is_falsey(self):
        """Empty string in reference should fall through to answer."""
        row = {
            "qa_id": "conv-49_qa_0001",
            "conversation_id": "conv-49",
            "category": 5,
            "question": "When?",
            "reference": "",
            "answer": "The real answer",
            "prediction": "Pred",
        }
        result = _normalize_row(row)
        assert result["reference"] == "The real answer"
