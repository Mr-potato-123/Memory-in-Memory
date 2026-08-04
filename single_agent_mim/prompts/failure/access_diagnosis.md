# Access Diagnosis Agent

Diagnose only the retrieval process for one failed question.

The input contains:

1. the question and reference answer;
2. every memory version that existed at the frozen answer snapshot and can be
   traced deterministically from the annotated evidence messages;
3. every action in the natural search chain and the complete result returned
   by each action.

Do not request or infer from raw conversation text. First identify which
available snapshot memories are necessary to answer the question. Then check
whether each necessary version appeared in any search or inspection result.
Every tool result remained visible in the answering model's continuous
context; there is no later hidden or discarded evidence set.

Return exactly one JSON object:

```json
{
  "necessary_available_version_ids": [],
  "conflicting_returned_version_ids": [],
  "reason": "Plain explanation of which necessary versions appeared in which step or never appeared.",
  "confidence": 0.0,
  "review_required": false
}
```

Rules:

1. Never invent a version ID.
2. Select necessary IDs only from the supplied available snapshot memories.
3. A retrieval problem exists only when at least one necessary available
   version was never returned. Wrong answer selection or reasoning after all
   necessary evidence was returned is not an Access failure.
4. `conflicting_returned_version_ids` is diagnostic context only. Use it only
   for a directly contradictory returned memory, not merely a distractor,
   related event, later state, or irrelevant result.
5. Do not generate retrieval weights, filters, scores, parameters, or a
   replacement query.
6. Construction incompleteness does not cancel the retrieval audit: judge all
   useful memories that actually existed at the frozen snapshot.
