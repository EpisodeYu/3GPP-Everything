---
version: 1
notes: |
  自校验（grounding-only for M4.2）。M4.2 simple fast path 只看忠实度 / 引用覆盖，
  不触发 retry；M4.3 接入 retry loop 后用同一 prompt 但 caller 允许 verdict=retry。
---
You audit a generated answer against the chunks that were supposed to ground it.
Emit ONE JSON object — no prose, no markdown fence — matching:

```
{
  "faithful": true | false,
  "coverage": <float 0..1>,
  "confidence": <float 0..1>,
  "verdict": "accept" | "retry" | "insufficient",
  "missing_aspects": [<string>]
}
```

Definitions:
- `faithful` = every factual claim in the answer is supported by at least one chunk.
- `coverage` = fraction of the user's request that the answer actually addresses.
- `confidence` = your overall trust in the answer (0..1).
- `verdict`:
  - `accept` if `faithful` is true AND `coverage >= 0.7`.
  - `retry` if `faithful` is true but `coverage < 0.7` AND there are concrete
    `missing_aspects` worth a second retrieval pass.
  - `insufficient` if `faithful` is false OR the chunks don't support a real answer.
- `missing_aspects` = short English nouns/phrases that the next retrieval pass
  should target. May be empty.

Inputs:

User question:
{{ user_input }}

Retrieved chunks (top {{ chunks|length }}):
{% for c in chunks %}
[{{ loop.index }}] spec_id={{ c.spec_id }} section_path={{ c.section_path | join('.') }}
{{ c.content[:400] }}
{% endfor %}

Generated answer:
{{ answer }}

Output: ONLY the JSON object.
