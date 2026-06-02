---
version: 1
notes: |
  多轮指代消解（2026-06-02 接通真多轮，A 层）。把依赖上文的追问，用对话历史
  补全成「自包含」的问题，再交给 classify / rewrite / hyde / retrieve 全链路。
  只解消指代/省略，不翻译、不臆造关键词；保留原语言。仅当 state.history 非空时
  由 graph 条件入边触发（首轮直连 classify，零额外成本）。模型走 LLM_LIGHT_MODEL
  + thinking=disabled，可复现。
---
You rewrite a possibly context-dependent follow-up question into a SINGLE
STANDALONE question, using the conversation history ONLY to resolve pronouns,
ellipsis, and implicit references (e.g. "it", "that field", "那 5G 呢",
"继续展开第 2 点").

Rules:
- Output exactly ONE line: the rewritten standalone question. No quotes, no
  commentary, no JSON, no explanation.
- Keep the SAME language as the current question.
- Resolve references ("it" / "that" / "这个" / "那个" / "继续" / "上面那个") into
  the concrete entity, IE, parameter, or topic taken from the history.
- Do NOT invent facts, spec numbers, section paths, or keywords that are not
  present in the history or the current question.
- If the current question is ALREADY self-contained, return it UNCHANGED.
- Be concise; preserve proper nouns verbatim (AMF, SMF, PUCCH-Config, 5G-AKA,
  gNB, NG-RAN, RRC, etc.).

Conversation history (oldest first):
{% for h in history %}
{{ h.role }}: {{ h.content }}
{% endfor %}

Current question:
{{ user_input }}
