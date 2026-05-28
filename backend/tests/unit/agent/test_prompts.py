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


def test_generate_qa_v5_marks_empty_section_path_as_none() -> None:
    """v5 prompt：chunk 空 section_path 时，元数据行必须显示 `<none>` 而不是裸空串；
    避免 LLM 看到 `section_path=` 后自行把 chunk body 里的 header 抄成 citation。"""
    text = render(
        "generate_qa",
        chunks=[
            {
                "spec_id": "38.331",
                "section_path": [],  # IE chunk: 无 clause
                "section_title": "*ControlResourceSet* information element",
                "content": (
                    "[38.331 § *ControlResourceSet* information element]\n\n" "-- ASN1START ..."
                ),
            },
            {
                "spec_id": "23.501",
                "section_path": ["6", "3", "1"],
                "section_title": "AMF",
                "content": "AMF stands for ...",
            },
        ],
        user_input="What is ControlResourceSet?",
        user_language="en",
    )
    # 空 section_path → `<none>`
    assert "section_path=<none>" in text
    # 非空仍是 dotted clause
    assert "section_path=6.3.1" in text


def test_generate_qa_v5_includes_chunk_header_antiwarning() -> None:
    """v5 prompt 必须显式提示 chunk body 第一行的 `[spec § title]` 是 chunker artifact，
    不能 verbatim 当 citation 抄。"""
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
    # 关键词出现即可（不锁死全文），避免 prompt 微调每次都要改测试
    assert "chunker artifact" in text or "chunker-artifact" in text
    assert "<none>" in text  # 规则 #2 的 `<none>` 显式 fallback 说明


def test_generate_qa_v5_version_bumped() -> None:
    meta, _ = load_prompt("generate_qa")
    assert meta.get("version", 0) >= 5, "generate_qa prompt 应该至少在 v5"
