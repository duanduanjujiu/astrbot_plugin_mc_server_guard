"""SRV 记录解析工具。

通过 dnspython 异步查询 _minecraft._tcp.<hostname> 的 SRV 记录，
得到 Minecraft 服务器的真实 IP 与端口。
"""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass

import dns.asyncresolver
import dns.exception
import dns.rdatatype
import dns.resolver
from astrbot.api import logger

from .address import is_ip_literal, split_host_port

# 解析结果缓存（秒）：监控每轮都要探测，缓存可避免每轮都做一次 DNS 查询。
# 成功结果缓存 5 分钟（兼顾动态 DNS 变更），失败结果短缓存 30 秒（避免故障期
# 反复等待 DNS 超时，但又能在恢复后较快重试）。
RESOLVE_CACHE_TTL = 300.0
NEGATIVE_CACHE_TTL = 30.0

# key: (address, fallback_port) → (expires_at, (ip, port, used_srv) | None)
_resolve_cache: dict[tuple[str, int], tuple[float, tuple[str, int, bool] | None]] = {}


def clear_resolve_cache() -> None:
    """清空解析缓存（插件重载配置后调用，让修改立即生效）。"""
    _resolve_cache.clear()


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
        # 优先 IPv4：必须返回当前命中的 AF_INET 项，而不是固定的 infos[0]，
        # 否则当系统把 AAAA 记录排在前面时会错误地返回 IPv6 地址。
        for info in infos:
            if info[0] == socket.AF_INET:
                return info[4][0]
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
            logger.warning(f"SRV 解析: 主机名 {target.host} 既无法解析为 IP 也不是 IPv4 字面量")
            return None

    return ip, target.port


async def resolve_server_target(
    address: str,
    fallback_port: int,
    *,
    timeout: float = 5.0,
    use_cache: bool = True,
) -> tuple[str, int, bool]:
    """统一的目标服务器解析入口（带 TTL 缓存）。

    根据 address 的形式选择不同解析路径：
      - SRV 记录名（以 `_` 开头）：查 SRV → 主机名 → IP
      - 已经是 IP 字面量（IPv4 / 带方括号或不带方括号的 IPv6）：直接返回
      - 普通域名：解析 A/AAAA 记录 → IP；无 A 记录时再尝试 SRV

    Args:
        address: 用户填的 server_address 字符串，可包含 :port 后缀
            （对 SRV 路径该 :port 会被忽略）。
        fallback_port: 当用户没指定端口时使用的端口（Java 25565 / Bedrock 19132）。
            对 SRV 路径该参数被忽略（端口由 SRV 记录决定）。
        timeout: DNS 解析超时。
        use_cache: 是否使用缓存（测试或强制刷新时可关闭）。

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

    cache_key = (address.lower(), fallback_port)
    if use_cache:
        cached = _resolve_cache.get(cache_key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1] if cached[1] is not None else ("", 0, False)

    result = await _resolve_server_target_uncached(address, fallback_port, timeout=timeout)

    if use_cache:
        ttl = RESOLVE_CACHE_TTL if result[0] else NEGATIVE_CACHE_TTL
        _resolve_cache[cache_key] = (time.monotonic() + ttl, result if result[0] else None)

    return result


async def _resolve_server_target_uncached(
    address: str,
    fallback_port: int,
    *,
    timeout: float,
) -> tuple[str, int, bool]:
    """实际的解析逻辑（不带缓存），参数含义见 ``resolve_server_target``。"""
    # 1) SRV 记录路径（端口由 SRV 决定，忽略用户填的 :port）
    if address.startswith("_"):
        host_part = address.rsplit(":", 1)[0].strip() if ":" in address else address
        if not host_part:
            return "", 0, False
        resolved = await resolve_srv_with_ip(host_part, timeout=timeout)
        if resolved is None:
            return "", 0, False
        ip, port = resolved
        return ip, port, True

    # 2) 拆分 host / port（支持 IPv6 方括号写法与裸 IPv6 字面量）
    parsed = split_host_port(address, default_port=fallback_port)
    if parsed is None:
        return "", 0, False
    host_part, port_to_use = parsed

    # 3) 已经是 IPv4/IPv6 字面量
    if is_ip_literal(host_part):
        return host_part, port_to_use, False

    # 4) 普通主机名 → 解析 A/AAAA 记录
    ip = await resolve_host(host_part, timeout=timeout)
    if ip is None:
        # 5) 域名本身没有 A 记录（典型情况：通过 SRV 指向内网/FRP 后端），
        #    自动尝试 Minecraft 标准 SRV 记录（补全 _minecraft._tcp. 前缀）。
        srv_name = f"_minecraft._tcp.{host_part}"
        srv_resolved = await resolve_srv_with_ip(srv_name, timeout=timeout)
        if srv_resolved is not None:
            return srv_resolved[0], srv_resolved[1], True
        return "", 0, False
    return ip, port_to_use, False
