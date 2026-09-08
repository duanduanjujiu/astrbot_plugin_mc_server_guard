"""通知服务（群消息 / UMO 目标）与一言。

目标有两种形态（至多一种生效，见 main.py 的解析逻辑）：
1. **UMO**（``<平台实例名>:<消息类型>:<会话ID>``，如 ``atri:GroupMessage:1092815819``）
   —— 走官方 ``context.send_message(umo, chain)``，适配任意平台 / 群聊 / 私聊；
2. **旧版纯 QQ 群号**（``1092815819``）
   —— 兼容旧版：在已注册的适配器里找支持 ``send_group_msg`` 的客户端发送。

UMO 发送失败时，如果它的会话 ID 是纯数字且消息类型是 GroupMessage，
会自动回退到第 2 种路径（QQ 群号），提高对 NapCat 等 OneBot 协议端的兼容性。
"""

import asyncio

import aiohttp
from astrbot.api import logger
from astrbot.api.event.filter import PlatformAdapterType

# 按优先级尝试的平台适配器（旧版 QQ 群号路径）
_PLATFORM_CANDIDATES = ("AIOCQHTTP", "QQ_OFFICIAL", "SATORI")

# 群消息发送失败时的重试次数与间隔。实测 NapCat/QQ 偶发 sendMsg 超时
# （ActionFailed retcode=1200），重试通常能成功送达。
SEND_MAX_ATTEMPTS = 3
SEND_RETRY_INTERVAL = 3.0

# 等待可用平台适配器出现的超时（秒）
PLATFORM_WAIT_TIMEOUT = 12.0


async def get_hitokoto() -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://v1.hitokoto.cn/?encode=text",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                if response.status == 200:
                    return (await response.text()).strip()
    except Exception as exc:
        logger.warning(f"获取一言失败: {exc}")
    return None


def describe_target(*, umo: str | None = None, group_id: str | None = None) -> str:
    """返回人类可读的目标描述（用于日志 / 命令回显）。"""
    if umo:
        return f"UMO: {umo}"
    if group_id:
        return f"QQ群: {group_id}"
    return "未配置"


def _looks_like_qq_group_umo(umo: str) -> bool:
    """UMO 是否形如 ``<prefix>:GroupMessage:<纯数字群号>``（可回退到群号发送）。"""
    try:
        _, msg_type, session = umo.split(":", 2)
    except ValueError:
        return False
    return msg_type.lower() == "groupmessage" and session.isdigit()


async def _send_by_umo(context, umo: str, text: str) -> bool:
    """通过 AstrBot 官方 context.send_message 向 UMO 发送纯文本。"""
    try:
        try:
            from astrbot.api.event import MessageChain
        except ImportError:  # 兼容较老版本：MessageChain 位于 core 下
            from astrbot.core.message.message_event_result import MessageChain

        chain = MessageChain().message(text)
        logger.info(
            f"通过 UMO 发送消息到 {umo}，消息长度: {len(text)}"
        )
        ok = await context.send_message(umo, chain)
        if ok:
            logger.info(f"UMO 消息发送成功: {umo}")
            return True
        logger.warning(f"UMO 消息发送失败(返回空/False): {umo}")
        return False
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(f"UMO 消息发送异常 {umo}: {exc}")
        return False


async def _resolve_platform(context):
    """在已注册的适配器里找一个支持 send_group_msg 的。"""
    for adapter_name in _PLATFORM_CANDIDATES:
        adapter_type = getattr(PlatformAdapterType, adapter_name, None)
        if adapter_type is None:
            continue
        candidate = context.get_platform(adapter_type)
        if candidate and hasattr(candidate, "get_client"):
            return candidate, adapter_name
    return None, None


async def wait_for_platform_ready(context, timeout_seconds: float = PLATFORM_WAIT_TIMEOUT, interval_seconds: float = 0.5):
    """循环等待至少一个可用平台适配器出现。"""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        platform, adapter_name = await _resolve_platform(context)
        if platform:
            logger.info(f"通知发送使用平台适配器: {adapter_name}")
            return platform
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(interval_seconds)


async def _send_to_qq_group(context, group_id: str, text: str) -> bool:
    """旧版路径：用 OneBot 风格 ``send_group_msg`` 向纯数字 QQ 群号发送。"""
    if not group_id or not group_id.isdigit():
        logger.error(f"QQ 群号不合法: {group_id!r}")
        return False

    try:
        platform = await wait_for_platform_ready(context)
        if not platform:
            logger.error("无法获取可用的平台客户端，无法发送群消息")
            return False

        client = platform.get_client()
        last_error: Exception | object | None = None
        for attempt in range(1, SEND_MAX_ATTEMPTS + 1):
            try:
                logger.info(
                    f"开始发送群消息到 {group_id}（第 {attempt}/{SEND_MAX_ATTEMPTS} 次），"
                    f"消息长度: {len(text)}"
                )
                result = await client.api.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=text,
                )
                message_id = None
                if isinstance(result, dict):
                    message_id = result.get("message_id") or result.get("msg_id")
                if message_id is not None:
                    logger.info(
                        f"群消息发送成功，message_id={message_id}（第 {attempt} 次尝试）"
                    )
                    return True

                last_error = result
                logger.warning(f"发送群消息失败（第 {attempt} 次）: {result}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(f"发送群消息异常（第 {attempt} 次）: {exc}")

            if attempt < SEND_MAX_ATTEMPTS:
                await asyncio.sleep(SEND_RETRY_INTERVAL)

        logger.error(
            f"群消息重试 {SEND_MAX_ATTEMPTS} 次后仍发送失败: {last_error}；"
            f"消息内容预览: {text[:200]}"
        )
        return False
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(f"发送群消息异常: {exc}")
        return False


async def notify(
    context,
    text: str,
    *,
    umo: str | None = None,
    group_id: str | None = None,
) -> bool:
    """把 ``text`` 推送到目标（UMO 优先，其次 QQ 群号）。

    Returns:
        是否至少有一次成功送达。
    """
    if not text:
        return False

    if umo:
        ok = await _send_by_umo(context, umo, text)
        if ok:
            return True
        # 兼容回退：UMO 指向的是 QQ 群（GroupMessage + 纯数字）
        if _looks_like_qq_group_umo(umo):
            fallback_group = umo.split(":", 2)[2]
            logger.warning(
                f"UMO 发送失败，回退到群号发送: {fallback_group}（原因见上方日志）"
            )
            return await _send_to_qq_group(context, fallback_group, text)
        return False

    if group_id:
        return await _send_to_qq_group(context, group_id, text)

    logger.error("推送目标未配置（UMO 与 QQ 群号均为空），无法发送通知")
    return False
