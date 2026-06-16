---
id: maintenance.node_summary_compaction
version: 0.1.0
owner: comgmem
stage: node_summary_compaction
---

# Role

Compress accumulated summaries for one existing MemoryNode into one faithful,
compact summary. The system has already decided that compression is required
because the configured source-count or token limit was reached.

# Rules

- Preserve all compatible information that remains useful for retrieval.
- Do not infer facts that are absent from the supplied accumulated summary.
- Do not decide whether nodes should merge, split, conflict, retire, or change
  identity.
- Do not output system IDs, source references, storage keys, graph structure,
  scores, confidence, or chain-of-thought.
- Keep the result concise enough to reduce the accumulated summary.

# Node Context

{{NODE_CONTEXT}}

# Accumulated Summary To Compress

{{ACCUMULATED_SUMMARY}}

# Trigger

{{TRIGGER_CONTEXT}}

# Output JSON

{{STRICT_JSON_SHAPE}}
