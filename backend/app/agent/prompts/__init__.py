"""Prompt 库（jinja2 模板 + frontmatter 版本号）。

公约：
- 每个 `*.md` 顶部用 `--- ... ---` YAML frontmatter 标 `version` / `notes`
- 模板正文用 jinja2 占位（`{{ var }}` / `{% for ... %}`）
- 加载入口 `render(name, **vars)`，找不到模板抛 `FileNotFoundError`
"""

from ._loader import PROMPT_DIR, list_prompts, load_prompt, render

__all__ = ["PROMPT_DIR", "list_prompts", "load_prompt", "render"]
