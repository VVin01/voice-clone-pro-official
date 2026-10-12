from __future__ import annotations

import logging
from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
)

logger = logging.getLogger(__name__)

DEFAULT_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="Start Voice Studio"),
]


def get_default_bot_commands() -> list[BotCommand]:
    """Return the canonical bot commands for the user-facing native commands menu.

    In alignment with Phase D8 Stage 2A, ordinary users see only /start in the
    native Commands Menu key beside the message input field. Other features
    (Voice Studio, Settings, Admin Dashboard) are accessed via inline menus.
    """
    return [BotCommand(command=c.command, description=c.description) for c in DEFAULT_COMMANDS]


async def setup_bot_commands(bot: Bot) -> None:
    """Register native Telegram bot commands for the default scope.

    - BotCommandScopeDefault receives only /start.
    - No admin or auxiliary commands are exposed in the native menu.
    - Raises RuntimeError if command registration fails.
    """
    default_cmds = get_default_bot_commands()
    try:
        await bot.set_my_commands(commands=default_cmds, scope=BotCommandScopeDefault())
        logger.info(
            "Registered %d default bot command(s) in BotCommandScopeDefault: %s",
            len(default_cmds),
            [c.command for c in default_cmds],
        )
    except Exception as e:
        logger.error("Failed to register default bot commands: %s", e)
        raise RuntimeError(f"Failed to register default bot commands: {e}") from e
