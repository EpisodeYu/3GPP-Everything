"""Voyage tokenizer 封装。

按 plan §0 / 用户指示，chunker 直接使用 Voyage 的 tokenizer
（`voyageai.Client.tokenize` / `count_tokens`），不再用 tiktoken 近似。

为什么不用 tiktoken：
- Voyage 文档明确说"我们的 tokenizer 与 tiktoken 不同；统计上 Voyage 比 tiktoken
  多 1.1-1.2 ×"。chunker 切分阈值若按 tiktoken 算，会比 Voyage 实际略短，导致
  embedding 时偶发超 chunk 长度；用 Voyage 自己的 tokenizer 才能精确。
- voyageai SDK 的 tokenize 走本地 HF tokenizers，不调网络（首次会从 HF 拉
  voyageai/voyage-4-large 的 tokenizer.json 缓存到 ~/.cache/huggingface）。
- 测得：30k token 单文本 ~1s；1000 短文本 batch ~21ms。chunker 一篇 spec
  调用次数受控，性能足够。

线程安全：模块级单例 Client，第一次调用 lazy 初始化；无 API key 也能跑（tokenize
是纯本地操作，Client 只是壳）。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence

import voyageai

DEFAULT_MODEL = os.environ.get("VOYAGE_TOKENIZER_MODEL") or "voyage-4-large"

_client_lock = threading.Lock()
_client: voyageai.Client | None = None


def _get_client() -> voyageai.Client:
    """单例 Client；tokenize 不需要真实 API key，传 'dummy' 避免环境变量缺失警告。"""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = voyageai.Client(api_key=os.environ.get("VOYAGE_API_KEY") or "dummy")
    return _client


def count_tokens(text: str, *, model: str = DEFAULT_MODEL) -> int:
    """单文本 token 计数。空串返回 0。"""
    if not text:
        return 0
    return _get_client().count_tokens([text], model=model)


def count_tokens_batch(texts: Sequence[str], *, model: str = DEFAULT_MODEL) -> int:
    """多文本 token 计数（总数）。"""
    if not texts:
        return 0
    return _get_client().count_tokens(list(texts), model=model)


def tokenize(text: str, *, model: str = DEFAULT_MODEL):
    """返回单文本的 tokenizers.Encoding 对象。

    Encoding 提供：
    - `.tokens`   : list[str]，每个 token 的字符串形态
    - `.ids`      : list[int]，每个 token 的整数 id
    - `.offsets`  : list[tuple[int, int]]，每个 token 在原文中的 (start, end) 字符偏移

    chunker 用 offsets 做"按 token 强切回溯到字符边界"，避免 split_by_tokens 把
    多字节字符切成半字节。
    """
    if not text:
        return None
    return _get_client().tokenize([text], model=model)[0]


def split_by_tokens(text: str, *, max_tokens: int, model: str = DEFAULT_MODEL) -> list[str]:
    """按 token 上限把长文本切成多片；每片字符边界对齐 token offsets。

    切片只在 token 之间切，不会切坏 UTF-8 多字节字符或 markdown 关键字符。
    返回的每片 token 数 ≤ max_tokens；可能比 max_tokens 略短（因为最后一个
    token 的 end offset 是字符边界）。

    注意：本函数只做"按 token 强切"，不做语义边界（句子/段落）回溯；那由
    section_splitter.py 的 paragraph fallback 链负责。
    """
    if not text:
        return []
    if max_tokens <= 0:
        raise ValueError(f"max_tokens must be > 0, got {max_tokens}")

    enc = tokenize(text, model=model)
    if enc is None:
        return []
    offsets = enc.offsets
    if len(offsets) <= max_tokens:
        return [text]

    pieces: list[str] = []
    cursor_token = 0
    n_tokens = len(offsets)
    while cursor_token < n_tokens:
        end_token = min(cursor_token + max_tokens, n_tokens)
        # offsets[cursor_token] 起点；offsets[end_token-1] 终点
        start_char = offsets[cursor_token][0]
        end_char = offsets[end_token - 1][1]
        pieces.append(text[start_char:end_char])
        cursor_token = end_token
    return pieces


def truncate_to_tokens(text: str, *, max_tokens: int, model: str = DEFAULT_MODEL) -> str:
    """把文本截断到 max_tokens 以内，返回前 max_tokens 个 token 对应的子串。"""
    if not text or max_tokens <= 0:
        return ""
    enc = tokenize(text, model=model)
    if enc is None:
        return ""
    offsets = enc.offsets
    if len(offsets) <= max_tokens:
        return text
    end_char = offsets[max_tokens - 1][1]
    return text[:end_char]
