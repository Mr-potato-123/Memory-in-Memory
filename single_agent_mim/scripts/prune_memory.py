"""Prune unused memories from one conversation's SQLite store.

A memory is "used" when any QA run cited it in ``access_final_evidence``.
All other currently-active memory versions (``system_to_commit IS NULL``) are
closed at the next commit boundary with ``close_reason='pruned_unused'``, so
they drop out of every subsequent snapshot while history stays intact.

Usage:
  python scripts/prune_memory.py --run-root outputs/v2_iter/iter2/train \
      [--conversation-id conv-30] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def prune_db(
    db_path: Path,
    conversation_id: str,
    *,
    dry_run: bool = False,
) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        latest = conn.execute(
            "SELECT COALESCE(MAX(system_from_commit), 0) AS m "
            "FROM memory_versions WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()["m"]

        active = conn.execute(
            "SELECT version_id FROM memory_versions "
            "WHERE conversation_id=? AND system_to_commit IS NULL",
            (conversation_id,),
        ).fetchall()
        active_ids = [row["version_id"] for row in active]

        used_rows = conn.execute(
            "SELECT DISTINCT version_id FROM access_final_evidence e "
            "JOIN access_runs r ON r.access_run_id = e.access_run_id "
            "WHERE r.conversation_id=?",
            (conversation_id,),
        ).fetchall()
        used_ids = {row["version_id"] for row in used_rows}

        # Also keep versions cited as context (visible during answering) even
        # if not final evidence; answering quality depends on both.
        context_rows = conn.execute(
            "SELECT DISTINCT c.version_id FROM access_answer_context c "
            "JOIN access_runs r ON r.access_run_id = c.access_run_id "
            "WHERE r.conversation_id=?",
            (conversation_id,),
        ).fetchall()
        used_ids |= {row["version_id"] for row in context_rows}

        to_prune = [vid for vid in active_ids if vid not in used_ids]
        stats = {
            "conversation_id": conversation_id,
            "active_total": len(active_ids),
            "used": len([v for v in active_ids if v in used_ids]),
            "pruned": len(to_prune),
            "close_commit": latest + 1,
            "dry_run": dry_run,
        }
        if dry_run:
            return stats

        if to_prune:
            conn.executemany(
                "UPDATE memory_versions SET system_to_commit=?, close_reason=? "
                "WHERE version_id=?",
                [
                    (latest + 1, "pruned_unused", vid)
                    for vid in to_prune
                ],
            )
        conn.commit()
        return stats
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True,
                        help="Root containing <conversation_id>/state/memory.sqlite3")
    parser.add_argument("--conversation-id", action="append",
                        help="Prune only this conversation (repeatable); "
                             "default: all subdirectories with a DB")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(args.run_root)
    targets = []
    if args.conversation_id:
        targets = [
            (cid, root / cid / "state" / "memory.sqlite3")
            for cid in args.conversation_id
        ]
    else:
        targets = []
        for db in sorted(root.rglob("state/memory.sqlite3")):
            cid = db.parts[root.parts.index(root) + 1]
            targets.append((cid, db))
    if not targets:
        print("No memory databases found under", root)
        return 2

    all_stats = []
    for cid, db in targets:
        if not db.exists():
            print(f"SKIP {cid}: {db} not found")
            continue
        stats = prune_db(db, cid, dry_run=args.dry_run)
        all_stats.append(stats)
        print(f"[{'dry-run' if args.dry_run else 'pruned'}] {cid}: "
              f"active={stats['active_total']} used={stats['used']} "
              f"removed={stats['pruned']}")
    (root / "prune_summary.json").write_text(
        json.dumps(all_stats, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
