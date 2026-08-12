"""Strict semantic binary Judge — CORRECT/WRONG.

Uses DeepSeek V4 Flash through the maintenance API configuration. Evaluates
every prediction with C/P/I labels. Never uses Token-F1 or the current date.
"""

from __future__ import annotations

import argparse
import copy
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

JUDGE_PROMPT_PATH = "prompts/judge/locomo_binary_judge.md"
JUDGE_PROMPT_VERSION = "locomo_binary_judge_v1"
VALID_LABELS = {"C", "W"}
# Canonical scoring keeps one independent QA per request. Concurrency is
# supplied by workers, not by mixing unrelated decisions in one context.
BATCH_SIZE = 1
MAX_RETRIES = 3


# ── Temporal context ────────────────────────────────────────────────

def _fmt_ts(ts: str | None) -> str:
    """Normalise a dataset timestamp string."""
    if not ts:
        return "unknown"
    return str(ts).strip()


def build_temporal_context(
    conversations: list,
    questions_map: dict[str, Any],
    cid: str,
    qa_id: str,
) -> dict[str, Any]:
    """Return conversation-level temporal anchors and evidence timestamps."""
    conv = None
    for c in conversations:
        if c.conversation_id == cid:
            conv = c
            break

    all_timestamps: list[str] = []
    if conv:
        for s in conv.sessions:
            if s.time:
                all_timestamps.append(_fmt_ts(s.time))

    conv_start = min(all_timestamps) if all_timestamps else "unknown"
    conv_end = max(all_timestamps) if all_timestamps else "unknown"

    # Evidence timestamps
    evidence_ts: list[dict[str, str]] = []
    question = None
    qas = questions_map.get(cid, [])
    for q in qas:
        if q.qa_id == qa_id:
            question = q
            break

    if question and conv:
        # Build message_id -> timestamp map
        msg_ts: dict[str, str] = {}
        for s in conv.sessions:
            for m in s.messages:
                msg_ts[m.message_id] = _fmt_ts(m.time or s.time)

        for item in question.source_evidence:
            if item and len(item) >= 2 and item[-1]:
                mid = item[-1]
                ts = msg_ts.get(mid, "unknown")
                evidence_ts.append({"message_id": mid, "timestamp": ts})

    return {
        "policy": (
            "Use only the fictional conversation timeline. "
            "Never use the current real-world date."
        ),
        "conversation_start": conv_start,
        "conversation_end": conv_end,
        "evidence_timestamps": evidence_ts,
    }


# ── Helpers ─────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


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


# ── CLI ─────────────────────────────────────────────────────────────

def arguments() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    p.add_argument("--judge-model", default="deepseek-v4-flash")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--workers", type=int, default=6,
                   help="Concurrent batch workers")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--smoke", action="store_true",
                   help="Only run 5 mandatory regression cases")
    p.add_argument("inputs", nargs="*",
                   help="Prediction JSONL files to judge")
    return p.parse_args()


# ── Judge client ────────────────────────────────────────────────────

def build_client(config_path: str, judge_model: str):
    """Create a DeepSeek client from the maintenance model config."""
    config = load_config(config_path)
    maintenance = config.models["maintenance"]
    values = maintenance.model_dump()
    # api_keys is excluded from model_dump; restore the complete pool so the
    # Judge can round-robin across all configured credentials.
    values["api_keys"] = list(maintenance.api_keys)
    values["model"] = judge_model
    values["temperature"] = 0.0
    values["max_tokens"] = 3000
    values["supports_json_mode"] = True
    # Judge uses json_mode which is incompatible with thinking tokens
    # DeepSeek V4 ignores temperature while thinking mode is enabled. Force
    # the documented disabled-thinking mode for deterministic binary judging.
    values["extra_body"] = {"thinking": {"type": "disabled"}}
    values["reasoning_effort"] = None
    values["reject_reasoning_output"] = False

    # Validate
    assert values["model"] == "deepseek-v4-flash", \
        f"model mismatch: {values['model']}"
    assert "api.deepseek.com" in (values.get("base_url") or ""), \
        f"base_url mismatch: {values.get('base_url')}"
    assert values["supports_json_mode"] is True, \
        "supports_json_mode must be True"

    return create_client(ModelConfig(**values)), config


# ── Batch judgment ──────────────────────────────────────────────────

def judge_batch(
    client,
    prompt: str,
    batch: list[dict],
    temporal_ctx: dict[str, dict],
    retries: int = MAX_RETRIES,
) -> list[dict]:
    """Judge a batch of items. Returns list of judgment dicts."""
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
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": payload_json},
                ],
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
                    f"judgments count mismatch: "
                    f"{len(judgments) if isinstance(judgments, list) else 'not_list'} "
                    f"!= {len(batch)}"
                )

            actual_ids = [str(j.get("qa_id", "")) for j in judgments]
            if actual_ids != expected_ids:
                raise ValueError(
                    f"QA ID order mismatch: expected={expected_ids}, "
                    f"got={actual_ids}"
                )

            results = []
            for j in judgments:
                label = str(j.get("label", "")).upper().strip()
                # Community prompt returns CORRECT/WRONG; normalize to C/W.
                if label == "CORRECT":
                    label = "C"
                elif label == "WRONG":
                    label = "W"
                if label not in VALID_LABELS:
                    raise ValueError(f"Invalid label: {label!r}")
                reason = str(j.get("reason", "")).strip()
                if not reason:
                    raise ValueError("Empty reason")
                # The binary prompt does not constrain reason length; keep a
                # generous bound to catch degenerate outputs without rejecting
                # legitimate verbose reasons.
                if len(reason.split()) > 60:
                    raise ValueError(
                        f"Reason too long ({len(reason.split())} words): "
                        f"{reason[:80]}..."
                    )
                results.append({"label": label, "reason": reason})
            return results

        except Exception as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 10))

    # Fallback: single-item retries
    if len(batch) > 1:
        print(f"  Batch failed after {retries} retries; "
              f"falling back to single-item. Error: {last_error}",
              flush=True)
        results = []
        for item in batch:
            try:
                single = judge_batch(
                    client, prompt, [item], temporal_ctx, retries=retries,
                )
                results.extend(single)
            except Exception as exc:
                results.append({
                    "label": None,
                    "reason": f"FAILED: {str(exc)[:100]}",
                    "qa_id": item["qa_id"],
                })
        return results

    raise RuntimeError(
        f"Judge failed after {retries} retries: {last_error}"
    )


# ── Main ────────────────────────────────────────────────────────────

def main() -> int:
    args = arguments()

    # ── Build client ──────────────────────────────────────────
    client, config = build_client(args.config, args.judge_model)
    print(f"Client: model={args.judge_model} "
          f"base_url={config.models['maintenance'].base_url}")

    # ── Load prompt ───────────────────────────────────────────
    prompt_path = Path(JUDGE_PROMPT_PATH)
    if not prompt_path.exists():
        print(f"ERROR: prompt not found: {prompt_path}")
        return 2
    prompt = prompt_path.read_text(encoding="utf-8")
    prompt_hash = _sha256_file(prompt_path)
    print(f"Prompt: {prompt_path} sha256={prompt_hash[:16]}...")

    # ── Output dir ────────────────────────────────────────────
    out_dir = Path(args.output_dir)
    if args.resume:
        if not out_dir.exists():
            out_dir.mkdir(parents=True, exist_ok=False)
    else:
        if out_dir.exists():
            print(f"ERROR: {out_dir} exists. Use --resume or new --output-dir.")
            return 2
        out_dir.mkdir(parents=True, exist_ok=False)

    judgments_path = out_dir / "judgments.jsonl"
    errors_path = out_dir / "errors.jsonl"
    progress_path = out_dir / "progress.jsonl"
    summary_path = out_dir / "summary.json"
    manifest_path = out_dir / "manifest.json"
    prompt_snapshot = out_dir / "prompt_snapshot.md"
    prompt_snapshot.write_text(prompt, encoding="utf-8")

    # ── Schema normalisation ──────────────────────────────────
    def _normalize_row(row: dict) -> dict:
        """Accept both legacy (answer/evidence) and current
        (reference/evidence_ids) schemas without changing the source file."""
        normalized = dict(row)
        # reference_answer — treat empty string as absent
        if "reference" in row and row["reference"] is not None and row["reference"] != "":
            normalized["reference"] = row["reference"]
        elif "answer" in row:
            # Adversarial/unanswerable LoCoMo rows may intentionally carry an
            # empty reference. Field presence, rather than truthiness, is the
            # schema contract for the legacy ``answer`` representation.
            normalized["reference"] = row["answer"]
        elif "reference" in row:
            # reference exists but is None or empty string → keep it
            normalized["reference"] = row["reference"]
        else:
            raise KeyError(
                f"Row {row.get('qa_id', 'unknown')}: "
                "neither 'reference' nor 'answer' is present"
            )
        # evidence_ids
        if "evidence_ids" in row:
            normalized["evidence_ids"] = row["evidence_ids"]
        elif "evidence" in row:
            normalized["evidence_ids"] = row.get("evidence", [])
        else:
            normalized["evidence_ids"] = []
        return normalized

    # ── Load predictions ──────────────────────────────────────
    pred_files = [Path(p) for p in args.inputs]
    if not pred_files:
        print("ERROR: no input files")
        return 2

    all_rows: list[dict] = []
    for pf in pred_files:
        if not pf.exists():
            print(f"ERROR: {pf} not found")
            return 2
        rows = _load_jsonl(pf)
        normalized = [_normalize_row(r) for r in rows]
        all_rows.extend(normalized)
        print(f"  {pf}: {len(rows)} rows")

    total_input = len(all_rows)
    print(f"Total input: {total_input}")

    # ── Smoke mode: filter to 5 regression cases ──────────────
    SMOKE_IDS = {
        "conv-48_qa_0002", "conv-30_qa_0006", "conv-30_qa_0002",
        "conv-30_qa_0000", "conv-42_qa_0003",
    }
    if args.smoke:
        all_rows = [r for r in all_rows if r["qa_id"] in SMOKE_IDS]
        if len(all_rows) != 5:
            missing = SMOKE_IDS - {r["qa_id"] for r in all_rows}
            print(f"ERROR: smoke test requires exactly 5 cases. "
                  f"Missing: {missing}")
            return 2
        print(f"SMOKE MODE: {len(all_rows)} cases")

    # ── Build temporal context ────────────────────────────────
    conversations, questions_map = load_dataset(config.dataset.path)
    temporal_ctx: dict[str, dict] = {}
    for row in all_rows:
        cid = row["conversation_id"]
        qa_id = row["qa_id"]
        temporal_ctx[qa_id] = build_temporal_context(
            conversations, questions_map, cid, qa_id,
        )

    # ── Resume ────────────────────────────────────────────────
    completed: dict[str, dict] = {}
    if args.resume and judgments_path.exists():
        for row in _load_jsonl(judgments_path):
            qid = row.get("qa_id", "")
            if qid and row.get("label") in VALID_LABELS:
                completed[qid] = row
        print(f"Resume: {len(completed)} already judged")

    pending = [r for r in all_rows if r["qa_id"] not in completed]

    # ── Judge batches (concurrent) ──────────────────────────────
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    write_lock = threading.Lock()
    retry_count = [0]
    fallback_count = [0]
    permanent_errors = [0]
    done_count = [0]

    def process_batch(batch: list[dict], batch_idx: int) -> None:
        """Judge one batch and append results thread-safely."""
        try:
            results = judge_batch(
                client, prompt, batch, temporal_ctx, retries=MAX_RETRIES,
            )
        except Exception as exc:
            with write_lock:
                for item in batch:
                    _append_jsonl(errors_path, {
                        "qa_id": item["qa_id"],
                        "error": str(exc)[:200],
                        "timestamp": _ts(),
                    })
                permanent_errors[0] += len(batch)
                retry_count[0] += MAX_RETRIES
            return

        with write_lock:
            for item, result in zip(batch, results):
                label = result.get("label")
                if label is None:
                    _append_jsonl(errors_path, {
                        "qa_id": item["qa_id"],
                        "error": result.get("reason", "unknown"),
                        "timestamp": _ts(),
                    })
                    permanent_errors[0] += 1
                    continue

                tc = temporal_ctx.get(item["qa_id"], {})
                judgment = {
                    "conversation_id": item["conversation_id"],
                    "qa_id": item["qa_id"],
                    "category": item["category"],
                    "label": label,
                    "reason": result["reason"],
                    "judge_model": args.judge_model,
                    "judge_prompt_version": JUDGE_PROMPT_VERSION,
                    "temporal_context": tc,
                }
                _append_jsonl(judgments_path, judgment)
                done_count[0] += 1

            # Progress
            _append_jsonl(progress_path, {
                "completed": done_count[0],
                "total": len(pending),
                "timestamp": _ts(),
            })

        if done_count[0] % 50 == 0:
            print(f"  judged={done_count[0]}/{len(pending)} "
                  f"({done_count[0] * 100 // max(len(pending), 1)}%) "
                  f"errors={permanent_errors[0]}",
                  flush=True)

    # Build batches
    batches = []
    for start in range(0, len(pending), args.batch_size):
        batches.append(pending[start:start + args.batch_size])

    print(f"Processing {len(batches)} batches with {args.workers} workers...")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_batch, batch, i): i
            for i, batch in enumerate(batches)
        }
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                print(f"  Worker error: {exc}", flush=True)

    print(f"  judged={done_count[0]}/{len(pending)} (100%) "
          f"errors={permanent_errors[0]}", flush=True)

    # ── Validate ──────────────────────────────────────────────
    all_judgments = _load_jsonl(judgments_path)
    j_ids = [j["qa_id"] for j in all_judgments]
    unique_ids = set(j_ids)
    invalid_labels = sum(1 for j in all_judgments
                         if j.get("label") not in VALID_LABELS)
    missing_f1 = sum(1 for j in all_judgments
                     if "f1" in j or "strict_score" in j)
    missing_ts = sum(1 for j in all_judgments
                     if "temporal_context" not in j)

    print(f"\nValidation:")
    print(f"  input rows:        {total_input}")
    print(f"  output rows:       {len(all_judgments)}")
    print(f"  unique qa_id:      {len(unique_ids)}")
    print(f"  missing qa_id:     {total_input - len(unique_ids)}")
    print(f"  invalid label:     {invalid_labels}")
    print(f"  permanent errors:  {permanent_errors[0]}")
    print(f"  records with F1:   {missing_f1}")
    print(f"  records no ts:     {missing_ts}")

    # ── Summary ───────────────────────────────────────────────
    labels = Counter(j["label"] for j in all_judgments)
    by_conv = defaultdict(lambda: Counter())
    by_cat = defaultdict(lambda: Counter())
    for j in all_judgments:
        by_conv[j["conversation_id"]][j["label"]] += 1
        by_cat[str(j["category"])][j["label"]] += 1

    summary = {
        "judge_model": args.judge_model,
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "total": len(all_judgments),
        "labels": dict(labels),
        "by_conversation": {
            cid: dict(cnt) for cid, cnt in sorted(by_conv.items())
        },
        "by_category": {
            cat: dict(cnt) for cat, cnt in sorted(by_cat.items())
        },
        "permanent_errors[0]": permanent_errors[0],
        "prompt_sha256": prompt_hash,
    }
    _write_json(summary_path, summary)
    print(f"\nLabels: C={labels["C"]} W={labels["W"]}")

    # ── Manifest ──────────────────────────────────────────────
    manifest = {
        "run_id": out_dir.name,
        "created_at": _ts(),
        "judge_model": args.judge_model,
        "base_url": config.models["maintenance"].base_url,
        "temperature": 0.0,
        "max_tokens": 3000,
        "batch_size": args.batch_size,
        "prompt_version": JUDGE_PROMPT_VERSION,
        "prompt_sha256": prompt_hash,
        "expected_input_rows": total_input,
        "completed_output_rows": len(all_judgments),
        "token_f1_used_for_routing": False,
        "temporal_anchor_policy": "fictional_conversation_timeline_only",
        "source_files": [str(p) for p in pred_files],
    }
    _write_json(manifest_path, manifest)

    # ── Smoke: verify mandatory labels ───────────────────────
    if args.smoke:
        MANDATORY = {
            "conv-48_qa_0002": "C",
            "conv-30_qa_0006": "C",
            "conv-30_qa_0002": "C",
            "conv-30_qa_0000": "W",
            "conv-42_qa_0003": "W",
        }
        jmap = {j["qa_id"]: j["label"] for j in all_judgments}
        all_ok = True
        for qid, expected in MANDATORY.items():
            got = jmap.get(qid, "MISSING")
            ok = "OK" if got == expected else "FAIL"
            if got != expected:
                all_ok = False
            print(f"  {ok} {qid}: expected={expected} got={got}")
        if not all_ok:
            print("SMOKE TEST FAILED — mandatory labels not matched")
            return 3
        print("SMOKE TEST PASSED")

    # ── Final validation ──────────────────────────────────────
    if invalid_labels > 0 or permanent_errors[0] > 0:
        print("VALIDATION FAILED")
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
