"""Fast read-only access evaluation over already-built conversation snapshots."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.artifacts import RunDir
from mim.config import load_config
from mim.eval.locomo import load_dataset
from mim.eval.metrics import compute_f1
from mim.llm import create_client
from mim.retrieval.embedder import Embedder
from mim.skills import SkillBank
from mim.workflows.use import MiMRuntime


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--output-run", required=True)
    p.add_argument("--sources", nargs="+", required=True,
                   help="source_run:conversation_id pairs")
    p.add_argument("--skill-bank-dir")
    p.add_argument("--qa-workers", type=int, default=4)
    args = p.parse_args()

    cfg = load_config(args.config)
    root = Path(args.output_run)
    root.mkdir(parents=True, exist_ok=True)
    _, questions = load_dataset(cfg.dataset.path)
    model = create_client(cfg.models["runtime"])
    embedder = Embedder(cfg.embedding.model, cfg.embedding.device,
                        cfg.embedding.normalize, cfg.embedding.batch_size)
    bank = None
    mode = "base"
    if args.skill_bank_dir:
        bank = SkillBank.load_published(args.skill_bank_dir)
        bank.freeze()
        mode = "mim"

    runtimes = []
    for spec in args.sources:
        source_run, cid = spec.split(":", 1)
        dst = root / cid
        (dst / "state").mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path("outputs") / source_run / "state" / "memory.sqlite3",
                     dst / "state" / "memory.sqlite3")
        rd = RunDir(f"{root.name}_{cid}", root.parent)
        rd.path = dst
        runtime = MiMRuntime(
            cfg, mode=mode, skill_bank=bank, run_dir=rd,
            runtime_model=model, embedder=embedder, phase="eval_answer_only",
            strict_construction=True, persist_access=False,
        )
        runtime.attach(cid)
        runtimes.append((cid, runtime, questions[cid]))

    def ask(item):
        cid, runtime, q = item
        access = runtime.ask(q)
        return {
            "conversation_id": cid, "qa_id": q.qa_id,
            "category": q.category, "question": q.question,
            "reference": q.reference_answer, "prediction": access.answer,
            "evidence_ids": access.evidence_ids,
            "skill_ids": access.used_skill_ids,
            "f1": float(compute_f1(access.answer, q.reference_answer, q.category)
                       if not access.error else 0.0),
            "runtime_tokens": access.total_tokens,
            "access_steps": access.steps,
            "error": access.error or "",
        }

    items = [(cid, runtime, q) for cid, runtime, qs in runtimes for q in qs]
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, args.qa_workers)) as ex:
        futures = [ex.submit(ask, item) for item in items]
        for f in as_completed(futures):
            rows.append(f.result())
    rows.sort(key=lambda r: (r["conversation_id"], r["qa_id"]))
    (root / "qa_results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    groups = defaultdict(list)
    for row in rows:
        groups[str(row["category"])].append(row["f1"])
    summary = {
        "mode": mode, "total_qa": len(rows),
        "overall_f1": sum(row["f1"] for row in rows) / len(rows) if rows else 0.0,
        "category_f1": {k: sum(v) / len(v) for k, v in groups.items()},
        "protocol_errors": sum(bool(row["error"]) for row in rows),
    }
    (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
