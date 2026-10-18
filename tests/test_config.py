"""config.py 地址解析 / 默认端口 / IPv6 测试。"""

from __future__ import annotations

import unittest

import _support  # noqa: F401

from minecraft_multi_monitor.address import format_host_port, split_host_port
from minecraft_multi_monitor.config import _parse_server_address, load_servers_from_config
from minecraft_multi_monitor.models import DEFAULT_BEDROCK_PORT, DEFAULT_JAVA_PORT


class ParseServerAddressTest(unittest.TestCase):
    def test_java_default_port(self):
        self.assertEqual(
            _parse_server_address("192.168.1.10"),
            ("192.168.1.10", DEFAULT_JAVA_PORT),
        )

    def test_bedrock_default_port(self):
        self.assertEqual(
            _parse_server_address("192.168.1.10", default_port=DEFAULT_BEDROCK_PORT),
            ("192.168.1.10", DEFAULT_BEDROCK_PORT),
        )

    def test_explicit_port_wins(self):
        self.assertEqual(
            _parse_server_address("play.example.com:25566", default_port=DEFAULT_BEDROCK_PORT),
            ("play.example.com", 25566),
        )

    def test_hostname_with_default_port(self):
        self.assertEqual(
            _parse_server_address("play.example.com"),
            ("play.example.com", DEFAULT_JAVA_PORT),
        )

    def test_srv_record_keeps_placeholder_port(self):
        self.assertEqual(
            _parse_server_address("_minecraft._tcp.example.com", default_port=DEFAULT_BEDROCK_PORT),
            ("_minecraft._tcp.example.com", DEFAULT_BEDROCK_PORT),
        )

    def test_srv_record_ignores_explicit_port(self):
        self.assertEqual(
            _parse_server_address("_minecraft._tcp.example.com:12345"),
            ("_minecraft._tcp.example.com", DEFAULT_JAVA_PORT),
        )

    def test_invalid_values_return_none(self):
        for value in ("", "   ", "host:99999", "host:abc", "[::1", "[]:25565"):
            with self.subTest(value=value):
                self.assertIsNone(_parse_server_address(value))


class Ipv6AddressTest(unittest.TestCase):
    def test_bracketed_ipv6_with_port(self):
        self.assertEqual(
            _parse_server_address("[2001:db8::1]:25566"),
            ("2001:db8::1", 25566),
        )

    def test_bracketed_ipv6_default_port(self):
        self.assertEqual(
            _parse_server_address("[2001:db8::1]", default_port=DEFAULT_BEDROCK_PORT),
            ("2001:db8::1", DEFAULT_BEDROCK_PORT),
        )

    def test_bare_ipv6_keeps_colons_and_uses_default_port(self):
        self.assertEqual(
            _parse_server_address("2001:db8::1"),
            ("2001:db8::1", DEFAULT_JAVA_PORT),
        )

    def test_format_host_port_brackets_ipv6(self):
        self.assertEqual(format_host_port("2001:db8::1", 25565), "[2001:db8::1]:25565")
        self.assertEqual(format_host_port("play.example.com", 25565), "play.example.com:25565")

    def test_split_host_port_roundtrip(self):
        host, port = split_host_port("[2001:db8::1]:25566", default_port=25565)
        self.assertEqual((host, port), ("2001:db8::1", 25566))
        self.assertEqual(format_host_port(host, port), "[2001:db8::1]:25566")


class LoadServersTest(unittest.TestCase):
    def _load(self, item: dict):
        return load_servers_from_config({"server_entries": [item]}, 45)

    def test_bedrock_server_defaults_to_19132(self):
        servers = self._load({"server_name": "bed", "server_address": "1.2.3.4", "server_type": "bedrock"})
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].server_port, DEFAULT_BEDROCK_PORT)
        self.assertEqual(servers[0].server_type, "bedrock")

    def test_bedrock_legacy_fields_default_to_19132(self):
        servers = self._load({"server_name": "bed", "server_ip": "1.2.3.4", "server_type": "bedrock"})
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].server_port, DEFAULT_BEDROCK_PORT)

    def test_java_server_defaults_to_25565(self):
        servers = self._load({"server_name": "java", "server_address": "1.2.3.4", "server_type": "java"})
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].server_port, DEFAULT_JAVA_PORT)

    def test_explicit_port_is_kept_for_bedrock(self):
        servers = self._load(
            {
                "server_name": "bed",
                "server_address": "1.2.3.4:19133",
                "server_type": "bedrock",
            }
        )
        self.assertEqual(servers[0].server_port, 19133)

    def test_ipv6_legacy_fields_are_formatted_with_brackets(self):
        servers = self._load({"server_name": "v6", "server_ip": "2001:db8::1", "server_port": 25566})
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].server_ip, "2001:db8::1")
        self.assertEqual(servers[0].server_port, 25566)
        self.assertEqual(servers[0].server_address, "[2001:db8::1]:25566")

    def test_disabled_server_is_skipped(self):
        servers = self._load({"server_name": "off", "server_address": "1.2.3.4", "enabled": False})
        self.assertEqual(servers, [])


if __name__ == "__main__":
    unittest.main()
