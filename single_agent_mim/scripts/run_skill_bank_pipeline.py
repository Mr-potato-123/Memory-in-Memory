"""Judge-first Skill Bank pipeline entry point.

Reads completed diagnosis packages, generates candidates, consolidates
them into official Bank versions, and optionally runs validation and
evaluation.  This is the formal experiment entry point described in
``docs/CLAUDE_SKILL_BANK_END_TO_END_GUIDE.md``.

Usage::

    python scripts/run_skill_bank_pipeline.py ^
      --config configs/qwen3_8b_dashscope.yaml ^
      --diagnosis-run outputs/diagnosis/bank1_train_flash ^
      --output-dir outputs ^
      --run-id bank2_build ^
      --initial-skill-bank-dir ../exp/single-agent/bank1/banks ^
      --workers 4 ^
      --stage all
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.agents.skill_learning import (
    BatchSkillCrudAgent,
    CandidateSkillAgent,
)
from mim.artifacts import RunDir
from mim.config import load_config
from mim.eval.locomo import load_dataset
from mim.retrieval.embedder import Embedder
from mim.skill_maker import (
    BatchSkillRetriever,
    CandidateClusterer,
    SkillBankPipeline,
    SkillCrudExecutor,
    SkillRepository,
    SkillPayloadValidator,
)
from mim.skill_maker.models import SkillCandidate
from mim.skill_maker.repository import SkillRecord
from mim.skill_maker.success_examples import SuccessfulSkillExampleIndex
from mim.skills import SkillBank
from mim.workflows.evaluate import MiMEvaluator

# ── Helpers ─────────────────────────────────────────────────────────


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_directory(path: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if not files:
        raise ValueError(f"Cannot hash empty directory: {path}")
    for item in files:
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _read_prompt(path_str: str) -> str:
    prompt_path = Path(path_str)
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _read_run_json(run_dir: RunDir, relative_path: str) -> Any | None:
    path = run_dir.path / relative_path
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _build_pipeline_event(event_name: str, **kw) -> dict:
    return {"timestamp": _ts(), "event": event_name, **kw}


# ── Input gate ───────────────────────────────────────────────────────


def collect_eligible_packages(
    diagnosis_root: Path, train_ids: list[str]
) -> tuple[list[dict], list[dict]]:
    """Return (access_packages, cons_packages) meeting all input-gate criteria.

    Section 6 of the Skill Bank guide defines the eligibility rules.
    """
    access: list[dict] = []
    cons: list[dict] = []
    seen_diagnosis_ids: set[str] = set()

    package_sources = [
        ("access", "answer_failure", {"ANSWER_FAILURE"}),
        ("access", "access_failure", {"ACCESS_FAILURE"}),
        ("cons", "cons_failure", {"CONS_FAILURE"}),
    ]
    for side, suffix, accepted_types in package_sources:
        packages_dir = diagnosis_root / suffix / "packages"
        if not packages_dir.is_dir():
            continue
        for conv_dir in sorted(packages_dir.iterdir()):
            if not conv_dir.is_dir():
                continue
            for pkg_file in sorted(conv_dir.glob("*.json")):
                try:
                    pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
                except Exception:
                    continue

                # ── Gate checks ──────────────────────────────────
                if pkg.get("diagnosis_type") not in accepted_types:
                    continue
                if pkg.get("status") != "completed":
                    continue
                if pkg.get("problem_found") is not True:
                    continue
                if pkg.get("review_required") is not False:
                    continue
                conv_id = pkg.get("conversation_id", "")
                if conv_id not in train_ids:
                    continue
                diag_id = pkg.get("diagnosis_id", "")
                if diag_id in seen_diagnosis_ids:
                    continue
                seen_diagnosis_ids.add(diag_id)
                pkg["_source_path"] = str(pkg_file.relative_to(diagnosis_root))
                pkg["_source_sha256"] = _sha256_file(pkg_file)
                pkg["_side"] = side

                if pkg["diagnosis_type"] in {
                    "ANSWER_FAILURE", "ACCESS_FAILURE",
                }:
                    access.append(pkg)
                else:
                    cons.append(pkg)

    return access, cons


# ── Candidate generation ────────────────────────────────────────────


def generate_candidates(
    packages: list[dict],
    side: str,
    candidate_agent: CandidateSkillAgent,
    validator: SkillPayloadValidator,
    repository: SkillRepository,
    run_dir: RunDir,
    workers: int,
    resume: bool,
    retry_failures: bool = False,
) -> dict:
    """Generate candidates for one side. Returns outcome counts."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    completed: set[str] = set()
    no_change_dir = run_dir.skills_dir() / "candidates" / side
    no_change_dir.mkdir(parents=True, exist_ok=True)
    no_change_path = no_change_dir / "no_change.jsonl"
    errors_path = no_change_dir / "generation_errors.jsonl"

    if resume:
        # Load already-completed diagnosis IDs from existing candidates
        for candidate in repository.list_candidates(side):
            completed.add(candidate.source_diagnosis_id)
        # Also load no-change records (always skip these)
        if no_change_path.exists():
            for line in no_change_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    completed.add(rec.get("diagnosis_id", ""))
        # Only skip error records if NOT retrying failures
        if not retry_failures and errors_path.exists():
            for line in errors_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    completed.add(rec.get("diagnosis_id", ""))
        if retry_failures:
            print(f"  [{side}] Resume: {len(completed)} already completed "
                  f"(retrying {len([p for p in packages if p['diagnosis_id'] not in completed])} failures)")
            # Clear old error records so re-processed successes aren't listed as both
            if errors_path.exists():
                errors_path.unlink()
        else:
            print(f"  [{side}] Resume: {len(completed)} already processed")

    pending = [p for p in packages if p["diagnosis_id"] not in completed]
    if not pending:
        print(f"  [{side}] All {len(packages)} packages already processed.")
        return {
            "total": len(packages),
            "proposed": 0,
            "no_change": 0,
            "failed": 0,
            "skipped": len(packages),
        }

    proposed = 0
    no_change = 0
    failed = 0
    done = 0

    def _process(pkg: dict) -> dict:
        try:
            candidate = candidate_agent.generate(
                diagnosis=pkg,
                side=side,
            )
        except Exception as exc:
            return {"status": "failed", "error": str(exc), "pkg": pkg}
        return {"status": "generated" if candidate else "no_change",
                "candidate": candidate, "pkg": pkg}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process, p): p for p in pending}
        for future in as_completed(futures):
            result = future.result()
            pkg = result["pkg"]
            diag_id = pkg["diagnosis_id"]

            if result["status"] == "failed":
                failed += 1
                run_dir.append_jsonl(
                    str(errors_path.relative_to(run_dir.path)),
                    {"diagnosis_id": diag_id,
                     "error": result.get("error", "unknown"),
                     "timestamp": _ts()},
                )
            elif result["status"] == "no_change":
                no_change += 1
                run_dir.append_jsonl(
                    str(no_change_path.relative_to(run_dir.path)),
                    {"diagnosis_id": diag_id,
                     "reason": "Candidate Agent returned no change.",
                     "timestamp": _ts()},
                )
            else:
                candidate = result["candidate"]
                # Validate
                is_valid, issues = validator.validate(
                    candidate.payload,
                    side=side,
                    reference_answer=pkg.get("reference_answer", ""),
                    question_entities=[],
                    gold_message_ids=pkg.get("gold_message_ids", []),
                )
                if not is_valid:
                    failed += 1
                    run_dir.append_jsonl(
                        str(errors_path.relative_to(run_dir.path)),
                        {"diagnosis_id": diag_id,
                         "error": f"Validation failed: {'; '.join(issues)}",
                         "timestamp": _ts()},
                    )
                else:
                    repository.save_candidate(candidate)
                    proposed += 1

            done += 1
            if done % 10 == 0:
                print(
                    f"  [{side}] candidates: {done}/{len(pending)} "
                    f"proposed={proposed} no_change={no_change} failed={failed}",
                    flush=True,
                )

    return {
        "total": len(packages),
        "proposed": proposed,
        "no_change": no_change,
        "failed": failed,
        "skipped": len(completed),
    }


# ── Main pipeline ────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Judge-first Skill Bank end-to-end pipeline."
    )
    parser.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    parser.add_argument("--diagnosis-run", required=True,
                        help="Path to a completed diagnosis run directory.")
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--run-id", required=True,
                        help="Unique ID for this Skill run.")
    parser.add_argument(
        "--initial-skill-bank-dir",
        default=None,
        help="Optional directory containing a physically isolated published Bank.",
    )
    parser.add_argument(
        "--successful-skill-traces",
        default=None,
        help=(
            "Optional JSONL of Judge-C Runtime Skill-use trajectories. "
            "When the initial Bank is non-empty, one trajectory is supplied "
            "to candidate generation for scope calibration."
        ),
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--stage", default="all",
                        choices=["candidates", "crud", "validate", "all"])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-failures", action="store_true",
                        help="With --resume, also retry previously failed items.")
    parser.add_argument("--max-items", type=int, default=0,
                        help="Limit candidate generation to N items per side (0 = all).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print eligible package counts and exit.")
    args = parser.parse_args()

    # ── Load config and dataset ─────────────────────────────────
    config = load_config(args.config)
    output_dir = Path(args.output_dir)
    if args.resume:
        run_dir = RunDir(args.run_id, output_dir)
        if not run_dir.path.exists():
            print(f"ERROR: Cannot resume — run directory does not exist: {run_dir.path}")
            return 2
    else:
        run_dir = RunDir.create(args.run_id, output_dir)
    print(f"Run directory: {run_dir.path}")

    conversations, questions = load_dataset(config.dataset.path)
    with open(config.dataset.split, encoding="utf-8") as f:
        split = json.load(f)
    train_ids = split["train"]

    # ── Input gate ──────────────────────────────────────────────
    diagnosis_root = Path(args.diagnosis_run)
    if not diagnosis_root.exists():
        print(f"ERROR: diagnosis run not found: {diagnosis_root}")
        return 2

    access_pkgs, cons_pkgs = collect_eligible_packages(
        diagnosis_root, train_ids,
    )
    print(f"Eligible Access packages:  {len(access_pkgs)}")
    print(f"Eligible Cons packages:   {len(cons_pkgs)}")

    if args.dry_run:
        return 0

    # Write source diagnoses manifest
    source_diagnoses: list[dict] = []
    for pkg in access_pkgs + cons_pkgs:
        source_diagnoses.append({
            "diagnosis_id": pkg["diagnosis_id"],
            "side": pkg["_side"],
            "source_path": pkg["_source_path"],
            "source_sha256": pkg["_source_sha256"],
            "judge_run_id": pkg.get("judge_run_id", ""),
            "diagnosis_run_id": pkg.get("diagnosis_run_id", ""),
            "conversation_id": pkg["conversation_id"],
            "qa_id": pkg["qa_id"],
        })
    run_dir.write_json("source_diagnoses.json", source_diagnoses)

    initial_bank_number = 0
    initial_records: list[dict] = []
    if args.initial_skill_bank_dir:
        _, initial_bank_number, initial_records = (
            SkillBank.read_published_records(args.initial_skill_bank_dir)
        )
    publish_bank_number = initial_bank_number + 1

    success_examples = None
    if args.successful_skill_traces:
        success_examples = SuccessfulSkillExampleIndex.load(
            args.successful_skill_traces
        )
        if success_examples.count() == 0:
            raise ValueError(
                "Successful Skill trajectory index contains no Judge-C "
                "examples with Skill IDs."
            )
        print(
            "Loaded successful Skill trajectories: "
            f"access={success_examples.count('access')} "
            f"construction={success_examples.count('construction')}"
        )

    # Write manifest
    manifest = {
        "run_id": args.run_id,
        "diagnosis_run": str(diagnosis_root.resolve()),
        "diagnosis_run_hash": _sha256(str(diagnosis_root.resolve())),
        "candidate_prompt_access_hash": _sha256_file(
            Path(config.prompts.skill_candidate_generation_access)
        ),
        "candidate_prompt_construction_hash": _sha256_file(
            Path(config.prompts.skill_candidate_generation_construction)
        ),
        "crud_prompt_access_hash": _sha256_file(
            Path(config.prompts.skill_batch_crud_access)
        ),
        "crud_prompt_construction_hash": _sha256_file(
            Path(config.prompts.skill_batch_crud_construction)
        ),
        "config_hash": _sha256(
            json.dumps(config.model_dump(mode="json"), sort_keys=True)
        ),
        "initial_skill_bank_dir": args.initial_skill_bank_dir,
        "initial_bank_hash": (
            _sha256_directory(Path(args.initial_skill_bank_dir))
            if args.initial_skill_bank_dir else None
        ),
        "successful_skill_traces": args.successful_skill_traces,
        "successful_skill_traces_hash": (
            _sha256_file(Path(args.successful_skill_traces))
            if args.successful_skill_traces else None
        ),
        "successful_skill_trace_counts": (
            {
                "access": success_examples.count("access"),
                "construction": success_examples.count("construction"),
            }
            if success_examples is not None else None
        ),
        "publish_bank": f"bank{publish_bank_number}",
        "dataset_sha256": split.get("dataset_sha256", ""),
        "created_at": _ts(),
    }
    run_dir.write_json("manifest.json", manifest)

    # ── Build components ────────────────────────────────────────
    from mim.llm import create_client
    import copy

    # Candidate generation: use full maintenance config (reasoning ok)
    candidate_model = create_client(config.models["maintenance"])

    # CRUD planning: lighter config without deep reasoning
    crud_config = copy.deepcopy(config.models["maintenance"])
    crud_config.reasoning_effort = None
    # DeepSeek V4 defaults to thinking enabled, so an empty extra_body does
    # not disable it.  CRUD is a schema-constrained consolidation task and is
    # intentionally run in Flash non-thinking mode.
    crud_config.extra_body = {"thinking": {"type": "disabled"}}
    crud_config.temperature = 0.0
    crud_config.max_tokens = 4000
    crud_config.timeout_seconds = 300
    crud_config.max_retries = 2
    crud_model = create_client(crud_config)

    embedder = Embedder(
        model_name=config.embedding.model,
        device=config.embedding.device,
    )

    candidate_agent_access = CandidateSkillAgent(
        candidate_model,
        prompt=_read_prompt(config.prompts.skill_candidate_generation_access),
        success_examples=success_examples,
    )
    candidate_agent_construction = CandidateSkillAgent(
        candidate_model,
        prompt=_read_prompt(config.prompts.skill_candidate_generation_construction),
        success_examples=success_examples,
    )
    batch_crud_agent_access = BatchSkillCrudAgent(
        crud_model,
        prompt=_read_prompt(config.prompts.skill_batch_crud_access),
    )
    batch_crud_agent_construction = BatchSkillCrudAgent(
        crud_model,
        prompt=_read_prompt(config.prompts.skill_batch_crud_construction),
    )
    validator = SkillPayloadValidator()
    repository = SkillRepository(run_dir.skills_dir())

    # Seed a prior published Bank as one immutable version-zero snapshot.
    if initial_records:
        if repository.current_version == "v000" and not repository.list_active():
            repository.seed_initial(
                [SkillRecord.from_dict(item) for item in initial_records]
            )
            print(
                f"Seeded Bank{initial_bank_number}: "
                f"{len(initial_records)} Skills"
            )
        elif not args.resume:
            print("ERROR: Initial Bank only allowed in an empty run.")
            return 2
        else:
            print(
                f"Reusing seeded Bank{initial_bank_number} during resume: "
                f"repository={repository.current_version}"
            )

    # Stage A: Candidate generation
    if args.stage in ("candidates", "all"):
        print("\n── Stage A: Candidate Generation ──")
        if args.max_items > 0:
            access_pkgs = access_pkgs[:args.max_items]
            cons_pkgs = cons_pkgs[:args.max_items]
            print(f"  (limited to {args.max_items} per side)")
        access_outcome = generate_candidates(
            access_pkgs, "access", candidate_agent_access, validator,
            repository, run_dir, args.workers, args.resume,
            retry_failures=args.retry_failures,
        )
        cons_outcome = generate_candidates(
            cons_pkgs, "construction", candidate_agent_construction, validator,
            repository, run_dir, args.workers, args.resume,
            retry_failures=args.retry_failures,
        )

        total_proposed = access_outcome["proposed"] + cons_outcome["proposed"]
        total_no_change = access_outcome["no_change"] + cons_outcome["no_change"]
        total_failed = access_outcome["failed"] + cons_outcome["failed"]
        print(
            f"\nCandidate generation complete: "
            f"proposed={total_proposed} no_change={total_no_change} "
            f"failed={total_failed}"
        )

        run_dir.append_jsonl("events.jsonl",
            _build_pipeline_event(
                "input_gate_completed",
                access_packages=len(access_pkgs),
                cons_packages=len(cons_pkgs),
            ))
        run_dir.append_jsonl("events.jsonl",
            _build_pipeline_event(
                "candidate_started",
                access=access_outcome,
                cons=cons_outcome,
            ))

    # Stages B-E: Consolidation (clustering, retrieval, CRUD, publication)
    if args.stage in ("crud", "all"):
        print("\n── Stages B-E: Clustering, Retrieval, CRUD ──")
        clusterer = CandidateClusterer(
            embedder,
            target_cluster_size=config.training.skill_cluster_target_size,
            max_batch_size=config.training.skill_crud_batch_size,
        )
        retriever = BatchSkillRetriever(
            embedder,
            max_bank_context=config.training.skill_batch_bank_context,
        )
        executor = SkillCrudExecutor(repository)
        pipeline = SkillBankPipeline(
            repository=repository,
            clusterer=clusterer,
            retriever=retriever,
            executor=executor,
            run_id=args.run_id,
            min_candidate_support=(
                config.training.skill_min_candidate_support
            ),
        )

        for side in ("access", "construction"):
            release_path = (
                run_dir.skills_dir()
                / "transactions"
                / f"tx_{side}_{args.run_id}_release.json"
            )
            if args.resume and release_path.exists():
                release = json.loads(release_path.read_text(encoding="utf-8"))
                published_version = str(
                    release.get("published_bank_version", "")
                )
                published_snapshot = (
                    run_dir.skills_dir()
                    / "official"
                    / "banks"
                    / f"bank_{published_version}.json"
                )
                if not published_version or not published_snapshot.exists():
                    raise RuntimeError(
                        f"Incomplete {side} release during resume: "
                        f"{release_path} points to missing snapshot "
                        f"{published_snapshot}."
                    )
                print(
                    f"  [{side}] reusing published release "
                    f"{published_version}"
                )
                continue

            _crud_agent = (
                batch_crud_agent_access if side == "access"
                else batch_crud_agent_construction
            )
            outcome = pipeline.consolidate(
                side=side,
                batch_crud_agent=_crud_agent,
                artifact_writer=(
                    lambda path, data: run_dir.write_json(path, data)
                ),
                artifact_reader=(
                    lambda path: _read_run_json(run_dir, path)
                    if args.resume else None
                ),
            )
            print(
                f"  [{side}] published={outcome['published']} "
                f"version={outcome['new_version']} "
                f"accepted={outcome['accepted']} rejected={outcome['rejected']}"
            )
            for err in outcome.get("errors", []):
                run_dir.append_jsonl(
                    f"skills/transactions/{side}/errors.jsonl", err,
                )
            for rejection in outcome.get("quality_rejections", []):
                run_dir.append_jsonl(
                    f"skills/transactions/{side}/quality_rejections.jsonl",
                    rejection,
                )
            if outcome["published"]:
                run_dir.append_jsonl("events.jsonl",
                    _build_pipeline_event("side_release_published",
                        side=side, version=outcome["new_version"]))

    # Stage F: Validation and selection
    if args.stage in ("validate", "all"):
        print("\n── Stage F: Validation & Selection ──")
        validation_ids = split["validation"]
        current = int(repository.current_version.removeprefix("v"))
        selected_file = run_dir.skills_dir() / "official" / "selected.json"
        original_version = 0
        if selected_file.exists():
            original_version = int(
                json.loads(selected_file.read_text(encoding="utf-8")).get(
                    "version", 0
                )
            )
        best_version: int | None = None
        best_key = (-1.0, -1.0, -1.0)
        scores: list[dict] = []
        runtime_client = create_client(config.models["runtime"])
        project_dir = Path(__file__).resolve().parents[1]
        config_path = str(Path(args.config).resolve())

        try:
            for version in range(current + 1):
                selected_path = repository.select_version(version)
                selected_data = json.loads(
                    selected_path.read_text(encoding="utf-8")
                )
                runtime_bank = SkillBank.from_records(
                    selected_data.get("skills", []),
                    bank_name=f"training_candidate_{version:03d}",
                )
                validation_name = f"{args.run_id}_validation_v{version:03d}"
                validation_path = output_dir / validation_name
                validation_summary = validation_path / "summary.json"
                predictions_path = validation_path / "qa_results.jsonl"

                if (
                    args.resume
                    and validation_summary.exists()
                    and predictions_path.exists()
                ):
                    report_data = json.loads(
                        validation_summary.read_text(encoding="utf-8")
                    )
                    print(f"  v{version:03d}: reusing Runtime validation")
                else:
                    if validation_path.exists():
                        raise RuntimeError(
                            "Incomplete validation directory exists: "
                            f"{validation_path}. Use a new run-id."
                        )
                    validation_run = RunDir.create(
                        validation_name,
                        output_dir,
                    )
                    evaluator = MiMEvaluator(
                        config,
                        validation_run,
                        runtime_model=runtime_client,
                        embedder=embedder,
                    )
                    report = evaluator.evaluate(
                        conversations=conversations,
                        questions=questions,
                        eval_ids=validation_ids,
                        mode="mim",
                        runtime_skill_bank=runtime_bank,
                        split_name="validation",
                    )
                    report_data = report.model_dump(mode="json")

                total = int(report_data.get("total_qa", 0))
                overall_f1 = float(report_data.get("overall_f1", 0.0))
                judge_dir = output_dir / f"{validation_name}_judge"
                judge_summary_path = judge_dir / "summary.json"
                command = [
                    sys.executable,
                    str(project_dir / "scripts" / "judge_predictions.py"),
                    "--config", config_path,
                    "--judge-model", config.models["maintenance"].model,
                    "--batch-size", "4",
                    "--workers", str(max(1, args.workers)),
                    "--output-dir", str(judge_dir.resolve()),
                ]
                if judge_dir.exists():
                    command.append("--resume")
                command.append(str(predictions_path.resolve()))
                if not judge_summary_path.exists():
                    completed = subprocess.run(
                        command,
                        cwd=project_dir,
                        check=False,
                    )
                    if completed.returncode != 0:
                        raise RuntimeError(
                            "Semantic Judge failed for Bank "
                            f"v{version:03d} with exit code "
                            f"{completed.returncode}"
                        )

                judge_summary = json.loads(
                    judge_summary_path.read_text(encoding="utf-8")
                )
                if judge_summary.get("judge_model") != (
                    config.models["maintenance"].model
                ):
                    raise RuntimeError(
                        f"Judge model mismatch for v{version:03d}: "
                        f"{judge_summary.get('judge_model')} != "
                        f"{config.models['maintenance'].model}"
                    )
                labels = judge_summary.get("labels", {})
                judged_total = sum(
                    int(labels.get(label, 0)) for label in ("C", "P", "I")
                )
                if total <= 0 or judged_total != total:
                    raise RuntimeError(
                        f"Incomplete validation Judge for v{version:03d}: "
                        f"{judged_total}/{total}"
                    )
                c_count = int(labels.get("C", 0))
                p_count = int(labels.get("P", 0))
                i_count = int(labels.get("I", 0))
                c_rate = c_count / total
                i_rate = i_count / total
                selection_key = (c_rate, -i_rate, overall_f1)

                scores.append({
                    "bank_version": version,
                    "judge_counts": {
                        "C": c_count,
                        "P": p_count,
                        "I": i_count,
                    },
                    "c_rate": c_rate,
                    "i_rate": i_rate,
                    "overall_f1": overall_f1,
                    "total_qa": total,
                    "runtime_run": str(validation_path),
                    "judge_run": str(judge_dir),
                })
                print(
                    f"  v{version:03d}: C={c_rate:.4f} I={i_rate:.4f} "
                    f"F1={overall_f1:.4f}"
                )
                if best_version is None or selection_key > best_key:
                    best_version = version
                    best_key = selection_key

                run_dir.append_jsonl(
                    "events.jsonl",
                    _build_pipeline_event(
                        "validation_version_completed",
                        version=version,
                        c_rate=c_rate,
                        i_rate=i_rate,
                        overall_f1=overall_f1,
                    ),
                )
        except Exception:
            repository.select_version(original_version)
            raise

        if best_version is None:
            repository.select_version(original_version)
            raise RuntimeError("No complete Bank version passed validation")
        selected_path = repository.select_version(best_version)
        SkillBank.export_published(
            selected_path,
            run_dir.skills_dir() / f"published_bank{publish_bank_number}",
            bank_number=publish_bank_number,
        )
        run_dir.write_json("skills/selection.json", {
            "selected_version": best_version,
            "selection_criteria": "highest C rate, then lowest I rate, then highest F1",
            "scores": scores,
        })
        print(f"\nSelected Bank version: v{best_version:03d}")

        # Summary
        summary = {
            "run_id": args.run_id,
            "diagnosis_run": str(diagnosis_root.resolve()),
            "eligible_access": len(access_pkgs),
            "eligible_cons": len(cons_pkgs),
            "selected_version": best_version,
            "scores": scores,
            "completed_at": _ts(),
        }
        run_dir.write_json("summary.json", summary)

    print(f"\nPipeline complete. Output: {run_dir.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
