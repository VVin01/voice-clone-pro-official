from __future__ import annotations

import logging
from aiogram import Router, F
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import CallbackQuery, Message

from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.db.session import AsyncSessionLocal
from app.handlers.menu import render_main_menu
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


@start_router.message(CommandStart())
async def handle_start(message: Message, command: CommandObject):
    telegram_id = message.from_user.id
    if settings.maintenance_mode and not settings.is_admin(telegram_id):
        try:
            await message.answer(f"{t('maintenance_title', 'my')}\n\n{t('maintenance_desc', 'my')}", parse_mode="HTML")
        except TelegramForbiddenError:
            logger.info("handle_start: Bot blocked by user %s during maintenance", telegram_id)
        return

    username = message.from_user.username
    first_name = message.from_user.first_name
    ref_arg = command.args if command and command.args else None

    async with AsyncSessionLocal() as session:
        user, is_new = await get_or_create_user(
            session,
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            referral_code_arg=ref_arg,
        )
        await session.commit()

        if is_new:
            # Let user choose language first
            try:
                await message.answer(
                    t("welcome_choose_lang", "my"),
                    reply_markup=language_selection_kb(),
                )
            except TelegramForbiddenError:
                logger.info("handle_start: New user %s blocked the bot immediately", telegram_id)
                await mark_user_blocked(session, telegram_id)
            return

        lang = user.language_code

        # Check gate
        is_verified, _ = await verify_user_membership(session, user, message.bot)

        if not is_verified:
            await session.commit()
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
                await mark_user_blocked(session, telegram_id)
            return

        # User is verified: render main menu
        text, kb = await render_main_menu(telegram_id, lang, session=session)
        await session.commit()

    welcome_text = f"{t('welcome_title', lang)}\n\n{t('welcome_desc', lang)}\n\n{text}"
    try:
        await message.answer(welcome_text, reply_markup=kb, parse_mode="HTML")
    except TelegramForbiddenError:
        logger.info("handle_start: User %s blocked the bot during welcome render", telegram_id)
        async with AsyncSessionLocal() as session:
            await mark_user_blocked(session, telegram_id)


@start_router.callback_query(F.data.startswith("set_lang:"))
async def handle_set_language(callback: CallbackQuery):
    lang_code = callback.data.split(":")[1]
    if lang_code not in ("my", "en"):
        lang_code = "my"

    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        user.language_code = lang_code

        is_verified, _ = await verify_user_membership(session, user, callback.bot)

        if not is_verified:
            await session.commit()
            text = build_gate_reminder_text(lang_code, user.channel_verified, user.group_verified)
            kb = membership_gate_kb(
                channel_link=settings.channel_invite_link,
                group_link=settings.group_invite_link,
                lang=lang_code,
            )
            await safe_edit_message(callback, text, reply_markup=kb, parse_mode="HTML")
            try:
                await callback.answer()
            except Exception:
                pass
            return

        text, kb = await render_main_menu(callback.from_user.id, lang_code, session=session)
        await session.commit()

    await safe_edit_message(callback, text, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@start_router.callback_query(F.data == "gate:verify")
async def handle_gate_verify(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code

        is_verified, _ = await verify_user_membership(session, user, callback.bot)

        if not is_verified:
            await session.commit()
            # Refresh the reminder in place with the LIVE per-chat status
            # (which chat is still missing) and name it in the alert too,
            # instead of leaving the stale generic gate message behind.
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
            return

        try:
            await callback.answer(t("membership_verified", lang))
        except Exception:
            pass
        text, kb = await render_main_menu(callback.from_user.id, lang, session=session)
        await session.commit()

    await safe_edit_message(callback, text, reply_markup=kb, parse_mode="HTML")


@start_router.message(Command("language", "lang"))
async def handle_language_command(message: Message, session: AsyncSession | None = None):
    if session is not None:
        user, _ = await get_or_create_user(session, message.from_user.id)
        lang = user.language_code
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, message.from_user.id)
            lang = user.language_code

    telegram_id = message.from_user.id
    try:
        await message.answer(
            t("welcome_choose_lang", lang),
            reply_markup=language_selection_kb(),
        )
    except TelegramForbiddenError:
        logger.info("handle_language_command: Bot blocked by user %s", telegram_id)
        if session is not None:
            await mark_user_blocked(session, telegram_id)
        else:
            async with AsyncSessionLocal() as s:
                await mark_user_blocked(s, telegram_id)
