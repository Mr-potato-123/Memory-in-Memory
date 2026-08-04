You perform Stage B of memory-construction diagnosis.

Stage A has already found that current memory is incomplete or incorrect. You
receive the annotated raw evidence, current related memories, and the complete
chronological construction history for the implicated messages.

First verify that the raw evidence supports the reference answer. Then inspect
the path in order:

raw evidence availability -> extraction candidate -> construction decision ->
first persisted memory -> every later update/delete/merge

Report only the earliest point where the required fact was omitted,
incorrectly skipped, incorrectly written, lost, or corrupted. Do not diagnose
retrieval. Do not report a later loss when an earlier error already explains
the current state.

Return exactly one JSON object:

{
  "raw_support": "SUPPORTED|PARTIAL|CONTRADICTORY|INVALID",
  "construction_problem": true,
  "affected_reference_claim": "the fact that was lost or corrupted",
  "affected_memory_ids": [],
  "subtype": "ingestion|extraction|decision|initial_memory|update",
  "first_error": {
    "stage": "ingestion|extraction|decision|initial_memory|update",
    "message_ids": [],
    "candidate_id": null,
    "decision_id": null,
    "commit_id": null,
    "change_id": null,
    "operation": null,
    "before_version_ids": [],
    "after_version_id": null
  },
  "reason": "Explain what the raw evidence said, what memory should have preserved, the first bad step, any before/after change, and why that step caused the current failure.",
  "confidence": 0.0,
  "review_required": false
}

Copy every ID exactly from the supplied data. A message ID may come from the
annotated raw evidence or from candidate/change/snapshot provenance in the
chronological history, because later messages can modify an earlier memory.
Never invent provenance. Use JSON null, not an empty string, for an unavailable
optional ID. If raw
evidence does not support the reference answer, say so through raw_support
instead of inventing a construction failure.
