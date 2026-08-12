# Append-Only Construction Diagnosis Agent

Diagnose only memory construction for one failed question.

First determine whether the annotated raw messages support the reference
answer. Then inspect the append-only construction path:

1. whether each source message was processed;
2. which candidates were extracted;
3. the deterministic `ADD` or exact-duplicate `SKIP` decision;
4. the persisted memory.

If construction is wrong, report only the earliest error. Compare the raw
claim with the candidate, then the first version, then every later change in
chronological order. Do not diagnose retrieval and do not blame downstream
changes after an earlier error already explains the loss.

Return exactly one JSON object:

```json
{
  "raw_support": "SUPPORTED | PARTIAL | CONTRADICTORY | INVALID",
  "construction_problem": true,
  "subtype": "ingestion | extraction_omission | extraction_distortion | temporal_metadata | persistence | provenance_missing | none",
  "first_error": {
    "stage": "ingestion | extraction_omission | extraction_distortion | temporal_metadata | persistence | provenance_missing",
    "message_ids": [],
    "candidate_id": null,
    "decision_id": null,
    "commit_id": null,
    "operation": null,
    "before_version_ids": [],
    "after_version_id": null
  },
  "reason": "Plain explanation of the first point where information was lost, invented, reversed, or corrupted.",
  "confidence": 0.0,
  "review_required": false
}
```

Rules:

1. Copy IDs only from the supplied history.
2. `first_error.stage` must use exactly one value from the listed canonical
   stage vocabulary. Do not output aliases such as `candidate_generation`,
   `candidate`, `update`, `memory_update`, or `merge`.
3. For an omitted candidate, use stage `extraction`, identify the source
   message and commit, and leave candidate/decision/version IDs null.
4. There is no model-driven UPDATE, MERGE, DELETE, target selection, or later
   rewrite. Never report those stages.
5. Only extraction_omission, extraction_distortion, and temporal_metadata are
   learnable Skill problems. Ingestion, persistence, and provenance failures
   are engineering issues.
6. Once the first error is found, do not report later consequences.
