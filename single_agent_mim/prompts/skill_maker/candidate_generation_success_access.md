# Mem0 Positive Experience Recorder

A correct no-Skill answer is preservation evidence, not evidence that a new
Skill is necessary. Mem0 performs one fixed search followed by one answer. It
has no A1, A2, query rewrite, supplemental retrieval, or Skill-controlled
top-k/depth.

Do not generate a Skill from this package. It may later be used as a matched
negative/control example for a failure-derived candidate.

Return exactly:

```json
{"decision":"NO_CHANGE_ALREADY_COVERED","reason":"The fixed Mem0 plus default answer policy already succeeded; retain this case as negative-control evidence."}
```
