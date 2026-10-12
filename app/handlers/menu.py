from __future__ import annotations

import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.db.session import AsyncSessionLocal
from app.services.i18n import t
from app.services.ledger import get_minute_balance, get_or_create_user, get_referral_stats
from app.ui.keyboards import back_to_menu_kb, main_menu_kb

logger = logging.getLogger(__name__)
menu_router = Router(name="menu")


async def render_main_menu(
    user_id: int,
    lang: str | None = None,
    session: AsyncSession | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    if session is not None:
        user, _ = await get_or_create_user(session, user_id)
        balance = await get_minute_balance(session, user_id)
        active_lang = lang or user.language_code
        is_admin = settings.is_admin(user_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, user_id)
            balance = await get_minute_balance(s, user_id)
            active_lang = lang or user.language_code
            is_admin = settings.is_admin(user_id)

    info_text = t(
        "menu_account_info",
        active_lang,
        category=user.account_category,
        total_min=balance.total_usable_minutes,
        purchased_min=balance.purchased_minutes,
    )
    text = f"{t('menu_title', active_lang)}\n\n{info_text}"
    kb = main_menu_kb(is_admin=is_admin, lang=active_lang)
    return text, kb


@menu_router.callback_query(F.data == "menu:home")
async def handle_menu_home(callback: CallbackQuery):
    text, kb = await render_main_menu(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@menu_router.callback_query(F.data == "menu:refer_earn")
async def handle_menu_refer_earn(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        stats = await get_referral_stats(session, user.id)

    bot_info = await callback.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user.referral_code}"

    text = (
        f"{t('referral_title', lang)}\n\n"
        f"{t('referral_desc', lang, referral_link=ref_link, rewarded=stats['rewarded_referrals'], max_referrals=stats['max_referrals'], earned_min=stats['earned_minutes'], earned_sec=stats['earned_seconds'], remaining_slots=stats['remaining_slots'], remaining_min=stats['remaining_minutes'])}"
    )

    await callback.message.edit_text(text, reply_markup=back_to_menu_kb(lang), parse_mode="HTML")
    await callback.answer()


@menu_router.callback_query(F.data == "menu:tutorial")
async def handle_menu_tutorial(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code

    text = f"{t('tutorial_title', lang)}\n\n{t('tutorial_desc', lang)}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_watch_tutorial", lang), url=settings.tutorial_video_link)],
            [InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home")],
        ]
    )

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@menu_router.message(Command("help"))
async def handle_help_command(message: Message, session: AsyncSession | None = None):
    if session is not None:
        user, _ = await get_or_create_user(session, message.from_user.id)
        lang = user.language_code
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, message.from_user.id)
            lang = user.language_code

    text = f"{t('help_title', lang)}\n\n{t('help_desc', lang)}"
    await message.answer(text, reply_markup=back_to_menu_kb(lang), parse_mode="HTML")
