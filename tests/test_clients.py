"""clients.py 协议解析与读取行为测试（不依赖真实 Minecraft 服务器）。"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import _support  # noqa: F401  (安装 stub + 修正 sys.path)

from minecraft_multi_monitor import clients
from minecraft_multi_monitor.models import ServerConfig, ServerSnapshot


def _make_server(port: int = 25565, server_type: str = "java") -> ServerConfig:
    return ServerConfig(
        key="test",
        enabled=True,
        server_name="test-server",
        server_ip="127.0.0.1",
        server_port=port,
        server_type=server_type,
        check_interval=45,
        server_address="",
    )


def _legacy_response_body(
    protocol: str = "127",
    version: str = "1.20.1",
    motd: str = "Hello MOTD",
    online: str = "3",
    max_players: str = "20",
) -> bytes:
    """构造 1.6 风格的 Legacy Ping 响应体（不含首字节 0xFF）。"""
    text = "\x00".join([protocol, version, motd, online, max_players])
    return text.encode("utf-16-be")


def _legacy_response(
    protocol: str = "127",
    version: str = "1.20.1",
    motd: str = "Hello MOTD",
    online: str = "3",
    max_players: str = "20",
) -> bytes:
    return b"\xff" + _legacy_response_body(protocol, version, motd, online, max_players)


class LegacyPayloadParseTest(unittest.TestCase):
    def test_full_payload(self):
        self.assertEqual(
            clients._parse_legacy_payload(_legacy_response_body()),
            ("127", "1.20.1", "Hello MOTD", 3, 20),
        )

    def test_payload_without_players(self):
        protocol, version, motd, online, max_players = clients._parse_legacy_payload(
            _legacy_response_body(protocol="47", version="1.8.9", motd="", online="0", max_players="50")
        )
        self.assertEqual((version, online, max_players), ("1.8.9", 0, 50))
        self.assertEqual(protocol, "47")
        self.assertEqual(motd, "")


class LegacyPingReadTest(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_response_is_read_completely(self):
        """响应被拆成两个 TCP 段时也必须完整读取（回归：旧实现单次 read() 会截断）。"""
        response = _legacy_response()

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            split = len(response) // 2
            writer.write(response[:split])
            await writer.drain()
            await asyncio.sleep(0.1)
            writer.write(response[split:])
            await writer.drain()
            await asyncio.sleep(0.1)
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = clients.LocalStatusClient(timeout=2.0)
            snapshot = await client._fetch_java_legacy(_make_server(port), "test", "127.0.0.1", port)
        finally:
            server.close()
            await server.wait_closed()

        self.assertEqual(snapshot.status, "online")
        self.assertEqual(snapshot.online, 3)
        self.assertEqual(snapshot.max_players, 20)
        self.assertEqual(snapshot.motd, "Hello MOTD")
        self.assertEqual(snapshot.version, "1.20.1")


class JavaFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_does_not_fall_back_to_legacy(self):
        """超时表示目标不可达，不应再消耗一个 Legacy Ping 超时预算。"""
        calls = {"slp": 0, "legacy": 0}

        async def fake_slp(*args, **kwargs):
            calls["slp"] += 1
            raise asyncio.TimeoutError()

        async def fake_legacy(*args, **kwargs):
            calls["legacy"] += 1
            raise asyncio.TimeoutError()

        client = clients.LocalStatusClient(timeout=0.05)
        client._fetch_java_slp = fake_slp  # type: ignore[method-assign]
        client._fetch_java_legacy = fake_legacy  # type: ignore[method-assign]

        with mock.patch.object(clients, "RETRY_BACKOFF", 0):
            result = await client.fetch_status(_make_server(), "test")

        self.assertIsNone(result)
        self.assertEqual(calls["slp"], clients.RETRY_COUNT)
        self.assertEqual(calls["legacy"], 0)

    async def test_protocol_error_still_falls_back_to_legacy(self):
        """非超时错误（如响应包异常）仍应回退到 Legacy Ping。"""
        expected = ServerSnapshot(
            key="test",
            name="test-server",
            server_ip="127.0.0.1",
            server_port=25565,
            server_type="java",
            status="online",
            motd="legacy",
            version="1.6.4",
            protocol="78",
            online=0,
            max_players=20,
            players=[],
            software="未知",
            map_name="未知",
            update_time="2026-01-01 00:00:00",
        )
        calls = {"slp": 0, "legacy": 0}

        async def fake_slp(*args, **kwargs):
            calls["slp"] += 1
            raise ValueError("bad packet")

        async def fake_legacy(*args, **kwargs):
            calls["legacy"] += 1
            return expected

        client = clients.LocalStatusClient(timeout=0.05)
        client._fetch_java_slp = fake_slp  # type: ignore[method-assign]
        client._fetch_java_legacy = fake_legacy  # type: ignore[method-assign]

        with mock.patch.object(clients, "RETRY_BACKOFF", 0):
            result = await client.fetch_status(_make_server(), "test")

        self.assertIs(result, expected)
        self.assertEqual(calls["slp"], 1)
        self.assertEqual(calls["legacy"], 1)


if __name__ == "__main__":
    unittest.main()
