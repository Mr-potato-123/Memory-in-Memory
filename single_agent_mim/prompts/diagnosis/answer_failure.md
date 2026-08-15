# Mem0 Answer Diagnosis

Diagnose a wrong answer produced after one fixed Mem0 search. Mem0 has no A1,
A2, planning stage, query rewrite, second retrieval, or adjustable search depth
in this experiment. Use only the memories in `runtime_search_chain`.

First decide whether the returned memories were already sufficient to produce
the reference answer. Sufficiency means that at least one valid combination of
returned memories supports every essential claim. Do not require every useful,
annotated, or redundant supporting memory to have been returned. Additional
unreturned evidence cannot turn an already sufficient context into a retrieval
failure.

If context is sufficient, identify why the prediction is wrong. Distinguish
memory-answering procedures such as entity attribution, temporal resolution,
list coverage, contradiction handling, evidence selection, unsupported
inference, and over-inclusion from generic wording/style mistakes. A failure is
Skill-learnable only when:

- its trigger is observable from the question and returned memories;
- the corrective operation can change only evidence interpretation or answer
  composition;
- it does not require another search, a larger top-k, hidden dialogue, the
  reference answer, or case-specific facts.

For exact/single-item questions, returning the correct item plus unsupported or
less-specific alternatives is an `OVER_INCLUSION` answer failure, not a
retrieval failure.

Do not answer again, use outside knowledge, inspect unreturned memories, or
propose a Skill. Copy memory IDs exactly.

Return exactly:

```json
{"essential_reference_claims":[{"claim":"atomic claim","supporting_retrieved_version_ids":[],"coverage":"FULL|PARTIAL|MISSING|INCORRECT"}],"retrieved_context_supports_abstention":false,"unresolved_material_contradiction":false,"failure_mode":"EVIDENCE_SELECTION|OVER_INCLUSION|ENTITY_ATTRIBUTION|TEMPORAL_REASONING|LIST_COVERAGE|CONTRADICTION_HANDLING|UNSUPPORTED_INFERENCE|GENERIC_INSTRUCTION_FOLLOWING|OTHER","skill_learnable":false,"observable_trigger":"condition visible in question and returned memories, or empty","corrective_operation":"answer-side operation, or empty","reason":"whether the returned context was sufficient and why the prediction failed","confidence":0.0,"review_required":false}
```
