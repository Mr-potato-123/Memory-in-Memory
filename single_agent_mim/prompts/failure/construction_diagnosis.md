# Fixed C1/C2 Construction Diagnosis

Diagnose only memory construction for one failed question. First verify that
the annotated raw messages support the reference answer. Then inspect the full
chronological construction history: ingestion, C1 candidates, C2 decisions
(including SKIP and semantic relations), persistence, and source provenance.

Report only the earliest error. C1 errors are omission, distortion, or temporal
metadata. C2 errors are wrong duplicate skipping or a harmful relation judgment.
Storage is append-only; UPDATE, MERGE, DELETE, replacement, and destructive
rewriting do not exist. Do not diagnose retrieval or answer composition.

Return exactly:

```json
{"raw_support":"SUPPORTED|PARTIAL|CONTRADICTORY|INVALID","construction_problem":true,"subtype":"ingestion|extraction_omission|extraction_distortion|temporal_metadata|relation_judgment|wrong_skip|persistence|provenance_missing|none","first_error":{"stage":"ingestion|extraction_omission|extraction_distortion|temporal_metadata|relation_judgment|wrong_skip|persistence|provenance_missing","message_ids":[],"candidate_id":null,"decision_id":null,"commit_id":null,"operation":null,"before_version_ids":[],"after_version_id":null},"reason":"plain explanation of the earliest error","confidence":0.0,"review_required":false}
```

Never invent provenance IDs.
