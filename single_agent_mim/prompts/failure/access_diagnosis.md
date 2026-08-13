# Access Retrieval Diagnosis

Diagnose only retrieval for one failed question. You receive the question and
reference answer, memory versions available at the frozen snapshot that descend
from annotated evidence messages, and the complete fixed Access search chain:
the mandatory original-question retrieval plus A1's single supplemental round.

Determine which available versions were necessary and whether each appeared in
any search step. Do not read or infer from raw conversation text. Do not propose
queries, filters, scores, weights, Skills, or a different topology. A separate
component diagnoses A2 answer composition.

Return exactly:

```json
{"necessary_available_version_ids":[],"conflicting_returned_version_ids":[],"reason":"plain evidence-based explanation","confidence":0.0,"review_required":false}
```

Use only supplied version IDs. A construction defect does not erase a genuine
retrieval defect for useful memories that did exist at the snapshot.
