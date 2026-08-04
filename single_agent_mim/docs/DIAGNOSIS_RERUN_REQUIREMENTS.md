# Diagnosis Rerun Requirements

This document is an execution specification for coding agents. It is not the
user-facing quality report.

## Current state

- Diagnosis v1 was stopped and deleted.
- Runtime source runs remain valid.
- Runtime prompts were already English and do not require a rerun.
- Access and Construction Diagnosis prompts are now English.
- Skill-Maker prompts are English.

## Required behavior before v2

1. Judge the original Runtime prediction semantically against the reference
   before treating low Token-F1 as a failure.
2. Record these as separate values:
   - `runtime_prediction_correct`
   - `maintenance_can_answer_from_returned_memory`
3. Skip Failure Diagnosis when the original Runtime prediction is semantically
   correct.
4. Pass `subject`, `world_start`, and `world_end` to Answer Check.
5. Treat Access as a repairable failure only when a necessary available
   snapshot memory was never returned.
6. Keep contradictory returned memories as audit context only.
7. Emit only canonical Construction stages:
   - `ingestion`
   - `extraction`
   - `wrong_candidate`
   - `wrong_skip`
   - `persistence`
   - `initial_memory`
   - `update_loss`
   - `wrong_merge`
   - `correction_failure`
   - `provenance_missing`
8. Canonicalize common aliases defensively.
9. Mark any package with a component model error as
   `partial_model_error`, never `completed`.
10. Ensure every repairable problem has a non-empty repair package.
11. Feed the complete deterministic repair package to Skill-Maker.

## Smoke gate

Run five conv-30 failures with a new run ID. Do not start the full dataset
unless:

- every prompt passes the English-only scan;
- no component has `model_error`;
- every routed report has a non-empty repair package;
- temporal questions expose world-valid time to Answer Check;
- no Skill directory is created;
- source SQLite remains unchanged.

See `RUNBOOK.md` for commands and paths.
