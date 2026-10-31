import logging
from typing import Any
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ErrorEvent

from app.db.session import AsyncSessionLocal
from app.handlers.admin import admin_router
from app.handlers.clone import clone_router
from app.handlers.custom_voice import custom_voice_router
from app.handlers.menu import menu_router
from app.handlers.payment import payment_router
from app.handlers.start import start_router
from app.middlewares import setup_middlewares
from app.services.broadcast import mark_user_blocked

logger = logging.getLogger(__name__)


def setup_handlers(dp: Dispatcher, session_factory: Any = None) -> None:
    setup_middlewares(dp, session_factory=session_factory)

    @dp.error()
    async def global_error_handler(event: ErrorEvent) -> bool:
        exc = event.exception

        if isinstance(exc, TelegramForbiddenError):
            user_id = None
            if event.update:
                if event.update.message and event.update.message.from_user:
                    user_id = event.update.message.from_user.id
                elif event.update.callback_query and event.update.callback_query.from_user:
                    user_id = event.update.callback_query.from_user.id
                elif event.update.my_chat_member and event.update.my_chat_member.from_user:
                    user_id = event.update.my_chat_member.from_user.id
                elif getattr(event.update, "inline_query", None) and event.update.inline_query.from_user:
                    user_id = event.update.inline_query.from_user.id

            logger.info("Suppressed TelegramForbiddenError (bot blocked by user %s): %s", user_id, exc)
            if user_id:
                session_maker = session_factory or AsyncSessionLocal
                try:
                    async with session_maker() as session:
                        await mark_user_blocked(session, user_id)
                except Exception as mark_err:
                    logger.debug("Failed to flag user %s as bot_blocked: %s", user_id, mark_err)
            return True

        if isinstance(exc, TelegramBadRequest):
            err_text = str(exc).lower()
            if any(
                term in err_text
                for term in (
                    "query is too old",
                    "query id is invalid",
                    "message is not modified",
                    "message to edit not found",
                    "message to delete not found",
                    "message can't be deleted",
                    "message can't be edited",
                    "chat not found",
                    "bot was blocked by the user",
                )
            ):
                logger.debug("Suppressed harmless TelegramBadRequest: %s", exc)
                return True

        if isinstance(exc, AttributeError) and "InaccessibleMessage" in str(exc):
            logger.warning("Suppressed InaccessibleMessage attribute error in global_error_handler: %s", exc)
            if event.update and event.update.callback_query:
                try:
                    await event.update.callback_query.answer()
                except Exception:
                    pass
            return True

        logger.error("Unhandled exception in event: %s", exc, exc_info=exc)
        return False

    for router in (start_router, menu_router, clone_router, payment_router, custom_voice_router, admin_router):
        if router.parent_router is not dp:
            router._parent_router = None
            dp.include_router(router)
