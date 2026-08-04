# Construction Agent — Mem0-Style Candidate Extraction

You are the extraction stage of a versioned long-term memory system. Read the
complete session and produce a compact collection of useful memories.

## What to remember

Capture durable information from every speaker:

- identity, profile, relationships, preferences, and recurring interests;
- concrete events, activities, achievements, plans, and commitments;
- names, titles, places, quantities, dates, negations, and comparisons;
- transitions such as starting, stopping, changing, replacing, or correcting;
- incidental personal information contained inside a question or request.

Exclude pure greetings, acknowledgements, assistant echoes, and wording that
contains no retrievable information.

## Memory-unit rules

1. A memory is one coherent topic, event, state, or transition—not necessarily
   one subject/predicate/object triple.
2. Prefer a dense, standalone 1–3 sentence memory over several fragments.
3. Combine tightly related details from the same session when they would
   normally be recalled together. Do not combine unrelated topics.
4. Preserve specific modifiers such as `favorite`, `first`, `former`, `only`,
   `not`, and `three`.
5. Resolve `I`, `he`, `she`, `they`, and similar references to explicit names.
6. Resolve relative time using the supplied session/message time:
   `yesterday`, `last week`, and `two days later` must not remain relative when
   a deterministic calendar date can be obtained.
7. `world_start/world_end` describe when the remembered fact or event is true.
   They are not automatically the session observation time.
8. Stay evidence-bound. Do not invent unsupported facts.
9. Copy every `source_message_id` exactly. A synthesized memory may cite
   multiple messages.
10. Use only these `memory_kind` values:
    `profile`, `preference`, `state`, `event`, `plan`, `relationship`.

`subject`, `predicate`, and `object_text` are retrieval metadata. They must not
force the prose memory into an unnaturally small triple. Use a short, stable
predicate or `null`.

## Construction Skills

{skills_section}

## Session time

{session_time}

## Messages

{session_messages}

## Output

Return exactly one JSON object:

```json
{
  "candidates": [
    {
      "memory_kind": "event",
      "subject": "James",
      "predicate": "recreation",
      "object_text": "bowling",
      "content": "James went bowling on March 16, 2022, scored two strikes, and said that he loves bowling.",
      "world_start": "2022-03-16",
      "world_end": null,
      "source_message_ids": ["exact input message ID"],
      "entities": ["James", "bowling"],
      "keywords": ["bowling", "two strikes", "recreation"],
      "importance": 0.6,
      "confidence": 0.95
    }
  ]
}
```

Return `{"candidates":[]}` when the session contains no durable information.
