"""tests/eval 共享 fixture：复用 integration 的 app_and_state / client / db_session。

直接 import 同名 fixture 函数 → pytest 收集时识别成本目录的 fixture。这种方式
绕开 pytest 8.x "pytest_plugins 必须放顶层 conftest" 的限制，且不会重复注册插件。

sys.path 注入：backend 项目独立 venv 不含 `eval` 包；eval marker 测试要 import
`eval.runner`，故先把 repo root 加进 sys.path。后续考虑改 backend pyproject 加
eval 路径依赖，目前 M7.1 不动 deps。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 复用 integration conftest 的 fixture：直接重命名 import 即可
from tests.integration.conftest import (  # noqa: E402, F401
    app_and_state,
    client,
    db_session,
    event_loop,
)
