"""单测 `eval.langfuse_dataset`：dataset upsert / score upload / 缺 key 容忍。

覆盖（M7.3 验收）：
- get_client：缺 key → None；有 key + mock SDK → 单例
- push_golden_to_langfuse：读 YAML → 按 item.id upsert；缺 client 返回 0；单条失败不阻塞
- _golden_to_item_payload：字段映射正确
- make_eval_trace_id：缺 client → None；有 client → 走 create_trace_id(seed=...)
- _coerce_score：float / NaN / None / 非数 归类
- push_run_score：bool → 0/1；None / NaN → skip；trace_id 缺 → 0；create_score 抛错只 skip 这一个
- runner.run_eval 接入 langfuse mock → trace_id / event / score 都被调
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from eval import langfuse_dataset as lf
from eval.langfuse_dataset import (
    _coerce_score,
    _golden_to_item_payload,
    _reset_for_tests,
    get_client,
    make_eval_trace_id,
    push_golden_to_langfuse,
    push_run_score,
)
from eval.retrieval.metrics import ExpectedSpec
from eval.runner_retrieval import GoldenItem

# === Fakes ================================================================


class FakeLangfuseClient:
    """记录 SDK 调用的极简 mock，覆盖 dataset / trace / score 4 个用到的方法。"""

    def __init__(
        self,
        *,
        fail_create_dataset: bool = False,
        fail_item_ids: tuple[str, ...] = (),
        fail_score_names: tuple[str, ...] = (),
        next_trace_id: str = "fake-trace-id-deadbeef",
    ) -> None:
        self.fail_create_dataset = fail_create_dataset
        self.fail_item_ids = set(fail_item_ids)
        self.fail_score_names = set(fail_score_names)
        self.next_trace_id = next_trace_id

        self.datasets: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.trace_id_calls: list[str | None] = []

    def create_dataset(self, *, name: str, description: str | None = None, **_: Any) -> Any:
        if self.fail_create_dataset:
            raise RuntimeError("dataset already exists (simulated)")
        self.datasets.append({"name": name, "description": description})
        return {"id": f"ds-{name}", "name": name}

    def create_dataset_item(self, *, dataset_name: str, id: str, **payload: Any) -> Any:
        if id in self.fail_item_ids:
            raise RuntimeError(f"simulated upsert failure for {id}")
        rec = {"dataset_name": dataset_name, "id": id, **payload}
        self.items.append(rec)
        return rec

    def create_trace_id(self, *, seed: str | None = None) -> str:
        self.trace_id_calls.append(seed)
        return self.next_trace_id

    def create_event(self, **kwargs: Any) -> Any:
        self.events.append(kwargs)
        return {"id": "ev-1"}

    def create_score(self, *, name: str, value: float, **kwargs: Any) -> None:
        if name in self.fail_score_names:
            raise RuntimeError(f"simulated score failure for {name}")
        self.scores.append({"name": name, "value": value, **kwargs})


def _golden_item(
    *,
    item_id: str = "x-001",
    expected_facts: list[str] | None = None,
    expected_specs: list[tuple[str, list[str]]] | None = None,
    forbidden: list[str] | None = None,
    must_say_not_found: bool = False,
    source: str = "hand_crafted",
) -> GoldenItem:
    return GoldenItem(
        id=item_id,
        category="definition",
        language="en",
        question="What is AMF?",
        expected_specs=[
            ExpectedSpec(spec_id=sid, sections=tuple(secs)) for sid, secs in (expected_specs or [])
        ],
        expected_facts=expected_facts or [],
        forbidden=forbidden or [],
        must_say_not_found=must_say_not_found,
        source=source,
        notes="note",
    )


def _write_minimal_golden(p: Path, items: list[dict[str, Any]]) -> None:
    doc = {
        "version": 1,
        "created_at": "2026-05-20",
        "total": len(items),
        "sources": ["hand_crafted"],
        "categories": ["definition"],
        "items": items,
    }
    p.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    _reset_for_tests()
    yield
    _reset_for_tests()


# === get_client ===========================================================


class TestGetClient:
    def test_missing_keys_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from eval.settings import EvalSettings

        s = EvalSettings(langfuse_public_key="", langfuse_secret_key="")
        assert get_client(s) is None
        assert get_client(s) is None  # 二次也 None（_init_failed 短路）

    def test_present_keys_uses_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """构造一个 fake Langfuse 类替换 SDK import → 验证单例 + 参数透传。"""
        from eval.settings import EvalSettings

        calls: list[dict[str, Any]] = []

        class _FakeSDK:
            def __init__(self, **kw: Any) -> None:
                calls.append(kw)

        # 替换 sys.modules['langfuse'] 的 Langfuse 名
        import sys
        import types

        fake_mod = types.ModuleType("langfuse")
        fake_mod.Langfuse = _FakeSDK  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "langfuse", fake_mod)

        s = EvalSettings(
            langfuse_public_key="pk-xxx",
            langfuse_secret_key="sk-yyy",
            langfuse_host="https://example.com",
        )
        c1 = get_client(s)
        c2 = get_client(s)
        assert isinstance(c1, _FakeSDK)
        assert c1 is c2  # 单例
        assert calls == [
            {
                "public_key": "pk-xxx",
                "secret_key": "sk-yyy",
                "host": "https://example.com",
            }
        ]


# === _golden_to_item_payload ==============================================


def test_golden_to_item_payload_full() -> None:
    item = _golden_item(
        item_id="def-1",
        expected_facts=["access and mobility"],
        expected_specs=[("23.501", ["5.2.1"])],
        forbidden=["LTE"],
        must_say_not_found=False,
    )
    p = _golden_to_item_payload(item)
    assert p["id"] == "def-1"
    assert p["input"] == {
        "question": "What is AMF?",
        "category": "definition",
        "language": "en",
    }
    assert p["expected_output"]["expected_facts"] == ["access and mobility"]
    assert p["expected_output"]["expected_specs"] == [{"spec_id": "23.501", "sections": ["5.2.1"]}]
    assert p["expected_output"]["forbidden"] == ["LTE"]
    assert p["expected_output"]["must_say_not_found"] is False
    assert p["metadata"]["source"] == "hand_crafted"
    assert p["metadata"]["notes"] == "note"


# === push_golden_to_langfuse ==============================================


class TestPushGolden:
    def test_no_client_returns_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 强制 get_client 返回 None
        monkeypatch.setattr(lf, "get_client", lambda *_a, **_kw: None)
        golden = tmp_path / "v1.yaml"
        _write_minimal_golden(
            golden,
            [
                {
                    "id": "x",
                    "category": "definition",
                    "language": "en",
                    "source": "hand_crafted",
                    "question": "?",
                    "expected_specs": [],
                    "expected_facts": [],
                    "forbidden": [],
                    "must_say_not_found": False,
                }
            ],
        )
        assert push_golden_to_langfuse(golden) == 0

    def test_upsert_all_items(self, tmp_path: Path) -> None:
        golden = tmp_path / "v1.yaml"
        _write_minimal_golden(
            golden,
            [
                {
                    "id": "def-1",
                    "category": "definition",
                    "language": "en",
                    "source": "hand_crafted",
                    "question": "Q1",
                    "expected_specs": [{"spec_id": "23.501", "sections": ["5.2.1"]}],
                    "expected_facts": ["AMF"],
                    "forbidden": [],
                    "must_say_not_found": False,
                },
                {
                    "id": "def-2",
                    "category": "definition",
                    "language": "en",
                    "source": "hand_crafted",
                    "question": "Q2",
                    "expected_specs": [],
                    "expected_facts": ["SMF"],
                    "forbidden": [],
                    "must_say_not_found": False,
                },
            ],
        )
        cli = FakeLangfuseClient()
        n = push_golden_to_langfuse(golden, dataset_name="tgpp-golden-test", client=cli)
        assert n == 2
        assert cli.datasets == [{"name": "tgpp-golden-test", "description": None}]
        assert [x["id"] for x in cli.items] == ["def-1", "def-2"]
        assert cli.items[0]["dataset_name"] == "tgpp-golden-test"
        assert cli.items[0]["input"]["question"] == "Q1"

    def test_existing_dataset_does_not_abort(self, tmp_path: Path) -> None:
        """二次推送：create_dataset 抛错被吞掉，item 仍能 upsert（SDK 文档保证 id 幂等）。"""
        golden = tmp_path / "v1.yaml"
        _write_minimal_golden(
            golden,
            [
                {
                    "id": "def-1",
                    "category": "definition",
                    "language": "en",
                    "source": "hand_crafted",
                    "question": "Q",
                    "expected_specs": [],
                    "expected_facts": [],
                    "forbidden": [],
                    "must_say_not_found": False,
                }
            ],
        )
        cli = FakeLangfuseClient(fail_create_dataset=True)
        assert push_golden_to_langfuse(golden, client=cli) == 1
        assert cli.datasets == []  # 创建失败没记录
        assert len(cli.items) == 1

    def test_single_item_failure_isolated(self, tmp_path: Path) -> None:
        golden = tmp_path / "v1.yaml"
        _write_minimal_golden(
            golden,
            [
                {
                    "id": "ok-1",
                    "category": "definition",
                    "language": "en",
                    "source": "hand_crafted",
                    "question": "Q",
                    "expected_specs": [],
                    "expected_facts": [],
                    "forbidden": [],
                    "must_say_not_found": False,
                },
                {
                    "id": "bad-1",
                    "category": "definition",
                    "language": "en",
                    "source": "hand_crafted",
                    "question": "Q",
                    "expected_specs": [],
                    "expected_facts": [],
                    "forbidden": [],
                    "must_say_not_found": False,
                },
            ],
        )
        cli = FakeLangfuseClient(fail_item_ids=("bad-1",))
        assert push_golden_to_langfuse(golden, client=cli) == 1
        assert [x["id"] for x in cli.items] == ["ok-1"]


# === make_eval_trace_id ===================================================


class TestMakeEvalTraceId:
    def test_no_client_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lf, "get_client", lambda *_a, **_kw: None)
        assert make_eval_trace_id("run-1", "def-1") is None

    def test_uses_seed(self) -> None:
        cli = FakeLangfuseClient(next_trace_id="trace-abc")
        tid = make_eval_trace_id("run-1", "def-1", client=cli)
        assert tid == "trace-abc"
        assert cli.trace_id_calls == ["run-1:def-1"]


# === _coerce_score / push_run_score =======================================


class TestCoerceScore:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, None),
            (0.5, 0.5),
            (1, 1.0),
            ("0.7", 0.7),
            ("nope", None),
            (math.nan, None),
        ],
    )
    def test_cases(self, raw: Any, expected: float | None) -> None:
        assert _coerce_score(raw) == expected


class TestPushRunScore:
    def test_no_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lf, "get_client", lambda *_a, **_kw: None)
        assert push_run_score("trace-1", {"a": 0.5}) == 0

    def test_no_trace_id(self) -> None:
        cli = FakeLangfuseClient()
        assert push_run_score(None, {"a": 0.5}, client=cli) == 0
        assert push_run_score("", {"a": 0.5}, client=cli) == 0
        assert cli.scores == []

    def test_writes_non_none_only(self) -> None:
        cli = FakeLangfuseClient()
        n = push_run_score(
            "trace-1",
            {
                "context_recall_section": 1.0,
                "fact_coverage": None,
                "ragas_faithfulness": 0.8,
                "ragas_answer_relevance": math.nan,
                "must_say_not_found_passed": True,
                "forbidden_violation": False,
            },
            comment="run=r1",
            metadata={"item_id": "def-1"},
            client=cli,
        )
        # context_recall_section + ragas_faithfulness + must_say(=1) + forbidden(=0) = 4
        assert n == 4
        recorded = {s["name"]: s["value"] for s in cli.scores}
        assert recorded["context_recall_section"] == 1.0
        assert recorded["ragas_faithfulness"] == 0.8
        assert recorded["must_say_not_found_passed"] == 1.0
        assert recorded["forbidden_violation"] == 0.0
        for s in cli.scores:
            assert s["trace_id"] == "trace-1"
            assert s["data_type"] == "NUMERIC"
            assert s["comment"] == "run=r1"
            assert s["metadata"] == {"item_id": "def-1"}

    def test_single_metric_failure_isolated(self) -> None:
        cli = FakeLangfuseClient(fail_score_names=("bad_metric",))
        n = push_run_score(
            "trace-1",
            {"good": 0.5, "bad_metric": 0.3, "good2": 0.7},
            client=cli,
        )
        assert n == 2
        assert sorted(s["name"] for s in cli.scores) == ["good", "good2"]


# === runner.run_eval 接入 langfuse mock ===================================


def _sse_body() -> str:
    chunk_payload = (
        '{"chunks": [{"spec_id": "23.501", "section_path": "5.2.1", "content": "AMF content"}]}'
    )
    lines = [
        "event: chunks_rerank",
        f"data: {chunk_payload}",
        "",
        "event: final",
        'data: {"answer": "AMF handles access and mobility.", "citations": [], "confidence": 0.8}',
        "",
        "event: end",
        "data: {}",
        "",
    ]
    return "\n".join(lines) + "\n"


def _mock_transport(*, session_id: str = "s1") -> httpx.MockTransport:
    body = _sse_body()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/sessions") and req.method == "POST":
            return httpx.Response(201, json={"id": session_id})
        if "/messages" in req.url.path and req.method == "POST":
            return httpx.Response(
                200,
                content=body.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_run_eval_pushes_to_langfuse_when_label_set(tmp_path: Path) -> None:
    from eval.runner import run_eval

    golden = tmp_path / "g.yaml"
    _write_minimal_golden(
        golden,
        [
            {
                "id": "def-1",
                "category": "definition",
                "language": "en",
                "source": "hand_crafted",
                "question": "What is AMF?",
                "expected_specs": [{"spec_id": "23.501", "sections": ["5.2.1"]}],
                "expected_facts": ["access and mobility"],
                "forbidden": [],
                "must_say_not_found": False,
            }
        ],
    )

    fake_client = FakeLangfuseClient(next_trace_id="trace-X")
    async with httpx.AsyncClient(transport=_mock_transport(), base_url="http://t") as cli:
        results = await run_eval(
            golden,
            client=cli,
            auth_token="t",
            langfuse_run_label="m7-smoke-2026-05-20",
            langfuse_dataset_name="tgpp-golden-v1",
            langfuse_client=fake_client,
        )

    assert len(results) == 1
    r = results[0]
    assert r.langfuse_trace_id == "trace-X"
    # 1 个 event + ≥3 个 score 上传
    assert len(fake_client.events) == 1
    assert fake_client.events[0]["trace_context"] == {"trace_id": "trace-X"}
    assert fake_client.events[0]["input"]["question"] == "What is AMF?"
    score_names = {s["name"] for s in fake_client.scores}
    assert "context_recall_section" in score_names
    assert "fact_coverage" in score_names
    assert "forbidden_violation" in score_names


@pytest.mark.asyncio
async def test_run_eval_no_langfuse_when_label_none(tmp_path: Path) -> None:
    """没传 langfuse_run_label → 完全跳过 langfuse 路径，原行为不变。"""
    from eval.runner import run_eval

    golden = tmp_path / "g.yaml"
    _write_minimal_golden(
        golden,
        [
            {
                "id": "def-1",
                "category": "definition",
                "language": "en",
                "source": "hand_crafted",
                "question": "Q?",
                "expected_specs": [],
                "expected_facts": [],
                "forbidden": [],
                "must_say_not_found": False,
            }
        ],
    )

    fake_client = FakeLangfuseClient()
    async with httpx.AsyncClient(transport=_mock_transport(), base_url="http://t") as cli:
        results = await run_eval(
            golden,
            client=cli,
            auth_token="t",
            langfuse_client=fake_client,  # 传了 client 但没传 label → 仍应跳过
        )

    assert results[0].langfuse_trace_id is None
    assert fake_client.events == []
    assert fake_client.scores == []
