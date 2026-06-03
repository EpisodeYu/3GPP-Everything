"""单测评测层纯函数：3 路 align / 检索专项指标 / 位置对冲聚合 / C spec 解析 /
报告聚合 / golden→questions。不触网（judge 的 LLM 调用靠 smoke 覆盖）。
"""

from __future__ import annotations

import pytest

from eval.huawei_compare import compare_eval as E
from eval.huawei_compare import compare_report as R
from eval.huawei_compare.collect_c import parse_c_cited_specs
from eval.huawei_compare.pairwise_judge import aggregate_pair
from eval.huawei_compare.schema import align, align_systems

pytestmark = pytest.mark.unit


# === 3 路 align =============================================================


def _sa(iid: str, sys: str, q: str = "q") -> dict:
    return {"item_id": iid, "system": sys, "question": q, "answer": f"{sys}-ans"}


def test_align_systems_three_way_union() -> None:
    a = [_sa("1", "A"), _sa("2", "A")]
    b = [_sa("1", "B")]
    c = [_sa("2", "C"), _sa("3", "C")]
    res = align_systems({"A": a, "B": b, "C": c})
    assert res["n_items"] == 3  # 1,2,3 并集
    by = {it["item_id"]: it for it in res["items"]}
    assert by["1"]["A"] and by["1"]["B"] and by["1"]["C"] is None
    assert by["2"]["A"] and by["2"]["B"] is None and by["2"]["C"]
    assert by["3"]["A"] is None and by["3"]["C"]
    # question 从任一非空系统取
    assert by["3"]["question"] == "q"


def test_align_backward_compat_two_way() -> None:
    res = align([_sa("1", "A")], [_sa("1", "B")])
    assert set(res["items"][0]) == {"item_id", "question", "A", "B"}


# === 检索专项（LLM-free）====================================================


def test_fact_in_context_recall_partial() -> None:
    facts = ["PDU Session logical connection", "SMF anchor", "ambient backscatter modulation"]
    contexts = ["The PDU Session is a logical connection anchored at the SMF."]
    cov = E.fact_in_context_recall(facts, contexts)
    assert cov == pytest.approx(2 / 3)  # 前两条命中,第三条没有


def test_fact_in_context_recall_none_when_no_context_or_facts() -> None:
    assert E.fact_in_context_recall(["x y z w"], []) is None  # 无检索 context(如 C)
    assert E.fact_in_context_recall([], ["anything"]) is None  # 无 expected_facts


def test_spec_recall() -> None:
    assert E.spec_recall(["23.501"], ["23.501", "29.502"]) is True
    assert E.spec_recall(["23.501"], ["38.331"]) is False
    assert E.spec_recall([], ["23.501"]) is None


# === 位置对冲聚合 ===========================================================


@pytest.mark.parametrize(
    "v_ab,v_ba,expect",
    [
        ("X", "Y", "1"),  # 两序都判系统1 好
        ("Y", "X", "2"),  # 两序都判系统2 好
        ("X", "X", "TIE"),  # 判官只挑甲 → 位置偏好被对冲掉
        ("Y", "Y", "TIE"),
        ("TIE", "TIE", "TIE"),
        ("X", "TIE", "1"),  # 一序赢一序平
        (None, "X", "2"),  # 一序失败按 TIE,另一序系统2(甲)赢
    ],
)
def test_aggregate_pair(v_ab: str | None, v_ba: str | None, expect: str) -> None:
    assert aggregate_pair(v_ab, v_ba) == expect


# === C spec 解析 ============================================================


def test_parse_c_cited_specs_spec_line() -> None:
    assert parse_c_cited_specs("SPEC: TS 23.501 clause 5.6\nANSWER: ...") == ["23.501"]


def test_parse_c_cited_specs_fallback_and_empty() -> None:
    assert parse_c_cited_specs("Per TS 38.331 the RRCReconfiguration ...") == ["38.331"]
    assert parse_c_cited_specs("This is not specified by 3GPP.") == []


# === 报告聚合 ===============================================================


def _scores_fixture() -> dict:
    def cell(present=True, fc=None, sr=None, ficr=None, util=None, nv=None, ok=True):
        c = {"present": present, "ok": ok, "spec_recall": sr}
        if fc is not None:
            c["fact_coverage"] = fc
        if ficr is not None:
            c["fact_in_context_recall"] = ficr
        if util is not None:
            c["utilization"] = util
        if nv is not None:
            c["negative_verdict"] = nv
        return c

    return {
        "systems": ["A", "C"],
        "judge_model": "glm-5.1",
        "items": [
            {  # 核心 series 正题
                "item_id": "p1",
                "category": "definition",
                "question": "q1",
                "expected_specs": ["23.501"],
                "per_system": {
                    "A": cell(fc=1.0, sr=True, ficr=1.0, util=1.0),
                    "C": cell(fc=0.5, sr=False),
                },
            },
            {  # 长尾 series 正题
                "item_id": "p2",
                "category": "table_lookup",
                "question": "q2",
                "expected_specs": ["28.554"],
                "per_system": {
                    "A": cell(fc=0.8, sr=True, ficr=0.9, util=0.89),
                    "C": cell(fc=0.2, sr=False),
                },
            },
            {  # 负题
                "item_id": "n1",
                "category": "negative",
                "question": "q3",
                "expected_specs": [],
                "per_system": {
                    "A": cell(nv="VALID_REFUSAL"),
                    "C": cell(nv="INVALID"),
                },
            },
        ],
        "pairwise": {
            "A_vs_C": [
                {"item_id": "p1", "winner": "A"},
                {"item_id": "p2", "winner": "A"},
                {"item_id": "n1", "winner": "TIE"},
            ]
        },
    }


def test_summarize_scorecard_and_headline() -> None:
    s = R.summarize(_scores_fixture())
    assert s["n_positive"] == 2 and s["n_negative"] == 1
    # A 正题 fact_coverage 均值 = (1.0+0.8)/2
    assert s["per_system"]["A"]["fact_coverage"] == pytest.approx(0.9)
    # A spec 命中 2/2;C 0/2
    assert s["per_system"]["A"]["spec_recall"].startswith("2/2")
    assert s["per_system"]["C"]["spec_recall"].startswith("0/2")
    # 幻觉率:C 负题 INVALID 1/1;A 0/1
    assert s["per_system"]["C"]["neg_invalid_halluc"].startswith("1/1")
    assert s["per_system"]["A"]["neg_valid_refusal"].startswith("1/1")
    # C 无检索 recall → None
    assert s["per_system"]["C"]["fact_in_context_recall"] is None
    # 头条:核心 series p1(23) vs 长尾 p2(28)
    assert s["headline"]["core"]["A"]["fact_coverage"] == pytest.approx(1.0)
    assert s["headline"]["tail"]["A"]["fact_coverage"] == pytest.approx(0.8)


def test_summarize_pairwise_counts() -> None:
    s = R.summarize(_scores_fixture())
    p = s["pairwise"]["A_vs_C"]
    assert p["w1"] == 2 and p["w2"] == 0 and p["tie"] == 1 and p["n"] == 3


def test_render_markdown_smoke() -> None:
    md = R.render_markdown(_scores_fixture())
    assert "# 华为对比测试报告" in md
    assert "Scorecard" in md and "成对盲评" in md and "头条" in md
    assert "glm-5.1" in md
