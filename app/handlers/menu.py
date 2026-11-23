from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.services.i18n import t
from app.services.ledger import (
    get_minute_balance,
    get_or_create_user,
    get_referral_stats,
    grant_daily_free_allowance,
)
from app.ui.keyboards import (
    language_selection_kb,
    main_menu_kb,
    referral_share_kb,
)

logger = logging.getLogger(__name__)
menu_router = Router(name="menu")


def _fmt_number(value: float | int | None) -> str:
    if value is None:
        return "0"
    f = float(value)
    if f.is_integer():
        return str(int(f))
    return f"{f:.1f}"


async def _safe_delete_message(message: Any) -> None:
    if message is None:
        return
    delete_fn = getattr(message, "delete", None)
    if callable(delete_fn):
        try:
            await delete_fn()
        except Exception:
            pass


async def _send_dm(
    callback: CallbackQuery,
    text: str,
    reply_markup: Any = None,
    parse_mode: str = "HTML",
) -> Any:
    msg = getattr(callback, "message", None)
    if msg is not None and callable(getattr(msg, "answer", None)):
        return await msg.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    bot = getattr(callback, "bot", None)
    if bot is not None:
        return await bot.send_message(
            chat_id=callback.from_user.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    return None


async def render_main_menu(
    user_id: int,
    lang: str | None = None,
    session: AsyncSession | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the Round 13 Part K Welcome message + 3-button inline keyboard."""
    if session is not None:
        user, _ = await get_or_create_user(session, user_id)
        await grant_daily_free_allowance(session, user_id)
        balance = await get_minute_balance(session, user_id)
        active_lang = lang or user.language_code or "my"
        is_admin = settings.is_admin(user_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, user_id)
            await grant_daily_free_allowance(s, user_id)
            await s.commit()
            balance = await get_minute_balance(s, user_id)
            active_lang = lang or user.language_code or "my"
            is_admin = settings.is_admin(user_id)

    category_raw = getattr(user.account_category, "value", str(user.account_category)).upper()
    if is_admin:
        plan_label = "👑 SYSTEM ADMIN"
        voice_label = "∞"
        recap_label = "∞"
    else:
        if category_raw == "PURCHASED":
            plan_label = "⭐ VIP"
        else:
            plan_label = t("hist_tier_free", active_lang)
        voice_label = t("welcome_voice_min", active_lang, mins=balance.clone_spendable_minutes)
        recap_val = 3 if getattr(user, "recap_credits", None) is None else user.recap_credits
        recap_label = t("welcome_recap_credits", active_lang, credits=_fmt_number(recap_val))

    text = t(
        "welcome_card",
        active_lang,
        plan=plan_label,
        voice=voice_label,
        recap=recap_label,
    )
    kb = main_menu_kb(lang=active_lang)
    return text, kb


@menu_router.callback_query(F.data == "menu:refer_earn")
async def handle_menu_refer_earn(
    callback: CallbackQuery,
    session: AsyncSession | None = None,
) -> None:
    """Round 13 Part K3b: send a NEW non-ephemeral referral message with Share button."""
    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code or "my"
        stats = await get_referral_stats(session, user.id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code or "my"
            stats = await get_referral_stats(s, user.id)

    if settings.maintenance_mode and not settings.is_admin(callback.from_user.id):
        try:
            await callback.answer(t("maintenance_desc", lang), show_alert=True)
        except Exception:
            pass
        return

    bot_info = await callback.bot.get_me()
    bot_username = getattr(bot_info, "username", None) or "voiceclonepro_bot"
    ref_link = f"https://t.me/{bot_username}?start={user.referral_code}"

    earned_min = _fmt_number(stats.get("earned_minutes", 0))
    invited_n = int(stats.get("rewarded_referrals", 0))
    text = (
        f"{t('refer_card_text', lang, ref_link=ref_link)}\n\n"
        f"{t('refer_card_stats_line', lang, n=invited_n, m=earned_min)}"
    )
    invite_text = t("refer_share_invite_text", lang)
    kb = referral_share_kb(ref_link=ref_link, invite_text=invite_text, lang=lang)

    try:
        await callback.answer()
    except Exception:
        pass
    await _send_dm(callback, text, reply_markup=kb, parse_mode="HTML")


@menu_router.callback_query(F.data == "menu:change_lang")
async def handle_change_language(
    callback: CallbackQuery,
    session: AsyncSession | None = None,
) -> None:
    """Round 13 Part K3: open the language picker (reusing user_lang: callback flow)."""
    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        active_lang = user.language_code or "my"
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            active_lang = user.language_code or "my"

    try:
        await callback.answer()
    except Exception:
        pass
    await _send_dm(
        callback,
        t("welcome_choose_lang", active_lang),
        reply_markup=language_selection_kb(
            active_lang=active_lang,
            show_back=False,
            lang=active_lang,
            callback_prefix="user_lang",
        ),
        parse_mode="HTML",
    )


@menu_router.callback_query(F.data.startswith("user_lang:"))
async def handle_user_language_toggle(
    callback: CallbackQuery,
    session: AsyncSession | None = None,
) -> None:
    """Round 13 Part K4: persist chosen language, delete picker, send fresh Welcome."""
    new_lang = callback.data.split(":", 1)[1] if ":" in (callback.data or "") else "my"
    if new_lang not in ("my", "en"):
        new_lang = "my"

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        already_active = (user.language_code == new_lang)
        user.language_code = new_lang
        await session.commit()
        text, kb = await render_main_menu(callback.from_user.id, new_lang, session=session)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            already_active = (user.language_code == new_lang)
            user.language_code = new_lang
            await s.commit()
            text, kb = await render_main_menu(callback.from_user.id, new_lang, session=s)

    toast_key = "lang_already_active_toast" if already_active else "lang_switched_toast"
    try:
        await callback.answer(t(toast_key, new_lang), show_alert=False)
    except Exception:
        pass
    await _safe_delete_message(callback.message)
    await _send_dm(callback, text, reply_markup=kb, parse_mode="HTML")
