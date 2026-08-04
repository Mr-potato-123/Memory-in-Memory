# MRAgent Code Audit

Audit date: 2026-07-23

Official repository: <https://github.com/Ji-shuo/MRAgent>

Pinned repository state:

- Commit: `7441506db984b7c4da32e8dbeb2527f2e351270a`
- Commit date: 2026-06-08
- Repository page at audit time: 222 stars, 21 forks
- Repository history at audit time: one commit
- License: no `LICENSE` file and no license metadata were present

## Access-Agent evidence

The released code contains a genuine multi-round memory-access loop rather
than only an embedding search followed by answer generation.

- `agent/tools.py` exposes seven graph-memory tools:
  `edges_by_tag`, `query_conversation_time`, `query_event_keywords`,
  `query_event_context`, `query_personal_information`,
  `query_personal_aspect`, and `query_topic_events`.
- `llm/controller.py` calls the LLM with `tool_choice="auto"`, executes the
  selected tools, returns their observations to the LLM, and repeats.
- The default safety budget in `common/config.py` is eight assistant rounds
  and fifty total tool calls.
- The prompt allows the agent to answer early when evidence is sufficient or
  continue navigating when it is incomplete.

This satisfies the strict Access Agent criterion because the LLM controls:

1. which memory tool/path to use;
2. tool arguments such as cue, tag, event, person, aspect, or topic;
3. whether to expand or redirect the search after seeing intermediate
   evidence;
4. whether to stop and answer.

## MiM insertion boundary

To preserve MRAgent as the base system, keep its Cue--Tag--Content graph,
construction pipeline, seven tool implementations, and retrieval budget.
Insert MiM only into the access policy:

- augment the tool descriptions/system policy with retrieved memory skills;
- let MiM influence tool selection, argument generation, branch pruning, and
  stopping;
- log failure trajectories and update skills outside the base graph.

Replacing the graph schema or tool implementations would no longer be a clean
`MRAgent + MiM` comparison.

## Reproduction risks

- The repository is a research snapshot with one commit and no tagged release.
- The absence of an explicit license makes redistribution or publishing a
  modified fork legally unclear. Keep the adapter in the MiM repository and
  avoid redistributing modified MRAgent source unless permission is obtained.
- The paper evaluates Gemini-2.5-Flash and Claude-Sonnet-4.5 as backbones;
  `gpt-4o-mini` is used as judge, not as the reported access agent. A pilot
  study is required before committing to a `gpt-4o-mini`-only main table.
