# Access Composition Diagnosis (A2)

Diagnose whether A2 received sufficient visible memories but selected,
combined, or expressed them incorrectly. Use only the exact visible context.

Split the reference into atomic claims. For each claim, cite visible version
IDs and assign `FULL`, `PARTIAL`, `MISSING`, or `INCORRECT`. `FULL` requires
all entities, relations, polarity, quantities, and times needed by that claim.
A list or multi-hop answer is sufficient only when every required item/hop is
covered and no visible evidence creates an unresolved contradiction.

Do not answer again, use outside knowledge, inspect unreturned memories, or
propose a Skill. Copy IDs exactly.

Return exactly:

```json
{"essential_reference_claims":[{"claim":"atomic claim","supporting_retrieved_version_ids":[],"coverage":"FULL|PARTIAL|MISSING|INCORRECT"}],"retrieved_context_supports_abstention":false,"unresolved_material_contradiction":false,"reason":"whether A2 had enough evidence and what it mishandled","confidence":0.0,"review_required":false}
```
