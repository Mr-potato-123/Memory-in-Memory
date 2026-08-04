"""Generate Skill candidates from V3 Diagnosis repair packages.

Reads ``<diagnosis-root>/<access_failure|cons_failure>/packages/*/*.json``
(each row is a full V3 report with problem_found=True and a repair_package),
feeds them to the CandidateSkillAgent, and saves candidates into a
``SkillRepository`` layout at ``<skills-dir>/candidates/<side>/<id>/candidate.json``
that ``run_skill_bank_pipeline_v2.py`` can consume.

Candidate generation is parallelized over the maintenance key pool.

Usage:
  python scripts/run_candidates_from_diagnosis.py \
      --config configs/qwen3_8b_dashscope.yaml \
      --diagnosis-root outputs/v2_iter/iter1/diagnosis \
      --skills-dir outputs/v2_iter/iter1/skills \
      --workers 8 [--max-items 0]
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.agents.skill_learning import CandidateSkillAgent
from mim.config import load_config
from mim.llm import create_client
from mim.skill_maker import SkillRepository


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_prompt(path: str) -> str:
    prompt_path = Path(path)
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file does not exist: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _collect_packages(diagnosis_root: Path) -> list[dict]:
    rows: list[dict] = []
    for component in ("access_failure", "cons_failure"):
        packages = diagnosis_root / component / "packages"
        if not packages.exists():
            continue
        for path in sorted(packages.rglob("*.json")):
            report = _load_json(path)
            side = "access" if component == "access_failure" else "construction"
            if not report.get("problem_found"):
                continue
            rows.append({"side": side, "report": report, "path": path})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    parser.add_argument("--diagnosis-root", required=True)
    parser.add_argument("--skills-dir", required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-items", type=int, default=0)
    args = parser.parse_args()

    config = load_config(args.config)
    packages = _collect_packages(Path(args.diagnosis_root))
    if args.max_items > 0:
        packages = packages[: args.max_items]
    print(f"Repair packages: {len(packages)} "
          f"(access={sum(1 for p in packages if p['side']=='access')}, "
          f"construction={sum(1 for p in packages if p['side']=='construction')})",
          flush=True)
    if not packages:
        print("No repair packages; nothing to generate.")
        return 0

    repository = SkillRepository(Path(args.skills_dir))
    maintenance = config.models["maintenance"]
    pool_size = min(len(getattr(maintenance, "api_keys", []) or [maintenance]) * 2, args.workers)
    pool_size = max(1, pool_size)

    def generate_one(item: dict) -> dict:
        side = item["side"]
        agent = CandidateSkillAgent(
            create_client(copy.deepcopy(maintenance)),
            prompt=_read_prompt(
                config.prompts.skill_candidate_generation_access
                if side == "access"
                else config.prompts.skill_candidate_generation_construction
            ),
        )
        report = item["report"]
        diag_id = str(report.get("diagnosis_id") or report.get("qa_id"))
        try:
            candidate = agent.generate(
                diagnosis=report,
                side=side,
            )
        except Exception as exc:
            return {"status": "error", "qa_id": report.get("qa_id"),
                    "side": side, "error": str(exc)[:300]}
        if candidate is None:
            return {"status": "no_change", "qa_id": report.get("qa_id"),
                    "side": side}
        repository.save_candidate(candidate)
        return {"status": "ok", "qa_id": report.get("qa_id"), "side": side,
                "candidate_id": candidate.candidate_id,
                "skill_id": candidate.skill_id,
                "diagnosis_id": diag_id}

    summary = {"ok": 0, "no_change": 0, "error": 0, "rows": []}
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        futures = {pool.submit(generate_one, item): item for item in packages}
        for future in as_completed(futures):
            outcome = future.result()
            summary[outcome["status"]] += 1
            summary["rows"].append(outcome)
            print(f"[{outcome['status']:8s}] {outcome.get('side','?'):12s} "
                  f"{outcome.get('qa_id','?')} {outcome.get('candidate_id','')}",
                  flush=True)

    summary_path = Path(args.skills_dir) / "candidates" / "generation_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nCandidates: ok={summary['ok']} no_change={summary['no_change']} "
          f"error={summary['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
