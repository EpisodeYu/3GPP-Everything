"""图片处理。

GSMA `marked/<rel>/<series>/<spec_uid>/*_img.jpg` 即 spec 同目录图片。
本模块负责：
- 通过 `hf_hub_download` 把图片缓存到本地（HF 本地缓存默认即可去重）
- 读 bytes、算 sha256，供 Vision 缓存 key（Redis: `tgpp:vision:{sha256}`）

不在此处调 Vision；那是 ingestion/images/vision.py 的事，本模块只准备 bytes + hash。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import hf_hub_download

GSMA_REPO_ID = "GSMA/3GPP"


@dataclass(frozen=True, slots=True)
class ResolvedImage:
    """单张图片解析结果。"""

    repo_path: str
    local_path: Path
    size: int
    sha256: str


def resolve_image(
    repo_path: str,
    *,
    revision: str | None = None,
    cache_dir: str | Path | None = None,
    token: str | None = None,
) -> ResolvedImage:
    """下载并 hash 单张图片。

    `huggingface_hub` 自带本地缓存（默认 ~/.cache/huggingface/hub）；同 revision
    下重复调用不会重新拉网络。返回值里给出本地 path、size、sha256。
    """
    local = hf_hub_download(
        repo_id=GSMA_REPO_ID,
        filename=repo_path,
        repo_type="dataset",
        revision=revision,
        cache_dir=str(cache_dir) if cache_dir else None,
        token=token,
    )
    local_path = Path(local)
    data = local_path.read_bytes()
    return ResolvedImage(
        repo_path=repo_path,
        local_path=local_path,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def hash_bytes(data: bytes) -> str:
    """暴露 sha256 工具方法，便于 Vision 缓存键统一口径。"""
    return hashlib.sha256(data).hexdigest()
