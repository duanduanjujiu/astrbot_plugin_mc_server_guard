"""Minecraft 多服务器监控：格式化与状态/玩家变化检测。

v1.0.0（本地 fork）起，上下线判定加入**滞回确认**：
单次 SLP 探测失败（超时 / SRV 抖动 / 服务器重启瞬间）不再立即推“已离线”，
需要连续 ``confirm_offline`` 次失败采样才确认离线；恢复在线同理需要
``confirm_online`` 次成功采样。避免网络抖动导致 已离线/已上线 刷屏，
也避免抖动清空玩家基线后误报“大量玩家加入”。

玩家进出 diff、1.19.1+ Anonymous Player 占位名补偿等逻辑保持原样。
"""

from .models import DisplayOptions, ServerConfig, ServerSnapshot, ServerState

# ---------------------------------------------------------------------------
# 稳定性常量（v1.0.0）
# ---------------------------------------------------------------------------

# 上下线翻转前需要的“连续同向采样”数：
#   - 离线：连续 3 次失败（默认 60s 一次检测 ≈ 3 分钟内持续失联）
#   - 在线：连续 2 次成功
DEFAULT_STATUS_CONFIRM_ONLINE = 2
DEFAULT_STATUS_CONFIRM_OFFLINE = 3

# 连续拉取失败达到该次数后，把该服务器检测间隔退避到 DEGRADED_POLL_INTERVAL，
# 减少对失联服务器的无效探测与日志。
DEGRADE_AFTER_FAILURES = 3
DEGRADED_POLL_INTERVAL = 60.0

# 1.19.1+ 原版/Forge 服务端在玩家刚进入时，状态查询 players.sample 里会短暂返回
# 占位名 "Anonymous Player"，真实名要等服务端刷新后才出现。这类名字不能当作玩家名播报。
_ANONYMOUS_NAMES = {
    "anonymous player",
    "anonymousplayer",
    "anonymous",
    "unknown",
    "unnamed",
    "anon",
    "隐藏玩家",
    "匿名玩家",
    "未知玩家",
}


def is_unknown_player_name(name: str) -> bool:
    """判断是否为占位/不可用的玩家名（例如服务端尚未解析出真实名时返回的 Anonymous Player）。"""
    if name is None:
        return True
    text = str(name).strip()
    if not text:
        return True
    lowered = text.lower().replace(" ", "")
    return lowered in _ANONYMOUS_NAMES or lowered.startswith("anonymousplayer")


def known_player_names(names: list[str] | None) -> list[str]:
    """过滤掉占位名，只保留真实可见的玩家名。"""
    if not names:
        return []
    return [n for n in names if not is_unknown_player_name(n)]


def format_server_info(snapshot: ServerSnapshot | None, display_options: DisplayOptions) -> str:
    if not snapshot:
        return "❌ 获取服务器数据失败"

    status_emoji = "🟢" if snapshot.status == "online" else "🔴"
    type_label = "基岩版" if snapshot.server_type == "bedrock" else "Java版"

    lines: list[str] = []
    if display_options.show_server_name:
        lines.append(f"{status_emoji} 服务器: {snapshot.name}")
    else:
        lines.append(f"{status_emoji} 状态: {'在线' if snapshot.status == 'online' else '离线'}")

    if display_options.show_server_address:
        # 优先展示主人填写的原值（如 mc.pengjinrui.top），fallback 到解析后的 IP:port
        addr = snapshot.server_address if snapshot.server_address else f"{snapshot.server_ip}:{snapshot.server_port}"
        lines.append(f"🌐 地址: {addr}")

    if display_options.show_server_type:
        lines.append(f"🔧 类型: {type_label}")

    if display_options.show_game_version:
        lines.append(f"🎮 版本: {snapshot.version}")

    if display_options.show_online_players:
        lines.append(f"👥 在线玩家: {snapshot.online}/{snapshot.max_players}")

    motd = snapshot.motd.strip()
    if display_options.show_motd and motd:
        lines.append(f"📝 MOTD: {motd[:120]}{'...' if len(motd) > 120 else ''}")

    if display_options.show_player_list:
        known = known_player_names(snapshot.players)
        if known:
            show_names = known[:8]
            line = f"📋 玩家列表: {', '.join(show_names)}"
            if len(known) > 8:
                line += f" (+{len(known) - 8}人)"
            lines.append(line)
        elif snapshot.online > 0:
            lines.append(f"📋 {snapshot.online} 名玩家在线（名单暂未公开）")
        else:
            lines.append("📋 当前无玩家在线")

    if display_options.show_update_time:
        lines.append(f"🕒 更新时间: {snapshot.update_time}")

    if snapshot.error and snapshot.status != "online":
        lines.append(f"⚠️ 说明: {snapshot.error}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 状态与玩家变化检测（v1.0.0：上下线带“连续采样确认”）
# ---------------------------------------------------------------------------


def _reset_player_baseline(
    state: ServerState, observed: str, snapshot: ServerSnapshot
) -> None:
    """把玩家进出基线重置为给定状态的初始值（不产生任何通知）。"""
    if observed == "online":
        state.last_player_count = snapshot.online
        state.last_player_list = known_player_names(snapshot.players)
    else:
        state.last_player_count = 0
        state.last_player_list = []
    state.pending_anonymous = 0
    state.pending_anonymous_polls = 0


def _diff_players(
    server: ServerConfig, state: ServerState, snapshot: ServerSnapshot
) -> tuple[bool, str | None]:
    """在“服务器稳定在线”的前提下 diff 玩家进出（含匿名占位名补偿）。

    仅在调用方确认 snapshot.status == "online" 时调用；本函数会更新
    last_player_count / last_player_list / pending_anonymous* 基线。
    """
    current_count = snapshot.online
    real_names = known_player_names(snapshot.players)
    changes: list[str] = []
    resolved_this_poll = False  # 本轮是否已播报出真实名/兜底名

    diff = current_count - (state.last_player_count if state.last_player_count is not None else 0)

    if diff > 0:
        # 有玩家加入
        newly_named = sorted(set(real_names) - set(state.last_player_list))
        if newly_named:
            changes.append(
                f"📈 {server.server_name}: {', '.join(newly_named)} 加入了服务器 (+{diff})"
            )
            state.last_player_list = real_names[:]
            state.pending_anonymous = 0
            resolved_this_poll = True
        else:
            # 人数增加但名单仍是占位名/不可见：挂起，等待名字解析
            state.pending_anonymous += diff
        state.last_player_count = current_count

    elif diff == 0:
        newly_named = sorted(set(real_names) - set(state.last_player_list))
        if newly_named and state.pending_anonymous > 0:
            # 之前挂起的匿名加入者，现在解析出了真实名
            changes.append(
                f"📈 {server.server_name}: {', '.join(newly_named)} 加入了服务器"
            )
            state.last_player_list = real_names[:]
            state.pending_anonymous = 0
            resolved_this_poll = True
        elif newly_named:
            # 人数没变但名单变化（极少见），只更新记录不播报
            state.last_player_list = real_names[:]
        else:
            state.last_player_list = real_names[:]

    else:  # diff < 0
        # 有玩家离开
        left_known = sorted(set(state.last_player_list) - set(real_names))
        if left_known:
            changes.append(
                f"📉 {server.server_name}: {', '.join(left_known)} 离开了服务器 ({diff})"
            )
        else:
            # 离开的是尚未解析出名字（匿名）的玩家
            changes.append(
                f"📉 {server.server_name}: 有 {-diff} 名玩家离开了服务器 ({diff})"
            )
        state.last_player_count = current_count
        state.last_player_list = real_names[:]
        if state.pending_anonymous > 0:
            # 粗略认为离开的人里包含挂起中的匿名玩家
            state.pending_anonymous = max(0, state.pending_anonymous + diff)

    # 兜底：匿名加入挂起太久仍未解析出真实名 → 按人数播报
    if state.pending_anonymous > 0 and not resolved_this_poll:
        state.pending_anonymous_polls += 1
        if state.pending_anonymous_polls >= 2:
            pending = state.pending_anonymous
            changes.append(
                f"📈 {server.server_name}: 有 {pending} 名玩家加入了服务器（名单暂未公开）"
            )
            state.pending_anonymous = 0
            state.pending_anonymous_polls = 0
    elif state.pending_anonymous == 0:
        state.pending_anonymous_polls = 0

    if changes:
        return True, "\n".join(changes)
    return False, None


def detect_changes(
    server: ServerConfig,
    state: ServerState,
    snapshot: ServerSnapshot,
    *,
    confirm_online: int = DEFAULT_STATUS_CONFIRM_ONLINE,
    confirm_offline: int = DEFAULT_STATUS_CONFIRM_OFFLINE,
) -> tuple[bool, str | None]:
    """检测一次采样的状态/玩家变化，返回 (是否有变化, 播报文本)。

    上下线判定带滞回（v1.0.0）：
    - 首次采样只静默建立基线（不推送任何“监控已启动”通知，避免插件加载/重载刷屏）；
    - 与当前稳定态相反的采样必须**连续**出现 ``confirm_*`` 次才确认翻转；
      翻转前的观测不会修改任何玩家基线，避免瞬时抖动清空记录；
    - 确认离线时清空玩家基线，恢复在线后把“已上线”与恢复瞬间在线玩家合并播报。

    玩家进出 diff（含 Anonymous Player 占位名补偿）沿用原实现。
    """
    observed = snapshot.status

    # 1) 首次采样：静默建立基线
    if state.stable_status is None:
        state.stable_status = observed
        state.contradict_probes = 0
        _reset_player_baseline(state, observed, snapshot)
        return False, None

    # 2) 与稳定态一致：在线则跑玩家 diff；离线则无任何事件
    if observed == state.stable_status:
        state.contradict_probes = 0
        if observed != "online":
            return False, None
        return _diff_players(server, state, snapshot)

    # 3) 与稳定态相反：连续采样确认后才翻转（期间不动任何基线）
    state.contradict_probes += 1
    required = confirm_offline if observed == "offline" else confirm_online
    if state.contradict_probes < required:
        return False, None

    state.contradict_probes = 0
    state.stable_status = observed

    if observed == "offline":
        # 已确认离线：清空玩家基线（离线期间的 clientlist 不可信）
        _reset_player_baseline(state, "offline", snapshot)
        return True, f"🔴 {server.server_name} 已离线"

    # 已确认恢复在线：先以 0 玩家基线进入 diff，把恢复瞬间在线的人
    # 与“已上线”合并为一条消息播报（与原实现行为一致）。
    _reset_player_baseline(state, "offline", snapshot)
    changes = [f"🟢 {server.server_name} 已上线"]
    _, join_msg = _diff_players(server, state, snapshot)
    if join_msg:
        changes.append(join_msg)
    return True, "\n".join(changes)
