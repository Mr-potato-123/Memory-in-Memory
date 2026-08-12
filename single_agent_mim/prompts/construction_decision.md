# Deprecated Construction Decision Prompt

The minimal single-agent runtime no longer calls an LLM for memory CRUD.
Construction is one evidence-bound extraction call followed by deterministic
`ADD` for new content and `SKIP` for exact duplicates. State changes and
corrections are appended as time-qualified memories so retrieval can inspect
history without destructive rewriting.

This file remains only for configuration compatibility with older runs.
