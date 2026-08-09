"""Flip diagnosis over two runs of the same validation conversations.

For every question whose judge label flipped between two Skill-Bank runs
(C->W or W->C), compare the correct side (answer, selected Skills with full
retrieval trace, visible memories, search actions) against the wrong side
(same evidence plus the standard three-stage diagnosis packages). A
FlipDiagnosisAgent builds one claim-level contrastive core and emits zero or
more answer/access/construction projections, written to
``<output-root>/<side>_failure/packages/<conversation>/<qa>_flip_failure.json``
so ``run_candidates_from_diagnosis.py`` can consume it unchanged.

Usage:
  python scripts/run_flip_diagnosis.py \
      --config configs/qwen3_8b_dashscope.yaml \
      --flips outputs/flip_diagnosis_input.json \
      --diagnosis-root outputs/flip_diag_prep \
      --output-root outputs/flip_diag_packages \
      --workers 8 [--max-items 0]
"""

from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.agents.flip_failure import FlipDiagnosisAgent
from mim.config import load_config
from mim.llm import create_client


RUN_DIRS = {
    "swap_val_base": "outputs/swap_val_base",
    "v1b_val_samebatch": "outputs/v1b_val_samebatch",
    "v2b_val_dedup": "outputs/v2b_val",
    "v2c_val": "outputs/v2c_eval/val",
    "v2c_test": "outputs/v2c_eval/test",
    "b4_val": "outputs/v2c_full_iter/val",
    "b4_test": "outputs/v2c_full_iter/test",
    "swap_test_base": "outputs/swap_test_base",
    "rebuild_val": "outputs/rebuild_iter/val",
    "rebuild_test": "outputs/rebuild_iter/test",
    "swap_train": "outputs/swap_train",
    "v2c_full_iter": "outputs/v2c_full_iter",
    "rebuild_train": "outputs/rebuild_iter/train",
    "bank1_train": "outputs/bank1_train",
    "bank2_obj_train": "outputs/bank2_obj/train",
}
DIAGNOSIS_DIRS: dict[str, str] = {}
RUN_QUESTIONS = {
    "swap_train": ("conv-30", "conv-42", "conv-43", "conv-44", "conv-47", "conv-50"),
    "v2c_full_iter": ("conv-30", "conv-42", "conv-43", "conv-44", "conv-47", "conv-50"),
    "rebuild_train": ("conv-30", "conv-42", "conv-43", "conv-44", "conv-47", "conv-50"),
    "bank1_train": ("conv-30", "conv-42", "conv-43", "conv-44", "conv-47", "conv-50"),
    "bank2_obj_train": ("conv-30", "conv-42", "conv-43", "conv-44", "conv-47", "conv-50"),
    "swap_val_base": ("conv-26", "conv-41"),
    "swap_test_base": ("conv-48", "conv-49"),
    "v1b_val_samebatch": ("conv-26", "conv-41"),
    "v2b_val_dedup": ("conv-26", "conv-41"),
    "v2c_val": ("conv-26", "conv-41"),
    "v2c_test": ("conv-48", "conv-49"),
    "b4_val": ("conv-26", "conv-41"),
    "b4_test": ("conv-48", "conv-49"),
    "rebuild_val": ("conv-26", "conv-41"),
    "rebuild_test": ("conv-48", "conv-49"),
}


def _conv(qa_id: str) -> str:
    return qa_id.split("_")[0]


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_traces(run_dir: Path, conversation_id: str) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    path = run_dir / conversation_id / "traces" / "access_traces.jsonl"
    for row in _load_jsonl(path):
        rows[str(row["qa_id"])] = row
    return rows


def _load_memory_contents(db_path: Path) -> dict[str, str]:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {
            str(version_id): content
            for version_id, content in conn.execute(
                "SELECT version_id, content FROM memory_versions"
            ).fetchall()
        }
    finally:
        conn.close()


def _load_current_memories(db_path: Path, snapshot_commit_id: int) -> list[dict]:
    """Load the actual memory state, not merely what Access happened to see."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT version_id, memory_id, version_no, content, memory_kind,
                      subject, predicate, object_text, world_start, world_end,
                      update_type
               FROM memory_versions
               WHERE system_from_commit<=?
                 AND (system_to_commit IS NULL OR system_to_commit>?)
               ORDER BY memory_id, version_no""",
            (snapshot_commit_id, snapshot_commit_id),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _load_construction_traces(run_dir: Path, conversation_id: str) -> list[dict]:
    path = run_dir / conversation_id / "traces" / "construction_traces.jsonl"
    return _load_jsonl(path)


def _construction_skill_traces(rows: list[dict]) -> list[dict]:
    traces: list[dict] = []
    for row in rows:
        trace = row.get("skill_trace")
        if not isinstance(trace, dict) or not trace:
            continue
        item = dict(trace)
        item.setdefault("session_id", row.get("session_id"))
        item.setdefault("commit_id", row.get("commit_id"))
        traces.append(item)
    return traces


def _visible_memories(trace: dict, memory_contents: dict[str, str]) -> list[dict]:
    visible = []
    for version_id in trace.get("visible_evidence_ids") or []:
        content = memory_contents.get(str(version_id))
        if content is None:
            continue
        visible.append({"version_id": str(version_id), "content": content})
    return visible


def _search_actions(trace: dict) -> list[dict]:
    steps = []
    for action in trace.get("actions") or []:
        act = action.get("action")
        if act in {"search_memory", "inspect_memory", "answer"}:
            steps.append(
                {
                    "action": act,
                    "arguments": action.get("arguments", {}),
                }
            )
    return steps


def _skill_trace_summary(trace: dict) -> dict | None:
    skill_trace = trace.get("skill_trace")
    if not isinstance(skill_trace, dict):
        return None
    return {
        "selected": [
            {
                "skill_id": item.get("skill_id"),
                "version_id": item.get("version_id"),
                "name": item.get("name"),
                "description": item.get("description"),
                "score": item.get("score"),
                "semantic_score": item.get("semantic_score"),
                "lexical_score": item.get("lexical_score"),
                "rerank_rank": item.get("rerank_rank"),
                "rerank_reason": item.get("rerank_reason"),
            }
            for item in skill_trace.get("selected", [])
        ],
        "nearby_not_selected": [
            {
                "skill_id": item.get("skill_id"),
                "name": item.get("name"),
                "score": item.get("score"),
            }
            for item in skill_trace.get("nearby_not_selected", [])
        ],
        "bank_version": skill_trace.get("bank_version"),
        "reranker_error": skill_trace.get("reranker_error"),
    }


def _load_standard_packages(
    diagnosis_root: Path, run_name: str, conversation_id: str, qa_id: str
) -> dict:
    packages: dict[str, dict] = {}
    base = Path(
        DIAGNOSIS_DIRS.get(
            run_name, str(diagnosis_root / run_name / "diagnosis")
        )
    )
    # Answer diagnosis is intentionally record-only in the standard pipeline,
    # hence it lives in a JSONL summary rather than packages/.
    answer_rows = _load_jsonl(base / "answer_failure" / "answer_failures.jsonl")
    for row in answer_rows:
        if str(row.get("qa_id")) == qa_id:
            packages["answer_failure"] = row
            break
    for component in ("access_failure", "cons_failure"):
        path = (
            base
            / component
            / "packages"
            / conversation_id
            / f"{qa_id}_{component.replace('_failure', '')}_failure.json"
        )
        if path.exists():
            packages[component] = json.loads(
                path.read_text(encoding="utf-8")
            )
    return packages


def build_flip_case(
    *,
    chain: dict,
    direction: str,
    qa_id: str,
    conversation_id: str,
    ok_run: str,
    bad_run: str,
    question: str,
    reference_answer: str,
    diagnosis_root: Path = Path("outputs/flip_diag_prep"),
) -> dict:
    ok_dir = Path(RUN_DIRS[ok_run])
    bad_dir = Path(RUN_DIRS[bad_run])
    ok_traces = _load_traces(ok_dir, conversation_id)
    bad_traces = _load_traces(bad_dir, conversation_id)
    ok_db = ok_dir / conversation_id / "state" / "memory.sqlite3"
    bad_db = bad_dir / conversation_id / "state" / "memory.sqlite3"

    ok_trace = ok_traces.get(qa_id)
    bad_trace = bad_traces.get(qa_id)
    if ok_trace is None or bad_trace is None:
        raise ValueError(
            f"Missing trace for {qa_id}: ok={ok_trace is not None} "
            f"bad={bad_trace is not None}"
        )

    ok_memories = _visible_memories(ok_trace, _load_memory_contents(ok_db))
    bad_memories = _visible_memories(bad_trace, _load_memory_contents(bad_db))

    standard_packages = _load_standard_packages(
        diagnosis_root, bad_run, conversation_id, qa_id
    )
    ok_construction = _load_construction_traces(ok_dir, conversation_id)
    bad_construction = _load_construction_traces(bad_dir, conversation_id)

    return {
        "qa_id": qa_id,
        "conversation_id": conversation_id,
        "flip": {
            "chain": chain["name"],
            "direction": direction,
            "from": chain["from"],
            "to": chain["to"],
        },
        "question": question,
        "reference_answer": reference_answer,
        "correct_side": {
            "run": ok_run,
            "answer": str(ok_trace.get("answer", "")),
            "skill_ids": ok_trace.get("skill_ids") or [],
            "skill_trace": _skill_trace_summary(ok_trace),
            "visible_memories": ok_memories,
            "search_actions": _search_actions(ok_trace),
            "final_evidence_ids": ok_trace.get("final_evidence_ids") or [],
            "current_memories": _load_current_memories(
                ok_db, int(ok_trace.get("snapshot_commit_id") or 0)
            ),
            "construction_traces": ok_construction,
            "construction_skill_traces": _construction_skill_traces(
                ok_construction
            ),
        },
        "wrong_side": {
            "run": bad_run,
            "answer": str(bad_trace.get("answer", "")),
            "skill_ids": bad_trace.get("skill_ids") or [],
            "skill_trace": _skill_trace_summary(bad_trace),
            "visible_memories": bad_memories,
            "search_actions": _search_actions(bad_trace),
            "final_evidence_ids": bad_trace.get("final_evidence_ids") or [],
            "current_memories": _load_current_memories(
                bad_db, int(bad_trace.get("snapshot_commit_id") or 0)
            ),
            "construction_traces": bad_construction,
            "construction_skill_traces": _construction_skill_traces(
                bad_construction
            ),
            "standard_diagnosis_packages": standard_packages,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    parser.add_argument(
        "--flips", required=True,
        help="flip_diagnosis_input.json produced by the flip computation step.",
    )
    parser.add_argument("--diagnosis-root", default="outputs/flip_diag_prep")
    parser.add_argument("--output-root", default="outputs/flip_diag_packages")
    parser.add_argument(
        "--run-dir",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Runtime run root used by a contrastive pair (repeatable).",
    )
    parser.add_argument(
        "--diagnosis-dir",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help=(
            "Standard diagnosis root for a named runtime run (repeatable). "
            "The path must directly contain answer_failure/access_failure/"
            "cons_failure."
        ),
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-items", type=int, default=0)
    args = parser.parse_args()

    config = load_config(args.config)
    flips = json.loads(Path(args.flips).read_text(encoding="utf-8"))
    for value in args.run_dir:
        if "=" not in value:
            raise SystemExit("--run-dir must use NAME=PATH")
        name, path = value.split("=", 1)
        if not name or not path:
            raise SystemExit("--run-dir must use non-empty NAME=PATH")
        RUN_DIRS[name] = path
    for value in args.diagnosis_dir:
        if "=" not in value:
            raise SystemExit("--diagnosis-dir must use NAME=PATH")
        name, path = value.split("=", 1)
        if not name or not path:
            raise SystemExit("--diagnosis-dir must use non-empty NAME=PATH")
        DIAGNOSIS_DIRS[name] = path
    chain_defs = {
        "chain1": {
            "name": "chain1",
            "from": "bank_empty",
            "to": "v1_b",
            "correct_side": "swap_val_base",
            "wrong_side_c2w": "v1b_val_samebatch",
            "wrong_side_w2c": "swap_val_base",
        },
        "chain2": {
            "name": "chain2",
            "from": "v1_b",
            "to": "v2_b",
            "correct_side": "v1b_val_samebatch",
            "wrong_side_c2w": "v2b_val_dedup",
            "wrong_side_w2c": "v1b_val_samebatch",
        },
        "chain_fulliter": {
            "name": "chain_fulliter",
            "from": "v2_c",
            "to": "bank4",
            "correct_side": "v2c_val",
            "wrong_side_c2w": "b4_val",
            "wrong_side_w2c": "v2c_val",
        },
        "chain_rebuild": {
            "name": "chain_rebuild",
            "from": "baseline",
            "to": "bank1_rebuild",
            "correct_side": "swap_val_base",
            "wrong_side_c2w": "rebuild_val",
            "wrong_side_w2c": "swap_val_base",
        },
        "chain_train": {
            "name": "chain_train",
            "from": "v1_b",
            "to": "v2_c",
            "correct_side": "swap_train",
            "wrong_side_c2w": "v2c_full_iter",
            "wrong_side_w2c": "swap_train",
        },
        "chain_train_b1": {
            "name": "chain_train_b1",
            "from": "bank_empty",
            "to": "bank1_rebuild",
            "correct_side": "rebuild_train",
            "wrong_side_c2w": "bank1_train",
            "wrong_side_w2c": "rebuild_train",
        },
        "chain_train_b2": {
            "name": "chain_train_b2",
            "from": "bank1_rebuild",
            "to": "bank2_obj",
            "correct_side": "bank1_train",
            "wrong_side_c2w": "bank2_obj_train",
            "wrong_side_w2c": "bank1_train",
        },
    }

    # New experiments carry their chain metadata in the pair file.  Preserve
    # the historical aliases above, but do not require source edits for every
    # new Bank transition.
    for name, value in flips.items():
        if name == "schema_version" or not isinstance(value, dict):
            continue
        required = {"from", "to"}
        if required.issubset(value):
            chain_defs[name] = {
                "name": str(value.get("name") or name),
                "from": str(value["from"]),
                "to": str(value["to"]),
                "correct_side": str(value.get("correct_side") or ""),
                "wrong_side_c2w": str(value.get("wrong_side_c2w") or ""),
                "wrong_side_w2c": str(value.get("wrong_side_w2c") or ""),
            }

    # 需要 question/reference —— 从任一运行的 qa_results 或预测文件取
    question_by_id: dict[str, dict] = {}
    referenced_runs = {
        str(entry.get(field) or "")
        for name, data in flips.items()
        if name != "schema_version" and isinstance(data, dict)
        for direction in ("C2W", "W2C")
        for entry in data.get(direction, [])
        if isinstance(entry, dict)
        for field in ("ok_run", "wrong_run")
    }
    referenced_runs.discard("")
    for run in sorted(referenced_runs):
        if run not in RUN_DIRS:
            raise SystemExit(
                f"No directory configured for run {run!r}; pass --run-dir {run}=PATH"
            )
        run_root = Path(RUN_DIRS[run])
        conversation_ids = RUN_QUESTIONS.get(run) or tuple(
            path.parent.name for path in sorted(run_root.glob("conv-*/qa_results.jsonl"))
        )
        for conversation_id in conversation_ids:
            path = run_root / conversation_id / "qa_results.jsonl"
            for row in _load_jsonl(path):
                question_by_id.setdefault(
                    str(row["qa_id"]),
                    {
                        "question": row.get("question", ""),
                        "reference": row.get("reference", ""),
                    },
                )

    items: list[dict] = []
    for name, chain in chain_defs.items():
        data = flips.get(name)
        if data is None:
            continue
        for direction in ("C2W", "W2C"):
            bad_run_default = chain[f"wrong_side_{direction.lower()}"]
            for entry in data.get(direction, []):
                if isinstance(entry, dict):
                    qa_id = str(entry["qa_id"])
                    ok_run = str(entry.get("ok_run") or chain["correct_side"])
                    bad_run = str(entry.get("wrong_run") or bad_run_default)
                else:
                    qa_id = str(entry)
                    ok_run = chain["correct_side"]
                    bad_run = bad_run_default
                info = question_by_id.get(qa_id)
                items.append(
                    {
                        "chain": chain,
                        "direction": direction,
                        "qa_id": qa_id,
                        "conversation_id": _conv(qa_id),
                        "ok_run": ok_run,
                        "bad_run": bad_run,
                        "question": info["question"] if info else "",
                        "reference": info["reference"] if info else "",
                    }
                )
    items.sort(key=lambda item: (item["qa_id"], item["chain"]["name"]))
    if args.max_items > 0:
        items = items[: args.max_items]
    print(f"Flip cases: {len(items)}", flush=True)

    output_root = Path(args.output_root)
    maintenance = config.models["maintenance"]
    prompt_path = Path("prompts/diagnosis/flip_diagnosis.md")
    prompt = prompt_path.read_text(encoding="utf-8")
    pool_size = max(
        1,
        min(len(getattr(maintenance, "api_keys", []) or [maintenance]) * 2, args.workers),
    )

    summary = {"ok": 0, "no_problem": 0, "error": 0, "rows": []}

    def diagnose_one(item: dict) -> dict:
        model_config = copy.deepcopy(maintenance)
        model_config.supports_json_mode = True
        model_config.extra_body = {"thinking": {"type": "disabled"}}
        model_config.reasoning_effort = None
        agent = FlipDiagnosisAgent(
            create_client(model_config), prompt=prompt
        )
        try:
            case = build_flip_case(
                chain=item["chain"],
                direction=item["direction"],
                qa_id=item["qa_id"],
                conversation_id=item["conversation_id"],
                ok_run=item["ok_run"],
                bad_run=item["bad_run"],
                question=item["question"],
                reference_answer=item["reference"],
                diagnosis_root=Path(args.diagnosis_root),
            )
        except Exception as exc:
            return {"status": "error", "qa_id": item["qa_id"],
                    "error": f"build: {exc}"}
        try:
            report = agent.diagnose(case)
        except Exception as exc:
            return {"status": "error", "qa_id": item["qa_id"],
                    "error": f"diagnose: {str(exc)[:300]}"}
        if not report["problem_found"]:
            return {"status": "no_problem", "qa_id": item["qa_id"],
                    "reason": report["reason"]}
        written: list[dict] = []
        for projection in report["projections"]:
            side = projection["side"]
            stage = projection["stage"]
            component_dir = (
                "access_failure" if side == "access" else "cons_failure"
            )
            package_path = (
                output_root / component_dir / "packages"
                / item["conversation_id"]
                / f"{item['qa_id']}_flip_{item['chain']['name']}_{stage}.json"
            )
            package_path.parent.mkdir(parents=True, exist_ok=True)
            package_path.write_text(
                json.dumps(projection, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            written.append({"side": side, "stage": stage, "path": str(package_path)})
        core_path = (
            output_root / "contrastive_core" / item["conversation_id"]
            / f"{item['qa_id']}_flip_{item['chain']['name']}.json"
        )
        core_path.parent.mkdir(parents=True, exist_ok=True)
        core_path.write_text(
            json.dumps(report["core"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {"status": "ok", "qa_id": item["qa_id"],
                "side": ",".join(row["stage"] for row in written),
                "projections": written, "core_path": str(core_path)}

    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        futures = {pool.submit(diagnose_one, item): item for item in items}
        for future in as_completed(futures):
            outcome = future.result()
            summary[outcome["status"]] += 1
            summary["rows"].append(outcome)
            print(
                f"[{outcome['status']:10s}] {outcome.get('qa_id', '?')} "
                f"{outcome.get('side', '')}",
                flush=True,
            )

    summary_path = output_root / "flip_diagnosis_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"\nFlip diagnosis: ok={summary['ok']} no_problem={summary['no_problem']} "
        f"error={summary['error']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
