"""Prompt 模板加载（jinja2 + YAML frontmatter）。

- `load_prompt(name)` 返回 `(metadata, template_str)`；name 不带 `.md` 后缀
- `render(name, **vars)` 直接得到渲染后的字符串
- jinja2 配置：`StrictUndefined`，渲染时少传一个变量直接抛错（被
  `test_prompts_render_without_undefined_vars` 单测捕获）
- 模板默认放在与本文件同目录的 `prompts/`；子目录用 `tools/web_search_prefix`
  这样的路径形式
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from jinja2 import Environment, FileSystemLoader, StrictUndefined

PROMPT_DIR = Path(__file__).parent

_ENV = Environment(
    loader=FileSystemLoader(str(PROMPT_DIR)),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    autoescape=False,
)


@lru_cache(maxsize=64)
def load_prompt(name: str) -> tuple[dict[str, Any], str]:
    """读 markdown，拆 YAML frontmatter 与模板正文。"""
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt not found: {path}")
    raw = path.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(raw)
    return meta, body


def render(name: str, **vars: Any) -> str:
    """渲染 prompt；若 frontmatter 注释 `lstrip=true` 则左侧空白被收掉。"""
    _meta, _body = load_prompt(name)
    template = _ENV.get_template(f"{name}.md")
    rendered = template.render(**vars)
    return _strip_frontmatter(rendered).strip() + "\n"


def list_prompts() -> list[str]:
    """枚举可用的 prompt 名（递归）。"""
    out: list[str] = []
    for p in sorted(PROMPT_DIR.rglob("*.md")):
        if p.name.startswith("_"):
            continue
        rel = p.relative_to(PROMPT_DIR).with_suffix("")
        out.append(rel.as_posix())
    return out


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    meta = yaml.safe_load(parts[1]) or {}
    return meta, parts[2].lstrip("\n")


def _strip_frontmatter(rendered: str) -> str:
    """jinja 渲染会保留 frontmatter 三横线区，调用方拿到的是渲染后正文，需要再剥一次。"""
    if not rendered.startswith("---"):
        return rendered
    parts = rendered.split("---", 2)
    if len(parts) < 3:
        return rendered
    return parts[2].lstrip("\n")
