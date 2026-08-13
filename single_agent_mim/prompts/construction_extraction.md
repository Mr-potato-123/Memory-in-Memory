# Construction C1 — Evidence-Bound Fact Extraction

Extract durable, self-contained memories from the current session. This stage
does not see or edit old memory. Preserve what the session says accurately;
do not decide whether a fact replaces, duplicates, or conflicts with history.

Remember durable profile facts, preferences, relationships, states, events,
plans, commitments, changes, corrections, negations, named entities, numbers,
and dates from every speaker. Exclude greetings, acknowledgements, empty
assistant echoes, and unsupported implications.

Memory requirements:

1. Resolve pronouns to explicit people when the session supports it.
2. Resolve relative dates using message/session time when possible.
3. Preserve polarity, quantities, modifiers, uncertainty, and whether a claim
   is a plan, completed event, preference, or current state.
4. Keep one coherent fact/event/state per memory. Combine only details that
   would normally be recalled together; do not merge unrelated claims.
5. Use only `profile`, `preference`, `state`, `event`, `plan`, `relationship`.
6. Copy source message IDs exactly and cite every message that directly
   supports a synthesized memory.
7. Skills are optional learned references. Apply an item only when its full
   observable trigger is present. Evidence and this contract override Skills.

## Construction Skills

{skills_section}

## Session time

{session_time}

## Messages

{session_messages}

Return exactly:

```json
{"candidates":[{"memory_kind":"event","subject":"explicit subject","predicate":"short retrieval label or null","object_text":"object or null","content":"standalone evidence-bound memory","world_start":"absolute time or null","world_end":null,"source_message_ids":["exact ID"],"entities":[],"keywords":[],"importance":0.5,"confidence":0.9}],"applied_skill_ids":[]}
```

Return `{"candidates":[],"applied_skill_ids":[]}` if nothing durable is stated.
List only supplied Skill version IDs that materially changed C1 output.
