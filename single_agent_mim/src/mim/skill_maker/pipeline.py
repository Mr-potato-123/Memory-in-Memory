"""Reusable Skill Bank consolidation pipeline.

Extracted from ``MiMTrainer._consolidate_candidates`` so the same logic
can be shared by the training workflow and the standalone Judge-first
Skill Bank entry point (``scripts/run_skill_bank_pipeline.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .batch import (
    BatchSkillRetriever,
    CandidateClusterer,
    SkillCrudExecutor,
)
from .models import (
    SkillBatchPlan,
    SkillCandidate,
    SkillCandidateBatch,
    SkillPayload,
)
from .repository import SkillRepository
from .validator import SkillPayloadValidator


class SkillBankPipeline:
    """Stateless consolidation service.

    Clusters candidates, retrieves official-Bank context per batch,
    plans multi-operation CRUD, detects and replans write-set conflicts,
    and publishes one atomic release per side.

    All LLM calls go through the supplied ``batch_crud_agent``; this
    class only orchestrates deterministic algorithmic stages.
    """

    def __init__(
        self,
        repository: SkillRepository,
        clusterer: CandidateClusterer,
        retriever: BatchSkillRetriever,
        executor: SkillCrudExecutor,
        run_id: str,
        min_candidate_support: int = 1,
    ):
        self._repository = repository
        self._clusterer = clusterer
        self._retriever = retriever
        self._executor = executor
        self._run_id = run_id
        self._min_candidate_support = max(1, min_candidate_support)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def consolidate(
        self,
        side: str,
        batch_crud_agent: Any,
        *,
        artifact_writer: Any | None = None,
        artifact_reader: Any | None = None,
    ) -> dict:
        """Run the full consolidation pipeline for one side.

        Returns a dictionary with keys ``published`` (bool),
        ``new_version`` (str | None), ``accepted`` (int), ``rejected``
        (int), and ``errors`` (list[str]).
        """
        result: dict = {
            "published": False,
            "new_version": None,
            "accepted": 0,
            "rejected": 0,
            "errors": [],
            "quality_rejections": [],
        }
        candidates = self._repository.list_candidates(side)
        if not candidates:
            return result

        # ── Preflight retrieval ──────────────────────────────────
        print(f"  [{side}] Preflight retrieval for {len(candidates)} candidates...", flush=True)
        preflight = self._retriever.retrieve(
            batch_id=f"{side}_{self._run_id}_preflight",
            candidates=candidates,
            repository=self._repository,
        )
        print(f"  [{side}] Preflight done: {len(preflight.relations)} relations", flush=True)
        best_relation: dict[str, Any] = {}
        for relation in preflight.relations:
            current = best_relation.get(relation.candidate_id)
            if (
                current is None
                or relation.combined_score > current.combined_score
            ):
                best_relation[relation.candidate_id] = relation
        for candidate in candidates:
            relation = best_relation.get(candidate.candidate_id)
            if relation and relation.combined_score >= 0.60:
                candidate.related_existing_skill_ids = list(
                    dict.fromkeys(
                        [
                            *candidate.related_existing_skill_ids,
                            relation.skill_id,
                        ]
                    )
                )
                self._repository.save_candidate(candidate)

        # ── Cluster and plan ─────────────────────────────────────
        print(f"  [{side}] Clustering {len(candidates)} candidates...", flush=True)
        groups = self._clusterer.cluster(candidates)
        print(f"  [{side}] {len(groups)} groups formed", flush=True)
        planned: list[tuple[SkillCandidateBatch, SkillBatchPlan]] = []
        for group_index, group in enumerate(groups):
            batch_id = (
                f"{side}_{self._run_id}_{group_index:04d}"
            )
            artifact_path = (
                f"skills/transactions/{side}/{batch_id}.json"
            )
            saved = artifact_reader(artifact_path) if artifact_reader else None
            if isinstance(saved, dict) and saved.get("status") == "planned":
                saved_batch = SkillCandidateBatch(**saved.get("batch", {}))
                saved_plan = SkillBatchPlan(**saved.get("plan", {}))
                expected_ids = {item.candidate_id for item in group}
                saved_ids = {
                    item.candidate_id for item in saved_batch.candidates
                }
                if (
                    saved_batch.batch_id != batch_id
                    or saved_plan.side != side
                    or saved_ids != expected_ids
                ):
                    raise RuntimeError(
                        "Saved CRUD batch does not match deterministic "
                        f"clustering result: {artifact_path}"
                    )
                print(
                    f"  [{side}] Group {group_index+1}/{len(groups)}: "
                    "reusing saved CRUD plan",
                    flush=True,
                )
                planned.append((saved_batch, saved_plan))
                continue
            print(f"  [{side}] Group {group_index+1}/{len(groups)}: "
                  f"{len(group)} candidates, retrieving...", flush=True)
            batch = self._retriever.retrieve(
                batch_id=batch_id,
                candidates=group,
                repository=self._repository,
            )
            official = self._repository.list_active(side)
            print(f"  [{side}] Group {group_index+1}: calling CRUD agent...", flush=True)
            plan = None
            last_error = None
            for crud_attempt in range(3):
                try:
                    plan = batch_crud_agent.plan(
                        batch=batch,
                        official_records=official,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    if crud_attempt < 2:
                        print(f"  [{side}] Group {group_index+1}: "
                              f"CRUD attempt {crud_attempt+1} failed ({exc}), retrying...",
                              flush=True)
                    continue
            if plan is None:
                err_msg = str(last_error)
                print(f"  [{side}] Group {group_index+1}: CRUD FAILED after 3 attempts: {err_msg}", flush=True)
                result["quality_rejections"].append({
                    "batch_id": batch_id,
                    "candidate_ids": [
                        c.candidate_id for c in group
                    ],
                    "base_bank_version": batch.base_bank_version,
                    "error": err_msg,
                })
                result["rejected"] += len(group)
                continue
            if not plan.candidate_resolutions:
                # Model returned valid JSON but empty resolutions — retry
                print(f"  [{side}] Group {group_index+1}: "
                      f"plan has 0 resolutions, treating as failure",
                      flush=True)
                result["errors"].append({
                    "batch_id": batch_id,
                    "candidate_ids": [c.candidate_id for c in group],
                    "base_bank_version": batch.base_bank_version,
                    "error": "CRUD plan has 0 candidate resolutions",
                })
                result["rejected"] += len(group)
                continue
            print(f"  [{side}] Group {group_index+1}: CRUD plan OK "
                  f"({len(plan.operations)} ops)", flush=True)
            planned.append((batch, plan))
            if artifact_writer:
                artifact_writer(
                    artifact_path,
                    {
                        "batch": batch.model_dump(mode="json"),
                        "plan": plan.model_dump(mode="json"),
                        "status": "planned",
                    },
                )

        if not planned:
            return result

        # ── Conflict replanning ──────────────────────────────────
        try:
            planned = self._replan_conflicts(
                side, planned, batch_crud_agent,
            )
        except Exception as exc:
            affected = [
                c for b, _ in planned for c in b.candidates
            ]
            result["errors"].append({
                "stage": "conflict_replan",
                "candidate_ids": [c.candidate_id for c in affected],
                "error": str(exc),
            })
            result["rejected"] += len(affected)
            return result

        # ── Build release plan ───────────────────────────────────
        release_candidates = [
            c for b, _ in planned for c in b.candidates
        ]
        release_resolutions = [
            r for _, p in planned for r in p.candidate_resolutions
        ]
        release_operations = [
            o for _, p in planned for o in p.operations
        ]

        # Optional conservative support gate.  The default is one because a
        # unique failure can still produce a reusable Skill; semantic quality
        # is primarily decided by CRUD content review and validation.
        rejected_new_candidates: set[str] = set()
        kept_operations = []
        payload_validator = SkillPayloadValidator()
        for operation in release_operations:
            if operation.operation.value != "add_skill":
                kept_operations.append(operation)
                continue
            support = set(operation.source_candidate_ids)
            payload = SkillPayload(
                name=operation.name,
                description=operation.description,
                content=operation.content,
            )
            valid_payload, payload_issues = payload_validator.validate(
                payload,
                side=side,
            )
            if (
                len(support) >= self._min_candidate_support
                and valid_payload
            ):
                kept_operations.append(operation)
            else:
                rejected_new_candidates.update(support)
                result["quality_rejections"].append({
                    "stage": "publication_quality_gate",
                    "skill_id": operation.skill_id,
                    "candidate_ids": sorted(support),
                    "error": "; ".join(payload_issues) if payload_issues else (
                        "New Skill lacks required candidate support: "
                        f"{len(support)} < {self._min_candidate_support}"
                    ),
                })
        if rejected_new_candidates:
            release_operations = [
                operation
                for operation in kept_operations
                if not (
                    set(operation.source_candidate_ids)
                    & rejected_new_candidates
                )
            ]
            for resolution in release_resolutions:
                if resolution.candidate_id in rejected_new_candidates:
                    resolution.resolution = "REJECTED"
                    resolution.target_skill_ids = []
                    resolution.reason = (
                        "Publication quality gate rejected the proposed "
                        "official Skill payload."
                    )

        # Guard: ensure every candidate has a resolution
        resolved_ids = {r.candidate_id for r in release_resolutions}
        missing = [
            c for c in release_candidates
            if c.candidate_id not in resolved_ids
        ]
        if missing:
            print(f"  [{side}] WARNING: {len(missing)} candidates have no "
                  f"resolution, excluding from release", flush=True)
            for m in missing:
                result["errors"].append({
                    "stage": "release",
                    "candidate_id": m.candidate_id,
                    "error": "No resolution in any batch plan",
                })
            result["rejected"] += len(missing)
            release_candidates = [
                c for c in release_candidates
                if c.candidate_id in resolved_ids
            ]

        if not release_candidates:
            return result

        if not release_operations:
            accepted_without_mutation = sum(
                resolution.resolution != "REJECTED"
                for resolution in release_resolutions
            )
            result["accepted"] = accepted_without_mutation
            result["rejected"] += (
                len(release_candidates) - accepted_without_mutation
            )
            return result

        release_batch = SkillCandidateBatch(
            batch_id=f"{side}_{self._run_id}_release",
            side=side,
            base_bank_version=self._repository.current_version,
            candidates=release_candidates,
            retrieved_skill_ids=list(
                dict.fromkeys(
                    sid
                    for b, _ in planned
                    for sid in b.retrieved_skill_ids
                )
            ),
            relations=[
                r for b, _ in planned for r in b.relations
            ],
        )
        release_plan = SkillBatchPlan(
            transaction_id=(
                f"tx_{side}_{self._run_id}_release"
            ),
            side=side,
            base_bank_version=self._repository.current_version,
            candidate_resolutions=release_resolutions,
            operations=release_operations,
        )

        # ── Execute ──────────────────────────────────────────────
        try:
            new_version = self._executor.apply(
                release_batch, release_plan,
            )
        except Exception as exc:
            result["errors"].append({
                "batch_id": release_batch.batch_id,
                "candidate_ids": [
                    c.candidate_id
                    for c in release_batch.candidates
                ],
                "base_bank_version": release_batch.base_bank_version,
                "error": str(exc),
            })
            result["rejected"] += len(release_batch.candidates)
            return result

        accepted = sum(
            r.resolution != "REJECTED"
            for r in release_plan.candidate_resolutions
        )
        result["published"] = True
        result["new_version"] = new_version
        result["accepted"] = accepted
        result["rejected"] += (
            len(release_batch.candidates) - accepted
        )

        if artifact_writer:
            artifact_writer(
                f"skills/transactions/{side}/release.json",
                {
                    "batch": release_batch.model_dump(mode="json"),
                    "plan": release_plan.model_dump(mode="json"),
                    "published_bank_version": new_version,
                },
            )

        return result

    # ------------------------------------------------------------------
    # Conflict replanning (union-find over write sets)
    # ------------------------------------------------------------------

    def _replan_conflicts(
        self,
        side: str,
        planned: list[tuple[SkillCandidateBatch, SkillBatchPlan]],
        batch_crud_agent: Any,
    ) -> list[tuple[SkillCandidateBatch, SkillBatchPlan]]:
        """Combine plans with overlapping write sets and replan."""
        while True:
            parent = list(range(len(planned)))

            def find(index: int) -> int:
                while parent[index] != index:
                    parent[index] = parent[parent[index]]
                    index = parent[index]
                return index

            def union(left: int, right: int) -> None:
                left_root, right_root = find(left), find(right)
                if left_root != right_root:
                    parent[right_root] = left_root

            write_sets = [
                self._skill_plan_write_set(plan)
                for _, plan in planned
            ]
            for left in range(len(planned)):
                for right in range(left + 1, len(planned)):
                    if write_sets[left] & write_sets[right]:
                        union(left, right)
            components: dict[int, list[int]] = {}
            for index in range(len(planned)):
                components.setdefault(find(index), []).append(index)
            if all(
                len(indices) == 1 for indices in components.values()
            ):
                return planned

            replanned: list[
                tuple[SkillCandidateBatch, SkillBatchPlan]
            ] = []
            for component_index, indices in enumerate(
                components.values()
            ):
                if len(indices) == 1:
                    replanned.append(planned[indices[0]])
                    continue
                candidates = [
                    c
                    for i in indices
                    for c in planned[i][0].candidates
                ]
                batch = self._retriever.retrieve(
                    batch_id=(
                        f"{side}_{self._run_id}_"
                        f"conflict_{component_index:03d}"
                    ),
                    candidates=candidates,
                    repository=self._repository,
                )
                plan = batch_crud_agent.plan(
                    batch=batch,
                    official_records=self._repository.list_active(side),
                )
                replanned.append((batch, plan))
            planned = replanned

    @staticmethod
    def _skill_plan_write_set(plan: SkillBatchPlan) -> set[str]:
        targets: set[str] = set()
        for operation in plan.operations:
            if operation.skill_id:
                targets.add(operation.skill_id)
            if operation.target_skill_id:
                targets.add(operation.target_skill_id)
        return targets
