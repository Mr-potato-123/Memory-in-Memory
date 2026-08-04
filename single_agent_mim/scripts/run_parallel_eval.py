"""Run evaluation with parallel conversation construction and answering."""
from __future__ import annotations
import json, sys, tempfile, shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.artifacts import RunDir
from mim.config import load_config
from mim.eval.locomo import load_dataset
from mim.eval.metrics import compute_f1
from mim.schemas import EvalReport, QAResult
from mim.skills import SkillBank
from mim.workflows.use import MiMRuntime
from mim.llm import create_client


def eval_one_conversation(
    config,
    conversation,
    questions,
    mode,
    bank,
    run_dir,
    embedder,
    runtime_model,
    split_name,
):
    """Evaluate one conversation in its own runtime."""
    runtime = MiMRuntime(
        config=config,
        mode=mode,
        skill_bank=bank,
        run_dir=run_dir,
        runtime_model=runtime_model,
        embedder=embedder,
        phase=split_name,
    )
    runtime.ingest(conversation)
    results = []
    for question in questions:
        access = runtime.ask(question)
        f1 = (
            compute_f1(access.answer, question.reference_answer, question.category)
            if not access.error else 0.0
        )
        results.append(QAResult(
            conversation_id=conversation.conversation_id,
            qa_id=question.qa_id,
            category=question.category,
            question=question.question,
            reference=question.reference_answer,
            prediction=access.answer,
            evidence_ids=access.evidence_ids,
            skill_ids=access.used_skill_ids,
            f1=float(f1),
            runtime_tokens=access.total_tokens,
            access_steps=access.steps,
            error=access.error,
        ))
    return results, runtime.last_construction_steps


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    p.add_argument("--split-name", default="validation")
    p.add_argument("--mode", default="mim")
    p.add_argument("--skill-bank-dir")
    p.add_argument("--run-id", required=True)
    args = p.parse_args()

    config = load_config(args.config)
    output_dir = config.output_dir
    run_dir = RunDir.create(args.run_id, output_dir)
    run_dir.write_yaml("config.resolved.yaml", config.to_resolved_dict())

    conversations, questions_map = load_dataset(config.dataset.path)
    split_path = config.dataset.split
    with open(split_path, encoding="utf-8") as f:
        split_data = json.load(f)
    eval_ids = set(split_data.get(args.split_name, []))

    # Load or create runtime model
    runtime_model = create_client(config.models["runtime"])
    from mim.retrieval.embedder import Embedder
    embedder = Embedder(model_name=config.embedding.model, device=config.embedding.device)

    # Load bank
    bank = None
    if args.mode == "mim":
        bank = SkillBank.load_published(args.skill_bank_dir)
        bank.freeze()

    # Filter conversations
    eval_convs = [
        c for c in conversations if c.conversation_id in eval_ids
    ]
    print(f"Parallel evaluating {len(eval_convs)} conversations: "
          f"{[c.conversation_id for c in eval_convs]}")

    all_results = []
    total_construction_steps = 0

    # Process conversations in parallel using ThreadPoolExecutor
    # Each conversation gets its own MiMRuntime with a temp DB
    with ThreadPoolExecutor(max_workers=len(eval_convs)) as executor:
        futures = {}
        for conv in eval_convs:
            # Each conversation uses a temp subdirectory for its runtime
            conv_run_id = f"{args.run_id}_{conv.conversation_id}"
            conv_run_dir = RunDir.create(conv_run_id, output_dir)
            conv_run_dir.write_yaml("config.resolved.yaml", config.to_resolved_dict())

            future = executor.submit(
                eval_one_conversation,
                config,
                conv,
                questions_map.get(conv.conversation_id, []),
                args.mode,
                bank,
                conv_run_dir,
                embedder,
                runtime_model,
                args.split_name,
            )
            futures[future] = conv.conversation_id

        for future in as_completed(futures):
            cid = futures[future]
            try:
                results, steps = future.result()
                all_results.extend(results)
                total_construction_steps += steps
                print(f"[{cid}] Done: {len(results)} questions, "
                      f"{steps} construction steps")
            except Exception as exc:
                print(f"[{cid}] FAILED: {exc}")
                raise

    # Write combined results
    for result in all_results:
        run_dir.append_jsonl("qa_results.jsonl", result.model_dump(mode="json"))

    # Write summary
    report = EvalReport(
        run_id=args.run_id,
        mode=args.mode,
        split_name=args.split_name,
        output_dir=str(run_dir.path),
    )
    report.total_qa = len(all_results)
    report.protocol_errors = sum(1 for r in all_results if r.error)
    report.total_runtime_tokens = sum(r.runtime_tokens for r in all_results)
    report.overall_f1 = (
        sum(r.f1 for r in all_results) / len(all_results) if all_results else 0.0
    )
    report.avg_access_steps = (
        sum(r.access_steps for r in all_results) / len(all_results)
        if all_results else 0.0
    )
    report.avg_construction_steps = (
        total_construction_steps / len(eval_convs)
        if eval_convs else 0.0
    )
    by_category = defaultdict(list)
    for r in all_results:
        if r.category is not None:
            by_category[r.category].append(r.f1)
    report.category_f1 = {
        cat: sum(scores) / len(scores)
        for cat, scores in by_category.items()
    }
    run_dir.write_json("summary.json", report.model_dump(mode="json"))

    print(f"\n=== Results ({args.mode} / {args.split_name}) ===")
    print(f"Overall F1: {report.overall_f1:.4f}")
    print(f"Total QA: {report.total_qa}")
    print(f"Protocol errors: {report.protocol_errors}")
    if report.category_f1:
        for cat, f1 in sorted(report.category_f1.items()):
            print(f"  Category {cat}: {f1:.4f}")
    print(f"Output: {report.output_dir}")


if __name__ == "__main__":
    main()
