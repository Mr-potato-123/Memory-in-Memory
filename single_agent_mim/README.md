# Memory in Memory (MiM)

MiM is a procedural meta-memory and Skill-learning system.  The primary
runtime uses Mem0 OSS as its factual-memory data plane; MiM owns only learned
Skills, bounded access control, experiment traces, diagnosis, and Skill-bank
maintenance.  The original SQLite factual backend remains available solely
for historical experiment compatibility.

## Agents

- Construction Agent: fixed C1 fact extraction plus C2 append-only change and
  relation judgment.
- Access Agent: original-query retrieval, one A1-planned supplemental round,
  then one A2 evidence-selection and answer call; no agent loop or standalone
  reranker.
- Access Diagnosis Agent: finds necessary available memories missed by search.
- Construction Diagnosis Agent: locates the earliest construction error.
- Candidate Skill Agent: turns one diagnosis into an unpublished reusable
  Skill or decides that the existing Bank is sufficient.
- Batch Skill CRUD Agent: clusters candidates, retrieves related official
  Skills, resolves conflicts, and publishes one transactional Bank update.

Runtime and maintenance models are configured separately. The current
experiment uses Qwen3-8B non-thinking for Runtime and `deepseek-v4-flash` for
Diagnosis and Skill-Maker.

The runtime is intentionally a minimal plugin demo: Construction Skills may
be supplied to Mem0 as additional extraction instructions, Access Skills guide
retrieval/evidence use, and neither can directly mutate factual memory.

## Quick check

```powershell
cd D:\Documents\Project\Memory_in_Memory\single_agent_mim
$env:PYTHONPATH = "src"
python -m pytest -q
python main.py smoke
```

## Commands

```powershell
python main.py use --config configs\default.yaml ...
python main.py train --config configs\default.yaml --run-id <run-id>
python main.py evaluate --config configs\default.yaml ...
python main.py smoke
```

For the Mem0-backed runtime use
`configs\deepseek_v4_flash_mem0.yaml`.  Install `mem0ai`, configure its LLM,
embedder, and vector store under `storage.mem0_config` (or use Mem0 defaults),
and provide credentials only through environment variables.

Do not use `main.py train` for diagnosis-only work because it proceeds into
candidate generation and batch Skill publication.

## Storage rule

- Mem0/Qdrant: factual-memory source of truth for new experiments.
- `outputs/`: ephemeral mutable run state, including MiM's SQLite trace ledger;
  contents are not source artifacts and may be deleted between experiments.
- `data/splits/`: reproducible dataset split definitions used by the runtime.
- repository root: source entry points only; no logs or temporary artifacts.

All model-facing prompts under `prompts/` must be English.
