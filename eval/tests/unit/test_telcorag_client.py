"""单测 `eval.huawei_compare.telcorag_client`：双重 JSON 解析 / context 切分 / 单题隔离 / 保序。"""

from __future__ import annotations

import json

import httpx
import pytest

from eval.huawei_compare.telcorag_client import (
    BaselineAnswer,
    _parse_process_query_body,
    _split_contexts,
    collect_baseline,
    query_telcorag,
)

# === 纯函数 ================================================================


@pytest.mark.unit
def test_parse_process_query_body_double_encoded() -> None:
    inner = {"result": "ans", "retrieval": "ctx", "query": "q"}
    # deploy_api：dict → json.dumps → FastAPI 再序列化一次 → httpx.json() 得到 str
    assert _parse_process_query_body(json.dumps(inner)) == inner


@pytest.mark.unit
def test_parse_process_query_body_plain_dict() -> None:
    d = {"result": "x"}
    assert _parse_process_query_body(d) == d


@pytest.mark.unit
def test_parse_process_query_body_bad_type() -> None:
    with pytest.raises(ValueError):
        _parse_process_query_body(12345)


@pytest.mark.unit
def test_split_contexts() -> None:
    assert _split_contexts("") == []
    assert _split_contexts("   ") == []
    assert _split_contexts("only one block") == ["only one block"]
    assert _split_contexts("a\n\n b \n\nc") == ["a", "b", "c"]


# === 单题 HTTP ============================================================


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.unit
async def test_query_telcorag_happy() -> None:
    inner = {
        "result": "PDU Session is an association between UE and DN.",
        "retrieval": "Retrieval 1: 23.501 §5.6\n\nRetrieval 2: 23.501 §3.1",
        "query": "What is a PDU session?",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/process_query/"
        sent = json.loads(req.content)
        assert sent["model_name"] == "gpt-4o-mini"
        assert sent["api_key"] == "sk-test"
        # 模拟 deploy_api 的双重编码：json= 把 str 再编码一次
        return httpx.Response(200, json=json.dumps(inner))

    async with _mock_client(handler) as client:
        ans = await query_telcorag(
            "def-001",
            "What is a PDU session in 5G?",
            client=client,
            base_url="http://localhost:8000",
            model_name="gpt-4o-mini",
            api_key="sk-test",
        )
    assert ans.ok
    assert ans.error is None
    assert "association between UE and DN" in ans.answer
    assert ans.contexts == ["Retrieval 1: 23.501 §5.6", "Retrieval 2: 23.501 §3.1"]
    assert ans.rephrased_query == "What is a PDU session?"


@pytest.mark.unit
async def test_query_telcorag_http_500_isolated() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with _mock_client(handler) as client:
        ans = await query_telcorag(
            "x", "q", client=client, base_url="http://h", model_name="m", api_key="k"
        )
    assert not ans.ok
    assert ans.error is not None
    assert ans.answer == ""


@pytest.mark.unit
async def test_query_telcorag_empty_result_marked_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=json.dumps({"result": "", "retrieval": "r", "query": "q"}))

    async with _mock_client(handler) as client:
        ans = await query_telcorag(
            "x", "q", client=client, base_url="http://h", model_name="m", api_key="k"
        )
    assert not ans.ok
    assert ans.error is not None and ans.error["type"] == "EmptyAnswer"


# === 批量 =================================================================


@pytest.mark.unit
async def test_collect_baseline_preserves_order_and_isolates() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        sent = json.loads(req.content)
        q = sent["query"]
        if q == "fail":
            return httpx.Response(503, text="down")
        body = json.dumps({"result": f"ans:{q}", "retrieval": "", "query": q})
        return httpx.Response(200, json=body)

    items = [("a", "q1"), ("b", "fail"), ("c", "q3")]
    async with _mock_client(handler) as client:
        out = await collect_baseline(
            items, client=client, base_url="http://h", model_name="m", api_key="k", concurrent=3
        )
    assert [a.item_id for a in out] == ["a", "b", "c"]  # 保序
    assert out[0].answer == "ans:q1" and out[0].ok
    assert not out[1].ok and out[1].error is not None  # 单题失败隔离
    assert out[2].answer == "ans:q3" and out[2].ok


@pytest.mark.unit
def test_baseline_answer_to_dict_roundtrip() -> None:
    a = BaselineAnswer(item_id="x", question="q", answer="a", contexts=["c1"])
    d = a.to_dict()
    assert d["item_id"] == "x" and d["contexts"] == ["c1"] and d["error"] is None
