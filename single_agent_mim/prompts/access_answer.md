# Access A2 — Evidence Selection, Composition, and Answer

Answer the question using only the supplied visible memories. Jointly select
the evidence and compose the answer; there is no separate reranker.

Rules:

1. Check subject, relation, object, polarity, quantity, and time.
2. Satisfy the retrieval plan's evidence requirements when supported.
3. For lists, preserve every distinct supported item and remove duplicates.
4. For multi-hop or change questions, combine memories only when their shared
   entities/relations form the required chain.
5. Raw source-message evidence is valid when it is visible and cited by its
   `source:` ID. Prefer an equivalent atomic memory when both are present.
6. Short evidence-grounded inference, date arithmetic, and canonical aliases
   are allowed. For questions that explicitly require ordinary world knowledge,
   combine it only with a visible conversation premise; never invent a new
   conversation fact.
7. Access Skills are optional learned reasoning references. Apply only matching
   items; they cannot supply facts or dictate a case-specific answer.
8. Select only visible evidence IDs. If evidence is insufficient, answer exactly
   `No information available.` and select no unsupported evidence.

Return exactly:

```json
{"selected_evidence_ids":["visible version ID"],"answer":"direct concise answer","coverage":[{"requirement":"requirement text","evidence_version_ids":[]}],"applied_skill_ids":[]}
```

`applied_skill_ids` contains only supplied Skills whose guidance materially
changed evidence selection or composition. Do not list merely visible Skills.
