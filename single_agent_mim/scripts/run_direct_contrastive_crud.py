"""Apply contrastive diagnoses directly to a shadow copy of a published Bank.

This path intentionally skips Candidate Skill generation, clustering, and
cluster summarization.  One QA pair is one rollbackable transaction; an
Access+Construction case is validated on a temporary repository before either
side is committed to the working shadow Bank.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mim.agents.skill_learning import DirectCaseCrudAgent
from mim.config import load_config
from mim.llm import create_client
from mim.retrieval.embedder import Embedder
from mim.skill_maker.batch import BatchSkillRetriever, SkillCrudExecutor
from mim.skill_maker.models import SkillCandidate, SkillCandidateBatch, SkillPayload
from mim.skill_maker.repository import SkillRecord, SkillRepository
from mim.skills import SkillBank


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def _load_progress(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {str(row["case_key"]): row for row in rows}


def _load_cases(root: Path) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for component in ("access_failure", "cons_failure"):
        for path in sorted((root / component / "packages").glob("*/*.json")):
            package = json.loads(path.read_text(encoding="utf-8"))
            flip = package.get("flip") if isinstance(package.get("flip"), dict) else {}
            chain = str(flip.get("chain") or "chain")
            qa_id = str(package.get("qa_id") or path.stem)
            key = f"{chain}__{qa_id}"
            grouped[key].append({"path": str(path), "package": package})
    cases = []
    for key, projections in grouped.items():
        directions = {
            str(item["package"].get("flip", {}).get("direction", ""))
            for item in projections
        }
        if len(directions) != 1:
            raise ValueError(f"Case has inconsistent directions: {key}: {directions}")
        sides = [str(item["package"].get("side", "")) for item in projections]
        if len(sides) != len(set(sides)):
            raise ValueError(f"Case has duplicate side projections: {key}: {sides}")
        cases.append(
            {
                "case_key": key,
                "direction": directions.pop(),
                "projections": sorted(
                    projections,
                    key=lambda item: (
                        0 if item["package"].get("side") == "construction" else 1
                    ),
                ),
            }
        )
    # Diagnose regressions first, then adopt improvements.  This makes the
    # working Bank conservative when two cases touch the same concise Skill.
    return sorted(
        cases,
        key=lambda item: (
            {"C2W": 0, "W2W": 1, "W2C": 2}.get(item["direction"], 3),
            item["case_key"],
        ),
    )


def _skill_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "skill_id" and child:
                found.add(str(child))
            else:
                found.update(_skill_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_skill_ids(child))
    return found


def _probe(
    *, case_id: str, side: str, diagnosis: dict[str, Any], repository: SkillRepository
) -> SkillCandidate:
    repair = diagnosis.get("repair_package") or {}
    mechanism = repair.get("mechanism") if isinstance(repair, dict) else {}
    mechanism_text = json.dumps(mechanism or {}, ensure_ascii=False)
    reason = str(diagnosis.get("reason", ""))
    known = {record.skill_id for record in repository.list_active(side)}
    return SkillCandidate(
        candidate_id=case_id,
        side=side,
        payload=SkillPayload(
            name="Direct contrastive evidence probe",
            description=(mechanism_text or reason)[:2000],
            content=[reason[:2000]] if reason else [],
        ),
        solves=json.dumps(repair, ensure_ascii=False)[:12000],
        related_existing_skill_ids=sorted(_skill_ids(diagnosis) & known),
        transition=str(
            diagnosis.get("transition")
            or (diagnosis.get("flip") or {}).get("direction")
            or ""
        ),
        failure_age=max(0, int(diagnosis.get("failure_age") or 0)),
        maintenance_intent=str(
            diagnosis.get("maintenance_intent_hint") or "REVISE"
        ),
        why_previous_round_failed=str(
            (diagnosis.get("failure_to_repair") or {}).get(
                "why_previous_round_failed", ""
            )
        ),
    )


def _batch(
    *,
    case_id: str,
    side: str,
    diagnosis: dict[str, Any],
    repository: SkillRepository,
    retriever: BatchSkillRetriever,
) -> SkillCandidateBatch:
    # SkillCandidate is used only as an in-memory retrieval query required by
    # the existing hybrid retriever.  It is never saved, generated, or shown to
    # the CRUD model as a proposed Skill.
    probe = _probe(
        case_id=case_id, side=side, diagnosis=diagnosis, repository=repository
    )
    return retriever.retrieve(
        batch_id=f"direct_{case_id}_{side}",
        candidates=[probe],
        repository=repository,
    )


def _validate_policy(plan, repository: SkillRepository, *, bank_cap: int) -> None:
    if len(plan.operations) > 3:
        raise ValueError("Direct CRUD permits at most three primitive operations")
    additions = [item for item in plan.operations if item.operation.value == "add_skill"]
    if len(additions) > 1:
        raise ValueError("Direct CRUD permits at most one new Skill per case side")
    for operation in additions:
        if not operation.skill_id.startswith(f"sk_{plan.side}_"):
            raise ValueError(
                f"New {plan.side} Skill ID must start with sk_{plan.side}_"
            )
    current = len(repository.list_active(plan.side))
    deletes = sum(
        item.operation.value == "delete_skill" for item in plan.operations
    )
    if current + len(additions) - deletes > bank_cap:
        raise ValueError(
            f"{plan.side} Bank cap {bank_cap} would be exceeded; update/merge/NOOP"
        )


def _stage_case(
    *,
    case: dict[str, Any],
    working: SkillRepository,
    retriever: BatchSkillRetriever,
    agent: DirectCaseCrudAgent,
    bank_cap: int,
    max_attempts: int,
) -> tuple[list[tuple[SkillCandidateBatch, Any]], list[dict[str, Any]]]:
    staged: list[tuple[SkillCandidateBatch, Any]] = []
    outcomes: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="mim_direct_case_") as directory:
        trial = SkillRepository(directory)
        trial.seed_initial([copy.deepcopy(item) for item in working.list_active()])
        # Match the working version number so optimistic version checks are
        # identical when the validated plans are committed below.
        trial._bank_version = int(working.current_version.removeprefix("v"))
        trial._save_bank(trial._bank_version, trial.list_active())
        trial._update_selected()

        for projection in case["projections"]:
            diagnosis = projection["package"]
            side = str(diagnosis["side"])
            case_id = f"{case['case_key']}__{side}"
            feedback = ""
            last_error = ""
            for attempt in range(1, max_attempts + 1):
                batch = _batch(
                    case_id=case_id,
                    side=side,
                    diagnosis=diagnosis,
                    repository=trial,
                    retriever=retriever,
                )
                try:
                    plan = agent.plan(
                        case_id=case_id,
                        side=side,
                        direction=case["direction"],
                        diagnosis=diagnosis,
                        batch=batch,
                        official_records=trial.list_active(side),
                        validation_feedback=feedback,
                    )
                    plan.transaction_id = re.sub(
                        r"[^A-Za-z0-9_.-]+",
                        "_",
                        f"tx_direct_{case_id}_{batch.base_bank_version}",
                    )
                    _validate_policy(plan, trial, bank_cap=bank_cap)
                    if plan.operations:
                        SkillCrudExecutor(trial).apply(batch, plan)
                    staged.append((batch, plan))
                    outcomes.append(
                        {
                            "side": side,
                            "stage": diagnosis.get("stage"),
                            "attempt": attempt,
                            "operation_count": len(plan.operations),
                            "resolution": plan.candidate_resolutions[0].resolution,
                            "reason": plan.candidate_resolutions[0].reason,
                            "source": projection["path"],
                        }
                    )
                    break
                except Exception as exc:
                    last_error = str(exc)
                    feedback = last_error[:1000]
                    if "insufficient balance" in last_error.lower():
                        raise
            else:
                if "insufficient balance" in last_error.lower():
                    raise RuntimeError(last_error)
                # A malformed or overlong Skill proposal is a rejected CRUD
                # proposal, not a reason to lose the whole shadow stream.  The
                # diagnosis remains auditable in the per-case progress row.
                outcomes.append(
                    {
                        "side": side,
                        "stage": diagnosis.get("stage"),
                        "attempt": max_attempts,
                        "operation_count": 0,
                        "resolution": "REJECTED",
                        "reason": f"proposal rejected after retries: {last_error}",
                        "source": projection["path"],
                    }
                )
    return staged, outcomes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/qwen3_8b_swap.yaml")
    parser.add_argument("--diagnosis-root", required=True)
    parser.add_argument("--initial-skill-bank-dir", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--prompt", default="prompts/skill_maker/direct_case_crud.md")
    parser.add_argument("--bank-cap", type=int, default=60)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    output = Path(args.output_root).resolve()
    repository_dir = output / "shadow" / "skills"
    progress_path = output / "direct_crud_progress.jsonl"
    summary_path = output / "direct_crud_summary.json"
    _, initial_number, raw_records = SkillBank.read_published_records(
        args.initial_skill_bank_dir
    )
    initial_records = [SkillRecord.from_dict(item) for item in raw_records]
    working = SkillRepository(repository_dir)
    if not working.list_active():
        working.seed_initial([copy.deepcopy(item) for item in initial_records])
    elif not args.resume:
        raise FileExistsError(
            f"Output shadow already exists; use --resume or a new output: {output}"
        )

    cases = _load_cases(Path(args.diagnosis_root))
    if args.max_cases > 0:
        cases = cases[: args.max_cases]
    completed = _load_progress(progress_path)
    embedder = Embedder(
        model_name=config.embedding.model,
        device=config.embedding.device,
        normalize=config.embedding.normalize,
        batch_size=config.embedding.batch_size,
    )
    retriever = BatchSkillRetriever(
        embedder,
        per_candidate_k=8,
        guaranteed_per_candidate=5,
        max_bank_context=config.training.skill_batch_bank_context,
    )
    model_config = copy.deepcopy(config.models["maintenance"])
    model_config.extra_body = {"thinking": {"type": "disabled"}}
    model_config.reasoning_effort = None
    prompt_path = Path(args.prompt)
    if not prompt_path.is_absolute():
        prompt_path = ROOT / prompt_path
    agent = DirectCaseCrudAgent(
        create_client(model_config), prompt=prompt_path.read_text(encoding="utf-8")
    )

    stopped_error = ""
    for index, case in enumerate(cases, start=1):
        if case["case_key"] in completed:
            print(f"[skip] {index}/{len(cases)} {case['case_key']}", flush=True)
            continue
        try:
            staged, outcomes = _stage_case(
                case=case,
                working=working,
                retriever=retriever,
                agent=agent,
                bank_cap=args.bank_cap,
                max_attempts=args.max_attempts,
            )
            before = working.current_version
            for batch, plan in staged:
                if plan.operations:
                    SkillCrudExecutor(working).apply(batch, plan)
            row = {
                "case_key": case["case_key"],
                "direction": case["direction"],
                "status": "ok",
                "bank_before": before,
                "bank_after": working.current_version,
                "projections": outcomes,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            _append_jsonl(progress_path, row)
            completed[case["case_key"]] = row
            operations = sum(item["operation_count"] for item in outcomes)
            print(
                f"[ok] {index}/{len(cases)} {case['case_key']} "
                f"{case['direction']} ops={operations} {working.current_version}",
                flush=True,
            )
        except Exception as exc:
            stopped_error = str(exc)
            print(
                f"[error] {index}/{len(cases)} {case['case_key']}: {stopped_error}",
                flush=True,
            )
            break

    rows = list(completed.values())
    summary = {
        "schema_version": "direct_iteration_crud_v2",
        "initial_bank": str(Path(args.initial_skill_bank_dir).resolve()),
        "diagnosis_root": str(Path(args.diagnosis_root).resolve()),
        "case_total": len(cases),
        "case_completed": len(rows),
        "C2W_completed": sum(item.get("direction") == "C2W" for item in rows),
        "W2C_completed": sum(item.get("direction") == "W2C" for item in rows),
        "W2W_completed": sum(item.get("direction") == "W2W" for item in rows),
        "projection_completed": sum(len(item.get("projections", [])) for item in rows),
        "operation_total": sum(
            projection.get("operation_count", 0)
            for item in rows
            for projection in item.get("projections", [])
        ),
        "noop_total": sum(
            projection.get("operation_count", 0) == 0
            for item in rows
            for projection in item.get("projections", [])
        ),
        "working_version": working.current_version,
        "official_skills": {
            side: len(working.list_active(side))
            for side in ("access", "construction")
        },
        "stopped_error": stopped_error,
        "complete": len(rows) == len(cases) and not stopped_error,
    }
    if summary["complete"]:
        selected = working.select_version(
            int(working.current_version.removeprefix("v"))
        )
        published = SkillBank.export_published(
            selected,
            output / "bank2" / "skills" / "published_bank2_direct",
            bank_number=initial_number + 1,
        )
        summary["published_dir"] = str(published)
    _atomic_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
