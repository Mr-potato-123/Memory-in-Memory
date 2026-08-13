"""Configuration loading, validation, and resolution.

Loads YAML, substitutes environment variables, validates with Pydantic,
and saves resolved config alongside every run.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from .schemas import hash_dict


# ── Env-var substitution ──────────────────────────────────────────

_ENV_RE = re.compile(r"\$\{(\w+)\}")


def _resolve_env(value: Any) -> Any:
    """Recursively replace ${VAR} patterns with env-var values."""
    if isinstance(value, str):
        def _repl(m: re.Match) -> str:
            return os.environ.get(m.group(1), "")
        return _ENV_RE.sub(_repl, value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


# ── Pydantic models ────────────────────────────────────────────────

class ModelConfig(BaseModel):
    provider: str  # "openai_compatible" | "anthropic" | "mock"
    model: str
    api_key: Optional[str] = None
    # Optional local-only key pool. Excluded from resolved configs/manifests.
    # The client factory distributes concurrent calls across these keys.
    api_keys: list[str] = Field(default_factory=list, exclude=True)
    api_key_env: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 1200
    timeout_seconds: int = 90
    max_retries: int = 3
    supports_json_mode: bool = True
    # Provider-specific generation controls. For DashScope Qwen3, use
    # ``extra_body={"enable_thinking": False}`` as documented officially.
    reasoning_effort: Optional[str] = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    reject_reasoning_output: bool = False


class DatasetConfig(BaseModel):
    path: str = "../LoCoMo/data/locomo10.json"
    split: str = "data/splits/locomo_6_2_2.json"


class EmbeddingConfig(BaseModel):
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    device: str = "cpu"
    normalize: bool = True
    batch_size: int = 32


class StorageConfig(BaseModel):
    backend: str = "sqlite"
    # Relative to the run directory. Absolute paths are rejected by MiMRuntime
    # so one run cannot silently overwrite another run's state.
    path: str = "state/memory.sqlite3"
    busy_timeout_ms: int = 5000
    journal_mode: str = "WAL"
    foreign_keys: bool = True


class ConstructionConfig(BaseModel):
    max_input_tokens: int = 12000
    window_messages: int = 40
    overlap_messages: int = 4
    max_steps_per_session: int = 8
    max_candidates_per_session: int = 30
    related_memory_limit: int = 10
    max_related_pool: int = 24
    # Legacy compatibility knobs. The minimal runtime uses one extraction
    # call followed by deterministic ADD/SKIP and exposes no CRUD search loop.
    max_search_more_calls: int = 0
    # Runtime Skill retrieval is two-stage: hybrid candidate generation,
    # followed by a strict applicability reranker that may abstain.
    skill_candidate_k: int = 10
    skill_top_k: int = 2
    skill_disclose_k: int = 5
    # A Skill is optional guidance, not mandatory context.  Runtime retrieval
    # may abstain when no trigger description is sufficiently relevant.
    skill_min_score: float = 0.20
    exact_duplicate_threshold: float = 1.0
    semantic_duplicate_candidate_threshold: float = 0.88


class RetrievalConfig(BaseModel):
    semantic_candidate_k: int = 30
    bm25_candidate_k: int = 30
    keyword_candidate_k: int = 30
    structured_candidate_k: int = 30
    result_top_k: int = 8
    max_result_top_k: int = 24
    max_query_expansions: int = 4
    max_depth: int = 3
    rrf_k: int = 60
    semantic_weight: float = 0.40
    bm25_weight: float = 0.30
    keyword_weight: float = 0.15
    structured_weight: float = 0.15
    entity_match_multiplier: float = 1.10
    time_valid_multiplier: float = 1.20
    current_active_multiplier: float = 1.05
    temporal_mismatch_multiplier: float = 0.50
    bm25_k1: float = 1.5
    bm25_b: float = 0.75


class AccessConfig(BaseModel):
    mode: str = "agentic"
    max_steps: int = 6
    max_steps_per_question: int = 6
    max_search_calls: int = 4
    max_inspect_calls: int = 2
    memory_top_k: int = 5
    skill_candidate_k: int = 10
    skill_top_k: int = 2
    skill_disclose_k: int = 5
    skill_min_score: float = 0.20
    result_top_k: int = 8
    max_source_messages: int = 8
    candidate_top_k: int = 60
    evidence_top_k: int = 16


class TrainingConfig(BaseModel):
    max_skill_iterations: int = 3
    replay_buffer_size: int = 10
    require_forced_replay: bool = True
    require_natural_replay: bool = True
    skill_cluster_target_size: int = 8
    skill_crud_batch_size: int = 10
    skill_batch_bank_context: int = 25
    # Candidate support is an optional conservative publication gate.  Keep
    # it at one by default: a unique diagnosis can still yield a genuinely
    # reusable Skill, and content quality is decided by CRUD + validation.
    skill_min_candidate_support: int = 1


class PromptsConfig(BaseModel):
    construction_extraction: str = "prompts/construction_extraction.md"
    construction_decision: str = "prompts/construction_decision.md"
    access: str = "prompts/access.md"
    access_v2: str = "prompts/access_v2.md"
    diagnosis_answer: str = "prompts/diagnosis/answer_failure.md"
    diagnosis_access: str = "prompts/diagnosis/access_failure.md"
    diagnosis_cons_screening: str = (
        "prompts/diagnosis/cons_failure_stage_a.md"
    )
    diagnosis_cons_trace: str = (
        "prompts/diagnosis/cons_failure_stage_b.md"
    )
    # Legacy combined diagnosis prompt paths. New code must use the four
    # diagnosis_* paths above.
    failure_access_diagnosis: str = "prompts/failure/access_diagnosis.md"
    failure_construction_diagnosis: str = (
        "prompts/failure/construction_diagnosis.md"
    )
    failure_blind_reanswer: str = "prompts/failure/blind_reanswer.md"
    skill_candidate_generation_access: str = (
        "prompts/skill_maker/candidate_generation_access.md"
    )
    skill_candidate_generation_construction: str = (
        "prompts/skill_maker/candidate_generation_construction.md"
    )
    skill_batch_crud_access: str = (
        "prompts/skill_maker/batch_crud_access.md"
    )
    skill_batch_crud_construction: str = (
        "prompts/skill_maker/batch_crud_construction.md"
    )
    skill_cluster_summarizer_access: str = (
        "prompts/skill_maker/cluster_summarizer_access.md"
    )
    skill_cluster_summarizer_construction: str = (
        "prompts/skill_maker/cluster_summarizer_construction.md"
    )


class MiMConfig(BaseModel):
    """Root configuration for a MiM run."""
    seed: int = 42
    output_dir: str = "outputs"
    dataset: DatasetConfig = Field(default_factory=DatasetConfig)
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    construction: ConstructionConfig = Field(default_factory=ConstructionConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    access: AccessConfig = Field(default_factory=AccessConfig)
    training: TrainingConfig = Field(default_factory=TrainingConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "MiMConfig":
        """Load config from YAML with env-var substitution."""
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        raw = _resolve_env(raw)
        # A structural smoke run can bypass heavyweight local transformer
        # initialization without copying configs (and their local credentials).
        # Normal experiments leave this unset and retain the YAML model.
        embedding_override = os.environ.get("MIM_EMBEDDING_MODEL", "").strip()
        if embedding_override:
            raw.setdefault("embedding", {})["model"] = embedding_override
        return cls(**raw)

    def to_resolved_dict(self) -> dict:
        """Return the full resolved config as a dict for saving."""
        return self.model_dump(mode="json")

    def config_hash(self) -> str:
        return hash_dict(self.to_resolved_dict())


def load_config(config_path: str | Path) -> MiMConfig:
    """Load and validate configuration from a YAML file."""
    return MiMConfig.from_yaml(config_path)
