"""Build train-only iterative cases from two deterministic Judge runs.

The learnable case set is C2W, W2C, and W2W.  C2C is counted only: stable
successes are neither diagnosis targets nor Candidate sources.  W2W carries a
failure age so repeated failures remain visible across Bank iterations.
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
    prior_failure_ages: dict[str, int] | None = None,
) -> dict[str, Any]:
    prior_failure_ages = prior_failure_ages or {}
    shared = sorted(before.keys() & after.keys())
    c2w: list[dict[str, str]] = []
    w2c: list[dict[str, str]] = []
    w2w: list[dict[str, Any]] = []
    c2c = 0
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
        elif transition == "W2W":
            w2w.append({
                "qa_id": qa_id,
                "before_run": from_run,
                "after_run": to_run,
                "failure_age": max(1, int(prior_failure_ages.get(qa_id, 0)) + 1),
            })
        elif transition == "C2C":
            c2c += 1
    return {
        "schema_version": "iteration_cases_v3",
        chain_id: {
            "name": chain_id,
            "from": from_bank,
            "to": to_bank,
            "from_run": from_run,
            "to_run": to_run,
            "correct_side": from_run,
            "wrong_side_c2w": to_run,
            "wrong_side_w2c": from_run,
            "C2W": c2w,
            "W2C": w2c,
            "W2W": w2w,
            "summary": {
                "shared": len(shared),
                "C2W": len(c2w),
                "W2C": len(w2c),
                "W2W": len(w2w),
                "C2C": c2c,
                "learnable_cases": len(c2w) + len(w2c) + len(w2w),
                "before_only": len(before.keys() - after.keys()),
                "after_only": len(after.keys() - before.keys()),
            },
        },
    }


def _load_prior_failure_ages(path: Path | None) -> dict[str, int]:
    """Load the latest W2W age per QA from a prior iteration-case file."""
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    ages: dict[str, int] = {}
    for name, chain in value.items():
        if name == "schema_version" or not isinstance(chain, dict):
            continue
        for row in chain.get("W2W", []):
            if not isinstance(row, dict) or not row.get("qa_id"):
                continue
            qa_id = str(row["qa_id"])
            ages[qa_id] = max(ages.get(qa_id, 0), int(row.get("failure_age", 1)))
    return ages


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-judgments", required=True)
    parser.add_argument("--to-judgments", required=True)
    parser.add_argument("--chain-id", required=True)
    parser.add_argument("--from-bank", required=True)
    parser.add_argument("--to-bank", required=True)
    parser.add_argument("--from-run", required=True)
    parser.add_argument("--to-run", required=True)
    parser.add_argument(
        "--prior-cases",
        help="Optional prior iteration_cases_v3 JSON used to increment W2W age.",
    )
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
        prior_failure_ages=_load_prior_failure_ages(
            Path(args.prior_cases) if args.prior_cases else None
        ),
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
