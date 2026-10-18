"""服务器地址解析工具：IPv4 / IPv6 / 域名 + 可选端口。

Minecraft 服务器地址存在多种写法：
- IPv4: ``192.168.1.10``、``192.168.1.10:25566``
- 域名: ``play.example.com``、``play.example.com:25566``
- IPv6: ``[2001:db8::1]``、``[2001:db8::1]:25566``（裸 IPv6 字面量按主机处理，
  端口需要用方括号写法显式指定，避免 ``2001:db8::1:25566`` 的二义性）

本模块提供统一的拆分 / 格式化实现，供配置解析、SRV 解析和状态客户端复用。
"""

from __future__ import annotations

import socket


def is_ipv4_literal(value: str) -> bool:
    """是否为 IPv4 字面量。"""
    if not value:
        return False
    try:
        socket.inet_pton(socket.AF_INET, value)
        return True
    except OSError:
        return False


def is_ipv6_literal(value: str) -> bool:
    """是否为 IPv6 字面量（允许带 zone id，如 ``fe80::1%eth0``）。"""
    if not value:
        return False
    host = value.split("%", 1)[0]
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return True
    except OSError:
        return False


def is_ip_literal(value: str) -> bool:
    """是否为 IPv4 或 IPv6 字面量。"""
    return is_ipv4_literal(value) or is_ipv6_literal(value)


def _parse_port(port_text: str) -> int | None:
    try:
        port = int(port_text)
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    return port


def split_host_port(address: str, *, default_port: int) -> tuple[str, int] | None:
    """把地址拆分为 ``(host, port)``；非法输入返回 ``None``。

    - ``[2001:db8::1]:25566`` → ``("2001:db8::1", 25566)``
    - ``[2001:db8::1]`` → ``("2001:db8::1", default_port)``
    - 裸 IPv6 ``2001:db8::1`` → ``("2001:db8::1", default_port)``
      （裸 IPv6 中的冒号不当作端口分隔符）
    - ``host:port`` / ``host`` 按常规处理
    """
    text = (address or "").strip().rstrip(".")
    if not text:
        return None

    if text.startswith("["):
        end = text.find("]")
        if end <= 1:
            return None
        host = text[1:end].strip()
        rest = text[end + 1 :].strip()
        if not is_ipv6_literal(host):
            return None
        if not rest:
            return host, default_port
        if not rest.startswith(":"):
            return None
        port = _parse_port(rest[1:])
        return (host, port) if port is not None else None

    if is_ipv6_literal(text):
        return text, default_port

    if ":" in text:
        host, port_text = text.rsplit(":", 1)
        host = host.strip()
        port = _parse_port(port_text)
        if not host or port is None:
            return None
        return host, port

    return text, default_port


def format_host_port(host: str, port: int) -> str:
    """按网络地址惯例格式化 ``host:port``（IPv6 自动加方括号）。"""
    if is_ipv6_literal(host):
        return f"[{host}]:{port}"
    return f"{host}:{port}"
