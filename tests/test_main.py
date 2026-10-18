"""main.py 运行时行为测试：通知队列、错误分类、配置重载、查询限流。

这些测试不启动 AstrBot：AstrBot / aiohttp 由 ``_support`` 注入的 stub 提供。
"""

from __future__ import annotations

import asyncio
import unittest

import _support  # noqa: F401

import main as plugin_module
from minecraft_multi_monitor.models import (
    DisplayOptions,
    FetchResult,
    PluginSettings,
    ServerConfig,
    ServerSnapshot,
)


def _make_plugin() -> plugin_module.MyPlugin:
    """构造一个不经过 AstrBot 加载流程的插件实例（只填测试需要的属性）。"""
    plugin = plugin_module.MyPlugin.__new__(plugin_module.MyPlugin)
    plugin.context = None
    plugin.config = {}
    plugin.settings = PluginSettings(
        target_group=None,
        target_umo=None,
        enable_auto_monitor=False,
        global_check_interval=45,
        display_options=DisplayOptions(),
    )
    plugin.servers = []
    plugin.server_states = {}
    plugin.server_locks = {}
    plugin.status_client = None
    plugin.task = None
    plugin._delayed_start_task = None
    plugin._terminated = False
    plugin._umo_override = None
    plugin._group_override = None
    plugin._notify_queue = asyncio.Queue(maxsize=2)
    plugin._notify_task = None
    plugin._last_query_at = 0.0
    return plugin


def _make_server(name: str = "测试服") -> ServerConfig:
    return ServerConfig(
        key="srv",
        enabled=True,
        server_name=name,
        server_ip="1.2.3.4",
        server_port=25565,
        server_type="java",
        check_interval=45,
        server_address="1.2.3.4",
    )


def _online_snapshot() -> ServerSnapshot:
    return ServerSnapshot(
        key="srv",
        name="测试服",
        server_ip="1.2.3.4",
        server_port=25565,
        server_type="java",
        status="online",
        motd="",
        version="1.20.1",
        protocol="763",
        online=0,
        max_players=100,
        players=[],
        update_time="2026-01-01 00:00:00",
    )


class _FakeStatusClient:
    def __init__(self, result: FetchResult):
        self.result = result
        self.quiet_flags: list[bool] = []

    async def fetch_status(self, server, source, *, quiet=False):
        self.quiet_flags.append(quiet)
        return self.result


class NotificationQueueTest(unittest.IsolatedAsyncioTestCase):
    async def test_notifications_are_sent_in_order(self):
        plugin = _make_plugin()
        sent: list[str] = []

        async def fake_send(text: str) -> bool:
            sent.append(text)
            return True

        plugin._send_notification = fake_send  # type: ignore[method-assign]
        plugin._queue_notification("first")
        plugin._queue_notification("second")

        await asyncio.wait_for(plugin._notify_queue.join(), timeout=2)
        self.assertEqual(sent, ["first", "second"])

        assert plugin._notify_task is not None
        plugin._notify_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await plugin._notify_task

    async def test_full_queue_drops_oldest(self):
        plugin = _make_plugin()
        # 用一个未完成的 Future 冒充正在运行的 worker，避免真的消费队列
        plugin._notify_task = asyncio.get_running_loop().create_future()

        plugin._queue_notification("a")
        plugin._queue_notification("b")
        plugin._queue_notification("c")  # 队列已满 → 丢弃 a

        self.assertEqual(plugin._notify_queue.qsize(), 2)
        self.assertEqual(plugin._notify_queue.get_nowait(), "b")
        self.assertEqual(plugin._notify_queue.get_nowait(), "c")


class FetchResultHandlingTest(unittest.IsolatedAsyncioTestCase):
    async def test_failure_synthesizes_offline_snapshot(self):
        plugin = _make_plugin()
        plugin.status_client = _FakeStatusClient(FetchResult(None, "timeout", "查询超时（5s）"))

        result = await plugin._fetch_server_data(_make_server(), "test")

        self.assertIsNotNone(result.snapshot)
        self.assertEqual(result.snapshot.status, "offline")
        self.assertEqual(result.error_kind, "timeout")
        self.assertIn("查询超时", result.snapshot.error or "")

    async def test_resolve_failure_has_no_snapshot(self):
        plugin = _make_plugin()
        plugin.status_client = _FakeStatusClient(FetchResult(None, "resolve", "地址解析失败"))

        result = await plugin._fetch_server_data(_make_server(), "test")

        self.assertIsNone(result.snapshot)
        self.assertEqual(result.error_kind, "resolve")

    async def test_tick_skips_on_resolve_failure(self):
        plugin = _make_plugin()
        plugin.servers = [_make_server()]
        plugin.status_client = _FakeStatusClient(FetchResult(None, "resolve", "地址解析失败"))
        state = plugin.server_states.setdefault("srv", plugin_module.ServerState())

        await plugin._monitor_tick(plugin.servers[0], state)

        self.assertIsNone(state.stable_status)
        self.assertEqual(state.consecutive_failures, 0)

    async def test_tick_queues_notification_on_status_change(self):
        plugin = _make_plugin()
        plugin.servers = [_make_server()]
        plugin.status_client = _FakeStatusClient(FetchResult(_online_snapshot()))

        queued: list[str] = []
        plugin._queue_notification = queued.append  # type: ignore[method-assign]

        state = plugin.server_states.setdefault("srv", plugin_module.ServerState())
        state.stable_status = "offline"
        state.contradict_probes = 0
        state.last_check_ts = 0.0

        # 恢复在线需要连续 2 次确认：第一次不推送，第二次推送
        await plugin._monitor_tick(plugin.servers[0], state)
        self.assertEqual(queued, [])

        state.last_check_ts = 0.0
        await plugin._monitor_tick(plugin.servers[0], state)
        self.assertEqual(len(queued), 1)
        self.assertIn("已上线", queued[0])

    async def test_safe_tick_isolates_exceptions(self):
        plugin = _make_plugin()

        async def boom(server, state):
            raise RuntimeError("boom")

        plugin._monitor_tick = boom  # type: ignore[method-assign]
        await plugin._safe_monitor_tick(_make_server())  # 不应抛出


class ReloadConfigTest(unittest.IsolatedAsyncioTestCase):
    async def test_reload_rebuilds_servers(self):
        plugin = _make_plugin()
        plugin.config = {
            "check_interval": 30,
            "server_entries": [
                {"server_name": "bed", "server_address": "1.2.3.4", "server_type": "bedrock"},
            ],
        }

        plugin._reload_runtime()

        self.assertEqual(len(plugin.servers), 1)
        self.assertEqual(plugin.servers[0].server_port, 19132)
        self.assertEqual(plugin.settings.global_check_interval, 30)
        self.assertEqual(set(plugin.server_states), {"bed"})


class QueryCooldownTest(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, plugin) -> list[str]:
        class _Event:
            def plain_result(self, text):
                return text

        return [item async for item in plugin.query_server_status(_Event())]

    async def test_second_query_within_cooldown_is_rejected(self):
        plugin = _make_plugin()
        plugin._last_query_at = asyncio.get_running_loop().time()

        replies = await self._collect(plugin)
        self.assertEqual(len(replies), 1)
        self.assertIn("查询太频繁", replies[0])

    async def test_query_without_servers_reports_empty_config(self):
        plugin = _make_plugin()
        plugin._last_query_at = 0.0

        replies = await self._collect(plugin)
        self.assertEqual(len(replies), 1)
        self.assertIn("没有启用的服务器配置", replies[0])


class StaleModulePurgeTest(unittest.TestCase):
    """热更新时清理 sys.modules 中旧子包缓存（v1.2.1 修复）。"""

    def _install_fake_module(self, name: str, file_path: str):
        import sys
        import types

        module = types.ModuleType(name)
        module.__file__ = file_path  # type: ignore[attr-defined]
        sys.modules[name] = module
        return module

    def test_modules_inside_plugin_dir_are_purged(self):
        import sys
        from pathlib import Path

        package_dir = Path(plugin_module.PLUGIN_DIR) / "fake_stale_pkg"
        self._install_fake_module("fake_stale_pkg", str(package_dir / "__init__.py"))
        self._install_fake_module("fake_stale_pkg.mod", str(package_dir / "mod.py"))

        try:
            plugin_module._purge_stale_package("fake_stale_pkg")
            self.assertNotIn("fake_stale_pkg", sys.modules)
            self.assertNotIn("fake_stale_pkg.mod", sys.modules)
        finally:
            sys.modules.pop("fake_stale_pkg", None)
            sys.modules.pop("fake_stale_pkg.mod", None)

    def test_modules_outside_plugin_dir_are_kept(self):
        import sys

        self._install_fake_module("fake_other_pkg", "/opt/other_plugin/fake_other_pkg/__init__.py")

        try:
            plugin_module._purge_stale_package("fake_other_pkg")
            self.assertIn("fake_other_pkg", sys.modules)
        finally:
            sys.modules.pop("fake_other_pkg", None)


if __name__ == "__main__":
    unittest.main()
