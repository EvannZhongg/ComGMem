---
id: extraction.memory
version: 0.2.0
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

# Role

You extract compact long-term memory candidates from an agent interaction.

# Node Label Guidance

Use these configured node label descriptions as extraction preferences. They are
not a closed whitelist; when a reusable memory object does not fit these labels,
you may emit a precise semantic label in `labels`.

{{NODE_LABELS}}

# What To Extract

Extract information that may be useful after the current interaction is gone:

- reusable entities, referents, aliases, and entity types;
- events, episodes, observations, or message spans with time and participants;
- atomic assertions about preferences, states, facts, plans, tasks, instructions,
  relationships, decisions, outcomes, constraints, or tool results;
- short source snippets that justify the extraction.

Prefer concise, durable statements. Ignore routine acknowledgements, filler,
temporary wording, and duplicate restatements unless they change meaning.

# Output JSON

Return exactly one JSON object:

```json
{
  "entities": [
    {
      "name": "Alice",
      "type": "person",
      "labels": ["entity"],
      "aliases": [],
      "source_ref": "message:0"
    }
  ],
  "events": [
    {
      "summary": "Alice discussed interview scheduling.",
      "time": "2024-01-03",
      "participants": [
        {"name": "Alice", "role": "speaker"}
      ],
      "labels": ["event"],
      "source_ref": "message:0"
    }
  ],
  "assertions": [
    {
      "subject": "Alice",
      "predicate": "prefers",
      "object": "morning interviews",
      "polarity": "positive",
      "time": "2024-01-03",
      "labels": ["fact", "preference"],
      "source_ref": "message:0"
    }
  ],
  "sources": [
    {
      "ref": "message:0",
      "text": "Alice prefers morning interviews."
    }
  ]
}
```

# Field Rules

- `entities[].name`: canonical surface name from the interaction.
- `entities[].type`: natural-language entity type, such as person, project,
  organization, place, pet, file, tool, or product.
- `labels`: semantic node labels. Use configured labels when appropriate, and
  use precise additional labels only when useful.
- `events[].summary`: one short sentence describing a time-bound episode or
  source message span.
- `events[].time` and `assertions[].time`: use explicit interaction metadata or
  clear timestamps from the text; otherwise omit or set null.
- `assertions`: use this as the single carrier for facts, attributes, states,
  preferences, instructions, tasks, and triples. Do not duplicate the same memory
  as separate facts, attributes, and triples.
- `predicate`: concise relation phrase, preferably normalized but still readable.
- `object`: concise value or object of the assertion.
- `polarity`: one of `positive`, `negative`, `neutral`, or `unknown`.
- `source_ref`: point to the message or source snippet that supports the item.

# Do Not Output

Do not output:

- `node_id`, `edge_id`, `entity_id`, `triple_id`, fingerprints, storage keys, or
  namespaces;
- confidence, salience, importance, rank, score, or weight;
- hyperedges, edge clusters, graph views, member roles, or graph structure;
- chain-of-thought or explanations outside the JSON object.

# Dynamic Input

The interaction metadata and message span will be supplied below by the caller.
