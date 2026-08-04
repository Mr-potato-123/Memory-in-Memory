# Claude Execution Guide: Judge-First, Physically Isolated Diagnosis

## 1. Working directory

```text
D:\Documents\Project\Memory_in_Memory\single_agent_mim
```

## 2. Objective

Build a diagnosis-only workflow whose entry condition is semantic error
identified by an LLM judge.

The objective is **not** to measure or optimize Token-F1. Token-F1 may remain
in source prediction files as historical metadata, but it must not:

- select which questions enter diagnosis;
- determine whether a Runtime answer is correct;
- route a failure;
- appear as the primary diagnosis metric.

The workflow must first use an LLM judge to make a short semantic decision and
extract the incorrect parts of the Runtime answer. Only judged errors proceed
to detailed diagnosis.

The task stops after producing diagnosis packages. Do not run Skill-Maker,
replay, Skill CRUD, Skill publication, validation, or evaluation.

## 3. Required high-level workflow

```text
Runtime prediction + reference answer
                |
                v
       Brief LLM-as-Judge
                |
       +--------+--------+
       |                 |
    CORRECT          PARTIAL / INCORRECT
       |                 |
   record only      error-fragment record
                         |
             +-----------+-----------+
             |                       |
             v                       v
    Access Failure Diagnosis   Cons Failure Diagnosis
             |                       |
             v                       v
    isolated Access package    isolated Cons package
```

Access Failure and Cons Failure must be isolated:

- logically;
- in model context;
- in code orchestration;
- in progress tracking;
- in event logs;
- in indexes and summaries;
- in physical output directories.

There must be no combined diagnosis report and no `both_failures` label.

## 4. Model configuration and context isolation

Use the configured maintenance model:

```text
deepseek-v4-pro
```

The brief judge, Access Failure Agent, and Cons Failure Agent may reuse the
same stateless maintenance client and API key.

Sharing a client does not permit sharing messages. Every call must construct a
new message list:

```python
[
    {"role": "system", "content": task_specific_prompt},
    {"role": "user", "content": task_specific_payload},
]
```

Never append one task to another task's conversation history.

The following contexts must remain separate:

1. one QA from another QA;
2. brief judge from Access Failure;
3. brief judge from Cons Failure;
4. Access Failure from Cons Failure;
5. one retry from the failed request that preceded it.

## 5. English-only contract

Everything model-facing or machine-facing in this workflow must be English:

- prompt filenames;
- prompt content;
- JSON keys;
- enum values;
- model-generated reasons;
- package filenames;
- directory names;
- event names;
- index and summary fields.

Raw dataset messages are preserved exactly and are not translated.

Use these prompt files:

```text
prompts/diagnosis/runtime_answer_judge.md
prompts/diagnosis/access_failure.md
prompts/diagnosis/cons_failure.md
```

Do not keep duplicate active prompt files under multiple names. After updating
configuration references and tests, remove superseded Diagnosis prompt files.

Before a run:

```powershell
rg -n --pcre2 "\p{Han}" prompts
```

The command must return no matches.

After generating packages:

```powershell
rg -n --pcre2 "\p{Han}" `
  outputs\diagnosis\<run-id>\judge `
  outputs\diagnosis\<run-id>\access_failure `
  outputs\diagnosis\<run-id>\cons_failure
```

The command must return no matches, excluding verbatim raw-message fields.
Implement the validator so that raw source-message content is excluded from
the language check rather than translated.

## 6. Source data

Use the six preserved Runtime runs:

```text
outputs/nsc_train/nsc_train_conv30_v1/
outputs/nsc_train/nsc_train_conv42_v1/
outputs/nsc_train/nsc_train_conv43_v1/
outputs/nsc_train/nsc_train_conv44_v1/
outputs/nsc_train/nsc_train_conv48_v1/
outputs/nsc_train/nsc_train_conv49_v1/
```

Each run contains:

```text
locomo_predictions.jsonl
state/memory.sqlite3
events.jsonl
summary.json
manifest.json
```

The total source set is 1,200 QA items:

| Conversation | QA count |
|---|---:|
| conv-30 | 105 |
| conv-42 | 260 |
| conv-43 | 242 |
| conv-44 | 158 |
| conv-48 | 239 |
| conv-49 | 196 |

Do not rebuild memory and do not rerun the Runtime model.

Open each SQLite source with:

```python
uri = f"{db_path.resolve().as_uri()}?mode=ro"
conn = sqlite3.connect(uri, uri=True)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA query_only=ON")
```

Do not instantiate `SQLiteMemoryStore` on a source database because its
initializer executes schema DDL.

## 7. Phase A: brief LLM-as-Judge

### 7.1 Judge every source prediction

Do not prefilter by Token-F1.

For every one of the 1,200 source QA items, send the judge:

- `conversation_id`;
- `qa_id`;
- question;
- reference answer;
- Runtime prediction.

Do not send memory, raw conversation, search traces, Construction history, or
Diagnosis results to this brief judge.

The judge evaluates only whether the prediction is semantically correct and
complete relative to the question and reference.

### 7.2 Judge output

Return exactly:

```json
{
  "verdict": "CORRECT | PARTIAL | INCORRECT",
  "incorrect_fragments": [
    {
      "text": "exact fragment copied from the Runtime prediction",
      "issue": "unsupported | contradictory | wrong_entity | wrong_relation | wrong_time | wrong_quantity | irrelevant"
    }
  ],
  "missing_reference_claims": [
    "short claim required by the reference but absent from the prediction"
  ],
  "brief_reason": "One short English sentence.",
  "confidence": 0.0
}
```

Rules:

1. Accept harmless paraphrases.
2. Accept equivalent date formats, such as `2023-07-21` and
   `21 July 2023`.
3. Accept different list order when the items are equivalent.
4. Use `CORRECT` only when the answer is semantically correct and complete.
5. Use `PARTIAL` when some required claims are correct but at least one is
   missing or wrong.
6. Use `INCORRECT` when the answer does not provide the required answer or is
   materially contradicted by the reference.
7. `incorrect_fragments[].text` must be copied from the Runtime prediction.
   Do not paraphrase it.
8. `missing_reference_claims` must be short claims, not a rewritten answer.
9. Do not provide chain-of-thought.
10. Keep `brief_reason` to one sentence.

### 7.3 Judge routing

```text
CORRECT:
    save judge result
    do not run Access Failure
    do not run Cons Failure

PARTIAL or INCORRECT:
    save error-fragment record
    independently enqueue Access Failure
    independently enqueue Cons Failure

Judge model error:
    mark retryable
    do not run either detailed diagnosis
```

Do not let Token-F1 override the judge verdict.

### 7.4 Judge physical storage

```text
outputs/diagnosis/<run-id>/judge/
├── events.jsonl
├── progress.jsonl
├── index.jsonl
├── summary.json
└── cases/
    └── <conversation-id>/
        └── <qa-id>_judge.json
```

The judge index must include all 1,200 items.

## 8. Shared error-fragment record

For `PARTIAL` and `INCORRECT`, create one immutable handoff file:

```text
outputs/diagnosis/<run-id>/judge/errors/<conversation-id>/<qa-id>_error.json
```

Format:

```json
{
  "case_id": "judge_error_<conversation-id>_<qa-id>",
  "conversation_id": "conv-30",
  "qa_id": "conv-30_qa_0001",
  "question": "English dataset question",
  "reference_answer": "reference answer",
  "runtime_prediction": "Runtime answer",
  "judge_verdict": "PARTIAL",
  "incorrect_fragments": [],
  "missing_reference_claims": [],
  "brief_reason": "Short English reason.",
  "judge_confidence": 0.95,
  "source_runtime_run": "outputs/nsc_train/...",
  "access_run_id": "exact saved Access run ID",
  "snapshot_commit_id": 19
}
```

Both detailed workflows may read this immutable judge artifact. Neither may
write to it.

The error-fragment file is the only diagnosis-routing artifact shared by
Access Failure and Cons Failure.

## 9. Phase B1: Access Failure workflow

### 9.1 Responsibility

Access Failure answers one question:

> Did the natural search chain fail to return any memory that both existed at
> the frozen snapshot and was necessary to answer the judged error?

It does not diagnose:

- raw conversation;
- candidate extraction;
- memory updates or merges;
- answer-model reasoning after all necessary memories were returned;
- Skill quality.

### 9.2 Input

Access Failure receives only:

- immutable judge error-fragment record;
- current snapshot memories deterministically traced from annotated evidence;
- complete Access action sequence;
- complete data returned by each Access action.

Access Failure must not receive:

- raw source-message text;
- Construction candidates;
- Construction decisions;
- Construction change history;
- Cons Failure output.

### 9.3 Decision

A repairable Access Failure exists only when:

1. a memory version existed at the frozen snapshot;
2. that version is necessary for correcting one or more judge-identified
   errors or missing claims;
3. the version never appeared in any search or inspection result.

Returned distractors and conflicting memories may be recorded as audit
context. They do not independently create an Access Failure.

### 9.4 Access package

```json
{
  "package_type": "ACCESS_FAILURE",
  "package_version": 1,
  "failure_id": "access_failure_<conversation-id>_<qa-id>",
  "case_id": "judge_error_<conversation-id>_<qa-id>",
  "status": "COMPLETED | REVIEW_REQUIRED | MODEL_ERROR | ENGINEERING_ERROR",
  "problem_found": true,
  "subtype": "MISSING_NECESSARY_MEMORY | NO_ACCESS_FAILURE",
  "question": "...",
  "reference_answer": "...",
  "runtime_prediction": "...",
  "incorrect_fragments": [],
  "missing_reference_claims": [],
  "snapshot_commit_id": 0,
  "access_run_id": "...",
  "necessary_available_memories": [],
  "returned_necessary_version_ids": [],
  "missing_necessary_memories": [],
  "search_steps": [],
  "reason": "English explanation.",
  "confidence": 0.0,
  "repair_package": {
    "missing_memories": [],
    "search_steps": [],
    "failure_mechanism": "English description of what the search chain failed to recover."
  }
}
```

Every `problem_found=true` and `status=COMPLETED` package must have a non-empty
`repair_package`.

### 9.5 Access physical storage

```text
outputs/diagnosis/<run-id>/access_failure/
├── events.jsonl
├── progress.jsonl
├── index.jsonl
├── summary.json
└── packages/
    └── <conversation-id>/
        └── <qa-id>_access_failure.json
```

Access Failure writes nowhere under `cons_failure/`.

## 10. Phase B2: Cons Failure workflow

`Cons` means Construction.

### 10.1 Responsibility

Cons Failure answers one question:

> Where is the earliest point at which the annotated raw evidence was lost,
> invented, reversed, corrupted, incorrectly skipped, or incorrectly changed
> during memory construction?

It does not diagnose:

- search queries;
- retrieval rank;
- Access filters;
- answer-model reasoning;
- Access Failure output;
- Skill quality.

### 10.2 Input

Cons Failure receives only:

- immutable judge error-fragment record;
- annotated raw source messages;
- deterministic chronological Construction history for those messages:
  - processing commits;
  - extracted candidates;
  - decisions, including SKIP;
  - initial versions;
  - every later affected change;
  - before and after versions;
  - current snapshot memories.

Cons Failure must not receive:

- Access Failure package;
- Access diagnosis reason;
- Access necessary/missing memory decision;
- search queries, ranks, scores, or retrieval filters.

### 10.3 Earliest-error rule

Inspect in this order:

```text
raw message
  -> message ingestion
  -> candidate extraction
  -> candidate decision
  -> initial persisted version
  -> version update / correction / merge / deletion
```

Report only the earliest error. If several raw claims are wrong, report the
earliest chronological error handled by the current package. Do not blame
later consequences.

### 10.4 Canonical stages

Use only:

```text
INGESTION
EXTRACTION
WRONG_CANDIDATE
WRONG_SKIP
PERSISTENCE
INITIAL_MEMORY
UPDATE_LOSS
WRONG_MERGE
CORRECTION_FAILURE
PROVENANCE_MISSING
NO_CONS_FAILURE
```

Do not output aliases such as:

```text
candidate_generation
candidate
update
memory_update
merge
decision
```

### 10.5 Cons package

```json
{
  "package_type": "CONS_FAILURE",
  "package_version": 1,
  "failure_id": "cons_failure_<conversation-id>_<qa-id>",
  "case_id": "judge_error_<conversation-id>_<qa-id>",
  "status": "COMPLETED | DATA_ERROR | REVIEW_REQUIRED | MODEL_ERROR | ENGINEERING_ERROR",
  "problem_found": true,
  "subtype": "EXTRACTION",
  "question": "...",
  "reference_answer": "...",
  "runtime_prediction": "...",
  "incorrect_fragments": [],
  "missing_reference_claims": [],
  "raw_support": "SUPPORTED | PARTIAL | CONTRADICTORY | INVALID",
  "source_messages": [],
  "construction_history": {
    "processed_commits": [],
    "candidates": [],
    "change_events": [],
    "snapshot_memories": []
  },
  "first_error": {
    "stage": "EXTRACTION",
    "message_ids": [],
    "candidate_id": null,
    "decision_id": null,
    "commit_id": null,
    "operation": null,
    "before_version_ids": [],
    "after_version_id": null
  },
  "reason": "English explanation of the earliest error.",
  "confidence": 0.0,
  "repair_package": {
    "source_messages": [],
    "first_error": {},
    "relevant_history": {},
    "failure_mechanism": "English description of the construction rule that failed."
  }
}
```

Every `problem_found=true`, `status=COMPLETED` package must have a non-empty
`repair_package`.

### 10.6 Cons physical storage

```text
outputs/diagnosis/<run-id>/cons_failure/
├── events.jsonl
├── progress.jsonl
├── index.jsonl
├── summary.json
└── packages/
    └── <conversation-id>/
        └── <qa-id>_cons_failure.json
```

Cons Failure writes nowhere under `access_failure/`.

## 11. Physical-isolation enforcement

Implement separate runner functions or classes:

```python
run_brief_judge(...)
run_access_failure(...)
run_cons_failure(...)
```

Do not use one generic function with a side flag if that function mixes
payload assembly, output paths, or status tracking.

Use distinct output handles:

```python
JudgeArtifactStore
AccessFailureArtifactStore
ConsFailureArtifactStore
```

Required assertions:

```python
assert access_root != cons_root
assert not access_path.is_relative_to(cons_root)
assert not cons_path.is_relative_to(access_root)
```

Access and Cons may run concurrently after a judge error file is atomically
published, but concurrency is optional. Logical and physical isolation is
mandatory.

If both workflows read the same source SQLite, each must open its own read-only
connection. Never share a mutable cursor or transaction.

## 12. Root run layout

```text
outputs/diagnosis/<run-id>/
├── manifest.json
├── judge/
│   ├── events.jsonl
│   ├── progress.jsonl
│   ├── index.jsonl
│   ├── summary.json
│   ├── cases/
│   └── errors/
├── access_failure/
│   ├── events.jsonl
│   ├── progress.jsonl
│   ├── index.jsonl
│   ├── summary.json
│   └── packages/
└── cons_failure/
    ├── events.jsonl
    ├── progress.jsonl
    ├── index.jsonl
    ├── summary.json
    └── packages/
```

Do not create:

```text
combined/
diagnoses.json
both_failures.jsonl
skills/
candidates/
replays/
```

The root manifest may record shared immutable configuration only. It must not
contain merged diagnosis results.

## 13. Separate progress and failure handling

Judge, Access, and Cons maintain separate progress records.

Judge statuses:

```text
COMPLETED
MODEL_ERROR
ENGINEERING_ERROR
```

Access and Cons statuses:

```text
COMPLETED
REVIEW_REQUIRED
MODEL_ERROR
ENGINEERING_ERROR
```

Cons may additionally use:

```text
DATA_ERROR
```

If any model call returns invalid JSON:

- preserve the raw model response in that workflow's error record;
- mark the item retryable;
- do not mark it completed;
- do not let the other workflow's success overwrite its status.

Access retry state must not affect Cons progress, and Cons retry state must not
affect Access progress.

## 14. Summaries

### Judge summary

```json
{
  "source_qa_total": 1200,
  "judge_completed": 0,
  "correct": 0,
  "partial": 0,
  "incorrect": 0,
  "judge_model_error": 0,
  "error_fragment_count": 0,
  "missing_reference_claim_count": 0
}
```

Do not use Token-F1 as a headline field.

### Access summary

```json
{
  "eligible_judged_errors": 0,
  "completed": 0,
  "missing_necessary_memory": 0,
  "no_access_failure": 0,
  "review_required": 0,
  "model_error": 0,
  "engineering_error": 0
}
```

### Cons summary

```json
{
  "eligible_judged_errors": 0,
  "completed": 0,
  "construction_problem": 0,
  "no_cons_failure": 0,
  "data_error": 0,
  "review_required": 0,
  "model_error": 0,
  "engineering_error": 0,
  "subtypes": {}
}
```

Do not create a merged Access-plus-Cons summary.

## 15. Required code changes

Create or refactor toward:

```text
src/mim/agents/runtime_answer_judge.py
src/mim/agents/access_failure.py
src/mim/agents/cons_failure.py
src/mim/diagnosis/judge_workflow.py
src/mim/diagnosis/access_workflow.py
src/mim/diagnosis/cons_workflow.py
src/mim/diagnosis/artifacts.py
scripts/run_judge_first_diagnosis.py
```

Keep the implementation minimal. Do not duplicate provenance SQL. Reuse the
existing deterministic provenance service through narrow read-only adapters.

Update configuration prompt fields to English names:

```yaml
prompts:
  runtime_answer_judge: prompts/diagnosis/runtime_answer_judge.md
  access_failure: prompts/diagnosis/access_failure.md
  cons_failure: prompts/diagnosis/cons_failure.md
```

Remove superseded active Diagnosis prompt/config fields after all references
and tests are updated.

Do not modify Runtime prompts or Runtime retrieval/construction behavior for
this task.

## 16. Test requirements

Add tests for:

1. Token-F1 is not used for judge routing.
2. Equivalent date formats are judged correct.
3. Correct paraphrases do not enter diagnosis.
4. Judge errors do not enter either diagnosis.
5. PARTIAL and INCORRECT independently enter both workflows.
6. Access receives no raw messages or Construction history.
7. Cons receives no search trace or Access package.
8. Access and Cons use different output roots.
9. No combined report is created.
10. All prompt files and generated non-raw fields are English.
11. Access repair requires a missing necessary available memory.
12. Cons reports only one canonical earliest error.
13. Temporal metadata reaches Answer Check where applicable.
14. Component model errors remain retryable.
15. Source SQLite is opened read-only and remains unchanged.
16. No Skill artifact is created.

Run:

```powershell
$env:PYTHONPATH = "src"
python -m pytest -q
```

## 17. Smoke run

Do not immediately judge all 1,200 items.

First run ten judge cases from conv-30, deliberately including:

- an equivalent date-format answer;
- a correct paraphrase;
- a partial list;
- a wrong entity;
- a wrong time;
- an unsupported extra fragment.

Then run detailed diagnosis only for the judged PARTIAL/INCORRECT cases.

Acceptance:

- judge results are short and English;
- exact incorrect Runtime fragments are retained;
- semantic equivalents are marked CORRECT;
- Access and Cons package counts match the judged-error count independently;
- Access and Cons files exist only in their own roots;
- every completed problem has a repair package;
- no model error is marked completed;
- no combined report or Skill artifact exists.

## 18. Full run

After the smoke gate passes, use a new run ID:

```powershell
python scripts\run_judge_first_diagnosis.py `
  --config configs\qwen3_8b_dashscope.yaml `
  --all-train `
  --output-dir outputs\diagnosis `
  --run-id judge_first_diagnosis_v1
```

Do not reuse or resume the deleted diagnosis v1 run.

## 19. Final delivery to the user

The machine-facing packages and Claude execution documentation remain English.

Write the user-facing final report in Chinese under:

```text
reports/judge_first_diagnosis_report_zh.md
```

The Chinese report must include:

1. source QA total;
2. judge correct/partial/incorrect counts;
3. number and types of incorrect fragments;
4. Access Failure counts;
5. Cons Failure counts;
6. model and engineering errors;
7. confirmation that Token-F1 was not used for routing;
8. confirmation that Runtime was not rerun;
9. confirmation that Skill-Maker was not executed;
10. confirmation that Access and Cons were logically and physically isolated;
11. output paths;
12. test results.

Do not report completion if any required QA remains unjudged or any judged
error lacks either an Access package or a Cons package.
