---
id: maintenance.edge_merge
version: 0.2.0
owner: c_hypermem
stage: edge_building
---

# Role

You decide whether a candidate HyperEdge should reuse an existing edge or be
created as a new edge.

This prompt is not connected to the current write path. It is retained only as a
placeholder for a future description-only edge maintenance flow.

# Input

The caller will provide:

- `candidate_edge`: description, members, source scope, and time hints
- `existing_edges`: retrieved edge candidates with caller-provided refs

# Decision Rules

- `reuse_edge`: same described memory grouping; reuse the existing edge ID.
- `append_members`: same ongoing appendable grouping; existing edge can take
  additional members.
- `new_version`: same described grouping but membership changed in a
  version-sensitive way.
- `new_edge`: related but distinct description, source scope, or time scope.
- `needs_review`: ambiguous or possibly conflicting.

Member overlap alone is not enough to merge. Description, source scope, and time
must be semantically compatible.

# Output JSON

Return exactly one JSON object:

```json
{
  "decision": "reuse_edge|append_members|new_version|new_edge|needs_review",
  "matched_existing_ref": "edge:0",
  "member_action": "unchanged|append|version|separate",
  "rationale": "Same described grouping with compatible source scope."
}
```

# Constraints

Do not output real system IDs, storage keys, scores, confidence, or cluster
decisions. Use only caller-provided refs such as `edge:0`.
