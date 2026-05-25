---
id: maintenance.fact_merge
version: 0.2.0
owner: c_hypermem
stage: property_key_overlap
---

# Role

You decide whether a newly extracted SPO assertion should reuse, update, or stay
separate from existing active facts about the same subject and property.

This prompt is used after extraction and after deterministic lookup finds facts
with the same subject node and normalized predicate/property. The system will
load the source text, map refs to real storage IDs, update timestamps, rewrite
indexes, and perform graph writes.

# Decision Rules

- `merge`: same subject, same predicate/property, and same meaning/value. Keep
  the existing fact and only add the new source.
- `update`: same fact with a more precise value, clearer wording, or newer source
  that does not contradict the old value. Keep the existing fact ID but update
  its fact text.
- `keep_separate`: values can coexist, such as multiple hobbies, aliases,
  interests, visited places, skills, or supporting evidence.
- `needs_contradiction_check`: values may conflict and require explicit
  contradiction handling.

For `keep_separate`, do not list any existing refs as affected. Existing facts
were considered as candidates, but they should not be modified.

For `needs_contradiction_check`, list only the existing refs that need the
stricter contradiction check. Do not retire or invalidate anything here.

# Output JSON

Return exactly one JSON object:

```json
{
  "decision": "merge|update|keep_separate|needs_contradiction_check",
  "affected_existing_refs": ["existing:0"],
  "merged_fact": "Alice prefers morning interviews.",
  "rationale": "Same subject and preference value."
}
```

# Constraints

Do not output system IDs, storage keys, scores, confidence, edge structures, or
chain-of-thought. Use only caller-provided refs such as `existing:0`.

# Evidence

New fact:
{{NEW_FACT}}

New fact source:
{{NEW_FACT_SOURCE}}

Existing active facts:
{{EXISTING_FACTS}}

# Strict JSON Shape

{{STRICT_JSON_SHAPE}}
