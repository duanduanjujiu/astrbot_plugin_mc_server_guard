"""本地 Minecraft 服务器状态查询客户端。

通过实现 Minecraft Server List Ping (SLP) 协议直接与本地服务器通信，不再依赖任何
远程 HTTP API。同时支持 Java 版（TCP）和 Bedrock 版（基于 RAKNET 的 Unconnected Ping）。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import socket
import struct
from datetime import datetime
from typing import Any

from astrbot.api import logger

from .models import ServerConfig, ServerSnapshot


# 默认超时（秒）。原远程客户端的查询超时普遍为 15-25 秒，本地直连应更短。
DEFAULT_TIMEOUT = 5.0

# 现代 SLP 握手时向服务器声明的协议版本。
# 注意：该字段是握手包【必填】字段，缺失会导致服务端无法解析握手包并直接断开连接
# （表现为"连接被服务器关闭、无任何响应"，进而错误回退到 Legacy Ping）。
# 状态查询阶段绝大多数服务端（1.7+，含 1.20.x / 1.21.x）不会校验协议号，这里取
# 1.20.1 的协议号 763，与主流目标版本保持一致即可。
SLP_PROTOCOL_VERSION = 763

# 单次查询的最大重试次数。第二次重试前等待 0.3 秒，与原 MineBBS 客户端保持一致。
RETRY_COUNT = 2
RETRY_BACKOFF = 0.3

# 用于 Bedrock RAKNET Unconnected Ping 的 magic 字节序列。
RAKNET_MAGIC = bytes.fromhex(
    "00ffff00fefefefefdfdfdfd12345678"
)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_player_names(player_list: Any) -> list[str]:
    """统一处理玩家列表字段，兼容 SLP 返回的 list[dict] 或 list[str]。"""
    if isinstance(player_list, str):
        if not player_list.strip() or player_list.strip() == "无":
            return []
        return [name.strip() for name in player_list.split(",") if name.strip()]

    if not isinstance(player_list, list):
        return []

    names: list[str] = []
    for player in player_list:
        if isinstance(player, dict):
            name = (
                player.get("name_clean")
                or player.get("name")
                or player.get("username")
                or "未知玩家"
            )
            names.append(_strip_mc_color_codes(str(name)))
        else:
            names.append(_strip_mc_color_codes(str(player)))
    return [name for name in names if name and name.strip() != ""]


def _strip_mc_color_codes(text: str) -> str:
    """去掉 MOTD/玩家名里的 Minecraft § 颜色/格式控制符（QQ 纯文本不识别）。"""
    if not text:
        return text
    # §x / \u00a7x：颜色码占用两个字符（§ + 一个字母/数字）。
    return re.sub(r"\u00a7.", "", text)


def _component_text(node: Any) -> str:
    """把一个现代文本组件（或其数组）拍平成纯文本。

    兼容：
      - 纯字符串
      - {"text": "..."}
      - {"text": "", "extra": [...]}（1.20.x 常见 MOTD）
      - 嵌套 extra / with / translate / keybind

    拼接规则：各片段直接相连（A+B+C → "ABC"）；只有片段内含 "\\n" 才产生换行。
    """
    raw: list[str] = []

    def walk(current: Any) -> None:
        if isinstance(current, str):
            raw.append(current)
        elif isinstance(current, list):
            for item in current:
                walk(item)
        elif isinstance(current, dict):
            text = current.get("text")
            if text is not None:
                raw.append(str(text))
            else:
                # 没有 text 字段的复合组件：优先展开 with 参数，其次显示 translate 键
                with_args = current.get("with")
                if isinstance(with_args, list):
                    for arg in with_args:
                        walk(arg)
                elif current.get("translate") is not None:
                    raw.append(str(current["translate"]))
                elif current.get("keybind") is not None:
                    raw.append(str(current["keybind"]))
            extra = current.get("extra")
            if isinstance(extra, list):
                for item in extra:
                    walk(item)

    walk(node)
    joined = _strip_mc_color_codes("".join(raw))
    # 只保留非空行，避免 {"text":""} 之类产生大量空行
    lines = [line for line in joined.split("\n")]
    text = "\n".join(lines).strip("\n")
    return text


def _extract_motd_from_json(motd_field: Any) -> str:
    """从 SLP JSON 的 description/motd 字段中提取纯文本。"""
    if isinstance(motd_field, dict):
        # 兼容旧格式的 clean/raw 字段（部分第三方 API/服务端会返回）
        legacy = motd_field.get("clean") or motd_field.get("raw")
        if legacy:
            return _strip_mc_color_codes(str(legacy))
        return _component_text(motd_field)
    if isinstance(motd_field, str):
        return _strip_mc_color_codes(motd_field)
    if isinstance(motd_field, list):
        return _component_text(motd_field)
    return _strip_mc_color_codes(str(motd_field or ""))


def _parse_legacy_payload(payload: bytes) -> tuple[str, str, str, int, int]:
    """解析 Legacy Ping (0xFE) 的响应体，返回 (protocol, version, motd, online, max)。

    Legacy Ping 各历史版本的字段布局略有差异，且部分现代服务端/代理会在响应里混入
    颜色码或多余前缀（例如实测 1.20.1 Forge 服务端返回的
    "=§1\\x00127\\x001.20.1\\x00<motd>\\x000\\x0088"）。
    因此这里不按固定下标硬编码，而是做"语义化"解析：
      1) 先按 \\x00 拆段，去掉空段和颜色码残余、非字母数字的错位字节；
      2) 段尾的 1-2 个数字即 max / online；
      3) 版本号取第一个形如 "1.x.y" 的段；
      4) 版本号与在线数之间的内容拼接为 MOTD。
    """
    try:
        text = payload.decode("utf-16-be", errors="replace")
    except Exception as exc:
        raise ValueError(f"Legacy Ping UTF-16BE 解码失败: {exc}") from exc

    def _clean_token(s: str) -> str:
        # 去掉 § 颜色码（含 "§1" 这类前缀残余）与首尾空白
        return re.sub(r"§.", "", s).strip()

    def _is_int(s: str) -> bool:
        try:
            int(s)
            return True
        except (TypeError, ValueError):
            return False

    tokens = [_clean_token(tok) for tok in text.split("\x00")]
    # 丢弃空段以及 '=' 这类非字母数字的错位残余
    tokens = [tok for tok in tokens if tok and re.search(r"[0-9A-Za-z]", tok)]

    if len(tokens) < 2:
        # 极少数 Beta 1.8 - 1.3 风格：以 § 分隔、无 \x00
        beta_tokens = [
            _clean_token(tok)
            for tok in re.split(r"§+", text)
            if tok and re.search(r"[0-9A-Za-z]", tok)
        ]
        if beta_tokens:
            tokens = beta_tokens

    if not tokens:
        raise ValueError(f"Legacy Ping 无法解析出有效字段（文本={text!r}）")

    # 段尾数字 => max / online
    online_players = 0
    max_players = 0
    numeric_indices = [i for i, tok in enumerate(tokens) if _is_int(tok)]
    if len(numeric_indices) >= 2:
        max_players = int(tokens[numeric_indices[-1]])
        online_players = int(tokens[numeric_indices[-2]])
    elif len(numeric_indices) == 1:
        max_players = int(tokens[numeric_indices[0]])

    # 版本号：第一个形如 "1.x[.y]" 的段；找不到则取第一个非数字段
    version_idx: int | None = None
    for i, tok in enumerate(tokens):
        if re.fullmatch(r"\d+(\.\d+)+[\w .-]*", tok):
            version_idx = i
            break
    if version_idx is None:
        first_non_num = next(
            (i for i, tok in enumerate(tokens) if not _is_int(tok)), None
        )
        if first_non_num is not None:
            version_idx = first_non_num
    version_name = tokens[version_idx] if version_idx is not None else "未知版本"

    # MOTD：版本号之后、online 数字之前的所有内容
    online_idx = (
        numeric_indices[-2]
        if len(numeric_indices) >= 2
        else (numeric_indices[0] if numeric_indices else len(tokens))
    )
    if version_idx is not None and version_idx + 1 < online_idx:
        motd = "".join(tokens[version_idx + 1:online_idx]).strip()
    else:
        motd = ""

    # protocol：版本号紧邻的前一个数字段（该字段并不可靠，仅尽力而为）
    if version_idx is not None and version_idx > 0 and _is_int(tokens[version_idx - 1]):
        protocol_str = tokens[version_idx - 1]
    else:
        protocol_str = "未知"

    return protocol_str, version_name, motd, online_players, max_players


def _varint_encode(value: int) -> bytes:
    """按 Minecraft 协议编码 VarInt。"""
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value != 0:
            byte |= 0x80
        out.append(byte)
        if value == 0:
            break
    return bytes(out)


async def _read_varint(reader: asyncio.StreamReader) -> int:
    """异步读取 1 个 VarInt。"""
    value = 0
    shift = 0
    for _ in range(5):
        byte_data = await reader.readexactly(1)
        byte = byte_data[0]
        value |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            return value
        shift += 7
    raise ValueError("VarInt 过长，可能数据包损坏")


def _pack_string(text: str) -> bytes:
    """将字符串打包为 [VarInt 长度][UTF-8 字节]。"""
    encoded = text.encode("utf-8")
    return _varint_encode(len(encoded)) + encoded


def _pack_short_string(text: str) -> bytes:
    """Bedrock 协议使用的短字符串：[1 字节长度][UTF-8 字节]。"""
    encoded = text.encode("utf-8")
    if len(encoded) > 255:
        raise ValueError("短字符串长度超过 255 字节")
    return bytes([len(encoded)]) + encoded


async def _read_string(reader: asyncio.StreamReader) -> str:
    """异步读取 [VarInt 长度][UTF-8 字节]。"""
    length = await _read_varint(reader)
    raw = await reader.readexactly(length)
    return raw.decode("utf-8", errors="replace")


async def _read_short_string(reader: asyncio.StreamReader) -> str:
    """异步读取 [1 字节长度][UTF-8 字节]。"""
    length_data = await reader.readexactly(1)
    length = length_data[0]
    raw = await reader.readexactly(length)
    return raw.decode("utf-8", errors="replace")


def _is_ip_literal(value: str) -> bool:
    """判断字符串是否为 IPv4 或 IPv6 字面量。"""
    if not value:
        return False
    try:
        socket.inet_pton(socket.AF_INET, value)
        return True
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, value)
        return True
    except OSError:
        pass
    return False


class LocalStatusClient:
    """基于 Minecraft SLP 协议的本地状态查询客户端。"""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout

    async def fetch_status(self, server: ServerConfig, source: str) -> ServerSnapshot | None:
        """根据 server_type 选择对应协议拉取服务器状态。失败返回 None。

        通过 server_address 统一解析目标地址（支持 IP / 域名 / SRV 记录名）。
        """
        # 通过 server_address 解析出真实 (ip, port)
        target_host = server.server_ip
        target_port = server.server_port
        used_srv = False

        # 优先用 server_address（用户填的统一地址）；兼容旧版只用 server_ip 的情况
        address_to_resolve = (server.server_address or "").strip()
        if address_to_resolve:
            try:
                from .srv_resolver import resolve_server_target
                ip, port, srv_flag = await resolve_server_target(
                    address_to_resolve,
                    fallback_port=server.server_port,
                    timeout=min(self.timeout, 5.0),
                )
                if ip:
                    target_host = ip
                    target_port = port
                    used_srv = srv_flag
                    if srv_flag:
                        logger.debug(
                            f"[{source}] [{server.server_name}] SRV 解析成功: "
                            f"{address_to_resolve} → {ip}:{port}"
                        )
                    elif address_to_resolve != ip:
                        logger.debug(
                            f"[{source}] [{server.server_name}] 主机名解析: "
                            f"{address_to_resolve} → {ip}:{port}"
                        )
                else:
                    # 解析失败：仅在 server_ip 非空时回退，否则直接返回 None
                    if server.server_ip:
                        logger.warning(
                            f"[{source}] [{server.server_name}] 解析失败 ({address_to_resolve})，"
                            f"回退到配置的 server_ip:server_port"
                        )
                        target_host = server.server_ip
                        target_port = server.server_port
                    else:
                        logger.warning(
                            f"[{source}] [{server.server_name}] 解析失败 ({address_to_resolve})，"
                            f"且未配置 server_ip 兜底，无法继续"
                        )
                        return None
            except Exception as exc:
                logger.warning(
                    f"[{source}] [{server.server_name}] 解析异常: {exc}，"
                    f"回退到配置的 server_ip:server_port"
                )
        elif server.server_ip and not _is_ip_literal(server.server_ip):
            # 旧版配置只有 server_ip，可能是域名，尝试解析
            try:
                from .srv_resolver import resolve_server_target
                ip, port, _ = await resolve_server_target(
                    server.server_ip,
                    fallback_port=server.server_port,
                    timeout=min(self.timeout, 5.0),
                )
                if ip:
                    target_host = ip
            except Exception:
                pass

        # 探测时用解析后的地址，但保持 server 对象不变（保持原配置以便 fallback snapshot 仍展示原配置）
        resolved_server = ServerConfig(
            key=server.key,
            enabled=server.enabled,
            server_name=server.server_name,
            server_ip=target_host,
            server_port=target_port,
            server_type=server.server_type,
            check_interval=server.check_interval,
            server_address=server.server_address,
        )

        last_error: Exception | None = None
        for attempt in range(1, RETRY_COUNT + 1):
            # 每次重试都需要新建一个协程对象，否则第二次 await 会失败。
            if resolved_server.server_type == "bedrock":
                attempt_coro = self._fetch_bedrock(resolved_server, source)
            else:
                attempt_coro = self._fetch_java(resolved_server, source)
            try:
                return await attempt_coro
            except asyncio.TimeoutError:
                last_error = asyncio.TimeoutError("query timeout")
                logger.warning(
                    f"[{source}] [{server.server_name}] 本地查询超时 "
                    f"({self.timeout}s, 尝试 {attempt}/{RETRY_COUNT})"
                )
            except (ConnectionRefusedError, OSError) as exc:
                last_error = exc
                logger.warning(
                    f"[{source}] [{server.server_name}] 本地连接失败: {exc} "
                    f"(尝试 {attempt}/{RETRY_COUNT})"
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"[{source}] [{server.server_name}] 本地查询异常: {exc} "
                    f"(尝试 {attempt}/{RETRY_COUNT})"
                )

            if attempt < RETRY_COUNT:
                await asyncio.sleep(RETRY_BACKOFF)

        logger.debug(
            f"[{source}] [{server.server_name}] 本地查询最终失败: {last_error}"
        )
        return None

    # ---------- Java 版 (TCP SLP / Legacy Ping) ----------

    async def _fetch_java(self, server: ServerConfig, source: str) -> ServerSnapshot:
        """通过 TCP SLP 协议查询 Java 版服务器。

        优先使用现代 SLP 协议（1.7+ 握手 + 状态查询包）。
        若服务器不支持 SLP（例如某些 1.6 之前的兼容服务端、Patched 的 Mohist、
        反向代理拒绝 SLP 等），自动回退到 Minecraft 1.6 之前的 Legacy Ping (0xFE) 协议。
        """
        host = server.server_ip
        port = server.server_port

        try:
            return await self._fetch_java_slp(server, source, host, port)
        except (asyncio.IncompleteReadError, ValueError, OSError) as exc:
            # SLP 路径在三种情况下需要回退：
            # 1. IncompleteReadError：连接被对方关闭，没读到任何字节（典型 Mohist/老服务器）
            # 2. ValueError：响应包 ID 异常、字段缺失
            # 3. OSError：底层网络错误
            err_text = str(exc) if isinstance(exc, (ValueError, OSError)) else "连接被服务器关闭（无响应数据）"
            logger.debug(
                f"[{source}] [{server.server_name}] 现代 SLP 失败 ({err_text})，"
                f"尝试 Legacy Ping 协议"
            )
            return await self._fetch_java_legacy(server, source, host, port)

    async def _fetch_java_slp(
        self,
        server: ServerConfig,
        source: str,
        host: str,
        port: int,
    ) -> ServerSnapshot:
        """通过现代 SLP 协议（握手 + 状态查询）查询 Java 版服务器。"""
        logger.debug(
            f"[{source}] [{server.server_name}] 本地 SLP 探测 (Java): "
            f"{host}:{port}"
        )

        # 构造握手包 + 状态请求包，一次性发送。
        # 注意：握手包必须携带 [包 ID][协议版本 VarInt][服务器地址 String][端口 u16][下一状态 VarInt]。
        # 早期版本漏发了"协议版本"字段，导致服务端无法解析握手包并直接断开 TCP 连接，
        # 从而永远无法走通现代 SLP（1.20.x 服务端也会如此表现）。
        handshake_packet = (
            _varint_encode(0x00)            # 包 ID：握手
            + _varint_encode(SLP_PROTOCOL_VERSION)  # 协议版本（必填！）
            + _pack_string(host)            # 服务器地址
            + struct.pack(">H", port)       # 端口（2 字节大端）
            + _varint_encode(1)             # 下一状态：1 表示状态查询
        )
        handshake_frame = _varint_encode(len(handshake_packet)) + handshake_packet

        status_request_packet = _varint_encode(0x00)  # 包 ID：状态请求
        status_request_frame = (
            _varint_encode(len(status_request_packet)) + status_request_packet
        )

        loop = asyncio.get_running_loop()
        connect_started = loop.time()

        # 建立 TCP 连接（不能用 async with，因为 open_connection 返回的是元组）
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=self.timeout,
        )
        try:
            writer.write(handshake_frame + status_request_frame)
            await asyncio.wait_for(writer.drain(), timeout=self.timeout)

            # 读取响应包长度 VarInt
            response_length = await asyncio.wait_for(
                _read_varint(reader), timeout=self.timeout
            )
            # 读取包 ID（必须是 0x00）
            packet_id = await asyncio.wait_for(
                _read_varint(reader), timeout=self.timeout
            )
            if packet_id != 0x00:
                raise ValueError(f"未知的响应包 ID: 0x{packet_id:02x}")

            # 读取 JSON 字符串
            json_text = await asyncio.wait_for(
                _read_string(reader), timeout=self.timeout
            )

            # 延迟测量（可选）：现代 SLP 中由【客户端】发送 Ping(0x01 + 8 字节时间戳)，
            # 服务端原样回写 Pong(0x01 + 同一 8 字节)。不能反过来等服务端先发，否则
            # 每次查询都会白白阻塞。整个 ping/pong 预算限定在 1 秒内，失败直接忽略。
            latency_ms: int | None = None
            try:
                ping_payload = _varint_encode(0x01) + struct.pack(
                    ">q", int(loop.time() * 1000)
                )
                ping_frame = _varint_encode(len(ping_payload)) + ping_payload
                writer.write(ping_frame)
                await asyncio.wait_for(writer.drain(), timeout=1.0)
                pong_frame_len = await asyncio.wait_for(
                    _read_varint(reader), timeout=1.0
                )
                pong_payload = await asyncio.wait_for(
                    reader.readexactly(pong_frame_len), timeout=1.0
                )
                if pong_payload and pong_payload[0] == 0x01:
                    latency_ms = int((loop.time() - connect_started) * 1000)
            except (
                asyncio.TimeoutError,
                asyncio.IncompleteReadError,
                ConnectionError,
                OSError,
            ):
                # 服务端不响应 ping（老版本 / 代理等），忽略即可
                latency_ms = None
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"SLP JSON 解析失败: {exc}") from exc

        return self._parse_java(server, data, latency_ms=latency_ms)

    async def _fetch_java_legacy(
        self,
        server: ServerConfig,
        source: str,
        host: str,
        port: int,
    ) -> ServerSnapshot:
        """通过 Minecraft 1.6 之前的 Legacy Ping (0xFE) 协议查询 Java 版服务器。

        Legacy Ping 有几种历史格式：
          - Beta 1.8 - 1.3：响应格式为
                [§, count, §, motd]
                其中 count 是 motd 长度
          - 1.4 - 1.5：响应格式为
                [§1, 0x00, protocol, 0x00, version, 0x00, motd, 0x00, online, 0x00, max]
                （前导 1 字节 §1 + 4 个 0x00 间隔，UTF-16BE）
          - 1.6：响应格式为
                [0x00, protocol, 0x00, version, 0x00, motd, 0x00, online, 0x00, max]
                （UTF-16BE，不含前导 §1）
        此方法兼容上述所有格式。
        """
        logger.debug(
            f"[{source}] [{server.server_name}] 本地 Legacy Ping (Java): "
            f"{host}:{port}"
        )

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=self.timeout,
        )
        try:
            writer.write(bytes([0xFE, 0x01]))
            await asyncio.wait_for(writer.drain(), timeout=self.timeout)

            # 读取响应：第一个字节必须是 0xFF
            first_byte = await asyncio.wait_for(reader.readexactly(1), timeout=self.timeout)
            if first_byte[0] != 0xFF:
                raise ValueError(f"Legacy Ping 响应首字节非 0xFF，得到 0x{first_byte[0]:02x}")

            # 余下所有内容是 UTF-16BE 字符串（部分服务端 MOTD 较长，读取上限放宽）
            payload = await asyncio.wait_for(
                reader.read(65536),
                timeout=min(2.0, self.timeout),
            )
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        if not payload:
            raise asyncio.IncompleteReadError(payload, 1)

        protocol_str, version_name, motd, online_players, max_players = (
            _parse_legacy_payload(payload)
        )

        return ServerSnapshot(
            key=server.key,
            name=server.server_name,
            server_ip=server.server_ip,
            server_port=server.server_port,
            server_type="java",
            status="online",
            motd=motd,
            version=version_name,
            protocol=protocol_str,
            online=online_players,
            max_players=max_players,
            players=[],  # Legacy Ping 不返回玩家列表
            software="未知",
            map_name="未知",
            update_time=_now_text(),
            host=f"{host}:{port}",
            server_address=server.server_address,
            latency_ms=None,
            error=None,
        )

    def _parse_java(
        self,
        server: ServerConfig,
        data: dict,
        latency_ms: int | None = None,
    ) -> ServerSnapshot:
        version_info = data.get("version", {}) or {}
        if isinstance(version_info, dict):
            version = _strip_mc_color_codes(str(version_info.get("name", "未知版本")))
            protocol = str(version_info.get("protocol", "未知"))
        else:
            version = _strip_mc_color_codes(str(version_info or "未知版本"))
            protocol = "未知"

        players_info = data.get("players", {}) or {}
        if isinstance(players_info, dict):
            online_players = int(players_info.get("online", 0) or 0)
            max_players = int(players_info.get("max", 0) or 0)
            player_names = _normalize_player_names(players_info.get("sample", []) or [])
        else:
            online_players = 0
            max_players = 0
            player_names = []

        motd = _extract_motd_from_json(data.get("description", {}) or {})

        # favicon 是 base64 编码的图片数据，前端不需要，不再解析。
        software = str(
            data.get("software")
            or data.get("server_mod")
            or "未知"
        )

        # modinfo / forgeData / etc.，暂不展开。
        return ServerSnapshot(
            key=server.key,
            name=server.server_name,
            server_ip=server.server_ip,
            server_port=server.server_port,
            server_type="java",
            status="online",
            motd=motd,
            version=version,
            protocol=protocol,
            online=online_players,
            max_players=max_players,
            players=player_names,
            software=software,
            map_name="未知",
            update_time=_now_text(),
            host=f"{server.server_ip}:{server.server_port}",
            server_address=server.server_address,
            latency_ms=latency_ms,
            error=None,
        )

    # ---------- Bedrock 版 (UDP Unconnected Ping) ----------

    async def _fetch_bedrock(self, server: ServerConfig, source: str) -> ServerSnapshot:
        """通过 RAKNET Unconnected Ping 查询 Bedrock 版服务器。"""
        logger.debug(
            f"[{source}] [{server.server_name}] 本地 RAKNET 探测 (Bedrock): "
            f"{server.server_ip}:{server.server_port}"
        )

        host = server.server_ip
        port = server.server_port

        # 构造 Unconnected Ping 数据包 (ID 0x01)
        ping_time = random.randint(0, 2**31 - 1)
        client_guid = random.randint(0, 2**63 - 1)
        packet = (
            bytes([0x01])                        # 包 ID
            + struct.pack(">Q", ping_time)       # 发送时间戳
            + struct.pack(">Q", client_guid)     # 客户端 GUID
            + RAKNET_MAGIC                       # magic 字节
        )

        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[bytes] = loop.create_future()

        class _BedrockPingProtocol(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple) -> None:  # type: ignore[override]
                if not response_future.done():
                    response_future.set_result(data)

            def error_received(self, exc: Exception) -> None:  # type: ignore[override]
                if not response_future.done():
                    response_future.set_exception(exc)

        transport, _ = await loop.create_datagram_endpoint(
            _BedrockPingProtocol,
            remote_addr=(host, port),
        )
        try:
            transport.sendto(packet)
            data = await asyncio.wait_for(response_future, timeout=self.timeout)
        finally:
            transport.close()

        return self._parse_bedrock(server, data)

    def _parse_bedrock(self, server: ServerConfig, data: bytes) -> ServerSnapshot:
        """解析 Bedrock Unconnected Pong (0x1c) 数据包。"""
        # 包头由 1B 包ID + 8B 时间戳 + 8B 服务器GUID + 16B magic = 33 字节
        header_len = 1 + 8 + 8 + len(RAKNET_MAGIC)
        if len(data) < header_len:
            raise ValueError(f"Bedrock 响应包过短: {len(data)} 字节")

        cursor = header_len
        # 字符串结构：[1 字节长度][UTF-8 内容]
        # 字段顺序（不同版本略有差异）：
        #   1. edition (MCPE; 教育版会有 EDU)
        #   2. MOTD 第一行（可选）
        #   3. protocol version
        #   4. version name
        #   5. online players
        #   6. max players
        #   7. server id
        #   8. sub-motd (可选)
        #   9. game mode (可选)
        #  10. integer 字段 (可选)
        #  11. 服务器端口 v4 (可选)
        #  12. 服务器端口 v6 (可选)
        #  13. 是否允许 LAN (可选)

        def read_short_string(buf: bytes, pos: int) -> tuple[str, int]:
            if pos >= len(buf):
                return "", pos
            length = buf[pos]
            pos += 1
            end = min(pos + length, len(buf))
            text = buf[pos:end].decode("utf-8", errors="replace")
            return text, end

        fields: list[str] = []
        while cursor < len(data):
            text, cursor = read_short_string(data, cursor)
            fields.append(text)

        # 将字段填充到预期位置，缺失则置空。
        def _f(idx: int) -> str:
            return fields[idx] if idx < len(fields) else ""

        edition = _f(0)
        motd_line_1 = _f(1)
        protocol_str = _f(2)
        version_name = _f(3)
        online_str = _f(4)
        max_str = _f(5)
        server_id = _f(6)
        sub_motd = _f(7)
        game_mode = _f(8)

        try:
            online_players = int(online_str) if online_str else 0
        except ValueError:
            online_players = 0
        try:
            max_players = int(max_str) if max_str else 0
        except ValueError:
            max_players = 0

        # MOTD：可能跨多行
        motd_lines = [line for line in (motd_line_1, sub_motd) if line]
        motd = "\n".join(motd_lines)

        # 显示名：优先使用游戏模式或版本名
        display_name = server.server_name
        # software 信息：edition + game mode
        software_parts = [edition] if edition else []
        if game_mode:
            software_parts.append(game_mode)
        software = " / ".join(software_parts) if software_parts else "Bedrock"

        # Bedrock 协议包不直接给出玩家列表，因此保持为空。
        player_names: list[str] = []

        return ServerSnapshot(
            key=server.key,
            name=display_name,
            server_ip=server.server_ip,
            server_port=server.server_port,
            server_type="bedrock",
            status="online",
            motd=motd,
            version=version_name or "未知版本",
            protocol=protocol_str or "未知",
            online=online_players,
            max_players=max_players,
            players=player_names,
            software=software,
            map_name="未知",
            update_time=_now_text(),
            host=f"{server.server_ip}:{server.server_port}",
            server_address=server.server_address,
            latency_ms=None,
            error=None,
        )


def build_status_client(_legacy_flag: bool = False) -> LocalStatusClient:
    """工厂函数：构造本地状态查询客户端。

    为兼容旧代码调用签名（原本根据 use_mcbbs_api 切换 MCStatusClient / MineBBSClient），
    保留位置参数但忽略其值。
    """
    return LocalStatusClient()