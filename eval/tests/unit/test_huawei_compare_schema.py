"""单测 `eval.huawei_compare.schema` 纯函数：B 引用解析 / raw→SystemAnswer / align / JSONL IO。"""

from __future__ import annotations

import pytest

from eval.huawei_compare.schema import (
    SYSTEM_A,
    SYSTEM_B,
    SystemAnswer,
    align,
    b_raw_to_answer,
    dump_jsonl,
    load_questions,
    parse_b_cited_specs,
)

_B_RETRIEVAL = (
    "Retrieval 1:\n...PDU session text...\n"
    "This retrieval is performed from the document 3GPP 23501-i20.docx.\n\n"
    "Retrieval 2:\n...more...\n"
    "This retrieval is performed from the document 3GPP 29272-i20.docx.\n\n"
    "Retrieval 3:\n...dup...\n"
    "This retrieval is performed from the document 3GPP 23501-i20.docx.\n"
)


@pytest.mark.unit
def test_parse_b_cited_specs_dedup_order() -> None:
    assert parse_b_cited_specs(_B_RETRIEVAL) == ["23.501", "29.272"]
    assert parse_b_cited_specs("") == []
    assert parse_b_cited_specs("no doc marker here") == []


@pytest.mark.unit
def test_b_raw_to_answer() -> None:
    raw = {
        "item_id": "x-1",
        "question": "Q?",
        "answer": "B answer (Retrieval 1)",
        "retrieval_raw": _B_RETRIEVAL,
        "rephrased_query": "concise Q",
        "elapsed_ms": 31000,
        "error": None,
        "model": "gpt-4o-mini",
    }
    a = b_raw_to_answer(raw)
    assert a.system == SYSTEM_B
    assert a.item_id == "x-1" and a.ok
    assert a.cited_specs == ["23.501", "29.272"]
    assert len(a.contexts) == 3  # 按空行切成 3 段
    assert a.meta["rephrased_query"] == "concise Q"


@pytest.mark.unit
def test_b_raw_empty_answer_marked_error() -> None:
    a = b_raw_to_answer({"item_id": "x", "question": "q", "answer": "", "retrieval_raw": ""})
    assert not a.ok and a.error is not None and a.error["type"] == "EmptyAnswer"


@pytest.mark.unit
def test_align_union_order_and_missing() -> None:
    a_recs = [
        SystemAnswer(item_id="i1", question="q1", system=SYSTEM_A, answer="a1").to_dict(),
        SystemAnswer(item_id="i2", question="q2", system=SYSTEM_A, answer="a2").to_dict(),
    ]
    b_recs = [
        SystemAnswer(item_id="i2", question="q2", system=SYSTEM_B, answer="b2").to_dict(),
        SystemAnswer(item_id="i3", question="q3", system=SYSTEM_B, answer="b3").to_dict(),
    ]
    res = align(a_recs, b_recs)
    assert res["n_items"] == 3
    assert [it["item_id"] for it in res["items"]] == ["i1", "i2", "i3"]  # 并集保序
    i1, i2, i3 = res["items"]
    assert i1["A"] is not None and i1["B"] is None  # A-only
    assert i2["A"]["answer"] == "a2" and i2["B"]["answer"] == "b2"  # both
    assert i3["A"] is None and i3["B"] is not None  # B-only


@pytest.mark.unit
def test_system_answer_roundtrip() -> None:
    a = SystemAnswer(
        item_id="i",
        question="q",
        system=SYSTEM_A,
        answer="x",
        contexts=["c1"],
        cited_specs=["23.501"],
        elapsed_ms=10,
    )
    assert SystemAnswer.from_dict(a.to_dict()) == a


@pytest.mark.unit
def test_load_questions_skips_incomplete(tmp_path) -> None:
    p = tmp_path / "q.jsonl"
    dump_jsonl(
        [
            {"item_id": "a", "question": "qa"},
            {"item_id": "", "question": "no id"},  # 跳过
            {"item_id": "b", "question": ""},  # 跳过
            {"item_id": "c", "question": "qc", "extra": 1},
        ],
        p,
    )
    qs = load_questions(p)
    assert [q["item_id"] for q in qs] == ["a", "c"]
