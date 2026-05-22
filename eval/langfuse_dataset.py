"""Langfuse Cloud Dataset / Trace / Score 上报（M7.3）。

口径：`docs/03-development/06-evaluation-and-observability.md §6` +
`docs/04-handoff/2026-05-19-m7-plan.md §4.4`。

为什么单独建 eval 子项目的 langfuse client（不复用 backend `app.agent.langfuse_handler`）：
- eval 子项目独立 venv，不 import backend；
- eval 跑评测时不一定接到 backend 的 Langfuse trace（且 backend 当前 chat 路径未挂
  CallbackHandler，evaluator 跑出来无对应 trace 时仍需要自建 client-side trace 持有 score）；
- 缺 key / 缺 SDK 时优雅 disable（不阻塞 runner）。

公开接口：
    get_client(settings=None) -> Langfuse | None
        进程级懒初始化；缺 key / import / 网络初始化失败均返回 None；幂等
    push_golden_to_langfuse(golden_path, *, dataset_name, client=None) -> int
        把 golden YAML 推送到 Langfuse Dataset；按 GoldenItem.id 幂等 upsert（v4
        `create_dataset_item(id=...)` 文档明确"upserts if an item with id already exists"）
        返回成功 upsert 的 item 数；缺 client 时返回 0 + log info
    push_run_score(trace_id, scores, *, comment=None, client=None) -> int
        对 dict 里非 None 的 NUMERIC 分数调 `create_score(trace_id=...)`；缺 trace_id /
        client 时返回 0；单个 metric 写失败 → log + 跳过，不阻塞其他
    make_eval_trace_id(run_label, item_id, *, client=None) -> str | None
        基于 `(run_label, item_id)` 用 `client.create_trace_id(seed=...)` 生成
        Langfuse 32 字符 trace_id；同一 (label, id) 多次跑得到同一 id（便于追 score 历史）

设计原则（与 backend.langfuse_handler 同口径）：
- 模块级单例 + 双检锁，幂等；失败后 _init_failed 短路避免反复重试
- 所有错误都吞掉转 log（warning/info），不抛到 runner
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Lock
from typing import Any

from eval.runner_retrieval import GoldenItem, load_golden
from eval.settings import EvalSettings, get_settings

log = logging.getLogger(__name__)


_client_lock = Lock()
_client: Any | None = None
_init_failed: bool = False


def get_client(settings: EvalSettings | None = None) -> Any | None:
    """进程级懒初始化 Langfuse 全局 client；缺 key / 失败 → None。"""
    global _client, _init_failed
    if _client is not None:
        return _client
    if _init_failed:
        return None
    with _client_lock:
        if _client is not None:
            return _client
        if _init_failed:
            return None
        s = settings or get_settings()
        if not s.langfuse_enabled:
            log.info("langfuse keys missing in eval settings; dataset/score disabled")
            _init_failed = True
            return None
        try:
            from langfuse import Langfuse

            _client = Langfuse(
                public_key=s.langfuse_public_key.strip(),
                secret_key=s.langfuse_secret_key.strip(),
                host=s.langfuse_host.strip() or "https://cloud.langfuse.com",
            )
            return _client
        except Exception as exc:  # pragma: no cover - 依赖网络/版本
            log.warning("langfuse client init failed: %s", exc)
            _init_failed = True
            return None


def _reset_for_tests() -> None:
    """单测专用：清掉单例状态。"""
    global _client, _init_failed
    with _client_lock:
        _client = None
        _init_failed = False


# === Dataset push ==========================================================


def _ensure_dataset(client: Any, name: str, *, description: str | None = None) -> bool:
    """幂等创建 dataset：已存在的 SDK 会抛冲突，吞掉。"""
    try:
        client.create_dataset(name=name, description=description)
        return True
    except Exception as exc:
        # 二次创建会 409 / 已存在的多数 SDK 实现也是抛通用异常；我们对失败一律降级为
        # "假设已存在"，后续 create_dataset_item 若真不存在会再抛错
        log.info("langfuse create_dataset(%s) skipped: %s", name, exc)
        return False


def _golden_to_item_payload(item: GoldenItem) -> dict[str, Any]:
    """GoldenItem → create_dataset_item 入参 dict。

    expected_output 保留 expected_facts / expected_specs / forbidden / must_say_not_found
    四样最小信息；evaluator 直接读 input.question + expected_output 做匹配/打分。
    metadata 留 source / language / category / teleqna_origin_id 给筛选用。
    """
    return {
        "id": item.id,
        "input": {
            "question": item.question,
            "category": item.category,
            "language": item.language,
        },
        "expected_output": {
            "expected_facts": list(item.expected_facts),
            "expected_specs": [
                {"spec_id": s.spec_id, "sections": list(s.sections)} for s in item.expected_specs
            ],
            "forbidden": list(item.forbidden),
            "must_say_not_found": bool(item.must_say_not_found),
        },
        "metadata": {
            "source": item.source,
            "teleqna_origin_id": item.teleqna_origin_id,
            "notes": item.notes,
        },
    }


def push_golden_to_langfuse(
    golden_path: Path,
    *,
    dataset_name: str = "tgpp-golden-v1",
    description: str | None = None,
    client: Any | None = None,
) -> int:
    """把 golden YAML 全量推送到 Langfuse Dataset。

    幂等：dataset 不存在则创建；每条 item 用 `GoldenItem.id` 作为 dataset item id 上传，
    SDK 文档明确 "Upserts if an item with id already exists"。
    缺 client（语句中不传且 settings 也没 key）或推送失败的单条 → log + 跳过，不抛。

    返回成功 upsert 的 item 数（含因网络抖动重跑也算成功的那次）。
    """
    cli = client if client is not None else get_client()
    if cli is None:
        log.info("langfuse client unavailable; skipping push_golden_to_langfuse")
        return 0

    items = load_golden(golden_path)
    _ensure_dataset(cli, dataset_name, description=description)

    ok = 0
    for it in items:
        payload = _golden_to_item_payload(it)
        try:
            cli.create_dataset_item(dataset_name=dataset_name, **payload)
            ok += 1
        except Exception as exc:
            log.warning("langfuse upsert dataset item %s failed: %s", it.id, exc)
    # v4 SDK 是后台批量发送，函数返回后进程若立刻退出会丢 buffer，dashboard 显示 0。
    try:
        cli.flush()
    except Exception as exc:
        log.warning("langfuse flush after dataset push failed: %s", exc)
    log.info("langfuse dataset push: %d/%d ok → %s", ok, len(items), dataset_name)
    return ok


# === Trace id / Score push =================================================


def make_eval_trace_id(
    run_label: str,
    item_id: str,
    *,
    client: Any | None = None,
) -> str | None:
    """基于 (run_label, item_id) 生成幂等 Langfuse trace_id。

    seed = f"{run_label}:{item_id}"；同一 (label, id) 多次跑 → 同一 trace_id，方便回看历史
    score。client 不可用时返回 None，runner 应跳过 score 上传。
    """
    cli = client if client is not None else get_client()
    if cli is None:
        return None
    try:
        return cli.create_trace_id(seed=f"{run_label}:{item_id}")
    except Exception as exc:  # pragma: no cover
        log.warning("langfuse create_trace_id failed for %s/%s: %s", run_label, item_id, exc)
        return None


def _coerce_score(value: Any) -> float | None:
    """把 ragas/runner 出来的分数转成 float；None / NaN / 非数 → None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def push_run_score(
    trace_id: str | None,
    scores: dict[str, float | bool | None],
    *,
    comment: str | None = None,
    metadata: dict[str, Any] | None = None,
    client: Any | None = None,
) -> int:
    """把单条 eval result 的 score dict 写到 Langfuse。

    - 缺 client / trace_id → 静默 skip 返回 0
    - bool（如 must_say_not_found_passed）转 NUMERIC 0/1（避免 v4 BOOLEAN 类型的额外
      enum 校验；现阶段 evaluator 只看分数大小）
    - 单个 metric 写失败 → log + 跳过，不阻塞其他

    返回成功 upsert 的 score 个数。
    """
    cli = client if client is not None else get_client()
    if cli is None or not trace_id:
        return 0

    ok = 0
    for name, raw in scores.items():
        v: float | None = (1.0 if raw else 0.0) if isinstance(raw, bool) else _coerce_score(raw)
        if v is None:
            continue
        try:
            cli.create_score(
                name=name,
                value=v,
                trace_id=trace_id,
                data_type="NUMERIC",
                comment=comment,
                metadata=metadata,
            )
            ok += 1
        except Exception as exc:
            log.warning("langfuse create_score(%s) failed: %s", name, exc)
    # 同 push_golden_to_langfuse 末尾：v4 后台批量发送，需显式 flush 才能落 dashboard。
    try:
        cli.flush()
    except Exception as exc:
        log.warning("langfuse flush after score push failed: %s", exc)
    return ok


__all__ = [
    "_coerce_score",
    "_golden_to_item_payload",
    "_reset_for_tests",
    "get_client",
    "make_eval_trace_id",
    "push_golden_to_langfuse",
    "push_run_score",
]
