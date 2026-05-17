"""Prompt 库渲染：所有模板必须能用最小变量集渲染、含 frontmatter 的版本号。"""

from __future__ import annotations

import pytest
import yaml

from app.agent.prompts import PROMPT_DIR, list_prompts, load_prompt, render

_MIN_VARS = {
    "classify": {"user_input": "What is AMF?"},
    "rewrite": {"user_input": "AMF 是什么"},
    "hyde": {"user_input": "Describe 5G registration procedure"},
    "multi_query": {"rewritten_query": "5G registration procedure"},
    "generate_qa": {
        "chunks": [
            {
                "spec_id": "23.501",
                "section_path": ["6", "3", "1"],
                "section_title": "AMF",
                "content": "AMF stands for Access and Mobility Management Function.",
            }
        ],
        "user_input": "What is AMF?",
        "user_language": "en",
    },
    "self_rag": {
        "chunks": [
            {
                "spec_id": "23.501",
                "section_path": ["6", "3", "1"],
                "content": "AMF stands for Access and Mobility Management Function.",
            }
        ],
        "answer": "AMF is the Access and Mobility Management Function.",
        "user_input": "What is AMF?",
    },
}


def test_six_prompts_present() -> None:
    names = list_prompts()
    expected = {"classify", "rewrite", "hyde", "multi_query", "generate_qa", "self_rag"}
    assert expected.issubset(set(names)), f"missing prompts: {expected - set(names)}"


@pytest.mark.parametrize(
    "name,vars_",
    list(_MIN_VARS.items()),
)
def test_render_does_not_raise(name: str, vars_: dict) -> None:
    text = render(name, **vars_)
    assert text.strip(), f"{name} rendered empty"
    # frontmatter 应该被剥掉
    assert not text.startswith("---")


def test_frontmatter_has_version() -> None:
    for name in _MIN_VARS:
        meta, _body = load_prompt(name)
        assert "version" in meta, f"{name} missing version frontmatter"


def test_each_prompt_has_yaml_frontmatter_on_disk() -> None:
    for path in sorted(PROMPT_DIR.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        assert raw.startswith("---\n"), f"{path.name} missing frontmatter opener"
        parts = raw.split("---", 2)
        assert len(parts) >= 3, f"{path.name} frontmatter not closed"
        # frontmatter 必须是合法 YAML
        yaml.safe_load(parts[1])
