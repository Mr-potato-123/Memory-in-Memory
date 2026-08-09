"""Merge candidate directories for one CRUD round, stripping stale traces.

Flip-diagnosis candidates carry ``related_existing_skill_ids`` pointing at
the WRONG-side run's Skill Bank (the previous round's trajectory). When those
candidates are merged with full-diagnosis candidates and CRUD against a NEW
seed bank, stale skill ids would poison duplicate detection and merge
decisions. This script strips them (and any other trace references) and copies
all candidates into one clean candidate root.

Usage:
  python scripts/merge_candidates.py \
      --out outputs/merged_candidates \
      --clean-flip outputs/flip_train_b1_skills/candidates \
      --full outputs/bank1_full_skills/candidates
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def strip_stale(candidate: dict) -> dict:
    """Remove previous-round trajectory references from one candidate."""
    candidate = dict(candidate)
    # related_existing_skill_ids came from the diagnosed (wrong-side) run's
    # skill trace; they do not describe the seed bank, so drop them all.
    candidate["related_existing_skill_ids"] = []
    candidate["parent_version_id"] = None
    return candidate


def copy_candidates(source: Path, out: Path, *, clean: bool) -> int:
    count = 0
    for side in ("access", "construction"):
        src_dir = source / side
        if not src_dir.exists():
            continue
        for candidate_dir in sorted(src_dir.glob("*/")):
            candidate_file = candidate_dir / "candidate.json"
            if not candidate_file.exists():
                continue
            data = json.loads(candidate_file.read_text(encoding="utf-8"))
            if clean:
                data = strip_stale(data)
            target = out / side / candidate_dir.name
            target.mkdir(parents=True, exist_ok=True)
            (target / "candidate.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            # Copy any staged records so repository history is preserved.
            for extra in candidate_dir.glob("*.jsonl"):
                shutil.copy2(extra, target / extra.name)
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--clean-flip", action="append", default=[],
                        help="Flip-diagnosis candidate roots; related ids are "
                             "stripped (repeatable).")
    parser.add_argument("--full", action="append", default=[],
                        help="Full-diagnosis candidate roots; copied as-is "
                             "(repeatable).")
    args = parser.parse_args()

    out = Path(args.out)
    if out.exists():
        raise FileExistsError(f"Output already exists: {out}")
    total = 0
    for source in args.clean_flip:
        n = copy_candidates(Path(source), out, clean=True)
        total += n
        print(f"[clean] {source}: {n} candidates (stale ids stripped)")
    for source in args.full:
        n = copy_candidates(Path(source), out, clean=False)
        total += n
        print(f"[full ] {source}: {n} candidates")
    print(f"Merged candidates: {total} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
