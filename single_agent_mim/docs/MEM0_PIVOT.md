# MiM on Mem0: architecture decision

Date: 2026-08-14

## Decision

New MiM experiments use Mem0 OSS as the factual-memory base.  MiM no longer
claims a novel factual store, extraction algorithm, vector index, or hybrid
retriever.  The research object is procedural memory learned from prior agent
trajectories.

```text
conversation
  -> Mem0 add/extraction/entity linking
  -> Mem0 factual memory + hybrid search

question + first Mem0 observation
  -> MiM Skill routing
  -> bounded access trajectory
  -> answer + evidence + Skill effect trace

ordinary train outcomes + traces
  -> MiM diagnosis / consolidation / CRUD
  -> immutable Skill Bank release
```

## Ownership boundary

Mem0 owns:

- fact extraction and consolidation;
- factual persistence and identity;
- semantic, keyword, and entity retrieval;
- factual-memory ranking and filtering.

MiM owns:

- the three-field Skill record and published Bank;
- construction/access Skill routing;
- the bounded answer-or-one-more-search policy;
- selected/injected/applied Skill traces;
- failure-opportunity diagnosis and Skill CRUD;
- baseline-versus-Bank evaluation.

SQLite remains a trace ledger for raw dataset messages, QA metadata, model
actions, externally owned Mem0 IDs, and answer-visible context.  It is not a
second factual-memory source under `storage.backend: mem0`.

## Fair comparison contract

For Access-only evaluation, baseline and MiM must use the same frozen Mem0
collection, query budget, answer model, prompt topology, and top-k settings.
Their only difference is whether MiM retrieves and injects a published Skill.

Construction-Skill evaluation requires separate Mem0 collections because a
construction intervention changes the stored facts.  Compare collection-level
baseline versus Bank releases; do not mix both modes under one `user_id`.

## Current implementation

- `src/mim/retrieval/mem0_backend.py` adapts Mem0 results to the read-only
  `MemoryHit` contract used by Access.
- `MiMRuntime` selects Mem0 with `storage.backend: mem0`.
- Mem0 is the factual source; the local SQLite database stores traces only.
- Mem0 IDs use the evidence namespace `mem0:<id>`.
- `storage.mem0_namespace` isolates factual snapshots.  Access-only baseline
  and Bank runs deliberately share a namespace; construction variants do not.
- `configs/deepseek_v4_flash_mem0.yaml` is the initial experiment config.

## Deliberate compatibility

The old SQLite factual implementation has not been deleted yet.  Existing
snapshots, diagnoses, and regression tests depend on it.  It should be treated
as a legacy backend and removed only after Mem0-backed evaluation and diagnosis
cover the required workflows.

## Known next work

1. Replace the split A1/A2 prompts with one bounded continuous Access history.
2. Make Access diagnosis consume external answer-context rows without assuming
   local `memory_versions` lineage.
3. Separate `selected`, `injected`, and `materially_applied` attribution for
   Mem0 construction Skills.
4. Pin the exact Mem0 SDK/version and full provider config in every run
   manifest.
5. Build one frozen Mem0 snapshot per conversation and reuse it for all
   Access-only baseline/Bank comparisons.
