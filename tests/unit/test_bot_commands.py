from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

from app.ui.commands import (
    get_default_bot_commands,
    setup_bot_commands,
)


def test_default_bot_commands_definitions():
    cmds = get_default_bot_commands()
    cmd_names = [c.command for c in cmds]
    # D8 Stage 2A requirement: exactly /start in the default native commands menu
    assert cmd_names == ["start"]
    assert len(cmd_names) == 1
    assert "admin" not in cmd_names
    assert "help" not in cmd_names
    assert "language" not in cmd_names

    start_cmd = cmds[0]
    assert start_cmd.command == "start"
    assert len(start_cmd.description) > 0
    assert len(start_cmd.description) <= 256


@pytest.mark.asyncio
async def test_setup_bot_commands_registers_only_start_in_default_scope():
    mock_bot = MagicMock(spec=Bot)
    mock_bot.set_my_commands = AsyncMock()

    await setup_bot_commands(mock_bot)

    # Exactly 1 call to set_my_commands
    assert mock_bot.set_my_commands.await_count == 1

    call1 = mock_bot.set_my_commands.await_args_list[0]
    call1_cmds = call1.kwargs["commands"]
    call1_scope = call1.kwargs["scope"]

    # Scope must be BotCommandScopeDefault
    assert isinstance(call1_scope, BotCommandScopeDefault)
    assert not isinstance(call1_scope, BotCommandScopeChat)

    # Registered command must be only /start
    assert [c.command for c in call1_cmds] == ["start"]
    assert "admin" not in [c.command for c in call1_cmds]


@pytest.mark.asyncio
async def test_setup_bot_commands_failure_raises():
    mock_bot = MagicMock(spec=Bot)
    mock_bot.set_my_commands = AsyncMock(side_effect=Exception("Telegram connection refused"))

    with pytest.raises(RuntimeError, match="Failed to register default bot commands"):
        await setup_bot_commands(mock_bot)


@pytest.mark.asyncio
async def test_startup_ordering_commands_before_polling():
    from main import run_bot_and_worker

    call_order = []

    async def mock_setup_commands(bot):
        call_order.append("setup_bot_commands")

    async def mock_start_polling(bot):
        call_order.append("start_polling")

    with patch("main.init_db", AsyncMock()), \
         patch("main.settings") as mock_settings, \
         patch("main.setup_bot_commands", side_effect=mock_setup_commands), \
         patch("aiogram.Dispatcher.start_polling", side_effect=mock_start_polling), \
         patch("main.Bot") as mock_bot_cls:

        mock_settings.bot_token = "fake_test_token_123456"
        mock_settings.log_level = "INFO"
        mock_bot_instance = MagicMock()
        mock_bot_instance.session.close = AsyncMock()
        mock_bot_cls.return_value = mock_bot_instance

        await run_bot_and_worker(mode="bot")

    assert call_order == ["setup_bot_commands", "start_polling"]
