""" "未找到"短语词表与 `is_not_found_answer()` 判定。

供 backend agent（`suggest_questions` 节点）+ eval runner（`must_say_not_found_passed`
metric）共用。两侧实现镜像，参数与返回值必须一致；改一侧务必同步另一侧。

镜像：`eval/not_found_phrases.py`（eval 子项目不依赖 backend，故复制；不要让二者漂移）

源：`docs/03-development/06-evaluation-and-observability.md §12 M7.1` +
    `docs/04-handoff/2026-05-20-suggested-questions.md §3.1`
"""

from __future__ import annotations

NOT_FOUND_PHRASES_EN: tuple[str, ...] = (
    "not found",
    "not specified",
    "no such",
    "does not define",
    "is not defined in",
    "outside the scope",
    # 2026-05-20 daily eval 复盘补充（详见 04-handoff/2026-05-20-daily-eval-findings.md）：
    # LLM 拒答常用表达，原 6 个偏正式短语覆盖不到
    "cannot answer",
    "cannot support",
    "cannot determine",
    "premise does not hold",
    "not mentioned",
    "does not apply",
    "does not exist",
    "no information",
)

NOT_FOUND_PHRASES_ZH: tuple[str, ...] = (
    "未找到",
    "未定义",
    "规范未规定",
    "不涉及",
    "不在范围内",
    "没有相关规定",
    # 2026-05-20 daily eval 复盘补充（详见 04-handoff/2026-05-20-daily-eval-findings.md）：
    # daily 16 条 negative 上 agent 实际用的拒答表达，原词表全没覆盖
    "前提不成立",
    "无法回答",
    "无法支持",
    "无法确定",
    "未提及",
    "并未涉及",
    "并不存在",
    "未包含",
)


def is_not_found_answer(answer: str, language: str) -> bool:
    """answer 是否触发"未找到"语义。

    en 题用 en 词表，zh 题用 zh 词表（互不交叉，避免中文 negative 被误判）。
    匹配规则：substring case-insensitive。
    """
    if not answer:
        return False
    haystack = answer.lower()
    phrases = NOT_FOUND_PHRASES_ZH if language == "zh" else NOT_FOUND_PHRASES_EN
    return any(p.lower() in haystack for p in phrases)


__all__ = [
    "NOT_FOUND_PHRASES_EN",
    "NOT_FOUND_PHRASES_ZH",
    "is_not_found_answer",
]
