# -*- coding: utf-8 -*-
"""
===================================
Bot Webhook 处理器
===================================

处理各平台的 Webhook 回调，分发到命令处理器。
"""

import json
import logging
import threading
from typing import Dict, Any, Optional, TYPE_CHECKING

from bot.models import BotResponse, WebhookResponse
from bot.dispatcher import get_dispatcher
from bot.platforms import ALL_PLATFORMS

if TYPE_CHECKING:
    from bot.platforms.base import BotPlatform

logger = logging.getLogger(__name__)

# 平台实例缓存
_platform_instances: Dict[str, 'BotPlatform'] = {}


def get_platform(platform_name: str) -> Optional['BotPlatform']:
    """
    获取平台适配器实例
    
    使用缓存避免重复创建。
    
    Args:
        platform_name: 平台名称
        
    Returns:
        平台适配器实例，或 None
    """
    if platform_name not in _platform_instances:
        platform_class = ALL_PLATFORMS.get(platform_name)
        if platform_class:
            _platform_instances[platform_name] = platform_class()
        else:
            logger.warning(f"[BotHandler] 未知平台: {platform_name}")
            return None
    
    return _platform_instances[platform_name]


def handle_webhook(
    platform_name: str,
    headers: Dict[str, str],
    body: bytes,
    query_params: Optional[Dict[str, list]] = None
) -> WebhookResponse:
    """
    处理 Webhook 请求
    
    这是所有平台 Webhook 的统一入口。
    
    Args:
        platform_name: 平台名称 (feishu, dingtalk, wecom, telegram)
        headers: HTTP 请求头
        body: 请求体原始字节
        query_params: URL 查询参数（用于某些平台的验证）
        
    Returns:
        WebhookResponse 响应对象
    """
    logger.info(f"[BotHandler] 收到 {platform_name} Webhook 请求")
    
    # 检查机器人功能是否启用
    from src.config import get_config
    config = get_config()
    
    if not getattr(config, 'bot_enabled', True):
        logger.info("[BotHandler] 机器人功能未启用")
        return WebhookResponse.success()
    
    # 获取平台适配器
    platform = get_platform(platform_name)
    if not platform:
        return WebhookResponse.error(f"Unknown platform: {platform_name}", 400)
    
    # 解析 JSON 数据
    try:
        data = json.loads(body.decode('utf-8')) if body else {}
    except json.JSONDecodeError as e:
        logger.error(f"[BotHandler] JSON 解析失败: {e}")
        return WebhookResponse.error("Invalid JSON", 400)
    
    logger.debug(f"[BotHandler] 请求数据: {json.dumps(data, ensure_ascii=False)[:500]}")
    
    # 处理 Webhook
    message, challenge_response = platform.handle_webhook(headers, body, data)
    
    # 如果是验证请求，直接返回验证响应
    if challenge_response:
        logger.info(f"[BotHandler] 返回验证响应")
        return challenge_response
    
    # 如果没有消息需要处理，返回空响应
    if not message:
        logger.debug("[BotHandler] 无需处理的消息")
        return WebhookResponse.success()
    
    logger.info(f"[BotHandler] 解析到消息: user={message.user_name}, content={message.content[:50]}")
    
    # 分发到命令处理器
    dispatcher = get_dispatcher()
    response = dispatcher.dispatch(message)
    
    # 格式化响应
    if response.text:
        webhook_response = platform.format_response(response, message)
        return webhook_response
    
    return WebhookResponse.success()


def handle_feishu_webhook(headers: Dict[str, str], body: bytes) -> WebhookResponse:
    """处理飞书 Webhook"""
    return handle_webhook('feishu', headers, body)


def handle_dingtalk_webhook(headers: Dict[str, str], body: bytes) -> WebhookResponse:
    """处理钉钉 Webhook"""
    return handle_webhook('dingtalk', headers, body)


def handle_wecom_webhook(headers: Dict[str, str], body: bytes) -> WebhookResponse:
    """处理企业微信 Webhook"""
    return handle_webhook('wecom', headers, body)


def handle_telegram_webhook(headers: Dict[str, str], body: bytes) -> WebhookResponse:
    """处理 Telegram Webhook"""
    return handle_webhook('telegram', headers, body)


def handle_discord_interaction(headers: Dict[str, str], body: bytes) -> WebhookResponse:
    """
    处理 Discord Interactions Endpoint 请求

    Discord 要求 3 秒内响应，而 /chat、/ask 等命令可能耗时较长，
    因此命令消息先返回延迟响应（type 5），在后台线程中执行命令后
    通过 Interaction Webhook 回填结果。

    Args:
        headers: HTTP 请求头
        body: 请求体原始字节

    Returns:
        WebhookResponse 响应对象
    """
    # 每次请求重新创建适配器，使 WebUI 中修改的 DISCORD_PUBLIC_KEY 立即生效
    from bot.platforms.discord import DiscordPlatform
    platform = DiscordPlatform()

    try:
        data = json.loads(body.decode('utf-8')) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(f"[BotHandler] Discord JSON 解析失败: {e}")
        return WebhookResponse.error("Invalid JSON", 400)

    # 签名验证和 PING 必须在启用开关之前处理，否则 Discord 无法完成端点校验
    message, early_response = platform.handle_webhook(headers, body, data)
    if early_response:
        return early_response

    if not message:
        logger.debug("[BotHandler] Discord 无需处理的交互")
        return WebhookResponse.success()

    from src.config import get_config
    if not getattr(get_config(), 'bot_enabled', True):
        logger.info("[BotHandler] 机器人功能未启用")
        return platform.format_response(
            BotResponse.text_response("机器人功能未启用"), message
        )

    logger.info(f"[BotHandler] 解析到 Discord 命令: user={message.user_name}, content={message.content[:50]}")

    def _run_and_followup() -> None:
        try:
            response = get_dispatcher().dispatch(message)
        except Exception as e:  # dispatcher 内部已兜底，此处防御线程静默退出
            logger.exception(f"[BotHandler] Discord 命令执行异常: {e}")
            response = BotResponse.error_response("命令执行失败")
        platform.send_followup(message, response)

    threading.Thread(
        target=_run_and_followup,
        name="discord-interaction",
        daemon=True,
    ).start()

    return platform.deferred_response()
