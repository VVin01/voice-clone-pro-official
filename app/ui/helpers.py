from __future__ import annotations

import logging
from typing import Any
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

logger = logging.getLogger(__name__)


async def safe_edit_message(
    callback: CallbackQuery,
    text: str,
    reply_markup: Any = None,
    parse_mode: str | None = "HTML",
    **kwargs: Any,
) -> Message | None:
    """
    Safely edits the message associated with a CallbackQuery.

    If callback.message is an InaccessibleMessage, None, or if edit_text fails
    (e.g., message was deleted or cannot be edited), it gracefully falls back
    to sending a new message to the user's chat.

    Also safely handles 'message is not modified' TelegramBadRequest.
    """
    msg = callback.message
    # Check if message is a valid Message instance with edit capability
    if isinstance(msg, Message):
        is_media = bool(
            getattr(msg, "photo", None)
            or getattr(msg, "document", None)
            or getattr(msg, "video", None)
            or getattr(msg, "animation", None)
            or getattr(msg, "audio", None)
            or getattr(msg, "voice", None)
            or getattr(msg, "caption", None)
        )
        if is_media and hasattr(msg, "edit_caption"):
            try:
                # Telegram caption limit is 1024 characters
                safe_caption = text if len(text) <= 1024 else text[:1020] + "..."
                return await msg.edit_caption(
                    caption=safe_caption,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    **kwargs,
                )
            except TelegramBadRequest as exc:
                err_str = str(exc).lower()
                if "message is not modified" in err_str:
                    logger.debug("safe_edit_message: Caption content unchanged.")
                    return msg
                logger.warning(
                    "safe_edit_message: TelegramBadRequest on edit_caption (%s), trying edit_text/fallback",
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "safe_edit_message: Exception on edit_caption (%s), trying edit_text/fallback",
                    exc,
                )

        if hasattr(msg, "edit_text"):
            try:
                return await msg.edit_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    **kwargs,
                )
            except TelegramBadRequest as exc:
                err_str = str(exc).lower()
                if "message is not modified" in err_str:
                    logger.debug("safe_edit_message: Message content unchanged.")
                    return msg
                logger.warning(
                    "safe_edit_message: TelegramBadRequest on edit_text (%s), falling back to send_message",
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "safe_edit_message: Exception on edit_text (%s), falling back to send_message",
                    exc,
                )

    # Fallback: Send a new message if editing is not possible or failed
    target_chat_id = (
        msg.chat.id
        if msg is not None and getattr(msg, "chat", None) is not None
        else callback.from_user.id
    )
    try:
        return await callback.bot.send_message(
            chat_id=target_chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            **kwargs,
        )
    except TelegramForbiddenError as exc:
        logger.info(
            "safe_edit_message: Bot was blocked by user %s: %s",
            target_chat_id,
            exc,
        )
        return None
    except Exception as exc:
        logger.error(
            "safe_edit_message: Failed to fallback send_message to %s: %s",
            target_chat_id,
            exc,
        )
        return None


async def safe_answer_callback(
    callback: CallbackQuery,
    text: str | None = None,
    show_alert: bool = False,
    **kwargs: Any,
) -> bool:
    """Safely answer callback query without raising exceptions on expired queries."""
    try:
        await callback.answer(text=text, show_alert=show_alert, **kwargs)
        return True
    except Exception as exc:
        logger.debug("safe_answer_callback: Suppressed callback answer error: %s", exc)
        return False
