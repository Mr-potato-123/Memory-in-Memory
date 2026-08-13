"""MiM — Memory in Memory: Single CLI entry point.

Commands:
  python main.py use       --conversation <path> --question "..." [--mode base|mim]
  python main.py train     --config <path> [--run-id <id>]
  python main.py evaluate  --config <path> --split-name test --mode base|mim [--run-id <id>]
  python main.py smoke     (runs smoke test with MockClient, no API key needed)

All business logic lives in src/mim/ — this file is a thin dispatcher.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from mim.config import load_config
from mim.schemas import (
    Conversation,
    DatasetSplit,
    Message,
    Question,
    Session,
)
from mim.artifacts import RunDir
from mim.eval.locomo import load_dataset, apply_split
from mim.workflows.use import MiMRuntime
from mim.workflows.train import MiMTrainer
from mim.workflows.evaluate import MiMEvaluator
from mim.skills import SkillBank


# ── CLI parser ───────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Memory in Memory (MiM) — Error-driven meta-memory layer",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # use
    u = sub.add_parser("use", help="Ingest a conversation and answer a question")
    u.add_argument("--conversation", required=True, help="Path to conversation JSON")
    u.add_argument("--question", required=True, help="Question text")
    u.add_argument("--mode", default="mim", choices=["base", "mim"])
    u.add_argument(
        "--skill-bank-dir",
        default=None,
        help="Directory containing the two physically isolated Bank1 files",
    )
    u.add_argument("--config", default="configs/default.yaml")
    u.add_argument("--run-id", default=None)

    # train
    t = sub.add_parser("train", help="Learn Skills from training failures")
    t.add_argument("--config", default="configs/default.yaml")
    t.add_argument("--dataset", default=None, help="Override dataset path")
    t.add_argument("--split", default=None, help="Override split file path")
    t.add_argument("--run-id", required=True)
    t.add_argument("--initial-skill-bank", default=None)

    # evaluate
    e = sub.add_parser("evaluate", help="Frozen evaluation on test/validation split")
    e.add_argument("--config", default="configs/default.yaml")
    e.add_argument("--split-name", required=True, choices=["validation", "test"])
    e.add_argument("--mode", required=True, choices=["base", "mim"])
    e.add_argument(
        "--skill-bank-dir",
        default=None,
        help="Directory containing the two physically isolated Bank1 files",
    )
    e.add_argument("--run-id", required=True)

    # smoke
    sm = sub.add_parser("smoke", help="Run smoke test with MockClient (no API key needed)")
    sm.add_argument("--config", default="configs/default.yaml")
    sm.add_argument("--run-id", default=None)

    return p


# ── Command handlers ─────────────────────────────────────────────

def _cmd_use(args):
    config = load_config(args.config)
    if args.run_id:
        run_id = args.run_id
    else:
        from datetime import datetime
        run_id = f"use_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    run_dir = RunDir.create(run_id, config.output_dir)
    run_dir.write_yaml("config.resolved.yaml", config.to_resolved_dict())

    # Load conversation
    conv = _load_single_conversation(args.conversation)
    if conv is None:
        print(f"ERROR: Could not load conversation from {args.conversation}")
        sys.exit(1)

    # Load skill bank if MiM mode
    bank = None
    if args.mode == "mim":
        if not args.skill_bank_dir:
            raise ValueError("MiM mode requires --skill-bank-dir.")
        bank = SkillBank.load_published(args.skill_bank_dir)

    # Build runtime
    runtime = MiMRuntime(
        config=config,
        mode=args.mode,
        skill_bank=bank,
        run_dir=run_dir,
    )

    # Ingest
    print(f"Ingesting conversation: {conv.conversation_id}")
    runtime.ingest(conv)

    # Ask
    q = Question(question=args.question, reference_answer="")
    result = runtime.ask(q)

    # Print result
    output = {
        "answer": result.answer,
        "evidence_ids": result.evidence_ids,
        "construction_skill_ids": [],
        "access_skill_ids": result.used_skill_ids,
        "trace_path": str(run_dir.path / "traces" / "access_traces.jsonl"),
        "memory_database": str(runtime.store.database_path),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))

    run_dir.update_manifest(phase="use", mode=args.mode)
    run_dir.save_manifest()

    return output


def _cmd_train(args):
    config = load_config(args.config)
    if args.dataset:
        config.dataset.path = args.dataset
    if args.split:
        config.dataset.split = args.split

    run_dir = RunDir.create(args.run_id, config.output_dir)

    # Save resolved config
    run_dir.write_yaml("config.resolved.yaml", config.to_resolved_dict())
    run_dir.update_manifest(
        phase="train",
        mode="mim",
        config_hash=config.config_hash(),
    )

    # Load dataset
    print(f"Loading dataset: {config.dataset.path}")
    conversations, questions = load_dataset(config.dataset.path)

    # Load split
    split = _load_split(config.dataset.split)

    print(f"Train: {len(split.train)} conversations")
    print(f"Validation: {len(split.validation)} conversations")
    print(f"Test: {len(split.test)} conversations")

    run_dir.update_manifest(
        dataset_hash=split.dataset_sha256,
        train_ids=split.train,
        validation_ids=split.validation,
    )

    # Train
    trainer = MiMTrainer(config, run_dir)
    result = trainer.train(
        conversations=conversations,
        questions=questions,
        train_ids=split.train,
        validation_ids=split.validation,
        initial_skill_bank=args.initial_skill_bank,
    )

    # Summary
    print(f"\n=== Training Complete ===")
    print(f"Conversations processed: {result.conversations_processed}")
    print(f"Total QA: {result.total_qa}")
    print(f"Failures detected: {result.failures_detected}")
    print(f"  Construction: {result.construction_failures}")
    print(f"  Access: {result.access_failures}")
    print(f"  Other: {result.other_failures}")
    print(f"  Invalid: {result.invalid_failures}")
    print(f"Candidates: {result.candidates_generated} generated, "
          f"{result.candidates_accepted} accepted, {result.candidates_rejected} rejected")
    print(f"Bank versions: {result.bank_versions}")
    print(f"Selected version: {result.selected_version} "
          f"(validation F1={result.validation_best_f1:.4f})")
    print(f"Output: {result.output_dir}")

    run_dir.save_manifest()


def _cmd_evaluate(args):
    config = load_config(args.config)
    run_dir = RunDir.create(args.run_id, config.output_dir)

    run_dir.write_yaml("config.resolved.yaml", config.to_resolved_dict())
    run_dir.update_manifest(
        phase="evaluate",
        mode=args.mode,
        split_name=args.split_name,
    )

    # Load dataset
    print(f"Loading dataset: {config.dataset.path}")
    conversations, questions = load_dataset(config.dataset.path)

    # Load split
    split = _load_split(config.dataset.split)
    eval_ids = split.test if args.split_name == "test" else split.validation
    print(f"Evaluating {len(eval_ids)} conversations in {args.split_name} split (mode={args.mode})")

    # Evaluate
    evaluator = MiMEvaluator(config, run_dir)
    report = evaluator.evaluate(
        conversations=conversations,
        questions=questions,
        eval_ids=eval_ids,
        mode=args.mode,
        skill_bank_dir=args.skill_bank_dir,
        split_name=args.split_name,
    )

    # Summary
    print(f"\n=== Evaluation Results ({args.mode} / {args.split_name}) ===")
    print(f"Overall F1: {report.overall_f1:.4f}")
    print(f"Total QA: {report.total_qa}")
    print(f"Protocol errors: {report.protocol_errors}")
    print(f"Total runtime tokens: {report.total_runtime_tokens}")
    print(f"Avg construction steps: {report.avg_construction_steps:.1f}")
    print(f"Avg access steps: {report.avg_access_steps:.1f}")
    if report.category_f1:
        print("Category F1:")
        for cat, f1 in sorted(report.category_f1.items()):
            print(f"  Category {cat}: {f1:.4f}")
    print(f"Output: {report.output_dir}")

    run_dir.save_manifest()


def _cmd_smoke(args):
    """Run the real SQLite Runtime with deterministic mock model outputs."""
    print("=== MiM SQLite Smoke Test ===")
    from datetime import datetime
    from mim.config import ModelConfig
    from mim.llm.mock_client import MockClient
    from mim.retrieval.embedder import Embedder

    config = load_config(args.config)
    config.embedding.model = "deterministic-hash"
    config.models["runtime"] = ModelConfig(
        provider="mock",
        model="mock-runtime",
        temperature=0.0,
        max_tokens=1200,
    )
    runtime_model = MockClient(config.models["runtime"])

    candidate_id = "cand_test_conv_0_session_01_000"
    memory_version_id = "mem_test_conv_0_0001_v1"
    runtime_model.set_script([
        runtime_model._make_resp(json.dumps({
            "candidates": [{
                "candidate_id": "local_1",
                "memory_kind": "state",
                "subject": "Alice",
                "predicate": "residence",
                "object_text": "Seattle",
                "content": "Alice lives in Seattle.",
                "world_start": "2023-05",
                "world_end": None,
                "source_message_ids": ["msg_1"],
                "entities": ["Alice", "Seattle"],
                "keywords": ["Alice", "Seattle", "residence"],
                "importance": 0.8,
                "confidence": 0.95,
            }]
        })),
        runtime_model._make_resp(json.dumps({
            "decisions": [{
                "candidate_id": candidate_id,
                "action": "ADD",
                "target_memory_id": None,
                "update_type": "add",
                "reason": "New durable residence state.",
                "merged_content": "Alice lives in Seattle.",
                "world_start": "2023-05",
                "world_end": None,
                "source_message_ids": ["msg_1"],
            }]
        })),
        runtime_model._make_resp(json.dumps({
            "additional_queries": [],
            "keywords": [],
            "entities": [],
            "include_history": False,
            "time_mode": "current",
            "target_time": None,
            "target_time_end": None,
            "evidence_requirements": ["Alice's current residence"],
            "applied_skill_ids": [],
        })),
        runtime_model._make_resp(json.dumps({
            "answer": "Seattle",
            "selected_evidence_ids": [memory_version_id],
            "coverage": [{
                "requirement": "Alice's current residence",
                "evidence_version_ids": [memory_version_id],
            }],
            "applied_skill_ids": [],
        })),
    ])

    conversation = Conversation(
        conversation_id="test_conv_0",
        sessions=[Session(
            session_id="session_01",
            time="2023-05",
            messages=[
                Message(
                    message_id="msg_1",
                    role="user",
                    speaker="Alice",
                    content="I just moved to Seattle.",
                    time="2023-05",
                ),
                Message(
                    message_id="msg_2",
                    role="assistant",
                    speaker="Assistant",
                    content="Congratulations!",
                    time="2023-05",
                ),
            ],
        )],
    )
    question = Question(
        qa_id="qa_test_0",
        question="Where does Alice live now?",
        reference_answer="Seattle",
        source_evidence=[["session_01", "msg_1"]],
    )

    run_id = args.run_id or (
        f"smoke_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    )
    run_dir = RunDir.create(run_id, config.output_dir)
    runtime = MiMRuntime(
        config=config,
        mode="base",
        run_dir=run_dir,
        embedder=Embedder("deterministic-hash"),
        runtime_model=runtime_model,
        phase="smoke",
    )
    runtime.ingest(conversation)
    result = runtime.ask(question)
    if result.error:
        raise RuntimeError(result.error)
    assert result.answer == "Seattle"
    assert result.evidence_ids == [memory_version_id]

    connection = runtime.store.open_read_connection()
    try:
        counts = {
            "messages": connection.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0],
            "candidates": connection.execute(
                "SELECT COUNT(*) FROM memory_candidates"
            ).fetchone()[0],
            "decisions": connection.execute(
                "SELECT COUNT(*) FROM construction_decisions"
            ).fetchone()[0],
            "versions": connection.execute(
                "SELECT COUNT(*) FROM memory_versions"
            ).fetchone()[0],
            "answer_context": connection.execute(
                "SELECT COUNT(*) FROM access_answer_context"
            ).fetchone()[0],
        }
    finally:
        connection.close()
    assert all(value > 0 for value in counts.values()), counts

    run_dir.update_manifest(
        phase="smoke",
        mode="base",
        status="passed",
        sqlite_counts=counts,
    )
    run_dir.save_manifest()
    print(json.dumps({
        "status": "passed",
        "answer": result.answer,
        "evidence_ids": result.evidence_ids,
        "sqlite_counts": counts,
        "output": str(run_dir.path),
    }, ensure_ascii=False, indent=2))


# ── Helpers ──────────────────────────────────────────────────────

def _load_single_conversation(path: str) -> Conversation | None:
    """Load a single conversation from a JSON file (demo format)."""
    p = Path(path)
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        if not data or not isinstance(data[0], dict):
            return None
        data = data[0]
    if not isinstance(data, dict):
        return None

    # Handle demo format: {id, sessions: [...], qas: [...]} or array of sessions
    sessions_data = data.get("sessions", [data])
    sessions: list[Session] = []
    for s_data in sessions_data if isinstance(sessions_data, list) else [sessions_data]:
        msgs_data = s_data.get("messages", s_data.get("content", []))
        if isinstance(msgs_data, list):
            messages = [
                Message(
                    message_id=m.get("message_id", f"msg_{i}"),
                    role=m.get("role", "user"),
                    speaker=m.get("speaker"),
                    content=str(m.get("content", "")),
                    time=m.get("time"),
                )
                for i, m in enumerate(msgs_data)
            ]
            sessions.append(Session(
                session_id=s_data.get("session_id", f"sess_{len(sessions)}"),
                messages=messages,
                time=s_data.get("time"),
            ))

    return Conversation(
        conversation_id=data.get("conversation_id", data.get("id", Path(path).stem)),
        sessions=sessions,
    )


def _load_split(path: str) -> DatasetSplit:
    """Load a split file or generate a default one."""
    p = Path(path)
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return DatasetSplit(**json.load(f))
    # Fallback: return empty split
    return DatasetSplit(dataset_sha256="unknown", seed=42)


# ── Main ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "use":
        _cmd_use(args)
    elif args.command == "train":
        _cmd_train(args)
    elif args.command == "evaluate":
        _cmd_evaluate(args)
    elif args.command == "smoke":
        _cmd_smoke(args)
    else:
        parser.print_help()
