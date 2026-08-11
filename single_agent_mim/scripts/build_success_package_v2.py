"""Build a default-policy success package v2: Judge-correct questions answered
with NO Skill, each enriched with (a) the full access trajectory and (b) the
construction process of every memory entry cited in the final answer.

This upgrades the v1 package so the candidate generator can see not only that
the default policy succeeded, but exactly HOW: which retrieval route found
the evidence, and which construction decisions (and Construction Skills, if
any) produced the cited memory entries.

Usage:
  python scripts/build_success_package_v2.py \
      --runtime-root outputs/empty_bank_two_phase_full_v3_20260809/train \
      --judgments outputs/empty_bank_two_phase_full_v3_20260809/judge/judgments.jsonl \
      --output outputs/b0_success_package_v2.jsonl
"""

from __future__ import annotations

import argparse
import json
import sqlite3
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


def _load_traces(run_root: Path, conversation_id: str) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    path = run_root / conversation_id / "traces" / "access_traces.jsonl"
    for row in _load_jsonl(path):
        rows[str(row["qa_id"])] = row
    return rows


def _load_construction_traces(run_root: Path, conversation_id: str) -> list[dict]:
    return _load_jsonl(
        run_root / conversation_id / "traces" / "construction_traces.jsonl"
    )


def _memory_ids(db_path: Path, version_ids: list[str]) -> dict[str, str]:
    """version_id -> memory_id for the cited versions."""
    if not db_path.exists() or not version_ids:
        return {}
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT version_id, memory_id FROM memory_versions "
            "WHERE version_id IN (%s)"
            % ",".join("?" * len(version_ids)),
            version_ids,
        ).fetchall()
        return {str(v): str(m) for v, m in rows}
    finally:
        conn.close()


def _memory_construction(
    db_path: Path,
    construction_traces: list[dict],
    memory_id: str,
) -> list[dict]:
    """Reconstruct how one memory entry was built, from the version chain.

    Uses construction_decisions.result_version_id to link each decision to
    the exact memory version it produced, then attaches the session's
    Construction Skill selection from the construction traces.
    """
    if not db_path.exists() or not memory_id:
        return []
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        versions = conn.execute(
            "SELECT version_id, version_no FROM memory_versions "
            "WHERE memory_id=? ORDER BY version_no",
            (memory_id,),
        ).fetchall()
        version_ids = [str(v["version_id"]) for v in versions]
        if not version_ids:
            return []
        decisions = conn.execute(
            "SELECT candidate_id, commit_id, action, update_type, "
            "         target_memory_id, result_version_id, reason "
            "FROM construction_decisions "
            "WHERE result_version_id IN (%s)"
            % ",".join("?" * len(version_ids)),
            version_ids,
        ).fetchall()
    finally:
        conn.close()

    # commit_id -> construction trace (session + skill selection)
    trace_by_commit = {
        str(t.get("commit_id")): t for t in construction_traces
    }
    decision_by_version = {
        str(d["result_version_id"]): d for d in decisions
    }
    history = []
    for version in versions:
        vid = str(version["version_id"])
        decision = decision_by_version.get(vid)
        if decision is None:
            continue
        trace = trace_by_commit.get(str(decision["commit_id"]), {})
        history.append(
            {
                "version_id": vid,
                "version_no": version["version_no"],
                "action": decision["action"],
                "update_type": decision["update_type"],
                "target_memory_id": decision["target_memory_id"],
                "candidate_id": decision["candidate_id"],
                "reason": (decision["reason"] or "")[:200],
                "session_id": trace.get("session_id"),
                "construction_skill_ids": trace.get("skill_ids") or [],
                "construction_skill_trace": trace.get("skill_trace"),
            }
        )
    return history


def _search_actions(trace: dict) -> list[dict]:
    steps = []
    for action in trace.get("actions") or []:
        act = action.get("action")
        if act in {"search_memory", "inspect_memory", "answer"}:
            steps.append({"action": act, "arguments": action.get("arguments", {})})
    return steps


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/qwen3_8b_swap_b0.yaml")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--judgments", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    config = load_config(args.config)
    _, questions_map = load_dataset(config.dataset.path)
    question_by_id = {
        q.qa_id: q for questions in questions_map.values() for q in questions
    }

    judged = {
        str(row.get("qa_id", ""))
        for row in _load_jsonl(Path(args.judgments))
        if str(row.get("label", "")).upper() == "C"
    }

    root = Path(args.runtime_root)
    examples = []
    seen = set()
    for qa_file in sorted(root.glob("*/qa_results.jsonl")):
        conversation_id = qa_file.parent.name
        traces = _load_traces(root, conversation_id)
        construction = _load_construction_traces(root, conversation_id)
        db = root / conversation_id / "state" / "memory.sqlite3"
        for row in _load_jsonl(qa_file):
            qa_id = str(row.get("qa_id", ""))
            if qa_id not in judged or qa_id in seen:
                continue
            if row.get("error"):
                continue
            if row.get("skill_ids"):
                continue  # skill was selected: not a default-policy success
            seen.add(qa_id)
            trace = traces.get(qa_id, {})
            final_evidence = trace.get("final_evidence_ids") or []
            memory_map = _memory_ids(db, final_evidence)
            example = {
                "qa_id": qa_id,
                "conversation_id": conversation_id,
                "category": row.get("category", ""),
                "question": row.get("question", ""),
                "reference_answer": row.get("reference", ""),
                "prediction": row.get("prediction", ""),
                "skill_ids": [],
                "judge_label": "C",
                "trajectory": {
                    "skill_trace": trace.get("skill_trace"),
                    "search_actions": _search_actions(trace),
                    "visible_memories": trace.get("visible_evidence_ids") or [],
                    "final_evidence_ids": final_evidence,
                },
                "memory_construction": [
                    {
                        "version_id": vid,
                        "memory_id": memory_map.get(vid, ""),
                        "history": _memory_construction(
                            db, construction, memory_map.get(vid, "")
                        ),
                    }
                    for vid in final_evidence
                ],
            }
            examples.append(example)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        for example in examples:
            handle.write(json.dumps(example, ensure_ascii=False) + "\n")
    with_construction = sum(
        1 for e in examples if any(h["history"] for h in e["memory_construction"])
    )
    print(
        f"Default-policy success package v2: {len(examples)} examples "
        f"({with_construction} with memory-construction history) -> {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
