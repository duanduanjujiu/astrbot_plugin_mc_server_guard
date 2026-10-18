"""测试用的 AstrBot API 最小 stub。

单元测试只覆盖纯逻辑模块（协议解析、配置解析、状态变化检测），
不需要启动 AstrBot。若环境中已安装真实 AstrBot，则直接使用真实模块。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types


def _make_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    return module


def install() -> None:
    """安装 stub（幂等）。真实 AstrBot / aiohttp 可导入时不做任何事。"""
    _install_astrbot_stub()
    _install_aiohttp_stub()


def _install_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return
    if importlib.util.find_spec("astrbot") is not None:
        return

    astrbot = _make_module("astrbot")

    api = _make_module("astrbot.api")
    api.logger = logging.getLogger("astrbot_stub")
    api.logger.addHandler(logging.NullHandler())
    api.logger.propagate = False

    class AstrBotConfig(dict):
        """AstrBot 配置对象 stub（真实实现是 dict 子类）。"""

    api.AstrBotConfig = AstrBotConfig

    event = _make_module("astrbot.api.event")

    class AstrMessageEvent:
        """消息事件 stub。"""

    class MessageChain:
        """消息链 stub。"""

        def message(self, text: str) -> "MessageChain":
            return self

    event.AstrMessageEvent = AstrMessageEvent
    event.MessageChain = MessageChain

    filter_pkg = _make_module("astrbot.api.event.filter")

    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    class PlatformAdapterType:
        AIOCQHTTP = "aiocqhttp"
        QQ_OFFICIAL = "qq_official"
        SATORI = "satori"

    def _decorator(*args, **kwargs):
        def wrapper(func):
            return func

        return wrapper

    filter_pkg.command = _decorator
    filter_pkg.command_group = _decorator
    filter_pkg.permission_type = _decorator
    filter_pkg.PermissionType = PermissionType
    filter_pkg.PlatformAdapterType = PlatformAdapterType
    event.filter = filter_pkg

    star = _make_module("astrbot.api.star")

    class Context:
        """插件上下文 stub。"""

    class Star:
        """插件基类 stub。"""

        def __init__(self, context=None):
            self.context = context

        async def initialize(self) -> None:
            return None

        async def terminate(self) -> None:
            return None

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name: str | None = None):
            return None

    def register(*args, **kwargs):
        def wrapper(cls):
            return cls

        return wrapper

    star.Context = Context
    star.Star = Star
    star.StarTools = StarTools
    star.register = register

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.event.filter"] = filter_pkg
    sys.modules["astrbot.api.star"] = star


def _install_aiohttp_stub() -> None:
    """aiohttp 仅被 "一言" 功能使用；测试环境没装时注入最小 stub。"""
    if "aiohttp" in sys.modules:
        return
    if importlib.util.find_spec("aiohttp") is not None:
        return

    aiohttp = _make_module("aiohttp")

    class ClientTimeout:
        def __init__(self, total=None, **kwargs):
            self.total = total
            self.kwargs = kwargs

    class ClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, *args, **kwargs):
            raise RuntimeError("aiohttp stub: 测试中禁止真实网络请求")

    aiohttp.ClientTimeout = ClientTimeout
    aiohttp.ClientSession = ClientSession
    sys.modules["aiohttp"] = aiohttp
