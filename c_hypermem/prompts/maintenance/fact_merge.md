---
id: maintenance.fact_merge
version: 0.2.0
owner: c_hypermem
stage: property_key_overlap
---

# Role

You decide whether a newly extracted SPO assertion should reuse, update, or stay
separate from existing facts that share the same `property_key`.

This prompt is used after extraction and after deterministic lookup finds facts
with the same subject node and normalized predicate/property. System will
handle IDs, timestamps, indexes, and graph writes.

# Input

The caller will provide:

- `property_key`
- `new_assertion`: subject, predicate, object, polarity, time, labels, source
- `existing_facts`: current fact texts, SPO triples, status, valid time, source

# Decision Rules

- `merge`: same subject, same predicate/property, and same meaning/value.
- `update`: same fact with a more precise value, clearer wording, or newer source
  that does not contradict the old value.
- `keep_separate`: values can coexist, such as multiple hobbies, aliases,
  interests, visited places, skills, or supporting evidence.
- `needs_contradiction_check`: values may conflict and require explicit
  contradiction handling.

# Output JSON

Return exactly one JSON object:

```json
{
  "decision": "merge|update|keep_separate|needs_contradiction_check",
  "matched_existing_refs": ["existing:0"],
  "merged_assertion": {
    "subject": "Alice",
    "predicate": "prefers",
    "object": "morning interviews",
    "polarity": "positive",
    "time": "2024-01-03",
    "labels": ["fact", "preference"]
  },
  "rationale": "Same subject and preference value."
}
```

# Constraints

Do not output system IDs, storage keys, scores, confidence, edge structures, or
chain-of-thought. Use only caller-provided refs such as `existing:0`.
