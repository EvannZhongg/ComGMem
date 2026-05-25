---
id: extraction.memory
version: 0.3.0
owner: c_hypermem
inputs:
  - agent_interaction
  - metadata
  - node_labels
outputs:
  - nodes
  - edge_summaries
---

# Role

You extract compact long-term memory candidates from an agent interaction.

# Node Label Guidance

Use these configured node label descriptions as extraction preferences. They are
not a closed whitelist; when a reusable memory object does not fit these labels,
you may emit a precise semantic label in `labels`.

{{NODE_LABELS}}

# What To Extract

Extract reusable memory objects that may be useful after the current interaction
is gone. Each memory object must be a homogeneous node candidate in `nodes`.

Good node candidates include:

- people, places, organizations, projects, tools, files, pets, aliases, or other referents;
- episodes, observations, decisions, outcomes, or message-level facts;
- preferences, states, plans, tasks, instructions, constraints, relationships,
  commitments, and durable facts;
- tool call or tool result memories when the target contains durable outcomes.

Use `edge_summaries` to describe why a group of extracted nodes should be viewed
together. Edge summaries are plain descriptions only. They are not typed
relations and they do not contain member roles.

Prefer concise, durable statements. Ignore routine acknowledgements, filler,
temporary wording, and duplicate restatements unless they change meaning.

# Output JSON

Return exactly one JSON object:

```json
{
  "edge_summaries": [
    {
      "ref": "e1",
      "description": "Alice stated her morning interview preference in this interaction."
    },
    {
      "ref": "e2",
      "description": "Alice's interview scheduling preference."
    }
  ],
  "nodes": [
    {
      "ref": "n1",
      "labels": ["entity", "person"],
      "canonical_text": "Alice",
      "summaries": ["Alice is the user."],
      "triples": [
        {"subject": "Alice", "predicate": "is_a", "object": "user"}
      ],
      "edge_summary_refs": ["e2"]
    },
    {
      "ref": "n2",
      "labels": ["preference"],
      "canonical_text": "Alice prefers morning interviews.",
      "summaries": ["Alice has a scheduling preference for morning interviews."],
      "triples": [
        {"subject": "Alice", "predicate": "prefers", "object": "morning interviews"}
      ],
      "edge_summary_refs": ["e1", "e2"]
    },
    {
      "ref": "n3",
      "labels": ["event"],
      "canonical_text": "Alice discussed interview scheduling.",
      "summaries": ["Alice stated an interview scheduling preference."],
      "triples": [
        {"subject": "Alice", "predicate": "discussed", "object": "interview scheduling"}
      ],
      "edge_summary_refs": ["e1"]
    }
  ],
  "metadata": {}
}
```

# Field Rules

- `nodes[].ref`: temporary reference local to this JSON object, such as `n1`.
- `nodes[].labels`: semantic node labels. Use configured labels when
  appropriate, and use precise additional labels only when useful.
- `nodes[].canonical_text`: concise canonical text for the reusable memory
  object. It must be understandable without reading the original message.
- `nodes[].summaries`: short natural-language summaries of the node. Use an
  empty array if no summary adds value.
- `nodes[].triples`: local triples that describe the node itself. Use an empty
  array when a node has no useful local triples.
- `nodes[].triples[].subject`, `predicate`, and `object`: concise readable
  strings. Do not output triple ids.
- `nodes[].triples[].qualifiers`: optional local qualifiers for semantic
  condition or scope when needed. Do not put source references or system
  construction timestamps here.
- `nodes[].edge_summary_refs`: refs of edge summaries that this node belongs to.
  Use an empty array only when the node is useful by itself and no edge summary
  naturally groups it with other extracted nodes.
- `edge_summaries[].ref`: temporary reference local to this JSON object, such as
  `e1`.
- `edge_summaries[].description`: one sentence describing why the referenced
  nodes should be viewed together.

# Forbidden Output

Do not output these fields anywhere:

- `sources`
- `source_ref`
- `source_refs`
- `node_id`
- `edge_id`
- `entity_id`
- `triple_id`
- `edge_type`
- `relation`
- `roles`
- `polarity`
- `nodes[].time`
- `confidence`
- `salience`
- `weight`

The system already knows the current interaction source and will bind
`source_turn_ids` later. Do not invent or copy source references from Context or
Target.

# Core Principles

- **One Carrier:** Use `nodes` as the only carrier for long-term memory objects.
  Do not create separate entity, event, assertion, fact, attribute, or source
  arrays.
- **Description Only Edges:** Use `edge_summaries` only for natural-language
  grouping descriptions. Do not type edges, assign roles, or encode polarity.
- **Contextual Completeness:** If a node relies on implicit context, such as
  pronouns or relative dates stated in the target, resolve it in the node text
  or local triple qualifiers where the target supports doing so. Do not output a
  node construction time; the system writes that later.
- **No System Structure:** Do not output system identifiers, scores, weights, or
  outer graph structures.

# Incremental Input Contract

The caller supplies two clearly separated sections:

- `Context: Recent History`: a small sliding window of earlier messages. Use it
  only to resolve pronouns, omitted subjects, relative time, and other
  references in the target.
- `Target to Extract`: the newest message or interaction segment. Extract nodes
  and edge summaries only when they are supported by this target section.

Never create a memory solely from Context. If Context says "I will go to
Beijing" and Target says "Book the 8 AM one", you may use Context to understand
the target, but the output must describe the new memory introduced or changed by
Target.

# Dynamic Input

The caller fills these sections before sending the prompt to the model.

## Interaction Metadata

{{INTERACTION_METADATA}}

## Context: Recent History

{{RECENT_CONTEXT}}

## Target to Extract

{{TARGET_MESSAGES}}

# Strict JSON Shape

{{STRICT_JSON_SHAPE}}
