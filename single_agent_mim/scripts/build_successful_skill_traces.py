"""Build auditable Judge-C Skill-use trajectory examples.

The output is maintenance-only JSONL.  Access examples preserve the selected
Skill, tool chain, returned evidence, and correct answer.  Construction
examples additionally require that a final answer cites a memory created in a
commit where a Construction Skill was selected.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Expected object at {path}:{line_no}")
        rows.append(value)
    return rows


def _runtime_runs(roots: Iterable[Path]) -> list[Path]:
    found: dict[str, Path] = {}
    for root in roots:
        candidates: list[Path] = []
        if (root / "state" / "memory.sqlite3").is_file():
            candidates.append(root)
        if (root / "runs").is_dir():
            candidates.extend(
                item for item in (root / "runs").iterdir() if item.is_dir()
            )
        candidates.extend(
            item
            for item in root.iterdir()
            if item.is_dir() and (item / "state" / "memory.sqlite3").is_file()
        ) if root.is_dir() else None
        for candidate in candidates:
            if not (candidate / "state" / "memory.sqlite3").is_file():
                continue
            key = str(candidate.resolve()).lower()
            found[key] = candidate
    return [found[key] for key in sorted(found)]


def _open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _selected(trace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "skill_id": item.get("skill_id"),
            "name": item.get("name"),
            "description": item.get("description"),
            "content": item.get("content", []),
            "rerank_reason": item.get("rerank_reason", ""),
        }
        for item in trace.get("selected", [])
        if isinstance(item, dict) and item.get("skill_id")
    ]


def _compact_response(action_type: str, response: dict[str, Any]) -> dict[str, Any]:
    result = {"status": response.get("status")}
    if action_type == "search_memory":
        result["hits"] = [
            {
                key: hit.get(key)
                for key in (
                    "version_id", "content", "memory_kind", "subject",
                    "world_start", "world_end", "score", "paths",
                )
                if key in hit
            }
            for hit in response.get("hits", [])[:4]
            if isinstance(hit, dict)
        ]
    elif action_type == "inspect_memory":
        result["versions"] = response.get("versions", [])[:3]
        result["sources"] = response.get("sources", [])[:3]
    elif action_type == "answer":
        result.update(
            {
                "answer": response.get("answer"),
                "evidence_version_ids": response.get(
                    "evidence_version_ids", []
                ),
            }
        )
    return result


def _access_example(
    conn: sqlite3.Connection,
    prediction: dict[str, Any],
) -> dict[str, Any] | None:
    access_run = conn.execute(
        """SELECT access_run_id, skill_trace_json
           FROM access_runs WHERE qa_id = ? AND status = 'completed'
           ORDER BY completed_at DESC, created_at DESC LIMIT 1""",
        (prediction["qa_id"],),
    ).fetchone()
    if access_run is None:
        return None
    trace = _loads(access_run["skill_trace_json"], {})
    selected = _selected(trace)
    if not selected:
        return None
    actions = []
    for row in conn.execute(
        """SELECT action_type, request_json, response_json
           FROM access_actions WHERE access_run_id = ? ORDER BY step_index""",
        (access_run["access_run_id"],),
    ):
        request = _loads(row["request_json"], {})
        response = _loads(row["response_json"], {})
        actions.append(
            {
                "action": row["action_type"],
                "arguments": request.get("arguments", {}),
                "reason": request.get("reason", ""),
                "result": _compact_response(row["action_type"], response),
            }
        )
    return {
        "schema_version": "successful_skill_trace_v1",
        "side": "access",
        "judge_label": "C",
        "conversation_id": prediction.get("conversation_id"),
        "qa_id": prediction.get("qa_id"),
        "skill_ids": [item["skill_id"] for item in selected],
        "selected_skills": selected,
        "question": prediction.get("question"),
        "execution_trace": actions,
        "final_evidence_ids": prediction.get("evidence", []),
        "answer": prediction.get("prediction"),
        "reference_answer": prediction.get("answer"),
        "attribution_note": (
            "Observed Judge-C run with these Skills selected; this is not "
            "causal proof that every selected Skill caused the result."
        ),
    }


def _construction_example(
    conn: sqlite3.Connection,
    prediction: dict[str, Any],
) -> dict[str, Any] | None:
    for version_id in prediction.get("evidence", []):
        memory = conn.execute(
            """SELECT version_id, content, memory_kind, subject, predicate,
                      world_start, world_end, system_from_commit,
                      created_by_skill_ids
               FROM memory_versions WHERE version_id = ?""",
            (version_id,),
        ).fetchone()
        if memory is None or not _loads(memory["created_by_skill_ids"], []):
            continue
        commit = conn.execute(
            """SELECT commit_id, session_id, skill_trace_json
               FROM construction_commits
               WHERE commit_id = ? AND status = 'committed'""",
            (memory["system_from_commit"],),
        ).fetchone()
        if commit is None:
            continue
        trace = _loads(commit["skill_trace_json"], {})
        selected = _selected(trace)
        if not selected:
            continue
        decision = conn.execute(
            """SELECT candidate_id, action, target_memory_id, update_type,
                      result_version_id, reason
               FROM construction_decisions
               WHERE commit_id = ? AND result_version_id = ?
               ORDER BY decision_index LIMIT 1""",
            (commit["commit_id"], version_id),
        ).fetchone()
        sources = [
            dict(row)
            for row in conn.execute(
                """SELECT m.message_id, m.speaker, m.content, m.occurred_at
                   FROM memory_version_message_edges e
                   JOIN messages m ON m.message_id = e.message_id
                   WHERE e.version_id = ? ORDER BY m.turn_index LIMIT 6""",
                (version_id,),
            )
        ]
        return {
            "schema_version": "successful_skill_trace_v1",
            "side": "construction",
            "judge_label": "C",
            "conversation_id": prediction.get("conversation_id"),
            "qa_id": prediction.get("qa_id"),
            "session_id": commit["session_id"],
            "skill_ids": [item["skill_id"] for item in selected],
            "selected_skills": selected,
            "source_messages": sources,
            "construction_decision": dict(decision) if decision else None,
            "resulting_memory": {
                key: memory[key]
                for key in (
                    "version_id", "content", "memory_kind", "subject",
                    "predicate", "world_start", "world_end",
                )
            },
            "downstream_question": prediction.get("question"),
            "final_evidence_ids": prediction.get("evidence", []),
            "answer": prediction.get("prediction"),
            "reference_answer": prediction.get("answer"),
            "attribution_note": (
                "Observed Judge-C answer citing a memory from a commit where "
                "these Skills were selected; this is not per-Skill causal proof."
            ),
        }
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runtime-root", action="append", required=True,
        help="Runtime run, a directory containing runs/, or a run bundle.",
    )
    parser.add_argument("--judgments", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    judgment_rows = _load_jsonl(Path(args.judgments))
    correct = {
        str(row.get("qa_id"))
        for row in judgment_rows
        if row.get("label") == "C"
    }
    runs = _runtime_runs(Path(value) for value in args.runtime_root)
    if not runs:
        raise FileNotFoundError("No Runtime runs with state/memory.sqlite3 found")

    examples: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for run in runs:
        predictions_path = run / "locomo_predictions.jsonl"
        if not predictions_path.is_file():
            predictions_path = run / "qa_results.jsonl"
        if not predictions_path.is_file():
            continue
        with _open_readonly(run / "state" / "memory.sqlite3") as conn:
            for prediction in _load_jsonl(predictions_path):
                qa_id = str(prediction.get("qa_id", ""))
                if qa_id not in correct or prediction.get("error"):
                    continue
                for builder in (_access_example, _construction_example):
                    example = builder(conn, prediction)
                    if example is None:
                        continue
                    key = (
                        example["side"],
                        str(example.get("qa_id", "")),
                        str(example.get("session_id", "")),
                    )
                    if key not in seen:
                        seen.add(key)
                        examples.append(example)

    examples.sort(
        key=lambda item: (
            item["side"], item.get("conversation_id", ""),
            item.get("qa_id", ""), item.get("session_id", ""),
        )
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in examples),
        encoding="utf-8",
    )
    counts = {
        side: sum(row["side"] == side for row in examples)
        for side in ("access", "construction")
    }
    print(
        f"Wrote {len(examples)} successful trajectories to {output} "
        f"(access={counts['access']}, construction={counts['construction']})"
    )
    if not examples:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
