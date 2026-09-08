"""运行时推送目标持久化。

聊天下令（``/推送目标``）设置的推送目标需要跨插件重载 / 进程重启保留，
因此写入 ``data/plugin_data/astrbot_plugin_mc_server_guard/relay_state.json``。

优先级（见 main.py）：
    运行时覆盖（本文件） > WebUI 面板配置（target_umo / target_group）

存储内容：
    {
        "umo": "atri:GroupMessage:1092815819",   # UMO 目标（可选）
        "group_id": "1092815819"                   # 旧版纯 QQ 群号目标（可选）
    }
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

_PLUGIN_NAME = "astrbot_plugin_mc_server_guard"
_STATE_FILE = "relay_state.json"


@dataclass(slots=True)
class RuntimeTarget:
    """运行时（聊天下令）设置的推送目标。两个字段至多一个非空。"""

    umo: str | None = None
    group_id: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.umo and not self.group_id


def _state_path() -> Path | None:
    """返回 state 文件路径；找不到 data 目录时返回 None（退化为不持久化）。"""
    try:
        from astrbot.api.star import StarTools

        data_dir = StarTools.get_data_dir(_PLUGIN_NAME)
        if data_dir:
            return Path(str(data_dir)) / _STATE_FILE
    except Exception:
        pass

    # 兜底：<数据根>/plugin_data/<plugin_name>/
    try:
        plugin_root = Path(__file__).resolve().parents[1]  # .../data/plugins/<plugin_name>
        data_root = plugin_root.parents[1]  # .../data
        candidate = data_root / "plugin_data" / _PLUGIN_NAME / _STATE_FILE
        if plugin_root.name == _PLUGIN_NAME and data_root.name == "data":
            return candidate
    except Exception:
        pass
    return None


def load_runtime_target() -> RuntimeTarget:
    path = _state_path()
    if path is None or not path.exists():
        return RuntimeTarget()
    try:
        payload: dict[str, Any] = json.loads(
            path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"读取运行时推送目标失败({path}): {exc}")
        return RuntimeTarget()
    umo = payload.get("umo")
    group_id = payload.get("group_id")
    return RuntimeTarget(
        umo=str(umo).strip() if isinstance(umo, str) and umo.strip() else None,
        group_id=(
            str(group_id).strip()
            if isinstance(group_id, str) and group_id.strip().isdigit()
            else None
        ),
    )


def save_runtime_target(*, umo: str | None = None, group_id: str | None = None) -> None:
    """原子写回运行时推送目标。umo 与 group_id 至多传一个（另一个会被清空）。"""
    path = _state_path()
    if path is None:
        logger.warning("无法定位插件 data 目录，推送目标将不持久化（仅本次运行生效）。")
        return
    if umo and group_id:
        logger.warning("umo 与 group_id 同时传入，忽略 group_id。")
        group_id = None
    payload = {
        "umo": umo.strip() if umo else "",
        "group_id": group_id.strip() if group_id else "",
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".relay_state.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_name, str(path))
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        logger.info(f"已保存运行时推送目标: {payload}")
    except OSError as exc:
        logger.error(f"保存运行时推送目标失败: {exc}")


def clear_runtime_target() -> None:
    """清除运行时覆盖（恢复使用 WebUI 面板配置）。"""
    save_runtime_target(umo="")
