import re

from astrbot.api import AstrBotConfig, logger

from .models import DisplayOptions, PluginSettings, ServerConfig, ServerState


DISPLAY_DEFAULTS = DisplayOptions()


def safe_int(value, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def safe_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default

    value_str = str(value).strip().lower()
    if value_str in {"true", "1", "yes", "on"}:
        return True
    if value_str in {"false", "0", "no", "off"}:
        return False
    return default


def parse_target_group(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value if value.isdigit() else None


def parse_umo(value) -> str | None:
    """校验并规范化 UMO 字符串（形如 ``<平台实例名>:<消息类型>:<会话ID>``）。

    例如 ``atri:GroupMessage:1092815819``。只做宽松结构校验，
    可达性由 ``/mc推送目标`` 的测试消息验证。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 3 or any(not part.strip() for part in parts[:3]):
        return None
    return text


def normalize_server_type(server_type: str) -> str:
    normalized = str(server_type or "java").strip().lower()
    return normalized if normalized in {"bedrock", "java"} else "java"


def slugify_server_key(server_name: str, fallback: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", server_name.strip().lower()).strip("_")
    return normalized or fallback


def load_display_options(config: AstrBotConfig) -> DisplayOptions:
    raw_options = config.get("display_options", {})
    if not isinstance(raw_options, dict):
        logger.warning("配置项 display_options 不是对象，已使用默认展示设置。")
        raw_options = {}

    return DisplayOptions(
        show_server_name=safe_bool(raw_options.get("show_server_name"), DISPLAY_DEFAULTS.show_server_name),
        show_server_address=safe_bool(raw_options.get("show_server_address"), DISPLAY_DEFAULTS.show_server_address),
        show_server_type=safe_bool(raw_options.get("show_server_type"), DISPLAY_DEFAULTS.show_server_type),
        show_game_version=safe_bool(raw_options.get("show_game_version"), DISPLAY_DEFAULTS.show_game_version),
        show_online_players=safe_bool(raw_options.get("show_online_players"), DISPLAY_DEFAULTS.show_online_players),
        show_motd=safe_bool(raw_options.get("show_motd"), DISPLAY_DEFAULTS.show_motd),
        show_player_list=safe_bool(raw_options.get("show_player_list"), DISPLAY_DEFAULTS.show_player_list),
        show_update_time=safe_bool(raw_options.get("show_update_time"), DISPLAY_DEFAULTS.show_update_time),
        show_hitokoto=safe_bool(raw_options.get("show_hitokoto"), DISPLAY_DEFAULTS.show_hitokoto),
    )


def load_settings(config: AstrBotConfig) -> PluginSettings:
    global_check_interval = safe_int(config.get("check_interval", 45), 45)
    if global_check_interval < 5:
        global_check_interval = 5

    return PluginSettings(
        target_group=parse_target_group(config.get("target_group")),
        target_umo=parse_umo(config.get("target_umo")),
        enable_auto_monitor=safe_bool(config.get("enable_auto_monitor", False), False),
        global_check_interval=global_check_interval,
        display_options=load_display_options(config),
    )


def resolve_check_interval(value, global_check_interval: int) -> int:
    interval = safe_int(value, global_check_interval)
    if interval <= 0:
        return global_check_interval
    return max(5, interval)


def load_servers_from_config(config: AstrBotConfig, global_check_interval: int) -> list[ServerConfig]:
    raw_servers = config.get("server_entries")
    if raw_servers is None:
        raw_servers = config.get("servers", [])

    if not isinstance(raw_servers, list):
        logger.warning("配置项 server_entries 不是列表，已忽略。")
        raw_servers = []

    servers: list[ServerConfig] = []
    for index, item in enumerate(raw_servers, start=1):
        if not isinstance(item, dict):
            logger.warning(f"server_entries 第 {index} 项不是对象，已跳过。")
            continue

        enabled = safe_bool(item.get("enabled", True), True)
        if not enabled:
            continue

        raw_key = str(item.get("key", "")).strip()
        raw_name = str(item.get("server_name", "")).strip()
        default_name = raw_key or f"服务器{index}"
        server_name = raw_name or default_name
        # 统一的"地址"输入字段：兼容旧版 server_ip+server_port+srv_record 三字段
        server_address = _normalize_server_address(item.get("server_address"))
        # 向后兼容：旧字段仍然可用（sr v3 -> v4 迁移）
        if not server_address:
            legacy_srv = _normalize_server_address(item.get("srv_record"))
            if legacy_srv:
                server_address = legacy_srv
            else:
                legacy_ip = str(item.get("server_ip", "")).strip()
                legacy_port = safe_int(item.get("server_port", 25565), 25565)
                if legacy_ip:
                    server_address = f"{legacy_ip}:{legacy_port}"

        parsed = _parse_server_address(server_address)
        if parsed is None:
            logger.warning(
                f"{server_name} 的 server_address 配置无效 ({server_address!r})，已跳过。"
            )
            continue

        server_ip, server_port = parsed
        server_type = normalize_server_type(item.get("server_type", "java"))
        check_interval = resolve_check_interval(item.get("check_interval", global_check_interval), global_check_interval)
        key = raw_key or slugify_server_key(server_name, f"server_{index}")

        servers.append(
            ServerConfig(
                key=key,
                enabled=enabled,
                server_name=server_name,
                server_ip=server_ip,
                server_port=server_port,
                server_type=server_type,
                check_interval=check_interval,
                server_address=server_address,
            )
        )

    return servers


def _normalize_server_address(value) -> str:
    """清洗用户输入：去除空白、尾部点号。空值返回空字符串。"""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return text


def _parse_server_address(address: str) -> tuple[str, int] | None:
    """解析用户填的统一地址，返回 (host, port)。

    支持的格式：
      - "192.168.1.10"             → ("192.168.1.10", 25565)
      - "192.168.1.10:25566"        → ("192.168.1.10", 25566)
      - "play.example.com"          → ("play.example.com", 25565)
      - "play.example.com:25566"     → ("play.example.com", 25566)
      - "_minecraft._tcp.example.com" → ("_minecraft._tcp.example.com", 25565)
      - "_minecraft._tcp.example.com:25565" → ("_minecraft._tcp.example.com", 25565)
        （SRV 记录的端口由记录本身决定；此处 :port 会被忽略）

    返回 None 表示输入无效。
    """
    if not address or not address.strip():
        return None

    text = address.strip().rstrip(".")
    if not text:
        return None

    # 判断是否 SRV 记录（开头是 _，例如 _minecraft._tcp.example.com）
    is_srv = text.startswith("_")

    # 拆分可选的端口后缀
    host_part = text
    explicit_port: int | None = None
    if ":" in text:
        # IPv6 不在常见范围内（用 [] 包裹），Minecraft 域名/IP 通常不含 IPv6 字面量
        host_part, port_str = text.rsplit(":", 1)
        host_part = host_part.strip()
        try:
            explicit_port = int(port_str)
            if not (1 <= explicit_port <= 65535):
                return None
        except ValueError:
            return None

    # 如果是 SRV，明确忽略端口（端口信息来自 DNS 记录本身）
    if is_srv:
        return host_part, 25565  # 占位端口，实际使用 SRV 解析后的端口

    if not host_part:
        return None

    port = explicit_port if explicit_port is not None else 25565
    return host_part, port


def build_initial_states(servers: list[ServerConfig]) -> dict[str, ServerState]:
    return {server.key: ServerState() for server in servers}
