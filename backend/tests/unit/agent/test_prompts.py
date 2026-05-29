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


def test_generate_qa_v6_chunks_are_numbered_with_index_prefix() -> None:
    """v6 索引引用：每个 chunk 在 prompt 里以 `[N]` 1-based 前缀渲染，N 与
    `parse_citations`/前端 `CitationInlineSyntax` 反查 citationsByRank 对齐。"""
    text = render(
        "generate_qa",
        chunks=[
            {
                "spec_id": "38.331",
                "section_path": [],
                "section_title": "PUCCH-Config IE",
                "content": "-- ASN1START ...",
            },
            {
                "spec_id": "23.501",
                "section_path": ["6", "3", "1"],
                "section_title": "AMF",
                "content": "AMF stands for ...",
            },
        ],
        user_input="What is PUCCH-Config?",
        user_language="en",
    )
    # 索引前缀按 loop.index（1-based）注入
    assert "[1] spec_id=38.331" in text
    assert "[2] spec_id=23.501" in text
    # 空 section_path 仍以 `<none>` 显式标，避免渲染成裸空串
    assert "section_path=<none>" in text
    assert "section_path=6.3.1" in text


def test_generate_qa_v6_rule2_uses_index_citation_format() -> None:
    """v6 rule 2 必须明确要求 `[N]` 索引引用形态，且禁掉 v5 老的 `[spec §section]`、
    中文括号 `［N］`、`(N)` 等漂移形态。"""
    text = render(
        "generate_qa",
        chunks=[
            {
                "spec_id": "23.501",
                "section_path": ["6", "3", "1"],
                "section_title": "AMF",
                "content": "AMF stands for ...",
            }
        ],
        user_input="X",
        user_language="en",
    )
    # 核心约束：`[N]` 形态描述出现
    assert "[N]" in text
    assert "1-based" in text
    # 防漂移护栏（不锁死具体措辞，关键字命中即可）
    assert "[spec_id §section]" in text  # 明确禁项之一
    assert "[chunk 1]" in text  # 明确禁项之一


def test_generate_qa_v6_version_bumped() -> None:
    meta, _ = load_prompt("generate_qa")
    assert meta.get("version", 0) >= 6, "generate_qa prompt 应该至少在 v6（索引引用）"
