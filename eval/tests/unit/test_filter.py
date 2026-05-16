"""eval/teleqna/filter.py 单测。"""

from __future__ import annotations

import json
from pathlib import Path

from eval.teleqna.filter import (
    DEFAULT_CATEGORIES_KEEP,
    FilterStats,
    _topic_score,
    filter_jsonl,
    filter_one,
)


def _item(
    *,
    question: str,
    options: dict[str, str] | None = None,
    answer: str = "option 1",
    explanation: str = "",
    category: str = "Standards specifications",
) -> dict:
    out = {
        "question": question,
        "answer": answer,
        "explanation": explanation,
        "category": category,
    }
    if options:
        for k, v in options.items():
            out[k] = v
    return out


class TestFilterOne:
    def test_kept_when_spec_in_whitelist(self) -> None:
        item = _item(
            question="What is PDU Session in 5G?",
            explanation="See TS 23.501 §5.6 for definition.",
        )
        verdict, enriched = filter_one(item)
        assert verdict == "kept"
        assert enriched["expected_specs_inferred"] == ["23.501"]
        assert enriched["specs_seen"] == ["23.501"]
        assert enriched["topic_score_5g"] >= 1

    def test_kept_with_multiple_specs(self) -> None:
        item = _item(
            question="Compare AMF (TS 23.501) and SMF in 5G.",
            explanation="TS 23.501 §6 and TS 23.502 §4 describe these.",
        )
        verdict, enriched = filter_one(item)
        assert verdict == "kept"
        assert set(enriched["expected_specs_inferred"]) == {"23.501", "23.502"}

    def test_rejected_category(self) -> None:
        item = _item(question="Q?", category="Lexicon", explanation="TS 38.331")
        verdict, _ = filter_one(item)
        assert verdict == "rejected_category"

    def test_rejected_no_spec(self) -> None:
        item = _item(
            question="Generic 5G question without spec ref",
            explanation="It's a 5G concept.",
        )
        verdict, _ = filter_one(item)
        assert verdict == "rejected_no_spec"

    def test_out_of_scope_when_specs_outside_whitelist(self) -> None:
        item = _item(
            question="What does TS 22.011 cover?",
            explanation="TS 22.011 v17 about service accessibility.",
        )
        verdict, enriched = filter_one(item)
        assert verdict == "out_of_scope"
        assert enriched["specs_seen"] == ["22.011"]
        assert enriched["expected_specs_inferred"] == []
        assert "out of POC 17-whitelist" in enriched["reject_reason"]

    def test_partial_whitelist_match_keeps_only_whitelisted(self) -> None:
        # 既引 24.501（whitelist）又引 22.011（whitelist 外）→ 留，但 inferred 只含 whitelist 的
        item = _item(
            question="Procedure references",
            explanation="See TS 24.501 §5.5 and TS 22.011 §3.",
        )
        verdict, enriched = filter_one(item)
        assert verdict == "kept"
        assert enriched["expected_specs_inferred"] == ["24.501"]
        assert set(enriched["specs_seen"]) == {"24.501", "22.011"}

    def test_aliases_applied(self) -> None:
        # 自定义 alias 把 38.331-h60 映射回 38.331
        aliases = {"38.331-h60": "38.331"}
        item = _item(
            question="Q",
            explanation="From ts38.331-h60 the IE is defined.",
        )
        verdict, enriched = filter_one(item, aliases=aliases)
        assert verdict == "kept"
        assert enriched["expected_specs_inferred"] == ["38.331"]

    def test_overview_category_kept(self) -> None:
        item = _item(
            question="What is the 5G core architecture?",
            category="Standards overview",
            explanation="TS 23.501 §4 covers the architecture.",
        )
        verdict, _ = filter_one(item)
        assert verdict == "kept"

    def test_strict_mode_drops_overview(self) -> None:
        item = _item(
            question="overview",
            category="Standards overview",
            explanation="TS 23.501",
        )
        verdict, _ = filter_one(item, categories_keep=frozenset({"Standards specifications"}))
        assert verdict == "rejected_category"


class TestTopicScore:
    def test_zero_when_no_keyword(self) -> None:
        # 之前 "NR" 子串会命中 "u**nr**elated" — word boundary 必须修复
        assert _topic_score("totally unrelated text") == 0

    def test_counts_unique_keywords(self) -> None:
        text = "AMF and SMF and PDU Session and gNB"
        # 4 个 keyword 命中
        assert _topic_score(text) == 4

    def test_empty(self) -> None:
        assert _topic_score("") == 0

    def test_word_boundary_prevents_false_positive(self) -> None:
        # 含字符串子串"nr"/"amf"但不是独立词 → 不命中
        assert _topic_score("an unrelated remark about famous foo") == 0
        # 真实 NR 缩写 + 标点 → 命中
        assert _topic_score("NR is part of 5G.") >= 1
        # 短缩写在词内不应命中
        assert _topic_score("This is a fAMFest test.") == 0

    def test_case_insensitive(self) -> None:
        assert _topic_score("amf and smf") == 2
        assert _topic_score("AMF and SMF") == 2


class TestFilterJsonl:
    def test_end_to_end_writes_filtered_and_oos(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw.jsonl"
        items = [
            _item(
                question="Q1 about 5G PDU Session",
                explanation="See TS 23.501 §5.6",
            ),
            _item(
                question="Q2 about TS 22.011",
                explanation="TS 22.011 v17",
            ),
            _item(question="Q3 no spec", explanation="just text"),
            _item(
                question="Q4 wrong category",
                category="Lexicon",
                explanation="TS 38.331",
            ),
            _item(
                question="Q5 multi spec partial",
                explanation="TS 24.501 §5.5 and TS 22.011 §3",
            ),
        ]
        raw.write_text("\n".join(json.dumps(it) for it in items) + "\n", encoding="utf-8")

        out_dir = tmp_path / "out"
        stats = filter_jsonl(raw_jsonl=raw, out_dir=out_dir)

        assert stats.total == 5
        assert stats.kept == 2  # Q1 + Q5
        assert stats.out_of_scope == 1  # Q2
        assert stats.rejected_no_spec == 1  # Q3
        assert stats.rejected_category == 1  # Q4
        assert stats.by_spec.get("23.501") == 1
        assert stats.by_spec.get("24.501") == 1

        kept = [json.loads(line) for line in (out_dir / "filtered.jsonl").read_text().splitlines()]
        oos = [
            json.loads(line) for line in (out_dir / "out_of_scope.jsonl").read_text().splitlines()
        ]
        assert len(kept) == 2
        assert len(oos) == 1

        stats_dict = json.loads((out_dir / "filter_stats.json").read_text())
        assert stats_dict["kept"] == 2
        assert stats_dict["by_spec"]["23.501"] == 1

    def test_default_categories_keep_constant(self) -> None:
        assert "Standards specifications" in DEFAULT_CATEGORIES_KEEP
        assert "Standards overview" in DEFAULT_CATEGORIES_KEEP


class TestFilterStats:
    def test_to_dict_sorts_by_spec(self) -> None:
        s = FilterStats(total=3, kept=2)
        s.by_spec = {"38.331": 1, "23.501": 1}
        d = s.to_dict()
        assert list(d["by_spec"].keys()) == ["23.501", "38.331"]
