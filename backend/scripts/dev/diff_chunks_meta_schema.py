"""一次性对比 ingestion 与 alembic 两侧的 `chunks_meta` schema 定义。

背景：M4.0 完成时 alembic 初版接管了 chunks_meta 表，但生产 PG 中早已有 394,859 行
数据按 `ingestion.indexer.pg_writer.chunks_meta_table`（基于 CREATE TABLE IF NOT
EXISTS 的过渡 schema）入库。M4.6 启动前需要确认这两份 schema 在硬字段（列名 / 类型
/ nullable / 索引覆盖 / UNIQUE / FK）上**完全一致**，否则未来 alembic migration 接管
真实 PG 时会破坏现有数据。

属于 handoff/2026-05-17-m4.6-m4.9-decisions.md 中 Q1（O6 双写校验）的一次性脚本：
跑过 → 归档报告到 docs/04-handoff/ → 不并入 CI。

用法（在 backend 虚环境下）::

    cd backend
    uv run python scripts/dev/diff_chunks_meta_schema.py \\
        --output ../docs/04-handoff/2026-05-17-chunks-meta-schema-diff.md

退出码：0 = ✅ 关键 schema 一致；非 0 = ❌ 有硬差异，触发 CLAUDE.md §5.1 上报。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Column, Index, MetaData, Table

PROJECT_ROOT = Path(__file__).resolve().parents[3]
INGESTION_PG_WRITER = PROJECT_ROOT / "ingestion" / "indexer" / "pg_writer.py"
ALEMBIC_INIT = (
    PROJECT_ROOT
    / "backend"
    / "alembic"
    / "versions"
    / "20260517_0737_9cf40059f3b1_init_schema.py"
)


def _load_module_from_path(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_ingestion_table() -> Table:
    """直接按文件路径加载 ingestion/indexer/pg_writer.py，取 `chunks_meta_table`。

    通过 spec_from_file_location 避开 ingestion 包未安装到 backend venv 的问题。
    pg_writer.py 内部只用 stdlib + sqlalchemy，无相对 import，安全。
    """
    mod = _load_module_from_path("ingestion_pg_writer", INGESTION_PG_WRITER)
    return mod.chunks_meta_table  # type: ignore[no-any-return]


def load_alembic_table() -> Table:
    """mock `alembic.op` 拦截 create_table / create_index 调用，重建 chunks_meta Table。

    思路：alembic revision 顶部 `from alembic import op`；只要 sys.modules["alembic"]
    是我们 fake 出的 module 且有 `op` 属性，import 语句就会拿到 fake op，所有
    op.create_table(...) 调用会进我们的 captures。
    """
    captured_columns: list[Any] = []
    captured_indexes: list[dict[str, Any]] = []
    captured_table_kwargs: dict[str, Any] = {}

    class _MockOp:
        @staticmethod
        def f(name: str) -> str:
            return name

        @staticmethod
        def create_table(name: str, *args: Any, **kwargs: Any) -> None:
            if name == "chunks_meta":
                captured_columns.extend(args)
                captured_table_kwargs.update(kwargs)

        @staticmethod
        def create_index(
            name: str, table_name: str, columns: list[str], **kwargs: Any
        ) -> None:
            if table_name == "chunks_meta":
                captured_indexes.append(
                    {
                        "name": name,
                        "columns": list(columns),
                        "unique": bool(kwargs.get("unique", False)),
                    }
                )

        @staticmethod
        def drop_table(_name: str) -> None:
            return None

        @staticmethod
        def drop_index(_name: str, table_name: str | None = None) -> None:
            return None

    fake_alembic = types.ModuleType("alembic")
    fake_alembic.op = _MockOp()  # type: ignore[attr-defined]
    sys.modules["alembic"] = fake_alembic

    mod = _load_module_from_path("init_revision", ALEMBIC_INIT)
    mod.upgrade()

    metadata = MetaData()
    table = Table("chunks_meta", metadata, *captured_columns, **captured_table_kwargs)

    for idx in captured_indexes:
        cols = [table.c[c] for c in idx["columns"]]
        Index(idx["name"], *cols, unique=idx["unique"])

    return table


def describe_column(c: Column) -> dict[str, Any]:
    return {
        "type": str(c.type),
        "nullable": bool(c.nullable),
        "primary_key": bool(c.primary_key),
        "autoincrement": (
            c.autoincrement if c.autoincrement is not True else "auto"
        ),
        "index": bool(c.index),
        "unique": bool(c.unique),
        "client_default": (
            str(getattr(c.default, "arg", c.default))
            if c.default is not None
            else None
        ),
        "server_default": (
            str(c.server_default.arg) if c.server_default is not None else None  # type: ignore[union-attr]
        ),
    }


def describe_constraint(c: Any) -> str:
    if isinstance(c, sa.UniqueConstraint):
        cols = ",".join(col.name for col in c.columns)
        return f"UNIQUE({cols})"
    if isinstance(c, sa.PrimaryKeyConstraint):
        cols = ",".join(col.name for col in c.columns)
        return f"PRIMARY KEY({cols})"
    if isinstance(c, sa.ForeignKeyConstraint):
        cols = ",".join(c.column_keys)
        targets = ",".join(str(fk.target_fullname) for fk in c.elements)
        return f"FOREIGN KEY({cols}) -> {targets}"
    if isinstance(c, sa.CheckConstraint):
        return f"CHECK({c.sqltext})"
    return type(c).__name__


def diff_tables(
    a: Table, b: Table, *, a_name: str, b_name: str
) -> tuple[list[str], dict[str, int]]:
    lines: list[str] = []
    errors = 0
    warnings = 0

    a_cols = {c.name: describe_column(c) for c in a.columns}
    b_cols = {c.name: describe_column(c) for c in b.columns}
    only_a = sorted(set(a_cols) - set(b_cols))
    only_b = sorted(set(b_cols) - set(a_cols))
    common = sorted(set(a_cols) & set(b_cols))

    lines.append("## 1. 列集合\n")
    if only_a or only_b:
        errors += 1
        lines.append(f"- 仅 **{a_name}** 有：{only_a or '∅'}")
        lines.append(f"- 仅 **{b_name}** 有：{only_b or '∅'}")
        lines.append("- 状态：❌ 列集合不一致")
    else:
        lines.append(f"- ✅ 双方列名完全一致（共 {len(common)} 列）")
    lines.append("")

    lines.append("## 2. 各列字段比对\n")
    # `index` / `unique` 是 Column 上的快捷写法标志（Column(..., index=True/unique=True)），
    # 与 alembic 用独立 `op.create_index` / `UniqueConstraint` 的写法等价，但 Column 属性会
    # 不同。最终事实由 §3（约束）与 §4（索引按列覆盖去重）判定，此处不再重复对比，避免误报。
    hard_keys = ("type", "nullable", "primary_key", "autoincrement")
    soft_keys = ("client_default", "server_default")
    col_diffs: list[str] = []
    for col in common:
        a_desc = a_cols[col]
        b_desc = b_cols[col]
        for key in hard_keys:
            if a_desc[key] != b_desc[key]:
                col_diffs.append(
                    f"| `{col}` | {key} | `{a_desc[key]}` | `{b_desc[key]}` | ❌ |"
                )
                errors += 1
        for key in soft_keys:
            if a_desc[key] != b_desc[key]:
                col_diffs.append(
                    f"| `{col}` | {key} | `{a_desc[key]}` | `{b_desc[key]}` | ⚠️ |"
                )
                warnings += 1

    if col_diffs:
        lines.append(f"| 列名 | 字段 | {a_name} | {b_name} | 状态 |")
        lines.append("|---|---|---|---|---|")
        lines.extend(col_diffs)
    else:
        lines.append(
            "- ✅ 共有列在硬字段（type / nullable / PK / index / unique / autoincrement）上完全一致；默认值若有差异已折叠"
        )
    lines.append("")

    a_cons = sorted(describe_constraint(c) for c in a.constraints)
    b_cons = sorted(describe_constraint(c) for c in b.constraints)
    a_set = set(a_cons)
    b_set = set(b_cons)
    only_a_cons = sorted(a_set - b_set)
    only_b_cons = sorted(b_set - a_set)

    lines.append("## 3. 约束（PK / UNIQUE / FK）\n")
    if only_a_cons or only_b_cons:
        has_fk_or_uq_diff = any(
            ("FOREIGN KEY" in x or "UNIQUE" in x)
            for x in only_a_cons + only_b_cons
        )
        if has_fk_or_uq_diff:
            errors += 1
            tag = "❌"
        else:
            warnings += 1
            tag = "⚠️"
        lines.append(f"- 仅 **{a_name}**：{only_a_cons or '∅'}")
        lines.append(f"- 仅 **{b_name}**：{only_b_cons or '∅'}")
        lines.append(f"- 状态：{tag}（PK 命名不同不影响业务，UNIQUE / FK 差异需修复）")
    else:
        lines.append(f"- ✅ 双方约束完全一致：{a_cons}")
    lines.append("")

    a_idx_by_cols = {
        tuple(sorted(c.name for c in idx.columns)): (idx.name, idx.unique)
        for idx in a.indexes
    }
    b_idx_by_cols = {
        tuple(sorted(c.name for c in idx.columns)): (idx.name, idx.unique)
        for idx in b.indexes
    }
    only_a_cov = sorted(set(a_idx_by_cols) - set(b_idx_by_cols))
    only_b_cov = sorted(set(b_idx_by_cols) - set(a_idx_by_cols))

    lines.append("## 4. 索引（按列覆盖去重，忽略名字）\n")
    if only_a_cov or only_b_cov:
        errors += 1
        lines.append(f"- 仅 **{a_name}** 覆盖：{only_a_cov or '∅'}")
        lines.append(f"- 仅 **{b_name}** 覆盖：{only_b_cov or '∅'}")
        lines.append("- 状态：❌ 索引列覆盖不一致")
    else:
        name_diffs: list[str] = []
        for cols in sorted(a_idx_by_cols):
            an, _ = a_idx_by_cols[cols]
            bn, _ = b_idx_by_cols[cols]
            if an != bn:
                name_diffs.append(
                    f"`({','.join(cols)})`：{a_name}={an} vs {b_name}={bn}"
                )
        if name_diffs:
            warnings += len(name_diffs)
            lines.append(
                "- ⚠️ 列覆盖一致，仅索引名差异（PG 上不影响查询计划）："
            )
            for d in name_diffs:
                lines.append(f"  - {d}")
        else:
            lines.append(
                f"- ✅ 双方索引（列覆盖 + 名字）完全一致（共 {len(a_idx_by_cols)} 个）"
            )
    lines.append("")

    return lines, {"errors": errors, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="将报告写入此 Markdown 文件（推荐写到 docs/04-handoff/）",
    )
    args = parser.parse_args()

    a = load_ingestion_table()
    b = load_alembic_table()

    lines: list[str] = []
    lines.append("# `chunks_meta` schema diff 报告（一次性，不入 CI）")
    lines.append("")
    lines.append(
        "- **A = `ingestion.indexer.pg_writer.chunks_meta_table`**（运行时写入侧；"
    )
    lines.append("  生产 PG 中现有 394,859 行数据均按这套 schema 入库）")
    lines.append(f"- **B = `backend/alembic/versions/{ALEMBIC_INIT.name}`**")
    lines.append("  （alembic init 期望侧；仅在干净 PG 上跑过 upgrade head）")
    lines.append("")
    lines.append("差异分级：")
    lines.append(
        "- ❌ 硬差异：影响业务 / 数据兼容 → 触发 CLAUDE.md §5.1 上报，不进 M4.6"
    )
    lines.append(
        "- ⚠️ 软差异：默认值 / PK 名 / 索引名 / autoincrement 元数据 → 不影响业务，登记即可"
    )
    lines.append("")

    body, stats = diff_tables(a, b, a_name="ingestion", b_name="alembic")
    lines.extend(body)

    lines.append("---")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    if stats["errors"] == 0:
        lines.append(
            f"- ✅ 关键 schema 一致（0 ❌，{stats['warnings']} ⚠️）"
        )
        lines.append(
            "- 已有 ingestion 数据的 PG 上沿用 alembic 接管**不会丢字段、不会改类型**"
        )
        lines.append("- 软差异（如有）仅影响首次写入的默认值或元数据命名，不动业务")
        lines.append("- 归档完毕；M4.6 可以放心启动")
    else:
        lines.append(
            f"- ❌ 发现 {stats['errors']} 项硬差异，{stats['warnings']} 项软差异"
        )
        lines.append(
            "- **停下！按 CLAUDE.md §5.1 上报；M4.6 不要启动**"
        )

    text = "\n".join(lines) + "\n"
    print(text)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"\n[ok] 报告已写到 {args.output}", file=sys.stderr)

    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
