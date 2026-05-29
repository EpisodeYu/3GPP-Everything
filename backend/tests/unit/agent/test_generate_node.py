"""generate_node：调 LLM 拼最终答案，并用正则抽 citations。"""

from __future__ import annotations

from app.agent.nodes import generate_node, parse_citations
from app.agent.nodes.generate import _render_tool_results, _sanitize_preview
from app.agent.state import AgentState
from app.agent.state import RetrievedChunk as StateChunk

from .conftest import StubLLM, make_deps


def _chunk(
    cid: str,
    *,
    spec: str,
    section: tuple[str, ...],
    title: str | None = None,
) -> StateChunk:
    return StateChunk(
        chunk_id=cid,
        spec_id=spec,
        section_path=section,
        section_title=title if title is not None else " / ".join(section),
        chunk_type="text",
        content=f"chunk content {cid}",
        score_rerank=0.9,
    )


async def test_generate_streams_answer_and_extracts_citations() -> None:
    answer = (
        "AMF is the Access and Mobility Management Function " "[1]. It anchors NAS signalling [1]."
    )
    llm = StubLLM(responses=[answer])
    deps = make_deps(llm=llm)
    state = AgentState(
        user_input="What is AMF?",
        user_language="en",
        rewritten_queries=["AMF function definition"],
        reranked=[
            _chunk("c1", spec="23.501", section=("6", "3", "1")),
            _chunk("c2", spec="38.331", section=("5", "3")),
        ],
    )

    out = await generate_node(state, deps=deps)
    assert "AMF" in out["final_answer"]
    assert len(out["citations"]) == 1, "重复 [1] 应去重"
    cite = out["citations"][0]
    assert cite["spec_id"] == "23.501"
    assert cite["chunk_id"] == "c1"
    assert cite["section_path"] == "6.3.1"
    assert cite["rank"] == 1
    # 生产路径走 chat_stream（不是非流式 chat）
    stream_calls = [c for c in llm.calls if c["kind"] == "chat_stream"]
    assert len(stream_calls) == 1
    assert stream_calls[0]["model"] == deps.settings.LLM_AGENT_MODEL
    # 不应再回落到非流式 chat
    assert not [c for c in llm.calls if c["kind"] == "chat"]


async def test_generate_falls_back_to_nonstream_when_stream_fails() -> None:
    """流式抛 LLMError → 回退到非流式 chat() 再产答案。"""
    from collections.abc import Sequence
    from typing import Any

    from app.core.errors import LLMError

    from .conftest import StubLLM as _Stub

    class _StreamBoomLLM(_Stub):
        async def chat_stream(  # type: ignore[override]
            self, messages: Sequence[dict[str, Any]], **kwargs: Any
        ) -> Any:
            self.calls.append({"kind": "chat_stream", "messages": list(messages), **kwargs})
            raise LLMError("network down")
            yield  # 让 mypy 知道这是 async generator

    llm = _StreamBoomLLM(responses=["Fallback answer [1]."])
    deps = make_deps(llm=llm)
    state = AgentState(
        user_input="X",
        user_language="en",
        reranked=[_chunk("c1", spec="23.501", section=("6", "3", "1"))],
    )
    out = await generate_node(state, deps=deps)
    assert "Fallback answer" in out["final_answer"]
    assert out["citations"][0]["chunk_id"] == "c1"
    assert out["citations"][0]["rank"] == 1
    assert [c["kind"] for c in llm.calls] == ["chat_stream", "chat"]


async def test_no_chunks_returns_fallback_message() -> None:
    deps = make_deps(llm=StubLLM(responses=["should not be called"]))
    state_en = AgentState(user_input="X", user_language="en", reranked=[])
    out_en = await generate_node(state_en, deps=deps)
    assert "Not found" in out_en["final_answer"]
    assert out_en["citations"] == []
    assert out_en["confidence"] == 0.0

    state_zh = AgentState(user_input="X", user_language="zh", reranked=[])
    out_zh = await generate_node(state_zh, deps=deps)
    assert "未在已索引" in out_zh["final_answer"]


# ===== parse_citations（v6 索引方案）单测 =====
# 形态：`[N]`（N = 1-based 序号 → chunks 列表对应 chunk）；越界/重复/非数字均不入选。


def test_parse_citations_basic_index_picks_correct_chunk() -> None:
    chunks = [
        _chunk("c1", spec="38.331", section=("5", "3")),
        _chunk("c2", spec="23.501", section=("6", "3", "1")),
        _chunk("c3", spec="38.213", section=("8", "1")),
    ]
    cites = parse_citations("see [2] for details, and [3] also.", chunks)
    assert [(c["rank"], c["chunk_id"]) for c in cites] == [(2, "c2"), (3, "c3")]
    assert cites[0]["spec_id"] == "23.501"
    assert cites[0]["section_path"] == "6.3.1"


def test_parse_citations_dedupes_repeated_index() -> None:
    chunks = [_chunk("c1", spec="38.331", section=("5", "3"))]
    cites = parse_citations("first [1] then [1] again [1].", chunks)
    assert len(cites) == 1
    assert cites[0]["rank"] == 1


def test_parse_citations_drops_out_of_bounds_index() -> None:
    chunks = [_chunk("c1", spec="38.331", section=("5", "3"))]
    # `[2]` 超界、`[0]` 不合法、`[1]` 命中
    cites = parse_citations("[1] and [2] and [0].", chunks)
    assert [c["rank"] for c in cites] == [1]


def test_parse_citations_handles_multi_chunk_consecutive_brackets() -> None:
    """LLM 多 chunk 引用形态 `[1][3]` —— 两个独立 match 都要留下。"""
    chunks = [
        _chunk("c1", spec="23.501", section=("6", "3", "1")),
        _chunk("c2", spec="23.501", section=("6", "3", "2")),
        _chunk("c3", spec="23.502", section=("4", "3", "2")),
    ]
    cites = parse_citations("AMF/SMF/UPF 协同[1][2][3]。", chunks)
    assert [c["rank"] for c in cites] == [1, 2, 3]


def test_parse_citations_ignores_legacy_spec_section_format() -> None:
    """v5 老格式 `[38.331 §5.3]` 已退役 —— 不再识别为引用（避免与 markdown link 误识）。"""
    chunks = [_chunk("c1", spec="38.331", section=("5", "3"))]
    cites = parse_citations("see [38.331 §5.3] for details.", chunks)
    assert cites == []


def test_parse_citations_does_not_match_markdown_links() -> None:
    """`[text](url)` markdown link 内的数字不应被误识为索引引用。"""
    chunks = [_chunk("c1", spec="38.331", section=("5", "3"))]
    cites = parse_citations("see [my doc](https://example.com/page1) and [1].", chunks)
    assert [c["rank"] for c in cites] == [1]


def test_parse_citations_handles_ie_chunk_empty_section() -> None:
    """IE chunk（section_path=()）通过 [N] 引用 → section_path 序列化为空串。"""
    chunks = [_chunk("c-ie", spec="38.331", section=(), title="PUCCH-Config IE")]
    cites = parse_citations("PUCCH-Config 定义见 [1]。", chunks)
    assert len(cites) == 1
    assert cites[0]["spec_id"] == "38.331"
    assert cites[0]["section_path"] == ""
    assert cites[0]["section_title"] == "PUCCH-Config IE"


def test_parse_citations_empty_inputs_return_empty() -> None:
    assert parse_citations("", [_chunk("c1", spec="38.331", section=("5",))]) == []
    assert parse_citations("plain text [1]", []) == []


def test_sanitize_preview_strips_html_tags() -> None:
    raw = "| <b>ControlResourceSet field descriptions</b> | desc |"
    out = _sanitize_preview(raw)
    assert "<b>" not in out
    assert "</b>" not in out
    assert "ControlResourceSet field descriptions" in out


def test_sanitize_preview_unwraps_emphasis_keeps_content() -> None:
    raw = "*ControlResourceSet* information element"
    out = _sanitize_preview(raw)
    assert "*" not in out
    assert "ControlResourceSet information element" in out


def test_sanitize_preview_drops_table_delimiter_lines_and_collapses_pipes() -> None:
    raw = "| h1 | h2 |\n" "|----|----|\n" "| DCI format 0_0 | DCI format 0_1 |"
    out = _sanitize_preview(raw)
    assert "|----" not in out
    assert "|---" not in out
    assert "DCI format 0_0" in out
    assert "DCI format 0_1" in out
    # delimiter 之外的管道符仍存在，但被规范化为 ` | `
    assert " | " in out


def test_sanitize_preview_truncates_long_text_with_ellipsis() -> None:
    raw = "A" * 500
    out = _sanitize_preview(raw, max_chars=180)
    assert len(out) == 180
    assert out.endswith("…")


def test_sanitize_preview_empty_returns_empty() -> None:
    assert _sanitize_preview("") == ""
    assert _sanitize_preview(None) == ""  # type: ignore[arg-type]


def test_sanitize_preview_strips_chunker_header_line() -> None:
    """chunker 注入的 `[spec § *IE* information element]` 头行必须被剥；
    否则前端 markdown 会把它当 citation 渲染成 chip（用户 2026-05-28 复测复现）。"""
    raw = (
        "[38.331 § *PUCCH-Config* information element]\n\n"
        "| PUCCH-Config field descriptions |\n"
        "| pucch-ResourceSetToAddModList |"
    )
    out = _sanitize_preview(raw)
    # 关键断言：sanitize 后不能再含 `[spec §...]` 这种完整 citation 形态
    # （否则前端 CitationInlineSyntax 会渲染出 chip 但 chunkId 永远缺）
    assert "[38.331" not in out
    assert "§" not in out
    # 但 chunker header 的内容已不在 → preview 直接展示表格内容
    assert "PUCCH-Config field descriptions" in out


def test_sanitize_preview_strips_chunker_header_even_when_spec_only() -> None:
    """clause 有数字的 chunker header 也要剥（`[23.501 § 6.3.1 AMF]`）。"""
    raw = "[23.501 § 6.3.1 AMF]\n\n" "AMF stands for Access and Mobility Management Function."
    out = _sanitize_preview(raw)
    assert "[23.501" not in out
    assert "AMF stands for" in out


def test_sanitize_preview_keeps_inline_citations_unchanged() -> None:
    """sanitize 只剥**独占一行**的 chunker header；行内的 [spec §sec] citation
    引用要保留（否则正文里 LLM 写的真 citation 也会被误吃）。"""
    raw = "见 [38.331 §5.3.5] 的描述，结合 [23.501 §6.3.1] 的定义。"
    out = _sanitize_preview(raw)
    assert "[38.331 §5.3.5]" in out
    assert "[23.501 §6.3.1]" in out


def test_render_tool_results_params_hits_no_residual_citation_markup() -> None:
    """端到端：tool 路径渲染 38.331 IE chunk（用户原报告复现），输出不应再含
    `[38.331 § ...]` 这种被前端误识别为 citation 的形态。"""
    state = AgentState(
        user_input="PUCCH-Config",
        tool_results={
            "params": {
                "query": "PUCCH-Config",
                "hits": [
                    {
                        "chunk_id": "c1",
                        "spec_id": "38.331",
                        "section_path": [],
                        "chunk_type": "table",
                        "score": 1.0,
                        "preview": (
                            "[38.331 § *PUCCH-Config* information element]\n\n"
                            "| <b>PUCCH-Config field descriptions</b> |\n"
                            "|----------------------------------------|\n"
                            "| pucch-ResourceSetToAddModList | list |"
                        ),
                    }
                ],
                "warning": None,
            }
        },
    )
    out = _render_tool_results(state)
    # tool 路径产生的"38.331 §:" 行头不带方括号，前端不会渲染为 chip
    assert "- 38.331 :" in out or "- 38.331 : " in out
    # 但 preview 里绝对不能再有 `[xx §yy]` 这种 chunker header（前端会渲染 chip）
    assert "[38.331" not in out
    assert "PUCCH-Config field descriptions" in out


def test_render_tool_results_params_hits_uses_sanitized_preview() -> None:
    """用户报告复现：DCI1_1 params hits → 期望 <b>、*xxx*、表格分隔行都被清掉。"""
    state = AgentState(
        user_input="DCI1_1 字段",
        tool_results={
            "params": {
                "query": "DCI1_1",
                "hits": [
                    {
                        "chunk_id": "c1",
                        "spec_id": "38.331",
                        "section_path": [],
                        "chunk_type": "table",
                        "score": 1.0,
                        "preview": (
                            "[38.331 § *PUCCH-Config* information element]\n\n"
                            "| <b>PUCCH-Config field descriptions</b> |\n"
                            "|----------------------------------------|\n"
                            "| field1 | desc1 |"
                        ),
                    }
                ],
                "warning": None,
            }
        },
    )
    out = _render_tool_results(state)
    assert "<b>" not in out
    assert "</b>" not in out
    assert "|----" not in out
    # `*PUCCH-Config*` 应被解包成 `PUCCH-Config`
    assert "*PUCCH-Config*" not in out
    assert "PUCCH-Config" in out
    # 空 section_path → section 段不应出现"§:"裸冒号（v5 优化：sect 段为空时省略 `§`）
    assert "§:" not in out


def test_render_tool_results_glossary_definition_also_sanitized() -> None:
    state = AgentState(
        user_input="X",
        tool_results={
            "glossary": {
                "matches": [
                    {
                        "term": "AMF",
                        "spec_id": "23.501",
                        "section_path": ["3", "1"],
                        "definition": "*Access* and **Mobility** <b>Management</b> Function",
                    }
                ]
            }
        },
    )
    out = _render_tool_results(state)
    assert "<b>" not in out
    assert "*Access*" not in out
    assert "**Mobility**" not in out
    assert "Access and Mobility Management Function" in out
