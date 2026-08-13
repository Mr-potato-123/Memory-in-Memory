# Access Skill Candidate Generator

Turn one validated A1 retrieval failure or A2 evidence-composition failure
into at most one reusable Access Skill. A Skill is an optional procedural
reference, never an answer and never case-specific data.

Runtime contract:

- Mandatory initial hybrid retrieval is fixed.
- A1 may formulate at most one bounded supplemental retrieval round and state
  evidence requirements.
- A2 selects visible evidence, combines facts, and answers once.
- There is no agent loop and no standalone reranker.

A useful Skill may improve query formulation, entity/time scope, whether
history is needed, evidence requirements, evidence selection, list coverage,
multi-hop composition, temporal comparison, contradiction handling, or
grounded sufficiency. It must not prescribe a specific answer, inject names/
dates/IDs, demand arbitrary repeated searches, or turn missing evidence into
an unsupported conclusion.

Generalize the failure mechanism, not the training topic. Triggers must be
observable from query or evidence structure (for example, explicit time scope,
comparison, list coverage, or a missing bridge), not from a combination of
case-specific occupations, activities, places, relationships, or events. If
the rule cannot be expressed without those topic details, return
`NO_CHANGE_NOT_A_SKILL_PROBLEM`.

Prefer revising a relevant ineffective selected Skill over adding overlap.
Remove or narrow a Skill implicated in regression. Preserve when the failure
does not reveal a reusable procedural rule.

Return exactly:

```json
{"decision":"PROPOSE_SKILL|NO_CHANGE_ALREADY_COVERED|NO_CHANGE_NOT_A_SKILL_PROBLEM","maintenance_intent":"ADD|REVISE|REMOVE|PRESERVE","related_existing_skill_ids":[],"skill":{"name":"short mechanism","description":"When an observable query/evidence condition holds","content":["A1 or A2 procedure","evidence check"]},"solves":"reusable failure mechanism","reason":"why this is learnable and bounded"}
```
