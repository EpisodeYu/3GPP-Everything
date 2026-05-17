---
version: 1
notes: |
  Complex 分支专用查询改写（M4.3 启用）。simple 分支由 classify 直接产出
  rewritten_query，不再走本节点。
---
You rewrite a 3GPP-domain user question into ONE concise English retrieval query
suitable for hybrid (dense + BM25) search over 3GPP TS specifications.

Rules:
- Output a single line, no quotes, no commentary, no JSON.
- Keep proper nouns (AMF, SMF, 5G-AKA, 5GS, gNB, NG-RAN, RRC, etc.).
- Resolve common abbreviations once (`5GS` -> `5G System`).
- Drop politeness ("please", "could you").
- <= 30 words.

User question:
{{ user_input }}
