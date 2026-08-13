"""Draft-first Skill Bank consolidation.

Flow per side:
diagnosis candidates -> semantic clusters -> 1-5 drafts per cluster ->
draft-at-a-time CRUD against the latest side-local working Bank -> one atomic
formal release.  Access and Construction use physically separate working
repositories, so they may execute concurrently without Bank-version races.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.agents.skill_learning import BatchSkillCrudAgent, CandidateSkillAgent
from mim.artifacts import RunDir
from mim.config import load_config
from mim.llm import create_client
from mim.retrieval.embedder import Embedder
from mim.skill_maker import SkillRepository
from mim.skill_maker.batch import BatchSkillRetriever, SkillCrudExecutor
from mim.skill_maker.cluster_v2 import cluster_v2
from mim.skill_maker.models import (
    CandidateResolution,
    SkillBatchPlan,
    SkillCandidate,
    SkillOperation,
    SkillOperationType,
    SkillPayload,
)
from mim.skill_maker.repository import SkillRecord
from mim.skill_maker.validator import SkillPayloadValidator
from mim.skills import SkillBank


def _read(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _stable_digest(value: Any, length: int = 12) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_source_candidates(root: Path, side: str) -> list[SkillCandidate]:
    side_dir = root / side
    candidates: list[SkillCandidate] = []
    for path in sorted(side_dir.glob("*/candidate.json")):
        candidate = SkillCandidate(**json.loads(path.read_text(encoding="utf-8")))
        # A previous V2 run stored drafts beside source candidates.  Never
        # summarize those drafts again.
        if (
            candidate.side == side
            and candidate.candidate_id.startswith(f"cand_{side}_")
            and not candidate.source_cluster_id
        ):
            candidates.append(candidate)
    ids = [candidate.candidate_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate {side} source candidate IDs")
    return candidates


def _cluster_id(side: str, index: int, cluster: list[SkillCandidate]) -> str:
    return (
        f"cluster_{side}_{index:04d}_"
        f"{_stable_digest(sorted(item.candidate_id for item in cluster), 10)}"
    )


def _parse_cluster_summary(
    data: dict[str, Any],
    *,
    side: str,
    cluster_id: str,
    candidates: list[SkillCandidate],
) -> tuple[list[SkillCandidate], list[dict[str, str]]]:
    skills = data.get("skills")
    rejected = data.get("rejected_candidates", [])
    # An empty skills list is legal when the summarizer rejects the whole
    # cluster (conservative prompt can legitimately decline every candidate).
    # Coverage is then verified below: every candidate must be accounted for
    # either as a draft's source or as a rejected candidate.
    if not isinstance(skills, list) or not 0 <= len(skills) <= 5:
        raise ValueError("skills must contain between 0 and 5 draft Skills")
    if not isinstance(rejected, list):
        raise ValueError("rejected_candidates must be a list")

    allowed = {candidate.candidate_id for candidate in candidates}
    rejected_rows: list[dict[str, str]] = []
    rejected_ids: set[str] = set()
    for item in rejected:
        if not isinstance(item, dict):
            raise ValueError("each rejected candidate must be an object")
        candidate_id = str(item.get("candidate_id", ""))
        if candidate_id not in allowed:
            raise ValueError(f"unknown rejected candidate ID: {candidate_id}")
        rejected_ids.add(candidate_id)
        rejected_rows.append(
            {"candidate_id": candidate_id, "reason": str(item.get("reason", ""))}
        )

    validator = SkillPayloadValidator()
    drafts: list[SkillCandidate] = []
    covered: set[str] = set()
    for index, item in enumerate(skills):
        if not isinstance(item, dict):
            raise ValueError("each draft Skill must be an object")
        source_ids = list(
            dict.fromkeys(str(value) for value in item.get("source_candidate_ids", []))
        )
        if not source_ids:
            raise ValueError(f"draft {index} has no source_candidate_ids")
        unknown = set(source_ids) - allowed
        if unknown:
            raise ValueError(f"draft {index} contains unknown source IDs: {unknown}")
        if set(source_ids) & rejected_ids:
            raise ValueError("a candidate cannot be both used and rejected")
        payload = SkillPayload(
            name=item.get("name", ""),
            description=item.get("description", ""),
            content=item.get("content", []),
        )
        valid, errors = validator.validate(payload, side=side)
        solves = str(item.get("solves", "")).strip()
        if not solves:
            errors.append("solves is empty")
            valid = False
        if len(solves) > 600:
            errors.append("solves is longer than 600 characters")
            valid = False
        if not valid:
            raise ValueError(f"invalid draft {index}: {'; '.join(errors)}")
        draft_key = {
            "cluster_id": cluster_id,
            "source_ids": sorted(source_ids),
            "payload": payload.model_dump(mode="json"),
            "solves": solves,
        }
        digest = _stable_digest(draft_key)
        source_candidates = [
            candidate
            for candidate in candidates
            if candidate.candidate_id in source_ids
        ]
        transitions = sorted({
            candidate.transition
            for candidate in source_candidates
            if candidate.transition
        })
        intents = {
            candidate.maintenance_intent for candidate in source_candidates
        }
        # A semantic cluster may combine add/revise evidence.  Escalating a
        # mixed cluster to REMOVE would be unsafe, so mixed intent is carried
        # forward as REVISE for the CRUD planner to adjudicate atomically.
        maintenance_intent = (
            next(iter(intents)) if len(intents) == 1 else "REVISE"
        )
        why_previous = list(dict.fromkeys(
            candidate.why_previous_round_failed
            for candidate in source_candidates
            if candidate.why_previous_round_failed
        ))
        drafts.append(
            SkillCandidate(
                candidate_id=f"draft_{side}_{digest}",
                skill_id=f"sk_{side}_draft_{digest[:10]}",
                side=side,
                payload=payload,
                solves=solves,
                source_candidate_ids=source_ids,
                source_cluster_id=cluster_id,
                source_failure_id="cluster_summary",
                transition=",".join(transitions),
                failure_age=max(
                    (candidate.failure_age for candidate in source_candidates),
                    default=0,
                ),
                maintenance_intent=maintenance_intent,
                why_previous_round_failed=" | ".join(why_previous)[:600],
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        )
        covered.update(source_ids)

    missing = allowed - covered - rejected_ids
    if missing:
        raise ValueError(f"cluster summary omitted candidate IDs: {sorted(missing)}")
    return drafts, rejected_rows


def _summarize_cluster(
    model: Any,
    prompt: str,
    candidates: list[SkillCandidate],
    side: str,
    cluster_id: str,
) -> tuple[list[SkillCandidate], list[dict[str, str]], dict[str, Any]]:
    candidate_rows = [
        {
            "candidate_id": candidate.candidate_id,
            "name": candidate.payload.name,
            "description": candidate.payload.description,
            "solves": candidate.solves,
            "content": candidate.payload.content,
        }
        for candidate in candidates
    ]
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "side": side,
                    "cluster_id": cluster_id,
                    "cluster_size": len(candidates),
                    "candidates": candidate_rows,
                },
                ensure_ascii=False,
            ),
        },
    ]
    last_error = ""
    for attempt in range(3):
        response = model.generate(
            messages,
            temperature=0.0,
            max_tokens=12000,
            json_mode=True,
        )
        data = CandidateSkillAgent._parse_json(response.text)
        try:
            drafts, rejected = _parse_cluster_summary(
                data,
                side=side,
                cluster_id=cluster_id,
                candidates=candidates,
            )
            return drafts, rejected, data
        except ValueError as exc:
            last_error = str(exc)
            if attempt < 2:
                messages.extend(
                    [
                        {"role": "assistant", "content": response.text},
                        {
                            "role": "user",
                            "content": (
                                "Repair only the JSON contract and candidate "
                                f"coverage errors, then return the full object: {last_error}"
                            ),
                        },
                    ]
                )
    raise ValueError(f"{cluster_id} summary failed: {last_error}")


def _generate_drafts(
    *,
    side: str,
    candidates: list[SkillCandidate],
    clusters: list[list[SkillCandidate]],
    model: Any,
    prompt: str,
    drafts_root: Path,
    workers: int,
    resume: bool,
) -> tuple[list[SkillCandidate], list[dict[str, str]]]:
    results: dict[int, tuple[list[SkillCandidate], list[dict[str, str]]]] = {}
    pending: dict[Any, tuple[int, str, Path]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for index, cluster in enumerate(clusters):
            cluster_id = _cluster_id(side, index, cluster)
            artifact = drafts_root / side / cluster_id / "summary.json"
            if resume and artifact.exists():
                saved = json.loads(artifact.read_text(encoding="utf-8"))
                results[index] = (
                    [SkillCandidate(**item) for item in saved.get("drafts", [])],
                    list(saved.get("rejected_candidates", [])),
                )
                continue
            future = pool.submit(
                _summarize_cluster,
                model,
                prompt,
                cluster,
                side,
                cluster_id,
            )
            pending[future] = (index, cluster_id, artifact)

        for future in as_completed(pending):
            index, cluster_id, artifact = pending[future]
            try:
                drafts, rejected, raw = future.result()
            except ValueError as exc:
                # A cluster that the summarizer cannot produce a valid
                # summary for is dropped wholesale: its candidates are
                # skipped rather than crashing the whole bank build.
                print(f"[warn] {cluster_id} summary failed after retries: "
                      f"{exc}; dropping cluster candidates", flush=True)
                _atomic_json(
                    artifact,
                    {
                        "cluster_id": cluster_id,
                        "source_candidate_ids": [
                            candidate.candidate_id for candidate in clusters[index]
                        ],
                        "drafts": [],
                        "rejected_candidates": [],
                        "raw_summary": {},
                        "error": str(exc)[:200],
                    },
                )
                results[index] = ([], [])
                continue
            _atomic_json(
                artifact,
                {
                    "cluster_id": cluster_id,
                    "source_candidate_ids": [
                        candidate.candidate_id for candidate in clusters[index]
                    ],
                    "drafts": [draft.model_dump(mode="json") for draft in drafts],
                    "rejected_candidates": rejected,
                    "raw_summary": raw,
                },
            )
            results[index] = (drafts, rejected)

    drafts = [draft for index in sorted(results) for draft in results[index][0]]
    rejected = [row for index in sorted(results) for row in results[index][1]]
    return drafts, rejected


def _working_transaction_path(repository: SkillRepository, transaction_id: str) -> Path:
    return repository.directory / "transactions" / f"{transaction_id}.json"


def _process_side(
    *,
    side: str,
    drafts: list[SkillCandidate],
    working_dir: Path,
    initial_records: list[SkillRecord],
    embedder: Embedder,
    crud_config: Any,
    crud_prompt: str,
    run_id: str,
    resume: bool,
) -> dict[str, Any]:
    repository = SkillRepository(working_dir)
    if initial_records and repository.current_version == "v000" and not repository.list_active():
        repository.seed_initial([copy.deepcopy(record) for record in initial_records])
    crud_model = create_client(crud_config)
    crud_agent = BatchSkillCrudAgent(crud_model, prompt=crud_prompt)
    retriever = BatchSkillRetriever(
        embedder,
        per_candidate_k=5,
        guaranteed_per_candidate=3,
        max_bank_context=12,
    )
    executor = SkillCrudExecutor(repository)
    decisions_dir = working_dir / "decisions"
    processed = 0
    rejected = 0
    crud_errors: list[dict[str, str]] = []
    current_cluster_id = ""
    cluster_touched_skill_ids: set[str] = set()

    for index, draft in enumerate(drafts):
        if draft.source_cluster_id != current_cluster_id:
            current_cluster_id = draft.source_cluster_id
            cluster_touched_skill_ids = set()
        transaction_id = f"tx_{side}_{run_id}_{index:04d}_{draft.candidate_id[-8:]}"
        transaction_path = _working_transaction_path(repository, transaction_id)
        decision_path = decisions_dir / f"{transaction_id}.json"
        if resume and (transaction_path.exists() or decision_path.exists()):
            processed += 1
            continue

        last_error = ""
        for attempt in range(3):
            batch = retriever.retrieve(
                batch_id=f"{side}_{run_id}_{index:04d}",
                candidates=[draft],
                repository=repository,
                excluded_skill_ids=cluster_touched_skill_ids,
            )
            try:
                plan = crud_agent.plan(
                    batch=batch,
                    official_records=repository.list_active(side),
                    validation_feedback=last_error,
                )
                plan.transaction_id = transaction_id
                if not plan.operations:
                    # A no-mutation resolution is still an auditable decision,
                    # but it must not create a meaningless Bank version.
                    if len(plan.candidate_resolutions) != 1 or (
                        plan.candidate_resolutions[0].candidate_id
                        != draft.candidate_id
                    ):
                        raise ValueError("draft requires exactly one CRUD resolution")
                    _atomic_json(
                        decision_path,
                        {
                            "batch": batch.model_dump(mode="json"),
                            "plan": plan.model_dump(mode="json"),
                            "working_bank_version": repository.current_version,
                        },
                    )
                    rejected += int(
                        plan.candidate_resolutions[0].resolution
                        in {"REJECTED", "NOT_A_SKILL_PROBLEM"}
                    )
                else:
                    before = {
                        record.skill_id: record.to_dict()
                        for record in repository.list_active(side)
                    }
                    try:
                        executor.apply(batch, plan)
                    except (ValueError, RuntimeError) as apply_error:
                        # A merge that would exceed the deterministic payload
                        # limit is not a reason to lose this candidate.  Keep
                        # the candidate as its own narrowly-scoped Skill.  The
                        # model may have proposed an over-broad merge, while
                        # this fallback preserves the already consolidated
                        # draft and keeps the bank executable.
                        error_text = str(apply_error).lower()
                        fallback_errors = (
                            "content too long",
                            "content too many items",
                            "description is empty",
                            "description lacks trigger condition",
                            "description too long",
                            "name is empty",
                            "content is empty",
                            "expected content is no longer present",
                            "overlapping edits",
                        )
                        if not any(marker in error_text for marker in fallback_errors):
                            raise
                        fallback = SkillBatchPlan(
                            transaction_id=transaction_id,
                            side=side,
                            base_bank_version=batch.base_bank_version,
                            candidate_resolutions=[
                                CandidateResolution(
                                    candidate_id=draft.candidate_id,
                                    resolution="CREATED",
                                    target_skill_ids=[],
                                    reason=(
                                        "Created separately because the CRUD "
                                        "plan was not safely applicable to the "
                                        "current Bank snapshot."
                                    ),
                                )
                            ],
                            operations=[
                                SkillOperation(
                                    operation=SkillOperationType.ADD_SKILL,
                                    skill_id=draft.skill_id,
                                    side=side,
                                    name=draft.payload.name,
                                    description=draft.payload.description,
                                    content=draft.payload.content,
                                    source_candidate_ids=[draft.candidate_id],
                                    reason=(
                                        "Preserve a distinct mechanism rather "
                                        "than over-expanding an existing Skill."
                                    ),
                                )
                            ],
                        )
                        executor.apply(batch, fallback)
                        plan = fallback
                    after = {
                        record.skill_id: record.to_dict()
                        for record in repository.list_active(side)
                    }
                    cluster_touched_skill_ids.update(
                        skill_id
                        for skill_id, record in after.items()
                        if before.get(skill_id) != record
                    )
                processed += 1
                break
            except Exception as exc:
                last_error = str(exc)
                if attempt == 2:
                    # One malformed/over-broad CRUD plan must not abort the
                    # entire side.  Keep an auditable rejection and continue
                    # with the remaining drafts; formal release only sees
                    # successfully applied working-bank records.
                    error = {
                        "side": side,
                        "candidate_id": draft.candidate_id,
                        "source_cluster_id": draft.source_cluster_id,
                        "error": last_error[:500],
                    }
                    crud_errors.append(error)
                    _atomic_json(
                        decisions_dir / f"{transaction_id}.error.json",
                        error,
                    )
                    rejected += 1
                    break

    return {
        "side": side,
        "drafts": len(drafts),
        "processed": processed,
        "rejected": rejected,
        "errors": crud_errors,
        "working_version": repository.current_version,
        "records": repository.list_active(side),
    }


def _formal_release(
    *,
    repository: SkillRepository,
    side_results: dict[str, dict[str, Any]],
    run_id: str,
) -> str:
    initial = {record.skill_id: record for record in repository.list_active()}
    final_records = {
        record.skill_id: record
        for side in ("access", "construction")
        for record in side_results[side]["records"]
    }
    staged: list[SkillRecord] = []
    for skill_id, record in final_records.items():
        previous = initial.get(skill_id)
        if previous is None or previous.to_dict() != record.to_dict():
            staged.append(copy.deepcopy(record))
    tombstones = sorted(set(initial) - set(final_records))
    if not staged and not tombstones:
        return repository.current_version
    return repository.publish_batch(
        staged,
        transaction_id=f"tx_{run_id}_formal_release",
        tombstone_skill_ids=tombstones,
        transaction_payload={
            "access_working_version": side_results["access"]["working_version"],
            "construction_working_version": side_results["construction"][
                "working_version"
            ],
            "draft_at_a_time": True,
        },
    )


def _export_variants(
    *,
    run_dir: RunDir,
    side_results: dict[str, dict[str, Any]],
    initial_records: list[SkillRecord],
    bank_number: int,
    selected_snapshot: Path,
) -> dict[str, str]:
    """Export full and side-only ablations for held-out selection."""
    initial_by_side = {
        side: [record for record in initial_records if record.side == side]
        for side in ("access", "construction")
    }
    variants: dict[str, list[SkillRecord]] = {
        "access_only": [
            *side_results["access"]["records"],
            *initial_by_side["construction"],
        ],
        "construction_only": [
            *initial_by_side["access"],
            *side_results["construction"]["records"],
        ],
    }
    exported = {
        "full": str(
            SkillBank.export_published(
                selected_snapshot,
                run_dir.skills_dir() / f"published_bank{bank_number}_full",
                bank_number=bank_number,
            )
        )
    }
    for name, records in variants.items():
        snapshot = run_dir.skills_dir() / "variants" / f"{name}.json"
        _atomic_json(
            snapshot,
            {
                "version": 1,
                "created_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
                "previous_version": 0,
                "skills": [record.to_dict() for record in records],
            },
        )
        exported[name] = str(
            SkillBank.export_published(
                snapshot,
                run_dir.skills_dir()
                / f"published_bank{bank_number}_{name}",
                bank_number=bank_number,
            )
        )
    return exported


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/qwen3_8b_dashscope.yaml")
    parser.add_argument("--source-candidates", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root",
                        help="Override config.output_dir as the run root.")
    parser.add_argument("--initial-skill-bank-dir")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--target-cluster-size", type=int, default=8)
    parser.add_argument("--max-cluster-size", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    output_root = args.output_root or config.output_dir
    if args.resume:
        run_dir = RunDir(args.run_id, output_root)
        if not run_dir.path.exists():
            raise FileNotFoundError(
                f"Cannot resume missing run directory: {run_dir.path}"
            )
    else:
        run_dir = RunDir.create(args.run_id, output_root)
    repository = SkillRepository(run_dir.skills_dir())
    source_root = Path(args.source_candidates)
    embedder = Embedder(model_name=config.embedding.model, device=config.embedding.device)

    initial_records: list[SkillRecord] = []
    initial_bank_number = 0
    if args.initial_skill_bank_dir:
        _, initial_bank_number, raw_records = SkillBank.read_published_records(
            args.initial_skill_bank_dir
        )
        initial_records = [SkillRecord.from_dict(item) for item in raw_records]
        if repository.current_version == "v000" and not repository.list_active():
            repository.seed_initial([copy.deepcopy(record) for record in initial_records])

    candidates = {
        side: _load_source_candidates(source_root, side)
        for side in ("access", "construction")
    }
    for side_candidates in candidates.values():
        for candidate in side_candidates:
            repository.save_candidate(candidate)

    clusters = {
        side: cluster_v2(
            candidates[side],
            embedder,
            target_cluster_size=args.target_cluster_size,
            max_cluster_size=args.max_cluster_size,
        )
        for side in ("access", "construction")
    }
    print(
        "Semantic clusters: "
        + ", ".join(
            f"{side}={len(groups)} sizes={[len(g) for g in groups]}"
            for side, groups in clusters.items()
        ),
        flush=True,
    )

    # Cluster summarization is a strict JSON task just like CRUD.  DeepSeek
    # V4 does not honor temperature while thinking is enabled, and its
    # reasoning tokens can consume the response budget before the draft is
    # emitted.  Use the same explicit disabled-thinking contract as the
    # diagnosis/Judge path.
    summarizer_config = copy.deepcopy(config.models["maintenance"])
    summarizer_config.extra_body = {"thinking": {"type": "disabled"}}
    summarizer_config.reasoning_effort = None
    summarizer_config.max_tokens = min(summarizer_config.max_tokens, 3000)
    summarizer_model = create_client(summarizer_config)
    drafts: dict[str, list[SkillCandidate]] = {}
    rejected: dict[str, list[dict[str, str]]] = {}
    for side in ("access", "construction"):
        prompt_path = (
            config.prompts.skill_cluster_summarizer_access
            if side == "access"
            else config.prompts.skill_cluster_summarizer_construction
        )
        drafts[side], rejected[side] = _generate_drafts(
            side=side,
            candidates=candidates[side],
            clusters=clusters[side],
            model=summarizer_model,
            prompt=_read(prompt_path),
            drafts_root=run_dir.skills_dir() / "drafts",
            workers=args.workers,
            resume=args.resume,
        )
        print(
            f"[{side}] {len(candidates[side])} candidates -> "
            f"{len(clusters[side])} semantic clusters -> {len(drafts[side])} drafts; "
            f"rejected_sources={len(rejected[side])}",
            flush=True,
        )

    crud_config = copy.deepcopy(config.models["maintenance"])
    crud_config.extra_body = {"thinking": {"type": "disabled"}}
    crud_config.reasoning_effort = None
    initial_by_side = {
        side: [record for record in initial_records if record.side == side]
        for side in ("access", "construction")
    }
    side_results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_to_side = {}
        for side in ("access", "construction"):
            crud_prompt = _read(
                config.prompts.skill_batch_crud_access
                if side == "access"
                else config.prompts.skill_batch_crud_construction
            )
            future = pool.submit(
                _process_side,
                side=side,
                drafts=drafts[side],
                working_dir=run_dir.skills_dir() / "working" / side,
                initial_records=initial_by_side[side],
                embedder=embedder,
                crud_config=copy.deepcopy(crud_config),
                crud_prompt=crud_prompt,
                run_id=args.run_id,
                resume=args.resume,
            )
            future_to_side[future] = side
        for future in as_completed(future_to_side):
            side = future_to_side[future]
            try:
                side_results[side] = future.result()
            except Exception as exc:
                # Preserve the completed side and make a hard failure
                # explicit; the final summary will refuse publication unless
                # both sides return a result.
                side_results[side] = {
                    "side": side,
                    "drafts": len(drafts[side]),
                    "processed": 0,
                    "rejected": len(drafts[side]),
                    "errors": [{"side": side, "error": str(exc)[:500]}],
                    "working_version": "v000",
                    "records": [],
                }
                print(f"[{side}] CRUD side failed: {exc}", flush=True)
            print(
                f"[{side}] CRUD complete: drafts={side_results[side]['drafts']} "
                f"skills={len(side_results[side]['records'])} "
                f"working={side_results[side]['working_version']}",
                flush=True,
            )

    formal_version = _formal_release(
        repository=repository,
        side_results=side_results,
        run_id=args.run_id,
    )
    selected = repository.select_version(int(formal_version.removeprefix("v")))
    publish_number = initial_bank_number + 1
    published = SkillBank.export_published(
        selected,
        run_dir.skills_dir() / f"published_bank{publish_number}",
        bank_number=publish_number,
    )
    variant_dirs = _export_variants(
        run_dir=run_dir,
        side_results=side_results,
        initial_records=initial_records,
        bank_number=publish_number,
        selected_snapshot=selected,
    )
    summary = {
        "run_id": args.run_id,
        "formal_version": formal_version,
        "published_bank": f"bank{publish_number}",
        "published_dir": str(published),
        "variant_dirs": variant_dirs,
        "source_candidates": {side: len(value) for side, value in candidates.items()},
        "semantic_clusters": {side: len(value) for side, value in clusters.items()},
        "drafts": {side: len(value) for side, value in drafts.items()},
        "rejected_source_candidates": {
            side: len(value) for side, value in rejected.items()
        },
        "official_skills": {
            side: len(side_results[side]["records"])
            for side in ("access", "construction")
        },
        "crud_errors": {
            side: side_results[side].get("errors", [])
            for side in ("access", "construction")
        },
    }
    run_dir.write_json("summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
