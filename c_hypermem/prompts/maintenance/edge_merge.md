---
id: maintenance.edge_merge
version: 0.2.0
owner: c_hypermem
stage: edge_building
---

# Role

You decide whether a candidate HyperEdge should reuse an existing edge or be
created as a new edge.

This prompt is used only after deterministic retrieval finds highly overlapping
topology: similar relation, compatible roles, overlapping members, source scope,
and time hints. System will assign or reuse edge IDs.

# Input

The caller will provide:

- `candidate_edge`: edge_type, relation, polarity, description, members, roles,
  time, source scope
- `existing_edges`: retrieved edge candidates with caller-provided refs

# Decision Rules

- `reuse_edge`: same relationship instance; reuse the existing edge ID.
- `append_members`: same ongoing appendable relationship; existing edge can take
  additional members.
- `new_version`: same relationship but membership changed in a version-sensitive
  way.
- `new_edge`: related but distinct relationship, source scope, time scope, role
  structure, or polarity.
- `needs_review`: ambiguous or possibly conflicting.

Member overlap alone is not enough to merge. Relation, polarity, roles, source
scope, and time must be compatible.

# Output JSON

Return exactly one JSON object:

```json
{
  "decision": "reuse_edge|append_members|new_version|new_edge|needs_review",
  "matched_existing_ref": "edge:0",
  "member_action": "unchanged|append|version|separate",
  "rationale": "Same relation and role structure with compatible source scope."
}
```

# Constraints

Do not output real system IDs, storage keys, scores, confidence, or cluster
decisions. Use only caller-provided refs such as `edge:0`.
