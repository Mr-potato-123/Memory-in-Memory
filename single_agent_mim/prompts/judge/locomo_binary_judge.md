# Strict Answer Judge — Binary CORRECT / WRONG

Your task is to label an answer to a question as "CORRECT" or "WRONG".

You will be given:
1. a question (posed by one user to another, about something one user
   should know about the other from their prior conversations),
2. a "gold" (ground-truth) reference answer,
3. a generated answer from the system under test,
4. temporal context (conversation timeline + evidence timestamps).

Judge semantic correctness, not word overlap or topic similarity.

Label CORRECT only when the generated answer entails every reference claim
required to answer the question:

- Paraphrases, canonical aliases, and reordered facts are allowed.
- The relevant entities, relations, polarity, quantities, and time must agree.
- A relative time is allowed only when the supplied fictional timeline makes
  it unambiguously equivalent to the reference time.
- For a list, conjunction, comparison, count, or multi-hop answer, every
  required component must be present. A partially correct answer is WRONG.
- Concise answers are allowed. Longer answers are CORRECT only when all extra
  material claims are consistent with the reference and do not change the
  answer.
- Merely mentioning the same person, event, or topic is not enough.

Label WRONG when any required claim is missing, a material claim is false or
unsupported by the reference, the answer concerns a different entity/event,
or an answerable question is rejected as unavailable.

Category 5 (adversarial) notes:
- The reference is intentionally empty and the question's premise is false.
- Label CORRECT when the answer correctly states that the information is
  unavailable or rejects the false premise.
- Label WRONG when the answer fabricates content or accepts the false
  premise.

Use the reference as the factual grading target and the supplied fictional
timestamps only to resolve temporal expressions. Do not assume additional
conversation facts, and never use the current real-world date.

Return exactly one JSON object:

{
  "judgments": [
    {
      "qa_id": "exact input qa_id",
      "label": "CORRECT" or "WRONG",
      "reason": "brief reason naming the decisive match or error"
    }
  ]
}

Return every input qa_id exactly once and in input order.
