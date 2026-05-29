"""Unit tests for eval.scripts.golden_repair_teleqna.

聚焦纯函数 / 状态变换；LLM 调用走 monkeypatch stub。
"""

from __future__ import annotations

from typing import Any

import pytest

from eval.scripts import golden_repair_teleqna as M


def _item(
    iid: str,
    *,
    source: str = "teleqna_transformed",
    category: str = "definition",
    forbidden: list[str] | None = None,
    facts: list[str] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    it: dict[str, Any] = {
        "id": iid,
        "source": source,
        "category": category,
        "question": f"Question for {iid}",
        "expected_specs": [{"spec_id": "23.501", "sections": []}],
        "expected_facts": facts or [],
        "forbidden": forbidden or [],
    }
    if notes is not None:
        it["notes"] = notes
    return it


class TestRepairForbidden:
    def test_clears_teleqna_non_negative(self) -> None:
        items = [
            _item("def-1", forbidden=["foo", "bar"]),
            _item("def-2", forbidden=[]),
            _item("neg-1", category="negative", forbidden=["X"]),
            _item("hand-1", source="hand_crafted", forbidden=["Y"]),
        ]
        changed = M.repair_forbidden(items)
        assert changed == 1
        assert items[0]["forbidden"] == []
        # negative + hand_crafted untouched
        assert items[2]["forbidden"] == ["X"]
        assert items[3]["forbidden"] == ["Y"]

    def test_breadcrumb_appended_to_notes(self) -> None:
        items = [_item("def-1", forbidden=["foo"], notes="original note")]
        M.repair_forbidden(items)
        assert "original note" in items[0]["notes"]
        assert "M7.7" in items[0]["notes"]
        assert "foo" in items[0]["notes"]  # original forbidden listed

    def test_breadcrumb_creates_notes_when_absent(self) -> None:
        items = [_item("def-1", forbidden=["foo"])]
        M.repair_forbidden(items)
        assert "M7.7" in items[0]["notes"]


class TestAudit:
    def test_counts(self) -> None:
        long_fact = "Long sentence with many words ending."
        items = [
            _item("def-1", forbidden=["A", "B"], facts=["short", long_fact]),
            _item("def-2", forbidden=[]),
            _item("hand-1", source="hand_crafted", forbidden=["Z"], facts=[long_fact]),
            _item("neg-1", category="negative", forbidden=["X"]),
        ]
        a = M._audit(items)
        assert a["teleqna_total"] == 3  # def-1 / def-2 / neg-1
        assert a["teleqna_non_negative"] == 2
        assert a["non_negative_with_forbidden"] == 1
        assert a["non_negative_forbidden_entries"] == 2
        # facts: only count non-negative teleqna → def-1 has 2 facts
        assert a["facts_total"] == 2
        assert a["facts_long_or_sentence_like"] == 1  # the "Long sentence...ending."


class TestRepairFactsLLMStub:
    def test_skips_already_atomic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[str] = []

        def stub(*a, **kw):
            called.append("called")
            return ["X"]

        monkeypatch.setattr(M, "_call_litellm_rewrite", stub)
        from eval.settings import EvalSettings

        s = EvalSettings(_env_file=None)  # type: ignore[call-arg]
        items = [
            _item("def-1", facts=["QPSK", "16QAM", "240 kHz"]),  # already atomic
        ]
        touched, errors = M.repair_facts(items, settings=s, model="x", budget=10)
        assert touched == 0
        assert errors == 0
        assert called == []

    def test_rewrites_long_facts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def stub(s, *, question, facts, model, timeout_s=60.0):
            assert "Long sentence" in facts[0]
            return ["Atom1", "Atom2"]

        monkeypatch.setattr(M, "_call_litellm_rewrite", stub)
        from eval.settings import EvalSettings

        s = EvalSettings(_env_file=None)  # type: ignore[call-arg]
        items = [
            _item("def-1", facts=["Long sentence about something.", "short"]),
        ]
        touched, _err = M.repair_facts(items, settings=s, model="x", budget=10)
        assert touched == 1
        assert items[0]["expected_facts"] == ["Atom1", "Atom2"]
        # original archived
        assert items[0]["_original_expected_facts"] == ["Long sentence about something.", "short"]
        assert "M7.7" in items[0]["notes"]

    def test_llm_failure_counts_error_keeps_facts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(M, "_call_litellm_rewrite", lambda *a, **k: None)
        from eval.settings import EvalSettings

        s = EvalSettings(_env_file=None)  # type: ignore[call-arg]
        items = [_item("def-1", facts=["Long sentence about something."])]
        touched, errors = M.repair_facts(items, settings=s, model="x", budget=10)
        assert touched == 0
        assert errors == 1
        # facts untouched
        assert items[0]["expected_facts"] == ["Long sentence about something."]

    def test_budget_stops_early(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = [0]

        def stub(*a, **kw):
            call_count[0] += 1
            return ["X"]

        monkeypatch.setattr(M, "_call_litellm_rewrite", stub)
        from eval.settings import EvalSettings

        s = EvalSettings(_env_file=None)  # type: ignore[call-arg]
        items = [
            _item(f"def-{i}", facts=["Long sentence one.", "Long sentence two."]) for i in range(5)
        ]
        touched, _err = M.repair_facts(items, settings=s, model="x", budget=2)
        assert touched == 2
        assert call_count[0] == 2  # stopped early
