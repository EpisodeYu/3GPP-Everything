"""Auto-mark all tests under `tests/unit/` with `unit` marker.

避免在每个测试文件顶部手写 `pytestmark = pytest.mark.unit`。
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "/tests/unit/" in str(item.fspath):
            item.add_marker(pytest.mark.unit)
