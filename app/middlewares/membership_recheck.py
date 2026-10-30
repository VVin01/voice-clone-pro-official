from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.schema import User
from app.services.gate import (
    build_gate_reminder_text,
    gate_still_missing_text,
    is_gate_configured,
    recheck_verified_membership,
)
from app.ui.keyboards import membership_gate_kb

logger = logging.getLogger(__name__)

# Events that already run the LIVE gate themselves (verify_user_membership)
# and must stay reachable for a user who has just been sent the reminder:
# /start, the first-run language pick and the "I've joined — check again"
# button.
_EXEMPT_CALLBACK_PREFIXES = ("set_lang:",)
_EXEMPT_CALLBACK_DATA = {"gate:verify"}


def _is_exempt(event: TelegramObject) -> bool:
    if isinstance(event, CallbackQuery):
        data = event.data or ""
        return data in _EXEMPT_CALLBACK_DATA or data.startswith(_EXEMPT_CALLBACK_PREFIXES)
    if isinstance(event, Message):
        text = (event.text or "").strip()
        if text.startswith("/start"):
            head = text.split(maxsplit=1)[0]
            return head == "/start" or head.startswith("/start@")
    return False


def _is_private(event: TelegramObject) -> bool:
    """Only private bot chats are gated (never the required group itself)."""
    if isinstance(event, Message):
        chat = getattr(event, "chat", None)
    elif isinstance(event, CallbackQuery):
        msg = getattr(event, "message", None)
        chat = getattr(msg, "chat", None) if msg is not None else None
        if chat is None:
            return True  # inline / inaccessible message: still the user's own tap
    else:
        return False
    chat_type = getattr(chat, "type", None)
    return chat_type is None or chat_type == "private"


class MembershipRecheckMiddleware(BaseMiddleware):
    """Periodic membership re-check for already-verified users.

    Closes the join-then-leave hole: channel_verified / group_verified are
    set once by the live gate, and before this middleware nothing in the
    bot re-checked them after /start. Registered as an outer middleware on
    messages and callback queries, AFTER AccessControlMiddleware.

    For a non-admin user in a private chat whose legacy flags say verified
    (and only when the gate is configured), it calls
    recheck_verified_membership(), which re-probes Telegram at most once
    per MEMBERSHIP_RECHECK_TTL_SECONDS. When the probe definitively says
    the user left either chat, the flags are reset and the event is
    replaced by the shared join reminder (both join buttons + gate:verify
    re-check). A Telegram API error never locks out a verified user here
    (fail safe); the live gate paths stay fail closed.

    Users whose flags are not set are left to the existing live gate at
    /start, the generate / custom-voice entry points and the Mini App API.
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
        if from_user is None or settings.is_admin(from_user.id):
            return await handler(event, data)
        if not is_gate_configured() or _is_exempt(event) or not _is_private(event):
            return await handler(event, data)

        try:
            async with self.session_factory() as session:
                user = await session.get(User, from_user.id)
                if user is None or not (user.channel_verified and user.group_verified):
                    allowed = True
                else:
                    bot = data.get("bot") or getattr(event, "bot", None)
                    allowed = await recheck_verified_membership(session, user, bot)
                    lang = user.language_code or "my"
                    ch_ok, gp_ok = bool(user.channel_verified), bool(user.group_verified)
                    if not allowed:
                        try:
                            await session.commit()
                        except Exception as e:
                            # Telegram definitively said "left": still block
                            # this event even if persisting the reset failed.
                            logger.error("MembershipRecheck: could not persist flag reset for %s: %s", from_user.id, e)
        except Exception as e:
            # Fail safe on unexpected errors in this cache path only: the
            # user was verified, and the live gate points still enforce.
            logger.error("MembershipRecheck: error for user %s: %s", from_user.id, e, exc_info=True)
            allowed = True

        if allowed:
            return await handler(event, data)

        await self._show_reminder(event, data, lang, ch_ok, gp_ok)
        return None

    @staticmethod
    async def _show_reminder(
        event: TelegramObject,
        data: Dict[str, Any],
        lang: str,
        ch_ok: bool,
        gp_ok: bool,
    ) -> None:
        state = data.get("state")
        if state is not None:
            try:
                await state.clear()
            except Exception as e:
                logger.debug("MembershipRecheck: could not clear FSM state: %s", e)

        text = build_gate_reminder_text(lang, ch_ok, gp_ok)
        kb = membership_gate_kb(
            channel_link=settings.channel_invite_link,
            group_link=settings.group_invite_link,
            lang=lang,
        )
        try:
            if isinstance(event, Message):
                await event.answer(text, reply_markup=kb, parse_mode="HTML")
            elif isinstance(event, CallbackQuery):
                # A NEW message rather than an in-place edit: the tapped
                # message may be a delivered output whose caption must not
                # be overwritten.
                bot = data.get("bot") or getattr(event, "bot", None)
                if bot is not None:
                    await bot.send_message(
                        chat_id=event.from_user.id,
                        text=text,
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                try:
                    await event.answer(gate_still_missing_text(lang, ch_ok, gp_ok), show_alert=True)
                except Exception:
                    pass
        except Exception as e:
            logger.info("MembershipRecheck: could not deliver join reminder to user %s: %s", event.from_user.id, e)
