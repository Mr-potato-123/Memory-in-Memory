# MiM Project Guide

## 1. Purpose

MiM is a minimum viable LoCoMo memory evaluation and Skill-learning system.
It keeps Runtime execution, Diagnosis, candidate learning, and official Skill
publication explicit and independently reproducible.

The active agents are:

- Construction Agent: converts an incoming conversation session into
  source-traceable, versioned memories.
- Access & Answer Agent: searches memory and answers in one continuous ReAct
  context.
- Access Diagnosis Agent: decides whether useful current memories were missed
  by the Runtime search chain.
- Construction Diagnosis Agent: finds the earliest point where annotated raw
  evidence was lost or corrupted.
- Candidate Skill Agent: converts one completed diagnosis into one unpublished
  reusable Skill, or returns that no Skill change is needed.
- Batch Skill CRUD Agent: consolidates semantically related candidates into the
  official Skill Bank.

Runtime and maintenance models use independent message contexts. Diagnosis and
candidate generation may inspect Runtime traces. Batch CRUD deliberately does
not receive diagnosis packages or Runtime traces; it uses only candidate
Skills, their short `solves` paragraphs, similarity relations, and related
official Skills.

## 2. Maintained project tree

```text
single_agent_mim/
├── configs/                    # model and workflow configuration
├── data/splits/                # frozen dataset split metadata
├── docs/                       # English developer/agent documentation
├── exp_raw_data/               # checked final experiment exports
├── outputs/                    # mutable run state
├── prompts/
│   ├── access.md
│   ├── construction_extraction.md
│   ├── construction_decision.md
│   ├── diagnosis/
│   └── skill_maker/
│       ├── candidate_generation.md
│       └── batch_crud.md
├── reports/                    # Chinese user-facing reports
├── scripts/                    # thin operational entry points
├── src/mim/
│   ├── agents/
│   │   ├── access.py
│   │   ├── construction.py
│   │   ├── access_failure.py
│   │   ├── cons_failure.py
│   │   └── skill_learning.py
│   ├── diagnosis/              # isolated Diagnosis V3 workflows
│   ├── retrieval/              # memory retrieval
│   ├── skill_maker/
│   │   ├── models.py
│   │   ├── repository.py
│   │   ├── batch.py
│   │   └── validator.py
│   ├── storage/                # SQLite memory and Runtime traces
│   ├── skills.py               # official Runtime Skill retrieval
│   └── workflows/              # use/train/evaluate orchestration
└── tests/
```

All model-facing prompts are English. User-facing reports are Chinese.

## 3. Runtime Skill retrieval

Runtime can only read official active Skills. Candidate files and pending CRUD
transactions are never part of Runtime retrieval.

### Access

- Retrieval side: `access`.
- Query: the exact QA question.
- Injection: selected Top-k Access Skills are included in the Access & Answer
  system context before the ReAct chain starts.

### Construction

- Retrieval side: `construction`.
- Query: the complete incoming session rendered as `speaker: content` lines.
- Injection: the same selected Construction Skills are supplied to candidate
  extraction and memory CRUD planning for that session.

### Ranking and disclosure

Official Runtime ranking uses:

```text
85% semantic similarity
15% lexical overlap
```

The trace records:

- frozen official Bank version;
- exact retrieval query and side;
- selected Top-k Skill snapshots and scores;
- the next `skill_disclose_k` Skill snapshots that were ranked but not loaded;
- semantic and lexical score components.

The selected Skills affect Runtime behavior. Disclosed near misses are stored
only for later Diagnosis and candidate generation.

## 4. Runtime trace persistence

Access traces are stored in `access_runs.skill_trace_json`.

Construction traces are stored in
`construction_commits.skill_trace_json`. The commit is linked to every input
message through `construction_inputs`, allowing Construction Diagnosis to load
only the Skill traces associated with annotated source messages.

Human-readable copies are also written under:

```text
outputs/<run-id>/traces/access_traces.jsonl
outputs/<run-id>/traces/construction_traces.jsonl
```

Existing SQLite files are migrated in place by adding the two JSON columns if
they are absent.

## 5. Diagnosis contracts

### Answer

Answer Diagnosis receives only the exact memory context visible to the Runtime
answer model. Its report also carries the Access Skill trace for audit, but an
Answer failure remains record-only.

### Access

Access Diagnosis receives:

- question and reference answer;
- current memories linked algorithmically from annotated evidence;
- the current Runtime search chain;
- the Access Skill trace, including selected Skills and disclosed near misses.

The Skill trace is included in the repair package only after an Access failure
is established. Access Diagnosis still does not read raw conversations,
construction history, or older memory versions.

### Construction

Construction Diagnosis first screens only current related memories. If a
construction problem is possible, it progressively loads:

- annotated raw source messages;
- memory candidates and decisions;
- chronological memory version changes;
- the Construction Skill traces for the commits that processed those source
  messages.

Only the earliest construction error is reported. The relevant Construction
Skill traces are attached to the report and repair package for candidate
generation.

## 6. Skill data contract

An official or candidate Skill has the Runtime-visible body:

```json
{
  "name": "Short human-readable name",
  "description": "When this Skill should be retrieved.",
  "content": [
    "One concise action or rule.",
    "Another action when needed."
  ]
}
```

A candidate additionally contains:

```json
{
  "candidate_id": "...",
  "side": "access",
  "solves": "One short paragraph explaining the general problem solved.",
  "related_existing_skill_ids": [],
  "source_diagnosis_id": "..."
}
```

`solves` is intentionally short. It carries enough meaning into batch CRUD
without forwarding the complete diagnosis package.

## 7. Physical Skill storage

```text
outputs/<train-run-id>/skills/
├── official/
│   ├── banks/
│   │   ├── bank_v000.json
│   │   └── bank_v001.json
│   └── selected.json
├── candidates/
│   ├── access/<candidate-id>/
│   └── construction/<candidate-id>/
└── transactions/<transaction-id>.json
```

Rules:

- Runtime reads only `official/`.
- Candidate generation writes only `candidates/<side>/`.
- Access and Construction candidate pools are physically separate.
- A candidate never becomes active through a file move or direct write.
- Only a validated batch transaction can create a new official Bank version.
- Legacy `skills/banks` and `skills/selected.json` are copied into
  `official/` on first load; the legacy files are not deleted automatically.

## 8. Candidate generation

For each diagnosed Access or Construction failure, Candidate Skill Agent reads:

- the completed diagnosis package;
- selected Runtime Skills;
- disclosed but not selected near-neighbor Skills;
- the exact official Bank version used at Runtime.

It returns one of:

```text
PROPOSE_SKILL
NO_CHANGE_ALREADY_COVERED
NO_CHANGE_NOT_A_SKILL_PROBLEM
```

Only `PROPOSE_SKILL` writes a candidate file. Candidate generation never writes
the official Bank.

## 9. Candidate clustering

Access and Construction are clustered independently. Candidate embeddings use:

```text
45% description embedding
35% content embedding
20% solves embedding
```

The implementation uses deterministic spherical K-means without adding a
scikit-learn dependency:

```text
K = ceil(candidate_count / target_cluster_size)
target_cluster_size = 8 by default
maximum ordinary CRUD batch size = 10
```

After K-means, groups are repaired algorithmically:

- candidates sharing a related official Skill are joined;
- candidates with strong lexical overlap are joined;
- oversized groups are split into bounded planning batches.

## 10. Unified batch retrieval

For every candidate group, the system computes the exact
`candidate × official Skill` matrix. The official Bank is expected to remain
small enough that approximate vector search is unnecessary.

The batch score uses:

```text
50% description semantic similarity
30% content semantic similarity
20% BM25 lexical similarity
```

Selection rules:

- every candidate keeps at least three official neighbors;
- candidate-declared related Skill IDs are mandatory;
- common high-coverage official Skills are added;
- the final official context is capped by
  `training.skill_batch_bank_context`.

The CRUD Agent receives the selected official Skill snapshots and the complete
candidate-to-selected-Skill relation rows.

## 11. Batch CRUD

Batch CRUD does not receive diagnosis packages or Skill traces. One batch may
create several Skills and emit several atomic operations:

```text
add_skill
rename_skill
update_description
add_content
update_content
delete_content
move_content
delete_skill
```

`delete_skill` is a soft deletion from the active Bank. Historical versions
remain in immutable Bank snapshots.

Every candidate must receive exactly one resolution. The Agent only proposes a
plan; it cannot write files directly. The deterministic executor validates:

- frozen base Bank version;
- candidate coverage;
- source candidate IDs;
- target existence and side;
- expected Skill versions;
- content indices and expected old content;
- cross-side mutation attempts.

## 12. Conflict handling and publication

All semantic groups for one side are planned against one frozen official Bank.
The workflow compares their write sets.

- Disjoint plans remain separate internally.
- Plans writing the same Skill are combined and regenerated by the CRUD Agent.
- After conflicts are removed, all remaining operations for that side are
  combined into one release transaction.
- Access and Construction each publish at most one official Bank version per
  consolidation round.

This keeps candidate work parallel while preventing stale concurrent writes
and excessive official Bank versions.

## 13. Evaluation boundary

Batch publication performs schema and transaction validation. It does not loop
on the same failures until retrieval succeeds.

Actual Skill effectiveness is measured in the next Runtime iteration through:

- natural Skill retrieval;
- selected versus disclosed Skill traces;
- Access or Construction error changes;
- final LLM-as-Judge correctness;
- Bank size, duplicate rate, and update churn.

Maintenance selects an internal `official/selected.json`, then exports the
winner into physically isolated `access_skill_bank_v1.json` and
`construction_skill_bank_v1.json` files. Evaluation loads their parent through
`--skill-bank-dir`, freezes Bank1, and never invokes Diagnosis, candidate
generation, or CRUD.

## 14. Language and verification

Before a formal run:

```powershell
rg -n --pcre2 "\p{Han}" prompts
python -m pytest -q
```

Prompt search must return no matches. The test suite must pass without calling
external model APIs.
