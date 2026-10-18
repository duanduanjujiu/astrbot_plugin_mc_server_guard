"""测试引导模块：把仓库根目录加入 sys.path，并安装 AstrBot stub。"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent

for _path in (str(_TESTS_DIR), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from astrbot_stub import install  # noqa: E402  (需要先修正 sys.path)

install()
