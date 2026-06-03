"""单测 `eval.huawei_compare.gen_questions` 纯函数：

采样计划 / chunk 过滤与正文剥离 / multi_section 取段 / LLM-JSON 校验 / R18 覆盖核验 /
选 100 平衡 / id 分配。不触网（生成主流程是 async + HTTP，靠 collect 端到端冒烟覆盖）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from eval.huawei_compare import gen_questions as G

pytestmark = pytest.mark.unit


# === 采样计划 =============================================================


def test_plan_slots_counts_and_marginals() -> None:
    targets = {"definition": 10, "formula": 5}
    quota = {"23": 3, "38": 1}
    slots = G.plan_slots(targets, quota, oversample=1.0)
    # 总数 = sum(targets)
    assert len(slots) == 15
    # 每 category 数量正确
    by_cat: dict[str, int] = {}
    for c, _ in slots:
        by_cat[c] = by_cat.get(c, 0) + 1
    assert by_cat == {"definition": 10, "formula": 5}
    # series 占比按 3:1 → definition 10 题 ≈ 7-8 / 2-3
    series = [s for c, s in slots if c == "definition"]
    assert series.count("23") >= series.count("38")


def test_plan_slots_oversample_rounds_up() -> None:
    slots = G.plan_slots({"definition": 10}, {"23": 1}, oversample=1.4)
    assert len(slots) == 14  # round(10*1.4)


def test_is_excluded_spec_filters_test_rf_specs() -> None:
    # 多部件 -N = 测试/一致性/study
    assert G.is_excluded_spec("38.521-3")
    assert G.is_excluded_spec("36.521-2")
    assert G.is_excluded_spec("23.700-81")
    # 单部件 EMC/一致性
    assert G.is_excluded_spec("38.113")
    assert G.is_excluded_spec("37.141")
    # 核心知识 spec 不排
    assert not G.is_excluded_spec("23.501")
    assert not G.is_excluded_spec("38.331")
    assert not G.is_excluded_spec("38.214")


# === chunk 正文 / 过滤 =====================================================


def test_chunk_text_strips_header() -> None:
    c = {"content": "[23.501 § 5.6.1 Overview]\n\nA PDU Session is a logical connection."}
    assert G.chunk_text(c) == "A PDU Session is a logical connection."


def test_is_usable_rejects_boilerplate_and_short() -> None:
    body = "x" * (G.MIN_TEXT_LEN + 10)
    assert G._is_usable({"section_title": "5 Functional description", "content": body}, "text")
    # boilerplate 标题
    assert not G._is_usable({"section_title": "Foreword", "content": body}, "text")
    # 太短
    assert not G._is_usable({"section_title": "Good", "content": "short"}, "text")


def _chunk(cid: str, ctype: str, clause: str, order: int, body_len: int = 300) -> dict:
    return {
        "chunk_id": cid,
        "chunk_type": ctype,
        "clause": clause,
        "section_title": f"sec {clause}",
        "document_order": order,
        "content": f"[23.501 § {clause}]\n\n" + ("word " * body_len),
    }


def test_pick_chunk_respects_type_and_used() -> None:
    import random

    chunks = [_chunk("a", "table", "5.1", 0), _chunk("b", "text", "5.2", 1)]
    picked = G._pick_chunk(chunks, "table", used=set(), rng=random.Random(0))
    assert picked["chunk_id"] == "a"
    # used 排除
    assert G._pick_chunk(chunks, "table", used={"a"}, rng=random.Random(0)) is None
    # 无该类型
    assert G._pick_chunk(chunks, "formula", used=set(), rng=random.Random(0)) is None


def test_multi_section_job_picks_siblings() -> None:
    import random

    chunks = [_chunk(str(i), "text", f"5.{i}", i) for i in range(4)]
    job = G._multi_section_job("23.501", chunks, used=set(), rng=random.Random(1))
    assert job is not None
    assert job.kind == "multi_section"
    assert 2 <= len(job.sections) <= 3
    assert job.spec_id == "23.501"


def test_multi_section_job_needs_two() -> None:
    import random

    chunks = [_chunk("0", "text", "5.0", 0)]
    assert G._multi_section_job("23.501", chunks, used=set(), rng=random.Random(0)) is None


# === LLM JSON 校验 ========================================================

_WL = {"23.501", "38.214"}


def test_validate_positive_ok() -> None:
    parsed = {
        "question": "What is a PDU Session per TS 23.501?",
        "category": "definition",
        "expected_specs": [{"spec_id": "23.501", "sections": ["5.6.1"]}],
        "expected_facts": ["logical connection", "UE", "data network"],
        "forbidden": ["LTE"],
        "notes": "tests defn",
    }
    item, skip = G.validate_and_normalize(parsed, kind="positive", whitelist=_WL)
    assert skip is None
    assert item["category"] == "definition"
    assert item["source"] == "hand_crafted"
    assert item["expected_specs"][0]["spec_id"] == "23.501"
    assert "must_say_not_found" not in item


def test_validate_positive_rejects_out_of_whitelist_spec() -> None:
    parsed = {
        "question": "Q?",
        "category": "definition",
        "expected_specs": [{"spec_id": "99.999", "sections": []}],
        "expected_facts": ["a", "b", "c"],
    }
    item, skip = G.validate_and_normalize(parsed, kind="positive", whitelist=_WL)
    assert item is None and skip == "no-whitelist-spec"


def test_validate_positive_rejects_too_few_facts() -> None:
    parsed = {
        "question": "Q?",
        "category": "formula",
        "expected_specs": [{"spec_id": "38.214", "sections": []}],
        "expected_facts": ["only one"],
    }
    item, skip = G.validate_and_normalize(parsed, kind="positive", whitelist=_WL)
    assert item is None and skip.startswith("facts<")


def test_validate_negative_forces_not_found_and_empty_specs() -> None:
    parsed = {
        "question": "What is the MAC address of a 5G UE PDU Session?",
        "expected_specs": [{"spec_id": "23.501", "sections": []}],  # 应被忽略
        "expected_facts": [],
        "forbidden": ["48-bit", "hexadecimal"],
        "notes": "UE has no MAC address",
    }
    item, skip = G.validate_and_normalize(parsed, kind="false_premise", whitelist=_WL)
    assert skip is None
    assert item["category"] == "negative"
    assert item["expected_specs"] == []
    assert item["must_say_not_found"] is True
    assert item["forbidden"] == ["48-bit", "hexadecimal"]


def test_validate_respects_skip_reason() -> None:
    item, skip = G.validate_and_normalize(
        {"skip_reason": "boilerplate"}, kind="positive", whitelist=_WL
    )
    assert item is None and skip == "boilerplate"


def test_validate_spec_id_normalization() -> None:
    parsed = {
        "question": "Q?",
        "category": "table_lookup",
        "expected_specs": [{"spec_id": "TS 23.501", "sections": ["5.1"]}],
        "expected_facts": ["a", "b", "c"],
    }
    item, _ = G.validate_and_normalize(parsed, kind="positive", whitelist=_WL)
    assert item["expected_specs"][0]["spec_id"] == "23.501"


# === R18 覆盖核验 =========================================================


@pytest.fixture
def b_db(tmp_path: Path) -> Path:
    db = tmp_path / "Documents.db"
    conn = sqlite3.connect(db)
    conn.execute("create table Standard(id text, data text)")
    text = "The PDU Session is a logical connection between the UE and the data network via SMF."
    conn.execute(
        "insert into Standard values(?,?)",
        ("23501-i20.docx", json.dumps({"id": "23501", "text": text, "source": "x"})),
    )
    conn.commit()
    conn.close()
    return db


def test_r18_corpus_coverage_hit(b_db: Path) -> None:
    corpus = G.B_R18_Corpus(b_db)
    assert corpus.available
    cov = corpus.fact_coverage(["logical connection", "data network", "SMF"], "23.501")
    assert cov == pytest.approx(1.0)


def test_r18_corpus_coverage_miss_for_r19_fact(b_db: Path) -> None:
    corpus = G.B_R18_Corpus(b_db)
    cov = corpus.fact_coverage(["ambient backscatter harvesting modulation"], "23.501")
    assert cov == 0.0


def test_r18_corpus_missing_spec_returns_zero(b_db: Path) -> None:
    corpus = G.B_R18_Corpus(b_db)
    assert corpus.fact_coverage(["anything"], "99.999") == 0.0


def test_r18_corpus_empty_facts_is_full(b_db: Path) -> None:
    assert G.B_R18_Corpus(b_db).fact_coverage([], "23.501") == 1.0


# === A 库对称门 ===========================================================


@pytest.fixture
def a_dir(tmp_path: Path) -> Path:
    d = tmp_path / "by_spec"
    d.mkdir()
    # 两篇 spec 都提到 "QUIC"（模拟 3GPP 引用的外部标准）；没篇提 "802.11be"
    for sid, body in [("29.500", "uses QUIC transport"), ("23.501", "QUIC over UDP")]:
        (d / f"{sid}.jsonl").write_text(
            json.dumps({"spec_id": sid, "content": f"[{sid}] {body}"}) + "\n", encoding="utf-8"
        )
    return d


def test_a_corpus_specs_mentioning_counts_files(a_dir: Path) -> None:
    corpus = G.A_Corpus(a_dir, min_specs=2)
    assert corpus.available
    assert corpus.specs_mentioning("QUIC") == 2
    assert corpus.specs_mentioning("802.11be") == 0


def test_a_corpus_substantive_hit_threshold(a_dir: Path) -> None:
    # QUIC 命中 2 >= min_specs 2 → 实质命中
    hit, info = G.A_Corpus(a_dir, min_specs=2).substantive_hit(["802.11be", "QUIC"])
    assert hit and "QUIC" in info
    # 全部域外术语 → 无命中
    hit2, _ = G.A_Corpus(a_dir, min_specs=2).substantive_hit(["802.11be", "route reflector"])
    assert not hit2


def test_a_corpus_short_term_ignored(a_dir: Path) -> None:
    assert G.A_Corpus(a_dir).specs_mentioning("UE") == 0  # <4 字符不查


def test_apply_symmetry_gate_drops_a_corpus_topics(a_dir: Path) -> None:
    # 一道 out_of_scope 探针含 A 库有的 QUIC → 应被剔；一道纯域外 → 保留
    r_bad = _res("negative", "out_of_scope")
    r_bad.probe_terms = ["QUIC"]
    r_good = _res("negative", "out_of_scope")
    r_good.probe_terms = ["802.11be", "EHT MCS"]
    r_fp = _res("negative", "false_premise")
    r_fp.probe_terms = ["NonexistentFooBarIE"]
    dropped = G.apply_symmetry_gate([r_bad, r_good, r_fp], G.A_Corpus(a_dir, min_specs=2))
    assert dropped == 1
    assert r_bad.item is None and r_bad.skip_reason.startswith("a-corpus-has")
    assert r_good.item is not None and r_fp.item is not None


def test_apply_symmetry_gate_skips_positives(a_dir: Path) -> None:
    r_pos = _res("definition", "positive")
    r_pos.probe_terms = ["QUIC"]  # positive 不该被对称门动
    G.apply_symmetry_gate([r_pos], G.A_Corpus(a_dir, min_specs=2))
    assert r_pos.item is not None


# === 选 100 + id =========================================================


def _res(cat: str, kind: str, series: str = "23", cov: float = 1.0) -> G.GenResult:
    item = {
        "category": cat,
        "language": "en",
        "question": f"q-{cat}-{series}-{id(object())}",
        "expected_specs": [] if cat == "negative" else [{"spec_id": "23.501", "sections": []}],
        "expected_facts": [] if cat == "negative" else ["a", "b", "c"],
        "forbidden": [],
        "source": "hand_crafted",
    }
    if cat == "negative":
        item["must_say_not_found"] = True
    job = G.GenJob(kind=kind, category=cat, series=series)
    return G.GenResult(job=job, item=item, r18_coverage=cov)


def test_select_balanced_hits_category_targets() -> None:
    results: list[G.GenResult] = []
    # 超采每类
    for _ in range(40):
        results.append(_res("definition", "positive"))
        results.append(_res("formula", "positive"))
    for _ in range(20):
        results.append(_res("negative", "false_premise"))
        results.append(_res("negative", "out_of_scope"))
    chosen = G.select_balanced(results)
    by_cat: dict[str, int] = {}
    for it in chosen:
        by_cat[it["category"]] = by_cat.get(it["category"], 0) + 1
    assert by_cat["definition"] == G.POSITIVE_CATEGORY_TARGETS["definition"]
    assert by_cat["formula"] == G.POSITIVE_CATEGORY_TARGETS["formula"]
    assert by_cat["negative"] == G.NEG_FALSE_PREMISE_TARGET + G.NEG_OUT_OF_SCOPE_TARGET


def test_balance_negatives_splits_subtypes() -> None:
    pool = [_res("negative", "false_premise") for _ in range(20)] + [
        _res("negative", "out_of_scope") for _ in range(20)
    ]
    picked = G._balance_negatives(pool, target=16)
    kinds = [p.job.kind for p in picked]
    assert kinds.count("false_premise") == G.NEG_FALSE_PREMISE_TARGET
    assert kinds.count("out_of_scope") == G.NEG_OUT_OF_SCOPE_TARGET


def test_round_robin_by_series_spreads() -> None:
    pool = [_res("definition", "positive", series="23") for _ in range(10)] + [
        _res("definition", "positive", series="38") for _ in range(10)
    ]
    picked = G._round_robin_by_series(pool, target=4)
    series = [p.job.series for p in picked]
    assert series.count("23") == 2 and series.count("38") == 2


def test_assign_ids_unique_and_prefixed() -> None:
    items = [
        {"category": "definition", "question": "b"},
        {"category": "definition", "question": "a"},
        {"category": "negative", "question": "c"},
    ]
    G._assign_ids(items)
    ids = [it["id"] for it in items]
    assert len(set(ids)) == 3
    assert all(i.startswith("hc-") for i in ids)
    # 按 question 排序后 a 在 b 前
    by_q = {it["question"]: it["id"] for it in items}
    assert by_q["a"] == "hc-def-001" and by_q["b"] == "hc-def-002"
    assert by_q["c"] == "hc-neg-001"
