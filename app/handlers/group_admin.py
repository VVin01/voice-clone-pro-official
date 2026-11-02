"""Group AI admin handler — see app/services/group_ai_admin.py.

Registered LAST among the routers so command/FSM handlers always win.
The handler itself is deliberately thin: all decisions (pre-filter,
kill switch, sensitive-topic hard rule, rate limits, the AI call) live
in the service module where they are unit-tested. It never raises —
any failure degrades to silence, never to a broken update pipeline.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.services import group_ai_admin as gai

logger = logging.getLogger(__name__)

group_admin_router = Router(name="group_admin")


async def _kill_switch_on(session: AsyncSession | None) -> bool:
    if session is not None:
        return await gai.is_enabled(session)
    async with AsyncSessionLocal() as s:
        return await gai.is_enabled(s)


async def _escalate(message: Message, bot: Bot | None, *, reason: str, deferral_text: str) -> None:
    """Post the polite deferral line + DM every admin (cooldown-gated)."""
    user = message.from_user
    if user is None or not gai.rate_limiter.can_defer(user.id):
        return
    gai.rate_limiter.record_deferral(user.id)
    try:
        await message.reply(deferral_text)
    except Exception as exc:
        logger.debug("group_ai_admin: deferral post failed: %s", exc)
    dm_text = gai.build_escalation_dm(
        author_name=user.first_name or "",
        author_username=user.username,
        author_id=user.id,
        text=message.text or "",
        chat_id=message.chat.id,
        message_id=message.message_id,
        reason=reason,
    )
    sender = bot or message.bot
    for admin_id in settings.admin_ids:
        try:
            await sender.send_message(admin_id, dm_text, parse_mode="HTML")
        except Exception as exc:
            logger.warning("group_ai_admin: escalation DM to %s failed: %s", admin_id, exc)


@group_admin_router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text,
)
async def handle_group_message(
    message: Message,
    state: FSMContext = None,
    session: AsyncSession | None = None,
    bot: Bot = None,
) -> None:
    try:
        # A user mid-flow in an FSM conversation owns their messages.
        if state is not None:
            try:
                if await state.get_state():
                    return
            except Exception:
                pass

        user = message.from_user
        chat_type = getattr(message.chat.type, "value", str(message.chat.type))
        reason = gai.prefilter_reason(
            chat_id=message.chat.id,
            chat_type=chat_type,
            text=message.text,
            from_user_id=user.id if user else None,
            from_user_is_bot=bool(user.is_bot) if user else True,
            sender_chat_id=message.sender_chat.id if message.sender_chat else None,
        )
        if reason is not None:
            return

        if not await _kill_switch_on(session):
            return

        # Hard rule first: payment/refund/account topics never reach
        # the AI — defer in the group and hand the question to a human.
        if gai.detect_sensitive_topic(message.text):
            await _escalate(
                message,
                bot,
                reason="sensitive",
                deferral_text=gai.DEFERRAL_TEXT_SENSITIVE,
            )
            return

        if not gai.rate_limiter.can_reply(user.id, message.message_id):
            return

        # Social fast paths (owner revision 2026-11-02): a pure
        # greeting or a short "want to try" starter fragment gets a
        # warm deterministic welcome/pointer — no AI call needed, and
        # the tone stays exactly as approved. Join service notices
        # never arrive here (they carry no user text).
        fast_path_text = None
        if gai.is_greeting_only(message.text):
            fast_path_text = gai.WELCOME_TEXT
        elif gai.is_starter_fragment(message.text):
            fast_path_text = gai.STARTER_POINTER_TEXT
        if fast_path_text is not None:
            await message.reply(fast_path_text)
            gai.rate_limiter.record_reply(user.id, message.message_id)
            return

        reply_to = message.reply_to_message
        is_reply_to_bot = bool(
            reply_to
            and reply_to.from_user
            and (reply_to.from_user.username or "").lower() == gai.BOT_USERNAME
        )
        bot_mentioned = f"@{gai.BOT_USERNAME}" in (message.text or "").lower()

        decision, reply_text = await gai.ask_group_admin_ai(
            text=message.text or "",
            author_name=user.first_name or "",
            bot_mentioned=bot_mentioned,
            is_reply_to_bot=is_reply_to_bot,
        )

        if decision == gai.DECISION_ESCALATE:
            await _escalate(
                message,
                bot,
                reason="ai_escalate",
                deferral_text=gai.DEFERRAL_TEXT_UNSURE,
            )
            return
        if decision != gai.DECISION_REPLY or not reply_text:
            return

        # Re-check the cap at send time (another reply may have landed
        # while the AI was thinking), then answer as a threaded reply.
        if not gai.rate_limiter.can_reply(user.id, message.message_id):
            return
        await message.reply(reply_text)
        gai.rate_limiter.record_reply(user.id, message.message_id)
    except Exception as exc:
        logger.warning("group_ai_admin: handler failed (silence): %s", exc)
