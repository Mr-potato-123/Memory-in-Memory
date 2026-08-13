# Access Retrieval Diagnosis (A1)

Diagnose whether useful memory versions existed at the frozen snapshot but
were absent from the complete fixed-topology retrieval context.

You receive the question, reference answer, relevant current snapshot
memories, and both retrieval stages: mandatory original-query retrieval and
the one bounded supplemental round. Decompose the reference into essential
claims, identify which supplied current versions materially support each
claim, then determine whether those versions were returned.

Do not inspect raw dialogue, diagnose construction, answer the question, or
propose a Skill. Do not invent IDs. Do not treat mere topic overlap as support.

Return exactly:

```json
{"essential_reference_claims":[{"claim":"atomic claim","supporting_current_version_ids":[]}],"reason":"what useful evidence was or was not retrieved","confidence":0.0,"review_required":false}
```
