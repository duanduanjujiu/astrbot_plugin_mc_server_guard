"""srv_resolver.py 主机名解析行为测试。"""

from __future__ import annotations

import socket
import unittest
from unittest import mock

import _support  # noqa: F401

from minecraft_multi_monitor import srv_resolver

_IPV6_INFO = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 0, 0, 0))
_IPV4_INFO = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 0))


class ResolveHostTest(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_ipv4_when_aaaa_first(self):
        """回归：旧实现固定返回 infos[0]，AAAA 排在前面时会错误返回 IPv6。"""
        with mock.patch("socket.getaddrinfo", return_value=[_IPV6_INFO, _IPV4_INFO]):
            self.assertEqual(await srv_resolver.resolve_host("example.com"), "192.0.2.1")

    async def test_returns_ipv6_when_only_ipv6(self):
        with mock.patch("socket.getaddrinfo", return_value=[_IPV6_INFO]):
            self.assertEqual(await srv_resolver.resolve_host("v6.example.com"), "2001:db8::1")

    async def test_dns_error_returns_none(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror):
            self.assertIsNone(await srv_resolver.resolve_host("bad.invalid"))


class EnsureSrvRecordNameTest(unittest.TestCase):
    def test_adds_minecraft_prefix(self):
        self.assertEqual(
            srv_resolver.ensure_srv_record_name("play.example.com"),
            "_minecraft._tcp.play.example.com",
        )

    def test_keeps_existing_prefix(self):
        self.assertEqual(
            srv_resolver.ensure_srv_record_name("_minecraft._tcp.play.example.com"),
            "_minecraft._tcp.play.example.com",
        )


if __name__ == "__main__":
    unittest.main()
