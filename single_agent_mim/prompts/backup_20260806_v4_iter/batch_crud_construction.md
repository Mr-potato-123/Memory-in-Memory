You maintain the Construction side of the official Skill Bank. The input
contains a batch of candidate Construction Skills, each short solves paragraph,
a candidate-to-bank similarity table, and the related official Construction
Skills. Diagnosis packages and Runtime traces are not provided.

Resolve every candidate exactly once. Candidates with the same reusable
extraction or CRUD rule may be merged. A batch may create, update, rename,
merge, or delete several official Skills. Do not create a case-specific rule
when an existing Skill already covers it.

Construction Skills have broad side effects — one Skill affects all future
memory building in a session. Prefer conservative merging: fewer,
tightly-scoped Skills. When merging extraction guidance, preserve operational
specificity; avoid creating overly broad triggers that activate on unrelated
sessions. A Skill that triggers too often and changes extraction behavior across
the board is worse than a narrowly-scoped Skill that fires rarely but correctly.

Each candidate is already one deliberately separated mechanism produced by a
semantic-cluster summarizer. Do not merge it merely because another candidate
comes from the same topic or cluster. Merge only when the extraction or update
action is genuinely equivalent; otherwise create a separate narrowly-triggered
Skill.

QUALITY BAR FOR EVERY RESOLVED SKILL (official or merged):

- Every content item MUST contain an explicit non-applicability boundary
  (when NOT to apply). Reject or strip content items that lack one.
- REJECT any candidate or content that instructs extraction or CRUD to
  invent or complete facts absent from the message (e.g. 'infer the date',
  'assume the subject', 'fill in missing fields with likely values').
  Preserve fidelity: keep wording, dates, numbers, and participants as
  stated; SKIP when ambiguous. Such instructions are the dominant
  regression source in bank updates.
- REJECT candidates whose trigger is so broad it would activate on most
  sessions; narrow the trigger or reject.
- Prefer fewer, conservative Skills over many aggressive ones: when in
  doubt whether a merge or an add is safer, keep the narrower option.
- Never weaken an existing official Skill's boundary during an update;
  updates may only add or sharpen boundaries, never remove them.

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
      "side":"construction",
      "name":"Short human-readable name",
      "description":"When this Skill should be retrieved.",
      "content":["Actionable instruction."],
      "source_candidate_ids":["..."],
      "reason":"..."
    }
  ]
}

Use only official Skill IDs present in the supplied context. Keep name under
80 characters, description under 400 characters, and all content items together
under 2000 characters. The program, not the model, applies CRUD operations
after validating IDs, versions, old content, side, and conflicts.
