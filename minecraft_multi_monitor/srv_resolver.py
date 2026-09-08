"""SRV 记录解析工具。

通过 dnspython 异步查询 _minecraft._tcp.<hostname> 的 SRV 记录，
得到 Minecraft 服务器的真实 IP 与端口。
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass

import dns.asyncresolver
import dns.exception
import dns.rdatatype
import dns.resolver

from astrbot.api import logger


@dataclass(slots=True)
class SrvTarget:
    """单个 SRV 解析结果。

    Minecraft SRV 记录的 target 是主机名（不一定是 IP），port 是端口，
    weight/priority 用于负载均衡，本插件只取第一个返回结果。
    """
    host: str
    port: int
    priority: int
    weight: int


def ensure_srv_record_name(name: str) -> str:
    """保证 SRV 记录以 _minecraft._tcp. 开头。

    Minecraft 客户端连接域名时，自动查询 `_minecraft._tcp.<domain>`。
    如果用户在 GUI 里只填了 `play.example.com`，这里补全前缀。
    """
    name = name.strip().rstrip(".")
    if not name:
        return name
    if name.startswith("_minecraft._tcp."):
        return name
    if name.startswith("_tcp.") or name.startswith("_minecraft."):
        # 用户只填了部分前缀（例如 `_minecraft.example.com` 或 `_tcp.example.com`）
        # 这种情况比较少见，但仍按原样处理，避免改坏用户配置
        return name
    return f"_minecraft._tcp.{name}"


async def resolve_srv(record_name: str, *, timeout: float = 5.0) -> SrvTarget | None:
    """异步解析 Minecraft SRV 记录，返回最优目标。

    Args:
        record_name: SRV 记录名，可省略 _minecraft._tcp. 前缀。
        timeout: DNS 查询超时（秒）。

    Returns:
        SrvTarget 或 None（解析失败/无记录）。
    """
    full_name = ensure_srv_record_name(record_name)

    loop = asyncio.get_running_loop()
    resolver = dns.asyncresolver.Resolver()
    # 让 resolver 在异步上下文里使用，避免阻塞事件循环
    resolver.lifetime = timeout
    resolver.timeout = timeout

    try:
        # dns.asyncresolver 默认会在 asyncio loop 里跑；这里显式 await 即可
        answer = await resolver.resolve(full_name, rdtype=dns.rdatatype.SRV, lifetime=timeout)
    except dns.resolver.NXDOMAIN:
        logger.info(f"SRV 解析: {full_name} 不存在 (NXDOMAIN)")
        return None
    except dns.resolver.NoAnswer:
        logger.info(f"SRV 解析: {full_name} 无 SRV 记录 (NoAnswer)")
        return None
    except dns.exception.Timeout:
        logger.warning(f"SRV 解析: {full_name} 超时（{timeout}s）")
        return None
    except Exception as exc:
        logger.warning(f"SRV 解析: {full_name} 异常: {exc}")
        return None

    if not answer:
        return None

    # 挑选 priority 最低（最高优先级）的记录；如果 priority 相同则按 weight 比例挑选。
    # 为简化，本插件直接取排序后的第一条结果。
    records = sorted(
        answer,
        key=lambda r: (r.priority, -r.weight),
    )
    top = records[0]

    # SRV target 是主机名，需要再解析为 A/AAAA 记录
    host = str(top.target).rstrip(".")
    if not host:
        logger.warning(f"SRV 解析: {full_name} 目标主机为空")
        return None

    return SrvTarget(
        host=host,
        port=int(top.port),
        priority=int(top.priority),
        weight=int(top.weight),
    )


async def resolve_host(host: str, *, timeout: float = 3.0) -> str | None:
    """把主机名解析为 IP（A/AAAA 记录），用 socket.getaddrinfo 在线程池里跑。

    Returns:
        IP 字符串或 None。
    """
    def _do() -> str | None:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return None
        if not infos:
            return None
        # 优先 IPv4
        for family, *_ in infos:
            if family == socket.AF_INET:
                return infos[0][4][0]
        return infos[0][4][0]

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_do),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(f"主机解析 {host} 超时（{timeout}s）")
        return None


async def resolve_srv_with_ip(record_name: str, *, timeout: float = 5.0) -> tuple[str, int] | None:
    """一次性解析：SRV → 主机名 → IP，返回 (ip, port) 元组。

    若 SRV 不存在则返回 None；调用方应 fallback 到默认的 server_ip / server_port。
    """
    target = await resolve_srv(record_name, timeout=timeout)
    if target is None:
        return None

    ip = await resolve_host(target.host, timeout=timeout)
    if ip is None:
        # fallback：直接把 SRV target 当作 IP 字符串使用（若它本身就是 IP）
        try:
            socket.inet_aton(target.host)
            return target.host, target.port
        except OSError:
            logger.warning(
                f"SRV 解析: 主机名 {target.host} 既无法解析为 IP 也不是 IPv4 字面量"
            )
            return None

    return ip, target.port


async def resolve_server_target(
    address: str,
    fallback_port: int,
    *,
    timeout: float = 5.0,
) -> tuple[str, int, bool]:
    """统一的目标服务器解析入口。

    根据 address 的形式选择不同解析路径：
      - SRV 记录名（以 `_` 开头）：查 SRV → 主机名 → IP
      - 已经是 IP 字面量：直接返回 (ip, fallback_port, False)
      - 普通域名：解析 A/AAAA 记录 → IP

    Args:
        address: 用户填的 server_address 字符串，可包含 :port 后缀
            （对 SRV 路径该 :port 会被忽略）。
        fallback_port: 当用户没指定端口时使用的端口（通常来自配置或默认值 25565）。
            对 SRV 路径该参数被忽略（端口由 SRV 记录决定）。
        timeout: DNS 解析超时。

    Returns:
        (ip, port, used_srv) 三元组：
          - ip: 最终用于连接的 IP 地址
          - port: 最终用于连接的端口
          - used_srv: 是否成功通过 SRV 解析得到结果

        解析失败时返回 ("", 0, False)，调用方应 fallback 到原始配置。
    """
    address = (address or "").strip().rstrip(".")
    if not address:
        return "", 0, False

    # 0) 拆分可选的 :port 后缀（除 SRV 路径外）
    host_part = address
    explicit_port: int | None = None
    if ":" in address and not address.startswith("_"):
        host_part, port_str = address.rsplit(":", 1)
        host_part = host_part.strip().rstrip(".")
        try:
            p = int(port_str)
            if 1 <= p <= 65535:
                explicit_port = p
        except ValueError:
            explicit_port = None
        if explicit_port is None:
            # 端口无效时整体视为无效
            return "", 0, False

    port_to_use = explicit_port if explicit_port is not None else fallback_port

    # 1) SRV 记录路径（端口由 SRV 决定，忽略用户填的 :port）
    if address.startswith("_"):
        resolved = await resolve_srv_with_ip(address, timeout=timeout)
        if resolved is None:
            return "", 0, False
        ip, port = resolved
        return ip, port, True

    if not host_part:
        return "", 0, False

    # 2) 已经是 IPv4/IPv6 字面量
    try:
        socket.inet_pton(socket.AF_INET, host_part)
        return host_part, port_to_use, False
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host_part)
        return host_part, port_to_use, False
    except OSError:
        pass

    # 3) 普通主机名 → 解析 A/AAAA 记录
    ip = await resolve_host(host_part, timeout=timeout)
    if ip is None:
        # 4) 用户填的可能是 Minecraft 标准域名（如 play.example.com），但域名本身
        #    没有 A 记录（典型情况：通过 SRV 指向内网/FRP 后端）。
        #    自动尝试 SRV 解析（补全 _minecraft._tcp. 前缀）。
        srv_name = f"_minecraft._tcp.{host_part}"
        srv_resolved = await resolve_srv_with_ip(srv_name, timeout=timeout)
        if srv_resolved is not None:
            return srv_resolved[0], srv_resolved[1], True
        return "", 0, False
    return ip, port_to_use, False