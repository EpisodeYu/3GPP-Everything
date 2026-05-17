---
version: 1
notes: |
  Multi-query 拆分（M4.3 启用）：把改写后的检索 query 拆 3-5 条不同角度 sub-query。
  simple 分支不走本 prompt。
---
You expand a 3GPP retrieval query into 3-5 distinct sub-queries that probe the
question from different angles (definition / procedure / signalling fields /
related entities / failure handling). Each sub-query MUST be:

- Independent (no pronouns referring back to other queries).
- English, <= 25 words.
- Grounded in the user's original intent — do NOT invent unrelated topics.

Output ONE JSON array of strings, e.g.:

```
["query A", "query B", "query C"]
```

No prose, no markdown fence, no extra fields.

Original retrieval query:
{{ rewritten_query }}
