# Memory in Memory (MiM)

MiM is a minimum viable LoCoMo evaluation and Skill-learning system built
around versioned, source-traceable memory.

## Agents

- Construction Agent: one-call extraction plus deterministic ADD/exact-SKIP.
- Access & Answer Agent: searches, reads, and answers in one ReAct context.
- Access Diagnosis Agent: finds necessary available memories missed by search.
- Construction Diagnosis Agent: locates the earliest construction error.
- Candidate Skill Agent: turns one diagnosis into an unpublished reusable
  Skill or decides that the existing Bank is sufficient.
- Batch Skill CRUD Agent: clusters candidates, retrieves related official
  Skills, resolves conflicts, and publishes one transactional Bank update.

Runtime and maintenance models are configured separately. The current
experiment uses Qwen3-8B non-thinking for Runtime and `deepseek-v4-flash` for
Diagnosis and Skill-Maker.

The runtime is intentionally a minimal plugin demo: Construction Skills are
optional extraction references, Access Skills are optional retrieval/answer
references, and neither can bypass evidence or issue storage mutations.

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

Do not use `main.py train` for diagnosis-only work because it proceeds into
candidate generation and batch Skill publication.

## Documentation

- [Project architecture and data layout](docs/PROJECT_GUIDE.md)
- [Operational runbook](docs/RUNBOOK.md)
- [Diagnosis rerun requirements for coding agents](docs/DIAGNOSIS_RERUN_REQUIREMENTS.md)
- [Claude Skill Bank end-to-end execution guide](docs/CLAUDE_SKILL_BANK_END_TO_END_GUIDE.md)
- [Chinese user-facing diagnosis quality report](reports/diagnosis_package_quality_report_zh.md)

## Storage rule

- `outputs/`: mutable and resumable run state, including SQLite.
- `exp_raw_data/`: checked final experiment exports.
- `docs/`: maintained documentation only.
- `reports/`: Chinese user-facing reports.
- repository root: source entry points only; no logs or temporary artifacts.

All model-facing prompts under `prompts/` must be English.
