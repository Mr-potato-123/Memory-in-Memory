# Mem0 Fixed-Search Diagnosis

Diagnose whether the single fixed Mem0 search omitted evidence required to
answer the question. Mem0 has no A1, A2, query-planning stage, second search,
or diagnosis-controlled retrieval depth in this experiment.

This component is observational only. Its findings are used to evaluate or
repair the memory/retrieval system and are never converted into an Access
Skill. Never recommend query expansion, supplemental retrieval, a larger
top-k, increased depth, reranking, or repeated search.

You receive current memories linked to the annotated source and the exact
memories returned by the runtime search. Decompose the reference into essential
claims. For each claim, decide whether the returned memories already contain a
minimum sufficient support set. Do not mark a retrieval failure merely because
some redundant, corroborating, newer, or more detailed memory was not returned.

If any returned memory combination fully supports the required answer, coverage
is `FULL`, even when other useful memories exist outside the top-k. `PARTIAL` or
`MISSING` is appropriate only when the missing content is necessary for the
claim itself.

Do not inspect raw dialogue, diagnose construction, evaluate answer wording, or
propose a Skill. Do not invent IDs or treat topic overlap as support.

Return exactly:

```json
{"essential_reference_claims":[{"claim":"atomic claim","supporting_current_version_ids":[],"supporting_retrieved_version_ids":[],"retrieval_coverage":"FULL|PARTIAL|MISSING|INCORRECT"}],"reason":"whether required evidence, rather than redundant evidence, was omitted","confidence":0.0,"review_required":false}
```
