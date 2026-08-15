# Mem0 Answer-Side Skill Candidate Generator

Turn one validated, Skill-learnable Mem0 answer failure into at most one
reusable answer-side Skill. The runtime has already completed exactly one fixed
Mem0 search before the Skill is applied. There is no A1, A2, retrieval planner,
query rewrite, second search, adjustable top-k/depth, or reranker.

Reject the case unless the diagnosis explicitly says:

- `retrieved_context_sufficient` is true;
- `skill_learnable` is true;
- the trigger is observable from the question and returned memories;
- the correction changes evidence interpretation, selection, or answer
  composition only.

The Skill must never request or simulate search, retrieval, query expansion,
top-k/depth changes, hidden context, or extra evidence. It must not encode a
specific answer, person, date, place, memory ID, or training topic.

Generalize the failure mechanism, not the example. Preserve these five parts in
`mechanism_signature`: observable trigger, evidence precondition, failed
behavior, corrective operation, and safety boundary. If any part cannot be
stated without case-specific details, return `NO_CHANGE_NOT_A_SKILL_PROBLEM`.

A single diagnosis is only a proposal source, not proof of generalization.
Candidate provenance must be preserved so later stages can require independent
support and contrastive validation.

Return exactly:

```json
{"decision":"PROPOSE_SKILL|NO_CHANGE_ALREADY_COVERED|NO_CHANGE_NOT_A_SKILL_PROBLEM","maintenance_intent":"ADD|REVISE|REMOVE|PRESERVE","related_existing_skill_ids":[],"mechanism_signature":{"observable_trigger":"","evidence_precondition":"","failed_behavior":"","corrective_operation":"","safety_boundary":""},"skill":{"name":"short mechanism","description":"When an observable question/evidence condition holds, including its boundary","content":["One concise answer-side operation","One evidence/safety check"]},"solves":"reusable answer failure mechanism","reason":"why this is executable after fixed Mem0 search"}
```
