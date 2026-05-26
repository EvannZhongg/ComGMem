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

# Task

Extract compact, long-term, reusable memory objects from the `Target to Extract`.

**You must act as a third-party observer. Extract durable facts not only about the User, but also about the Assistant (e.g., decisions made, tasks committed to, or important tool results summarized by the Assistant).**

# Input Processing Rule
You will receive `Context: Recent History` and a `Target to Extract`. 
* Use the Context **only** to resolve pronouns, omitted subjects, or relative time references in the Target. 
* Extract memories **only** if they are explicitly supported by the new information in the Target. Do not extract memories solely from the Context.
* Do not treat role or section labels such as `User`, `Assistant`, `Context`, or `Target` as extractable facts.

# Node Label Guidance

Use these configured node label descriptions as extraction preferences. They are
not a closed whitelist; when a reusable memory object does not fit these labels,
you may emit a precise semantic label in `labels`.

{{NODE_LABELS}}

# Output JSON Schema Rules
Return exactly one JSON object adhering strictly to `{{STRICT_JSON_SHAPE}}`. Never output system IDs, timestamps, outer graph structures, or confidence scores.

* `nodes`: The only carrier for memory objects (entities, events, preferences, tasks, facts). 
* `nodes[].ref`: A temporary local reference (e.g., "n1").
* `nodes[].canonical_text`: A concise standalone statement, understandable without the original message.
* `nodes[].triples`: Describe the node's internal attributes (subject, predicate, object). Leave empty if not applicable.
* Each `nodes[].triples[]` object may contain only `subject`, `predicate`, `object`, and optional `qualifiers`; never output `subject_label`, `object_label`, or any other extra triple field.
* Every `triples[] ` item must include non-empty subject, predicate, and object; qualifiers may only be attached to a complete triple and must never appear as a standalone triple item.
* `edge_summaries`: Describe the coherent interaction, event, or situation that connects the linked nodes, such as a user request and assistant response. Keep descriptions specific but concise; do not type edges or assign roles.
* `nodes[].edge_summary_refs`: Link the node to the relevant `edge_summaries[].ref`.
* Every node should reference at least one relevant `edge_summaries[].ref`; leave `edge_summary_refs` empty only when the node is genuinely standalone.

# Output JSON

Return exactly one JSON object:

```json
{
  "edge_summaries": [
    {
      "ref": "e1",
      "description": "User preferred morning interviews, and Assistant committed to helping schedule one."
    },
    {
      "ref": "e2",
      "description": "Assistant planned a calendar reminder for the morning interview."
    }
  ],
  "nodes": [
    {
      "ref": "n1",
      "labels": ["preference"],
      "canonical_text": "User prefers morning interviews.",
      "summaries": ["User has a scheduling preference for morning interviews."],
      "triples": [
        {"subject": "User", "predicate": "prefers", "object": "morning interviews"}
      ],
      "edge_summary_refs": ["e1"]
    },
    {
      "ref": "n2",
      "labels": ["task"],
      "canonical_text": "Assistant will set a calendar reminder for the morning interview.",
      "summaries": ["Assistant committed to setting a calendar reminder for the morning interview."],
      "triples": [
        {"subject": "Assistant", "predicate": "will_set_reminder_for", "object": "morning interview"}
      ],
      "edge_summary_refs": ["e1", "e2"]
    },
    {
      "ref": "n3",
      "labels": ["event"],
      "canonical_text": "Morning interview.",
      "summaries": ["The morning interview is the appointment that the calendar reminder concerns."],
      "triples": [
        {"subject": "morning interview", "predicate": "has_reminder", "object": "calendar reminder"},
        {"subject": "morning interview", "predicate": "scheduled_part_of_day", "object": "morning"}
      ],
      "edge_summary_refs": ["e2"]
    }
  ],
  "metadata": {}
}
```

## Interaction Metadata

{{INTERACTION_METADATA}}

## Context: Recent History

{{RECENT_CONTEXT}}

## Target to Extract

{{TARGET_MESSAGES}}

# Strict JSON Shape

{{STRICT_JSON_SHAPE}}
