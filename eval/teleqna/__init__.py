"""TeleQnA 拉取与过滤模块。

数据源：https://github.com/netop-team/TeleQnA
- 仓库内 `TeleQnA.txt.zip`（密码 'teleqnadataset'），解压后 ~10000 题 JSON
- 字段：question / option 1..n / answer / explanation / category

M3 评测的核心约束（用户在 2026-05-16 决议中强调）：
- 题目必须严格属于本期 17 篇 spec whitelist
- filter 推断不到 expected_specs ∈ whitelist 的题目 → out_of_scope.jsonl

子模块：
- `whitelist`：17 篇 spec_id 与对照表
- `pull`：克隆 / 解压 / 解析 raw JSON → jsonl
- `filter`：category + 关键词 + spec 推断 → filtered.jsonl
"""

from .whitelist import (
    POC_17_SPECS,
    SPEC_ALIAS_PATH,
    is_in_whitelist,
    load_spec_aliases,
    normalize_spec_id,
)

__all__ = [
    "POC_17_SPECS",
    "SPEC_ALIAS_PATH",
    "is_in_whitelist",
    "load_spec_aliases",
    "normalize_spec_id",
]
