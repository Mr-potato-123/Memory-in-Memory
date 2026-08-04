"""LoCoMo 1-5 rating Judge — strong-model pointwise scoring.

Same infrastructure as judge_predictions.py (batch, workers, resume,
temporal context) but emits a 1-5 score per question instead of a C/P/I
label. Optional C/P/I mapping: score>=4 -> C, 3 -> P, <=2 -> I.

Usage:
  python scripts/judge_ratings.py --config configs/qwen3_8b_dashscope.yaml \
      --output-dir outputs/rating_judge/full --workers 12 \
      outputs/bank1_draft_crud_v2_eval/conv-26/locomo_predictions.jsonl ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.config import ModelConfig, load_config
from mim.eval.locomo import load_dataset
from mim.llm import create_client

JUDGE_PROMPT_PATH = "prompts/judge/locomo_rating_judge.md"
JUDGE_PROMPT_VERSION = "locomo_rating_judge_v1"
VALID_SCORES = {1, 2, 3, 4, 5}
BATCH_SIZE = 4
MAX_RETRIES = 3


# ── Temporal context (same as judge_predictions.py) ─────────────

def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "unknown"
    return str(ts).strip()


def build_temporal_context(conversations, questions_map, cid, qa_id):
    conv = None
    for c in conversations:
        if c.conversation_id == cid:
            conv = c
            break
    all_timestamps = []
    if conv:
        for s in conv.sessions:
            if s.time:
                all_timestamps.append(_fmt_ts(s.time))
    conv_start = min(all_timestamps) if all_timestamps else "unknown"
    conv_end = max(all_timestamps) if all_timestamps else "unknown"
    evidence_ts = []
    question = None
    for q in questions_map.get(cid, []):
        if q.qa_id == qa_id:
            question = q
            break
    if question and conv:
        msg_ts = {}
        for s in conv.sessions:
            for m in s.messages:
                msg_ts[m.message_id] = _fmt_ts(m.time or s.time)
        for item in question.source_evidence:
            if item and len(item) >= 2 and item[-1]:
                evidence_ts.append(
                    {"message_id": item[-1],
                     "timestamp": msg_ts.get(item[-1], "unknown")})
    return {
        "policy": ("Use only the fictional conversation timeline. "
                   "Never use the current real-world date."),
        "conversation_start": conv_start,
        "conversation_end": conv_end,
        "evidence_timestamps": evidence_ts,
    }


# ── Helpers ─────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_jsonl(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


# ── Judge ───────────────────────────────────────────────────────

def build_client(config_path: str, judge_model: str):
    config = load_config(config_path)
    maintenance = config.models["maintenance"]
    values = maintenance.model_dump()
    values["model"] = judge_model
    values["temperature"] = 0.0
    values["max_tokens"] = 3000
    values["supports_json_mode"] = True
    values["extra_body"] = {}
    values["reasoning_effort"] = None
    values["reject_reasoning_output"] = False
    return create_client(ModelConfig(**values)), config


def judge_batch(client, prompt, batch, temporal_ctx, retries=MAX_RETRIES):
    payload_items = []
    for item in batch:
        tc = temporal_ctx.get(item["qa_id"], {})
        payload_items.append({
            "qa_id": item["qa_id"],
            "category": item["category"],
            "question": item["question"],
            "reference": item["reference"],
            "prediction": item["prediction"],
            "temporal_context": tc,
        })
    payload_json = json.dumps({"items": payload_items}, ensure_ascii=False)
    expected_ids = [item["qa_id"] for item in batch]

    last_error = None
    for attempt in range(retries):
        try:
            resp = client.generate(
                [{"role": "system", "content": prompt},
                 {"role": "user", "content": payload_json}],
                temperature=0.0,
                max_tokens=3000,
                json_mode=True,
            )
            text = resp.text
            if not text or not text.strip():
                raise ValueError("Empty response content")
            body = json.loads(text)
            judgments = body.get("judgments")
            if not isinstance(judgments, list) or len(judgments) != len(batch):
                raise ValueError(
                    f"judgments count mismatch: {len(judgments) if isinstance(judgments, list) else 'not_list'} != {len(batch)}")
            actual_ids = [str(j.get("qa_id", "")) for j in judgments]
            if actual_ids != expected_ids:
                raise ValueError(f"QA ID order mismatch: {actual_ids} != {expected_ids}")
            results = []
            for j in judgments:
                score = int(j.get("score", 0))
                if score not in VALID_SCORES:
                    raise ValueError(f"Invalid score: {score!r}")
                reason = str(j.get("reason", "")).strip()
                if not reason:
                    raise ValueError("Empty reason")
                results.append({"score": score, "reason": reason})
            return results
        except Exception as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 10))

    if len(batch) > 1:
        print(f"  Batch failed after {retries} retries; "
              f"falling back to single-item. Error: {last_error}", flush=True)
        results = []
        for item in batch:
            try:
                results.extend(judge_batch(client, prompt, [item],
                                           temporal_ctx, retries=retries))
            except Exception as exc:
                results.append({"score": None,
                                "reason": f"FAILED: {str(exc)[:100]}"})
        return results
    raise RuntimeError(f"Judge failed after {retries} retries: {last_error}")


# ── Main ────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    p.add_argument("--judge-model", default="deepseek-v4-flash")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--resume", action="store_true")
    p.add_argument("inputs", nargs="*")
    args = p.parse_args()

    client, config = build_client(args.config, args.judge_model)
    prompt_path = Path(JUDGE_PROMPT_PATH)
    if not prompt_path.exists():
        print(f"ERROR: prompt not found: {prompt_path}")
        return 2
    prompt = prompt_path.read_text(encoding="utf-8")
    prompt_hash = _sha256_file(prompt_path)

    out_dir = Path(args.output_dir)
    if args.resume:
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        if out_dir.exists():
            print(f"ERROR: {out_dir} exists. Use --resume or new --output-dir.")
            return 2
        out_dir.mkdir(parents=True, exist_ok=False)

    judgments_path = out_dir / "judgments.jsonl"
    errors_path = out_dir / "errors.jsonl"
    summary_path = out_dir / "summary.json"

    pred_files = [Path(f) for f in args.inputs]
    if not pred_files:
        print("ERROR: no input files")
        return 2
    all_rows = []
    for pf in pred_files:
        if not pf.exists():
            print(f"ERROR: {pf} not found")
            return 2
        rows = _load_jsonl(pf)
        normalized = []
        for row in rows:
            r = dict(row)
            if "reference" not in r and "answer" in r:
                r["reference"] = r["answer"]
            r.setdefault("evidence_ids", [])
            normalized.append(r)
        all_rows.extend(normalized)
        print(f"  {pf}: {len(rows)} rows")

    conversations, questions_map = load_dataset(config.dataset.path)
    temporal_ctx = {
        row["qa_id"]: build_temporal_context(
            conversations, questions_map, row["conversation_id"], row["qa_id"])
        for row in all_rows
    }

    completed = {}
    if args.resume and judgments_path.exists():
        for row in _load_jsonl(judgments_path):
            if row.get("qa_id") and row.get("score") in VALID_SCORES:
                completed[row["qa_id"]] = row
    pending = [r for r in all_rows if r["qa_id"] not in completed]

    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    write_lock = threading.Lock()
    permanent_errors = [0]
    done_count = [0]

    def process_batch(batch, _idx):
        try:
            results = judge_batch(client, prompt, batch, temporal_ctx)
        except Exception as exc:
            with write_lock:
                for item in batch:
                    _append_jsonl(errors_path, {
                        "qa_id": item["qa_id"], "error": str(exc)[:200],
                        "timestamp": _ts()})
                permanent_errors[0] += len(batch)
            return
        with write_lock:
            for item, result in zip(batch, results):
                if result.get("score") is None:
                    _append_jsonl(errors_path, {
                        "qa_id": item["qa_id"],
                        "error": result.get("reason", "unknown")[:200],
                        "timestamp": _ts()})
                    permanent_errors[0] += 1
                    continue
                tc = temporal_ctx.get(item["qa_id"], {})
                judgment = {
                    "conversation_id": item["conversation_id"],
                    "qa_id": item["qa_id"],
                    "category": item["category"],
                    "score": result["score"],
                    "label": "C" if result["score"] >= 4
                    else ("P" if result["score"] == 3 else "I"),
                    "reason": result["reason"],
                    "judge_model": args.judge_model,
                    "judge_prompt_version": JUDGE_PROMPT_VERSION,
                    "temporal_context": tc,
                }
                _append_jsonl(judgments_path, judgment)
                done_count[0] += 1
        if done_count[0] % 100 == 0:
            print(f"  rated={done_count[0]}/{len(pending)} "
                  f"errors={permanent_errors[0]}", flush=True)

    batches = [pending[i:i + args.batch_size]
               for i in range(0, len(pending), args.batch_size)]
    print(f"Processing {len(batches)} batches with {args.workers} workers...",
          flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_batch, b, i): i
                   for i, b in enumerate(batches)}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                print(f"  Worker error: {exc}", flush=True)

    all_judgments = _load_jsonl(judgments_path)
    scores = Counter(j["score"] for j in all_judgments)
    labels = Counter(j["label"] for j in all_judgments)
    mean = (sum(j["score"] for j in all_judgments) / len(all_judgments)
            if all_judgments else 0.0)
    by_cat = defaultdict(list)
    for j in all_judgments:
        by_cat[j.get("category")].append(j["score"])
    summary = {
        "judge_model": args.judge_model,
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "total": len(all_judgments),
        "mean_score": round(mean, 3),
        "score_distribution": dict(sorted(scores.items())),
        "labels_from_scores": dict(labels),
        "by_category_mean": {
            str(cat): round(sum(v) / len(v), 3)
            for cat, v in sorted(by_cat.items())
        },
        "permanent_errors": permanent_errors[0],
        "prompt_sha256": prompt_hash,
    }
    _write_json(summary_path, summary)
    print(f"\nMean score: {mean:.3f}")
    print(f"Score distribution: {dict(sorted(scores.items()))}")
    print(f"Labels (>=4 C, 3 P, <=2 I): {dict(labels)}")
    print(f"Per-category mean: {summary['by_category_mean']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
