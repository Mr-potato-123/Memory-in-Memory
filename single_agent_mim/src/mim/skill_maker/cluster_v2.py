"""Semantic clustering for the draft-first Skill consolidation pipeline."""
from __future__ import annotations
import numpy as np
from typing import Protocol

from .models import SkillCandidate
from .batch import CandidateClusterer


class EmbedderLike(Protocol):
    def encode(self, texts: list[str]) -> np.ndarray: ...


def cluster_v2(
    candidates: list[SkillCandidate],
    embedder: EmbedderLike,
    *,
    target_cluster_size: int = 12,
    max_cluster_size: int = 25,
) -> list[list[SkillCandidate]]:
    """Return deterministic semantic groups suitable for one LLM summary.

    ``CandidateClusterer`` historically used ``max_batch_size`` as its small
    input fast path.  Passing a huge value there therefore disabled K-means
    and made the old V2 code slice one arbitrary, UUID-ordered group.  Here the
    fast-path boundary is deliberately the semantic target size.  Oversized
    groups are recursively re-clustered; positional slicing is only a final
    fallback for identical vectors that cannot be separated by K-means.
    """
    if len(candidates) <= target_cluster_size:
        return [list(candidates)] if candidates else []

    def split(group: list[SkillCandidate]) -> list[list[SkillCandidate]]:
        if len(group) <= max_cluster_size:
            return [group]
        kmeans = CandidateClusterer(
            embedder,
            target_cluster_size=target_cluster_size,
            max_batch_size=target_cluster_size,
        )
        children = kmeans.cluster(group)
        if len(children) <= 1 or max(map(len, children)) >= len(group):
            # All embeddings are effectively identical.  Stable candidate-ID
            # ordering makes the unavoidable fallback reproducible.
            ordered = sorted(group, key=lambda item: item.candidate_id)
            return [
                ordered[start : start + max_cluster_size]
                for start in range(0, len(ordered), max_cluster_size)
            ]
        result: list[list[SkillCandidate]] = []
        for child in children:
            result.extend(split(child))
        return result

    initial = CandidateClusterer(
        embedder,
        target_cluster_size=target_cluster_size,
        max_batch_size=target_cluster_size,
    ).cluster(candidates)
    groups = [child for group in initial for child in split(group)]
    return sorted(
        groups,
        key=lambda group: (
            -len(group),
            min(item.candidate_id for item in group),
        ),
    )
