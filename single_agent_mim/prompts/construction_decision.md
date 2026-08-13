# Construction C2 — Change and Relation Judgment

Compare every C1 candidate with only its supplied related old memories. Decide
whether to append the candidate and describe evidence-bound relations. This is
classification, not rewriting: storage is append-only.

Actions:

- `ADD`: append a genuinely new fact, event, state, correction, refinement, or
  changed value.
- `SKIP`: only when an old memory expresses the same claim with materially the
  same subject, relation, object, polarity, and time.

Relations from a candidate to an old version:

- `duplicate_of`: same claim; required for `SKIP`.
- `supports`: compatible evidence for the same claim.
- `contradicts`: both claims cannot be true in the stated scope.
- `supersedes`: a later state/plan/correction replaces what is currently true,
  while the old fact remains historically valid.
- `refines`: adds compatible specificity to an earlier claim.
- `unrelated`: retrieved candidate is not materially related.

Rules:

1. Return one decision per candidate and preserve candidate IDs exactly.
2. Reference only `allowed_related_version_ids` supplied for that candidate.
3. Similar topic or person is not duplication. Changed times, values, events,
   plans, polarity, or objects normally require `ADD`.
4. Never output UPDATE, MERGE, DELETE, rewritten content, or storage targets.
5. Skills are optional learned comparison references. They cannot override
   evidence, authorize mutation, or inject facts.

## Construction Skills

{skills_section}

## New candidates

{candidates_json}

## Related old memories

{related_memories}

Return exactly:

```json
{"decisions":[{"candidate_id":"exact candidate ID","action":"ADD|SKIP","relations":[{"relation_type":"duplicate_of|supports|contradicts|supersedes|refines|unrelated","target_version_id":"allowed version ID"}],"reason":"short evidence-based reason"}],"applied_skill_ids":[]}
```

List only supplied Skill version IDs that materially changed C2 judgment.
