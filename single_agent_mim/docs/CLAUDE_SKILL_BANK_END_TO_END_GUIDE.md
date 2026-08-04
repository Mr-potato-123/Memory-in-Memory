# Claude Execution Guide: Diagnosis to Candidate Skills, CRUD, Skill Bank, and Evaluation

## 1. Mission

Execute the complete MiM Skill-learning pipeline from already completed,
Judge-first diagnosis packages:

```text
accepted training diagnoses
  -> candidate Skill generation
  -> side-isolated candidate stores
  -> semantic grouping
  -> candidate-to-official-Bank retrieval
  -> batch CRUD planning
  -> conflict detection and replanning
  -> transactional official Skill Bank publication
  -> validation-only Bank selection
  -> frozen Base and MiM test evaluation
  -> DeepSeek C/P/I semantic judgment
  -> auditable final report and raw-data export
```

This is an execution and integration task. Do not redesign the diagnosis
workflow or replace the existing Skill components.

The formal experiment starts from diagnosis packages that were produced from
training conversations and selected by the Judge-first workflow. Token F1 must
not be used to decide which answers enter Skill learning.

## 2. Non-negotiable architecture

### 2.1 Access and Construction stay separate

Access and Construction are independent learning sides:

- Access candidates may only become Access Skills.
- Construction candidates may only become Construction Skills.
- Never mix both sides in one candidate cluster, CRUD request, or transaction.
- A CRUD operation may not modify a Skill from the other side.
- The two sides may share the same maintenance API credentials, but every model
  request must use an independent request context.

The official Bank may contain both sides, but retrieval always filters by side.

### 2.2 Candidates and official Skills stay physically separate

Use this exact logical layout inside one Skill run:

```text
outputs/<skill-run-id>/
  config.resolved.yaml
  manifest.json
  events.jsonl
  source_diagnoses.json
  skills/
    official/
      banks/
        bank_v000.json
        bank_v001.json
        ...
      selected.json
    candidates/
      access/
        <candidate-id>/
          candidate.json
          revisions.jsonl
      construction/
        <candidate-id>/
          candidate.json
          revisions.jsonl
    transactions/
      access/
        <batch-id>.json
        errors.jsonl
        release.json
      construction/
        <batch-id>.json
        errors.jsonl
        release.json
      <transaction-id>.json
  validation/
    <bank-version>/
      runtime/
      judge/
  selection.json
  summary.json
```

`skills/official/selected.json` is maintenance-side state only. After
validation selects a winner, export it to the two Runtime-only files under
`skills/published_bank1/`: `access_skill_bank_v1.json` and
`construction_skill_bank_v1.json`. Runtime must load that directory through
`--skill-bank-dir`; it must never read the combined maintenance snapshot.
Candidate generation writes only under `skills/candidates/`. The only legal
promotion path is a validated `SkillCrudExecutor` transaction.

Never copy candidate files into `official/`. Never let Runtime retrieval scan
`candidates/`.

### 2.3 Minimal Runtime-visible Skill format

The reusable Skill body is:

```json
{
  "name": "Short human-readable name",
  "description": "When this Skill should be retrieved.",
  "content": [
    "One or more concise, actionable instructions."
  ]
}
```

Candidate-only metadata may include:

```json
{
  "candidate_id": "cand_access_...",
  "side": "access",
  "solves": "A short paragraph explaining the general problem.",
  "related_existing_skill_ids": [],
  "source_diagnosis_id": "access_conv-...",
  "status": "staged"
}
```

Do not add a large metadata tail to the Runtime-visible payload. Case-specific
names, dates, answers, memory IDs, message IDs, and diagnosis IDs do not belong
in `name`, `description`, or `content`.

## 3. Current code that must be reused

Read these files before changing or running the pipeline:

```text
src/mim/agents/skill_learning.py
src/mim/skill_maker/models.py
src/mim/skill_maker/repository.py
src/mim/skill_maker/batch.py
src/mim/skill_maker/validator.py
src/mim/skills.py
src/mim/workflows/train.py
src/mim/workflows/evaluate.py
prompts/skill_maker/candidate_generation.md
prompts/skill_maker/batch_crud.md
scripts/judge_predictions.py
```

The existing implementation already provides:

- `CandidateSkillAgent`;
- `BatchSkillCrudAgent`;
- `CandidateClusterer`;
- `BatchSkillRetriever`;
- `SkillCrudExecutor`;
- `SkillRepository`;
- versioned official Bank files;
- selected and nearby Runtime Skill traces;
- frozen Base/MiM evaluation;
- DeepSeek C/P/I judgment.

Do not duplicate these algorithms in a new script.

## 4. Required orchestration entry point

At the time this guide was written, `main.py train` is not the correct formal
entry point for this task. It reruns Runtime and routes failures with an F1
threshold. The accepted experiment is Judge-first and begins with completed
diagnosis packages.

Create one thin orchestration entry point if it does not yet exist:

```text
scripts/run_skill_bank_pipeline.py
```

It should expose:

```powershell
python scripts\run_skill_bank_pipeline.py `
  --config configs\qwen3_8b_dashscope.yaml `
  --diagnosis-run outputs\diagnosis\<accepted-diagnosis-run> `
  --output-dir outputs `
  --run-id <skill-run-id> `
  --workers 4 `
  --stage all
```

Required arguments:

- `--config`: resolved model, dataset, embedding, and Skill settings.
- `--diagnosis-run`: immutable source diagnosis directory.
- `--output-dir`: mutable run parent, normally `outputs`.
- `--run-id`: a new unique ID; never overwrite another run.
- `--initial-skill-bank`: optional `selected.json` from a previous accepted
  round.
- `--workers`: candidate-generation concurrency only; default 4.
- `--stage`: `candidates`, `crud`, `validate`, or `all`.
- `--resume`: continue the same stage using artifact IDs, without repeating
  completed API calls.

The script must be a thin coordinator. If consolidation logic is currently
private inside `MiMTrainer`, extract it into a reusable public workflow service
and call that service from both places. Do not paste a second copy of
`_consolidate_candidates`.

Before any formal API run, add focused tests for:

- diagnosis package discovery and filtering;
- explicit empty trace normalization for a truly empty initial Bank;
- resume without duplicate candidates;
- Access/Construction physical isolation;
- candidate generation never mutating `official/`;
- CRUD never receiving raw diagnosis packages or Runtime traces;
- one resolution for every candidate;
- cross-side CRUD rejection;
- conflict replanning;
- one transactional release per side per round;
- validation selection never reading test results.

## 5. Environment and preflight

Run from the repository root:

```powershell
cd D:\Documents\Project\Memory_in_Memory\single_agent_mim
$env:PYTHONPATH = "src"
```

Do not install local Qwen weights. Runtime and maintenance models are API
clients configured in `configs/qwen3_8b_dashscope.yaml`.

Verify prompt language:

```powershell
rg -n --pcre2 "\p{Han}" prompts
```

Any match in a model-facing prompt blocks the formal run.

Run the full local test suite:

```powershell
python -m pytest -q
python main.py smoke --config configs\default.yaml
```

Also inspect the resolved experiment inputs:

```powershell
Get-Content data\splits\locomo_6_2_2.json
Get-Content configs\qwen3_8b_dashscope.yaml
```

Record hashes of the following in the run manifest:

- dataset file;
- split file;
- source Judge result file;
- source diagnosis directory manifest or a deterministic file-list hash;
- candidate-generation prompt;
- batch-CRUD prompt;
- resolved config;
- initial selected Bank, if supplied.

Do not print API keys into logs, manifests, or reports.

## 6. Input gate: which diagnosis packages are eligible

Use only training-split diagnoses whose source answer was labeled `I` or `P`
by the accepted Judge-first run.

Eligible Access input:

```text
diagnosis_type == "ACCESS_FAILURE"
status == "completed"
problem_found == true
review_required == false
conversation_id belongs to the train split
```

Eligible Construction input:

```text
diagnosis_type == "CONS_FAILURE"
status == "completed"
problem_found == true
review_required == false
conversation_id belongs to the train split
```

Do not generate candidates from:

- correct answers labeled `C`;
- Answer Failure only;
- `NO_ACCESS_FAILURE`;
- `NO_CONS_FAILURE`;
- `record_only`;
- incomplete packages;
- packages with `model_error`;
- packages requiring review;
- validation or test conversations.

Create `source_diagnoses.json` as one JSON array entry per accepted package:

```json
{
  "diagnosis_id": "...",
  "side": "access",
  "source_path": "...",
  "source_sha256": "...",
  "judge_run_id": "...",
  "diagnosis_run_id": "...",
  "conversation_id": "...",
  "qa_id": "..."
}
```

Reject duplicate `diagnosis_id` values. Do not silently choose one duplicate.

### 6.1 Skill trace compatibility

For Access, candidate generation must receive the diagnosis's
`skill_trace`. For Construction, it must receive
`construction_skill_traces`.

The trace must disclose:

- official Skills actually selected for the Runtime Agent;
- nearby official Skills ranked below the injection cut;
- immutable Skill snapshots and scores;
- the official Bank version observed at Runtime.

An older diagnosis package may not contain these fields. Apply this rule:

- If the source Runtime used an empty `v000` Bank, normalize the missing field
  to an explicit empty trace and record that normalization in the manifest.
- If the source Runtime used any non-empty Bank, missing or mismatched traces
  invalidate the package. Regenerate Runtime and diagnosis; do not reconstruct
  a fake historical trace from the current Bank.

An explicit empty Access trace should follow the current
`SkillRetrievalTrace` contract, including `side`, `bank_version`, `query`,
`top_k`, `disclose_k`, `selected`, and `nearby_not_selected`.

## 7. Stage A: candidate Skill generation

Process Access and Construction packages independently. Candidate calls may
run concurrently with at most `--workers` in-flight calls. Every call must
have its own message list and model response object.

For each package:

1. Load the complete diagnosis package.
2. Validate its side and eligibility.
3. Attach the exact Skill trace observed during the failed Runtime operation.
4. Call `CandidateSkillAgent.generate`.
5. Validate the returned payload with `SkillPayloadValidator`.
6. Save a proposal only in the side-specific candidate directory.
7. Append one durable progress event before starting the next item.

The Candidate Agent may return:

- `PROPOSE_SKILL`;
- `NO_CHANGE_ALREADY_COVERED`;
- `NO_CHANGE_NOT_A_SKILL_PROBLEM`.

No-change is a valid, auditable result. Store it in:

```text
skills/candidates/<side>/no_change.jsonl
```

Store failures in:

```text
skills/candidates/<side>/generation_errors.jsonl
```

Do not retry malformed model output forever. Use the configured retry count,
then record the error and continue. A formal release must report unresolved
generation errors.

Candidate acceptance checks:

- `name` is short and general;
- `description` explains when retrieval should select the Skill;
- `content` is a non-empty list of actionable rules;
- `solves` is one short paragraph;
- no case-specific answer is copied;
- every `related_existing_skill_id` was present in the supplied trace;
- source side and candidate side match;
- official Bank files are byte-identical before and after this stage.

Candidate generation may observe diagnosis details and Skill traces. Later
CRUD must not observe them.

## 8. Stage B: semantic grouping

Group Access candidates separately from Construction candidates.

Use `CandidateClusterer` with the configured values:

```text
target cluster size: 8
maximum CRUD batch size: 10
```

Its dense candidate representation uses:

```text
description semantic embedding: 45%
content semantic embedding:     35%
solves semantic embedding:      20%
```

The implementation uses deterministic spherical K-means. It then refines
groups using:

- a shared related-official-Skill anchor;
- strong lexical overlap;
- the maximum batch-size boundary.

Do not cluster solely on `name`. Do not allow a group larger than 10. Save the
candidate IDs and grouping parameters so the grouping can be reproduced.

## 9. Stage C: unified official-Bank retrieval for each batch

For each semantic group, call `BatchSkillRetriever`.

It computes the exact candidate-by-official-Skill matrix using:

```text
description semantic similarity: 50%
content semantic similarity:     30%
BM25 lexical similarity:         20%
```

The retrieval result must:

- guarantee related official context for every candidate when the Bank is not
  empty;
- include candidate-declared official anchors when valid;
- include useful batch-level hub Skills;
- contain no more than the configured Bank context limit, normally 25;
- contain only official active Skills from the same side.

When the initial Bank is empty, an empty retrieved context is correct. The CRUD
Agent may create several new Skills in that batch.

Persist the full candidate-to-Bank relation table in the transaction planning
artifact. These scores are algorithmic context; they are not Runtime Skills.

## 10. Stage D: batch CRUD planning

Call `BatchSkillCrudAgent` once per semantic batch.

The CRUD Agent receives only:

- candidate Skills;
- each candidate's short `solves` paragraph;
- the candidate-to-Bank relation table;
- the related official Skill records.

It must not receive:

- diagnosis packages;
- raw conversations;
- memory histories;
- Runtime search traces;
- gold evidence beyond what was already abstracted into the candidate.

One plan may emit multiple operations:

- `add_skill`;
- `rename_skill`;
- `update_description`;
- `add_content`;
- `update_content`;
- `delete_content`;
- `move_content`;
- `delete_skill`.

Every candidate must receive exactly one resolution:

- `CREATED`;
- `MERGED_INTO_EXISTING`;
- `MERGED_INTO_CANDIDATE`;
- `ALREADY_COVERED`;
- `NOT_A_SKILL_PROBLEM`;
- `REJECTED`.

The executor must reject:

- a missing candidate resolution;
- duplicate candidate resolutions;
- a target Skill not supplied to the CRUD Agent;
- a stale `expected_skill_version`;
- an invalid content index or `expected_content`;
- an empty invalid Skill payload;
- a cross-side mutation;
- a plan whose `base_bank_version` is stale.

## 11. Stage E: conflict detection, replanning, and publication

All initial batch plans for one side must be planned against the same frozen
official Bank version.

Before publication:

1. Compute the write set of every plan.
2. Detect plans that modify the same official Skill.
3. Merge connected conflicting plan groups.
4. Retrieve the union of relevant official context.
5. Ask the CRUD Agent to replan each conflict group.
6. Repeat until the remaining write sets are disjoint.
7. Validate the combined release plan.

Publish one atomic official Bank version per side per consolidation round.
Several new or updated Skills may be included in one version.

The expected progression from an empty Bank is normally:

```text
v000: empty initial Bank
v001: first side release
v002: second side release, cumulative with v001
```

The exact order may differ, but each version must be immutable and cumulative.
Access and Construction transactions remain side-restricted even though the
Bank version is joint.

After publication, verify:

- every published Skill has `status == "active"`;
- candidate files still exist separately;
- deleted Skills are absent from the new active snapshot;
- old Bank versions are unchanged;
- the transaction record lists source candidate IDs;
- the official version can be loaded by `SkillBank`;
- Runtime retrieval returns only Skills from the requested side.

Do not manually edit a `bank_vNNN.json` file.

## 12. Stage F: validation-only Bank selection

Never use test results to choose a Bank version, prompt, threshold, retrieval
weight, or model setting.

Evaluate the Base system and every eligible cumulative Bank version on the
validation split. Before each version run:

1. Read the immutable repository version into a frozen in-memory Runtime view.
2. Run MiM evaluation with a new run ID and a fresh SQLite state directory.
3. Run the C/P/I Judge on the resulting predictions.
4. Store Runtime and Judge artifacts under the corresponding validation
   version directory.

The existing evaluation command is:

```powershell
python main.py evaluate `
  --config configs\qwen3_8b_dashscope.yaml `
  --split-name validation `
  --mode mim `
  --skill-bank-dir outputs\<skill-run-id>\skills\published_bank1 `
  --run-id <skill-run-id>_validation_vNNN
```

Base validation:

```powershell
python main.py evaluate `
  --config configs\qwen3_8b_dashscope.yaml `
  --split-name validation `
  --mode base `
  --run-id <skill-run-id>_validation_base
```

Select the official version deterministically:

1. highest validation `C` rate;
2. then lowest validation `I` rate;
3. then highest validation Token F1;
4. then the smaller Bank;
5. then the earlier Bank version.

C/P/I semantic judgment is the primary selection metric. Token F1 is only a
secondary report and tie-breaker; it is not a diagnosis-routing rule.

Write `selection.json` with every version's metrics and the exact tie-breaking
decision. Finally call `select_version(best_version)`, export the selected
snapshot with `SkillBank.export_bank1(...)`, and verify that both isolated
Bank1 files can be loaded together.

## 13. Prediction schema compatibility before Judge

`main.py evaluate` currently writes `QAResult` rows using:

```text
reference
evidence_ids
```

Some older experiment files and `judge_predictions.py` expect:

```text
answer
evidence
```

Before the formal run, make the Judge input loader accept both forms:

```text
reference_answer = row["reference"] if present else row["answer"]
evidence_ids = row["evidence_ids"] if present else row.get("evidence", [])
```

Normalize internally without changing the source file. Reject a row if neither
`reference` nor `answer` is present. Add a regression test containing one row
from each schema.

Do not rewrite correct Runtime predictions merely to satisfy the Judge loader.

## 14. Stage G: frozen test evaluation

After validation selection, freeze all of the following:

- selected Bank hash;
- Runtime model and non-thinking setting;
- maintenance/Judge model;
- prompts and prompt hashes;
- dataset and split hashes;
- retrieval configuration;
- random seed.

Run Base and MiM on the test split using separate run IDs and fresh state:

```powershell
python main.py evaluate `
  --config configs\qwen3_8b_dashscope.yaml `
  --split-name test `
  --mode base `
  --run-id <experiment-id>_test_base
```

```powershell
python main.py evaluate `
  --config configs\qwen3_8b_dashscope.yaml `
  --split-name test `
  --mode mim `
  --skill-bank-dir outputs\<skill-run-id>\skills\published_bank1 `
  --run-id <experiment-id>_test_mim
```

Do not regenerate memories from another run directory. Each evaluation run
must construct and query its own SQLite state from the same test conversation.

## 15. Stage H: DeepSeek C/P/I Judge

Judge Base and MiM predictions independently with the same Judge prompt and
model:

```powershell
python scripts\judge_predictions.py `
  --config configs\qwen3_8b_dashscope.yaml `
  --judge-model deepseek-v4-flash `
  --batch-size 4 `
  --workers 6 `
  --output-dir outputs\<experiment-id>_judge_base `
  outputs\<experiment-id>_test_base\qa_results.jsonl
```

```powershell
python scripts\judge_predictions.py `
  --config configs\qwen3_8b_dashscope.yaml `
  --judge-model deepseek-v4-flash `
  --batch-size 4 `
  --workers 6 `
  --output-dir outputs\<experiment-id>_judge_mim `
  outputs\<experiment-id>_test_mim\qa_results.jsonl
```

Judge labels:

- `C`: correct;
- `P`: partially correct;
- `I`: incorrect.

The Judge must use only the fictional LoCoMo conversation timeline. It must not
interpret temporal questions using the current real-world date.

Final Judge acceptance:

- output row count equals input row count;
- every `qa_id` is unique;
- no missing IDs;
- labels are only `C`, `P`, or `I`;
- no permanent errors;
- the Judge output contains no F1-derived correctness label;
- Base and MiM use the same Judge prompt hash.

## 16. Live monitoring

The formal run must be monitored stage by stage. The pipeline entry point must
append machine-readable events to:

```text
outputs/<skill-run-id>/events.jsonl
```

Recommended event names:

```text
input_gate_completed
candidate_started
candidate_proposed
candidate_no_change
candidate_failed
clustering_completed
batch_retrieval_completed
crud_plan_completed
crud_plan_failed
conflict_detected
conflict_replanned
side_release_published
validation_version_completed
bank_selected
test_runtime_completed
judge_completed
pipeline_completed
```

Monitor one file:

```powershell
Get-Content outputs\<skill-run-id>\events.jsonl -Tail 30 -Wait
```

Also check stage counts without launching permanent polling processes:

```powershell
(Get-ChildItem outputs\<skill-run-id>\skills\candidates\access `
  -Recurse -Filter candidate.json).Count

(Get-ChildItem outputs\<skill-run-id>\skills\candidates\construction `
  -Recurse -Filter candidate.json).Count

Get-ChildItem outputs\<skill-run-id>\skills\official\banks
Get-Content outputs\<skill-run-id>\summary.json
```

Stop immediately if:

- candidate generation changes an official Bank file;
- Access and Construction appear in the same batch;
- a CRUD request contains a diagnosis package or raw conversation;
- a transaction targets an undisclosed official Skill;
- an official Bank version is overwritten;
- a validation/test run reuses mutable SQLite state;
- the Judge has missing or duplicate QA IDs;
- model output contains an unhandled schema error.

## 17. Resume and failure policy

`--resume` must use durable IDs:

- completed diagnosis ID for candidate generation;
- candidate ID for candidate storage;
- batch ID for CRUD planning;
- transaction ID for publication;
- evaluation run ID for Runtime output;
- QA ID for Judge output.

Resume must not:

- create a second candidate for an already completed diagnosis;
- publish the same transaction twice;
- append duplicate Judge rows;
- change an immutable official Bank version;
- skip a failed item without recording it.

If a candidate or CRUD call fails after configured retries, record the failure
and continue gathering diagnostics. Do not publish a formal Bank while silent
unresolved errors exist. Either resolve them, explicitly reject them with a
reason, or mark the run incomplete.

Use a new run ID after changing prompts, model settings, input diagnoses,
clustering parameters, retrieval weights, or source Bank.

## 18. Required final artifacts

Keep mutable working state in `outputs/`.

After the user accepts the run, publish only the paper-facing artifacts under
the MiM repository root:

```text
exp/
  single-agent/
    bank0/
      train/
      validation/
      test/
    bank1/
      banks/
        access_skill_bank_v1.json
        construction_skill_bank_v1.json
      validation/
    PAPER_RESULTS.md
    PAPER_RESULTS_MANIFEST.json
```

Keep mutable run state, diagnosis packages, candidates, CRUD transactions, and
ablations under `outputs/`. Formal Single-Agent artifacts belong only under
the root `exp/single-agent/`; other frameworks must use sibling directories.
Do not create another `single_agent_mim/exp` tree.

`report.json` must contain:

- source diagnosis run ID and hash;
- counts of eligible Access and Construction diagnoses;
- proposed, no-change, failed, accepted, merged, covered, and rejected
  candidate counts;
- cluster and batch counts by side;
- official Bank versions and Skill counts by side;
- selected version and validation selection table;
- Base and MiM Token F1 overall and by category;
- Base and MiM C/P/I counts and rates overall, by conversation, and by
  category;
- Base-to-MiM changes for C, P, and I;
- protocol errors, Judge errors, token usage, and run IDs;
- all relevant config, prompt, dataset, split, and Bank hashes.

## 19. Claude's final handoff

Return a concise Chinese user-facing report, while keeping prompts, schemas,
code comments, manifests, and machine-facing instructions in English.

The report must state:

1. what code was added or changed;
2. which diagnosis run was consumed;
3. how many packages passed the input gate;
4. candidate outcomes by side;
5. CRUD and Bank version outcomes;
6. which Bank version was selected and why;
7. Base versus MiM validation and test results under both Token F1 and C/P/I;
8. any unresolved errors or excluded packages;
9. exact paths to the selected Bank, raw predictions, Judge results, and final
   experiment export.

Do not claim completion until:

- tests pass;
- the official/candidate isolation checks pass;
- the selected Bank exists and is frozen;
- both Base and MiM test runs finish;
- both Judge runs have complete unique QA coverage;
- the final artifacts and report are present.

## 20. Compact execution checklist

```text
[ ] Read the current Skill and evaluation code.
[ ] Add or verify the thin Judge-first Skill pipeline entry point.
[ ] Add schema-compatibility and resume tests.
[ ] Run prompt-language gate and full tests.
[ ] Freeze and hash source diagnoses and initial Bank.
[ ] Filter eligible training Access and Construction packages.
[ ] Validate or explicitly normalize historical Skill traces.
[ ] Generate side-isolated candidates with bounded concurrency.
[ ] Record proposals, no-change decisions, and errors.
[ ] Cluster each side independently.
[ ] Retrieve official Bank context per candidate batch.
[ ] Plan multi-operation CRUD without diagnosis or Runtime traces.
[ ] Detect and replan overlapping write sets.
[ ] Publish one atomic release per side.
[ ] Evaluate all eligible Bank versions on validation only.
[ ] Judge validation predictions and select deterministically.
[ ] Freeze the selected Bank and experiment configuration.
[ ] Run Base and MiM on test with fresh state.
[ ] Run complete C/P/I Judge for both.
[ ] Validate row coverage, IDs, labels, and hashes.
[ ] Export accepted raw artifacts without overwriting prior experiments.
[ ] Deliver the Chinese handoff report with exact paths.
```
