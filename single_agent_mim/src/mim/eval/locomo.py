"""LoCoMo / LongMemEval dataset loader.

Loads conversation data from JSON, normalizes to Conversation/Session/Message/Question
schemas, and applies fixed conversation-level splits.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from ..schemas import (
    Conversation,
    DatasetSplit,
    Message,
    Question,
    Session,
)


def load_dataset(path: str | Path) -> tuple[list[Conversation], dict[str, list[Question]]]:
    """Load a dataset file and convert to MiM schemas.

    Supports two formats:

    1. LongMemEval format (list of QA entries with haystack sessions):
       Each entry has question, answer, haystack_session_ids, haystack_dates, etc.

    2. Pseudo-QA format (qa_20 directory):
       Each file is a conversation with qas and sessions arrays.

    Returns:
        (conversations, questions_by_conv_id)
    """
    path = Path(path)

    if path.is_dir():
        return _load_qa20_dir(path)
    with open(path, "r", encoding="utf-8") as f:
        preview = json.load(f)
    if (
        isinstance(preview, list)
        and preview
        and isinstance(preview[0], dict)
        and "conversation" in preview[0]
        and "qa" in preview[0]
    ):
        return _load_locomo_data(preview)
    return _load_longmemeval_data(preview)


def _load_longmemeval_file(path: Path) -> tuple[list[Conversation], dict[str, list[Question]]]:
    """Load LongMemEval-style dataset (e.g., longmemeval_s_cleaned.json)."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return _load_longmemeval_data(data)


def _load_longmemeval_data(data: list[dict]) -> tuple[list[Conversation], dict[str, list[Question]]]:
    """Normalize already-loaded LongMemEval data."""

    conversations: list[Conversation] = []
    questions_by_conv: dict[str, list[Question]] = {}

    # LongMemEval format: each entry is a QA with haystack sessions
    for i, entry in enumerate(data):
        # Use question_id as conversation_id if available
        conv_id = entry.get("question_id", f"conv_{i}")

        # Build session(s) from haystack
        sessions = _build_sessions_from_haystack(entry)

        # Build question
        q = Question(
            qa_id=entry.get("question_id", f"qa_{i}"),
            question=entry.get("question", ""),
            reference_answer=entry.get("answer", ""),
            category=entry.get("question_type", None),
            source_evidence=_extract_source_evidence(entry),
        )

        conversations.append(Conversation(
            conversation_id=conv_id,
            sessions=sessions,
        ))
        questions_by_conv[conv_id] = [q]

    return conversations, questions_by_conv


def _load_locomo_data(data: list[dict]) -> tuple[list[Conversation], dict[str, list[Question]]]:
    """Load the official LoCoMo ``locomo10.json`` structure.

    LoCoMo repeats ``dia_id`` values (for example ``D1:1``) across samples.
    Runtime Message IDs therefore use ``<conversation_id>:<dia_id>``.  QA
    evidence receives the same deterministic namespace, so joins remain exact
    without an LLM or fuzzy matching while satisfying SQLite global keys.
    """
    conversations: list[Conversation] = []
    questions_by_conv: dict[str, list[Question]] = {}

    for sample_index, sample in enumerate(data):
        conv_id = str(sample.get("sample_id") or f"conv_{sample_index}")
        raw_conv = sample.get("conversation", {})
        speaker_a = raw_conv.get("speaker_a")

        session_numbers = sorted(
            int(key.removeprefix("session_"))
            for key, value in raw_conv.items()
            if key.startswith("session_")
            and key.removeprefix("session_").isdigit()
            and isinstance(value, list)
        )
        sessions: list[Session] = []
        for session_number in session_numbers:
            session_key = f"session_{session_number}"
            session_id = f"{conv_id}_s{session_number:02d}"
            session_time = raw_conv.get(f"{session_key}_date_time")
            messages: list[Message] = []
            for turn_index, raw_message in enumerate(raw_conv.get(session_key, [])):
                speaker = str(raw_message.get("speaker", "unknown"))
                local_message_id = str(
                    raw_message.get("dia_id")
                    or f"s{session_number:02d}_m{turn_index:03d}"
                )
                messages.append(Message(
                    message_id=f"{conv_id}:{local_message_id}",
                    role="user" if speaker == speaker_a else "assistant",
                    speaker=speaker,
                    content=str(raw_message.get("text", "")),
                    time=session_time,
                ))
            sessions.append(Session(
                session_id=session_id,
                messages=messages,
                time=session_time,
            ))

        questions: list[Question] = []
        for qa_index, raw_qa in enumerate(sample.get("qa", [])):
            evidence_ids = [
                f"{conv_id}:{item}"
                for item in raw_qa.get("evidence", [])
                if str(item)
            ]
            category = raw_qa.get("category")
            try:
                category = int(category) if category is not None else None
            except (TypeError, ValueError):
                category = None
            questions.append(Question(
                qa_id=f"{conv_id}_qa_{qa_index:04d}",
                question=str(raw_qa.get("question", "")),
                reference_answer=str(raw_qa.get("answer", "")),
                category=category,
                source_evidence=[["", message_id] for message_id in evidence_ids],
            ))

        conversations.append(Conversation(
            conversation_id=conv_id,
            sessions=sessions,
        ))
        questions_by_conv[conv_id] = questions

    return conversations, questions_by_conv


def _load_qa20_dir(path: Path) -> tuple[list[Conversation], dict[str, list[Question]]]:
    """Load qa_20 directory format (each file is one conversation with QAs and sessions)."""
    conversations: list[Conversation] = []
    questions_by_conv: dict[str, list[Question]] = {}

    files = sorted(path.glob("qa_*.json"))
    for fpath in files:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)

        conv_id = str(data.get("id", fpath.stem))

        # Build sessions
        raw_sessions = data.get("sessions", [])
        if isinstance(raw_sessions, dict):
            # sessions is a dict of session_id → messages
            sessions = []
            for session_id, msgs in raw_sessions.items():
                messages = []
                if isinstance(msgs, list):
                    for mi, m in enumerate(msgs):
                        if isinstance(m, dict):
                            messages.append(Message(
                                message_id=m.get("message_id", f"msg_{mi}"),
                                role=m.get("role", "assistant"),
                                content=m.get("content", ""),
                                time=m.get("time"),
                            ))
                sessions.append(Session(
                    session_id=session_id,
                    messages=messages,
                ))
        elif isinstance(raw_sessions, list):
            sessions = []
            for si, raw_session in enumerate(raw_sessions):
                if not isinstance(raw_session, dict):
                    continue
                raw_messages = raw_session.get(
                    "messages", raw_session.get("content", [])
                )
                messages = [
                    Message(
                        message_id=m.get(
                            "message_id", f"{conv_id}_s{si:02d}_m{mi:03d}"
                        ),
                        role=m.get("role", "assistant"),
                        speaker=m.get("speaker"),
                        content=m.get("content", ""),
                        time=m.get("time", raw_session.get("time")),
                    )
                    for mi, m in enumerate(raw_messages)
                    if isinstance(m, dict)
                ]
                sessions.append(Session(
                    session_id=raw_session.get(
                        "session_id", f"{conv_id}_s{si:02d}"
                    ),
                    messages=messages,
                    time=raw_session.get("time"),
                ))
        else:
            sessions = []

        # Build questions
        qas = data.get("qas", [])
        if isinstance(qas, dict):
            qas = [qas]  # single QA object
        conv_qs: list[Question] = []
        for qi, qa in enumerate(qas):
            if isinstance(qa, dict):
                conv_qs.append(Question(
                    qa_id=qa.get("qa_id", f"qa_{conv_id}_{qi}"),
                    question=qa.get("question", ""),
                    reference_answer=qa.get("answer", ""),
                    category=qa.get("type", None),
                    source_evidence=qa.get("source", []),
                ))

        conversations.append(Conversation(
            conversation_id=conv_id,
            sessions=sessions,
        ))
        questions_by_conv[conv_id] = conv_qs

    return conversations, questions_by_conv


def _build_sessions_from_haystack(entry: dict) -> list[Session]:
    """Build Session objects from LongMemEval haystack format."""
    session_ids = entry.get("haystack_session_ids", [])
    dates = entry.get("haystack_dates", [])

    if not session_ids:
        return [Session(
            session_id=f"session_{entry.get('question_id', 'unknown')}",
            messages=[],
        )]

    # In LongMemEval, each session_id maps to a conversation.
    # The actual message content is typically in the dataset's session data.
    # For the cleaned version, we build one wrapper session per haystack entry.
    sessions: list[Session] = []
    for si, sid in enumerate(session_ids):
        date = dates[si] if si < len(dates) else None
        sessions.append(Session(
            session_id=sid,
            messages=[],  # actual messages loaded separately if available
            time=date,
        ))
    return sessions


def _extract_source_evidence(entry: dict) -> list[list[str]]:
    """Extract source evidence from dataset entry."""
    answer_session_ids = entry.get("answer_session_ids", [])
    source = []
    for sid in answer_session_ids:
        source.append([sid, ""])  # session-level evidence
    return source


def apply_split(
    conversations: list[Conversation],
    split: DatasetSplit,
) -> tuple[list[Conversation], list[Conversation], list[Conversation]]:
    """Split conversations into train/validation/test based on split file.

    Returns (train_convs, val_convs, test_convs).
    """
    conv_map = {c.conversation_id: c for c in conversations}

    train = [conv_map[cid] for cid in split.train if cid in conv_map]
    val = [conv_map[cid] for cid in split.validation if cid in conv_map]
    test = [conv_map[cid] for cid in split.test if cid in conv_map]

    return train, val, test
