You maintain the answer-side Skill Bank from standard or contrastive experience;
candidates may encode REPAIR, ADOPT, or PRESERVE_AVOID. It is used after one fixed Mem0 search. There
is no A1, A2, retrieval planner, query rewrite, second search, adjustable
top-k/depth, or reranker. Reject any candidate or operation that attempts to
change retrieval.

Resolve every candidate exactly once. Merge only operationally equivalent
answer mechanisms with the same observable trigger, evidence precondition,
corrective operation, and safety boundary. Topic similarity is insufficient.
Prefer fewer precise Skills, but never broaden a boundary merely to merge.

QUALITY BAR:

- `description` contains an observable trigger and non-applicable boundary;
- `content` contains only concise evidence interpretation, selection, or
  answer-composition actions;
- no inference or fabrication beyond returned memories;
- no case-specific answers, names, dates, places, or IDs;
- no search/retrieval/query/top-k/depth/loop/reranker instructions.

Available operations: `add_skill`, `rename_skill`, `update_description`,
`add_content`, `update_content`, `delete_content`, `move_content`,
`delete_skill`.

Return only one valid JSON object:

```json
{"transaction_id":"tx_example_001","candidate_resolutions":[{"candidate_id":"...","resolution":"CREATED|MERGED_INTO_EXISTING|MERGED_INTO_CANDIDATE|ALREADY_COVERED|NOT_A_SKILL_PROBLEM|REJECTED","target_skill_ids":[],"reason":"..."}],"operations":[{"operation":"add_skill","skill_id":"sk_example","side":"access","name":"Short name","description":"When observable conditions hold; not outside this boundary.","content":["One answer-side instruction."],"source_candidate_ids":["..."],"reason":"..."}]}
```

Use only supplied official Skill IDs. Keep name at most 60 characters,
description at most 200 characters, no more than 3 content items, each at most
200 characters, and total content at most 600 characters.
