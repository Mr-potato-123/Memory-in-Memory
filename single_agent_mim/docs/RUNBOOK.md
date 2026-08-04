# MiM Runbook

## 1. Environment

```powershell
cd D:\Documents\Project\Memory_in_Memory\single_agent_mim
$env:PYTHONPATH = "src"
```

Do not install local Qwen weights. Runtime and maintenance models are API
clients configured in `configs/qwen3_8b_dashscope.yaml`.

## 2. Prompt-language gate

All model-facing prompts must be English:

```powershell
rg -n --pcre2 "\p{Han}" prompts
```

Any match blocks a formal run.

Runtime prompts:

```text
prompts/access.md
prompts/construction_extraction.md
prompts/construction_decision.md
```

Changing these requires rebuilding memory and/or rerunning answers.

Maintenance prompts:

```text
prompts/failure/
prompts/skill_maker/
```

Changing only these preserves Runtime output but invalidates the corresponding
Diagnosis or Skill results.

## 3. Tests

```powershell
python -m pytest -q
```

No formal API run starts until tests pass.

## 4. Generate train Runtime answers

One conversation:

```powershell
python scripts\run_train_answers.py `
  --config configs\qwen3_8b_dashscope.yaml `
  --conversation-id conv-30 `
  --output-dir outputs\nsc_train `
  --run-id nsc_train_conv30_v1
```

Resume only after `ingestion_complete` exists:

```powershell
python scripts\run_train_answers.py `
  --config configs\qwen3_8b_dashscope.yaml `
  --conversation-id conv-30 `
  --output-dir outputs\nsc_train `
  --run-id nsc_train_conv30_v1 `
  --resume
```

Expected QA counts:

| Conversation | QA |
|---|---:|
| conv-30 | 105 |
| conv-42 | 260 |
| conv-43 | 242 |
| conv-44 | 158 |
| conv-48 | 239 |
| conv-49 | 196 |

## 5. Diagnosis-only run

Diagnosis currently remains stopped. Do not resume the deleted invalid run.
After the requirements in `DIAGNOSIS_RERUN_REQUIREMENTS.md` are tested, start
with a new run ID and output parent:

```powershell
python scripts\run_diagnosis_only.py `
  --config configs\qwen3_8b_dashscope.yaml `
  --source-run outputs\nsc_train\nsc_train_conv30_v1 `
  --conversation-id conv-30 `
  --output-dir outputs\diagnosis `
  --diagnosis-run-id diagnosis_deepseek_v4_pro_v2 `
  --max-failures 5
```

Acceptance before a full run:

- exactly five complete packages;
- no component `model_error`;
- every diagnosed problem has a non-empty repair package;
- canonical Construction stages only;
- Answer Check input includes temporal metadata;
- no Skill directories;
- source SQLite remains unchanged.

Only then run all train conversations:

```powershell
python scripts\run_diagnosis_only.py `
  --config configs\qwen3_8b_dashscope.yaml `
  --all-train `
  --output-dir outputs\diagnosis `
  --diagnosis-run-id diagnosis_deepseek_v4_pro_v2
```

## 6. Monitoring

```powershell
Get-Content `
  outputs\diagnosis\diagnosis_deepseek_v4_pro_v2\events.jsonl `
  -Tail 30 -Wait
```

Use one monitor only. Do not launch multiple permanent Python polling
processes.

## 7. Output rules

- Logs belong inside their run directory.
- Never write `*.stdout.log` or `*.stderr.log` at repository root.
- Temporary and smoke runs must be removed after validation.
- `outputs/` is resumable state.
- `exp_raw_data/` is checked final experiment data.
- A failed or invalid run is deleted after its audit is recorded.

## 8. Stop a run safely

Locate exact command lines:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like "*run_diagnosis_only.py*" } |
  Select-Object ProcessId, CommandLine
```

Stop only the verified PID:

```powershell
Stop-Process -Id <verified-pid>
```

Never terminate every Python process indiscriminately.
