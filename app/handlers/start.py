from __future__ import annotations

import logging
from aiogram import Router, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import CallbackQuery, Message

from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.db.session import AsyncSessionLocal
from app.handlers.menu import render_main_menu
from app.services.gate import verify_user_membership
from app.services.i18n import t
from app.services.ledger import get_or_create_user
from app.ui.keyboards import language_selection_kb, membership_gate_kb

logger = logging.getLogger(__name__)
start_router = Router(name="start")


@start_router.message(CommandStart())
async def handle_start(message: Message, command: CommandObject):
    telegram_id = message.from_user.id
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
            await message.answer(
                t("welcome_choose_lang", "my"),
                reply_markup=language_selection_kb(),
            )
            return

        lang = user.language_code

        # Check gate
        is_verified, _ = await verify_user_membership(session, user, message.bot)
        await session.commit()

        if not is_verified:
            text = f"{t('membership_gate_title', lang)}\n\n{t('membership_gate_desc', lang)}"
            kb = membership_gate_kb(
                channel_link=settings.channel_invite_link,
                group_link=settings.group_invite_link,
                lang=lang,
            )
            await message.answer(text, reply_markup=kb, parse_mode="HTML")
            return

    # User is verified: render main menu
    text, kb = await render_main_menu(telegram_id, lang)
    welcome_text = f"{t('welcome_title', lang)}\n{t('welcome_desc', lang)}\n\n{text}"
    await message.answer(welcome_text, reply_markup=kb, parse_mode="HTML")


@start_router.callback_query(F.data.startswith("set_lang:"))
async def handle_set_language(callback: CallbackQuery):
    lang_code = callback.data.split(":")[1]
    if lang_code not in ("my", "en"):
        lang_code = "my"

    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        user.language_code = lang_code
        await session.commit()

        is_verified, _ = await verify_user_membership(session, user, callback.bot)
        await session.commit()

        if not is_verified:
            text = f"{t('membership_gate_title', lang_code)}\n\n{t('membership_gate_desc', lang_code)}"
            kb = membership_gate_kb(
                channel_link=settings.channel_invite_link,
                group_link=settings.group_invite_link,
                lang=lang_code,
            )
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            await callback.answer()
            return

    text, kb = await render_main_menu(callback.from_user.id, lang_code)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@start_router.callback_query(F.data == "gate:verify")
async def handle_gate_verify(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code

        is_verified, _ = await verify_user_membership(session, user, callback.bot)
        await session.commit()

        if not is_verified:
            await callback.answer(t("membership_not_verified", lang), show_alert=True)
            return

    await callback.answer(t("membership_verified", lang))
    text, kb = await render_main_menu(callback.from_user.id, lang)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@start_router.message(Command("language", "lang"))
async def handle_language_command(message: Message, session: AsyncSession | None = None):
    if session is not None:
        user, _ = await get_or_create_user(session, message.from_user.id)
        lang = user.language_code
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, message.from_user.id)
            lang = user.language_code

    await message.answer(
        t("welcome_choose_lang", lang),
        reply_markup=language_selection_kb(),
    )
