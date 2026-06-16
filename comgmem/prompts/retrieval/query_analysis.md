---
id: retrieval.query_analysis
version: 0.1.0
owner: comgmem
---

# Task

Analyze the query for entities, time constraints, preferences, tasks, and expected answer type.

# Query

{{QUERY}}

# Requirements

Return JSON only. The output is used as retrieval metadata, not as a final answer.

Expected shape:

```json
{
  "normalized_query": "canonical plain-language query",
  "bm25_query": "space-separated lexical terms if useful",
  "entities": [
    {"type": "person|place|organization|project|object|concept|unknown", "text": "entity mention"}
  ],
  "attributes": {
    "intent": "short retrieval intent label",
    "time_constraints": [],
    "expected_labels": []
  }
}
```

Do not output memory IDs, scores, or retrieved memory content.
