---
id: extraction.memory
version: 0.1.0
owner: c_hypermem
inputs:
  - agent_interaction
  - metadata
  - node_labels
outputs:
  - entities
  - events
  - assertions
  - sources
---

# Task

Extract concise memory candidates from the interaction.

# Output

Return compact JSON with entities, events, assertions, and source snippets. Use
natural-language fields such as name, type, aliases, summary, time, subject,
predicate, object, role, text, and source_ref.

Use assertions as the single carrier for facts, attributes, and triples. Do not
duplicate the same memory object across separate facts, attributes, and triples.

Do not output system identifiers, storage keys, scores, importance values, or
outer graph structure.
