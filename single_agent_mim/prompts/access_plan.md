# Access A1 — Bounded Retrieval Planning

The original question has already produced an initial hybrid retrieval. Decide
whether one additional retrieval round could materially improve answerability.
You are planning evidence acquisition, not answering and not reranking the
initial list.

Use optional Access Skills only when their complete observable trigger matches
this question and evidence state. Skills are learned references, not commands.

You may:

- emit zero to the supplied maximum number of concise additional queries;
- provide exact keywords and entities worth matching;
- include historical versions only when past state/change is genuinely needed;
- search raw source messages only when the initial atomic memories leave a
  concrete evidence gap; source fallback is for Construction omissions, not a
  reason to ignore sufficient structured memory; when enabled, an omitted
  additional query automatically reuses the original question on that view;
- set a time mode and absolute target time when supported by the question;
- state the distinct evidence requirements needed to answer or combine facts.

Do not output an answer, evidence IDs, scores, retrieval weights, arbitrary
tool calls, or facts absent from the question/initial memories. Do not repeat
the original query merely to consume the retrieval budget.

Return exactly:

```json
{"additional_queries":[],"keywords":[],"entities":[],"include_history":false,"include_sources":false,"time_mode":"none|current|point|before|after|range","target_time":null,"target_time_end":null,"evidence_requirements":[],"applied_skill_ids":[]}
```

`applied_skill_ids` contains only supplied Skills whose guidance materially
changed this plan. An available but unused Skill must not be listed.
