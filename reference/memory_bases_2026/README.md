# Memory Base References

Created: 2026-07-22  
Updated: 2026-07-23

This folder indexes source materials for the memory-base survey report under
the `AGENT` directory. Some PDFs live in the parent `reference` folder because
they were already present before this survey pass.

## Selected Bases After Final Impact/Insertability Audit

- `2507.07957_mirix.pdf`
  - MIRIX: Multi-Agent Memory System for LLM-Based Agents.
  - Selected as the established-but-not-old Access-Agent LTM architecture: six memory types, Meta/Memory/Chat Agents, Active Retrieval, and agent-selected targeted search.
- `2507.07957_mirix.txt`
  - Text extraction used to verify the coarse-to-targeted conversational retrieval workflow and retrieval-method selection.
- `mirix_github_snapshot.html`
  - MIRIX GitHub repository snapshot.
- `mirix_github_api.json`
  - GitHub API metadata captured on 2026-07-23.
- `2604.04853_memmachine.pdf`
  - MemMachine: A Ground-Truth-Preserving Memory System for Personalized AI Agents.
  - Selected as the 2026 frontier base because it has an explicit Retrieval Agent.
- `2604.04853_memmachine.txt`
  - Text extraction used for retrieval/access audit.
- `memmachine_github_snapshot.html`
  - MemMachine GitHub repository snapshot.
- `2606.06036_mragent_active_memory_reconstruction.pdf`
  - MRAgent: Memory is Reconstructed, Not Retrieved: Graph Memory for LLM Agents.
  - Selected as the ICML 2026 active-access base: an LLM iteratively selects graph-memory tools and traversal directions, prunes branches, and stops when evidence is sufficient.
- `2606.06036_mragent_active_memory_reconstruction.txt`
  - Text extraction used to verify Cue--Tag--Content construction, active reconstruction, and LoCoMo/LongMemEval experiments.
- `mragent_github_snapshot.html`
  - Official MRAgent GitHub repository snapshot.
- `mragent_readme.md`
  - Official repository README, including the released LoCoMo pipeline and seven memory tools.
- `mragent_code_audit.md`
  - Local mechanism/insertability audit pinned to commit `7441506db984b7c4da32e8dbeb2527f2e351270a`.

## Auxiliary Non-Agentic LTM Material

- `2510.18866_lightmem_iclr2026.pdf`
  - LightMem: Lightweight and Efficient Memory-Augmented Generation.
  - Auxiliary non-agentic LTM material: ICLR 2026, modular and reproducible, but its retrieval path does not satisfy the Access-Agent hard constraint.
- `2510.18866_lightmem_iclr2026.txt`
  - Text extraction used for impact/insertability audit.
- `lightmem_github_snapshot.html`
  - LightMem GitHub repository snapshot.

## Demoted From Main Bases

- `2026.acl-demo.27_hindsight.pdf`
  - Hindsight: Structured Agent Memory that Retains, Recalls, and Reflects.
  - Demoted because TEMPR recall is a fixed four-channel retrieval pipeline; CARA reflect is an answer/reflection agent, not an Access Agent mediating the retrieval plan.
- `2026.acl-demo.27_hindsight.txt`
  - Text extraction used to audit the fixed recall pipeline.
- `2512.12818_hindsight_full.pdf`
  - Full architecture paper, Hindsight is 20/20.
- `2512.12818_hindsight_full.txt`
  - Text extraction of the full architecture paper.
- `hindsight_github_snapshot.html`
  - Hindsight GitHub repository snapshot.
- `hindsight_github_api.json`
  - GitHub API metadata captured on 2026-07-23.
- `2025.emnlp-main.1318_memoryos.pdf`
  - Memory OS of AI Agent, official EMNLP 2025 Main proceedings version (Oral).
  - Demoted because retrieval is a fixed segment/page and semantic top-k pipeline, not an Access Agent.
- `2025.emnlp-main.1318_memoryos.txt`
  - Text extraction used for the construction/update/retrieval audit.
- `2506.06326_memoryos.pdf`
  - arXiv version of Memory OS of AI Agent.
- `2506.06326_memoryos.txt`
  - Text extraction of the arXiv version.
- `memoryos_github_snapshot.html`
  - MemoryOS GitHub repository snapshot.
- `memoryos_github_api.json`
  - GitHub API metadata captured on 2026-07-23.
- `2310.08560_memgpt_letta.pdf`
  - MemGPT: Towards LLMs as Operating Systems.
  - Demoted because a 2023 method is too old for the three main bases of a paper planned for November 2026; retained as the historical source of function-call-based agentic memory access.
- `2310.08560_memgpt_letta.txt`
  - Text extraction used for retrieval/access audit.
- `letta_official_docs.html`
  - Letta official docs front page.
- `letta_memory_docs.html`
  - Letta stateful agents / memory docs snapshot.
- `2502.12110_a_mem.pdf`
  - A-Mem: Agentic Memory for LLM Agents.
  - Demoted because its access side is query embedding + cosine similarity + top-k, even though its construction/link/evolution side is agentic.
- `2502.12110_a_mem.txt`
  - Text extraction used for retrieval/access audit.
- `a_mem_github_snapshot.html`
  - A-Mem GitHub repository snapshot.
- `../2601.02553v3.pdf`
  - SimpleMem: Efficient Lifelong Memory for LLM Agents.
  - Strong 2026 backup/upper baseline. It has intent-aware retrieval planning, but its prompt-heavy pipeline may leave limited and hard-to-attribute improvement space for MiM.
- `../2601.02553v3.txt`
  - Text extraction used for retrieval/access audit.
- `../../baseline/SimpleMem/README.md`
  - Local SimpleMem repository README.
- `../../baseline/SimpleMem/MCP/README.md`
  - SimpleMem MCP server docs.
- `../../baseline/SimpleMem/SKILL/README.md`
  - SimpleMem skill docs.
- `2603.19935_memori.pdf`
  - Memori: A Persistent Memory Layer for Efficient, Context-Aware LLM Agents.
  - Demoted because its evaluated access side is FAISS similarity search plus BM25 hybrid search over semantic triples.
- `2603.19935_memori.txt`
  - Text extraction used for retrieval/access audit.
- `memori_github_snapshot.html`
  - Memori GitHub repository snapshot.
- `memos_github_snapshot.html`
  - MemOS GitHub repository snapshot. MemOS is influential, but treated as engineering related work because it is a broad OS-level system and not ideal for the first MiM adapter pass.

## Strong Related / Competitor Material

- `2603.03296_plugmem.pdf`
  - PlugMem: A Task-Agnostic Plugin Memory Module for LLM Agents.
  - Its controller satisfies the Access-Agent criterion, but PlugMem is itself a memory plugin. It is retained as a direct plugin-level competitor rather than a `base + MiM` main experiment.
- `2603.03296_plugmem.txt`
  - Text extraction used for controller/access audit.
- `plugmem_github_snapshot.html`
  - PlugMem GitHub repository snapshot.
- `2026.acl-long.749_apex_mem.pdf`
  - APEX-MEM: Agentic Semi-Structured Memory with Temporal Reasoning for Long-Term Conversational AI.
  - ACL 2026 Long Paper and the strongest replacement candidate for MRAgent if official code becomes available; its multi-tool QnAAgent satisfies the Access-Agent requirement.
- `2026.acl-long.749_apex_mem.txt`
  - Text extraction used to verify the ReAct tool loop, graph tools, and LoCoMo/LongMemEval setup.
- `2026.acl-long.981_agemem.pdf`
  - AgeMem: Agentic Memory: Learning Unified Long-Term and Short-Term Memory Management for Large Language Model Agents.
  - ACL 2026 Long Paper with agent-controlled LTM/STM tools; not selected because it requires RL-policy adaptation and has no LoCoMo/LongMemEval experiment.
- `2026.acl-long.981_agemem.txt`
  - Text extraction used to audit its tool interface and training/evaluation scope.
- `agemem_github_snapshot.html`
  - Official AgeMem GitHub repository snapshot.
- `2602.15313_mnemis.pdf`
  - Mnemis: Dual-Route Retrieval on Hierarchical Graphs for Long-Term LLM Memory.
  - ACL 2026 Main / Microsoft work with deliberate System-2 global selection. Not selected as a main base because current community visibility is modest and the official repository exposes only part of the full pipeline.
- `2602.15313_mnemis.txt`
  - Text extraction used for construction/retrieval and implementation-completeness audit.
- `mnemis_github_snapshot.html`
  - Mnemis GitHub repository snapshot.
- `mnemis_github_api.json`
  - GitHub API metadata captured on 2026-07-23.
- `2606.24775_agent_native_memory_systems_memorydata.pdf`
  - Are We Ready For An Agent-Native Memory System? A Systematic Evaluation on Data Management.
  - Used as third-party taxonomy/evaluation evidence across 12 representative memory systems.
- `2606.24775_agent_native_memory_systems_memorydata.txt`
  - Text extraction used for taxonomy and candidate audit.
- `memorydata_github_snapshot.html`
  - MemoryData GitHub repository snapshot.
- `2604.07798_lightweight_llm_agent_memory_slm.pdf`
  - Lightweight LLM Agent Memory via SLM-based modularization.
  - Related lightweight memory work, not selected over ICLR LightMem.
- `2604.07798_lightweight_llm_agent_memory_slm.txt`
  - Text extraction used for related-work audit.
- `2605.15759_dimmem.pdf`
  - DimMem: lightweight dimensional memory framework.
  - 2026 related work; not selected because community maturity is currently low.
- `2605.15759_dimmem.txt`
  - Text extraction used for related-work audit.
- `2605.28773_fluxmem.pdf`
  - FluxMem: connectivity-evolving memory framework.
  - 2026 related work; not selected because code availability/maturity is uncertain.
- `2605.28773_fluxmem.txt`
  - Text extraction used for related-work audit.
- `2603.07670_memory_survey_autonomous_llm_agents.pdf`
  - Memory for Autonomous LLM Agents: Mechanisms, Evaluation, and Emerging Frontiers.
  - Used to justify the write--manage--read decomposition of agent memory.
- `2603.07670_memory_survey_autonomous_llm_agents.txt`
  - Text extraction used for the one-sided-plugin discussion.
- `2607.13591_memcon_controlled_process.pdf`
  - Memory as a Controlled Process: Learned Adaptive Memory Management for LLM Agents.
  - 2026 work arguing for backend-agnostic adaptive memory control.
- `2607.13591_memcon_controlled_process.txt`
  - Text extraction used for the controller/plugin discussion. PDF text extraction had syntax warnings, so use cautiously.
- `2603.00026_actmem.pdf`
  - ActMem: Bridging the Gap Between Memory Retrieval and Reasoning in LLM Agents.
  - 2026 work connecting retrieval with active causal reasoning.
- `2603.00026_actmem.txt`
  - Text extraction used for related-work discussion.
- `2601.14287_chain_of_memory.pdf`
  - Chain-of-Memory: Lightweight Memory Construction with Dynamic Evolution for LLM Agents.
  - 2026 work supporting a lightweight-construction / sophisticated-utilization view.
- `2601.14287_chain_of_memory.txt`
  - Text extraction used for related-work discussion.
- `../2602.02474v2.pdf`
  - MemSkill: Learning and Evolving Memory Skills for Self-Evolving Agents.
  - Very close to MiM; better treated as closest related work or direct competitor than as a base.
- `../2602.02474v2.txt`
  - Text extraction used for retrieval/access audit.
- `../2605.13941v1.pdf`
  - EvolveMem: Self-Evolving Memory Architecture via AutoResearch for LLM Agents.
  - Strong 2026 related work on evolving retrieval infrastructure.
- `../2605.13941v1.txt`
  - Text extraction used for retrieval/access audit.

## Engineering / Excluded Material

- `2501.13956_zep_graphiti.pdf`
  - Backup source for Zep/Graphiti.
  - Engineering-famous temporal KG memory, but not the best fit for the access-agent argument.
- `langmem_official_docs.html`
  - LangMem official docs snapshot.
  - Useful implementation reference, but not selected as an academic base.
- `langmem_memory_tools_api.html`
  - LangMem memory tools API reference snapshot.
- `langmem_github_snapshot.html`
  - LangMem GitHub repository snapshot.
- `2504.19413_mem0.pdf`
  - Excluded because the user explicitly removed Mem0 from scope: the local Mem0 version has no real access agent.
