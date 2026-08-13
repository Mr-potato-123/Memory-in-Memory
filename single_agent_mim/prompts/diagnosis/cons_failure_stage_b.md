# Construction Diagnosis (C1/C2)

Stage A has established that current memory is incomplete or incorrect.
Verify the annotated raw evidence, then locate the earliest failure in:

`ingestion → C1 extraction → C2 ADD/SKIP and relation judgment → persistence`

C1 failures include omitted facts, distorted entities/relations/polarity/
quantity, and wrong temporal metadata. C2 failures include incorrectly
skipping a non-duplicate candidate, adding a true duplicate, or misclassifying
`duplicate_of`, `supports`, `contradicts`, `supersedes`, or `refines` in a way
that loses the change structure. Storage is append-only: UPDATE, MERGE, DELETE,
replacement, and destructive rewrite do not exist in this runtime.

Return exactly:

```json
{"raw_support":"SUPPORTED|PARTIAL|CONTRADICTORY|INVALID","construction_problem":true,"affected_reference_claim":"lost or corrupted fact","affected_memory_ids":[],"subtype":"ingestion|extraction_omission|extraction_distortion|temporal_metadata|relation_judgment|wrong_skip|persistence","first_error":{"stage":"ingestion|extraction_omission|extraction_distortion|temporal_metadata|relation_judgment|wrong_skip|persistence","message_ids":[],"candidate_id":null,"decision_id":null,"commit_id":null,"change_id":null,"operation":null,"before_version_ids":[],"after_version_id":null},"reason":"raw evidence, observed C1/C2 output, and earliest mismatch","confidence":0.0,"review_required":false}
```

Only C1 extraction/distortion/time and C2 relation/wrong-skip decisions are
learnable Construction Skill sources. Ingestion/persistence are engineering
issues. Never invent provenance IDs.
