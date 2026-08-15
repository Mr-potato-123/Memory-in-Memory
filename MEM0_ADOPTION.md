# MiM on Mem0: version audit and adoption decision

Date: 2026-08-14

## Decision

- Stop extending `single_agent_mim`; keep it as read-only legacy/reference for now.
- Use the official Mem0 repository as the new implementation base.
- Develop MiM's Skill layer from Python SDK tag `v2.0.18`, commit
  `c427a453a89c5a3fee73cdb2e4c4df6a651e1692`.
- Local working branch: `mem0/mim-skill-v2.0.18`.
- Do not use `main` or a moving feature branch as an experimental baseline.

`single_agent_mim` has not been deleted or reverted. Its existing working-tree
changes are preserved, but new implementation work should happen in `mem0`.

## Three different version numbers

Mem0 discussions often mix three independent version axes:

1. Python distribution version, such as `mem0ai==0.1.116`, `1.0.5`, or `2.0.18`.
2. hosted/HTTP API version, such as Platform API v1, v2, or v3.
3. memory algorithm generation, especially the legacy update/delete pipeline
   versus the newer v3 memory algorithm.

Therefore, a paper saying “Mem0 v2” may describe an API rather than Python
package `2.x`. Every experiment must record all three axes explicitly.

## Literature and repository audit

| Work / repository | Public evidence | Version conclusion |
| --- | --- | --- |
| Mem0 paper, arXiv:2504.19413 | Submitted 2025-04-28. Official repository tags around submission were `v0.1.93` (2025-04-21) and `v0.1.94` (2025-04-26). The evaluation directory existed before submission. | The paper belongs to the `0.1.9x` implementation era; `v0.1.94` is the closest public release, but the paper does not provide enough evidence to call it the exact experimental commit. |
| A-MEM, arXiv:2502.12110 | Current reproduction repository has no `mem0ai` dependency and no Mem0 source/import. | No runnable official Mem0 version is pinned; do not infer one from publication date. |
| MemoryOS, arXiv:2506.06326 | Current repository has no `mem0ai` dependency or Mem0 implementation; its README describes integrated Mem0 comparisons as ongoing work. | No public Mem0 version can be established from the repository. |
| SimpleMem, arXiv:2601.02553 | Current requirements contain no `mem0ai`; the repository reports Mem0 comparison numbers but does not ship a pinned official Mem0 baseline. | Version is unreported/unverifiable from released code. |
| MemoryBench, arXiv:2510.17281 | Repository vendors Mem0 source; `baselines/mem0/pyproject.toml` declares `version = "0.1.116"`. | Exact bundled baseline: Python package `0.1.116` (legacy algorithm family). |
| MemTrace, arXiv:2605.28732 | `requirements.txt` pins `mem0ai==1.0.5`, and source comments link to the `v1.0.5` implementation. | Exact baseline: Python package `1.0.5` (legacy algorithm family). |
| Current official `memory-benchmarks` | OSS Docker requirements point to the moving branch `feat/v3-pipeline`; cloud runs use Platform v3. | It evaluates the new v3 algorithm, but the current OSS dependency is not reproducible without recording the resolved commit. |

## What “usually used” actually means

There is no single dominant version across the literature. The defensible
summary is chronological:

- 2025 literature mostly evaluates the legacy `0.1.x` family.
- late-2025/early-2026 work may use `1.0.x`, which still represents the legacy
  memory pipeline in the examples inspected here.
- the redesigned v3 algorithm is represented in the Python SDK `2.x` line;
  this is the relevant base for a new system, not for reproducing old Mem0
  paper scores.

The strongest recurring reproducibility problem is not which version wins; it
is that many papers do not pin a package, commit, prompts, retrieval depth, or
hosted API behavior at all.

## Experimental policy for MiM

Every MiM result built on Mem0 must record:

- Mem0 git commit and Python package version;
- algorithm generation and API version;
- extraction/update prompts and models;
- embedding model and vector-store configuration;
- search `top_k`, thresholds, reranking, filters, and token budget;
- whether the run uses OSS or Platform;
- MiM Skill commit and configuration.

For comparison work, maintain two separate tracks:

- `legacy-paper-repro`: tag `v0.1.94` only when reproducing the 2025 Mem0 paper era;
- `mim-skill-mainline`: tag `v2.0.18` as the initial frozen base for new MiM work.

The MiM Skill layer should remain separable from Mem0 core so that the base can
be upgraded and ablated without rebuilding a second memory system.
