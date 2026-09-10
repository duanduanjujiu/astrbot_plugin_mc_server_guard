"""AstrBot Minecraft 多服务器监控插件（AI 修改版 v1.0.1）。

v1.0.1（本轮改动）：
- 聊天指令全部加 ``mc`` 前缀，避免与 astrbot_plugin_ts3_server_guard 等插件的
  同名指令冲突：``/mc_query``、``/mc_push_target``、``/mc_push_test``、
  ``/mc_start_server_monitor``、``/mc_stop_server_monitor``、``/mc_reset_monitor``。

相对上游 0.1.0 的改动（v1.0.0，与 TS3 插件 v3 同思路）：
- **上下线滞回判定**：单次 SLP 探测失败不再直接推“已离线”，需连续
  ``confirm_offline`` 次失败采样确认（恢复在线同理），避免抖动刷屏。
- **单台服务器异常隔离**：任一台服务器轮询抛异常不会让整个监控循环退出
  （旧版异常处理包在 while 外层，一旦有未捕获异常监控会永久停止）。
- **生命周期竞态修复**：延迟自动启动任务被持有并在 terminate 取消，杜绝
  插件重载后残留僵尸 monitor 任务。
- **失联降频**：服务器连续拉取失败后检测间隔自动退避到 60s。
- **推送目标支持 UMO**：``/mc_push_target <UMO|QQ群号|本群|清除>``、``/mc_push_test``，
  聊天下令设置持久化于 ``data/plugin_data/.../relay_state.json``，
  优先级高于 WebUI 面板的 ``target_umo`` / ``target_group``。
- 发送优先走官方 ``context.send_message(umo, ...)``；UMO 为
  ``GroupMessage`` + 纯数字群号时失败自动回退旧版 ``send_group_msg``。

服务器配置 / 启停仍通过 AstrBot WebUI（``_conf_schema.json``）管理。
"""

import asyncio
import os
import sys

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from minecraft_multi_monitor.clients import build_status_client
from minecraft_multi_monitor.config import (
    build_initial_states,
    load_servers_from_config,
    load_settings,
    parse_umo,
)
from minecraft_multi_monitor.models import ServerConfig, ServerSnapshot, ServerState
from minecraft_multi_monitor.monitoring import (
    DEGRADE_AFTER_FAILURES,
    DEGRADED_POLL_INTERVAL,
    DEFAULT_STATUS_CONFIRM_OFFLINE,
    DEFAULT_STATUS_CONFIRM_ONLINE,
    detect_changes,
    format_server_info,
)
from minecraft_multi_monitor.services import describe_target, get_hitokoto, notify
from minecraft_multi_monitor.state import (
    clear_runtime_target,
    load_runtime_target,
    save_runtime_target,
)


def _command_rest(event: AstrMessageEvent) -> str:
    """取 ``/命令`` 之后的参数字符串（不含命令本身）。"""
    raw = (getattr(event, "message_str", None) or "").strip()
    parts = raw.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


# `@register(...)` 在 AstrBot 4.x 中已 deprecated 但仍可用（详见 TS3 main.py 注释）。
# 我们保留它作为向后兼容，并在 `metadata.yaml` 里维护权威元数据。
@register("astrbot_plugin_mc_server_guard", "duanduanjujiu", "Minecraft 多服务器监控插件", "1.0.1", repo="https://github.com/duanduanjujiu/astrbot_plugin_mc_server_guard")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        self.settings = load_settings(self.config)
        self.task: asyncio.Task | None = None
        self._delayed_start_task: asyncio.Task | None = None
        self._terminated = False

        # ---- 推送目标（聊天下令的运行时覆盖 > WebUI 面板配置）----
        runtime_target = load_runtime_target()
        self._umo_override: str | None = runtime_target.umo
        self._group_override: str | None = runtime_target.group_id

        self.servers = load_servers_from_config(
            self.config, self.settings.global_check_interval
        )
        self.server_states = build_initial_states(self.servers)
        self.server_locks = {server.key: asyncio.Lock() for server in self.servers}
        self.status_client = build_status_client()

        logger.info(
            "Minecraft 多服务器监控插件已加载，"
            f"推送目标: {self._target_desc()}, 自动启动: {self.settings.enable_auto_monitor}, "
            f"服务器数: {len(self.servers)}, 查询方式: 本地 SLP 协议直连"
        )

        if self.settings.enable_auto_monitor and self.servers:
            self._delayed_start_task = asyncio.create_task(self._delayed_auto_start())

    async def initialize(self):
        logger.info("Minecraft 多服务器监控插件初始化完成")

    async def terminate(self):
        """停止插件：取消延迟启动 + 主监控任务，避免残留僵尸协程。"""
        self._terminated = True
        pending = [
            t
            for t in (self._delayed_start_task, self.task)
            if t is not None and not t.done()
        ]
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"Minecraft 任务终止时异常: {exc!r}")
        logger.info("Minecraft 插件已停止（监控任务已取消）")

    # ------------------------------------------------------------------
    # 推送目标解析
    # ------------------------------------------------------------------

    def _effective_umo(self) -> str | None:
        return self._umo_override or self.settings.target_umo or None

    def _effective_group_id(self) -> str | None:
        return self._group_override or self.settings.target_group or None

    def _target_desc(self) -> str:
        return describe_target(
            umo=self._effective_umo(), group_id=self._effective_group_id()
        )

    # ------------------------------------------------------------------
    # 生命周期 / 自动启动
    # ------------------------------------------------------------------

    async def _delayed_auto_start(self):
        try:
            await asyncio.sleep(5)
            if self._terminated:
                return  # 插件已被重载/卸载，放弃启动（防僵尸 monitor）
            if not self.task or self.task.done():
                self.task = asyncio.create_task(self.monitor_loop())
                self.task.add_done_callback(self._on_monitor_task_done)
                logger.info("Minecraft 多服务器监控已自动启动")
        except asyncio.CancelledError:
            logger.info("Minecraft 延迟自动启动被取消")
            raise
        except Exception as exc:
            logger.error(f"Minecraft 自动启动失败: {exc}", exc_info=True)

    def _on_monitor_task_done(self, fut: asyncio.Task) -> None:
        """monitor task 结束时回调：正常取消不打日志，异常则打完整堆栈。"""
        if fut.cancelled():
            logger.info("Minecraft monitor 任务被取消")
            return
        try:
            exc = fut.exception()
        except asyncio.CancelledError:
            logger.info("Minecraft monitor 任务被取消")
            return
        if exc is not None:
            logger.error(
                f"Minecraft monitor 任务异常退出: {exc!r}",
                exc_info=exc,
            )

    async def _fetch_server_data(self, server: ServerConfig, source: str):
        """拉取单台服务器的状态。

        当本地 SLP 探测失败（超时、连接拒绝、协议错误等）时，本地直连意味着
        服务器大概率已离线，因此这里直接构造一个 status="offline" 的 snapshot
        以便 detect_changes 能（在连续失败确认后）识别"在线 → 离线"的事件。
        """
        from datetime import datetime

        from minecraft_multi_monitor.models import ServerSnapshot

        lock = self.server_locks.setdefault(server.key, asyncio.Lock())
        async with lock:
            snapshot = await self.status_client.fetch_status(server, source)
            if snapshot is not None:
                return snapshot

            now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return ServerSnapshot(
                key=server.key,
                name=server.server_name,
                server_ip=server.server_ip,
                server_port=server.server_port,
                server_type=server.server_type,
                status="offline",
                motd="",
                version="未知",
                protocol="未知",
                online=0,
                max_players=0,
                players=[],
                software="未知",
                map_name="未知",
                update_time=now_text,
                host=f"{server.server_ip}:{server.server_port}",
                server_address=server.server_address,
                latency_ms=None,
                error="本地 SLP 探测失败（超时或连接被拒绝）",
            )

    async def get_all_server_status_text(self) -> str:
        if not self.servers:
            return "❌ 当前没有启用的服务器配置"

        blocks: list[str] = []
        for index, server in enumerate(self.servers, start=1):
            snapshot = await self._fetch_server_data(server, source="manual")
            blocks.append(
                f"===== {index}. {server.server_name} =====\n"
                f"{format_server_info(snapshot, self.settings.display_options)}"
            )
        return "\n\n".join(blocks)

    async def monitor_loop(self):
        logger.info("Minecraft monitor_loop 已启动")
        try:
            while True:
                await asyncio.sleep(1)
                if not self.servers:
                    continue
                for server in list(self.servers):
                    state = self.server_states.setdefault(server.key, ServerState())
                    try:
                        await self._monitor_tick(server, state)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # 单台服务器故障不影响其它服务器，也不让整个循环退出
                        logger.error(
                            f"[monitor] [{server.server_name}] 单服务器轮询异常: {exc!r}",
                            exc_info=True,
                        )
        except asyncio.CancelledError:
            logger.info("Minecraft 监控循环已取消")
            raise
        except Exception as exc:
            logger.error(f"Minecraft 监控循环异常: {exc!r}", exc_info=True)

    async def _monitor_tick(self, server: ServerConfig, state: ServerState) -> None:
        """单台服务器的一个检测节拍。"""
        now = asyncio.get_running_loop().time()

        # 失联降频：连续失败超过阈值后拉长检测间隔
        interval: float = float(server.check_interval)
        if state.consecutive_failures >= DEGRADE_AFTER_FAILURES:
            interval = max(interval, DEGRADED_POLL_INTERVAL)
        if now - state.last_check_ts < interval:
            return
        state.last_check_ts = now

        snapshot = await self._fetch_server_data(server, source="monitor")
        if snapshot is None:
            logger.warning(f"[monitor] [{server.server_name}] 本次未获取到服务器状态")
            return

        # 健康统计：任何一次拉取成功/失败都计入（用于降频与上下线判定）
        if snapshot.status == "online":
            state.consecutive_failures = 0
            state.last_success_ts = now
        else:
            state.consecutive_failures += 1

        changed, message = detect_changes(
            server,
            state,
            snapshot,
            confirm_online=DEFAULT_STATUS_CONFIRM_ONLINE,
            confirm_offline=DEFAULT_STATUS_CONFIRM_OFFLINE,
        )
        if not changed or not message:
            # 无状态变化：只保留 DEBUG 级日志，避免每轮刷 INFO
            logger.debug(
                f"[monitor] [{server.server_name}] 无状态变化: "
                f"status={snapshot.status}, "
                f"players={snapshot.online}/{snapshot.max_players}"
            )
            return

        logger.info(f"[monitor] [{server.server_name}] 检测到状态变化: {message}")
        logger.info(
            f"[monitor] [{server.server_name}] 变化详情: "
            f"status={snapshot.status}, "
            f"players={snapshot.online}/{snapshot.max_players}, "
            f"names={snapshot.players}"
        )
        detail = format_server_info(snapshot, self.settings.display_options)
        final_message = f"🔔 服务器状态变化：\n{message}\n\n📊 当前状态：\n{detail}"
        if self.settings.display_options.show_hitokoto:
            hitokoto = await get_hitokoto()
            if hitokoto:
                final_message += f"\n\n💬 {hitokoto}"

        sent = await self._send_notification(final_message)
        if not sent:
            logger.error(
                f"[monitor] [{server.server_name}] 状态变化已识别，但通知发送失败"
            )

    async def _send_notification(self, final_text: str) -> bool:
        umo = self._effective_umo()
        group_id = self._effective_group_id()
        sent = await notify(self.context, final_text, umo=umo, group_id=group_id)
        if not sent:
            logger.error(
                f"通知发送失败，当前推送目标: {describe_target(umo=umo, group_id=group_id)}"
            )
        return sent

    async def _send_test(
        self, *, umo: str | None, group_id: str | None, cause: str
    ) -> str:
        """设置目标后立即发一条测试消息，返回给用户看的回执文本。"""
        if umo:
            dest = f"UMO: {umo}"
        elif group_id:
            dest = f"QQ群: {group_id}"
        else:
            return "❌ 目标为空，设置失败。"
        final_message = (
            "🔔 Minecraft 监控插件推送测试\n"
            f"推送目标已设置（来源：{cause}）。\n"
            f"当前目标: {dest}\n"
            "这是一条测试消息。"
        )
        sent = await self._send_notification(final_message)
        if sent:
            return f"✅ 设置成功，已向新目标发送测试消息（请查收）。\n当前推送目标: {dest}"
        return (
            f"⚠️ 目标已保存，但测试消息发送失败（请查看 AstrBot 日志）。\n"
            f"当前推送目标: {dest}\n"
            f"请确认 UMO/群号正确、机器人已连接该平台、且机器人未被该群禁言。"
        )

    # ------------------------------------------------------------------
    # 聊天命令
    # ------------------------------------------------------------------

    @filter.command("mc_start_server_monitor")
    async def start_server_monitor_task(self, event: AstrMessageEvent):
        if not self.servers:
            yield event.plain_result("❌ 当前没有启用的服务器配置，请先在 WebUI 中配置 server_entries")
            return

        # 若延迟自动启动还没跑完，先取消它，避免出现两个监控循环
        if self._delayed_start_task and not self._delayed_start_task.done():
            self._delayed_start_task.cancel()
            try:
                await self._delayed_start_task
            except asyncio.CancelledError:
                pass
            self._delayed_start_task = None

        if self.task and not self.task.done():
            yield event.plain_result("✅ 监控任务已经在运行中")
            return

        self.task = asyncio.create_task(self.monitor_loop())
        self.task.add_done_callback(self._on_monitor_task_done)
        yield event.plain_result(f"✅ 多服务器监控已启动，当前监控 {len(self.servers)} 台服务器")

    @filter.command("mc_stop_server_monitor")
    async def stop_server_monitor_task(self, event: AstrMessageEvent):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            yield event.plain_result("✅ 监控任务已停止")
        else:
            yield event.plain_result("❌ 当前没有正在运行的监控任务")

    @filter.command("mc_query")
    async def query_server_status(self, event: AstrMessageEvent):
        text = await self.get_all_server_status_text()
        if self.settings.display_options.show_hitokoto:
            hitokoto = await get_hitokoto()
            if hitokoto:
                text += f"\n\n💬 {hitokoto}"
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mc_reset_monitor")
    async def reset_monitor(self, event: AstrMessageEvent):
        """重置监控缓存（管理员）。先暂停监控再清空，避免与运行中的 monitor_loop 竞态。"""
        # 1) 如果监控正在跑，先停掉（与 mc_stop_server_monitor 一致），避免新旧 state 交替时
        #    运行中的 _monitor_tick 还在修改被丢弃的旧 state 对象。
        was_running = False
        if self.task and not self.task.done():
            was_running = True
            self.task.cancel()
            try:
                await asyncio.wait_for(self.task, timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("reset_monitor: 等待 monitor 任务取消超时（3s）")
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"reset_monitor: 取消 monitor 任务时异常: {exc!r}")

        # 2) 此时 monitor_loop 已完全停住，可以安全地替换 state / lock。
        self.server_states = build_initial_states(self.servers)
        self.server_locks = {server.key: asyncio.Lock() for server in self.servers}

        if was_running:
            yield event.plain_result(
                "✅ 监控状态缓存已重置（监控任务已暂停，使用 /mc_start_server_monitor 重启）"
            )
        else:
            yield event.plain_result("✅ 监控状态缓存已重置")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mc_push_target")
    async def set_push_target(self, event: AstrMessageEvent):
        """查看 / 设置通知推送目标。支持输入 UMO 或 QQ 群号。"""
        rest = _command_rest(event)

        if not rest:
            yield event.plain_result(self._target_help_text())
            return

        lowered = rest.lower()
        if lowered in {"清除", "clear", "reset", "删除", "取消"}:
            clear_runtime_target()
            self._umo_override = None
            self._group_override = None
            yield event.plain_result(
                "已清除聊天下令设置的推送目标，恢复使用 WebUI 面板配置 "
                "(target_umo / target_group)。\n\n"
                f"当前生效推送目标: {self._target_desc()}"
            )
            return

        if lowered in {"本群", "当前", "这里", "当前会话", "here"}:
            umo = (getattr(event, "unified_msg_origin", None) or "").strip()
            if not umo:
                yield event.plain_result(
                    "❌ 无法获取当前会话的 UMO（该平台可能不支持主动消息），"
                    "请改用 /mc_push_target <UMO> 手动指定。"
                )
                return
            save_runtime_target(umo=umo)
            self._umo_override = umo
            self._group_override = None
            reply = await self._send_test(umo=umo, group_id=None, cause="绑定当前会话")
            yield event.plain_result(reply)
            return

        umo = parse_umo(rest)
        if umo:
            save_runtime_target(umo=umo)
            self._umo_override = umo
            self._group_override = None
            reply = await self._send_test(umo=umo, group_id=None, cause="输入 UMO")
            yield event.plain_result(reply)
            return

        if rest.isdigit():
            save_runtime_target(group_id=rest)
            self._group_override = rest
            self._umo_override = None
            reply = await self._send_test(umo=None, group_id=rest, cause="输入 QQ 群号")
            yield event.plain_result(reply)
            return

        yield event.plain_result(
            "❌ 无法识别的参数。\n\n" + self._target_usage_text()
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mc_push_test")
    async def push_test(self, event: AstrMessageEvent):
        """向当前推送目标发送一条测试消息。"""
        umo = self._effective_umo()
        group_id = self._effective_group_id()
        if not umo and not group_id:
            yield event.plain_result(
                "❌ 当前未配置推送目标。\n"
                "请先使用 /mc_push_target <UMO 或 QQ群号> 设置。"
            )
            return
        final_message = (
            "🔔 Minecraft 监控插件推送测试\n"
            "这是一条测试消息。\n"
            f"当前推送目标: {self._target_desc()}"
        )
        sent = await self._send_notification(final_message)
        if sent:
            yield event.plain_result("✅ 测试消息已发送，请到目标会话查收。")
        else:
            yield event.plain_result(
                "❌ 测试消息发送失败（请查看 AstrBot 日志）。\n"
                f"当前推送目标: {self._target_desc()}"
            )

    def _target_usage_text(self) -> str:
        return (
            "【用法】（管理员）\n"
            "/mc_push_target <UMO>        按 UMO 设置，例如：\n"
            "                             /mc_push_target atri:GroupMessage:1092815819\n"
            "/mc_push_target <QQ群号>     兼容旧版纯群号，例如：/mc_push_target 123456789\n"
            "/mc_push_target 本群         把当前会话设为推送目标\n"
            "/mc_push_target 清除         恢复使用 WebUI 面板配置\n"
            "/mc_push_test                 向当前目标发送测试消息"
        )

    def _target_help_text(self) -> str:
        return (
            f"当前生效推送目标: {self._target_desc()}\n\n"
            + self._target_usage_text()
        )


__all__ = ["MyPlugin"]
