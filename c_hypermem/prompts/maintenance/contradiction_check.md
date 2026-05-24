---
id: maintenance.contradiction_check
version: 0.2.0
owner: c_hypermem
stage: property_key_overlap
---

# Role

You decide whether a new SPO assertion contradicts existing facts with the same
`property_key`.

This prompt handles the most basic SPO-level conflict case. System will
retire or invalidate old fact nodes, create correction edges, and update valid
time when needed.

# Input

The caller will provide:

- `property_key`
- `new_assertion`
- `existing_facts`
- optional temporal metadata

# Decision Rules

- `same_value`: the new assertion says the same thing.
- `compatible`: both values can be true at the same time.
- `contradiction`: both values cannot be true for the same subject, predicate,
  and time scope.
- `uncertain`: not enough context to decide.

Use time carefully. A newer state may supersede an older state without making the
older statement false for its original time.

# Output JSON

Return exactly one JSON object:

```json
{
  "conflict_state": "same_value|compatible|contradiction|uncertain",
  "affected_existing_refs": ["existing:0"],
  "recommended_old_status": "active|retired|invalidated|uncertain",
  "valid_time_update": {
    "old_end": "2024-01-03",
    "new_start": "2024-01-04"
  },
  "rationale": "The new value replaces the old value for the same property."
}
```

# Constraints

Do not output node IDs, edge IDs, triple IDs, storage keys, scores, confidence,
or graph structure. Use only caller-provided refs such as `existing:0`.
