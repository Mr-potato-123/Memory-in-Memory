# Construction Skill Candidate Generator

Turn one validated Construction failure into at most one reusable procedural
Skill for the fixed C1/C2 pipeline.

- C1 extracts precise, evidence-bound facts from the current session.
- C2 compares each candidate with a bounded old-memory pool, chooses ADD/SKIP,
  and labels append-only semantic relations.

A Skill may improve omission resistance, atomicity, coreference, polarity,
quantities, time normalization, plans versus completed events, assistant facts,
duplicate judgment, or recognition of support/contradiction/supersession/
refinement. It may not invent facts, copy case entities/dates/answers/IDs, or
request UPDATE, MERGE, DELETE, overwrite, database targets, or extra loops.

Generalize the first-break mechanism, not the training topic. The observable
trigger should describe discourse or evidence structure (for example, a
contrast, correction, causal transition, or several independently asserted
facts), not a combination of domain nouns from this case. If no mechanism can
be stated without the case's occupations, activities, places, relationships,
or events, return `NO_CHANGE_NOT_A_SKILL_PROBLEM`.

Prefer revising a relevant ineffective selected Skill over creating overlap.
Remove or narrow a Skill implicated in regression. Do not create Skills for
ingestion, persistence, malformed output, or other engineering failures.

Return exactly:

```json
{"decision":"PROPOSE_SKILL|NO_CHANGE_ALREADY_COVERED|NO_CHANGE_NOT_A_SKILL_PROBLEM","maintenance_intent":"ADD|REVISE|REMOVE|PRESERVE","related_existing_skill_ids":[],"skill":{"name":"short mechanism","description":"When an observable session/candidate condition holds","content":["C1 or C2 procedure","verification boundary"]},"solves":"reusable first-break mechanism","reason":"why this belongs to a Skill"}
```
