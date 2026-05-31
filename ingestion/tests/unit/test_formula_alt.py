"""LaTeX 公式 alt-text 抽取单测（`ingestion/chunker/formula_alt.py`）。

覆盖：
- `extract_latex_symbols`：display / inline / 混合 / 转义 / 上限 / 边界
- `has_stripped_formula_marker`：trigger+gap / anchor 连发 / bullet orphan / 反例
- `build_formula_annotation`：纯文本 / 仅公式 / 仅抽空 / 二者皆有

样本采自 2026-05-30 ragas uplift handoff 中"ctx_recall 卡在 formula 0.52"的真实
chunk 片段（38.211 §5.3.1, 38.211 §8.4.2.2.1, 38.212 §5.4.1.2, 38.214 §8.1.7）。
"""

from __future__ import annotations

from ingestion.chunker.formula_alt import (
    build_formula_annotation,
    extract_latex_symbols,
    has_stripped_formula_marker,
)

# ---------------------------------------------------------------------------
# extract_latex_symbols
# ---------------------------------------------------------------------------


def test_extract_inline_math_simple() -> None:
    symbols = extract_latex_symbols(r"the bit sequence $\mathbf{v}$ is written")
    assert symbols == ["v"]


def test_extract_inline_math_with_subscript() -> None:
    symbols = extract_latex_symbols(r"$v_k = e_k$ ; for $k=0$ to $E-1$")
    # 各 token 都应被拆好；E、k、v_k、e_k 都该出现
    assert "v_k" in symbols
    assert "e_k" in symbols
    assert "k" in symbols
    assert "E" in symbols
    # 纯数字 0、1 不进列表
    assert "0" not in symbols and "1" not in symbols


def test_extract_inline_math_text_wrapper_stripped() -> None:
    """`$N_{\\text{slot}}^{\\text{subframe}, \\mu}$` → N_slot, subframe, mu 之类的 token。"""
    src = (
        r"OFDM symbol $l \in \{0, 1, \dots, N_{\text{slot}}^{\text{subframe}, \mu} "
        r"N_{\text{symb}}^{\text{slot}} - 1\}$ in a subframe"
    )
    symbols = extract_latex_symbols(src)
    # 关键标识符必须命中（顺序不严格）
    lowered = [s.lower() for s in symbols]
    assert any("slot" in s for s in lowered)
    assert any("subframe" in s for s in lowered)
    assert any("symb" in s for s in lowered)
    assert "mu" in lowered or "Mu" in symbols
    assert "l" in symbols
    # 排版关键字与省略号不应留
    assert "text" not in lowered
    assert "dots" not in lowered
    assert "ldots" not in lowered


def test_extract_display_math_block() -> None:
    """`$$...$$` 块的内容应能抽出符号；CASES 等结构关键字应过滤。"""
    src = (
        r"$$t_{start,l}^{\mu} = \begin{cases} 0 & l = 0 "
        r"\\ t_{start,l-1}^{\mu} + (N_u^{\mu} + N_{CP,l-1}^{\mu}) \cdot T_c & "
        r"\text{otherwise} \end{cases}$$"
    )
    symbols = extract_latex_symbols(src)
    lowered = [s.lower() for s in symbols]
    # `t_start`，`mu`，`N_u`，`N_CP`，`T_c` 应在
    assert any(s.startswith("t_") for s in lowered)
    assert "mu" in lowered
    assert any("n_u" in s or "n_cp" in s for s in lowered)
    assert any("t_c" in s for s in lowered)
    # 结构关键字应过滤
    assert "begin" not in lowered
    assert "end" not in lowered
    assert "cases" not in lowered
    assert "otherwise" not in lowered
    assert "cdot" not in lowered


def test_extract_display_takes_precedence_over_inline() -> None:
    """`$$...$$` 必须先被抓，否则会被 `$ ... $` 误吃。"""
    src = "$$a + b = c$$ and inline $d = e$"
    symbols = extract_latex_symbols(src)
    assert "a" in symbols and "b" in symbols and "c" in symbols
    assert "d" in symbols and "e" in symbols


def test_extract_greek_letters_via_backslash_command() -> None:
    src = r"$\Delta f$ and $\alpha$ and $n^{\text{RA}}$"
    symbols = extract_latex_symbols(src)
    # `\Delta` → "Delta"；`\alpha` → "alpha"
    assert "Delta" in symbols
    assert "alpha" in symbols
    # `n^{\text{RA}}` → 应抽出含 RA 的 token
    assert any("RA" in s for s in symbols)


def test_extract_returns_empty_when_no_math() -> None:
    text = "Just a normal paragraph with no math at all."
    assert extract_latex_symbols(text) == []


def test_extract_preserves_order_and_dedupes() -> None:
    """同符号多次出现只保留首次顺位。"""
    src = "$N$ first, then $N$ again, then $M$, then $N$"
    symbols = extract_latex_symbols(src)
    assert symbols == ["N", "M"]


def test_extract_respects_max_symbols_cap() -> None:
    src = " ".join(f"$x_{i}$" for i in range(60))
    symbols = extract_latex_symbols(src, max_symbols=5)
    assert len(symbols) == 5


def test_extract_filters_pure_numbers() -> None:
    src = "$x = 3.14$ and $y = 42$"
    symbols = extract_latex_symbols(src)
    assert "x" in symbols and "y" in symbols
    assert "3.14" not in symbols
    assert "42" not in symbols
    assert "3" not in symbols


def test_extract_does_not_crash_on_unclosed_dollar() -> None:
    """单独一个 `$` 不应让函数崩；返回空或忽略半截。"""
    src = "incomplete $math without closing delimiter"
    # 不抛即可（具体抽到什么是次要的）
    extract_latex_symbols(src)


# ---------------------------------------------------------------------------
# has_stripped_formula_marker
# ---------------------------------------------------------------------------


def test_stripped_marker_defined_by_then_where() -> None:
    """38.211 §5.3.1 形态："is defined by\\n\\nwhere..."。"""
    text = (
        "The time-continuous signal on antenna port and subcarrier spacing configuration "
        "for OFDM symbol in a subframe for any physical channel or signal except PRACH "
        "is defined by\n\nwhere at the start of the subframe,\n\nand\n"
    )
    assert has_stripped_formula_marker(text) is True


def test_stripped_marker_8_4_2_2_1_skeleton() -> None:
    """38.211 §8.4.2.2.1 极端样本：trigger + 空段 + where + 空段 + and 全 skeleton。"""
    text = (
        "The sequence for the sidelink primary synchronization signal is defined by\n"
        "\n"
        "where\n"
        "\n"
        "and\n"
    )
    assert has_stripped_formula_marker(text) is True


def test_stripped_marker_bullet_orphan() -> None:
    """38.211 §5.3.1 中的 `- is given by clause 4.2;` 这种 LHS 已丢的 bullet。"""
    text = (
        "where\n\n"
        "and\n\n"
        "- is given by clause 4.2;\n"
        "- is the subcarrier spacing configuration;\n"
    )
    assert has_stripped_formula_marker(text) is True


def test_stripped_marker_converted_to_as() -> None:
    """38.214 §8.1.7 形态：'... is converted to ... as:\\n\\nwhere ...'。"""
    text = (
        "A given resource reservation period in milliseconds is converted to a period "
        "in logical slots as:\n\nwhere is the number of slots that belong to a resource pool.\n"
    )
    assert has_stripped_formula_marker(text) is True


def test_stripped_marker_negative_normal_prose() -> None:
    """正常散文不应误判：没有 trigger + gap + anchor。"""
    text = (
        "The UE transmits sidelink CSI-RS within a unicast PSSCH transmission "
        "if the following conditions hold. The number of antenna ports is configured "
        "via higher-layer parameters as needed."
    )
    assert has_stripped_formula_marker(text) is False


def test_stripped_marker_negative_inline_math_only() -> None:
    """全 inline math 保留、无抽空模式 → False。"""
    text = (
        "Denoting by $M$ the rate matching output sequence length, the bit selection "
        "output bit sequence $\\mathbf{v}$ is generated as follows: for $k=0$ to $E-1$ "
        "use $v_k = e_k$."
    )
    assert has_stripped_formula_marker(text) is False


def test_stripped_marker_negative_single_anchor_not_enough() -> None:
    """单一 'where' 行 + 上下文充足 → 不算抽空（避免常规散文误报）。"""
    text = (
        "The system uses higher-layer signaling.\n\n"
        "where higher-layer signaling refers to RRC.\n\n"
        "The procedure is documented in 38.331.\n"
    )
    # 注意：这里没有 trigger 短语在前，只有一个 anchor 行 → False
    assert has_stripped_formula_marker(text) is False


# ---------------------------------------------------------------------------
# build_formula_annotation
# ---------------------------------------------------------------------------


def test_annotation_empty_for_plain_prose() -> None:
    text = "Normal paragraph without any math or stripped formula markers."
    assert build_formula_annotation(text) == ""


def test_annotation_symbols_only_when_no_stripped_pattern() -> None:
    src = "Sequence $v_k = e_k$ for $k = 0$ to $E-1$."
    ann = build_formula_annotation(src)
    assert ann.startswith("Formula symbols:")
    assert "stripped" not in ann.lower()
    assert "v_k" in ann
    assert "e_k" in ann


def test_annotation_stripped_note_only_when_no_symbols() -> None:
    """38.211 §8.4.2.2.1 极端形态：trigger + 抽空 + 无任何 `$...$`。"""
    src = (
        "The sequence for the sidelink primary synchronization signal is defined by\n"
        "\nwhere\n\nand\n"
    )
    ann = build_formula_annotation(src)
    assert "Formula symbols:" not in ann
    assert "stripped formula" in ann.lower()


def test_annotation_combines_symbols_and_stripped_note() -> None:
    """同 chunk 既有保留的 inline math、又有抽空模式（很常见）→ 两条都出现。"""
    src = (
        "OFDM symbol $l \\in \\{0, 1\\}$ is defined by\n"
        "\nwhere $N_{\\text{slot}}$ is the slot count,\n"
        "\nand\n"
    )
    ann = build_formula_annotation(src)
    assert "Formula symbols:" in ann
    assert "stripped formula" in ann.lower()


def test_annotation_38211_5_3_1_real_sample() -> None:
    """38.211 §5.3.1 真实片段（handoff §3.4 提到的 ctx_recall 0.52 苦主）。

    断言：
    - 含 stripped 标注（GSMA marker 把 OFDM 基带公式抽空）
    - symbols 行覆盖 hand-formula-001 expected_facts 的 English alias：
      OFDM symbol / subcarrier spacing / antenna port / time-continuous signal
      虽然这些是英文短语不是 LaTeX，但 inline `$N_{slot}^{subframe,mu}$` 抽出的
      `subframe` / `slot` / `mu` 可与 facts 中相关词形匹配
    """
    src = (
        "The time-continuous signal on antenna port and subcarrier spacing configuration "
        "for OFDM symbol  $l \\in \\{0, 1, \\dots, N_{\\text{slot}}^{\\text{subframe}, \\mu} "
        "N_{\\text{symb}}^{\\text{slot}} - 1\\}$  in a subframe for any physical channel or "
        "signal except PRACH is defined by\n"
        "\n"
        "where at the start of the subframe,\n"
        "\n"
        "and\n"
        "\n"
        "- is given by clause 4.2;\n"
        "- is the subcarrier spacing configuration;\n"
    )
    ann = build_formula_annotation(src)
    assert "Formula symbols:" in ann
    assert "stripped formula" in ann.lower()
    # 抽出的符号应包含 subframe / slot 等关键概念
    assert "subframe" in ann.lower()
    assert "slot" in ann.lower()
