# -*- coding: utf-8 -*-
"""Discord Interactions Endpoint（Slash 命令）适配器测试。"""

import json
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

# Keep this test runnable when optional LLM runtime deps are not installed.
for optional_module in ("litellm", "json_repair"):
    try:
        __import__(optional_module)
    except ModuleNotFoundError:
        sys.modules[optional_module] = mock.MagicMock()

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import bot.handler as handler
from bot.models import BotResponse, ChatType
from bot.platforms import ALL_PLATFORMS
from bot.platforms.discord import DiscordPlatform


TIMESTAMP = "1700000000"


@pytest.fixture
def keypair():
    private_key = Ed25519PrivateKey.generate()
    public_hex = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return private_key, public_hex


def _signed(private_key, payload, timestamp=TIMESTAMP):
    body = json.dumps(payload).encode("utf-8")
    signature = private_key.sign(timestamp.encode("utf-8") + body).hex()
    headers = {"x-signature-ed25519": signature, "x-signature-timestamp": timestamp}
    return headers, body


def _command(name="analyze", options=None, guild=True):
    payload = {
        "id": "111",
        "application_id": "222",
        "token": "interaction-token",
        "type": 2,
        "channel_id": "333",
        "data": {"name": name, "options": options or []},
    }
    user = {"id": "444", "username": "alice", "global_name": "Alice"}
    if guild:
        payload["guild_id"] = "555"
        payload["member"] = {"user": user}
    else:
        payload["user"] = user
    return payload


class _InlineThread:
    """Run thread targets synchronously so follow-ups are observable in tests."""

    def __init__(self, target=None, **_kwargs):
        self._target = target

    def start(self):
        self._target()


def test_discord_is_registered():
    assert ALL_PLATFORMS["discord"] is DiscordPlatform


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def test_verify_accepts_valid_signature(keypair):
    private_key, public_hex = keypair
    headers, body = _signed(private_key, {"type": 1})
    assert DiscordPlatform(public_hex).verify_request(headers, body) is True


def test_verify_header_lookup_is_case_insensitive(keypair):
    private_key, public_hex = keypair
    headers, body = _signed(private_key, {"type": 1})
    headers = {"X-Signature-Ed25519": headers["x-signature-ed25519"],
               "X-Signature-Timestamp": headers["x-signature-timestamp"]}
    assert DiscordPlatform(public_hex).verify_request(headers, body) is True


def test_verify_rejects_tampered_body(keypair):
    private_key, public_hex = keypair
    headers, _ = _signed(private_key, {"type": 1})
    assert DiscordPlatform(public_hex).verify_request(headers, b'{"type": 2}') is False


def test_verify_rejects_tampered_timestamp(keypair):
    private_key, public_hex = keypair
    headers, body = _signed(private_key, {"type": 1})
    headers["x-signature-timestamp"] = "1700000001"
    assert DiscordPlatform(public_hex).verify_request(headers, body) is False


def test_verify_rejects_signature_from_other_key(keypair):
    _, public_hex = keypair
    headers, body = _signed(Ed25519PrivateKey.generate(), {"type": 1})
    assert DiscordPlatform(public_hex).verify_request(headers, body) is False


@pytest.mark.parametrize("headers", [
    {},
    {"x-signature-timestamp": TIMESTAMP},
    {"x-signature-ed25519": "ab" * 64},
    {"x-signature-ed25519": "not-hex", "x-signature-timestamp": TIMESTAMP},
])
def test_verify_rejects_missing_or_malformed_headers(keypair, headers):
    _, public_hex = keypair
    assert DiscordPlatform(public_hex).verify_request(headers, b"{}") is False


@pytest.mark.parametrize("public_key", ["", "zz" * 32, "abcd"])
def test_verify_fails_closed_without_valid_public_key(keypair, public_key):
    private_key, _ = keypair
    headers, body = _signed(private_key, {"type": 1})
    assert DiscordPlatform(public_key).verify_request(headers, body) is False


# ---------------------------------------------------------------------------
# PING handling
# ---------------------------------------------------------------------------

def test_unsigned_ping_is_rejected_with_401(keypair):
    _, public_hex = keypair
    message, response = DiscordPlatform(public_hex).handle_webhook({}, b'{"type": 1}', {"type": 1})
    assert message is None
    assert response.status_code == 401


def test_signed_ping_returns_pong(keypair):
    private_key, public_hex = keypair
    headers, body = _signed(private_key, {"type": 1})
    message, response = DiscordPlatform(public_hex).handle_webhook(headers, body, {"type": 1})
    assert message is None
    assert response.status_code == 200
    assert response.body == {"type": 1}


# ---------------------------------------------------------------------------
# Slash command parsing
# ---------------------------------------------------------------------------

def test_parse_guild_command_with_options(keypair):
    _, public_hex = keypair
    payload = _command(options=[
        {"name": "stock_code", "type": 3, "value": "600519"},
        {"name": "full_report", "type": 5, "value": True},
    ])
    message = DiscordPlatform(public_hex).parse_message(payload)

    assert message.content == "/analyze 600519 full"
    assert message.get_command_and_args() == ("analyze", ["600519", "full"])
    assert message.user_id == "444"
    assert message.user_name == "Alice"
    assert message.chat_id == "333"
    assert message.chat_type == ChatType.GROUP
    assert message.raw_data["application_id"] == "222"
    assert message.raw_data["interaction_token"] == "interaction-token"


def test_parse_false_boolean_option_is_dropped(keypair):
    _, public_hex = keypair
    payload = _command(options=[
        {"name": "stock_code", "type": 3, "value": "AAPL"},
        {"name": "full_report", "type": 5, "value": False},
    ])
    assert DiscordPlatform(public_hex).parse_message(payload).content == "/analyze AAPL"


def test_parse_direct_message_and_subcommands(keypair):
    _, public_hex = keypair
    payload = _command(name="ask", guild=False, options=[
        {"name": "stock", "type": 1, "options": [{"name": "code", "type": 3, "value": "600519"}]},
    ])
    message = DiscordPlatform(public_hex).parse_message(payload)
    assert message.content == "/ask stock 600519"
    assert message.chat_type == ChatType.PRIVATE
    assert message.user_id == "444"


@pytest.mark.parametrize("payload", [
    {"type": 3, "data": {"name": "analyze"}},  # component interaction
    {"type": 2, "data": {}},                    # command without name
])
def test_parse_ignores_unsupported_interactions(keypair, payload):
    _, public_hex = keypair
    assert DiscordPlatform(public_hex).parse_message(payload) is None


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

def test_format_response_truncates_and_blocks_mentions(keypair):
    _, public_hex = keypair
    platform = DiscordPlatform(public_hex)
    message = platform.parse_message(_command())
    response = platform.format_response(BotResponse.text_response("x" * 2500), message)

    assert response.body["type"] == 4
    assert len(response.body["data"]["content"]) == 2000
    assert response.body["data"]["allowed_mentions"] == {"parse": []}


def test_send_followup_patches_original_message(keypair):
    _, public_hex = keypair
    platform = DiscordPlatform(public_hex)
    message = platform.parse_message(_command())

    with mock.patch("bot.platforms.discord.requests.patch") as patch:
        patch.return_value = SimpleNamespace(status_code=200, text="")
        assert platform.send_followup(message, BotResponse.text_response("done")) is True

    url = patch.call_args.args[0]
    assert url == "https://discord.com/api/v10/webhooks/222/interaction-token/messages/@original"
    assert patch.call_args.kwargs["json"]["content"] == "done"


def test_send_followup_reports_http_failure(keypair):
    _, public_hex = keypair
    platform = DiscordPlatform(public_hex)
    message = platform.parse_message(_command())

    with mock.patch("bot.platforms.discord.requests.patch") as patch:
        patch.return_value = SimpleNamespace(status_code=404, text="Unknown Webhook")
        assert platform.send_followup(message, BotResponse.text_response("done")) is False


# ---------------------------------------------------------------------------
# End-to-end handler
# ---------------------------------------------------------------------------

def _config(public_hex, bot_enabled=True):
    return SimpleNamespace(discord_public_key=public_hex, bot_enabled=bot_enabled)


def test_handler_defers_then_sends_dispatcher_result(keypair):
    private_key, public_hex = keypair
    payload = _command(options=[{"name": "stock_code", "type": 3, "value": "600519"}])
    headers, body = _signed(private_key, payload)
    dispatcher = mock.Mock()
    dispatcher.dispatch.return_value = BotResponse.text_response("submitted")

    with mock.patch("src.config.get_config", return_value=_config(public_hex)), \
            mock.patch.object(handler, "get_dispatcher", return_value=dispatcher), \
            mock.patch.object(handler.threading, "Thread", _InlineThread), \
            mock.patch("bot.platforms.discord.requests.patch") as patch:
        patch.return_value = SimpleNamespace(status_code=200, text="")
        response = handler.handle_discord_interaction(headers, body)

    assert response.status_code == 200
    assert response.body == {"type": 5}
    dispatched = dispatcher.dispatch.call_args.args[0]
    assert dispatched.get_command_and_args() == ("analyze", ["600519"])
    assert patch.call_args.kwargs["json"]["content"] == "submitted"


def test_handler_rejects_bad_signature_before_dispatch(keypair):
    private_key, public_hex = keypair
    headers, _ = _signed(private_key, _command())
    tampered = json.dumps(_command(name="batch")).encode("utf-8")
    dispatcher = mock.Mock()

    with mock.patch("src.config.get_config", return_value=_config(public_hex)), \
            mock.patch.object(handler, "get_dispatcher", return_value=dispatcher):
        response = handler.handle_discord_interaction(headers, tampered)

    assert response.status_code == 401
    dispatcher.dispatch.assert_not_called()


def test_handler_answers_ping_even_when_bot_disabled(keypair):
    private_key, public_hex = keypair
    headers, body = _signed(private_key, {"type": 1})

    with mock.patch("src.config.get_config", return_value=_config(public_hex, bot_enabled=False)):
        response = handler.handle_discord_interaction(headers, body)

    assert response.body == {"type": 1}


def test_handler_replies_immediately_when_bot_disabled(keypair):
    private_key, public_hex = keypair
    headers, body = _signed(private_key, _command())
    dispatcher = mock.Mock()

    with mock.patch("src.config.get_config", return_value=_config(public_hex, bot_enabled=False)), \
            mock.patch.object(handler, "get_dispatcher", return_value=dispatcher):
        response = handler.handle_discord_interaction(headers, body)

    assert response.body["type"] == 4
    dispatcher.dispatch.assert_not_called()


def test_handler_rejects_invalid_json(keypair):
    _, public_hex = keypair
    with mock.patch("src.config.get_config", return_value=_config(public_hex)):
        response = handler.handle_discord_interaction({}, b"not json")
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# HTTP route
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from api.app import create_app
    import src.auth as auth

    auth._auth_enabled = False
    return TestClient(create_app())


def test_route_returns_404_when_not_configured(client):
    with mock.patch("src.config.get_config", return_value=_config(None)):
        response = client.post("/bot/discord", content=b'{"type": 1}')
    assert response.status_code == 404


def test_route_answers_signed_ping(client, keypair):
    private_key, public_hex = keypair
    headers, body = _signed(private_key, {"type": 1})
    with mock.patch("src.config.get_config", return_value=_config(public_hex)):
        response = client.post("/bot/discord", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"type": 1}


def test_route_rejects_unsigned_ping(client, keypair):
    _, public_hex = keypair
    with mock.patch("src.config.get_config", return_value=_config(public_hex)):
        response = client.post("/bot/discord", content=b'{"type": 1}')
    assert response.status_code == 401
