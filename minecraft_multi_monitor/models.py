from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 默认值（与 _conf_schema.json 中的 default 字段保持一致）
# 改这里即可同步到 config.py 加载逻辑与 schema 提示。
# ---------------------------------------------------------------------------

# 单服务器默认检测间隔（秒）。范围 [MIN_CHECK_INTERVAL, +∞)。
DEFAULT_CHECK_INTERVAL = 45
MIN_CHECK_INTERVAL = 5

# 插件加载后是否默认自动启动监控（与 schema "enable_auto_monitor" default 对齐）
DEFAULT_ENABLE_AUTO_MONITOR = True


@dataclass(slots=True)
class DisplayOptions:
    show_server_name: bool = True
    show_server_address: bool = False
    show_server_type: bool = True
    show_game_version: bool = False
    show_online_players: bool = True
    show_motd: bool = True
    show_player_list: bool = False
    show_update_time: bool = True
    show_hitokoto: bool = False


@dataclass(slots=True)
class PluginSettings:
    target_group: str | None
    target_umo: str | None
    enable_auto_monitor: bool
    global_check_interval: int
    display_options: DisplayOptions


@dataclass(slots=True)
class ServerConfig:
    key: str
    enabled: bool
    server_name: str
    server_ip: str
    server_port: int
    server_type: str
    check_interval: int
    # 统一的"服务器地址"输入字段：
    # - IP: 192.168.1.10 或 192.168.1.10:25565
    # - 域名: play.example.com 或 play.example.com:25566
    # - SRV 记录名: _minecraft._tcp.play.example.com
    server_address: str = ""


@dataclass(slots=True)
class ServerState:
    """单台服务器在监控循环中维护的运行时状态。

    分组：
    - "稳定态"：已确认并（可能已推送过）的服务器状态，用于上下线滞回判定；
    - "玩家基线"：仅在服务器稳定在线时可信，供玩家进出 diff 使用；
    - "匿名挂起"：1.19.1+ 服务端 sample 短暂返回占位名时的补偿计数。
    """

    # ---- 稳定态 / 上下线滞回（见 monitoring.detect_changes）----
    # 已确认（并已静默建基线/推送）的状态；None 表示插件刚启动。
    stable_status: str | None = None  # "online" | "offline"
    # 连续与 stable_status 相反的采样数；达到阈值后翻转。
    contradict_probes: int = 0
    # 连续拉取失败次数（status == "offline" 的采样）；用于降频/降级。
    consecutive_failures: int = 0
    # 最近一次成功拉取的时间戳（loop.time()）。
    last_success_ts: float = 0.0

    # ---- 玩家基线（只在稳定在线时更新）----
    last_player_count: int | None = None
    last_player_list: list[str] = field(default_factory=list)
    last_check_ts: float = 0.0
    # 1.19.1+ 服务端在玩家刚进入时，状态查询的 players.sample 会短暂返回占位名
    # "Anonymous Player"，真实名字要等服务器刷新后才出现。这几个字段用于把这类
    # 加入事件"挂起"，待名字解析出来后再播报真名，避免把占位名当玩家名推送。
    pending_anonymous: int = 0            # 尚未解析出真名的加入人数
    pending_anonymous_polls: int = 0      # 连续多少个周期仍未解析（用于兜底播报）


@dataclass(slots=True)
class ServerSnapshot:
    key: str
    name: str
    server_ip: str
    server_port: int
    server_type: str
    status: str
    motd: str
    version: str
    protocol: str
    online: int
    max_players: int
    players: list[str]
    software: str
    map_name: str
    update_time: str
    host: str = ""
    # 用户在 server_address 配置里填的原值（域名 / IP / SRV 记录名）。
    # 用于"地址"栏展示，让主人看到自己填的内容而不是解析后的 IP。
    server_address: str = ""
    latency_ms: int | None = None
    error: str | None = None
