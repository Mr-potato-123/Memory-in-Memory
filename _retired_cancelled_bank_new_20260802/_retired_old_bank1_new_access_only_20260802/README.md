# Bank1_new Validation Report

## Naming and scope

- `Bank0`: no-Skill baseline.
- `Bank1`: the preserved first published Skill Bank.
- `Bank1_new`: regenerated from the same Bank0 diagnosis packages after revising only the Skill-Maker abstraction instructions.
- Validation split: `conv-26` and `conv-41`, 392 questions.
- Runtime: Qwen3-8B, non-thinking, temperature 0.
- Judge: DeepSeek-V4-Flash, `locomo_semantic_judge_v2`.

## Prompt revision

Candidate generation no longer prohibits broad abstraction from a single diagnosis or requires a nearest negative case. It now asks the model to:

1. abstract the reusable failure mechanism rather than copy entities, dates, answers, or topic nouns;
2. explain briefly in `solves` the diagnosed cause, why the abstraction is supported, and its general applicability;
3. allow one diagnosis to produce a reusable candidate;
4. let batch CRUD merge candidates by transferable mechanism.

Runtime retrieval, diagnosis inputs, validation questions, and Judge prompt were not changed.

## Results

| Version | F1 | C | P | I | C rate | C+P rate |
|---|---:|---:|---:|---:|---:|---:|
| Bank0 | 32.63 | 165 | 52 | 175 | 42.09% | 55.36% |
| Bank1 | **36.32** | **185** | **60** | **147** | **47.19%** | **62.50%** |
| Bank1_new Access-only | 34.55 | 176 | 46 | 170 | 44.90% | 56.63% |
| Bank1_new Access + Construction | 33.08 | 173 | 51 | 168 | 44.13% | 57.14% |

The official Bank1_new selection is Access-only because the selection priority is highest C, then lower I, then F1. The Access + Construction result is retained under `validation/candidates/access_construction` as an ablation.

## Skill Bank size

| Build | Access Skills | Construction Skills |
|---|---:|---:|
| Previous discarded Bank1_new | 88 | 91 |
| Regenerated Bank1_new candidate | 68 | 92 |
| Official Bank1_new publication | 68 | 0 |

The abstraction change made the Access bank substantially more compact and improved Access-only F1 over the discarded run (33.63 to 34.55). It did not recover the preserved Bank1 result. Construction remains negative for F1 and strict correctness on this validation set.

