"""Regression tests for the Shelson-specific Telegram extensions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from hermes_cli.commands import _SLACK_VIA_HERMES_ONLY, resolve_command
from plugins.platforms.telegram.adapter import (
    TelegramAdapter,
    _mdv2_chunk_balanced,
    _mdv2_rechunk,
)


def test_card_command_remains_registered_without_consuming_a_slack_slot():
    card = resolve_command("card")
    assert card is not None
    assert card.gateway_only is True
    assert "card" in _SLACK_VIA_HERMES_ONLY


def test_markdown_rechunk_keeps_inline_links_balanced():
    formatted = (
        ("First line with enough text to fill the chunk budget. " * 4)
        + "\n"
        "[Complete link](https://example.com/path)\n"
        + ("Final line with enough text to require another chunk. " * 4)
    )
    chunks = _mdv2_rechunk(formatted, 300, len)
    assert chunks is not None
    assert len(chunks) > 1
    assert all(_mdv2_chunk_balanced(chunk) for chunk in chunks)
    assert sum("Complete link" in chunk for chunk in chunks) == 1


@pytest.mark.asyncio
async def test_card_command_uses_normal_telegram_authorization_gate():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._is_user_authorized_from_message = MagicMock(return_value=False)
    message = SimpleNamespace()
    update = SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=12345),
    )
    context = SimpleNamespace(
        user_data={},
        bot=SimpleNamespace(send_message=AsyncMock()),
    )

    await adapter._handle_card_command(update, context)

    adapter._is_user_authorized_from_message.assert_called_once_with(message)
    context.bot.send_message.assert_not_awaited()
