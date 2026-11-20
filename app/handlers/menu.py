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
    MAX_REFERRALS_REWARDED,
    REFERRAL_REWARD_SECONDS,
    get_minute_balance,
    get_or_create_user,
    get_referral_stats,
    grant_monthly_free_allowance,
)
from app.services.models import get_displayable_models_for_user
from app.ui.helpers import NOT_MODIFIED, safe_answer_callback, safe_edit_message
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


async def _menu_tts_spendable_today(
    session: AsyncSession, user, referral_seconds: int
) -> float:
    """Batch BZ: today's spendable TTS seconds for the main menu —
    the referral bucket plus today's daily-quota remainder, computed
    through the same helpers /api/me and the TTS submit path use
    (``tts_daily_limit_minutes`` + ``tts_cap_consumed_seconds``), so
    the menu figure can never disagree with enforcement. When the
    tier's daily cap is 0 (unlimited) only the referral figure is
    returned — an unbounded remainder has no honest number. Any
    failure falls back to the referral-only figure the menu showed
    before BZ."""
    try:
        from app.services.tts_service import (
            tts_cap_consumed_seconds,
            tts_daily_limit_minutes,
        )

        limit_min = tts_daily_limit_minutes(user, is_admin=False)
        if limit_min <= 0:
            return float(referral_seconds or 0)
        consumed = await tts_cap_consumed_seconds(session, user.id)
        return float(referral_seconds or 0) + max(
            0.0, limit_min * 60 - float(consumed)
        )
    except Exception:
        return float(referral_seconds or 0)


async def render_main_menu(
    user_id: int,
    lang: str | None = None,
    session: AsyncSession | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    if session is not None:
        user, _ = await get_or_create_user(session, user_id)
        await grant_monthly_free_allowance(session, user_id)
        balance = await get_minute_balance(session, user_id)
        _tts_spendable_today = await _menu_tts_spendable_today(
            session, user, balance.referral_seconds or 0
        )
        active_lang = lang or user.language_code
        is_admin = settings.is_admin(user_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, user_id)
            await grant_monthly_free_allowance(s, user_id)
            await s.commit()
            balance = await get_minute_balance(s, user_id)
            _tts_spendable_today = await _menu_tts_spendable_today(
                s, user, balance.referral_seconds or 0
            )
            active_lang = lang or user.language_code
            is_admin = settings.is_admin(user_id)

    category_raw = getattr(user.account_category, "value", str(user.account_category)).upper()
    if is_admin:
        category_badge = "👑 SYSTEM ADMIN"
    elif category_raw == "PURCHASED":
        category_badge = "⭐ VIP"  # R40 canon: user-facing name is VIP
    else:
        category_badge = t("hist_tier_free", active_lang)

    _recap_credits = getattr(user, "recap_credits", None)
    if is_admin:
        # Admin parity (journey audit 2026-11-02): the Mini App header
        # shows ADMIN ∞, but this bot-chat header rendered the admin's
        # underlying FREE grants (3.0 min / 3 credits) — the account
        # line already said SYSTEM ADMIN while the balances disagreed.
        # Admins render the ∞ variant; free/purchased are untouched.
        info_text = t(
            "menu_account_info_admin",
            active_lang,
            category=category_badge,
        )
    else:
        info_text = t(
            "menu_account_info",
            active_lang,
            category=category_badge,
            # Batch AW: the Voice balance line is the clone-spendable
            # figure — referral seconds are TTS-only and get their
            # own labelled line below instead of folding into it.
            total_min=balance.clone_spendable_minutes,
            purchased_min=balance.purchased_minutes,
            recap_credits=3 if _recap_credits is None else _recap_credits,
        )
        # Batch BZ: the TTS line now states today's SPENDABLE TTS
        # (referral + daily-quota remainder) and renders for every
        # non-admin — before BZ it appeared only when the referral
        # bucket was non-empty, so a new FREE user saw no TTS figure
        # at all despite a fresh daily allowance. (The History
        # surfaces keep the referral-only menu_tts_balance_line.)
        info_text += "\n" + t(
            "menu_tts_today_line",
            active_lang,
            tts_min=_tts_spendable_today / 60.0,
            tts_sec=int(round(_tts_spendable_today)),
        )
    text = info_text
    # Bot cleanup 2026-11-03: the membership button renders ONLY while
    # the user is unverified, from the persisted gate flags — the same
    # state verify_user_membership maintains and the Mini App gate
    # reads (no parallel state). Once verified it disappears; if the
    # flags lapse (join-then-leave recheck), it reappears.
    membership_pending = not (
        getattr(user, "channel_verified", False)
        and getattr(user, "group_verified", False)
    )
    kb = main_menu_kb(is_admin=is_admin, lang=active_lang, membership_pending=membership_pending)
    return text, kb


async def _user_lang_or_default(user_id: int) -> str:
    """Best-effort language lookup for the error fallback (Batch BX)."""
    try:
        async with AsyncSessionLocal() as s:
            user_rec, _ = await get_or_create_user(s, user_id)
            return user_rec.language_code or "my"
    except Exception:
        return "my"


async def _answer_callback_first(callback: CallbackQuery) -> None:
    """Stop the button's spinner immediately (Batch BX).

    Old inline buttons (previous Refer/Guide/History messages) made
    the 🏠 button LOOK unresponsive: the handler awaited a full menu
    re-render before answering, so on any slow render the tap
    appeared to do nothing. Answer first, render after.
    """
    try:
        await callback.answer()
    except Exception:
        pass


@menu_router.callback_query(F.data == "menu:home")
async def handle_menu_home(callback: CallbackQuery, state: FSMContext | None = None):
    await _answer_callback_first(callback)
    if state is not None:
        await state.clear()
    try:
        text, kb = await render_main_menu(callback.from_user.id)
        result = await safe_edit_message(
            callback, text, reply_markup=kb, parse_mode="HTML",
            report_unmodified=True,
        )
        if result is NOT_MODIFIED:
            # The tapped message is already visually identical (a menu
            # rendered earlier, often deep in chat history) — an edit
            # is a no-op there, so deliver a FRESH menu at the bottom
            # of the chat where the user is actually looking.
            await callback.bot.send_message(
                callback.from_user.id, text, reply_markup=kb, parse_mode="HTML",
            )
    except Exception:
        logger.exception("menu:home render failed; sending a fallback error")
        try:
            lang = await _user_lang_or_default(callback.from_user.id)
            await callback.bot.send_message(
                callback.from_user.id, t("bot_generic_error", lang), parse_mode="HTML",
            )
        except Exception:
            pass


@menu_router.callback_query(F.data == "menu:change_lang")
async def handle_change_language(callback: CallbackQuery, session: AsyncSession | None = None):
    from app.ui.keyboards import language_selection_kb
    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        active_lang = user.language_code
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            active_lang = user.language_code
    await safe_edit_message(
        callback,
        "🌐 ကျေးဇူးပြု၍ အသုံးပြုလိုသော ဘာသာစကားကို ရွေးချယ်ပါ / Please select your preferred language:",
        reply_markup=language_selection_kb(active_lang=active_lang, show_back=True, lang=active_lang),
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

    # Maintenance: referral rewards are a write surface (hunt C1).
    if settings.maintenance_mode and not settings.is_admin(callback.from_user.id):
        try:
            await callback.answer(t("maintenance_desc", lang), show_alert=True)
        except Exception:
            pass
        return

    bot_info = await callback.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user.referral_code}"

    text = (
        f"{t('referral_title', lang)}\n\n"
        f"{t('referral_desc', lang, referral_link=ref_link, rewarded=stats['rewarded_referrals'], max_referrals=stats['max_referrals'], earned_min=stats['earned_minutes'], earned_sec=stats['earned_seconds'], remaining_slots=stats['remaining_slots'], remaining_min=stats['remaining_minutes'], referral_seconds=REFERRAL_REWARD_SECONDS, referral_cap_min=(MAX_REFERRALS_REWARDED * REFERRAL_REWARD_SECONDS) // 60)}"
    )

    await safe_edit_message(callback, text, reply_markup=back_to_menu_kb(lang), parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


async def render_help_settings_content(
    session: AsyncSession,
    user_id: int,
    section: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
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

    _section_key_map = {
        "voice": "help_sec_voice",
        "recap": "help_sec_recap",
        "downloader": "help_sec_downloader",
        "plans": "help_sec_plans",
        "refer": "help_sec_refer",
        "lang": "help_sec_lang",
    }
    _tpl_key = _section_key_map.get(section or "", "help_menu_summary")

    guide_text = t(
        _tpl_key,
        lang,
        free_min=free_min_str,
        free_sec=settings.free_monthly_allowance_seconds,
        char_free=f"{settings.character_limit_free:,}",
        char_purchased=f"{settings.character_limit_purchased:,}",
        # Batch BX / BZ: keep FREE voice daily clause live from settings.
        voice_daily_free_clause=(
            t(
                "guide_voice_daily_free_capped",
                lang,
                minutes=int(settings.voice_daily_limit_minutes_free or 0),
            )
            if int(settings.voice_daily_limit_minutes_free or 0) > 0
            else t("guide_voice_daily_free_uncapped", lang)
        ),
        voice_daily_paid=settings.voice_daily_limit_minutes_paid,
        tts_daily_free=settings.tts_daily_limit_minutes_free,
        tts_daily_paid=settings.tts_daily_limit_minutes_paid,
        recap_free_min=settings.recap_free_max_duration_seconds // 60,
        vip_recap_min=int(getattr(settings, "recap_max_duration_seconds", 600) or 600) // 60,
        retention_hours=settings.media_retention_days * 24,
        referral_seconds=REFERRAL_REWARD_SECONDS,
        referral_cap_min=(MAX_REFERRALS_REWARDED * REFERRAL_REWARD_SECONDS) // 60,
    )

    text = f"{t('help_settings_title', lang)}\n━━━━━━━━━━━━━━━━━━━\n\n{guide_text}{video_status_note}"
    kb = help_settings_kb(
        video_link=current_link,
        is_video_enabled=is_enabled,
        lang=lang,
        active_section=section if section in _section_key_map else None,
    )
    return text, kb


@menu_router.callback_query(F.data.startswith("help_sec:"))
async def handle_help_section(callback: CallbackQuery, session: AsyncSession | None = None):
    section = callback.data.split(":", 1)[1] if ":" in callback.data else ""
    if session is not None:
        text, kb = await render_help_settings_content(session, callback.from_user.id, section=section)
    else:
        async with AsyncSessionLocal() as s:
            text, kb = await render_help_settings_content(s, callback.from_user.id, section=section)

    await safe_edit_message(callback, text, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


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


# Audit fix G: /help, /history and /samples render private per-user
# content (worst case, /history posts the caller's balance, jobs and
# ledger overview) — they must never run in a group chat, so all three
# are private-only (same F.chat.type pattern as app/handlers/payment.py).
@menu_router.message(Command("help"), F.chat.type == "private")
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

async def render_history_overview_content(
    session: AsyncSession,
    user_id: int,
    tg_user: Any = None,
) -> tuple[str, InlineKeyboardMarkup]:
    user, _ = await get_or_create_user(session, user_id)
    lang = user.language_code
    is_admin = settings.is_admin(user_id)
    balance = await get_minute_balance(session, user_id)

    jobs = await get_user_history_jobs(session, user_id, limit=5)
    events = await get_user_history_ledger_events(session, user_id, limit=5)
    all_jobs = await get_user_history_jobs(session, user_id, limit=50)
    all_events = await get_user_history_ledger_events(session, user_id, limit=50)
    models_map = await get_models_map_for_jobs(session, jobs)

    tg_username = getattr(tg_user, "username", None) or user.username
    u_handle = f"@{html.escape(tg_username)}" if tg_username else f"User {user.id}"
    raw_first = (user.first_name or "").strip()
    if not raw_first or raw_first.lower() == "voice clone pro":
        raw_first = (getattr(tg_user, "first_name", None) or "").strip()
    if raw_first.lower() == "voice clone pro":
        raw_first = ""
    u_name = html.escape(raw_first)
    user_display = f"{u_name} ({u_handle})" if u_name else u_handle

    cat_raw = getattr(user.account_category, "value", str(user.account_category)).upper()
    if is_admin:
        tier_badge = "👑 SYSTEM ADMIN"
    elif cat_raw == "PURCHASED":
        tier_badge = "⭐ VIP"
    else:
        tier_badge = t("hist_tier_free", lang)

    # Batch AW: the overview totals are the CLONE-SPENDABLE balance;
    # referral seconds are TTS-only and are neither folded into the
    # free/bonus line nor the headline total — they get their own
    # labelled line under the summary when the bucket is non-empty.
    total_usable_min = balance.clone_spendable_minutes
    total_usable_sec = balance.clone_spendable_seconds
    purchased_min = balance.purchased_minutes
    free_min = balance.monthly_free_minutes + balance.welcome_minutes
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
                dur_label = t("hist_duration", lang, dur_sec=dur_sec, dur_min=dur_min)
                job_title = t("hist_job_title", lang)
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
                _src_keys = {
                    "MONTHLY_FREE": "hist_source_monthly",
                    "WELCOME": "hist_source_welcome",
                    "REFERRAL": "hist_source_referral",
                    "PURCHASE": "hist_source_purchase_overview",
                    "CONSUME": "hist_source_consume_overview",
                    "ADJUST": "hist_source_admin",
                }
                src_title = t(_src_keys[src], lang) if src in _src_keys else src
                amt_label = t("hist_amount", lang, sign=sign, sec=sec, min_val=min_val)
                note_snippet = f" | 📝 <i>{html.escape(ev.note[:30])}</i>" if ev.note else ""
                line = (
                    f"• {icon} <b>{src_title}</b> · <code>{amt_label}</code>\n"
                    f"  🕒 <code>{time_str}</code>{note_snippet}\n"
                )
                activity_lines.append(line)

    activity_content = "".join(activity_lines)

    if is_admin:
        summary_text = t(
            "hist_overview_summary_admin",
            lang,
            user_display=user_display,
            tier_badge=tier_badge,
            consumed_min=consumed_min,
            activity_content=activity_content,
        )
    else:
        summary_text = t(
            "hist_overview_summary",
            lang,
            user_display=user_display,
            tier_badge=tier_badge,
            total_min=total_usable_min,
            total_sec=total_usable_sec,
            purchased_min=purchased_min,
            free_min=free_min,
            consumed_min=consumed_min,
            activity_content=activity_content,
        )
        if (balance.referral_seconds or 0) > 0:
            summary_text += "\n" + t(
                "menu_tts_balance_line",
                lang,
                tts_min=balance.referral_seconds / 60.0,
                tts_sec=balance.referral_seconds,
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
                st_icon = t("hist_status_success", lang)
            elif st_upper == "FAILED":
                st_icon = t("hist_status_failed", lang)
            else:
                st_icon = t("hist_status_running", lang)

            dur_sec = j.seconds_consumed if j.seconds_consumed > 0 else j.seconds_estimated
            dur_min = dur_sec / 60.0
            ts = j.created_at.strftime("%Y-%m-%d %H:%M UTC") if j.created_at else ""
            preview = html.escape((j.input_text or "").replace("\n", " ").strip()[:40])

            item_text = t(
                "hist_job_item",
                lang,
                job_id=j.id,
                model_name=m_name,
                st_icon=st_icon,
                dur_sec=dur_sec,
                dur_min=dur_min,
                ts=ts,
                preview=preview,
            )
            lines.append(item_text)

    text = "\n".join(lines)
    kb = user_history_sub_kb(current_view="jobs", lang=lang)
    return text, kb


async def render_history_ledger_content(session: AsyncSession, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user, _ = await get_or_create_user(session, user_id)
    lang = user.language_code
    is_admin = settings.is_admin(user_id)
    balance = await get_minute_balance(session, user_id)
    events = await get_user_history_ledger_events(session, user_id, limit=15)

    # Batch AW: headline total is clone-spendable (see overview note);
    # the referral/TTS bucket is labelled separately when non-empty.
    total_usable_min = balance.clone_spendable_minutes
    total_usable_sec = balance.clone_spendable_seconds
    consumed_min = balance.consumed_seconds / 60.0

    lines = [t("history_ledger_title", lang)]
    if is_admin:
        lines.append(
            t(
                "hist_ledger_header_admin",
                lang,
                consumed_min=consumed_min,
            )
        )
    else:
        lines.append(
            t(
                "hist_ledger_header",
                lang,
                total_min=total_usable_min,
                total_sec=total_usable_sec,
                consumed_min=consumed_min,
            )
        )
        if (balance.referral_seconds or 0) > 0:
            lines.append(
                t(
                    "menu_tts_balance_line",
                    lang,
                    tts_min=balance.referral_seconds / 60.0,
                    tts_sec=balance.referral_seconds,
                )
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
            _src_keys = {
                "MONTHLY_FREE": "hist_source_monthly_ledger",
                "WELCOME": "hist_source_welcome",
                "REFERRAL": "hist_source_referral",
                "PURCHASE": "hist_source_purchase_ledger",
                "CONSUME": "hist_source_consume_ledger",
                "ADJUST": "hist_source_admin",
            }
            src_title = t(_src_keys[src], lang) if src in _src_keys else src
            amt_label = t("hist_amount", lang, sign=sign, sec=sec, min_val=min_val)
            note_str = html.escape(ev.note or "None")

            ev_text = t(
                "hist_ledger_item",
                lang,
                icon=icon,
                src_title=src_title,
                amt_label=amt_label,
                ts=ts,
                note_str=note_str,
            )
            lines.append(ev_text)

    text = "\n".join(lines)
    kb = user_history_sub_kb(current_view="ledger", lang=lang)
    return text, kb


@menu_router.callback_query(F.data == "menu:history")
async def handle_menu_history(callback: CallbackQuery, session: AsyncSession | None = None):
    # Batch BX: same defensive shape as handle_menu_home.
    await _answer_callback_first(callback)
    try:
        if session is not None:
            text, kb = await render_history_overview_content(session, callback.from_user.id, tg_user=callback.from_user)
        else:
            async with AsyncSessionLocal() as s:
                text, kb = await render_history_overview_content(s, callback.from_user.id, tg_user=callback.from_user)
        result = await safe_edit_message(
            callback, text, reply_markup=kb, parse_mode="HTML",
            report_unmodified=True,
        )
        if result is NOT_MODIFIED:
            await callback.bot.send_message(
                callback.from_user.id, text, reply_markup=kb, parse_mode="HTML",
            )
    except Exception:
        logger.exception("menu:history render failed; sending a fallback error")
        try:
            lang = await _user_lang_or_default(callback.from_user.id)
            await callback.bot.send_message(
                callback.from_user.id, t("bot_generic_error", lang), parse_mode="HTML",
            )
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


# Audit fix G: private-only — see the /help filter above.
@menu_router.message(Command("history"), F.chat.type == "private")
async def handle_history_command(message: Message, session: AsyncSession | None = None):
    if session is not None:
        text, kb = await render_history_overview_content(session, message.from_user.id, tg_user=message.from_user)
    else:
        async with AsyncSessionLocal() as s:
            text, kb = await render_history_overview_content(s, message.from_user.id, tg_user=message.from_user)
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
        models = await get_displayable_models_for_user(session, user_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, user_id)
            lang = user.language_code
            models = await get_displayable_models_for_user(s, user_id)

    title = t("voice_samples_title", lang)
    kb = voice_samples_catalog_kb(models, lang=lang, current_user_id=user_id)
    await safe_edit_message(callback, title, reply_markup=kb, parse_mode="HTML")
    await safe_answer_callback(callback)


# Audit fix G: private-only — see the /help filter above.
@menu_router.message(Command("samples", "voices"), F.chat.type == "private")
async def handle_samples_command(message: Message, session: AsyncSession | None = None):
    user_id = message.from_user.id
    if session is not None:
        user, _ = await get_or_create_user(session, user_id)
        lang = user.language_code
        models = await get_displayable_models_for_user(session, user_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, user_id)
            lang = user.language_code
            models = await get_displayable_models_for_user(s, user_id)

    title = t("voice_samples_title", lang)
    kb = voice_samples_catalog_kb(models, lang=lang, current_user_id=user_id)
    await message.answer(title, reply_markup=kb, parse_mode="HTML")


@menu_router.callback_query(F.data.startswith("sample:play:"))
async def handle_play_voice_sample(callback: CallbackQuery, session: AsyncSession | None = None):
    try:
        model_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        _lang = "my"
        try:
            if session is not None:
                _u, _ = await get_or_create_user(session, callback.from_user.id)
                _lang = _u.language_code
            else:
                async with AsyncSessionLocal() as _s:
                    _u, _ = await get_or_create_user(_s, callback.from_user.id)
                    _lang = _u.language_code
        except Exception:
            pass
        await safe_answer_callback(callback, t("sample_invalid_request", _lang), show_alert=True)
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
        await safe_answer_callback(callback, t("sample_model_not_found", lang), show_alert=True)
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
        await safe_answer_callback(callback, t("sample_send_failed", lang), show_alert=True)

