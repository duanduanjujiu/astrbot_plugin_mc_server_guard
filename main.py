"""AstrBot Minecraft 多服务器监控插件（AI 修改版 v1.2.1）。

v1.2.1（修复插件热更新后无法加载）：
- AstrBot 重载插件时只会移除 ``data.plugins.<plugin>`` 模块，但本插件通过
  ``sys.path`` 以顶层包名导入 ``minecraft_multi_monitor``。旧子包仍留在
  ``sys.modules`` 中，热更新后新增的类 / 函数（如 ``FetchResult``）会
  ``ImportError``。现于导入子包前清理指向本插件目录的同名顶层包缓存，
  插件更新后无需重启 AstrBot，直接重载即可生效。

v1.2.0（运行时健壮性与运维改进）：
- **通知异步化**：状态变化通知入队后由后台 worker 发送，平台不可用 / 发送慢时
  不再阻塞监控循环（队列满时丢弃最旧通知）。
- **/mc_reload**（管理员）：重新读取 WebUI 配置并重建监控状态，修改配置后无需
  手动重载插件；旧的 DNS 缓存会一并清空。
- **错误分类**：区分 DNS/SRV 解析失败与服务器不可达。解析失败只跳过本轮，
  不再误报"已离线"。
- **DNS 缓存**：解析结果缓存 5 分钟（失败缓存 30 秒），避免每轮轮询都查 DNS。
- **IPv6 地址**：支持 ``[2001:db8::1]:25566`` / 裸 IPv6 字面量；旧版
  ``server_ip`` 字段也会自动加方括号。
- **/mc_query 并发 + 限流**：多台服务器并发探测，并加 3 秒全局冷却，
  防止高频调用触发大量探测。
- **ADMIN 门禁**：``/mc_start_server_monitor``、``/mc_stop_server_monitor``
  与 reset / reload / push_* 一致，仅管理员可用。
- **日志降噪**：已降级的失联服务器把逐次重试的 WARNING 降为 DEBUG。
- 清理死代码（未使用的 ``software`` / ``map_name`` / ``last_success_ts`` 等），
  延迟（``latency_ms``）现在会展示在状态消息中。

v1.1.1（协议健壮性修复）：
- 超时不再回退 Legacy Ping：Python 3.11+ 的 ``asyncio.TimeoutError`` 是
  ``OSError`` 的子类，旧逻辑会把"服务器不可达"当成"该换协议"，导致离线服务器
  单轮探测耗时翻倍（5s 超时 × 2 协议 × 2 重试）。
- Legacy Ping 改为循环读取直到 EOF / 空闲超时，修复响应被拆成多个 TCP 段时
  只读到半包、解析出错误玩家数与 MOTD 的问题。
- 多台服务器改为并发探测（``asyncio.gather``），单台慢服务器不再拖累其他服务器。
- SRV 解析后的 A 记录选择修正：真正按"IPv4 优先"返回，而不是固定返回第一条。
- Bedrock 版未显式填写端口时默认 19132（旧逻辑固定 25565，导致基岩服连不上）。
- 移除已废弃的 ``@register`` 装饰器（AstrBot 3.5.19+ 会自动识别 ``Star`` 子类，
  元数据以 ``metadata.yaml`` 为准）。
- 新增 ``tests/`` 单元测试（``python -m unittest discover -s tests -v``）。

v1.0.1：
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
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


def _purge_stale_package(package_name: str) -> None:
    """清理 sys.modules 中指向本插件目录的同名顶层包缓存。

    AstrBot 重载插件只移除 ``data.plugins.<plugin>`` 模块，而 main.py 通过
    ``sys.path`` 以顶层包名导入 ``minecraft_multi_monitor``。如果不清缓存，
    热更新后仍会命中旧子包，新增的类 / 函数就会 ImportError。
    只清理 ``__file__`` 位于本插件目录的模块，避免误删其他插件的同名包。
    """
    prefix = package_name + "."
    for module_name in list(sys.modules):
        if module_name != package_name and not module_name.startswith(prefix):
            continue
        module = sys.modules.get(module_name)
        location = getattr(module, "__file__", None)
        if not location:
            sys.modules.pop(module_name, None)
            continue
        try:
            inside_plugin = os.path.abspath(location).startswith(PLUGIN_DIR + os.sep)
        except (OSError, TypeError):
            inside_plugin = False
        if inside_plugin:
            sys.modules.pop(module_name, None)


_purge_stale_package("minecraft_multi_monitor")

from minecraft_multi_monitor.address import format_host_port
from minecraft_multi_monitor.clients import build_status_client
from minecraft_multi_monitor.config import (
    build_initial_states,
    load_servers_from_config,
    load_settings,
    parse_umo,
)
from minecraft_multi_monitor.models import FetchResult, ServerConfig, ServerSnapshot, ServerState
from minecraft_multi_monitor.monitoring import (
    DEFAULT_STATUS_CONFIRM_OFFLINE,
    DEFAULT_STATUS_CONFIRM_ONLINE,
    DEGRADE_AFTER_FAILURES,
    DEGRADED_POLL_INTERVAL,
    detect_changes,
    format_server_info,
)
from minecraft_multi_monitor.services import describe_target, get_hitokoto, notify
from minecraft_multi_monitor.srv_resolver import clear_resolve_cache
from minecraft_multi_monitor.state import (
    clear_runtime_target,
    load_runtime_target,
    save_runtime_target,
)

# 通知发送队列上限：发送失败/平台不可用时最多积压这么多条通知，超过则丢弃最旧的。
NOTIFY_QUEUE_MAXSIZE = 100

# 手动查询冷却（秒）：防止任何成员高频刷 /mc_query 触发大量探测。
QUERY_COOLDOWN_SECONDS = 3.0


def _command_rest(event: AstrMessageEvent) -> str:
    """取 ``/命令`` 之后的参数字符串（不含命令本身）。"""
    raw = (getattr(event, "message_str", None) or "").strip()
    parts = raw.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


# AstrBot 3.5.19+ 会自动识别继承自 ``Star`` 的插件类，无需再使用已废弃的
# ``@register`` 装饰器；插件元数据以 ``metadata.yaml`` 为准（见官方开发文档）。
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

        self.servers = load_servers_from_config(self.config, self.settings.global_check_interval)
        self.server_states = build_initial_states(self.servers)
        self.server_locks = {server.key: asyncio.Lock() for server in self.servers}
        self.status_client = build_status_client()

        # 通知改为队列 + 后台 worker 异步发送：发送慢（平台不可用最长等 12s）时
        # 不会阻塞监控循环或拖慢其他服务器的检测节拍。
        self._notify_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=NOTIFY_QUEUE_MAXSIZE)
        self._notify_task: asyncio.Task | None = None
        self._last_query_at = 0.0

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
            t for t in (self._delayed_start_task, self.task, self._notify_task) if t is not None and not t.done()
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
        return describe_target(umo=self._effective_umo(), group_id=self._effective_group_id())

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

    async def _fetch_server_data(self, server: ServerConfig, source: str, *, quiet: bool = False) -> FetchResult:
        """拉取单台服务器的状态，返回 :class:`FetchResult`。

        - 成功：``snapshot`` 为在线快照；
        - 失败但属于"服务器不可达"（超时 / 网络错误 / 协议异常）：合成一个
          ``status="offline"`` 的快照，供 detect_changes 在连续确认后上报"已离线"；
        - 地址解析失败（DNS / SRV）：``snapshot`` 为 None、``error_kind="resolve"``，
          调用方应跳过本轮——本机 DNS 故障不代表服务器离线。

        Args:
            quiet: 降级中的服务器传 True，把逐次重试的 WARNING 日志降到 DEBUG。
        """
        lock = self.server_locks.setdefault(server.key, asyncio.Lock())
        async with lock:
            result = await self.status_client.fetch_status(server, source, quiet=quiet)

        if result.snapshot is not None or result.error_kind == "resolve":
            return result

        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        snapshot = ServerSnapshot(
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
            update_time=now_text,
            host=format_host_port(server.server_ip, server.server_port),
            server_address=server.server_address,
            latency_ms=None,
            error=result.error_message or "本地探测失败",
        )
        return FetchResult(snapshot, result.error_kind, result.error_message)

    async def get_all_server_status_text(self) -> str:
        if not self.servers:
            return "❌ 当前没有启用的服务器配置"

        # 并发探测：多台服务器（含离线服务器）不会互相排队等待。
        servers = list(self.servers)
        results = await asyncio.gather(
            *(self._fetch_server_data(server, source="manual") for server in servers),
            return_exceptions=True,
        )

        blocks: list[str] = []
        for index, (server, result) in enumerate(zip(servers, results), start=1):
            if isinstance(result, BaseException):
                detail = f"❌ 查询异常：{result!r}"
            elif result.snapshot is None:
                detail = f"❌ 无法取值：{result.error_message or result.error_kind or '未知错误'}"
            else:
                detail = format_server_info(result.snapshot, self.settings.display_options)
            blocks.append(f"===== {index}. {server.server_name} =====\n{detail}")
        return "\n\n".join(blocks)

    async def monitor_loop(self):
        logger.info("Minecraft monitor_loop 已启动")
        try:
            while True:
                await asyncio.sleep(1)
                if not self.servers:
                    continue
                # 并发探测：单台服务器超时（最长约 10s）不再阻塞其他服务器的节拍。
                # _safe_monitor_tick 内部已做异常隔离，gather 不会因单台故障中断。
                await asyncio.gather(*(self._safe_monitor_tick(server) for server in list(self.servers)))
        except asyncio.CancelledError:
            logger.info("Minecraft 监控循环已取消")
            raise
        except Exception as exc:
            logger.error(f"Minecraft 监控循环异常: {exc!r}", exc_info=True)

    async def _safe_monitor_tick(self, server: ServerConfig) -> None:
        """执行单台服务器的一个检测节拍，吞掉非取消异常，避免影响其他服务器。"""
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

        degraded = state.consecutive_failures >= DEGRADE_AFTER_FAILURES
        result = await self._fetch_server_data(server, source="monitor", quiet=degraded)
        if result.snapshot is None:
            # 地址解析失败不代表服务器离线（可能是本机 DNS 故障）：跳过本轮，
            # 不修改上下线状态，也不计入 consecutive_failures。
            logger.warning(
                f"[monitor] [{server.server_name}] 地址解析失败，跳过本轮（不计入掉线）: "
                f"{result.error_message or result.error_kind}"
            )
            return

        snapshot = result.snapshot
        # 健康统计：任何一次拉取成功/失败都计入（用于降频与上下线判定）
        if snapshot.status == "online":
            state.consecutive_failures = 0
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

        # 入队后立即返回：发送由后台 worker 负责，避免平台发送慢拖住监控循环。
        self._queue_notification(final_message)

    async def _send_notification(self, final_text: str) -> bool:
        umo = self._effective_umo()
        group_id = self._effective_group_id()
        sent = await notify(self.context, final_text, umo=umo, group_id=group_id)
        if not sent:
            logger.error(f"通知发送失败，当前推送目标: {describe_target(umo=umo, group_id=group_id)}")
        return sent

    # ------------------------------------------------------------------
    # 通知队列（异步发送）
    # ------------------------------------------------------------------

    def _ensure_notify_worker(self) -> None:
        """确保后台通知 worker 正在运行。"""
        if self._notify_task is None or self._notify_task.done():
            self._notify_task = asyncio.create_task(self._notify_worker())
            self._notify_task.add_done_callback(self._on_notify_task_done)

    def _on_notify_task_done(self, fut: asyncio.Task) -> None:
        if fut.cancelled():
            return
        try:
            exc = fut.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error(f"Minecraft 通知 worker 异常退出: {exc!r}", exc_info=exc)

    async def _notify_worker(self) -> None:
        """串行消费通知队列；发送失败只记日志，不影响监控循环。"""
        while True:
            text = await self._notify_queue.get()
            try:
                sent = await self._send_notification(text)
                if not sent:
                    logger.error("通知发送失败（已丢弃该条，监控循环不受影响）")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"通知发送异常: {exc!r}", exc_info=True)
            finally:
                self._notify_queue.task_done()

    def _queue_notification(self, text: str) -> None:
        """把通知放入发送队列；队列满时丢弃最旧的一条，保留最新状态。"""
        self._ensure_notify_worker()
        try:
            self._notify_queue.put_nowait(text)
            return
        except asyncio.QueueFull:
            pass

        try:
            dropped = self._notify_queue.get_nowait()
            self._notify_queue.task_done()
            logger.warning(f"通知队列已满，丢弃最旧通知: {dropped[:50]}...")
        except asyncio.QueueEmpty:
            pass
        try:
            self._notify_queue.put_nowait(text)
        except asyncio.QueueFull:
            logger.error("通知队列已满，最新通知也未能入队，已丢弃")

    async def _send_test(self, *, umo: str | None, group_id: str | None, cause: str) -> str:
        """设置目标后立即发一条测试消息，返回给用户看的回执文本。"""
        if umo:
            dest = f"UMO: {umo}"
        elif group_id:
            dest = f"QQ群: {group_id}"
        else:
            return "❌ 目标为空，设置失败。"
        final_message = (
            f"🔔 Minecraft 监控插件推送测试\n推送目标已设置（来源：{cause}）。\n当前目标: {dest}\n这是一条测试消息。"
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mc_start_server_monitor")
    async def start_server_monitor_task(self, event: AstrMessageEvent):
        """启动监控任务（管理员）。"""
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mc_stop_server_monitor")
    async def stop_server_monitor_task(self, event: AstrMessageEvent):
        """停止监控任务（管理员）。"""
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
        """立即查询全部服务器状态（对所有用户开放，带全局冷却）。"""
        now = asyncio.get_running_loop().time()
        remaining = QUERY_COOLDOWN_SECONDS - (now - self._last_query_at)
        if remaining > 0:
            yield event.plain_result(f"⏳ 查询太频繁，请 {int(remaining) + 1} 秒后再试")
            return
        self._last_query_at = now

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
            yield event.plain_result("✅ 监控状态缓存已重置（监控任务已暂停，使用 /mc_start_server_monitor 重启）")
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
                    "❌ 无法获取当前会话的 UMO（该平台可能不支持主动消息），请改用 /mc_push_target <UMO> 手动指定。"
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

        yield event.plain_result("❌ 无法识别的参数。\n\n" + self._target_usage_text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mc_push_test")
    async def push_test(self, event: AstrMessageEvent):
        """向当前推送目标发送一条测试消息。"""
        umo = self._effective_umo()
        group_id = self._effective_group_id()
        if not umo and not group_id:
            yield event.plain_result("❌ 当前未配置推送目标。\n请先使用 /mc_push_target <UMO 或 QQ群号> 设置。")
            return
        final_message = f"🔔 Minecraft 监控插件推送测试\n这是一条测试消息。\n当前推送目标: {self._target_desc()}"
        sent = await self._send_notification(final_message)
        if sent:
            yield event.plain_result("✅ 测试消息已发送，请到目标会话查收。")
        else:
            yield event.plain_result(
                f"❌ 测试消息发送失败（请查看 AstrBot 日志）。\n当前推送目标: {self._target_desc()}"
            )

    # ------------------------------------------------------------------
    # 配置重载
    # ------------------------------------------------------------------

    def _reload_runtime(self) -> None:
        """从 ``self.config`` 重新加载设置 / 服务器列表，并重建运行时状态。"""
        self.settings = load_settings(self.config)
        self.servers = load_servers_from_config(self.config, self.settings.global_check_interval)
        self.server_states = build_initial_states(self.servers)
        self.server_locks = {server.key: asyncio.Lock() for server in self.servers}
        self.status_client = build_status_client()
        # 配置可能改了地址，清空 DNS 缓存让新地址立即生效
        clear_resolve_cache()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mc_reload")
    async def reload_config(self, event: AstrMessageEvent):
        """重新读取 WebUI 配置并重建监控状态（管理员）。"""
        was_running = self.task is not None and not self.task.done()
        if was_running:
            self.task.cancel()
            try:
                await asyncio.wait_for(self.task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as exc:
                logger.warning(f"mc_reload: 停止旧监控任务时异常: {exc!r}")
            self.task = None

        self._reload_runtime()

        if was_running and self.servers:
            self.task = asyncio.create_task(self.monitor_loop())
            self.task.add_done_callback(self._on_monitor_task_done)

        status = "已自动重启监控" if was_running and self.servers else "监控当前未运行"
        yield event.plain_result(
            f"✅ 配置已重新加载。\n"
            f"服务器数: {len(self.servers)}\n"
            f"检测间隔: {self.settings.global_check_interval}s\n"
            f"推送目标: {self._target_desc()}\n"
            f"{status}（如修改了 WebUI 配置且此处未生效，请在插件页重载插件）"
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
        return f"当前生效推送目标: {self._target_desc()}\n\n" + self._target_usage_text()


__all__ = ["MyPlugin"]
