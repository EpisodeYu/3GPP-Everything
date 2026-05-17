---
version: 1
notes: |
  Hypothetical Document Embedding（M4.3 启用）；让 LLM 假装写一段"理想答案"，用其
  embedding 一起检索。simple 分支不走本 prompt。
---
You are drafting a hypothetical 3GPP TS specification paragraph that, if it existed,
would directly answer the user's question. The text will be embedded and used as a
retrieval probe alongside the original question.

Rules:
- 200-400 tokens.
- Match the dense, declarative style of 3GPP normative clauses.
- Use canonical 3GPP terminology (AMF, SMF, UPF, gNB, RRC, NAS, PDU Session, ...).
- Do NOT invent section numbers, table numbers, or quote citations.
- No bullet points, no markdown headings — write a single coherent paragraph.

User question:
{{ user_input }}
