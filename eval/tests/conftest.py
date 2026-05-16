"""eval pytest 配置。

照搬 ingestion/tests/conftest.py 的做法：把项目根加入 sys.path，
让 `from eval.retrieval import ...` 在 pytest 里能找到。

不引入 live Qdrant / LiteLLM fixture（M3 单测全部纯函数）；
真正的 retrieval smoke 走 `eval cli retrieval smoke` 手工触发或 pytest -m integration。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
