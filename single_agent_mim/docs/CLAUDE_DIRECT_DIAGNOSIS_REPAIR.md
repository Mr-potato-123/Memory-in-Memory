# Claude Execution Specification: Three Independent Failure Diagnoses

## 1. Goal

Repair the diagnosis system so that it answers three different questions:

1. **Answer Failure**: Did the runtime model receive enough information but
   still produce a wrong answer?
2. **Access Failure**: Did the current memory contain useful
   answer-supporting information that the runtime search chain failed to
   retrieve?
3. **Cons Failure**: Is required information missing from or incorrect in the
   current memory because memory construction failed?

Do not merge these questions into one classifier. They use different evidence
and produce different artifacts.

Do not run Skill-Maker in this task.

## 2. Fixed top-level workflow

The correctness Judge remains the entry gate:

```text
question + reference answer + runtime prediction
                      |
                      v
              C / P / I Judge
               |         |
               C         P or I
               |         |
              stop       v
                    Answer Diagnosis
                          |
                          v
                 +--------+--------+
                 |                 |
                 v                 v
          Access Diagnosis   Cons Diagnosis
```

The steps are:

1. If the existing Judge label is `C`, do not diagnose the item.
2. If the Judge label is `P` or `I`, run Answer Diagnosis first.
3. After Answer Diagnosis completes, run Access Diagnosis and Cons Diagnosis
   in parallel with fresh, isolated model contexts.
4. Do not let the Answer result block either parallel diagnosis.
5. Do not let Access and Cons block or relabel each other.

The three diagnoses are independent findings, not a forced mutually exclusive
three-class classifier. In ordinary cases, an Answer Failure will imply no
Access Failure for the required claims, but the program must not enforce that
assumption.

## 3. Shared rules

All model prompts, model-visible field names, enum values, schemas, and
generated diagnosis artifacts must be in English.

The Chinese report is user-facing only.

Use the same configured DeepSeek credential and model for all three
diagnoses, but create a fresh client/message context for every item. Sharing a
credential must never share conversation context or output state.

Use the new Judge artifact:

```text
outputs/judge/deepseek_v4_pro_locomo_judge_v2/judgments.jsonl
```

Every diagnosis record must identify:

```text
judge_run_id
diagnosis_run_id
conversation_id
qa_id
snapshot_commit_id
source_runtime_run
```

Open SQLite read-only:

```text
mode=ro
PRAGMA query_only=ON
```

No diagnosis stage may modify memory, replay construction, run a new
retrieval, regenerate an answer, or generate a Skill.

## 4. Terminology and deterministic data preparation

Use plain meanings:

- `current_related_memories`: active memory rows at the frozen snapshot that
  are deterministically associated with the annotated evidence IDs.
- `retrieved_memories`: memory objects actually returned anywhere in the
  original runtime ReAct search chain.
- `retrieved_current_memories`: the subset of `retrieved_memories` whose
  version IDs are active at the frozen snapshot.
- `construction_history`: chronological candidates, decisions, commits,
  changes, parent links, and before/after memory versions relevant to an
  evidence message or memory entry.

The runtime search chain is cumulative:

```text
search -> read -> optionally search again -> read -> answer
```

Every memory returned at any runtime search or inspect step was visible to the
runtime answer model. Keep step boundaries for audit, and also compute the
union of returned IDs.

For Access, filter that union to current snapshot versions. Access never sees
historical versions.

The evidence-to-current-memory lookup is deterministic program logic. It is
not permission to expose lineage, old versions, or raw text to Answer or
Access.

## 5. Answer Diagnosis

### 5.1 Responsibility

Answer Diagnosis asks:

> Did the runtime model receive enough information in its actual search
> results to produce the reference answer, but still answer incorrectly?

This is the first diagnostic stage after a `P/I` Judge result.

It is not a second answering attempt. Do not ask DeepSeek to answer the
question again.

### 5.2 Allowed input

Give the Answer model only:

- question;
- reference answer;
- runtime prediction;
- immutable Judge label and brief reason;
- the ordered runtime search/inspect steps;
- the exact memory objects returned at those steps.

Do not give it:

- relevant current memories that were not retrieved;
- raw conversation messages;
- annotated evidence text;
- construction candidates or decisions;
- memory history or provenance chains;
- Access or Cons output;
- Skill data.

The purpose is to reproduce what information the runtime answering model
actually had, not what it could have found.

### 5.3 Semantic decision

The model decomposes the reference answer into essential factual claims and
marks whether each claim is supported by the retrieved memories.

Information is sufficient only when:

- every essential reference claim is supported;
- the support is present in the returned content, not inferred from an ID or
  provenance link;
- no unresolved contradiction in the returned content prevents the reference
  answer from being justified.

The program computes:

```python
retrieved_context_sufficient = all(
    claim.supporting_retrieved_version_ids
    for claim in essential_reference_claims
) and not unresolved_material_contradiction

answer_failure = (
    judge_label in {"P", "I"}
    and retrieved_context_sufficient
)
```

The LLM must not set `answer_failure`.

### 5.4 Model output

Require exactly one JSON object:

```json
{
  "essential_reference_claims": [
    {
      "claim": "A factual claim required by the reference answer.",
      "supporting_retrieved_version_ids": [
        "mem_conv-00_0001_v1"
      ]
    }
  ],
  "unresolved_material_contradiction": false,
  "reason": "Plain-language judgment based only on retrieved content.",
  "confidence": 0.0,
  "review_required": false
}
```

Reject unknown IDs and claims that are not required by the reference answer.

### 5.5 Artifact rule

Write one audit record for every eligible `P/I` item.

If `answer_failure=true`:

- set `diagnosis_type=ANSWER_FAILURE`;
- append the record to `answer_failure/answer_failures.jsonl`;
- do not create a repair package;
- do not route it to Skill-Maker.

Otherwise set `diagnosis_type=NO_ANSWER_FAILURE`. This result does not mean
Access or Cons exists; it only means the retrieved context was not sufficient
to prove an Answer Failure.

## 6. Access Diagnosis

### 6.1 Responsibility

Access Diagnosis asks:

> Among the useful answer-supporting entries that exist in the current memory,
> did the original runtime search chain retrieve all of them?

An Access Failure exists when at least one useful current memory entry was not
returned.

Access does not require the whole current memory to be sufficient. This allows
Access and Cons to be found independently on the same QA: construction may
have lost one fact while retrieval also missed another useful fact that still
exists.

### 6.2 Allowed input

Give the Access model:

- question;
- reference answer;
- frozen snapshot commit ID;
- `current_related_memories`;
- ordered runtime search/inspect step metadata;
- `retrieved_current_memories`;
- returned current version IDs and step indices.

Do not give it:

- raw conversation messages or source text;
- any historical or non-current memory version;
- lineage edges, version chains, parent versions, or earlier contents;
- before/after memory pairs;
- construction candidates or decisions;
- construction history;
- Cons output;
- Skill data.

The runtime prediction is not needed for this decision and should be omitted
from the Access prompt.

If `inspect_memory` returned historical versions during runtime, preserve them
for Answer Diagnosis because the runtime model saw them, but remove them from
the Access payload.

### 6.3 Semantic decision

The Access model identifies current memory entries that materially support an
essential reference claim. A memory is not useful merely because it is linked
to an evidence ID or mentions the same person.

The program computes:

```python
useful_current_ids = union(
    claim.supporting_current_version_ids
    for claim in essential_reference_claims
)

retrieved_current_ids = union_of_current_ids_returned_at_any_runtime_step
missing_useful_current_ids = useful_current_ids - retrieved_current_ids
access_failure = bool(missing_useful_current_ids)
```

The LLM must not set `access_failure`.

### 6.4 Model output

Require:

```json
{
  "essential_reference_claims": [
    {
      "claim": "A factual claim required by the reference answer.",
      "supporting_current_version_ids": [
        "mem_conv-00_0001_v1"
      ]
    }
  ],
  "reason": "Which current entries are useful and why.",
  "confidence": 0.0,
  "review_required": false
}
```

Reject:

- unknown, inactive, or historical version IDs;
- a memory ID whose current content does not support the stated claim;
- claims not required by the reference answer;
- generated retrieval queries, keywords, filters, weights, or search-depth
  instructions.

### 6.5 Artifact rule

If `access_failure=true`, create one Access repair package:

```json
{
  "schema_version": "access_failure_v3",
  "diagnosis_type": "ACCESS_FAILURE",
  "conversation_id": "...",
  "qa_id": "...",
  "question": "...",
  "reference_answer": "...",
  "useful_current_memories": [],
  "retrieved_current_version_ids": [],
  "missing_useful_current_memories": [],
  "search_steps": [],
  "reason": "Plain-language explanation.",
  "confidence": 0.0,
  "review_required": false
}
```

The repair package records what was missed and what the original search steps
returned. It does not prescribe a new query. Skill-Maker decides any later
retrieval repair.

If no useful current entry was missed:

```text
diagnosis_type = NO_ACCESS_FAILURE
repair_package = null
```

## 7. Cons Diagnosis

### 7.1 Responsibility

Cons Diagnosis asks:

> Does the current memory fail to preserve information needed for the
> reference answer, and if so, where did that information first become
> missing, incorrect, or corrupted during construction?

Cons never receives runtime retrieval results.

### 7.2 Two-stage progressive disclosure

Cons must use two model calls or two strictly separated prompt stages.

#### Stage A: decide whether a construction problem is present

Give the model only:

- question;
- reference answer;
- frozen snapshot commit ID;
- `current_related_memories`.

Do not give Stage A:

- runtime prediction;
- search actions;
- retrieved memories;
- raw conversation text;
- candidates, decisions, commits, or version history;
- Access or Answer output.

Stage A determines whether the current related memories collectively preserve
the information required by the reference answer.

Require:

```json
{
  "essential_reference_claims": [
    {
      "claim": "A factual claim required by the reference answer.",
      "supporting_current_version_ids": [],
      "coverage": "FULL | PARTIAL | MISSING | INCORRECT"
    }
  ],
  "cons_candidate": true,
  "reason": "What is missing or wrong in the current memory.",
  "confidence": 0.0,
  "review_required": false
}
```

If all essential claims are faithfully preserved, output
`NO_CONS_FAILURE` and stop Cons for that QA.

#### Stage B: locate the first construction error

Run Stage B only when Stage A returns a valid `cons_candidate=true`.

The program now supplies:

- annotated raw evidence message IDs and text;
- current related memory entries;
- the full chronological construction path for the implicated messages and
  entries;
- extraction candidates;
- construction decisions;
- commits and memory-change operations;
- explicit parents;
- before and after memory versions.

Stage B must inspect the path in chronological order:

```text
raw evidence availability
-> extraction candidate
-> construction decision
-> first persisted memory
-> each later update/delete/merge affecting the information
```

It reports only the earliest point where the required information was:

- not ingested;
- omitted during extraction;
- incorrectly skipped;
- incorrectly written in the first memory;
- lost or corrupted by a later update/delete/merge.

If the annotated raw evidence itself does not support the reference answer,
return `data_error` for human review rather than inventing a construction
failure.

### 7.3 Cons output and explanation

Require:

```json
{
  "schema_version": "cons_failure_v3",
  "diagnosis_type": "CONS_FAILURE",
  "conversation_id": "...",
  "qa_id": "...",
  "question": "...",
  "reference_answer": "...",
  "affected_reference_claim": "...",
  "raw_evidence_message_ids": [],
  "affected_memory_ids": [],
  "first_error": {
    "stage": "ingestion | extraction | decision | initial_memory | update",
    "commit_id": null,
    "change_id": null,
    "operation": null,
    "before_version_id": null,
    "after_version_id": null
  },
  "reason": "Plain-language account of what the raw evidence said, how it should have been preserved, and exactly where it was first lost or corrupted.",
  "confidence": 0.0,
  "review_required": false
}
```

The reason must be understandable without reading the database. It must name
the lost or corrupted fact and describe the before/after change when the first
error is an update.

The program must validate every returned ID and fill known provenance fields
deterministically. Never publish invented lineage.

Only one Cons repair package is created per QA, for the earliest verified
construction error. Later errors may be listed in internal audit data but must
not become additional repair targets.

## 8. Independence and overlap

Keep these boundaries:

| Diagnosis | Sees retrieved memories | Sees current related memories not retrieved | Sees raw evidence/history | Creates repair package |
|---|---:|---:|---:|---:|
| Answer | Yes, exactly what runtime saw | No | No | Never |
| Access | Yes, current versions only | Yes | No | Only on failure |
| Cons Stage A | No | Yes | No | No |
| Cons Stage B | No | Yes | Yes | Only earliest failure |

Do not create:

```text
both_failures
combined_diagnosis
overall_failure_type
```

Access and Cons may both create their own package for one QA. That is not a
special fourth label; it is simply two independent findings.

## 9. Program logic versus model logic

Use deterministic code for:

- reading the frozen snapshot;
- mapping annotated evidence IDs to active current memory entries;
- parsing all runtime search steps;
- filtering Access input to current versions;
- set union, set difference, and ID membership;
- sorting construction history;
- resolving change parents and before/after versions;
- selecting the earliest validated error after the model identifies a
  semantic break;
- schema validation;
- output routing and resume state.

Use the model only for:

- breaking the reference answer into essential claims;
- judging whether memory text semantically supports a claim;
- judging whether retrieved content is sufficient;
- recognizing missing, corrupted, or contradicted information;
- explaining the earliest construction error in plain language.

Do not ask the model to invent IDs, retrieval parameters, version links, or
database facts.

## 10. Implemented files

The refactor implements:

```text
src/mim/agents/answer_failure.py
src/mim/agents/access_failure.py
src/mim/agents/cons_failure.py
src/mim/diagnosis/answer_workflow.py
src/mim/diagnosis/access_workflow.py
src/mim/diagnosis/cons_workflow.py
src/mim/diagnosis/artifacts.py
scripts/run_answer_failure.py
scripts/run_access_failure.py
scripts/run_cons_failure.py
prompts/diagnosis/answer_failure.md
prompts/diagnosis/access_failure.md
prompts/diagnosis/cons_failure_stage_a.md
prompts/diagnosis/cons_failure_stage_b.md
tests/test_answer_failure.py
tests/test_access_failure.py
tests/test_cons_failure.py
tests/test_diagnosis_isolation.py
```

The implementation reuses read-only provenance helpers and does not refactor
memory construction, retrieval, runtime answering, evaluation, or
Skill-Maker.

The new runners do not import the old combined diagnosis runners or
`AnswerCheckAgent`.
Answer Diagnosis replaces neither the runtime answer nor the correctness
Judge; it only checks whether the original retrieved context was sufficient.

For the actual execution commands, monitoring, resume procedure, and output
audit, follow:

```text
docs/CLAUDE_RUN_DIAGNOSIS_V3.md
```

## 11. Physical artifact layout

Use:

```text
outputs/diagnosis/deepseek_v4_pro_diag_v3/
├── answer_failure/
│   ├── answer_failures.jsonl
│   ├── progress.jsonl
│   ├── errors.jsonl
│   ├── summary.json
│   └── manifest.json
├── access_failure/
│   ├── packages/
│   │   └── <conversation-id>/
│   │       └── <qa-id>_access_failure.json
│   ├── progress.jsonl
│   ├── errors.jsonl
│   ├── summary.json
│   └── manifest.json
└── cons_failure/
    ├── packages/
    │   └── <conversation-id>/
    │       └── <qa-id>_cons_failure.json
    ├── progress.jsonl
    ├── errors.jsonl
    ├── summary.json
    └── manifest.json
```

Answer creates no `packages` directory.

Each component owns its progress, errors, summary, manifest, and resume logic.
No component may use another component's artifact as its completion marker.

## 12. Prompts and model calls

All four prompt files must be English-only and request exactly one JSON
object.

Use:

```text
model = deepseek-v4-pro
temperature = 0.0
supports_json_mode = true
base_url = https://api.deepseek.com
```

Suggested output limits:

```text
Answer         3000 tokens
Access         3000 tokens
Cons Stage A   3000 tokens
Cons Stage B   5000 tokens
```

For every call:

1. use a fresh messages array;
2. require non-empty final content;
3. reject truncation;
4. parse and validate JSON;
5. validate every ID against the supplied input;
6. retry at most twice after the initial attempt;
7. preserve the final invalid response in that component's `errors.jsonl`;
8. mark the item `model_error`, never `completed`.

Do not store chain-of-thought.

## 13. Bounded concurrency

Run the full Answer phase first:

```text
Answer workers = 4
```

After it completes, start Access and Cons at the same time:

```text
Access workers = 4
Cons workers   = 4
total maximum = 8 concurrent model calls
```

A Cons worker performs Stage A and, only when needed, Stage B sequentially for
the same QA. Do not use a nested executor.

Each worker owns its client and each item owns its read-only SQLite connection
and fresh message arrays. Only the runner's main thread writes JSON/JSONL.

Use bounded submission with at most twice the worker count in flight. Keep
independent circuit breakers, logs, counters, and resume state for Answer,
Access, and Cons.

## 14. Required tests

### Answer tests

1. Answer sees only the actual retrieved chain, question, reference, and
   runtime prediction.
2. It receives no unretrieved memory, raw text, or history.
3. Fully supported retrieved context plus Judge `P/I` produces
   `ANSWER_FAILURE`.
4. Missing support for one essential claim produces `NO_ANSWER_FAILURE`.
5. `ANSWER_FAILURE` writes one audit row and no repair package.
6. Unknown returned IDs are rejected.

### Access tests

1. Access receives current related and retrieved current memories.
2. Access receives no raw source text or construction history.
3. Access receives no historical version, lineage edge, or before/after pair.
4. Historical inspect results are removed from the Access payload.
5. A useful current ID not in the returned-current union produces
   `ACCESS_FAILURE`.
6. Retrieving every useful current ID produces `NO_ACCESS_FAILURE`.
7. Access may exist even when current memory is incomplete for another claim.
8. Missing IDs and failure status are computed by code.
9. The package contains no generated query, keyword, filter, or weight.

### Cons tests

1. Cons Stage A receives current related memories and no search results.
2. Cons Stage A receives no raw/history data.
3. Cons Stage B runs only after a valid Cons candidate.
4. Cons Stage B receives raw evidence and chronological history but no search
   results.
5. Missing ingestion maps to `ingestion`.
6. Omitted candidate maps to `extraction`.
7. Wrong skip maps to `decision`.
8. First persisted corruption maps to `initial_memory`.
9. Later destructive change maps to `update`.
10. Only the earliest error becomes a package.
11. Every update error includes verified before and after versions.
12. Unsupported raw evidence returns `data_error`.
13. The explanation states the fact, first bad step, and why it is wrong.

### Isolation tests

1. The three components use distinct prompts and message arrays.
2. Answer completes before Access and Cons are scheduled.
3. Access and Cons then run independently in parallel.
4. No result blocks or relabels another result.
5. No combined package or `both_failures` label exists.
6. Answer never creates a repair package.
7. Each component resumes from only its own valid artifacts.
8. Access cannot write under Cons, and Cons cannot write under Access.
9. Skill-Maker is never called.

Use fake clients in unit tests. Unit tests must not call the live API.

## 15. Acceptance criteria

Do not declare the repair complete unless:

1. `C` rows stop before diagnosis and every valid `P/I` row receives an
   Answer record.
2. Answer runs before the parallel Access/Cons phase.
3. Answer receives only the information actually seen by the runtime model.
4. Answer Failure is record-only and never creates a repair package.
5. Access reads only current memory and current retrieved entries.
6. Access never reads raw conversation or any memory history.
7. Cons never reads runtime retrieval results.
8. Cons Stage A first decides from current memory and the reference answer.
9. Cons Stage B exposes raw evidence/history only after Stage A finds a
   construction candidate.
10. Cons reports and explains only the earliest verified construction error.
11. Access and Cons can both report independently for one QA.
12. No combined label or package exists.
13. All prompts and generated machine artifacts are English.
14. JSON mode and ID validation remain enabled.
15. Failures remain retryable and are not marked completed.
16. No Skill is generated.

## 16. User-facing report

Create:

```text
reports/diagnosis_v3_report_zh.md
```

The Chinese report must include:

- Judge `P/I` input count;
- Answer completion, failure, and error counts;
- Access completion, failure, and error counts;
- Cons Stage A candidate count;
- Cons completion, failure, subtype, and error counts;
- repair-package counts by component;
- confirmation that Answer created no package;
- confirmation that Access received no raw/history data;
- confirmation that Cons received no search results;
- confirmation that only Cons candidates entered Stage B;
- configured and observed concurrency;
- retry and resume validation;
- confirmation that no combined package and no Skill output exists.
