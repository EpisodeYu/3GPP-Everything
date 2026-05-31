"""LaTeX 公式 alt-text 抽取（背景：2026-05-30 ragas 4-metric uplift handoff §3.4）。

GSMA marker 上游对 3GPP spec 的 LaTeX 抽取不稳定：
- 部分 `$$...$$` 块在抽取阶段就丢了内容，留下 "is defined by\\n\\nwhere\\nand"
  这种 描述句 + 占位 anchor 的 skeleton（典型样例：38.211 §5.3.1 / §8.4.2.2.1、
  38.214 §8.1.7）。
- 部分 `$...$` inline math 保留，但夹在散文里 retrieval 信号弱（voyage-4 dense
  对 LaTeX token 不敏感、BM25 又拿不到拆好的标识符）。

本模块在 chunker 层做两件事 —— **不试图恢复已丢的源文本**（那需要换上游 marker
或 OCR 重抽），只增强 retrieval signal：

1. `has_stripped_formula_marker(text)`：识别 "defined by / given by / as follows"
   后紧跟空段 + "where / and / with / here" 的模式 → chunk 里追加一行
   `[公式占位：上游 markdown 抽空]` 让 LLM 知道"这里原本有公式"。
2. `extract_latex_symbols(text)`：从保留的 `$...$` / `$$...$$` 中抽变量名 + 符号
   token → chunk 末尾追加 `Formula symbols: ...` alt-text，BM25 拿到拆好的标识符、
   dense embed 拿到自然语言列表。

调用方：`builder._build_text_chunk_content` 调 `build_formula_annotation`；
有信号则把返回串 append 到 chunk content。无信号返回空串、零开销。
"""

from __future__ import annotations

import re

# trigger 短语：以这些介词短语结尾、紧跟空段、紧接 anchor 词 → 高概率是被抽空的公式
_TRIGGER_PHRASE_RE = re.compile(
    r"\b("
    r"defined\s+by|given\s+by|expressed\s+by|computed\s+(?:as|by)|obtained\s+(?:as|by)|"
    r"determined\s+by|described\s+by|denoted\s+by|generated\s+(?:as|by)|"
    r"calculated\s+(?:as|by)|written\s+as|stated\s+as|formulated\s+as|"
    r"converted\s+to[^.\n]*as|as\s+follows|is\s+given\s+(?:as|by)"
    r")[\t ]*[:.,]?[\t ]*$",
    re.IGNORECASE | re.MULTILINE,
)
# 抽空 anchor：紧接空段后的孤立短词（"where" / "and" / "with" / "here" / "in which"
# 单独成段 + 后续上下文继续描述变量含义）。
_GAP_ANCHOR_RE = re.compile(
    r"^[\t ]*(where|and|with|here|in which|for which|such that)[\t ]*[:.,]?[\t ]*$",
    re.IGNORECASE | re.MULTILINE,
)
# bullet 抽空：`- is given by ...` / `- are defined as ...`（LHS 变量名已丢，只剩
# 谓语介词短语）。
_BULLET_ORPHAN_RE = re.compile(
    r"^[-\*]\s+(is|are|denotes?|represents?)\s+(given|defined|expressed|the|a|an)\b",
    re.IGNORECASE | re.MULTILINE,
)
# display / inline 数学块。`\$\$ ... \$\$` 必须优先匹配，否则会被吃成 `\$ ... \$`。
_DISPLAY_MATH_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_MATH_RE = re.compile(r"(?<!\$)\$(?!\$)([^$\n]{1,400}?)(?<!\$)\$(?!\$)")

# LaTeX 关键字 / 语法 token：从 \\command 抽出后会落到这里，统一丢掉。变量符号
# 单字母（l / k / N / mu）保留 — Greek 字母被 \command 转成普通 token 后也走这条。
_LATEX_KEYWORDS: frozenset[str] = frozenset(
    {
        # 函数 / operator
        "sum",
        "prod",
        "int",
        "iint",
        "iiint",
        "oint",
        "lim",
        "limsup",
        "liminf",
        "sup",
        "inf",
        "max",
        "min",
        "arg",
        "log",
        "ln",
        "lg",
        "exp",
        "mod",
        "gcd",
        "sin",
        "cos",
        "tan",
        "cot",
        "sec",
        "csc",
        "sinh",
        "cosh",
        "tanh",
        "arcsin",
        "arccos",
        "arctan",
        # 关系符 / 连接词
        "in",
        "notin",
        "subset",
        "supset",
        "subseteq",
        "supseteq",
        "cup",
        "cap",
        "leq",
        "geq",
        "neq",
        "sim",
        "approx",
        "equiv",
        "propto",
        "perp",
        "parallel",
        "models",
        "vdash",
        # 箭头
        "to",
        "rightarrow",
        "leftarrow",
        "Rightarrow",
        "Leftarrow",
        "leftrightarrow",
        "mapsto",
        # 省略 / 分隔
        "ldots",
        "cdots",
        "vdots",
        "ddots",
        "dots",
        # 排版命令（外壳，内部已被剥离）
        "frac",
        "dfrac",
        "tfrac",
        "sqrt",
        "left",
        "right",
        "big",
        "Big",
        "bigg",
        "Bigg",
        "biggl",
        "biggr",
        "begin",
        "end",
        "cases",
        "matrix",
        "pmatrix",
        "bmatrix",
        "vmatrix",
        "array",
        "text",
        "textrm",
        "textbf",
        "textit",
        "mathrm",
        "mathbf",
        "mathit",
        "mathbb",
        "mathcal",
        "mathsf",
        "mathtt",
        "boldsymbol",
        "operatorname",
        "overline",
        "underline",
        "overrightarrow",
        "underbrace",
        "overbrace",
        "label",
        "tag",
        "displaystyle",
        "textstyle",
        "scriptstyle",
        # 量词 / 逻辑
        "forall",
        "exists",
        "neg",
        "land",
        "lor",
        "implies",
        # 散粒
        "cdot",
        "times",
        "div",
        "pm",
        "mp",
        "ast",
        "star",
        "circ",
        "bullet",
        "otimes",
        "oplus",
        "infty",
        "partial",
        "nabla",
        "emptyset",
        "varnothing",
        # 连词残留
        "if",
        "then",
        "else",
        "otherwise",
        "where",
        "for",
        "while",
        "and",
        "or",
    }
)


def has_stripped_formula_marker(text: str) -> bool:
    """是否含 "上游公式被抽空" 模式。

    判定为正的条件（任一命中）：
    - trigger 短语行后跟≥1空行再跟 gap anchor 行
    - 连续 ≥ 2 个孤立 anchor 行（"where" / "and" / "with" 单独成段）
    - 含 ≥ 1 个 bullet orphan（"- is given by ..." 类）
    """
    if not text:
        return False
    # 模式 A：trigger + 空段 + anchor
    if _trigger_followed_by_gap(text):
        return True
    # 模式 B：连续多个 anchor 段
    anchor_lines = _GAP_ANCHOR_RE.findall(text)
    if len(anchor_lines) >= 2:
        return True
    # 模式 C：bullet orphan
    return bool(_BULLET_ORPHAN_RE.search(text))


def _trigger_followed_by_gap(text: str) -> bool:
    """trigger 短语之后（≥ 1 空行）紧跟 anchor 行/段。

    例：
        "... defined by\\n\\nwhere ..."
        "... given by:\\n\\nand"
    """
    for m in _TRIGGER_PHRASE_RE.finditer(text):
        tail = text[m.end() : m.end() + 200]
        if _GAP_THEN_ANCHOR_RE.match(tail):
            return True
    return False


_GAP_THEN_ANCHOR_RE = re.compile(
    r"\s*\n\s*\n\s*(where|and|with|here|in which|for which|such that)\b",
    re.IGNORECASE,
)


def extract_latex_symbols(text: str, *, max_symbols: int = 40) -> list[str]:
    """从 `$...$` / `$$...$$` 中抽变量名 / 符号 token，去重保序返回。

    规则：
    1. 找出全部 display + inline math 段（注意 `$$` 比 `$` 先匹配）
    2. 每段做规范化：剥 `\\text{}` / `\\mathrm{}` / `\\mathbf{}` 等排版命令的外壳但
       保留内部；`\\command` 转 ` command `（让 `\\Delta` → `Delta` 保留为 token）
    3. 按分隔符（空格 / 逗号 / 等号 / 运算符 / 括号 / 大括号）切 token；保留 `_` 和
       `^` 作为标识符内部组合符（`N_slot^subframe` 整体一个 token）
    4. 过滤：纯数字、LaTeX 关键字（`frac`/`text`/`begin`/`cases`/...）、空字符串丢
    5. 去重保序；上限 `max_symbols`，防止罕见超长公式拉爆 chunk content

    Returns:
        token 列表（顺序为出现顺序）；无公式或全部被过滤掉时返回空列表。
    """
    if not text or "$" not in text:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> bool:
        """加 token；返回是否到上限。"""
        tok = tok.strip().strip("_^.,").strip()
        if not tok:
            return False
        if tok in seen:
            return False
        # 纯数字或纯运算符 token 丢
        if tok.lstrip("-+").replace(".", "").isdigit():
            return False
        if tok.lower() in _LATEX_KEYWORDS:
            return False
        # 必须至少含 1 个字母（否则是 `_^,` 这类残渣）
        if not any(c.isalpha() for c in tok):
            return False
        seen.add(tok)
        out.append(tok)
        return len(out) >= max_symbols

    def _emit_from_formula(formula: str) -> bool:
        normalized = _normalize_latex(formula)
        return any(_add(tok) for tok in normalized.split())

    for m in _DISPLAY_MATH_RE.finditer(text):
        if _emit_from_formula(m.group(1)):
            return out
    # 把已 match 的 `$$...$$` 替换成空格，避免被 inline 二次抓
    inline_scan_text = _DISPLAY_MATH_RE.sub(" ", text)
    for m in _INLINE_MATH_RE.finditer(inline_scan_text):
        if _emit_from_formula(m.group(1)):
            return out
    return out


_TEXT_WRAPPER_RE = re.compile(
    r"\\(?:text|textrm|textbf|textit|mathrm|mathbf|mathit|mathbb|mathcal|mathsf|mathtt|operatorname|boldsymbol)\s*\{([^{}]*)\}"
)
_COMMAND_RE = re.compile(r"\\([a-zA-Z]+)")


def _normalize_latex(formula: str) -> str:
    """把 LaTeX 片段拆成空格分隔的 token 流。

    保留 `_` / `^` 作为标识符内部组合符，方便上游把 `N_slot^subframe_mu` 当一个
    可检索 token；其余分隔符（空格 / 逗号 / 等号 / 运算符 / 大括号 / 反斜杠）一律
    转空格。
    """
    s = formula
    # 1. `\text{X}` / `\mathrm{X}` etc. → ` X `，递归 2 层（足够覆盖 3GPP spec 中
    #    `N_{\\text{slot}}^{\\text{subframe}, \\mu}` 这类常见嵌套）
    for _ in range(3):
        new = _TEXT_WRAPPER_RE.sub(r" \1 ", s)
        if new == s:
            break
        s = new
    # 2. `\command` → ` command ` （Greek 字母 / 函数名都通过这条保留；后续被
    #    `_LATEX_KEYWORDS` 过滤）
    s = _COMMAND_RE.sub(r" \1 ", s)
    # 3. 括号、大括号、反斜杠、运算符 → 空格；保留 `_` / `^` 作为标识符内部
    s = re.sub(r"[{}\\\[\]()|]", " ", s)
    s = re.sub(r"[=+*/<>!~&%@?:;\"'`]", " ", s)
    s = re.sub(r",", " ", s)
    # `-` 一律当分隔符（math 块内 `E-1` / `x^{-1}` 拆成 `E`/`1`/`x^` 都 OK，数字
    # token 在上层会被过滤掉、`x^` 经 strip 还原为 `x`）
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def build_formula_annotation(text: str, *, max_symbols: int = 40) -> str:
    """供 builder 调用：根据 chunk 文本拼"公式 alt-text"。

    Returns:
        - 不含 `$` 且无抽空模式 → 空串（builder 直接跳过）
        - 含公式 / 抽空模式 → 多行 alt-text，形如：
            `Formula symbols: N_slot, subframe, mu, a_k,l, Delta, f`
            `[Note: source markdown contains stripped formula(s); ...]`
    """
    if not text:
        return ""

    lines: list[str] = []
    symbols = extract_latex_symbols(text, max_symbols=max_symbols)
    if symbols:
        lines.append("Formula symbols: " + ", ".join(symbols))
    if has_stripped_formula_marker(text):
        lines.append(
            "[Note: source markdown contains stripped formula(s); "
            "variable names and structure described in surrounding prose]"
        )
    return "\n".join(lines)


__all__ = [
    "build_formula_annotation",
    "extract_latex_symbols",
    "has_stripped_formula_marker",
]
