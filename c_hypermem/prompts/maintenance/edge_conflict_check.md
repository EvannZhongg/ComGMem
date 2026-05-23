---
id: maintenance.edge_conflict_check
version: 0.1.0
owner: c_hypermem
---

# Task

Check whether related relationship statements can all be true at the same time, or whether they need review because of conflict.

Return compact JSON with a conflict state, affected statements, and a short rationale. Do not output system identifiers, scores, or storage keys.
