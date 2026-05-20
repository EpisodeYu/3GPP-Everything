"""单测 `eval.sse_parser`：一次解析 / 流式 / JSON 解码 / ping 忽略 / 多 data 拼接。"""

from __future__ import annotations

import json

import pytest

from eval.sse_parser import SSEEvent, SSEStreamParser, parse_sse_text


def _sse_lines(*events: tuple[str, str | dict]) -> str:
    """生成与 sse-starlette 一致的 SSE payload（每帧 event:/data:/空行）。"""
    parts: list[str] = []
    for ev, data in events:
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        parts.append(f"event: {ev}")
        for line in payload.splitlines() or [""]:
            parts.append(f"data: {line}")
        parts.append("")  # 帧分隔
    return "\n".join(parts) + "\n"


class TestParseSseText:
    def test_single_event(self) -> None:
        text = _sse_lines(("run_start", {"run_id": "r1"}))
        out = parse_sse_text(text)
        assert len(out) == 1
        assert out[0].event == "run_start"
        assert out[0].parse_json() == {"run_id": "r1"}

    def test_multiple_events_in_order(self) -> None:
        text = _sse_lines(
            ("run_start", {"run_id": "r1"}),
            ("node_start", {"node": "retrieve"}),
            ("node_end", {"node": "retrieve", "duration_ms": 42}),
            ("token", {"delta": "Hello "}),
            ("token", {"delta": "world."}),
            ("final", {"answer": "Hello world.", "citations": [], "confidence": 0.5}),
            ("end", {}),
        )
        out = parse_sse_text(text)
        assert [e.event for e in out] == [
            "run_start",
            "node_start",
            "node_end",
            "token",
            "token",
            "final",
            "end",
        ]
        assert out[3].parse_json()["delta"] == "Hello "
        assert out[5].parse_json()["answer"] == "Hello world."

    def test_ping_comment_ignored(self) -> None:
        text = (
            ": ping\n"
            "event: run_start\n"
            "data: {\"run_id\": \"r1\"}\n"
            "\n"
            ": keepalive\n"
            "event: end\n"
            "data: {}\n"
            "\n"
        )
        out = parse_sse_text(text)
        assert len(out) == 2
        assert [e.event for e in out] == ["run_start", "end"]

    def test_multi_line_data_joined_with_newline(self) -> None:
        text = "event: chunks_hit\ndata: line1\ndata: line2\n\n"
        out = parse_sse_text(text)
        assert len(out) == 1
        assert out[0].data == "line1\nline2"

    def test_empty_input(self) -> None:
        assert parse_sse_text("") == []

    def test_event_at_eof_without_trailing_blank(self) -> None:
        text = "event: end\ndata: {}\n"  # 没有结尾空行
        out = parse_sse_text(text)
        assert len(out) == 1
        assert out[0].event == "end"


class TestSseEventParseJson:
    def test_ok(self) -> None:
        ev = SSEEvent(event="x", data='{"a": 1}')
        assert ev.parse_json() == {"a": 1}

    def test_invalid_json_raises(self) -> None:
        ev = SSEEvent(event="x", data="not json")
        with pytest.raises(ValueError, match="not json"):
            ev.parse_json()

    def test_non_dict_raises(self) -> None:
        ev = SSEEvent(event="x", data="[1, 2]")
        with pytest.raises(ValueError, match="not json dict"):
            ev.parse_json()


class TestStreamParser:
    def test_feed_lines_one_event(self) -> None:
        p = SSEStreamParser()
        for line in [
            "event: run_start",
            'data: {"run_id": "r1"}',
            "",
        ]:
            p.feed(line)
        evs = list(p.drain())
        assert len(evs) == 1
        assert evs[0].event == "run_start"

    def test_feed_lines_multiple_events_incremental(self) -> None:
        p = SSEStreamParser()
        feed_chunks = [
            "event: run_start",
            'data: {"run_id": "r1"}',
            "",
            "event: token",
            'data: {"delta": "Hi"}',
            "",
            "event: end",
            "data: {}",
        ]
        seen: list[str] = []
        for line in feed_chunks:
            p.feed(line)
            for ev in p.drain():
                seen.append(ev.event)
        for ev in p.close():  # 把没 trailing-blank 的最后一帧刷出
            seen.append(ev.event)
        assert seen == ["run_start", "token", "end"]

    def test_ping_lines_in_stream_ignored(self) -> None:
        p = SSEStreamParser()
        for line in [
            ": ping",
            "event: end",
            "data: {}",
            "",
        ]:
            p.feed(line)
        evs = list(p.drain())
        assert len(evs) == 1
        assert evs[0].event == "end"

    def test_close_without_pending_yields_nothing(self) -> None:
        p = SSEStreamParser()
        for line in ["event: end", "data: {}", ""]:
            p.feed(line)
        list(p.drain())
        assert list(p.close()) == []
