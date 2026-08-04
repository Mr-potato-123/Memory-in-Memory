# Claude Run Guide: Diagnosis V3

## Purpose

Run the implemented three-stage diagnosis system. Do not redesign or refactor
the system during this run.

The fixed order is:

```text
Judge P/I rows
      |
      v
Answer phase
      |
      v
Access phase + Cons phase in parallel
```

Answer Failure is record-only. Access and Cons create separate repair packages.
Do not run Skill-Maker.

## Preconditions

Work from:

```text
D:\Documents\Project\Memory_in_Memory\single_agent_mim
```

Confirm:

```powershell
python -m pytest tests/test_diagnosis_v3.py -q
```

Expected:

```text
6 passed
```

Use this Judge input:

```text
outputs/judge/deepseek_v4_pro_locomo_judge_v2/judgments.jsonl
```

Use this new output root:

```text
outputs/diagnosis/deepseek_v4_pro_diag_v3
```

Do not reuse `judge_first_diag_v2` or `judge_first_diag_v3` as resume state.

## Shared arguments

The six train source runs are:

```powershell
$sourceArgs = @(
  "--source-run", "conv-30=outputs/nsc_train/nsc_train_conv30_v1",
  "--source-run", "conv-42=outputs/nsc_train/nsc_train_conv42_v1",
  "--source-run", "conv-43=outputs/nsc_train/nsc_train_conv43_v1",
  "--source-run", "conv-44=outputs/nsc_train/nsc_train_conv44_v1",
  "--source-run", "conv-48=outputs/nsc_train/nsc_train_conv48_v1",
  "--source-run", "conv-49=outputs/nsc_train/nsc_train_conv49_v1"
)
```

Use:

```powershell
$commonArgs = @(
  "--config", "configs/qwen3_8b_dashscope.yaml",
  "--judge-results", "outputs/judge/deepseek_v4_pro_locomo_judge_v2/judgments.jsonl",
  "--diagnosis-run-id", "deepseek_v4_pro_diag_v3",
  "--output-root", "outputs/diagnosis/deepseek_v4_pro_diag_v3",
  "--workers", "4"
) + $sourceArgs
```

## Step 1: smoke run

Use a separate smoke root and one item:

```powershell
$smokeArgs = @(
  "--config", "configs/qwen3_8b_dashscope.yaml",
  "--judge-results", "outputs/judge/deepseek_v4_pro_locomo_judge_v2/judgments.jsonl",
  "--diagnosis-run-id", "deepseek_v4_pro_diag_v3_smoke",
  "--output-root", "outputs/diagnosis/deepseek_v4_pro_diag_v3_smoke",
  "--workers", "1",
  "--max-items", "1"
) + $sourceArgs

python scripts/run_answer_failure.py @smokeArgs
python scripts/run_access_failure.py @smokeArgs
python scripts/run_cons_failure.py @smokeArgs
```

Check:

- all three commands exit with code 0;
- each component has one completed progress row;
- Answer has no `packages` directory;
- Access and Cons do not write into each other's directories;
- no `skills`, `combined`, or `both_failures` directory exists.

Do not copy smoke artifacts into the full output root.

## Step 2: full Answer phase

Run:

```powershell
python scripts/run_answer_failure.py @commonArgs
```

Wait until it exits successfully.

Confirm:

- `answer_failure/progress.jsonl` has one terminal row for every eligible
  Judge `P/I` item without a runtime error;
- `answer_failure/answer_failures.jsonl` contains only positive Answer Failure
  records;
- `answer_failure/packages` does not exist;
- every model or data error in `summary.json` is recorded for retry.

Do not start Access or Cons while the Answer phase has unattempted items. Both
runners enforce this ordering gate. A terminal Answer error does not block the
independent Access and Cons diagnoses; resume Answer separately afterward.

## Step 3: Access and Cons in parallel

After Answer completes, start these commands at the same time in two terminals
or two independently monitored execution cells.

Terminal A:

```powershell
python scripts/run_access_failure.py @commonArgs
```

Terminal B:

```powershell
python scripts/run_cons_failure.py @commonArgs
```

Each runner uses four workers. The intended combined maximum is eight active
DeepSeek calls.

Do not start a second copy of either command.

## Monitoring

Both runners print one line for every completed QA:

```text
[access] completed=... qa=... status=... problem=...
[cons] completed=... qa=... status=... problem=...
```

Monitor both streams throughout the run. Stop only the affected component if
one of these repeats:

- authentication failure;
- invalid model name;
- provider-wide timeout;
- repeated empty output;
- repeated invalid JSON;
- missing database or prompt path.

One component failing does not invalidate completed output from the other.

Inspect summaries independently:

```powershell
Get-Content outputs/diagnosis/deepseek_v4_pro_diag_v3/answer_failure/summary.json
Get-Content outputs/diagnosis/deepseek_v4_pro_diag_v3/access_failure/summary.json
Get-Content outputs/diagnosis/deepseek_v4_pro_diag_v3/cons_failure/summary.json
```

## Resume

Resume only the failed component by adding:

```text
--resume
```

Examples:

```powershell
python scripts/run_access_failure.py @commonArgs --resume
python scripts/run_cons_failure.py @commonArgs --resume
```

Resume state is component-local. Never use Answer progress to mark Access or
Cons completed.

## Required output audit

Verify this layout:

```text
outputs/diagnosis/deepseek_v4_pro_diag_v3/
├── answer_failure/
│   ├── answer_failures.jsonl
│   ├── progress.jsonl
│   ├── errors.jsonl          # only if errors occurred
│   ├── summary.json
│   └── manifest.json
├── access_failure/
│   ├── packages/
│   ├── progress.jsonl
│   ├── errors.jsonl          # only if errors occurred
│   ├── summary.json
│   └── manifest.json
└── cons_failure/
    ├── packages/
    ├── progress.jsonl
    ├── errors.jsonl          # only if errors occurred
    ├── summary.json
    └── manifest.json
```

Audit at least five random packages from each repair-producing component.

For Access, confirm:

- only current memory versions are present;
- no raw message text is present;
- no historical version, parent link, or before/after pair is present;
- missing useful current IDs are absent from the returned-current ID union;
- no generated query, keyword, filter, or retrieval weight is present.

For Cons, confirm:

- no runtime search result is present;
- Stage A screening is preserved;
- Stage B exists only when Stage A found a candidate;
- raw evidence supports the reference answer;
- only the earliest error is selected;
- update errors contain verified before and after versions;
- the reason explains the raw fact, expected memory, first bad step, and
  impact.

For Answer, confirm:

- it uses only the exact runtime-visible chain;
- every reference claim has retrieved support when `ANSWER_FAILURE` is true;
- no repair package or Skill route exists.

## Final report

Create:

```text
reports/diagnosis_v3_report_zh.md
```

Write the report in Chinese. Include:

- eligible Judge `P/I` count;
- completed/error counts for all three components;
- Answer Failure count;
- Access Failure and package counts;
- Cons Stage-A candidate count;
- Cons Failure and subtype counts;
- five-sample manual quality audit for Access;
- five-sample manual quality audit for Cons;
- retries and resume actions;
- observed peak concurrency;
- confirmation that Answer created no package;
- confirmation that Access contains no raw/history data;
- confirmation that Cons contains no retrieval data;
- confirmation that no combined package and no Skill output exists.

Do not report F1 as the diagnosis objective.
