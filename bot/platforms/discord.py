# -*- coding: utf-8 -*-
"""
===================================
Discord 平台适配器
===================================

处理 Discord Interactions Endpoint（Slash 命令）回调：
1. 使用应用公钥（Ed25519）验证请求签名，验证失败返回 401
2. 响应 Discord 的 PING 校验请求
3. 将 Slash 命令（APPLICATION_COMMAND）解析为统一的 BotMessage
4. 先返回延迟响应（type 5），命令执行完成后通过 Interaction Webhook 回填结果

Discord Interactions 文档：
https://discord.com/developers/docs/interactions/overview
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from bot.platforms.base import BotPlatform
from bot.models import BotMessage, BotResponse, ChatType, WebhookResponse


logger = logging.getLogger(__name__)

# Interaction 类型
INTERACTION_TYPE_PING = 1
INTERACTION_TYPE_APPLICATION_COMMAND = 2

# Interaction 响应类型
RESPONSE_TYPE_PONG = 1
RESPONSE_TYPE_CHANNEL_MESSAGE = 4
RESPONSE_TYPE_DEFERRED_CHANNEL_MESSAGE = 5

# 命令选项类型：子命令 / 子命令组（其值在嵌套 options 中）
OPTION_TYPE_SUB_COMMAND = 1
OPTION_TYPE_SUB_COMMAND_GROUP = 2

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_MESSAGE_LIMIT = 2000
FOLLOWUP_TIMEOUT_SECONDS = 10


def _load_public_key(public_key_hex: Optional[str]) -> Optional[Ed25519PublicKey]:
    """将十六进制公钥解析为 Ed25519 公钥对象，无效时返回 None。"""
    if not public_key_hex:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex.strip()))
    except ValueError:
        logger.error("[Discord] DISCORD_PUBLIC_KEY 格式无效（应为 64 位十六进制字符串），所有请求将被拒绝")
        return None


def _get_header(headers: Dict[str, str], name: str) -> str:
    """大小写不敏感地读取请求头。"""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


def _flatten_option_values(options: List[Dict[str, Any]]) -> List[str]:
    """按顺序展开命令选项值；子命令名作为参数保留。"""
    values: List[str] = []
    for option in options or []:
        if option.get("type") in (OPTION_TYPE_SUB_COMMAND, OPTION_TYPE_SUB_COMMAND_GROUP):
            values.append(str(option.get("name", "")))
            values.extend(_flatten_option_values(option.get("options", [])))
        elif "value" in option:
            value = option["value"]
            if isinstance(value, bool):
                # 布尔选项（如 full_report）转为 analyze 命令可识别的参数
                if value:
                    values.append("full")
            else:
                values.append(str(value))
    return [v for v in values if v]


def _truncate(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class DiscordPlatform(BotPlatform):
    """
    Discord 平台适配器（Interactions Endpoint 模式）

    配置要求：
    - DISCORD_PUBLIC_KEY: 应用公钥（Developer Portal > General Information > Public Key）

    未配置或配置无效时，所有请求都会被拒绝（fail-closed）。
    """

    def __init__(self, public_key_hex: Optional[str] = None):
        if public_key_hex is None:
            from src.config import get_config
            public_key_hex = getattr(get_config(), "discord_public_key", None)
        self._public_key = _load_public_key(public_key_hex)

    @property
    def platform_name(self) -> str:
        """平台标识名称"""
        return "discord"

    def verify_request(self, headers: Dict[str, str], body: bytes) -> bool:
        """验证 Discord 请求签名

        Discord 使用 Ed25519 对 ``timestamp + body`` 签名：
        - X-Signature-Ed25519: 十六进制签名
        - X-Signature-Timestamp: 时间戳

        Args:
            headers: HTTP 请求头
            body: 请求体原始字节

        Returns:
            签名是否有效
        """
        if self._public_key is None:
            logger.warning("[Discord] 未配置有效的 DISCORD_PUBLIC_KEY，拒绝请求")
            return False

        signature = _get_header(headers, "X-Signature-Ed25519")
        timestamp = _get_header(headers, "X-Signature-Timestamp")
        if not signature or not timestamp:
            logger.warning("[Discord] 缺少签名请求头")
            return False

        try:
            self._public_key.verify(bytes.fromhex(signature), timestamp.encode("utf-8") + body)
        except (InvalidSignature, ValueError):
            logger.warning("[Discord] 签名验证失败")
            return False
        return True

    def handle_webhook(
        self,
        headers: Dict[str, str],
        body: bytes,
        data: Dict[str, Any],
    ) -> Tuple[Optional[BotMessage], Optional[WebhookResponse]]:
        """处理 Discord Interaction 请求

        与基类不同，Discord 要求包括 PING 在内的所有请求都先通过签名验证，
        验证失败必须返回 401。
        """
        if not self.verify_request(headers, body):
            return None, WebhookResponse.error("Invalid request signature", 401)

        if data.get("type") == INTERACTION_TYPE_PING:
            return None, WebhookResponse.success({"type": RESPONSE_TYPE_PONG})

        return self.parse_message(data), None

    def parse_message(self, data: Dict[str, Any]) -> Optional[BotMessage]:
        """将 Slash 命令 Interaction 解析为统一格式

        Slash 命令 ``/analyze stock_code:600519 full_report:True`` 会被转换为
        ``/analyze 600519 full``，与其他平台的文本命令保持一致。

        Args:
            data: 解析后的 JSON 数据

        Returns:
            BotMessage 对象，或 None（不需要处理）
        """
        if data.get("type") != INTERACTION_TYPE_APPLICATION_COMMAND:
            return None

        command_data = data.get("data") or {}
        command_name = str(command_data.get("name", "")).strip()
        if not command_name:
            return None

        args = _flatten_option_values(command_data.get("options", []))
        content = " ".join(["/" + command_name] + args)

        # 服务器内调用时用户信息在 member.user，私信中在 user
        user = (data.get("member") or {}).get("user") or data.get("user") or {}
        guild_id = data.get("guild_id") or ""

        return BotMessage(
            platform="discord",
            message_id=str(data.get("id", "")),
            user_id=str(user.get("id", "")),
            user_name=user.get("global_name") or user.get("username") or "unknown",
            chat_id=str(data.get("channel_id", "")),
            chat_type=ChatType.GROUP if guild_id else ChatType.PRIVATE,
            content=content,
            raw_content=content,
            mentioned=True,
            raw_data={
                "interaction_id": data.get("id"),
                "application_id": data.get("application_id"),
                "interaction_token": data.get("token"),
                "guild_id": guild_id,
                "channel_id": data.get("channel_id"),
                "command_name": command_name,
                "options": command_data.get("options", []),
            },
        )

    def format_response(self, response: BotResponse, message: BotMessage) -> WebhookResponse:
        """将统一响应转换为 Discord 即时消息响应（type 4）"""
        return WebhookResponse.success({
            "type": RESPONSE_TYPE_CHANNEL_MESSAGE,
            "data": self._message_payload(response.text),
        })

    def deferred_response(self) -> WebhookResponse:
        """返回延迟响应（type 5），Discord 会显示“正在思考”，结果随后回填。"""
        return WebhookResponse.success({"type": RESPONSE_TYPE_DEFERRED_CHANNEL_MESSAGE})

    def send_followup(self, message: BotMessage, response: BotResponse) -> bool:
        """通过 Interaction Webhook 编辑延迟响应的原始消息

        Interaction token 有效期 15 分钟，无需 Bot Token。

        Returns:
            是否发送成功
        """
        raw = message.raw_data or {}
        application_id = raw.get("application_id")
        token = raw.get("interaction_token")
        if not application_id or not token:
            logger.error("[Discord] 缺少 application_id 或 interaction token，无法回填结果")
            return False

        text = response.text or "✅ 已完成"
        url = f"{DISCORD_API_BASE}/webhooks/{application_id}/{token}/messages/@original"
        try:
            resp = requests.patch(url, json=self._message_payload(text), timeout=FOLLOWUP_TIMEOUT_SECONDS)
        except requests.RequestException as e:
            logger.error(f"[Discord] 回填结果失败: {e}")
            return False

        if resp.status_code >= 300:
            logger.error(f"[Discord] 回填结果失败: HTTP {resp.status_code} {resp.text[:200]}")
            return False
        return True

    @staticmethod
    def _message_payload(text: str) -> Dict[str, Any]:
        return {
            "content": _truncate(text),
            # 机器人回复不应触发 @everyone / 角色提醒
            "allowed_mentions": {"parse": []},
        }
