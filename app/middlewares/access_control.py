from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.enums import UserStatus
from app.models.schema import User
from app.services.i18n import t

logger = logging.getLogger(__name__)


class AccessControlMiddleware(BaseMiddleware):
    """
    Inbound access-control outer middleware.

    Intercepts Message and CallbackQuery events before filters, commands, and FSM states.
    - If user is an admin: bypasses immediately with zero DB lookup.
    - If user is not yet in the database: allows through (for initial registration / /start).
    - If user has status BLOCKED:
        * Message: responds with account suspension notice.
        * CallbackQuery: answers callback query with account suspension notice as an alert.
        * Drops the event immediately (does not invoke downstream handler).
    - If an unexpected database error occurs: fails closed gracefully for non-admins with
      a generic error notice without crashing the dispatcher.
    """

    def __init__(self, session_factory: Any = None) -> None:
        super().__init__()
        self.session_factory = session_factory or AsyncSessionLocal

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        if from_user is None:
            # Events without an originating Telegram user pass through
            return await handler(event, data)

        user_id = from_user.id

        # Admin bypass: immediate pass-through, zero DB query, zero FSM interference
        if settings.is_admin(user_id):
            return await handler(event, data)

        # Database status check for non-admin users
        try:
            async with self.session_factory() as session:
                user = await session.get(User, user_id)
        except Exception as e:
            logger.error(
                "AccessControl: Database error while checking user %s: %s",
                user_id,
                e,
                exc_info=True,
            )
            # Fail closed gracefully for non-admins
            try:
                if isinstance(event, Message):
                    await event.answer("⚠️ Service temporarily unavailable. Please try again later.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("⚠️ Service temporarily unavailable. Please try again later.", show_alert=True)
            except Exception as notify_err:
                logger.debug(
                    "AccessControl: Failed to send DB error notice to user %s: %s",
                    user_id,
                    notify_err,
                )
            return None

        # If user is not in DB yet, allow through (e.g. /start registration)
        if user is None:
            return await handler(event, data)

        # Check if user status is BLOCKED
        if user.status == UserStatus.BLOCKED.value:
            lang = getattr(user, "language_code", None) or "my"
            notice = t("user_account_suspended", lang)
            logger.info(
                "AccessControl: Blocked user %s attempted interaction (event=%s)",
                user_id,
                type(event).__name__,
            )
            try:
                if isinstance(event, Message):
                    await event.answer(notice)
                elif isinstance(event, CallbackQuery):
                    await event.answer(notice, show_alert=True)
            except Exception as send_err:
                logger.debug(
                    "AccessControl: Failed to send suspension notice to user %s: %s",
                    user_id,
                    send_err,
                )
            return None

        return await handler(event, data)
