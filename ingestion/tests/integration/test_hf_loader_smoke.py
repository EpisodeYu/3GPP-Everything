"""HF 真实拉取烟雾测试。

走真实 HF API，确认 38.211（最小的 5G 物理层规范）能：
- 在 HF tree 里找到 raw.md
- iter_specs 能流式产出 SpecBundle
- raw.md 解析到的 sections ≥ 5
- 同目录图片可下载，sha256 长 64

默认会跳过；CI / 本地需要时设 `RUN_HF_INTEGRATION=1`。
"""

from __future__ import annotations

import os

import pytest

from ingestion.hf_loader import GsmaHfLoader, resolve_image
from ingestion.hf_loader.loader import _basename

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_HF_INTEGRATION") != "1",
    reason="set RUN_HF_INTEGRATION=1 to run real HF API tests",
)


@pytest.mark.integration
def test_loader_can_pin_revision_and_load_38211():
    loader = GsmaHfLoader()  # 自动 pin 当前 main
    assert loader.revision and len(loader.revision) >= 7

    entries, stats = loader.build_manifest(
        releases=["Rel-19"], include_original=False, progress=False
    )
    assert stats.raw_entries > 0
    spec_38211 = next((e for e in entries if e.spec_uid == "38211"), None)
    assert spec_38211 is not None
    assert spec_38211.spec_id == "38.211"
    assert _basename(spec_38211.raw_md_path) == "raw.md"
    # 38.211 图片应为 3 张（README 上对的口径）
    assert 1 <= spec_38211.image_count <= 10

    # 流式加载这一篇
    bundle = next(iter(loader.iter_specs([spec_38211])))
    assert bundle.spec_id == "38.211"
    assert len(bundle.sections) >= 5
    # 至少有一个章节带 clause（如 "1 Scope"）
    assert any(s.clause for s in bundle.sections)


@pytest.mark.integration
def test_resolve_image_returns_stable_hash():
    loader = GsmaHfLoader()
    entries, _ = loader.build_manifest(releases=["Rel-19"], include_original=False, progress=False)
    spec_38211 = next(e for e in entries if e.spec_uid == "38211")
    assert spec_38211.image_paths, "expected 38211 to have images"
    img = resolve_image(spec_38211.image_paths[0], revision=loader.revision)
    assert img.size > 0
    assert len(img.sha256) == 64
