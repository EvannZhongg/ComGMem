---
id: maintenance.hyper_edge_description_compaction
version: 0.1.0
owner: comgmem
stage: hyper_edge_description_compaction
---

# Role

Compress accumulated descriptions for one existing HyperEdge into one faithful,
compact description. The system calls you only when the configured source-count
or token limit is reached.

# Rules

- Preserve compatible information that remains useful for retrieval.
- Do not infer information that is absent from the supplied accumulated
  descriptions and member context.
- Do not decide whether edges should merge, split, conflict, retire, or change
  identity.
- Do not output system IDs, source references, storage keys, graph structure,
  scores, confidence, or chain-of-thought.
- Keep the result concise enough to reduce the accumulated description.

# Edge Context

{{EDGE_CONTEXT}}

# Accumulated Description To Compress

{{ACCUMULATED_DESCRIPTION}}

# Trigger

{{TRIGGER_CONTEXT}}

# Output JSON

{{STRICT_JSON_SHAPE}}
