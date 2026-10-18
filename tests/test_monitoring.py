"""monitoring.py 状态滞回与玩家变化检测测试。"""

from __future__ import annotations

import unittest

import _support  # noqa: F401

from minecraft_multi_monitor.models import (
    DisplayOptions,
    ServerConfig,
    ServerSnapshot,
    ServerState,
)
from minecraft_multi_monitor.monitoring import (
    DEFAULT_STATUS_CONFIRM_OFFLINE,
    DEFAULT_STATUS_CONFIRM_ONLINE,
    detect_changes,
    format_server_info,
)


def _make_server() -> ServerConfig:
    return ServerConfig(
        key="srv",
        enabled=True,
        server_name="测试服",
        server_ip="1.2.3.4",
        server_port=25565,
        server_type="java",
        check_interval=45,
        server_address="1.2.3.4",
    )


def _snapshot(
    status: str = "online",
    online: int = 0,
    players: list[str] | None = None,
) -> ServerSnapshot:
    return ServerSnapshot(
        key="srv",
        name="测试服",
        server_ip="1.2.3.4",
        server_port=25565,
        server_type="java",
        status=status,
        motd="",
        version="1.20.1",
        protocol="763",
        online=online,
        max_players=100,
        players=players or [],
        update_time="2026-01-01 00:00:00",
    )


class StatusHysteresisTest(unittest.TestCase):
    def test_first_sample_is_silent(self):
        changed, message = detect_changes(_make_server(), ServerState(), _snapshot("online"))
        self.assertFalse(changed)
        self.assertIsNone(message)

    def test_offline_requires_consecutive_failures(self):
        state = ServerState()
        server = _make_server()
        detect_changes(server, state, _snapshot("online"))

        for _ in range(DEFAULT_STATUS_CONFIRM_OFFLINE - 1):
            changed, _ = detect_changes(server, state, _snapshot("offline"))
            self.assertFalse(changed)

        changed, message = detect_changes(server, state, _snapshot("offline"))
        self.assertTrue(changed)
        self.assertIn("已离线", message or "")

    def test_single_success_does_not_recover(self):
        state = ServerState()
        server = _make_server()
        detect_changes(server, state, _snapshot("offline"))

        for _ in range(DEFAULT_STATUS_CONFIRM_ONLINE - 1):
            changed, _ = detect_changes(server, state, _snapshot("online", 0))
            self.assertFalse(changed)

        changed, message = detect_changes(server, state, _snapshot("online", 0))
        self.assertTrue(changed)
        self.assertIn("已上线", message or "")

    def test_contradiction_counter_resets_on_match(self):
        state = ServerState()
        server = _make_server()
        detect_changes(server, state, _snapshot("online"))

        detect_changes(server, state, _snapshot("offline"))
        detect_changes(server, state, _snapshot("online"))
        for _ in range(DEFAULT_STATUS_CONFIRM_OFFLINE - 1):
            changed, _ = detect_changes(server, state, _snapshot("offline"))
            self.assertFalse(changed)


class PlayerDiffTest(unittest.TestCase):
    def _online(self, players: list[str], online: int | None = None) -> ServerSnapshot:
        return _snapshot("online", online if online is not None else len(players), players)

    def test_join_is_reported(self):
        state = ServerState()
        server = _make_server()
        detect_changes(server, state, self._online([], 0))

        changed, message = detect_changes(server, state, self._online(["Alice"], 1))
        self.assertTrue(changed)
        self.assertIn("Alice", message or "")

    def test_leave_is_reported(self):
        state = ServerState()
        server = _make_server()
        detect_changes(server, state, self._online(["Alice", "Bob"], 2))

        changed, message = detect_changes(server, state, self._online(["Alice"], 1))
        self.assertTrue(changed)
        self.assertIn("Bob", message or "")

    def test_anonymous_placeholder_is_not_reported_as_player_name(self):
        state = ServerState()
        server = _make_server()
        detect_changes(server, state, self._online([], 0))

        # 服务端返回占位名 + 人数增加：不应立刻播报 Anonymous Player
        first = _snapshot("online", 1, ["Anonymous Player"])
        changed, message = detect_changes(server, state, first)
        self.assertFalse(changed)
        self.assertIsNone(message)
        # 加入事件被挂起，等待真实名解析后再播报
        self.assertEqual(state.pending_anonymous, 1)


class FormatServerInfoTest(unittest.TestCase):
    def test_offline_snapshot_shows_error(self):
        snapshot = _snapshot("offline")
        snapshot.error = "连接超时"
        text = format_server_info(snapshot, DisplayOptions())
        self.assertIn("连接超时", text)

    def test_latency_is_displayed_when_present(self):
        snapshot = _snapshot("online")
        snapshot.latency_ms = 42
        text = format_server_info(snapshot, DisplayOptions())
        self.assertIn("42 ms", text)

    def test_player_list_truncated(self):
        players = [f"player{i}" for i in range(10)]
        options = DisplayOptions(show_player_list=True)
        text = format_server_info(_snapshot("online", 10, players), options)
        self.assertIn("+2人", text)


if __name__ == "__main__":
    unittest.main()
