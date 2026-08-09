"""Build train-only C2W/W2C pairs from two deterministic Judge runs.

The output remains compatible with ``run_flip_diagnosis.py`` while making the
run names and Bank transition explicit instead of relying on hand-written
JSON.  Only label flips are emitted; stable C2C/W2W rows are summarized.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_labels(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line_no, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        qa_id = str(value.get("qa_id", ""))
        label = str(value.get("label", "")).upper()
        if not qa_id or label not in {"C", "W"}:
            raise ValueError(f"Invalid judgment at {path}:{line_no}")
        if qa_id in rows:
            raise ValueError(f"Duplicate qa_id {qa_id!r} in {path}")
        rows[qa_id] = value
    return rows


def build_pairs(
    *,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    chain_id: str,
    from_bank: str,
    to_bank: str,
    from_run: str,
    to_run: str,
) -> dict[str, Any]:
    shared = sorted(before.keys() & after.keys())
    c2w: list[dict[str, str]] = []
    w2c: list[dict[str, str]] = []
    stable = {"C2C": 0, "W2W": 0}
    for qa_id in shared:
        transition = (
            str(before[qa_id]["label"]).upper()
            + "2"
            + str(after[qa_id]["label"]).upper()
        )
        if transition == "C2W":
            c2w.append(
                {"qa_id": qa_id, "ok_run": from_run, "wrong_run": to_run}
            )
        elif transition == "W2C":
            w2c.append(
                {"qa_id": qa_id, "ok_run": to_run, "wrong_run": from_run}
            )
        else:
            stable[transition] += 1
    return {
        "schema_version": "contrastive_pairs_v2",
        chain_id: {
            "name": chain_id,
            "from": from_bank,
            "to": to_bank,
            "correct_side": from_run,
            "wrong_side_c2w": to_run,
            "wrong_side_w2c": from_run,
            "C2W": c2w,
            "W2C": w2c,
            "summary": {
                "shared": len(shared),
                "C2W": len(c2w),
                "W2C": len(w2c),
                **stable,
                "before_only": len(before.keys() - after.keys()),
                "after_only": len(after.keys() - before.keys()),
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-judgments", required=True)
    parser.add_argument("--to-judgments", required=True)
    parser.add_argument("--chain-id", required=True)
    parser.add_argument("--from-bank", required=True)
    parser.add_argument("--to-bank", required=True)
    parser.add_argument("--from-run", required=True)
    parser.add_argument("--to-run", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    result = build_pairs(
        before=_load_labels(Path(args.from_judgments)),
        after=_load_labels(Path(args.to_judgments)),
        chain_id=args.chain_id,
        from_bank=args.from_bank,
        to_bank=args.to_bank,
        from_run=args.from_run,
        to_run=args.to_run,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result[args.chain_id]["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
