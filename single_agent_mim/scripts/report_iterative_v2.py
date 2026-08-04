"""Summarise the iterative V2 experiment results.

Usage:
  python scripts/report_iterative_v2.py --output-root outputs/v2_iter
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def judge_summary(path: Path) -> dict | None:
    data = _load_json(path)
    if not data:
        return None
    labels = data.get("labels", {})
    return {
        "C": labels.get("C", 0),
        "P": labels.get("P", 0),
        "I": labels.get("I", 0),
        "total": data.get("total", 0),
    }


def print_section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default="outputs/v2_iter")
    args = parser.parse_args()
    root = Path(args.output_root)

    print_section("ITERATION 1 (Bank0 train -> Bank1)")
    for name in ("train", "judge", "diagnosis"):
        path = root / "iter1" / name
        print(f"\n[{name}]")
        if name == "train":
            summary = _load_json(path / "summary.json")
            if summary:
                for conv in summary.get("conversations", []):
                    print(f"  {conv.get('conversation_id', '?'):8s} "
                          f"qa={conv.get('total_qa', '?')} "
                          f"const_errors={conv.get('construction_errors', '?')} "
                          f"errors={len(conv.get('errors', []))}")
        elif name == "judge":
            print(f"  {judge_summary(path / 'summary.json')}")
        else:
            for comp in ("answer_failure", "access_failure", "cons_failure"):
                summary = _load_json(path / comp / "summary.json")
                if summary:
                    print(f"  {comp}: {summary}")
    cand_summary = _load_json(
        root / "iter1" / "skills" / "candidates" / "generation_summary.json"
    )
    if cand_summary:
        print(f"\n[candidates] ok={cand_summary.get('ok')} "
              f"no_change={cand_summary.get('no_change')} "
              f"error={cand_summary.get('error')}")
    bank = _load_json(root / "iter1" / "bank1" / "summary.json")
    if bank:
        print(f"[bank1] clusters={bank.get('semantic_clusters')} "
              f"drafts={bank.get('drafts')} "
              f"official={bank.get('official_skills')}")

    print_section("ITERATION 2 (Bank1 train over pruned memory -> Bank2)")
    for name in ("train", "judge"):
        path = root / "iter2" / name
        print(f"\n[{name}]")
        if name == "train":
            summary = _load_json(path / "summary.json")
            if summary:
                for conv in summary.get("conversations", []):
                    print(f"  {conv.get('conversation_id', '?'):8s} "
                          f"qa={conv.get('total_qa', '?')} "
                          f"resumed={conv.get('resumed_from', '?')} "
                          f"errors={len(conv.get('errors', []))}")
        else:
            print(f"  {judge_summary(path / 'summary.json')}")
    cand_summary2 = _load_json(
        root / "iter2" / "skills" / "candidates" / "generation_summary.json"
    )
    if cand_summary2:
        print(f"\n[candidates] ok={cand_summary2.get('ok')} "
              f"no_change={cand_summary2.get('no_change')} "
              f"error={cand_summary2.get('error')}")
    bank2 = _load_json(root / "iter2" / "bank2" / "summary.json")
    if bank2:
        print(f"[bank2] clusters={bank2.get('semantic_clusters')} "
              f"drafts={bank2.get('drafts')} "
              f"official={bank2.get('official_skills')}")

    prune1 = _load_json(root / "iter1" / "train" / "prune_summary.json")
    prune2 = _load_json(root / "iter2" / "train" / "prune_summary.json")
    print_section("MEMORY PRUNING")
    for label, data in (("iter1", prune1), ("iter2", prune2)):
        if not data:
            continue
        total_active = sum(row.get("active_total", 0) for row in data)
        total_pruned = sum(row.get("pruned", 0) for row in data)
        print(f"  {label}: active={total_active} pruned={total_pruned} "
              f"({total_pruned * 100 // max(total_active, 1)}%)")

    print_section("VALIDATION — FOUR VARIANTS (shared memory, Bank2)")
    variants = {}
    for name in ("bank0", "full", "acc", "cons"):
        summary = judge_summary(root / "val_eval" / name / "judge" / "summary.json")
        variants[name] = summary
        if summary:
            cp = summary["C"] + summary["P"]
            print(f"  {name:6s}: C={summary['C']:3d} P={summary['P']:3d} "
                  f"I={summary['I']:3d} | C+P={cp:3d} "
                  f"({cp * 100 / summary['total']:.1f}%)")

    if all(variants.values()):
        print_section("TRANSITION MATRIX: cons -> full (access-skill effect)")
        cons_j = {
            r["qa_id"]: r["label"]
            for r in _load_jsonl(root / "val_eval" / "cons" / "judge" / "judgments.jsonl")
        }
        full_j = {
            r["qa_id"]: r["label"]
            for r in _load_jsonl(root / "val_eval" / "full" / "judge" / "judgments.jsonl")
        }
        matrix = Counter((cons_j.get(q), full_j.get(q)) for q in cons_j)
        for (a, b), n in sorted(matrix.items()):
            if a != b:
                print(f"    cons {a} -> full {b}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
