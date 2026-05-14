"""ingestion pytest 配置。

把项目根加入 sys.path，让 `from ingestion.hf_loader import ...` 在 pytest 里能找到。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
