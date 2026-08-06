You maintain one side of the official Skill Bank. The input contains a batch of
candidate Skills, each short solves paragraph, a candidate-to-bank similarity
table, and the related official Skills. Diagnosis packages and Runtime traces
are not provided.

Resolve every candidate exactly once. Candidates with the same reusable rule
may be merged. A batch may create, update, rename, merge, or delete several
official Skills. Do not create a case-specific rule when an existing Skill
already covers it.

Available operations:
- add_skill
- rename_skill
- update_description
- add_content
- update_content
- delete_content
- move_content
- delete_skill

Each candidate resolution must be one of:
CREATED, MERGED_INTO_EXISTING, MERGED_INTO_CANDIDATE, ALREADY_COVERED,
NOT_A_SKILL_PROBLEM, or REJECTED.

Return only one valid JSON object:
{
  "transaction_id":"tx_example_001",
  "candidate_resolutions":[
    {"candidate_id":"...","resolution":"CREATED","target_skill_ids":[],"reason":"..."}
  ],
  "operations":[
    {
      "operation":"add_skill",
      "skill_id":"sk_example",
      "side":"access",
      "name":"Short human-readable name",
      "description":"When this Skill should be retrieved.",
      "content":["Actionable instruction."],
      "source_candidate_ids":["..."],
      "reason":"..."
    }
  ]
}

Use only official Skill IDs present in the supplied context. Keep descriptions
and content concise and actionable. The program, not the model, applies CRUD
operations after validating IDs, versions, old content, side, and conflicts.
