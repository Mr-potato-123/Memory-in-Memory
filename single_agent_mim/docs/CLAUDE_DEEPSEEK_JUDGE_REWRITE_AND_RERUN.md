# Claude Direct Execution Specification: Rewrite and Re-run the DeepSeek LoCoMo Judge

## 1. Execute this specification exactly

Do not ask the user to choose an evaluation design. The design is fixed below.

Your task is to:

1. stop the obsolete duplicate diagnosis runners without deleting their data;
2. rewrite the LoCoMo Judge implementation;
3. use `deepseek-v4-pro` through the existing maintenance API configuration;
4. retain the ternary labels `C`, `P`, and `I`;
5. add deterministic LoCoMo conversation-time metadata;
6. re-judge all 1,200 frozen train predictions;
7. validate and report the new Judge results;
8. stop after the Judge report.

Do not run a new diagnosis workflow. Do not run Skill-Maker. Do not modify the
memory construction or retrieval code.

All prompts, schemas, logs, and AI-facing reports must be English. The final
user-facing report must be Chinese.

## 2. Fixed evaluation design

Use a pointwise, reference-guided, rubric-based LLM Judge.

Each item contains:

- the question;
- the reference answer;
- the candidate prediction;
- the LoCoMo category;
- the annotated evidence message timestamps;
- the conversation start and end timestamps.

The Judge evaluates semantic correctness and completeness. It does not
evaluate style, verbosity, helpfulness, creativity, or token overlap.

Do not send any of the following to the Judge:

- Token-F1;
- retrieved memories;
- search traces;
- raw conversation text;
- construction history;
- old Judge labels;
- diagnosis results;
- runtime model identity;
- the current system date.

## 3. Fixed ternary labels

### `C`: Correct

Use `C` when all essential reference claims are present and no material
contradiction is introduced.

The following must still be `C`:

- harmless paraphrases;
- a compatible, more specific answer;
- a semantically equivalent date expression;
- a correctly anchored relative-time expression;
- additional details that do not contradict or change the required answer.

Do not downgrade an answer merely because it is longer than the reference.
The reference is a minimal answer, not an exhaustive wording template.

### `P`: Partially correct

Use `P` when the prediction contains at least one essential correct claim but:

- omits another essential claim;
- returns only part of a required list;
- introduces a limited material error;
- remains too ambiguous to establish the full reference answer.

### `I`: Incorrect

Use `I` when the prediction:

- contradicts the reference;
- gives the wrong entity, event, relation, or date;
- contains no essential correct claim;
- is non-responsive;
- answers another question;
- claims that information is unavailable when the reference is answerable.

### Diagnosis routing

The Judge preserves `C/P/I` for analysis. Later routing is:

```text
C   -> do not diagnose
P/I -> diagnose
```

Do not calculate or store a partial numerical score.

## 4. Fixed temporal policy

### 4.1 Never use wall-clock time

The date on the computer, the API request date, and the model's knowledge of
the current year are irrelevant.

Every temporal expression belongs to the fictional LoCoMo conversation.

### 4.2 Build temporal context from the dataset

Load the dataset with:

```python
from mim.eval.locomo import load_dataset
```

Use `config.dataset.path`.

Build these deterministic maps:

```text
conversation_id -> Conversation
qa_id            -> Question
message_id       -> message timestamp
conversation_id  -> earliest session timestamp
conversation_id  -> latest session timestamp
```

For every QA, resolve all message IDs in `Question.source_evidence` to their
dataset timestamps.

The Judge item must contain:

```json
{
  "temporal_context": {
    "policy": "Use only the fictional conversation timeline. Never use the current real-world date.",
    "conversation_start": "earliest dataset session timestamp",
    "conversation_end": "latest dataset session timestamp",
    "evidence_timestamps": [
      {
        "message_id": "conv-30:D1:2",
        "timestamp": "4:04 pm on 20 January, 2023"
      }
    ]
  }
}
```

Do not include raw evidence message content.

### 4.3 Resolve relative time with this exact rule

For `yesterday`, `last week`, `next month`, `a few years ago`, and similar
expressions:

1. Use the annotated evidence timestamps as the primary anchors.
2. If several evidence timestamps exist, accept the prediction when at least
   one annotated evidence timestamp makes it semantically equivalent to the
   reference.
3. If no evidence timestamp exists, use `conversation_end` as the fallback
   anchor.
4. Never use the current date as a fallback.
5. Accept equivalent granularity. For example, an exact date inside the
   correct reference month is compatible with that month.
6. If a relative expression has the correct temporal relation but cannot be
   resolved precisely from the supplied dataset timestamps, use `P`, not `I`.

### 4.4 Mandatory expected labels

These regression labels are fixed:

| QA ID | Required label | Reason |
|---|---|---|
| `conv-48_qa_0002` | `C` | `a few years ago` is equivalent under the 2023 evidence timeline |
| `conv-30_qa_0006` | `C` | `next month`, anchored in January 2023, resolves to February 2023 |
| `conv-30_qa_0002` | `C` | The prediction contains the shared essential answer, dancing; harmless additions do not invalidate it |
| `conv-30_qa_0000` | `I` | May 2023 contradicts 19 January 2023 |
| `conv-42_qa_0003` | `I` | March 2022 contradicts the week before 21 January 2022 |

The full run must not start until a smoke test produces these labels.

## 5. Files to change

Change only the Judge implementation and its tests:

```text
scripts/judge_predictions.py
prompts/judge/locomo_semantic_judge.md
tests/test_judge_predictions.py
```

If `tests/test_judge_predictions.py` does not exist, create it.

Do not create a new framework or refactor unrelated code. Keep the
implementation minimal.

## 6. Create the fixed English Judge prompt

Create:

```text
prompts/judge/locomo_semantic_judge.md
```

Use this prompt:

```markdown
# LoCoMo Semantic Answer Judge

You are a strict but fair pointwise evaluator for the LoCoMo long-term memory
QA benchmark.

For each item, compare the candidate prediction with the reference answer and
the question. Evaluate semantic correctness and completeness, not token
overlap.

Labels:

- C: The prediction contains every essential reference claim and introduces no
  material contradiction. Harmless paraphrases, compatible specificity,
  equivalent temporal expressions, and non-contradictory additional details
  are allowed.
- P: The prediction contains at least one essential correct claim but omits
  another essential claim, gives only a proper subset of a required list,
  contains a limited material error, or remains materially ambiguous.
- I: The prediction is wrong, contradictory, non-responsive, answers a
  different question, or says that information is unavailable when the
  reference is answerable.

Rules:

1. The reference answer defines the essential factual content. Its exact
   wording is not required.
2. Do not downgrade an answer merely because it is longer than the reference.
3. Ignore harmless additional details. Downgrade only material errors,
   contradictions, or ambiguity.
4. For list questions, a proper subset of essential reference items is P.
5. For temporal questions, use only the supplied fictional conversation
   timeline.
6. Never use today's date, the API request date, or outside wall-clock time.
7. Resolve relative time from the supplied annotated evidence timestamps.
8. When several evidence timestamps exist, accept the prediction if at least
   one annotated evidence timestamp makes it equivalent to the reference.
9. If no evidence timestamp exists, use conversation_end as the only fallback.
10. Accept temporal expressions that resolve to the same date or interval even
    when their surface forms or granularity differ.
11. If the temporal relation is correct but exact resolution is impossible
    from the supplied timestamps, use P rather than I.
12. For category 5, the reference is intentionally empty. Use C only when the
    prediction correctly states that the answer is unavailable or rejects the
    false premise.
13. Do not mention Token-F1.
14. Keep each reason factual and under 24 words.

Return exactly one JSON object:

{
  "judgments": [
    {
      "qa_id": "exact input qa_id",
      "label": "C|P|I",
      "reason": "brief semantic reason"
    }
  ]
}

Return every input qa_id exactly once and in input order.
```

Load this prompt from disk. Do not retain a second embedded Judge prompt in
Python.

## 7. Rewrite `scripts/judge_predictions.py`

### 7.1 Fixed CLI

The script must accept:

```text
--config
--judge-model
--batch-size
--output-dir
--resume
inputs...
```

Defaults:

```text
--config configs/qwen3_8b_dashscope.yaml
--judge-model deepseek-v4-pro
--batch-size 4
```

`--output-dir` is required.

### 7.2 Fixed DeepSeek client

Replace the current runtime-derived client.

Start from:

```python
maintenance = load_config(config_path).models["maintenance"]
values = maintenance.model_dump()
```

Set:

```python
values["model"] = "deepseek-v4-pro"
values["temperature"] = 0.0
values["max_tokens"] = 3000
values["supports_json_mode"] = True
```

Retain the maintenance configuration's:

- DeepSeek base URL;
- DeepSeek API key;
- thinking mode;
- reasoning effort;
- timeout;
- retries.

Do not inherit anything from `models.runtime`.

Do not print or persist the API key.

Before the first API call, validate:

```text
model == deepseek-v4-pro
base_url == https://api.deepseek.com
supports_json_mode == true
```

Abort if any condition fails.

### 7.3 Fixed API behavior

Use:

```python
client.generate(
    messages,
    temperature=0.0,
    max_tokens=3000,
    json_mode=True,
)
```

Every batch gets a fresh `messages` list. Never append earlier Judge outputs
to later calls.

DeepSeek JSON mode must send:

```json
{"type": "json_object"}
```

through the repository client abstraction.

Parse only the final response content. Do not persist reasoning content.

### 7.4 Fixed batch and retry policy

Use batch size 4.

For each batch:

1. reject empty content;
2. reject `finish_reason == "length"` when finish reason is available;
3. parse the JSON object;
4. require a `judgments` list;
5. require exact ordered QA IDs;
6. require exactly one result per input item;
7. require each label to be `C`, `P`, or `I`;
8. require a non-empty reason of at most 24 words.

Retry the complete batch up to three times.

If it still fails, split it into four single-item requests. Retry each
single-item request up to three times.

If a single item still fails:

- write it to `errors.jsonl`;
- do not invent a label;
- continue with other items;
- make the final command fail validation.

### 7.5 Fixed resume behavior

On `--resume`:

- load existing `judgments.jsonl`;
- validate every existing record;
- index records by `qa_id`;
- skip only already valid QA IDs;
- never append a duplicate QA ID.

Without `--resume`, abort if the output directory already exists.

## 8. Fixed output layout

Write only to:

```text
outputs/judge/deepseek_v4_pro_locomo_judge_v2/
├── judgments.jsonl
├── summary.json
├── manifest.json
├── progress.jsonl
├── errors.jsonl
└── prompt_snapshot.md
```

Do not overwrite or modify:

```text
outputs/nsc_train_all_judge.jsonl
outputs/nsc_train_partial_judge.jsonl
```

### Judgment record

Each line of `judgments.jsonl` must use:

```json
{
  "conversation_id": "conv-48",
  "qa_id": "conv-48_qa_0002",
  "category": 2,
  "label": "C",
  "reason": "The relative phrase is equivalent under the January 2023 evidence timestamp.",
  "judge_model": "deepseek-v4-pro",
  "judge_prompt_version": "locomo_semantic_judge_v2",
  "temporal_context": {
    "conversation_start": "...",
    "conversation_end": "...",
    "evidence_timestamps": [
      {
        "message_id": "conv-48:D1:5",
        "timestamp": "..."
      }
    ]
  }
}
```

Do not store:

```text
f1
strict_score
partial_aware_score
```

### Manifest

Store:

- run ID;
- creation time;
- source prediction paths and SHA-256 hashes;
- dataset path and SHA-256 hash;
- prompt version and SHA-256 hash;
- model;
- base URL without credentials;
- temperature;
- max tokens;
- batch size;
- expected row count;
- completed row count;
- explicit `token_f1_used_for_routing: false`;
- temporal anchor policy.

## 9. Tests Claude must implement

Use fake model clients. Unit tests must not call the live API.

Implement these tests:

1. The Judge client is derived from `models.maintenance`.
2. The effective model is `deepseek-v4-pro`.
3. The effective base URL is `https://api.deepseek.com`.
4. JSON mode remains enabled.
5. The Judge payload contains no F1 field.
6. The Judge payload contains no current date field.
7. Evidence message IDs resolve to dataset timestamps.
8. Conversation start and end are deterministic.
9. Existing valid output resumes without duplicate QA IDs.
10. Empty response content is retried.
11. Invalid JSON is retried.
12. A failed four-item batch falls back to single-item calls.
13. Missing, duplicated, or reordered QA IDs are rejected.
14. Labels outside `C/P/I` are rejected.
15. Reasons longer than 24 words are rejected and retried.
16. The regression fixture for `conv-48_qa_0002` contains a 2023 evidence
    timestamp.
17. The regression fixture for `conv-30_qa_0006` contains a January 2023
    evidence timestamp.

Run the relevant test file and the existing test suite. Fix only failures
caused by this Judge change.

## 10. Execution sequence

### Step 1: Stop obsolete duplicate diagnosis runners

Gracefully stop all existing processes running:

```text
run_judge_first_diagnosis.py
run_judge_first_diagnosis_concurrent.py
```

Do not delete or modify their outputs. Record that they are incomplete legacy
runs.

### Step 2: Implement and test

Modify only the three files listed in Section 5.

Run unit tests before any live API call.

### Step 3: Run a five-case live smoke test

Use exactly:

```text
conv-48_qa_0002 -> C
conv-30_qa_0006 -> C
conv-30_qa_0002 -> C
conv-30_qa_0000 -> I
conv-42_qa_0003 -> I
```

Store the smoke test under:

```text
outputs/judge/deepseek_v4_pro_locomo_judge_v2_smoke/
```

If any required label differs:

1. inspect the prompt, payload timestamps, and final response;
2. correct the general rubric or temporal payload;
3. rerun all five smoke cases;
4. do not add QA-ID-specific rules to the production prompt or code.

Do not start the full run until all five labels match.

### Step 4: Run the full 1,200-item Judge

Use these six files:

```text
outputs/nsc_train/nsc_train_conv30_v1/locomo_predictions.jsonl
outputs/nsc_train/nsc_train_conv42_v1/locomo_predictions.jsonl
outputs/nsc_train/nsc_train_conv43_v1/locomo_predictions.jsonl
outputs/nsc_train/nsc_train_conv44_v1/locomo_predictions.jsonl
outputs/nsc_train/nsc_train_conv48_v1/locomo_predictions.jsonl
outputs/nsc_train/nsc_train_conv49_v1/locomo_predictions.jsonl
```

Use one process and batch size 4. Do not launch a second Judge process.

The command must follow this form:

```powershell
python scripts/judge_predictions.py `
  --config configs/qwen3_8b_dashscope.yaml `
  --judge-model deepseek-v4-pro `
  --batch-size 4 `
  --output-dir outputs/judge/deepseek_v4_pro_locomo_judge_v2 `
  outputs/nsc_train/nsc_train_conv30_v1/locomo_predictions.jsonl `
  outputs/nsc_train/nsc_train_conv42_v1/locomo_predictions.jsonl `
  outputs/nsc_train/nsc_train_conv43_v1/locomo_predictions.jsonl `
  outputs/nsc_train/nsc_train_conv44_v1/locomo_predictions.jsonl `
  outputs/nsc_train/nsc_train_conv48_v1/locomo_predictions.jsonl `
  outputs/nsc_train/nsc_train_conv49_v1/locomo_predictions.jsonl
```

Monitor:

- completed item count;
- retry count;
- batch-to-single fallback count;
- permanent failure count;
- `C/P/I` counts;
- output file growth.

### Step 5: Validate the full output

Require:

```text
input rows                 = 1200
output rows                = 1200
unique qa_id               = 1200
missing qa_id              = 0
duplicate qa_id            = 0
invalid label              = 0
permanent errors           = 0
records containing F1      = 0
records lacking timestamps = 0
```

If validation fails, resume the same immutable run after fixing only the
general failure. Do not start another competing run.

### Step 6: Compare old and new labels

Compare the new result with:

```text
outputs/nsc_train_all_judge.jsonl
```

Generate:

- new overall `C/P/I` counts;
- counts by conversation;
- counts by category;
- the old-to-new `C/P/I` transition matrix;
- all temporal label changes;
- all old `P/I` items changed to `C`;
- all old `C` items changed to `P/I`;
- the five mandatory regression results.

The old labels are only comparison data. They must never appear in the new
Judge input.

### Step 7: Stop

After the Judge output and reports are complete:

- do not run diagnosis;
- do not run Skill-Maker;
- do not delete old results;
- wait for user review.

## 11. Required reports

Create an English execution report:

```text
docs/DEEPSEEK_JUDGE_V2_EXECUTION_REPORT.md
```

Create a Chinese user report:

```text
reports/deepseek_judge_v2_report_zh.md
```

The Chinese report must include:

- the exact implementation changes;
- confirmation that the DeepSeek maintenance API was used;
- confirmation that F1 was not used;
- the temporal-anchor algorithm;
- the five smoke-test results;
- final `C/P/I` counts;
- old-to-new transition matrix;
- all changed temporal judgments;
- API retries and failures;
- output artifact paths;
- explicit confirmation that diagnosis and Skill-Maker were not run.

## 12. Completion criteria

Do not declare completion unless:

1. only the Judge implementation, Judge prompt, and Judge tests were changed;
2. DeepSeek configuration is derived from `models.maintenance`;
3. the effective model is `deepseek-v4-pro`;
4. JSON mode is enabled;
5. the Judge uses the LoCoMo fictional timeline;
6. the current real-world date is never used;
7. all five smoke labels match;
8. all 1,200 items have valid `C/P/I` judgments;
9. no output record contains F1;
10. old Judge results remain unchanged;
11. the transition report exists;
12. both execution reports exist;
13. no new diagnosis was started;
14. Skill-Maker was not run.

