from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.handlers.menu import _safe_delete_message, _send_dm, render_main_menu
from app.models.enums import UserStatus
from app.services.broadcast import mark_user_blocked
from app.services.gate import (
    build_gate_reminder_text,
    gate_still_missing_text,
    verify_user_membership,
)
from app.services.i18n import t
from app.services.ledger import get_or_create_user
from app.ui.helpers import safe_edit_message
from app.ui.keyboards import language_selection_kb, membership_gate_kb

logger = logging.getLogger(__name__)
start_router = Router(name="start")


def _is_banned_user(user: Any, telegram_id: int) -> bool:
    """Mirror AccessControlMiddleware: status=BLOCKED is banned (admins bypass)."""
    return (
        getattr(user, "status", None) == UserStatus.BLOCKED.value
        and not settings.is_admin(telegram_id)
    )


def _needs_language_pick(user: Any, is_new: bool) -> bool:
    """First-run users have no saved language on their user record."""
    if is_new:
        return True
    saved = (getattr(user, "language_code", None) or "").strip()
    return saved not in ("my", "en")


@start_router.message(CommandStart(), F.chat.type == "private")
async def handle_start(
    message: Message,
    command: CommandObject | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Round 13 Part K /start flow:
    1. Attribute ?start=<code> referral first.
    2. First-run user (no saved language) -> language picker (set_lang:).
    3. Returning user -> live membership probe:
       - still a member -> Welcome + 3 inline buttons straight away (no gate).
       - left channel/group -> join gate (both join links + verify button).
    """
    telegram_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    ref_arg = command.args if command and command.args else None

    async def _run(s: AsyncSession) -> None:
        user, is_new = await get_or_create_user(
            s,
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            referral_code_arg=ref_arg,
        )
        if is_new:
            # Mark language as not yet chosen by the user (first-run picker will persist it).
            user.language_code = ""
        await s.commit()

        if _is_banned_user(user, telegram_id):
            banned_lang = user.language_code or "my"
            try:
                await message.answer(t("user_account_suspended", banned_lang))
            except TelegramForbiddenError:
                logger.info("handle_start: Bot blocked by banned user %s", telegram_id)
            return

        if settings.maintenance_mode and not settings.is_admin(telegram_id):
            maint_lang = user.language_code or "my"
            try:
                await message.answer(
                    f"{t('maintenance_title', maint_lang)}\n\n{t('maintenance_desc', maint_lang)}",
                    parse_mode="HTML",
                )
            except TelegramForbiddenError:
                logger.info("handle_start: Bot blocked by user %s during maintenance", telegram_id)
            return

        if _needs_language_pick(user, is_new):
            try:
                await message.answer(
                    t("welcome_choose_lang", "my"),
                    reply_markup=language_selection_kb(callback_prefix="set_lang"),
                )
            except TelegramForbiddenError:
                logger.info("handle_start: New user %s blocked the bot immediately", telegram_id)
                await mark_user_blocked(s, telegram_id)
            return

        lang = user.language_code or "my"

        # Probe live membership at /start: returning members go straight to Welcome;
        # ex-members (or unverified users) see the join gate first.
        is_verified, _ = await verify_user_membership(s, user, message.bot)
        if not is_verified:
            await s.commit()
            text = build_gate_reminder_text(lang, user.channel_verified, user.group_verified)
            kb = membership_gate_kb(
                channel_link=settings.channel_invite_link,
                group_link=settings.group_invite_link,
                lang=lang,
            )
            try:
                await message.answer(text, reply_markup=kb, parse_mode="HTML")
            except TelegramForbiddenError:
                logger.info("handle_start: User %s blocked the bot during membership gate", telegram_id)
                await mark_user_blocked(s, telegram_id)
            return

        text, kb = await render_main_menu(telegram_id, lang, session=s)
        await s.commit()
        try:
            await message.answer(text, reply_markup=kb, parse_mode="HTML")
        except TelegramForbiddenError:
            logger.info("handle_start: User %s blocked the bot during welcome render", telegram_id)
            await mark_user_blocked(s, telegram_id)

    if session is not None:
        await _run(session)
    else:
        async with AsyncSessionLocal() as s:
            await _run(s)


@start_router.callback_query(F.data.startswith("set_lang:"))
async def handle_set_language(
    callback: CallbackQuery,
    session: AsyncSession | None = None,
) -> None:
    """First-run language picker completion: save language, delete picker, then gate -> welcome."""
    lang_code = callback.data.split(":", 1)[1] if ":" in (callback.data or "") else "my"
    if lang_code not in ("my", "en"):
        lang_code = "my"

    async def _run(s: AsyncSession) -> tuple[str, Any]:
        user, _ = await get_or_create_user(s, callback.from_user.id)

        if _is_banned_user(user, callback.from_user.id):
            banned_lang = user.language_code or lang_code
            await s.commit()
            return t("user_account_suspended", banned_lang), None

        user.language_code = lang_code
        is_verified, _ = await verify_user_membership(s, user, callback.bot)

        if not is_verified:
            await s.commit()
            text = build_gate_reminder_text(lang_code, user.channel_verified, user.group_verified)
            kb = membership_gate_kb(
                channel_link=settings.channel_invite_link,
                group_link=settings.group_invite_link,
                lang=lang_code,
            )
            return text, kb

        text, kb = await render_main_menu(callback.from_user.id, lang_code, session=s)
        await s.commit()
        return text, kb

    if session is not None:
        next_text, next_kb = await _run(session)
    else:
        async with AsyncSessionLocal() as s:
            next_text, next_kb = await _run(s)

    try:
        await callback.answer()
    except Exception:
        pass
    await _safe_delete_message(callback.message)
    await _send_dm(callback, next_text, reply_markup=next_kb, parse_mode="HTML")


@start_router.callback_query(F.data == "gate:verify")
async def handle_gate_verify(
    callback: CallbackQuery,
    session: AsyncSession | None = None,
) -> None:
    """Membership gate verification: on pass, delete gate message and send Welcome."""
    async def _run(s: AsyncSession) -> tuple[bool, str, str, Any]:
        user, _ = await get_or_create_user(s, callback.from_user.id)
        lang = user.language_code or "my"

        if _is_banned_user(user, callback.from_user.id):
            await s.commit()
            try:
                await callback.answer(t("user_account_suspended", lang), show_alert=True)
            except Exception:
                pass
            return False, lang, "", None

        is_verified, _ = await verify_user_membership(s, user, callback.bot)

        if not is_verified:
            await s.commit()
            reminder = build_gate_reminder_text(lang, user.channel_verified, user.group_verified)
            kb = membership_gate_kb(
                channel_link=settings.channel_invite_link,
                group_link=settings.group_invite_link,
                lang=lang,
            )
            await safe_edit_message(callback, reminder, reply_markup=kb, parse_mode="HTML")
            try:
                await callback.answer(
                    gate_still_missing_text(lang, user.channel_verified, user.group_verified),
                    show_alert=True,
                )
            except Exception:
                pass
            return False, lang, "", None

        text, kb = await render_main_menu(callback.from_user.id, lang, session=s)
        await s.commit()
        return True, lang, text, kb

    if session is not None:
        passed, lang, welcome_text, welcome_kb = await _run(session)
    else:
        async with AsyncSessionLocal() as s:
            passed, lang, welcome_text, welcome_kb = await _run(s)

    if not passed:
        return

    try:
        await callback.answer(t("membership_verified", lang))
    except Exception:
        pass
    await _safe_delete_message(callback.message)
    await _send_dm(callback, welcome_text, reply_markup=welcome_kb, parse_mode="HTML")
