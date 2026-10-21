from __future__ import annotations

import html
import logging
from pathlib import Path
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.models.schema import BotSetting, VoiceModel
from app.db.session import AsyncSessionLocal
from app.services.dynamic_settings import load_dynamic_settings
from app.services.history import (
    _normalize_dt,
    clear_user_history,
    get_models_map_for_jobs,
    get_user_history_jobs,
    get_user_history_ledger_events,
)
from app.services.i18n import t
from app.services.ledger import (
    get_minute_balance,
    get_or_create_user,
    get_referral_stats,
    grant_monthly_free_allowance,
)
from app.services.models import get_available_models_for_user
from app.ui.helpers import safe_answer_callback, safe_edit_message
from app.ui.keyboards import (
    back_to_menu_kb,
    help_settings_kb,
    main_menu_kb,
    user_history_clear_confirm_kb,
    user_history_kb,
    user_history_sub_kb,
    voice_samples_catalog_kb,
)

logger = logging.getLogger(__name__)
menu_router = Router(name="menu")


async def render_main_menu(
    user_id: int,
    lang: str | None = None,
    session: AsyncSession | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    if session is not None:
        user, _ = await get_or_create_user(session, user_id)
        await grant_monthly_free_allowance(session, user_id)
        balance = await get_minute_balance(session, user_id)
        active_lang = lang or user.language_code
        is_admin = settings.is_admin(user_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, user_id)
            await grant_monthly_free_allowance(s, user_id)
            await s.commit()
            balance = await get_minute_balance(s, user_id)
            active_lang = lang or user.language_code
            is_admin = settings.is_admin(user_id)

    category_raw = getattr(user.account_category, "value", str(user.account_category)).upper()
    if is_admin:
        category_badge = f"👑 SYSTEM ADMIN ({category_raw})"
    elif category_raw == "PURCHASED":
        category_badge = "⭐ VIP PURCHASED"
    else:
        category_badge = "🎁 FREE MEMBER" if active_lang == "en" else "🎁 FREE အဖွဲ့ဝင်"

    info_text = t(
        "menu_account_info",
        active_lang,
        category=category_badge,
        total_min=balance.total_usable_minutes,
        purchased_min=balance.purchased_minutes,
        recap_credits=getattr(user, "recap_credits", 3) or 3,
    )
    text = info_text
    kb = main_menu_kb(is_admin=is_admin, lang=active_lang)
    return text, kb


@menu_router.callback_query(F.data == "menu:home")
async def handle_menu_home(callback: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    text, kb = await render_main_menu(callback.from_user.id)
    await safe_edit_message(callback, text, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@menu_router.callback_query(F.data == "menu:change_lang")
async def handle_change_language(callback: CallbackQuery):
    from app.ui.keyboards import language_selection_kb
    await safe_edit_message(
        callback,
        "🌐 ကျေးဇူးပြု၍ အသုံးပြုလိုသော ဘာသာစကားကို ရွေးချယ်ပါ / Please select your preferred language:",
        reply_markup=language_selection_kb(),
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


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

    await safe_edit_message(callback, text, reply_markup=back_to_menu_kb(lang), parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


async def render_help_settings_content(session: AsyncSession, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user, _ = await get_or_create_user(session, user_id)
    lang = user.language_code

    # Automatically reload any persistent Admin settings from DB
    await load_dynamic_settings(session)

    link_row = (await session.execute(select(BotSetting).where(BotSetting.key == "tutorial_video_link"))).scalar_one_or_none()
    current_link = link_row.value if link_row is not None else settings.tutorial_video_link
    en_row = (await session.execute(select(BotSetting).where(BotSetting.key == "tutorial_video_enabled"))).scalar_one_or_none()
    is_enabled = (en_row.value.lower() == "true") if en_row else True

    video_status_note = ""
    if not is_enabled or not current_link:
        video_status_note = f"\n\nℹ️ <i>{t('tutorial_temporarily_unavailable', lang)}</i>"

    free_min_val = settings.free_monthly_allowance_seconds / 60
    free_min_str = f"{free_min_val:.1f}" if free_min_val % 1 != 0 else f"{int(free_min_val)}"

    guide_text = t(
        "help_settings_guide",
        lang,
        free_min=free_min_str,
        free_sec=settings.free_monthly_allowance_seconds,
        char_free=f"{settings.character_limit_free:,}",
        char_purchased=f"{settings.character_limit_purchased:,}",
        daily_limit=settings.free_daily_job_limit,
    )

    text = f"{t('help_settings_title', lang)}\n━━━━━━━━━━━━━━━━━━━\n\n{guide_text}{video_status_note}"
    kb = help_settings_kb(video_link=current_link, is_video_enabled=is_enabled, lang=lang)
    return text, kb


@menu_router.callback_query(F.data.in_({"menu:help_settings", "menu:tutorial"}))
async def handle_menu_help_settings(callback: CallbackQuery, session: AsyncSession | None = None):
    if session is not None:
        text, kb = await render_help_settings_content(session, callback.from_user.id)
    else:
        async with AsyncSessionLocal() as s:
            text, kb = await render_help_settings_content(s, callback.from_user.id)

    await safe_edit_message(callback, text, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


handle_menu_tutorial = handle_menu_help_settings


@menu_router.callback_query(F.data.startswith("user_lang:"))
async def handle_user_language_toggle(callback: CallbackQuery, session: AsyncSession | None = None):
    new_lang = callback.data.split(":")[1]
    if new_lang not in ("my", "en"):
        new_lang = "my"

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        if user.language_code == new_lang:
            try:
                await callback.answer(t("lang_already_active_toast", new_lang), show_alert=False)
            except Exception:
                pass
            return
        user.language_code = new_lang
        await session.commit()
        text, kb = await render_help_settings_content(session, callback.from_user.id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            if user.language_code == new_lang:
                try:
                    await callback.answer(t("lang_already_active_toast", new_lang), show_alert=False)
                except Exception:
                    pass
                return
            user.language_code = new_lang
            await s.commit()
            text, kb = await render_help_settings_content(s, callback.from_user.id)

    try:
        await callback.answer(t("lang_switched_toast", new_lang), show_alert=False)
    except Exception:
        pass
    await safe_edit_message(callback, text, reply_markup=kb, parse_mode="HTML")


@menu_router.message(Command("help"))
async def handle_help_command(message: Message, session: AsyncSession | None = None):
    if session is not None:
        text, kb = await render_help_settings_content(session, message.from_user.id)
    else:
        async with AsyncSessionLocal() as s:
            text, kb = await render_help_settings_content(s, message.from_user.id)

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ---------------------------------------------------------------------------
# User History (Audio Jobs, Minute Inflow & Outflow, Clear History)
# ---------------------------------------------------------------------------

async def render_history_overview_content(session: AsyncSession, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user, _ = await get_or_create_user(session, user_id)
    lang = user.language_code
    balance = await get_minute_balance(session, user_id)

    jobs = await get_user_history_jobs(session, user_id, limit=5)
    events = await get_user_history_ledger_events(session, user_id, limit=5)
    all_jobs = await get_user_history_jobs(session, user_id, limit=50)
    all_events = await get_user_history_ledger_events(session, user_id, limit=50)
    models_map = await get_models_map_for_jobs(session, jobs)

    u_handle = f"@{html.escape(user.username)}" if user.username else f"User {user.id}"
    u_name = html.escape(user.first_name or "")
    user_display = f"{u_name} ({u_handle})" if u_name else u_handle

    cat_raw = getattr(user.account_category, "value", str(user.account_category)).upper()
    tier_badge = "⭐ VIP PURCHASED" if cat_raw == "PURCHASED" else ("🎁 FREE MEMBER" if lang == "en" else "🎁 FREE အဖွဲ့ဝင်")

    total_usable_min = balance.total_usable_minutes
    total_usable_sec = balance.total_usable_seconds
    purchased_min = balance.purchased_minutes
    free_min = balance.monthly_free_minutes + balance.welcome_minutes + balance.referral_minutes
    consumed_min = balance.consumed_seconds / 60.0

    combined = []
    for j in jobs:
        combined.append((j.created_at, "job", j))
    for ev in events:
        combined.append((ev.created_at, "ledger", ev))

    combined.sort(key=lambda x: _normalize_dt(x[0]), reverse=True)
    recent_items = combined[:5]

    activity_lines = []
    if not recent_items:
        activity_lines.append(t("history_empty", lang))
    else:
        for dt, item_type, item in recent_items:
            time_str = dt.strftime("%m-%d %H:%M") if dt else ""
            if item_type == "job":
                j = item
                m = models_map.get(j.model_id) if j.model_id else None
                m_name = html.escape(m.name if m else (j.edge_voice or "Voice Model"))
                st_upper = str(j.status).upper()
                st_icon = "✅" if st_upper == "SUCCESS" else ("❌" if st_upper == "FAILED" else "⚙️")
                dur_sec = j.seconds_consumed if j.seconds_consumed > 0 else j.seconds_estimated
                dur_min = dur_sec / 60.0
                preview = html.escape((j.input_text or "").replace("\n", " ").strip()[:32])
                dur_label = f"{dur_sec}s (-{dur_min:.1f}m)" if lang == "en" else f"{dur_sec}s (-{dur_min:.1f} မိနစ်)"
                job_title = "Voice Generation" if lang == "en" else "အသံထုတ်လုပ်မှု"
                line = (
                    f"• 🎙 <b>{job_title} ({m_name})</b> · <code>{dur_label}</code> {st_icon}\n"
                    f"  🕒 <code>{time_str}</code> | 💬 <i>\"{preview}\"</i>\n"
                )
                activity_lines.append(line)
            else:
                ev = item
                is_credit = (ev.direction == "CREDIT")
                icon = "➕" if is_credit else "➖"
                sign = "+" if is_credit else "-"
                sec = ev.seconds
                min_val = sec / 60.0

                src = ev.source.upper()
                if src == "MONTHLY_FREE":
                    src_title = "Monthly Free Allowance" if lang == "en" else "လစဉ် အခမဲ့မိနစ်"
                elif src == "WELCOME":
                    src_title = "Welcome Bonus" if lang == "en" else "Welcome Bonus ဆုလက်ဆောင်"
                elif src == "REFERRAL":
                    src_title = "Referral Reward" if lang == "en" else "သူငယ်ချင်းဖိတ်ခေါ်မှု ဆုကြေး"
                elif src == "PURCHASE":
                    src_title = "Minute Purchase" if lang == "en" else "မိနစ်ဝယ်ယူမှု အတည်ပြုခြင်း"
                elif src == "CONSUME":
                    src_title = "Voice Clone Usage" if lang == "en" else "အသံထုတ်လုပ်မှု သုံးစွဲချက်"
                elif src == "ADJUST":
                    src_title = "Admin Adjustment" if lang == "en" else "Admin အထူးချိန်ညှိမှု"
                else:
                    src_title = src

                amt_label = f"{sign}{sec}s ({sign}{min_val:.1f}m)" if lang == "en" else f"{sign}{sec}s ({sign}{min_val:.1f} မိနစ်)"
                note_snippet = f" | 📝 <i>{html.escape(ev.note[:30])}</i>" if ev.note else ""
                line = (
                    f"• {icon} <b>{src_title}</b> · <code>{amt_label}</code>\n"
                    f"  🕒 <code>{time_str}</code>{note_snippet}\n"
                )
                activity_lines.append(line)

    activity_content = "".join(activity_lines)

    if lang == "my":
        summary_text = (
            f"👤 <b>အကောင့်:</b> {user_display} ({tier_badge})\n"
            f"⏱ <b>လက်ရှိလက်ကျန်:</b> <b>{total_usable_min:.1f}</b> မိနစ် ({total_usable_sec:,} စက္ကန့်)\n"
            f"💎 <b>ဝယ်ယူထားသော လက်ကျန်:</b> <b>{purchased_min:.1f}</b> မိနစ်\n"
            f"🎁 <b>အခမဲ့/ဆုလက်ဆောင် လက်ကျန်:</b> <b>{free_min:.1f}</b> မိနစ်\n"
            f"📊 <b>သုံးစွဲပြီး မိနစ်စုစုပေါင်း:</b> <b>{consumed_min:.1f}</b> မိနစ်\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 <b>လတ်တလော မှတ်တမ်းများ (Recent Activity):</b>\n\n"
            f"{activity_content}"
        )
    else:
        summary_text = (
            f"👤 <b>Account:</b> {user_display} ({tier_badge})\n"
            f"⏱ <b>Usable Balance:</b> <b>{total_usable_min:.1f}</b> mins ({total_usable_sec:,} seconds)\n"
            f"💎 <b>Purchased Balance:</b> <b>{purchased_min:.1f}</b> mins\n"
            f"🎁 <b>Free / Bonus Balance:</b> <b>{free_min:.1f}</b> mins\n"
            f"📊 <b>Total Consumed:</b> <b>{consumed_min:.1f}</b> mins\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 <b>Recent Activity Stream:</b>\n\n"
            f"{activity_content}"
        )

    text = f"{t('history_overview_title', lang)}\n\n{summary_text}"
    kb = user_history_kb(job_count=len(all_jobs), ledger_count=len(all_events), lang=lang)
    return text, kb


async def render_history_jobs_content(session: AsyncSession, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user, _ = await get_or_create_user(session, user_id)
    lang = user.language_code
    jobs = await get_user_history_jobs(session, user_id, limit=15)
    models_map = await get_models_map_for_jobs(session, jobs)

    lines = [t("history_jobs_title", lang)]
    if not jobs:
        lines.append(f"\n{t('history_empty', lang)}")
    else:
        for j in jobs:
            m = models_map.get(j.model_id) if j.model_id else None
            m_name = html.escape(m.name if m else (j.edge_voice or "Voice Model"))
            st_upper = str(j.status).upper()
            if st_upper == "SUCCESS":
                st_icon = "✅ အောင်မြင်" if lang == "my" else "✅ Success"
            elif st_upper == "FAILED":
                st_icon = "❌ မအောင်မြင်" if lang == "my" else "❌ Failed"
            else:
                st_icon = "⚙️ လုပ်ဆောင်ဆဲ" if lang == "my" else "⚙️ Running"

            dur_sec = j.seconds_consumed if j.seconds_consumed > 0 else j.seconds_estimated
            dur_min = dur_sec / 60.0
            ts = j.created_at.strftime("%Y-%m-%d %H:%M UTC") if j.created_at else ""
            preview = html.escape((j.input_text or "").replace("\n", " ").strip()[:40])

            if lang == "my":
                item_text = (
                    f"• <code>#{j.id[:8]}</code> · <b>{m_name}</b> ({st_icon})\n"
                    f"  ⏱ သုံးစွဲချိန်: <b>{dur_sec}s</b> (~{dur_min:.1f} မိနစ်) | 🕒 <code>{ts}</code>\n"
                    f"  💬 <i>\"{preview}\"</i>\n"
                )
            else:
                item_text = (
                    f"• <code>#{j.id[:8]}</code> · <b>{m_name}</b> ({st_icon})\n"
                    f"  ⏱ Duration: <b>{dur_sec}s</b> (~{dur_min:.1f} mins) | 🕒 <code>{ts}</code>\n"
                    f"  💬 <i>\"{preview}\"</i>\n"
                )
            lines.append(item_text)

    text = "\n".join(lines)
    kb = user_history_sub_kb(current_view="jobs", lang=lang)
    return text, kb


async def render_history_ledger_content(session: AsyncSession, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user, _ = await get_or_create_user(session, user_id)
    lang = user.language_code
    balance = await get_minute_balance(session, user_id)
    events = await get_user_history_ledger_events(session, user_id, limit=15)

    total_usable_min = balance.total_usable_minutes
    total_usable_sec = balance.total_usable_seconds
    consumed_min = balance.consumed_seconds / 60.0

    lines = [t("history_ledger_title", lang)]
    if lang == "my":
        lines.append(
            f"⏱ <b>လက်ရှိ သုံးစွဲနိုင်သော လက်ကျန်:</b> <b>{total_usable_min:.1f}</b> မိနစ် ({total_usable_sec:,} စက္ကန့်)\n"
            f"📊 <b>သုံးစွဲပြီး မိနစ်စုစုပေါင်း:</b> <b>{consumed_min:.1f}</b> မိနစ်\n"
        )
    else:
        lines.append(
            f"⏱ <b>Current Usable Balance:</b> <b>{total_usable_min:.1f}</b> mins ({total_usable_sec:,}s)\n"
            f"📊 <b>Total Consumed:</b> <b>{consumed_min:.1f}</b> mins\n"
        )

    if not events:
        lines.append(f"\n{t('history_empty', lang)}")
    else:
        for ev in events:
            is_credit = (ev.direction == "CREDIT")
            icon = "🟢" if is_credit else "🔴"
            sign = "+" if is_credit else "-"
            sec = ev.seconds
            min_val = sec / 60.0
            ts = ev.created_at.strftime("%Y-%m-%d %H:%M UTC") if ev.created_at else ""

            src = ev.source.upper()
            if src == "MONTHLY_FREE":
                src_title = "Monthly Free Allowance" if lang == "en" else "လစဉ် အခမဲ့မိနစ် ရရှိမှု"
            elif src == "WELCOME":
                src_title = "Welcome Bonus" if lang == "en" else "Welcome Bonus ဆုလက်ဆောင်"
            elif src == "REFERRAL":
                src_title = "Referral Reward" if lang == "en" else "သူငယ်ချင်းဖိတ်ခေါ်မှု ဆုကြေး"
            elif src == "PURCHASE":
                src_title = "Minute Package Purchase" if lang == "en" else "မိနစ်ပက်ကေ့ဂျ် ဝယ်ယူမှု"
            elif src == "CONSUME":
                src_title = "Voice Clone Audio Deduction" if lang == "en" else "အသံထုတ်လုပ်မှု မိနစ်နုတ်ယူချက်"
            elif src == "ADJUST":
                src_title = "Admin Adjustment" if lang == "en" else "Admin အထူးချိန်ညှိမှု"
            else:
                src_title = src

            amt_label = f"{sign}{sec}s ({sign}{min_val:.1f}m)" if lang == "en" else f"{sign}{sec}s ({sign}{min_val:.1f} မိနစ်)"
            note_str = html.escape(ev.note or "None")

            if lang == "my":
                ev_text = (
                    f"• {icon} <b>{src_title}</b>: <code>{amt_label}</code>\n"
                    f"  🕒 ရက်စွဲ: <code>{ts}</code> | 📝 မှတ်ချက်: <i>{note_str}</i>\n"
                )
            else:
                ev_text = (
                    f"• {icon} <b>{src_title}</b>: <code>{amt_label}</code>\n"
                    f"  🕒 Date: <code>{ts}</code> | 📝 Note: <i>{note_str}</i>\n"
                )
            lines.append(ev_text)

    text = "\n".join(lines)
    kb = user_history_sub_kb(current_view="ledger", lang=lang)
    return text, kb


@menu_router.callback_query(F.data == "menu:history")
async def handle_menu_history(callback: CallbackQuery, session: AsyncSession | None = None):
    if session is not None:
        text, kb = await render_history_overview_content(session, callback.from_user.id)
    else:
        async with AsyncSessionLocal() as s:
            text, kb = await render_history_overview_content(s, callback.from_user.id)
    await safe_edit_message(callback, text, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@menu_router.callback_query(F.data == "history:jobs")
async def handle_history_jobs(callback: CallbackQuery, session: AsyncSession | None = None):
    if session is not None:
        text, kb = await render_history_jobs_content(session, callback.from_user.id)
    else:
        async with AsyncSessionLocal() as s:
            text, kb = await render_history_jobs_content(s, callback.from_user.id)
    await safe_edit_message(callback, text, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@menu_router.callback_query(F.data == "history:ledger")
async def handle_history_ledger(callback: CallbackQuery, session: AsyncSession | None = None):
    if session is not None:
        text, kb = await render_history_ledger_content(session, callback.from_user.id)
    else:
        async with AsyncSessionLocal() as s:
            text, kb = await render_history_ledger_content(s, callback.from_user.id)
    await safe_edit_message(callback, text, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@menu_router.callback_query(F.data == "history:clear_prompt")
async def handle_history_clear_prompt(callback: CallbackQuery, session: AsyncSession | None = None):
    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
    lang = user.language_code
    text = t("history_clear_prompt_title", lang)
    kb = user_history_clear_confirm_kb(lang=lang)
    await safe_edit_message(callback, text, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@menu_router.callback_query(F.data == "history:clear_confirm")
async def handle_history_clear_confirm(callback: CallbackQuery, session: AsyncSession | None = None):
    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        await clear_user_history(session, callback.from_user.id)
        text, kb = await render_history_overview_content(session, callback.from_user.id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            await clear_user_history(s, callback.from_user.id)
            text, kb = await render_history_overview_content(s, callback.from_user.id)

    try:
        await callback.answer(t("history_cleared_toast", lang), show_alert=False)
    except Exception:
        pass
    await safe_edit_message(callback, text, reply_markup=kb, parse_mode="HTML")


@menu_router.message(Command("history"))
async def handle_history_command(message: Message, session: AsyncSession | None = None):
    if session is not None:
        text, kb = await render_history_overview_content(session, message.from_user.id)
    else:
        async with AsyncSessionLocal() as s:
            text, kb = await render_history_overview_content(s, message.from_user.id)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Voice Model Samples Catalog (User Audio Previews)
# ---------------------------------------------------------------------------

@menu_router.callback_query(F.data.in_({"menu:voice_samples", "clone:voice_samples"}))
async def handle_voice_samples_catalog(callback: CallbackQuery, session: AsyncSession | None = None):
    user_id = callback.from_user.id
    if session is not None:
        user, _ = await get_or_create_user(session, user_id)
        lang = user.language_code
        models = await get_available_models_for_user(session, user_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, user_id)
            lang = user.language_code
            models = await get_available_models_for_user(s, user_id)

    title = t("voice_samples_title", lang)
    kb = voice_samples_catalog_kb(models, lang=lang, current_user_id=user_id)
    await safe_edit_message(callback, title, reply_markup=kb, parse_mode="HTML")
    await safe_answer_callback(callback)


@menu_router.message(Command("samples", "voices"))
async def handle_samples_command(message: Message, session: AsyncSession | None = None):
    user_id = message.from_user.id
    if session is not None:
        user, _ = await get_or_create_user(session, user_id)
        lang = user.language_code
        models = await get_available_models_for_user(session, user_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, user_id)
            lang = user.language_code
            models = await get_available_models_for_user(s, user_id)

    title = t("voice_samples_title", lang)
    kb = voice_samples_catalog_kb(models, lang=lang, current_user_id=user_id)
    await message.answer(title, reply_markup=kb, parse_mode="HTML")


@menu_router.callback_query(F.data.startswith("sample:play:"))
async def handle_play_voice_sample(callback: CallbackQuery, session: AsyncSession | None = None):
    try:
        model_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "Invalid sample request", show_alert=True)
        return

    user_id = callback.from_user.id
    if session is not None:
        user, _ = await get_or_create_user(session, user_id)
        lang = user.language_code
        model = await session.get(VoiceModel, model_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, user_id)
            lang = user.language_code
            model = await s.get(VoiceModel, model_id)

    if not model:
        await safe_answer_callback(callback, "Model not found", show_alert=True)
        return

    sample_path = model.sample_audio_path
    file_exists = bool(sample_path and Path(sample_path).is_file())
    is_tg_file = bool(
        sample_path
        and (
            sample_path.startswith("AgAC")
            or sample_path.startswith("CQAC")
            or (len(sample_path) > 30 and not sample_path.startswith("/"))
        )
    )

    if not (file_exists or is_tg_file):
        not_avail = t("voice_sample_not_available", lang, model_name=model.name)
        await safe_answer_callback(callback, not_avail, show_alert=True)
        return

    caption = f"🎙️ <b>{html.escape(model.name)}</b> — {t('voice_sample_caption', lang, model_name=model.name)}"
    use_voice_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_use_this_voice", lang), callback_data="menu:voice_clone")],
            [InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home")],
        ]
    )

    try:
        if file_exists:
            p = Path(sample_path)
            audio_input = FSInputFile(str(p), filename=f"{model.name}_sample{p.suffix}")
            if p.suffix.lower() in (".ogg", ".oga"):
                await callback.bot.send_voice(
                    chat_id=callback.from_user.id,
                    voice=audio_input,
                    caption=caption,
                    reply_markup=use_voice_kb,
                    parse_mode="HTML",
                )
            else:
                await callback.bot.send_audio(
                    chat_id=callback.from_user.id,
                    audio=audio_input,
                    caption=caption,
                    title=f"{model.name} Sample",
                    performer="Voice Clone Pro",
                    reply_markup=use_voice_kb,
                    parse_mode="HTML",
                )
        else:
            await callback.bot.send_audio(
                chat_id=callback.from_user.id,
                audio=sample_path,
                caption=caption,
                title=f"{model.name} Sample",
                performer="Voice Clone Pro",
                reply_markup=use_voice_kb,
                parse_mode="HTML",
            )
        toast = t("voice_sample_sent_toast", lang, model_name=model.name)
        await safe_answer_callback(callback, toast, show_alert=False)
    except Exception as exc:
        logger.error("Failed to send sample audio for model %s: %s", model_id, exc)
        await safe_answer_callback(callback, "⚠️ Failed to send audio sample", show_alert=True)

