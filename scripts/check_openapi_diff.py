#!/usr/bin/env python3
"""比对后端 openapi.json schemas 与前端 Dart client fromJson 字段集。

M5.6 docs 05-frontend.md §13 / §14 [auto] 项：手写 Dart client 替代 codegen 的
代价是 "schema 漂移由 CI 监控"。本脚本是 CI 兜底。

判定规则：
    - 对每个共享 schema name（OpenAPI components.schemas 里出现 + Dart 里也有
      `factory <Name>.fromJson(...)`），比对字段集。
    - 后端有、前端缺 → ERROR（前端读不到新字段）。
    - 后端 required 字段中、前端有但类型不匹配 → 暂不深查（手写 client 不强类型
      绑定，超出本脚本范围；human review 把关）。
    - 前端有、后端无 → WARNING（可能是手写 alias / 前端遗留字段）。
    - 任一 ERROR → exit 1。

用法：
    python scripts/check_openapi_diff.py
    python scripts/check_openapi_diff.py --openapi-url http://localhost:8002/openapi.json
    python scripts/check_openapi_diff.py --openapi-file /tmp/openapi.json

环境：
    默认 `--openapi-source app` 直接 import `backend.app.main:app` 拿 schemas，
    需要后端依赖装好（`cd backend && uv sync --extra dev`）。CI 走这条路径。

设计取舍：
    - 不强行覆盖 SendMessageBody / IndexRebuildBody 这类只输入不输出的 body
      schema（前端无 fromJson，无法对照）。
    - 不解析 oneOf / allOf / $ref 嵌套 schema 内字段差异（手写 Dart 也是平铺
      解析）；只比顶层 properties keys。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_API_DIR = ROOT / "frontend" / "lib" / "data" / "api"

# 前端 Dart 类名 ↔ 后端 OpenAPI schema 名的命名等价表。
# 加在这里的对子在 diff 时会用 backend value 端的字段集做对照。
# 出现新的 alias 时同步更新本表，避免 Dart 端为追求与 OpenAPI 命名一致就改一堆引用。
DART_TO_BACKEND_ALIAS: dict[str, str] = {
    "Me": "MeResponse",
}

# Dart fromJson 解析正则：
#   factory <ClassName>.fromJson(Map<String, dynamic> <param>) {... <param>['<field>'] ...}
#   factory <ClassName>.fromJson(Map<String, dynamic> <param>) => <ClassName>(... <param>['<field>'] ...);
#
# 先抓 `factory <Class>.fromJson(<Map ... param>) (=>|{)`，捕获 param 名，
# 然后用动态拼接的 RE 匹配 `<param>['<field>']` 提取字段集。block 截到最近的
# `\n);` 或 `\n  }` 收尾。
RE_FACTORY = re.compile(
    r"factory\s+(\w+)\.fromJson\s*"
    r"\(\s*Map<[^>]+>\s+(\w+)\s*\)\s*"
    r"(?:=>|\{)"
    r"([\s\S]*?)"
    r"(?:\)\s*;|\}\s*\n)",
)


def load_openapi_from_app() -> dict[str, Any]:
    """import backend.app.main:app 并调 app.openapi()。"""
    sys.path.insert(0, str(ROOT / "backend"))
    try:
        from app.main import app  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - import error 在 CI/dev 双场景都直白
        raise SystemExit(
            f"无法 import backend.app.main:app（请先 `cd backend && uv sync --extra dev`）：{exc}"
        ) from exc
    return app.openapi()


def load_openapi_from_url(url: str) -> dict[str, Any]:
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def load_openapi_from_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def collect_backend_schemas(openapi: dict[str, Any]) -> dict[str, set[str]]:
    """提取 components.schemas[name].properties keys。"""
    out: dict[str, set[str]] = {}
    schemas = openapi.get("components", {}).get("schemas", {})
    for name, schema in schemas.items():
        props = schema.get("properties")
        if not isinstance(props, dict):
            continue
        out[name] = set(props.keys())
    return out


def collect_frontend_factories(api_dir: Path) -> dict[str, dict[str, Any]]:
    """扫描 *.dart 中的 factory <Name>.fromJson 块，提取 json[...] key 集合。

    Returns: { class_name: { 'fields': set[str], 'file': Path } }
    """
    out: dict[str, dict[str, Any]] = {}
    for dart in sorted(api_dir.glob("*.dart")):
        text = dart.read_text(encoding="utf-8")
        for m in RE_FACTORY.finditer(text):
            cls = m.group(1)
            param = m.group(2)
            body = m.group(3)
            key_re = re.compile(
                rf"\b{re.escape(param)}\[\s*['\"]([^'\"]+)['\"]\s*\]"
            )
            keys = set(key_re.findall(body))
            if not keys:
                continue
            if cls in out:
                out[cls]["fields"] |= keys
            else:
                out[cls] = {"fields": keys, "file": dart.relative_to(ROOT)}
    return out


def diff(
    backend: dict[str, set[str]],
    frontend: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for cls_name, fe in sorted(frontend.items()):
        backend_name = DART_TO_BACKEND_ALIAS.get(cls_name, cls_name)
        be_fields = backend.get(backend_name)
        if be_fields is None:
            warnings.append(
                f"[WARN] Dart 类 `{cls_name}` ({fe['file']}) 在 OpenAPI schemas 里找不到 — "
                "可能是前端手写聚合 / 已废弃 schema"
            )
            continue
        fe_fields: set[str] = fe["fields"]
        missing_in_fe = be_fields - fe_fields
        extra_in_fe = fe_fields - be_fields
        if missing_in_fe:
            errors.append(
                f"[ERROR] `{cls_name}` 后端有但前端 fromJson 漏读："
                f"{sorted(missing_in_fe)}（{fe['file']}）"
            )
        if extra_in_fe:
            warnings.append(
                f"[WARN] `{cls_name}` 前端 fromJson 读了 OpenAPI 没有的字段："
                f"{sorted(extra_in_fe)}（{fe['file']}）"
            )
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--openapi-source",
        choices=["app", "url", "file"],
        default="app",
        help="default: app（import backend.app.main:app）",
    )
    parser.add_argument("--openapi-url", default="http://localhost:8002/openapi.json")
    parser.add_argument("--openapi-file", type=Path)
    parser.add_argument(
        "--api-dir",
        type=Path,
        default=FRONTEND_API_DIR,
        help=f"default: {FRONTEND_API_DIR.relative_to(ROOT)}",
    )
    args = parser.parse_args()

    if args.openapi_source == "app":
        openapi = load_openapi_from_app()
    elif args.openapi_source == "url":
        openapi = load_openapi_from_url(args.openapi_url)
    else:
        if not args.openapi_file:
            parser.error("--openapi-source file 需要 --openapi-file")
        openapi = load_openapi_from_file(args.openapi_file)

    backend = collect_backend_schemas(openapi)
    frontend = collect_frontend_factories(args.api_dir)

    print(
        f"[scan] backend schemas: {len(backend)}; "
        f"frontend factories: {len(frontend)}",
        file=sys.stderr,
    )

    errors, warnings = diff(backend, frontend)
    for w in warnings:
        print(w)
    for e in errors:
        print(e)

    if errors:
        print(
            f"\nFAIL：{len(errors)} 个 schema 漂移。手写 Dart client 必须同步 "
            "（详见 docs/03-development/05-frontend.md §13）。",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK：{len(frontend)} 个共享 schema 字段对齐"
        f"（{len(warnings)} warning 不影响通过）。",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
