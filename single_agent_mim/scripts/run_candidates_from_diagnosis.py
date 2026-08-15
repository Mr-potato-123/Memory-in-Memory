"""Generate Skill candidates from standard Diagnosis repair packages.

Reads ``<diagnosis-root>/<answer_failure|cons_failure>/packages/*/*.json``
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
from mim.skill_maker.success_examples import (
    NoSkillSuccessIndex,
    SuccessfulSkillExampleIndex,
)
from mim.skill_maker import SkillRepository


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_prompt(path: str) -> str:
    prompt_path = Path(path)
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file does not exist: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _collect_packages(diagnosis_root: Path) -> list[dict]:
    by_qa: dict[str, list[dict]] = {}
    for component in ("answer_failure", "cons_failure"):
        packages = diagnosis_root / component / "packages"
        if not packages.exists():
            continue
        for path in sorted(packages.rglob("*.json")):
            report = _load_json(path)
            side = (
                "construction"
                if component == "cons_failure"
                else "access"
            )
            if not report.get("problem_found"):
                continue
            if component == "answer_failure":
                repair = report.get("repair_package")
                repair = repair if isinstance(repair, dict) else {}
                if not (
                    report.get("retrieved_context_sufficient") is True
                    and report.get("skill_learnable") is True
                    and repair.get("eligible_for_skill_generation") is True
                    and repair.get("failure_scope")
                    == "memory_answering_procedure"
                ):
                    continue
            qa_id = str(report.get("qa_id") or report.get("diagnosis_id") or path)
            by_qa.setdefault(qa_id, []).append(
                {"side": side, "component": component,
                 "report": report, "path": path, "kind": "failure"}
            )

    # Answer-side and Construction diagnoses remain separate. Fixed Mem0
    # retrieval failures are intentionally absent because post-search Skills
    # cannot repair them.
    rows: list[dict] = []
    for qa_id in sorted(by_qa):
        items = by_qa[qa_id]
        rows.extend(items)
    component_order = {
        "answer_failure": 0,
        "cons_failure": 1,
    }
    rows.sort(key=lambda item: (
        component_order[item["component"]],
        str(item["report"].get("qa_id") or item["report"].get("diagnosis_id") or ""),
        str(item["path"]),
    ))
    return rows


def _load_success_packages(path: Path | None) -> list[dict]:
    # Under fixed-search Mem0, a no-Skill success is a control example.  It is
    # never positive evidence that a new Runtime Skill is necessary.
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    parser.add_argument("--diagnosis-root", required=True)
    parser.add_argument("--skills-dir", required=True)
    parser.add_argument("--success-examples",
                        help="JSONL of Judge-C skill-use trajectories; "
                             "one matched example is attached per diagnosis "
                             "for scope calibration.")
    parser.add_argument("--success-package",
                        help="JSONL of Judge-C questions answered with NO "
                             "Skill (default-policy successes); the most "
                             "similar one is attached per diagnosis as "
                             "DEFAULT_POLICY_SUCCESS_EXAMPLE.")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-concurrency", type=int, default=6,
                        help="Hard cap on parallel candidate generations per "
                             "process (default 6 = 3 keys x 2).")
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip packages whose source diagnosis already has a saved candidate.",
    )
    parser.add_argument(
        "--package-source", choices=("both", "failure", "success"),
        default="both", help="Generate one source shard or the joint set.",
    )
    parser.add_argument(
        "--failure-side", choices=("all", "access", "construction"),
        default="all", help="Further shard failure packages by Skill side.",
    )
    parser.add_argument(
        "--retry-errors-from",
        help="Only regenerate QA IDs whose prior generation summary has status=error.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    success_index = (
        SuccessfulSkillExampleIndex.load(Path(args.success_examples))
        if args.success_examples
        else None
    )
    default_policy_index = (
        NoSkillSuccessIndex.load(Path(args.success_package))
        if args.success_package
        else None
    )
    if default_policy_index is not None:
        print(
            f"Default-policy success examples: "
            f"{default_policy_index.count()}",
            flush=True,
        )
    packages = (
        _collect_packages(Path(args.diagnosis_root))
        if args.package_source in {"both", "failure"}
        else []
    )
    if args.failure_side != "all":
        packages = [
            item for item in packages if item.get("side") == args.failure_side
        ]
    failure_count = len(packages)
    success_packages = (
        _load_success_packages(
            Path(args.success_package) if args.success_package else None
        )
        if args.package_source in {"both", "success"}
        else []
    )
    packages.extend(success_packages)
    if args.retry_errors_from:
        prior = json.loads(Path(args.retry_errors_from).read_text(encoding="utf-8"))
        retry_ids = {
            str(row.get("qa_id"))
            for row in prior.get("rows", [])
            if row.get("status") == "error" and row.get("qa_id")
        }
        packages = [
            item for item in packages
            if str(item["report"].get("qa_id")) in retry_ids
        ]
        print(f"Retrying prior errors: {len(packages)}", flush=True)
    if args.max_items > 0:
        packages = packages[: args.max_items]
    print(f"Learning packages: {len(packages)} "
          f"(failures_after_adjudication={failure_count}, "
          f"success={len(success_packages)}, "
          f"access={sum(1 for p in packages if p['side']=='access')}, "
          f"construction={sum(1 for p in packages if p['side']=='construction')})",
          flush=True)
    if not packages:
        print("No repair packages; nothing to generate.")
        return 0

    repository = SkillRepository(Path(args.skills_dir))
    existing_source_ids: set[str] = set()
    if args.resume:
        for side in ("access", "construction"):
            for existing in repository.list_candidates(side):
                if existing.source_diagnosis_id:
                    existing_source_ids.add(str(existing.source_diagnosis_id))
        before = len(packages)
        packages = [
            item for item in packages
            if str(
                item["report"].get("diagnosis_id")
                or item["report"].get("qa_id")
            ) not in existing_source_ids
        ]
        print(
            f"Resume: skipped {before - len(packages)} packages with existing candidates.",
            flush=True,
        )
    maintenance = config.models["maintenance"]
    # The provider may expose one API key while still permitting multiple
    # in-flight requests.  The old key-count cap silently reduced every run
    # to two workers.  Concurrency is now an explicit operator choice; the
    # caller can lower it when the provider returns rate-limit errors.
    pool_size = min(args.workers, args.max_concurrency)
    pool_size = max(1, pool_size)
    print(f"Candidate generation concurrency: {pool_size}", flush=True)

    def generate_one(item: dict) -> dict:
        side = item["side"]
        kind = item.get("kind", "failure")
        agent = CandidateSkillAgent(
            create_client(copy.deepcopy(maintenance)),
            prompt=_read_prompt(
                "prompts/skill_maker/candidate_generation_success_access.md"
                if kind == "success"
                else config.prompts.skill_candidate_generation_access
                if side == "access"
                else config.prompts.skill_candidate_generation_construction
            ),
            success_examples=success_index if kind == "failure" else None,
            default_policy_examples=(
                default_policy_index if kind == "failure" else None
            ),
        )
        report = item["report"]
        original_qa_id = report.get("qa_id")
        if kind == "success":
            report = {
                "diagnosis_id": f"success_{original_qa_id}",
                "qa_id": original_qa_id,
                "source_mode": "success",
                "stage": "post_search_recovery",
                "question": report.get("question"),
                "positive_runtime_example": report,
                "instruction": (
                    "Internalize only a non-trivial reusable Access decision "
                    "that remains useful after a default first search."
                ),
            }
        diag_id = str(report.get("diagnosis_id") or report.get("qa_id"))
        try:
            candidate = agent.generate(
                diagnosis=report,
                side=side,
            )
        except Exception as exc:
            return {"status": "error", "qa_id": report.get("qa_id"),
                    "side": side, "source": kind, "error": str(exc)[:300]}
        if candidate is None:
            return {"status": "no_change", "qa_id": report.get("qa_id"),
                    "side": side, "source": kind}
        try:
            repository.save_candidate(candidate)
        except Exception as exc:
            return {"status": "error", "qa_id": report.get("qa_id"),
                    "side": side, "source": kind,
                    "error": f"save_candidate: {str(exc)[:300]}"}
        return {"status": "ok", "qa_id": report.get("qa_id"), "side": side,
                "source": kind,
                "candidate_id": candidate.candidate_id,
                "skill_id": candidate.skill_id,
                "diagnosis_id": diag_id}

    summary = {"ok": 0, "no_change": 0, "error": 0,
               "failure_packages": failure_count,
               "success_packages": len(success_packages), "rows": []}
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        futures = {pool.submit(generate_one, item): item for item in packages}
        for future in as_completed(futures):
            try:
                outcome = future.result()
            except Exception as exc:
                item = futures[future]
                outcome = {
                    "status": "error",
                    "qa_id": item["report"].get("qa_id"),
                    "side": item["side"],
                    "source": item.get("kind", "failure"),
                    "error": f"worker: {str(exc)[:300]}",
                }
            summary[outcome["status"]] += 1
            summary["rows"].append(outcome)
            print(f"[{outcome['status']:8s}] {outcome.get('source','?'):7s} "
                  f"{outcome.get('side','?'):12s} "
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
